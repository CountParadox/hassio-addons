"""Panalapse - timelapse capture for Panasonic AW PTZ cameras (Home Assistant add-on)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

from aiohttp import web

import time

from . import discover
from .gallery import Gallery, job_root, make_thumb, prune_thumbs
from .mqtt import MQTT_DEFAULTS, MqttBridge, iso
from .notify import HaNotifier
from .panasonic import CONTROLS, CameraError, PanasonicCamera, jpeg_size
from .scheduler import Scheduler, next_capture
from .storage import Destination, reset_sessions
from .store import CAMERA_DEFAULTS, DATA_DIR, JOB_DEFAULTS, Store
from .video import VIDEO_DIR, Renderer

log = logging.getLogger("panalapse")
STATIC = os.path.join(os.path.dirname(__file__), "static")
THUMBS = os.path.join(DATA_DIR, "thumbs")
PTZ_WATCHDOG = 1.2   # seconds without a refresh before a held move is stopped


class CameraEntry:
    def __init__(self, config: dict):
        self.config = config
        self.client = PanasonicCamera(config["host"], config.get("username", ""),
                                      config.get("password", ""), int(config.get("port") or 80))
        info = config.get("info") or {}
        self.client.max_resolution = info.get("max_resolution")
        self.client.model = info.get("model")
        self.job_lock = asyncio.Lock()
        self.probing = False
        self.last_frame: bytes | None = None
        self.current_preset: int | None = None
        self.applied_by: str | None = None     # job id whose settings are on the camera
        self._watchdog: asyncio.Task | None = None

    def max_resolution(self) -> int:
        return self.client.max_resolution or 1280

    @property
    def detected(self) -> bool:
        return bool((self.config.get("info") or {}).get("model"))

    def touched(self):
        """Someone moved the camera or changed a setting by hand."""
        self.current_preset = None
        self.applied_by = None

    def arm_watchdog(self):
        if self._watchdog:
            self._watchdog.cancel()

        async def stop_later():
            await asyncio.sleep(PTZ_WATCHDOG)
            try:
                await self.client.stop()
            except CameraError:
                pass
        self._watchdog = asyncio.create_task(stop_later())

    def disarm_watchdog(self):
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None


class App:
    def __init__(self):
        self.store = Store()
        self.cameras: dict[str, CameraEntry] = {}
        self.probe_camera = None        # set by build_app: full Detect for a camera id
        for c in self.store.data["cameras"]:
            self.cameras[c["id"]] = CameraEntry(c)
        self.scheduler = Scheduler(self)
        self.renderer = Renderer(self)
        self.gallery = Gallery(self)
        self.mqtt = MqttBridge(self)
        self.notifier = HaNotifier(self)

    # ------------------------------------------------------------- status
    def job_status(self, job: dict) -> dict:
        """One timelapse's state, as sent over MQTT (and usable by any API)."""
        s = self.scheduler.st(job["id"])
        if not job.get("enabled"):
            status = "disabled"
        elif s.running:
            status = "capturing"
        elif s.last_error:
            status = "error"
        elif self.store.data["settings"].get("paused"):
            status = "paused"
        elif s.next_due:
            status = "scheduled"
        else:
            status = "finished"
        cam = self.store.get("cameras", job.get("camera_id", "")) or {}
        return {
            "status": status,
            "problem": bool(s.last_error),
            "enabled": bool(job.get("enabled")),
            "camera": cam.get("name"),
            "last_capture": iso(s.last_capture),
            "next_capture": iso(s.next_due) if job.get("enabled") else None,
            "frames": self.store.data["counters"].get(job["id"], 0),
            "last_path": s.last_path,
            "last_error": s.last_error,
            "last_error_at": iso(s.last_error_at),
            "warning": s.last_warning,
        }

    def capture_done(self, job: dict, ok: bool, error: str | None, image: bytes | None, rel: str | None):
        """Called by the scheduler after every capture attempt."""
        if ok and rel:
            self.gallery.added(job, rel, time.time())
        if image is not None and (jpeg_size(image) or (0, 0))[0] > 1920:
            try:    # a 4K still is a lot to push through MQTT for a dashboard picture
                image = make_thumb(image, 1920)
            except Exception:
                log.debug("couldn't shrink image for MQTT", exc_info=True)
        self.mqtt.job_updated(job["id"], image)
        asyncio.create_task(self.notifier.job_result(job, ok, error))

    async def health_loop(self):
        """Check idle cameras now and then so 'online' stays true to life."""
        while True:
            await asyncio.sleep(30)
            for cid, e in list(self.cameras.items()):
                if e.job_lock.locked() or time.monotonic() - e.client._last < 25:
                    continue
                try:
                    await e.client.command("#O")
                except CameraError:
                    continue
                except Exception:
                    log.debug("health check failed", exc_info=True)
                    continue
                if not e.detected and not e.probing and self.probe_camera:
                    # Added (or started) while the camera was offline: detect it now.
                    log.info("%s is answering now, detecting it", e.config.get("name"))
                    e.probing = True
                    asyncio.create_task(self.probe_camera(cid))

    def destination(self, dest_id: str | None) -> Destination | None:
        d = self.store.get("destinations", dest_id or "")
        return Destination.from_dict(d) if d else None

    def cam(self, request: web.Request) -> CameraEntry:
        entry = self.cameras.get(request.match_info["cid"])
        if not entry:
            raise web.HTTPNotFound(text="camera not found")
        return entry

    # ------------------------------------------------------------- state
    def public_state(self) -> dict:
        def strip(d: dict) -> dict:
            out = {k: v for k, v in d.items() if k != "password"}
            out["has_password"] = bool(d.get("password"))
            return out

        cams = []
        for c in self.store.data["cameras"]:
            e = self.cameras.get(c["id"])
            cc = strip(c)
            cc["online"] = e.client.online if e else False
            cc["busy"] = e.job_lock.locked() if e else False
            cc["last_error"] = e.client.last_error if e else None
            cams.append(cc)
        jobs = []
        for j in self.store.data["jobs"]:
            s = self.scheduler.st(j["id"])
            jj = dict(j)
            jj["runtime"] = {
                "next_due": s.next_due.isoformat(timespec="seconds") if s.next_due else None,
                "last_capture": s.last_capture.isoformat(timespec="seconds") if s.last_capture else None,
                "last_path": s.last_path,
                "last_error": s.last_error,
                "last_warning": s.last_warning,
                "last_error_at": s.last_error_at.isoformat(timespec="seconds") if s.last_error_at else None,
                "running": s.running,
                "count": self.store.data["counters"].get(j["id"], 0),
                "has_image": s.last_image is not None,
            }
            jobs.append(jj)
        return {
            "cameras": cams,
            "destinations": [strip(d) for d in self.store.data["destinations"]],
            "jobs": jobs,
            "settings": {k: v for k, v in self.store.data["settings"].items() if k != "mqtt"},
            "controls": [{"key": c.key, "label": c.label, "kind": c.kind, "options": c.options,
                          "min": c.vmin, "max": c.vmax, "hexwidth": c.hexwidth, "auto": c.auto,
                          "group": c.group, "center": c.center} for c in CONTROLS],
            "renders": [r.to_dict() for r in sorted(self.renderer.renders.values(),
                                                    key=lambda r: r.started, reverse=True)][:20],
            "videos": self.renderer.list_videos(),
            "mqtt": self.mqtt.public(),
            "ha": {"available": self.notifier.available,
                   "notifications": self.store.data["settings"].get("ha_notifications", True)},
            "now": datetime.now().isoformat(timespec="seconds"),
        }


