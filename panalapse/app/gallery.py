"""Browse the stills a timelapse has saved, straight from its storage.

Listings are cached for a short while (walking a share with thousands of files
takes a few seconds) and new captures are added to the cache as they happen.
Thumbnails are made with Pillow and kept in /data/cache/thumbs, which is
trimmed to a fixed size.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import time
from datetime import datetime

from PIL import Image

from .store import DATA_DIR

log = logging.getLogger(__name__)
THUMB_DIR = os.path.join(DATA_DIR, "cache", "thumbs")
THUMB_CACHE_MAX = 300 * 1024 * 1024
LIST_TTL = 60.0
PER_SHOT = re.compile(r"\{(date|year|month|day|hour|time|datetime|seq)")


def job_root(job: dict, camera: dict | None) -> str:
    """The part of the folder template that's the same for every shot."""
    parts = [p for p in (job.get("folder_template") or "").replace("\\", "/").split("/") if p]
    keep = []
    for p in parts:
        if PER_SHOT.search(p):
            break
        keep.append(p.replace("{job}", job.get("name", "job"))
                     .replace("{camera}", (camera or {}).get("name", "camera")))
    root = "/".join(keep)
    return re.sub(r'[<>:"|?*\x00-\x1f]', "_", root)


class Gallery:
    def __init__(self, app):
        self.app = app
        self._cache: dict[str, dict] = {}      # job id -> {key, at, items: {rel: ts}}
        self._locks: dict[str, asyncio.Lock] = {}
        self._thumb_writes = 0

    def _key(self, job: dict) -> tuple:
        cam = self.app.store.get("cameras", job.get("camera_id", ""))
        return (job.get("destination_id"), job_root(job, cam))

    async def items(self, job: dict, refresh: bool = False) -> dict[str, float]:
        """rel path -> modified time for every image under the job's folder."""
        jid = job["id"]
        key = self._key(job)
        c = self._cache.get(jid)
        if c and c["key"] == key and not refresh and time.monotonic() - c["at"] < LIST_TTL:
            return c["items"]
        async with self._locks.setdefault(jid, asyncio.Lock()):
            c = self._cache.get(jid)
            if c and c["key"] == key and not refresh and time.monotonic() - c["at"] < LIST_TTL:
                return c["items"]
            dest = self.app.destination(job.get("destination_id"))
            if not dest:
                raise ValueError("This timelapse has no storage set")
            root = key[1]
            files = await asyncio.to_thread(lambda: list(dest.walk_images(root)))
            items = {rel: ts for rel, ts in files if "/videos/" not in f"/{rel}"}
            self._cache[jid] = {"key": key, "at": time.monotonic(), "items": items}
            return items

    def added(self, job: dict, rel: str, when: float):
        """A capture was just saved: keep the cached listing current."""
        c = self._cache.get(job["id"])
        if c and c["key"] == self._key(job):
            c["items"][rel] = when

    async def days(self, job: dict, refresh=False) -> list[dict]:
        out: dict[str, list] = {}
        for ts in (await self.items(job, refresh)).values():
            d = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            e = out.setdefault(d, [0, ts, ts])
            e[0] += 1
            e[1], e[2] = min(e[1], ts), max(e[2], ts)
        return [{"day": d, "count": v[0],
                 "first": datetime.fromtimestamp(v[1]).strftime("%H:%M"),
                 "last": datetime.fromtimestamp(v[2]).strftime("%H:%M")}
                for d, v in sorted(out.items(), reverse=True)]

    async def images(self, job: dict, day: str) -> list[dict]:
        items = await self.items(job)
        sel = []
        for rel, ts in items.items():
            dt = datetime.fromtimestamp(ts)
            if dt.strftime("%Y-%m-%d") == day:
                sel.append((ts, rel, dt))
        sel.sort()
        return [{"p": rel, "t": dt.strftime("%H:%M:%S")} for _ts, rel, dt in sel]

    async def check_path(self, job: dict, rel: str) -> str:
        rel = rel.replace("\\", "/").lstrip("/")
        root = self._key(job)[1]
        if ".." in rel.split("/") or not rel.lower().endswith((".jpg", ".jpeg")):
            raise PermissionError("Not an image path")
        if root and not rel.startswith(root + "/"):
            raise PermissionError("That image isn't in this timelapse's folder")
        return rel

    async def read(self, job: dict, rel: str) -> bytes:
        rel = await self.check_path(job, rel)
        return await self.app.destination(job.get("destination_id")).read(rel)

    async def thumb(self, job: dict, rel: str, width: int = 320) -> bytes:
        rel = await self.check_path(job, rel)
        h = hashlib.sha1(f"{job.get('destination_id')}|{rel}|{width}".encode()).hexdigest()
        path = os.path.join(THUMB_DIR, h[:2], h + ".jpg")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        data = await self.app.destination(job.get("destination_id")).read(rel)
        out = await asyncio.to_thread(make_thumb, data, width)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".part", "wb") as f:
            f.write(out)
        os.replace(path + ".part", path)
        self._thumb_writes += 1
        if self._thumb_writes % 500 == 0:
            asyncio.get_running_loop().run_in_executor(None, prune_thumbs)
        return out


def make_thumb(data: bytes, width: int) -> bytes:
    im = Image.open(io.BytesIO(data))
    im.draft("RGB", (width, width))          # fast JPEG downscale while decoding
    im = im.convert("RGB")
    im.thumbnail((width, width * 4))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=78, optimize=True)
    return buf.getvalue()


def prune_thumbs(limit: int = THUMB_CACHE_MAX):
    """Delete the least recently made thumbnails once the cache is over its size."""
    files = []
    total = 0
    for dirpath, _d, names in os.walk(THUMB_DIR):
        for n in names:
            p = os.path.join(dirpath, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
            total += st.st_size
    if total <= limit:
        return
    files.sort()
    for _m, size, p in files:
        try:
            os.remove(p)
        except OSError:
            pass
        total -= size
        if total <= limit * 0.8:
            break
    log.info("Thumbnail cache trimmed to %.0f MB", total / 1e6)
