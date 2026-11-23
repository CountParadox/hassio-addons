"""Turn a folder of captured stills into an MP4 with ffmpeg.

Frames are streamed from the destination (SMB or local) straight into ffmpeg's
stdin, so no copy of the image set is needed on the Home Assistant host.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime

from .panasonic import jpeg_size

log = logging.getLogger(__name__)
VIDEO_DIR = os.path.join(os.environ.get("DATA_DIR", "/data"), "videos")


class Render:
    def __init__(self, rid: str, params: dict):
        self.id = rid
        self.params = params
        self.status = "queued"     # queued | listing | encoding | uploading | done | error | cancelled
        self.total = 0
        self.done = 0
        self.message = ""
        self.output: str | None = None
        self.uploaded_to: str | None = None
        self.started = datetime.now()
        self.proc: asyncio.subprocess.Process | None = None
        self.cancel_requested = False

    def to_dict(self):
        return {"id": self.id, "status": self.status, "total": self.total, "done": self.done,
                "message": self.message, "output": os.path.basename(self.output) if self.output else None,
                "uploaded_to": self.uploaded_to, "started": self.started.isoformat(timespec="seconds"),
                "name": self.params.get("name")}


class Renderer:
    def __init__(self, app):
        self.app = app
        self.renders: dict[str, Render] = {}
        self._queue: asyncio.Queue[Render] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def start(self):
        os.makedirs(VIDEO_DIR, exist_ok=True)
        self._task = asyncio.create_task(self._worker())

    def submit(self, params: dict) -> Render:
        rid = datetime.now().strftime("%Y%m%d%H%M%S%f")
        r = Render(rid, params)
        self.renders[rid] = r
        self._queue.put_nowait(r)
        return r

    def cancel(self, rid: str):
        r = self.renders.get(rid)
        if r:
            r.cancel_requested = True
            if r.proc and r.proc.returncode is None:
                r.proc.kill()
            if r.status == "queued":
                r.status = "cancelled"

    @staticmethod
    def list_videos():
        if not os.path.isdir(VIDEO_DIR):
            return []
        out = []
        for fn in sorted(os.listdir(VIDEO_DIR), reverse=True):
            if fn.endswith(".mp4"):
                p = os.path.join(VIDEO_DIR, fn)
                out.append({"name": fn, "size": os.path.getsize(p),
                            "modified": datetime.fromtimestamp(os.path.getmtime(p)).isoformat(timespec="seconds")})
        return out

    async def _worker(self):
        while True:
            r = await self._queue.get()
            if r.cancel_requested:
                continue
            try:
                await self._run(r)
            except Exception as e:
                log.exception("render failed")
                r.status = "error"
                r.message = str(e) or type(e).__name__

    async def _run(self, r: Render):
        p = r.params
        dest = self.app.destination(p.get("destination_id"))
        if not dest:
            raise ValueError("Choose a storage destination")
        folder = (p.get("folder") or "").strip("/\\")
        fps = max(1, min(120, int(p.get("fps") or 25)))
        nth = max(1, int(p.get("every_nth") or 1))
        width = int(p.get("width") or 0)
        crf = max(12, min(40, int(p.get("crf") or 20)))
        d_from = p.get("date_from") or ""
        d_to = p.get("date_to") or ""

        r.status = "listing"
        r.message = f"Listing images in {dest.full(folder)}"
        files = await asyncio.to_thread(lambda: list(dest.walk_images(folder)))
        # skip anything in a videos/ folder, filter by modified date, sort by path (names carry the timestamp)
        sel = []
        for rel, mtime in files:
            if "/videos/" in f"/{rel}":
                continue
            day = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
            if (d_from and day < d_from) or (d_to and day > d_to):
                continue
            sel.append(rel)
        sel.sort()
        sel = sel[::nth]
        if not sel:
            raise ValueError("No .jpg images found for that folder / date range")
        r.total = len(sel)

        first = await dest.read(sel[0])
        size = jpeg_size(first) or (1920, 1080)
        out_w = width or size[0]
        out_w -= out_w % 2
        out_h = round(out_w * size[1] / size[0])
        out_h -= out_h % 2

        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", p.get("name") or "timelapse").strip("_") or "timelapse"
        out_name = f"{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
        out_path = os.path.join(VIDEO_DIR, out_name)

        # JPEGs are full range; convert to standard (TV) range so players show it correctly.
        vf = [f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:in_range=pc:out_range=tv",
              f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2", "format=yuv420p"]
        if p.get("deflicker"):
            vf.insert(0, "deflicker=size=10:mode=pm")
        cmd = [os.environ.get("FFMPEG", "ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg", "-i", "-",
               "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
               "-pix_fmt", "yuv420p", "-color_range", "tv", "-movflags", "+faststart", out_path]
        r.status = "encoding"
        r.message = f"Encoding {r.total} frames at {out_w}x{out_h}, {fps} fps"
        r.proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE,
                                                      stderr=asyncio.subprocess.PIPE)
        stderr_task = asyncio.create_task(r.proc.stderr.read())
        try:
            data = first
            for i, rel in enumerate(sel):
                if r.cancel_requested:
                    raise asyncio.CancelledError
                if i > 0:
                    try:
                        data = await dest.read(rel)
                    except OSError as e:
                        log.warning("skipping %s: %s", rel, e)
                        continue
                r.proc.stdin.write(data)
                await r.proc.stdin.drain()
                r.done = i + 1
            r.proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
            pass
        rc = await r.proc.wait()
        err = (await stderr_task).decode(errors="replace").strip()
        if r.cancel_requested:
            r.status = "cancelled"
            r.message = "Cancelled"
            if os.path.exists(out_path):
                os.remove(out_path)
            return
        if rc != 0:
            raise RuntimeError(f"ffmpeg failed ({rc}): {err[-500:]}")
        r.output = out_path
        if p.get("upload", True):
            r.status = "uploading"
            rel = f"{folder}/videos/{out_name}" if folder else f"videos/{out_name}"
            r.message = f"Copying video to {dest.full(rel)}"
            await dest.upload_file(rel, out_path)
            r.uploaded_to = dest.full(rel)
        r.status = "done"
        r.message = f"{r.total} frames → {out_name}"
