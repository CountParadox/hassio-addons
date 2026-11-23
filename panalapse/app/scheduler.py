"""Works out when each job is due, and captures frames."""
from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import date, datetime, time as dtime, timedelta

from .panasonic import CONTROLS, CameraError

log = logging.getLogger(__name__)


def _parse_time(s: str) -> dtime:
    try:
        h, m = (int(x) for x in (s or "00:00").split(":")[:2])
        return dtime(h % 24, m % 60)
    except ValueError:
        return dtime(0, 0)


def _parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def next_capture(job: dict, now: datetime) -> datetime | None:
    """First capture time >= now that fits the job's days / hours / dates."""
    interval = max(1, int(job.get("interval_seconds") or 60))
    days = set(job.get("days") or [])
    if not days:
        return None
    t_start, t_end = _parse_time(job.get("start_time")), _parse_time(job.get("end_time"))
    d_from, d_to = _parse_date(job.get("start_date")), _parse_date(job.get("end_date"))

    # A window belongs to the day it starts on; start the search a day back so
    # an overnight window that began yesterday is still found.
    for offset in range(-1, 9):
        d = now.date() + timedelta(days=offset)
        if d.weekday() not in days:
            continue
        if (d_from and d < d_from) or (d_to and d > d_to):
            continue
        start = datetime.combine(d, t_start)
        end = datetime.combine(d, t_end)
        if end <= start:          # equal = 24 h, earlier = runs past midnight
            end += timedelta(days=1)
        if now <= start:
            return start
        if now <= end:
            steps = math.ceil((now - start).total_seconds() / interval)
            cand = start + timedelta(seconds=steps * interval)
            if cand <= end:      # the end time itself is included
                return cand
    return None


def render_template(tpl: str, job: dict, camera: dict, when: datetime, seq: int) -> str:
    values = {
        "job": job.get("name", "job"),
        "camera": camera.get("name", "camera"),
        "date": when.strftime("%Y-%m-%d"),
        "time": when.strftime("%H%M%S"),
        "datetime": when.strftime("%Y%m%d_%H%M%S"),
        "year": when.strftime("%Y"), "month": when.strftime("%m"), "day": when.strftime("%d"),
        "hour": when.strftime("%H"),
        "seq": seq,
    }

    def sub(m: re.Match) -> str:
        key, _, fmt = m.group(1).partition(":")
        if key not in values:
            return m.group(0)
        v = values[key]
        try:
            return format(v, fmt) if fmt else str(v)
        except (ValueError, TypeError):
            return str(v)

    out = re.sub(r"\{([a-z]+(?::[^}]*)?)\}", sub, tpl)
    # Keep paths safe on SMB/Windows shares.
    return re.sub(r'[<>:"|?*\x00-\x1f]', "_", out).replace("\\", "/")


class JobState:
    def __init__(self):
        self.next_due: datetime | None = None
        self.last_capture: datetime | None = None
        self.last_path: str | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.last_warning: str | None = None     # settings the camera refused
        self.last_image: bytes | None = None
        self.running = False
        self.session_count = 0


