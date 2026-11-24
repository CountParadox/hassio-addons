"""Where captured images and rendered videos are written.

Two kinds of destination:
  smb    - a UNC path such as \\\\nas\\timelapse\\site1, written directly over SMB2/3
           with smbprotocol (no kernel mount, so the add-on needs no extra privileges)
  local  - a folder inside the container, e.g. /share/panalapse or /media/timelapse
           (Home Assistant's own network storage mounts appear under /share and /media)
"""
from __future__ import annotations

import asyncio
import os
import posixpath
import threading
from dataclasses import dataclass
from datetime import timezone
from typing import Iterator

import smbclient
import smbclient.path

_smb_lock = threading.Lock()
_registered: set[tuple] = set()


def reset_sessions():
    """Forget cached SMB connections, e.g. after a destination's credentials change."""
    with _smb_lock:
        _registered.clear()
        try:
            smbclient.reset_connection_cache()
        except Exception:   # nothing cached / already closed
            pass


def parse_unc(unc: str) -> tuple[str, str, str]:
    """'\\\\server\\share\\a\\b' or '//server/share/a/b' -> (server, share, 'a\\b')."""
    p = unc.strip().replace("/", "\\").lstrip("\\")
    parts = [x for x in p.split("\\") if x]
    if len(parts) < 2:
        raise ValueError("UNC path must look like \\\\server\\share[\\folder]")
    return parts[0], parts[1], "\\".join(parts[2:])


@dataclass
class Destination:
    id: str
    name: str
    type: str                # "smb" | "local"
    path: str                # UNC path or local folder
    username: str = ""
    password: str = ""
    domain: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Destination":
        return cls(id=d["id"], name=d.get("name") or d["id"], type=d.get("type", "smb"),
                   path=d.get("path", ""), username=d.get("username", ""),
                   password=d.get("password", ""), domain=d.get("domain", ""))

    # ------------------------------------------------------------- helpers
    def _smb_session(self):
        server, _, _ = parse_unc(self.path)
        user = self.username
        if self.domain and user and "\\" not in user and "@" not in user:
            user = f"{self.domain}\\{user}"
        key = (server, user, self.password)
        with _smb_lock:
            if key not in _registered:
                smbclient.register_session(server, username=user or None,
                                           password=self.password or None,
                                           connection_timeout=15)
                _registered.add(key)

    def full(self, rel: str = "") -> str:
        rel = rel.replace("\\", "/").strip("/")
        if self.type == "smb":
            server, share, sub = parse_unc(self.path)
            parts = [p for p in [sub] + rel.split("/") if p]
            return "\\\\" + "\\".join([server, share] + [p.replace("/", "\\") for p in parts])
        return posixpath.join(self.path, rel) if rel else self.path

    # ------------------------------------------------------------- sync ops
    def _write(self, rel: str, data: bytes):
        target = self.full(rel)
        if self.type == "smb":
            self._smb_session()
            parent = target.rsplit("\\", 1)[0]
            smbclient.makedirs(parent, exist_ok=True)
            with smbclient.open_file(target, mode="wb") as f:
                f.write(data)
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, target)

    def _upload_file(self, rel: str, local_path: str):
        target = self.full(rel)
        if self.type == "smb":
            self._smb_session()
            smbclient.makedirs(target.rsplit("\\", 1)[0], exist_ok=True)
            with open(local_path, "rb") as src, smbclient.open_file(target, mode="wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(local_path, "rb") as src, open(target, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)

    def _read(self, rel: str) -> bytes:
        target = self.full(rel)
        if self.type == "smb":
            self._smb_session()
            with smbclient.open_file(target, mode="rb") as f:
                return f.read()
        with open(target, "rb") as f:
            return f.read()

    def walk_images(self, rel: str) -> Iterator[tuple[str, float]]:
        """Yield (relative path, mtime) for every .jpg under rel, recursively."""
        root = self.full(rel)
        exts = (".jpg", ".jpeg")
        if self.type == "smb":
            self._smb_session()
            base_len = len(self.full(""))
            # scandir returns times with the directory listing, so there's no
            # extra round trip per file (which matters with thousands of frames)
            stack = [root]
            while stack:
                d = stack.pop()
                try:
                    entries = list(smbclient.scandir(d))
                except FileNotFoundError:
                    continue
                for e in entries:
                    info = e.smb_info
                    if info.file_attributes & 0x10:          # FILE_ATTRIBUTE_DIRECTORY
                        stack.append(d + "\\" + e.name)
                    elif e.name.lower().endswith(exts):
                        t = info.last_write_time
                        if t.tzinfo is None:
                            t = t.replace(tzinfo=timezone.utc)
                        yield (d + "\\" + e.name)[base_len:].lstrip("\\").replace("\\", "/"), t.timestamp()
        else:
            if not os.path.isdir(root):
                return
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    if fn.lower().endswith(exts):
                        full = os.path.join(dirpath, fn)
                        yield os.path.relpath(full, self.full("")).replace(os.sep, "/"), os.path.getmtime(full)

    def _test(self) -> str:
        probe = ".panalapse_write_test"
        self._write(probe, b"ok")
        if self.type == "smb":
            smbclient.remove(self.full(probe))
        else:
            os.remove(self.full(probe))
        return f"Wrote and removed a test file in {self.full('')}"

    # ------------------------------------------------------------- async API
    async def write(self, rel: str, data: bytes):
        await asyncio.to_thread(self._write, rel, data)

    async def upload_file(self, rel: str, local_path: str):
        await asyncio.to_thread(self._upload_file, rel, local_path)

    async def read(self, rel: str) -> bytes:
        return await asyncio.to_thread(self._read, rel)

    async def test(self) -> str:
        return await asyncio.to_thread(self._test)