def _sign(v) -> int:
    v = int(v or 0)
    return (v > 0) - (v < 0)


def err(e: Exception, status=400):
    return web.json_response({"ok": False, "error": str(e) or type(e).__name__}, status=status)


def build_app() -> web.Application:
    core = App()
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_):
        # Tag the script and stylesheet with their modified time, so a browser
        # never keeps an old copy after the add-on is updated.
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
            html = f.read()
        for name in ("app.js", "style.css"):
            v = int(os.path.getmtime(os.path.join(STATIC, name)))
            html = html.replace(f"static/{name}", f"static/{name}?v={v}")
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    @routes.get("/api/state")
    async def state(_):
        return web.json_response(core.public_state())

    # -------------------------------------------------------------- cameras
    @routes.post("/api/cameras")
    async def save_camera(req):
        body = await req.json()
        allowed = {k: body[k] for k in ("id", "name", "host", "port", "username", "password",
                                        "preview_resolution", "stream_path") if k in body}
        if not allowed.get("host"):
            return err(ValueError("Camera IP / host is required"))
        allowed["host"] = allowed["host"].strip().removeprefix("http://").rstrip("/")
        allowed["name"] = (allowed.get("name") or allowed["host"]).strip()
        old = core.store.get("cameras", allowed.get("id", "")) if allowed.get("id") else None
        conn_changed = not old or any(str(old.get(k)) != str(allowed.get(k, old.get(k)))
                                      for k in ("host", "port", "username")) or allowed.get("password")
        cam = core.store.upsert("cameras", allowed, CAMERA_DEFAULTS, "cam")
        if conn_changed or cam["id"] not in core.cameras:
            core.cameras[cam["id"]] = CameraEntry(cam)
            asyncio.create_task(_probe(cam["id"]))
        else:
            core.cameras[cam["id"]].config = cam
        return web.json_response({"ok": True, "id": cam["id"]})

    async def _probe(cid: str, write_tests: bool = True) -> dict:
        entry = core.cameras[cid]
        entry.probing = True
        try:
            async with entry.job_lock:      # don't test settings in the middle of a capture
                info = await entry.client.probe(write_tests, previous=entry.config.get("info"))
        finally:
            entry.probing = False
        entry.touched()
        if info.get("model"):
            entry.config["info"] = info
            core.store.save()
        return info

    core.probe_camera = _probe

    def _remember_max(e: CameraEntry):
        """The snapshot code may have learnt the largest size; keep it."""
        info = e.config.get("info")
        if info is not None and e.client.max_resolution and info.get("max_resolution") != e.client.max_resolution:
            info["max_resolution"] = e.client.max_resolution
            core.store.save()

    @routes.delete("/api/cameras/{cid}")
    async def delete_camera(req):
        cid = req.match_info["cid"]
        core.store.delete("cameras", cid)
        core.cameras.pop(cid, None)
        return web.json_response({"ok": True})

    # ------------------------------------------------------------ discovery
    def _known_hosts() -> dict:
        return {c["host"]: c for c in core.store.data["cameras"]
                if int(c.get("port") or 80) == 80}

    @routes.get("/api/discover")
    async def discover_info(_):
        subnets = await discover.host_subnets()
        for host in _known_hosts():
            try:
                net = str(discover.ipaddress.ip_interface(host + "/24").network)
            except ValueError:          # a host name, not an IP
                continue
            if net not in subnets:
                subnets.append(net)
        return web.json_response({"subnets": subnets})

    @routes.post("/api/discover")
    async def discover_scan(req):
        body = await req.json()
        try:
            addrs = discover.parse_targets(body.get("targets") or "")
        except ValueError as e:
            return err(e)
        if not addrs:
            return err(ValueError("Enter a subnet such as 192.168.1.0/24"))
        known = _known_hosts()
        res = await discover.scan(addrs, (body.get("username") or "").strip(),
                                  body.get("password") or "", skip=set(known))
        for host in sorted(set(known) & set(addrs), key=lambda h: discover.ipaddress.ip_address(h)):
            c = known[host]
            res["found"].append({"host": host, "status": "added", "name": c.get("name"),
                                 "model": (c.get("info") or {}).get("model")})
        res["found"].sort(key=lambda d: discover.ipaddress.ip_address(d["host"]))
        log.info("Camera scan: %d addresses in %ss, %d found", res["scanned"], res["seconds"],
                 sum(1 for f in res["found"] if f["status"] != "added"))
        return web.json_response(res)

    @routes.post("/api/cameras/{cid}/probe")
    async def probe(req):
        core.cam(req)
        info = await _probe(req.match_info["cid"])
        if not info.get("model"):
            return err(CameraError(info.get("error") or "No answer from camera"), 502)
        return web.json_response({"ok": True, "info": info})

    @routes.get("/api/cameras/{cid}/snapshot")
    async def snapshot(req):
        e = core.cam(req)
        res = int(req.query.get("res") or 0) or int(e.config.get("preview_resolution") or 0) \
            or e.max_resolution()
        if e.job_lock.locked() and e.last_frame:
            # A job is capturing; don't fight it for the camera.
            data = e.last_frame
        else:
            try:
                data = await e.client.snapshot(res, verify=False)
                e.last_frame = data
            except CameraError as ex:
                return err(ex, 502)
            finally:
                _remember_max(e)
        return web.Response(body=data, content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    @routes.post("/api/cameras/{cid}/ptz")
    async def ptz(req):
        e = core.cam(req)
        b = await req.json()
        action = b.get("action")
        speed = int(b.get("speed") or 25)
        try:
            if action == "move":
                e.touched()
                # pan / tilt are directions (-1, 0, 1); speed is 1-49
                await e.client.pan_tilt(_sign(b.get("pan")) * speed, _sign(b.get("tilt")) * speed)
                e.arm_watchdog()
            elif action == "zoom":
                e.touched()
                await e.client.zoom(int(b.get("dir", 0)) * speed)
                e.arm_watchdog()
            elif action == "focus":
                e.touched()
                await e.client.focus(int(b.get("dir", 0)) * speed)
                e.arm_watchdog()
            elif action == "onetouch":
                kind = b.get("kind")
                if kind not in PanasonicCamera.ONE_TOUCH:
                    return err(ValueError(f"unknown one-touch function {kind!r}"), 400)
                e.touched()
                await e.client.one_touch(kind)
            elif action == "refresh":
                e.arm_watchdog()
            elif action == "stop":
                e.disarm_watchdog()
                await e.client.stop()
                await e.client.command("#F50")
            else:
                return err(ValueError(f"unknown action {action}"))
        except CameraError as ex:
            return err(ex, 502)
        return web.json_response({"ok": True})

    @routes.post("/api/cameras/{cid}/preset")
    async def preset(req):
        e = core.cam(req)
        b = await req.json()
        n = int(b.get("n", 0))
        if not 1 <= n <= 100:
            return err(ValueError("Preset number must be 1-100"))
        presets = e.config.setdefault("presets", {})
        try:
            if b.get("action") == "recall":
                async with e.job_lock:
                    await e.client.preset_recall(n)
                e.current_preset = n
                e.applied_by = None
            elif b.get("action") == "save":
                async with e.job_lock:
                    await e.client.preset_save(n)
                e.current_preset = n
                entry = presets.setdefault(str(n), {})
                entry["name"] = (b.get("name") or entry.get("name") or f"Preset {n}").strip()
                try:
                    img = await e.client.snapshot(e.max_resolution(), verify=False)
                    os.makedirs(THUMBS, exist_ok=True)
                    with open(os.path.join(THUMBS, f"{e.config['id']}_{n}.jpg"), "wb") as f:
                        f.write(img)
                    entry["thumb"] = True
                except CameraError:
                    pass
                core.store.save()
            elif b.get("action") == "rename":
                presets.setdefault(str(n), {})["name"] = (b.get("name") or f"Preset {n}").strip()
                core.store.save()
            elif b.get("action") == "forget":
                presets.pop(str(n), None)
                core.store.save()
            else:
                return err(ValueError("unknown action"))
        except CameraError as ex:
            return err(ex, 502)
        return web.json_response({"ok": True})

    @routes.get("/api/cameras/{cid}/preset/{n}/thumb")
    async def preset_thumb(req):
        p = os.path.join(THUMBS, f"{req.match_info['cid']}_{int(req.match_info['n'])}.jpg")
        if not os.path.exists(p):
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "no-cache"})

    @routes.get("/api/cameras/{cid}/controls")
    async def get_controls(req):
        e = core.cam(req)
        keys = list(((e.config.get("info") or {}).get("controls") or {}).keys())
        try:
            vals = await e.client.read_controls(keys)
            power_state = await e.client.power_state()
        except CameraError as ex:
            return err(ex, 502)
        return web.json_response({"ok": True, "values": vals, "power": power_state})

    @routes.post("/api/cameras/{cid}/controls")
    async def set_controls(req):
        e = core.cam(req)
        b = await req.json()
        results = {}
        order = [c.key for c in CONTROLS]
        for key in sorted((k for k in b if k in order), key=order.index):
            try:
                results[key] = await e.client.set_control(key, b[key])
            except CameraError as ex:
                results[key] = f"error: {ex}"
        e.applied_by = None
        return web.json_response({"ok": True, "results": results})

    @routes.post("/api/cameras/{cid}/command")
    async def raw_command(req):
        e = core.cam(req)
        b = await req.json()
        cmd = (b.get("cmd") or "").strip()
        if not cmd:
            return err(ValueError("Empty command"))
        try:
            reply = await e.client.command(cmd)
        except CameraError as ex:
            return err(ex, 502)
        e.applied_by = None
        return web.json_response({"ok": True, "reply": reply})

    # --------------------------------------------------------- destinations
    @routes.post("/api/destinations")
    async def save_dest(req):
        b = await req.json()
        d = {k: b[k] for k in ("id", "name", "type", "path", "username", "password", "domain") if k in b}
        if not d.get("path"):
            return err(ValueError("Path is required"))
        if d.get("type", "smb") == "smb":
            try:
                from .storage import parse_unc
                parse_unc(d["path"])
            except ValueError as ex:
                return err(ex)
        reset_sessions()
        obj = core.store.upsert("destinations", d, {"name": "", "type": "smb", "path": "",
                                                    "username": "", "password": "", "domain": ""}, "dst")
        return web.json_response({"ok": True, "id": obj["id"]})

    @routes.delete("/api/destinations/{did}")
    async def delete_dest(req):
        core.store.delete("destinations", req.match_info["did"])
        return web.json_response({"ok": True})

    @routes.post("/api/destinations/{did}/test")
    async def test_dest(req):
        d = core.destination(req.match_info["did"])
        if not d:
            raise web.HTTPNotFound()
        try:
            msg = await d.test()
        except Exception as ex:   # smbprotocol raises a variety of types
            return err(ex, 502)
        return web.json_response({"ok": True, "message": msg})

    # ---------------------------------------------------------------- jobs
    @routes.post("/api/jobs")
    async def save_job(req):
        b = await req.json()
        d = {k: b[k] for k in list(JOB_DEFAULTS) + ["id"] if k in b}
        if "interval_seconds" in d:
            d["interval_seconds"] = max(1, int(d["interval_seconds"]))
        job = core.store.upsert("jobs", d, JOB_DEFAULTS, "job", secret_fields=())
        core.scheduler.reschedule(job["id"])
        for e in core.cameras.values():
            if e.applied_by == job["id"]:
                e.applied_by = None   # settings may have changed; re-apply next capture
        return web.json_response({"ok": True, "id": job["id"]})

    @routes.delete("/api/jobs/{jid}")
    async def delete_job(req):
        core.store.delete("jobs", req.match_info["jid"])
        core.scheduler.state.pop(req.match_info["jid"], None)
        core.notifier.failing.pop(req.match_info["jid"], None)
        core.mqtt.job_updated(req.match_info["jid"], None)
        return web.json_response({"ok": True})

    @routes.post("/api/jobs/{jid}/capture")
    async def capture_now(req):
        b = await req.json() if req.can_read_body else {}
        try:
            await core.scheduler.capture(req.match_info["jid"], save=b.get("save", True))
        except Exception as ex:
            return err(ex, 502)
        s = core.scheduler.st(req.match_info["jid"])
        return web.json_response({"ok": True, "path": s.last_path})

    @routes.get("/api/jobs/{jid}/last.jpg")
    async def job_last(req):
        s = core.scheduler.st(req.match_info["jid"])
        if not s.last_image:
            raise web.HTTPNotFound()
        return web.Response(body=s.last_image, content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    # -------------------------------------------------------------- images
    def _job(req) -> dict:
        job = core.store.get("jobs", req.match_info["jid"])
        if not job:
            raise web.HTTPNotFound(text="timelapse not found")
        return job

    @routes.get("/api/jobs/{jid}/days")
    async def image_days(req):
        job = _job(req)
        try:
            days = await core.gallery.days(job, refresh=bool(req.query.get("refresh")))
        except Exception as ex:      # storage unreachable etc.
            return err(ex, 502)
        cam = core.store.get("cameras", job.get("camera_id", ""))
        dest = core.destination(job.get("destination_id"))
        return web.json_response({"ok": True, "days": days,
                                  "folder": dest.full(job_root(job, cam)) if dest else None})

    @routes.get("/api/jobs/{jid}/images")
    async def image_list(req):
        job = _job(req)
        try:
            imgs = await core.gallery.images(job, req.query.get("day", ""))
        except Exception as ex:
            return err(ex, 502)
        return web.json_response({"ok": True, "images": imgs})

    @routes.get("/api/jobs/{jid}/image")
    async def image_get(req):
        job = _job(req)
        rel = req.query.get("p", "")
        size = req.query.get("size", "full")
        try:
            if size == "full":
                data = await core.gallery.read(job, rel)
            else:
                data = await core.gallery.thumb(job, rel, {"large": 1920, "medium": 640}.get(size, 320))
        except PermissionError as ex:
            return err(ex, 403)
        except FileNotFoundError:
            raise web.HTTPNotFound()
        except Exception as ex:
            return err(ex, 502)
        headers = {"Cache-Control": "private, max-age=86400"}
        if req.query.get("download"):
            headers["Content-Disposition"] = f'attachment; filename="{os.path.basename(rel)}"'
        return web.Response(body=data, content_type="image/jpeg", headers=headers)

    @routes.post("/api/jobs/preview-schedule")
    async def preview_schedule(req):
        """Next few capture times for an unsaved job form."""
        job = dict(JOB_DEFAULTS)
        job.update(await req.json())
        out, t = [], datetime.now()
        for _ in range(5):
            n = next_capture(job, t)
            if not n:
                break
            out.append(n.isoformat(timespec="seconds"))
            t = n + timedelta(seconds=1)
        return web.json_response({"ok": True, "times": out})

    # -------------------------------------------------------------- settings
    @routes.post("/api/settings")
    async def settings(req):
        b = await req.json()
        settings = core.store.data["settings"]
        if "paused" in b:
            settings["paused"] = bool(b["paused"])
        if "ha_notifications" in b:
            settings["ha_notifications"] = bool(b["ha_notifications"])
        restart_mqtt = False
        if isinstance(b.get("mqtt"), dict):
            m = dict(MQTT_DEFAULTS)
            m.update(settings.get("mqtt") or {})
            for k in MQTT_DEFAULTS:
                if k in b["mqtt"]:
                    if k == "password" and not b["mqtt"][k]:
                        continue          # blank = keep the saved password
                    m[k] = int(b["mqtt"][k] or 1883) if k == "port" else str(b["mqtt"][k]).strip()
            if m["mode"] not in ("auto", "custom", "off"):
                return err(ValueError("MQTT mode must be auto, custom or off"))
            restart_mqtt = m != settings.get("mqtt") or b["mqtt"].get("reconnect")
            settings["mqtt"] = m
        core.store.save()
        if restart_mqtt:
            await core.mqtt.restart()
        core.mqtt.job_updated("", None)      # push the new pause state etc.
        return web.json_response({"ok": True})

    # -------------------------------------------------------------- videos
    @routes.post("/api/renders")
    async def start_render(req):
        b = await req.json()
        r = core.renderer.submit(b)
        return web.json_response({"ok": True, "id": r.id})

    @routes.post("/api/renders/{rid}/cancel")
    async def cancel_render(req):
        core.renderer.cancel(req.match_info["rid"])
        return web.json_response({"ok": True})

    def _video_path(name: str) -> str:
        name = os.path.basename(name)
        p = os.path.join(VIDEO_DIR, name)
        if not name.endswith(".mp4") or not os.path.exists(p):
            raise web.HTTPNotFound()
        return p

    @routes.get("/api/videos/{name}")
    async def get_video(req):
        p = _video_path(req.match_info["name"])
        headers = {}
        if req.query.get("download"):
            headers["Content-Disposition"] = f'attachment; filename="{os.path.basename(p)}"'
        return web.FileResponse(p, headers=headers)

    @routes.delete("/api/videos/{name}")
    async def delete_video(req):
        os.remove(_video_path(req.match_info["name"]))
        return web.json_response({"ok": True})

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except json.JSONDecodeError as ex:
            return err(ValueError(f"Bad JSON in request: {ex}"))
        except Exception as ex:   # report instead of a bare 500
            log.exception("error handling %s", request.path)
            return err(ex, 500)

    app = web.Application(client_max_size=4 * 1024 * 1024, middlewares=[errors])
    app.add_routes(routes)
    app.router.add_static("/static/", STATIC)

    async def on_start(_app):
        core.scheduler.start()
        core.renderer.start()
        core.mqtt.start()
        asyncio.create_task(core.health_loop())
        asyncio.get_running_loop().run_in_executor(None, prune_thumbs)
        # Refresh camera info in the background so a new firmware / model is picked up.
        for cid in list(core.cameras):
            asyncio.create_task(_safe_probe(cid))

    async def _safe_probe(cid):
        try:
            await _probe(cid, write_tests=False)
        except Exception:
            log.warning("probe of %s failed", cid, exc_info=True)

    async def on_stop(_app):
        await core.mqtt.stop()

    app.on_startup.append(on_start)
    app.on_shutdown.append(on_stop)
    app["core"] = core
    return app


def main():
    level = "info"
    opts = os.path.join(DATA_DIR, "options.json")
    if os.path.exists(opts):
        try:
            level = json.load(open(opts)).get("log_level", "info")
        except (OSError, ValueError):
            pass
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("smbprotocol").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    port = int(os.environ.get("PORT", "8099"))
    log.info("Panalapse listening on port %s, data in %s", port, DATA_DIR)
    web.run_app(build_app(), host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