class Scheduler:
    def __init__(self, app):
        self.app = app          # main.App (store, cameras, destinations)
        self.state: dict[str, JobState] = {}
        self._task: asyncio.Task | None = None

    def st(self, job_id: str) -> JobState:
        return self.state.setdefault(job_id, JobState())

    def reschedule(self, job_id: str | None = None):
        now = datetime.now()
        for job in self.app.store.data["jobs"]:
            if job_id is None or job["id"] == job_id:
                self.st(job["id"]).next_due = next_capture(job, now) if job.get("enabled") else None

    def start(self):
        self.reschedule()
        self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            try:
                now = datetime.now()
                paused = self.app.store.data["settings"].get("paused")
                for job in list(self.app.store.data["jobs"]):
                    s = self.st(job["id"])
                    if not job.get("enabled"):
                        s.next_due = None
                        continue
                    if s.next_due is None:
                        s.next_due = next_capture(job, now)
                    if s.next_due and now >= s.next_due:
                        due = s.next_due
                        s.next_due = next_capture(job, max(now, due) + timedelta(milliseconds=500))
                        if paused:
                            continue
                        if s.running:
                            log.warning("%s: previous capture still running, skipping %s", job["name"], due)
                            continue
                        asyncio.create_task(self.capture(job["id"], when=due))
            except Exception:  # never let the loop die
                log.exception("scheduler loop error")
            await asyncio.sleep(0.25)

    # ------------------------------------------------------------- capture
    async def prepare_camera(self, job: dict, cam_entry) -> None:
        cam = cam_entry.client
        if job.get("power_on"):
            if await cam.power_state() == "standby":
                log.info("%s: camera in standby, powering on", job["name"])
                await cam.power(True)
                await asyncio.sleep(10)
                cam_entry.current_preset = None
                cam_entry.applied_by = None

        # With "reapply" on (the default) the preset and settings are sent before
        # every shot, in case something outside Panalapse (a control panel, the
        # camera's own web page) has changed them. Otherwise they are only sent
        # when Panalapse knows the camera was touched.
        reapply = job.get("reapply", True)
        wait = 0.0
        preset = int(job.get("preset") or 0)
        if preset and (reapply or cam_entry.current_preset != preset):
            known_there = cam_entry.current_preset == preset
            await cam.preset_recall(preset)
            cam_entry.current_preset = preset
            settle = float(job.get("settle_seconds") or 0)
            wait = min(settle, 1.0) if known_there else settle

        if reapply or cam_entry.applied_by != job["id"]:
            info = cam_entry.config.get("info") or {}
            readonly = set(info.get("readonly") or [])
            supported = {k for k in (info.get("controls") or {}) if k not in readonly}
            mode = job.get("exposure_mode", "leave")
            settings: dict = {}
            if mode == "auto":
                accepted = info.get("accepted") or {}
                ranges = info.get("ranges") or {}

                def ok(c):
                    if c.key in accepted:
                        return c.auto in accepted[c.key]
                    if c.key in ranges:
                        return ranges[c.key][0] <= int(c.auto, 16) <= ranges[c.key][1]
                    return True
                settings = {c.key: c.auto for c in CONTROLS
                            if c.auto is not None and c.key in supported and ok(c)}
            elif mode == "manual":
                settings = {k: v for k, v in (job.get("exposure") or {}).items()
                            if v not in (None, "") and k in supported}
            # Iris mode must go first so the iris position is accepted.
            # A refused setting is a warning, not a reason to miss the shot.
            warnings = []
            order = [c.key for c in CONTROLS]
            for key in sorted(settings, key=order.index):
                try:
                    await cam.set_control(key, settings[key])
                except CameraError as e:
                    if "ER2" in str(e):          # busy: try once more
                        await asyncio.sleep(0.5)
                        try:
                            await cam.set_control(key, settings[key])
                            continue
                        except CameraError as e2:
                            e = e2
                    warnings.append(str(e))
            for line in (job.get("custom_commands") or "").splitlines():
                line = line.strip()
                if line and not line.startswith(";"):
                    try:
                        await cam.command_checked(line)
                    except CameraError as e:
                        warnings.append(str(e))
            cam_entry.applied_by = job["id"]
            joined = "; ".join(warnings) or None
            if joined and joined != self.st(job["id"]).last_warning:
                log.warning("%s: %s", job["name"], joined)
            self.st(job["id"]).last_warning = joined

        if wait:
            await asyncio.sleep(wait)

    async def capture(self, job_id: str, when: datetime | None = None, save: bool = True) -> bytes:
        job = self.app.store.get("jobs", job_id)
        if not job:
            raise ValueError("job not found")
        s = self.st(job_id)
        cam_entry = self.app.cameras.get(job["camera_id"])
        if not cam_entry:
            raise ValueError("job has no camera")
        s.running = True
        when = when or datetime.now()
        try:
            async with cam_entry.job_lock:
                await self.prepare_camera(job, cam_entry)
                res = int(job.get("resolution") or 0) or cam_entry.max_resolution()
                data = await cam_entry.client.snapshot(res)
            cam_entry.last_frame = data
            s.last_image = data
            if save:
                dest = self.app.destination(job.get("destination_id"))
                if not dest:
                    raise ValueError("job has no storage destination")
                counters = self.app.store.data["counters"]
                seq = counters.get(job_id, 0) + 1
                folder = render_template(job.get("folder_template") or "", job, cam_entry.config, when, seq)
                name = render_template(job.get("filename_template") or "{datetime}.jpg",
                                       job, cam_entry.config, when, seq)
                if not name.lower().endswith((".jpg", ".jpeg")):
                    name += ".jpg"
                rel = f"{folder.strip('/')}/{name}" if folder.strip("/") else name
                await dest.write(rel, data)
                counters[job_id] = seq
                self.app.store.save()
                s.last_path = dest.full(rel)
                s.session_count += 1
            s.last_capture = datetime.now()
            s.last_error = None
            return data
        except (CameraError, OSError, ValueError, Exception) as e:  # noqa: B014 - report everything
            s.last_error = str(e) or type(e).__name__
            s.last_error_at = datetime.now()
            log.error("%s: capture failed: %s", job.get("name"), s.last_error)
            raise
        finally:
            s.running = False
