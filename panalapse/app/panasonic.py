"""Async client for Panasonic AW-series PTZ cameras (HTTP CGI interface).

Every call goes over plain HTTP:
  /cgi-bin/aw_ptz?cmd=#XXX&res=1   pan/tilt/zoom/focus/preset/power ("#" commands)
  /cgi-bin/aw_cam?cmd=XXX&res=1    camera / image settings
  /cgi-bin/camera?resolution=N     JPEG still (AW-UE4: 1280 at most)
  rtsp://host/MediaInput/h264/stream_1   H.264 video; one frame of it gives a
                                         full-resolution (e.g. 4K) still

The cameras' embedded web servers are fragile: they close the connection after
every request and hang if hammered. So each camera gets one lock and a minimum
gap between requests.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import struct
import time
from urllib.parse import quote
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

MIN_GAP = 0.15          # Panasonic asks for >=130 ms between commands
SNAP_RESOLUTIONS = [3840, 1920, 1280, 640, 320]
STREAM_RESOLUTION = -1   # job "resolution" value meaning "a frame from the video stream"
STREAM_PATH = "MediaInput/h264/stream_1"
STREAM_TIMEOUT = 25


class CameraError(Exception):
    pass


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) of a JPEG without decoding it."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + seg_len
    return None


# --------------------------------------------------------------------------
# Image controls. Not every model supports every control, so each camera is
# probed with the query command and only the controls that answer are shown.
# --------------------------------------------------------------------------
@dataclass
class Control:
    key: str
    label: str
    query: str              # aw_cam (or "#..." for aw_ptz) query command
    reply_prefix: str       # strip this from the reply to get the value
    setter: str             # format string; {v} is the value
    kind: str = "select"    # select | range
    options: list = field(default_factory=list)   # [(value, label)]
    vmin: int = 0
    vmax: int = 0
    hexwidth: int = 2       # for range controls sent as hex
    auto: str | None = None  # value used by the "Auto" exposure profile
    group: str = "exposure"
    center: int | None = None  # range controls shown as an offset from this (e.g. -4..+4)
    write_probe: bool = True   # re-send the current value during detection to test writability
    depends: str | None = None  # only adjustable when this other control is adjustable


def _gain_opts():
    opts = [("80", "Auto (AGC)")]
    opts += [(f"{0x08 + db:02X}", f"{db} dB") for db in range(0, 43, 3)]
    return opts


CONTROLS: list[Control] = [
    Control("iris_mode", "Iris mode", "QRS", "ORS:", "ORS:{v}",
            options=[("1", "Auto"), ("0", "Manual")], auto="1"),
    Control("iris", "Iris position", "#AXI", "axi", "#AXI{v}", kind="range",
            vmin=0x555, vmax=0xFFF, hexwidth=3, write_probe=False, depends="iris_mode"),
    Control("gain", "Gain", "QGU", "OGU:", "OGU:{v}", options=_gain_opts(), auto="80"),
    Control("shutter_mode", "Shutter mode", "QSJ:03", "OSJ:03:", "OSJ:03:{v}",
            options=[("0", "Off"), ("1", "Step"), ("2", "Synchro"), ("3", "ELC (auto)")], auto="0"),
    Control("shutter_step", "Shutter step", "QSJ:06", "OSJ:06:", "OSJ:06:{v}", kind="range",
            vmin=0, vmax=0xFFFF, hexwidth=4, write_probe=False, depends="shutter_mode"),
    Control("ae_level", "Brightness (picture level)", "QSD:48", "OSD:48:", "OSD:48:{v}", kind="range",
            vmin=0, vmax=0x64, hexwidth=2, auto="32", center=0x32),
    Control("nd", "ND filter", "QFT", "OFT:", "OFT:{v}",
            options=[("0", "Through"), ("1", "1/4"), ("2", "1/16"), ("3", "1/64")]),
    Control("white_balance", "White balance", "QAW", "OAW:", "OAW:{v}",
            options=[("0", "ATW (auto tracking)"), ("1", "AWB A"), ("2", "AWB B"),
                     ("4", "3200K"), ("5", "5600K"), ("9", "VAR")], auto="0", group="colour"),
    Control("scene", "Scene file", "QSF", "OSF:", "XSF:{v}",
            options=[("0", "Scene 1"), ("1", "Scene 2"), ("2", "Scene 3"), ("3", "Scene 4")],
            group="colour", write_probe=False),   # re-selecting a scene would reset its settings
    Control("focus_mode", "Focus mode", "QAF", "OAF:", "OAF:{v}",
            options=[("1", "Auto"), ("0", "Manual")], auto="1", group="focus"),
]
CONTROL_BY_KEY = {c.key: c for c in CONTROLS}


class PanasonicCamera:
    def __init__(self, host: str, username: str = "", password: str = "", port: int = 80):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._auth: httpx.Auth | None = None
        self._snap_res: int | None = None   # last resolution requested from the camera
        self.max_resolution: int | None = None
        self.model: str | None = None
        self.online = False
        self.last_error: str | None = None

    @property
    def base(self) -> str:
        return f"http://{self.host}" + ("" if self.port == 80 else f":{self.port}")

    async def _get(self, path: str, params: dict, timeout: float = 6.0) -> httpx.Response:
        # Build the query string by hand: these cameras don't URL-decode "%3A",
        # so "QSD:48" must be sent with a literal colon. Only '#', '&', '%',
        # '+', space and '=' are escaped. Parameter order matters too: the AW-UE4
        # returns an empty body unless "cmd" comes before "res", so callers
        # pass cmd first and dict order is kept.
        def enc(v: str) -> str:
            return (str(v).replace("%", "%25").replace("#", "%23").replace("&", "%26")
                    .replace("+", "%2B").replace(" ", "%20").replace("=", "%3D"))
        url = self.base + path + "?" + "&".join(f"{k}={enc(v)}" for k, v in params.items())
        async with self._lock:
            gap = MIN_GAP - (time.monotonic() - self._last)
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                async with httpx.AsyncClient(timeout=timeout, auth=self._auth) as cl:
                    r = await cl.get(url, headers={"Connection": "close"})
                    if r.status_code == 401 and self.username:
                        # Work out which auth the camera wants, then retry once.
                        challenge = r.headers.get("www-authenticate", "").lower()
                        self._auth = (httpx.DigestAuth(self.username, self.password)
                                      if "digest" in challenge
                                      else httpx.BasicAuth(self.username, self.password))
                        r = await cl.get(url, auth=self._auth,
                                         headers={"Connection": "close"})
                self.online = True
                return r
            except httpx.HTTPError as e:
                self.online = False
                if isinstance(e, httpx.TimeoutException):
                    self.last_error = "no answer (timed out)"
                elif isinstance(e, httpx.ConnectError):
                    self.last_error = f"can't connect ({e})" if str(e) else "can't connect"
                else:
                    self.last_error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                raise CameraError(f"{self.host}: {self.last_error}") from e
            finally:
                self._last = time.monotonic()

    # ---------------------------------------------------------------- commands
    async def command(self, cmd: str) -> str:
        """Send one command; '#'-prefixed go to aw_ptz, the rest to aw_cam."""
        path = "/cgi-bin/aw_ptz" if cmd.startswith("#") else "/cgi-bin/aw_cam"
        r = await self._get(path, {"cmd": cmd, "res": "1"})
        if r.status_code != 200:
            raise CameraError(f"{cmd}: HTTP {r.status_code}")
        return r.text.strip()

    async def command_checked(self, cmd: str) -> str:
        reply = await self.command(cmd)
        if reply[:2].upper() == "ER":
            meaning = {"ER1": "not supported", "ER2": "camera busy",
                       "ER3": "value out of range / not allowed in current mode"}
            raise CameraError(f"{cmd} → {reply} ({meaning.get(reply[:3].upper(), 'error')})")
        return reply

    # ----------------------------------------------------------------- PTZ
    async def pan_tilt(self, pan: int, tilt: int):
        """pan/tilt -49..49 (0 = stop). Negative pan = left, negative tilt = down."""
        p = max(1, min(99, 50 + int(pan)))
        t = max(1, min(99, 50 + int(tilt)))
        return await self.command(f"#PTS{p:02d}{t:02d}")

    async def zoom(self, speed: int):
        """speed -49..49 (negative = wide, positive = tele, 0 = stop)."""
        return await self.command(f"#Z{max(1, min(99, 50 + int(speed))):02d}")

    async def focus(self, speed: int):
        """speed -49..49 (negative = near, positive = far, 0 = stop)."""
        return await self.command(f"#F{max(1, min(99, 50 + int(speed))):02d}")

    async def stop(self):
        await self.command("#PTS5050")
        await self.command("#Z50")

    async def preset_recall(self, n: int):
        """n is 1-based (1..100)."""
        return await self.command_checked(f"#R{n - 1:02d}")

    async def preset_save(self, n: int):
        return await self.command_checked(f"#M{n - 1:02d}")

    async def preset_delete(self, n: int):
        return await self.command_checked(f"#C{n - 1:02d}")

    ONE_TOUCH = {
        "af": ("OSE:69:1", "One-touch auto focus"),
        "awb": ("OWS", "Auto white balance set"),
        "abb": ("OAS", "Auto black balance set"),
    }

    async def one_touch(self, kind: str) -> str:
        """Run a one-shot function: af (focus once), awb (white set), abb (black set)."""
        cmd, label = self.ONE_TOUCH[kind]
        reply = await self.command(cmd)
        code = reply[:3].upper()
        if code == "ER1":
            raise CameraError(f"{label} isn't supported by this camera ({cmd} → ER1)")
        if code == "ER3":
            hint = {"af": "switch focus to manual first",
                    "awb": "set white balance to AWB A or AWB B first",
                    "abb": ""}[kind]
            raise CameraError(f"{label} isn't available in the current mode"
                              + (f": {hint}" if hint else "") + f" ({cmd} → ER3)")
        if code.startswith("ER"):
            raise CameraError(f"{label}: camera replied {reply}")
        return reply

    async def power(self, on: bool):
        return await self.command(f"#O{1 if on else 0}")

    async def power_state(self) -> str:
        r = await self.command("#O")
        return {"p1": "on", "p0": "standby", "p3": "turning on"}.get(r, r)

    # -------------------------------------------------------------- controls
    async def probe(self, write_tests: bool = True, previous: dict | None = None) -> dict:
        """Read model, max snapshot size and which image controls exist.

        With write_tests, each setting is also tried: dropdowns are set to each
        option in turn and ranges are probed for their limits, and the original
        value is put back afterwards. That briefly changes the picture, so it only
        runs when a camera is added or "Detect" is pressed. Without it, the
        previous results are kept."""
        info = {"model": None, "firmware": None, "power": None, "controls": {}, "max_resolution": None}
        try:
            r = await self.command("QID")
            info["model"] = r.split(":", 1)[1] if r.startswith("OID:") else None
            self.model = info["model"]
            r = await self.command("QSV")
            info["firmware"] = r.split(":", 1)[1] if r.startswith("OSV:") else None
            info["power"] = await self.power_state()
        except CameraError as e:
            info["error"] = str(e)
            return info
        if not write_tests and previous and previous.get("model") == info["model"]:
            for k in ("readonly", "ranges", "accepted"):
                info[k] = previous.get(k, {} if k != "readonly" else [])
        else:
            write_tests = True
        if write_tests:
            info["readonly"], info["ranges"], info["accepted"] = [], {}, {}
        for c in CONTROLS:
            try:
                val = await self.read_control(c)
            except CameraError:
                continue
            if val is None:
                continue
            info["controls"][c.key] = val
            if not write_tests:
                continue
            if c.depends and (c.depends in info["readonly"] or c.depends not in info["controls"]):
                info["readonly"].append(c.key)
                continue
            if not c.write_probe:
                continue
            try:
                if not await self._try_set(c, val):
                    info["readonly"].append(c.key)
                    continue
                if c.kind == "range":
                    info["ranges"][c.key] = await self._find_range(c, int(val, 16))
                else:
                    ok = [val]
                    try:
                        for opt, _label in c.options:
                            if opt != val and await self._try_set(c, opt):
                                ok.append(opt)
                    finally:
                        await self._try_set(c, val)
                    info["accepted"][c.key] = ok
                    if len(ok) < 2:
                        info["readonly"].append(c.key)   # can only "change" to what it already is
            except CameraError:
                info["readonly"].append(c.key)
        info["max_resolution"] = await self.detect_max_resolution()
        return info

    async def _try_set(self, c: Control, value) -> bool:
        cmd = c.setter.format(v=self.format_value(c, value))
        for _ in range(2):
            reply = await self.command(cmd)
            if not reply.upper().startswith("ER2"):    # ER2 = busy, retry once
                break
            await asyncio.sleep(0.4)
        return not reply.upper().startswith("ER")

    async def _find_range(self, c: Control, cur: int) -> list[int]:
        """Find the accepted min/max of a range control by probing outward from
        the current value, then put the original value back."""
        async def edge(direction: int) -> int:
            limit = c.vmax if direction > 0 else c.vmin
            good, step = cur, 1
            bad = None
            while True:              # widen until refused or at the limit
                cand = cur + direction * step
                if (direction > 0 and cand >= limit) or (direction < 0 and cand <= limit):
                    cand = limit
                if await self._try_set(c, cand):
                    good = cand
                    if cand == limit:
                        return good
                    step *= 2
                else:
                    bad = cand
                    break
            while abs(bad - good) > 1:   # narrow down between good and bad
                mid = (good + bad) // 2
                if await self._try_set(c, mid):
                    good = mid
                else:
                    bad = mid
            return good
        try:
            hi = await edge(+1)
            lo = await edge(-1)
        finally:
            await self._try_set(c, cur)
        return [lo, hi]

    async def read_control(self, c: Control) -> str | None:
        reply = await self.command(c.query)
        if reply.upper().startswith("ER") or not reply.lower().startswith(c.reply_prefix.lower()):
            return None
        return reply[len(c.reply_prefix):]

    async def read_controls(self, keys: list[str]) -> dict:
        out = {}
        for k in keys:
            c = CONTROL_BY_KEY.get(k)
            if c:
                try:
                    out[k] = await self.read_control(c)
                except CameraError:
                    out[k] = None
        return out

    @staticmethod
    def format_value(c: Control, value) -> str:
        if c.kind == "range":
            v = int(value, 16) if isinstance(value, str) else int(value)
            v = max(c.vmin, min(c.vmax, v))
            return f"{v:0{c.hexwidth}X}"
        return str(value)

    async def set_control(self, key: str, value) -> str:
        c = CONTROL_BY_KEY[key]
        return await self.command_checked(c.setter.format(v=self.format_value(c, value)))

    # ------------------------------------------------------------- snapshot
    async def detect_max_resolution(self) -> int | None:
        for res in SNAP_RESOLUTIONS:
            try:
                r = await self._get("/cgi-bin/camera", {"resolution": str(res)}, timeout=12)
            except CameraError:
                continue
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                self.max_resolution = res
                self._snap_res = res
                return res
        return None

    def stream_url(self, path: str = "", hide_password: bool = False) -> str:
        path = (path or STREAM_PATH).strip()
        if path.startswith("rtsp://"):
            return path
        cred = ""
        if self.username:
            pw = "***" if hide_password else quote(self.password, safe="")
            cred = f"{quote(self.username, safe='')}:{pw}@"
        return f"rtsp://{cred}{self.host}/{path.lstrip('/')}"

    async def stream_frame(self, path: str = "") -> bytes:
        """Grab one frame of the camera's RTSP video as a JPEG, at the stream's
        full resolution (3840x2160 on an AW-UE4). Takes about 2 seconds."""
        url = self.stream_url(path)
        cmd = [os.environ.get("FFMPEG", "ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
               "-rtsp_transport", "tcp", "-timeout", str(10_000_000), "-i", url,
               "-frames:v", "1", "-an", "-q:v", "2", "-f", "image2pipe", "-c:v", "mjpeg", "-"]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), STREAM_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            self.last_error = "video stream: no picture (timed out)"
            raise CameraError(f"{self.host}: {self.last_error}")
        if proc.returncode != 0 or not out.startswith(b"\xff\xd8"):
            lines = err.decode(errors="replace").strip().splitlines()
            msg = lines[-1] if lines else f"ffmpeg exit code {proc.returncode}"
            msg = re.sub(r"rtsp://[^@\s]*@", "rtsp://", msg)         # never show the password
            msg = re.sub(r"^\[[^\]]*\]\s*", "", msg)
            if re.search(r"Error number -1(38|10)|Connection refused|timed out", msg):
                msg = "can't connect to the camera's video stream (RTSP, port 554)"
            elif "404" in msg:
                msg += f" - check the video stream path ({self.stream_url(path, hide_password=True)})"
            elif "401" in msg:
                msg += " - the camera wants a user name and password for its video stream"
            self.last_error = f"video stream: {msg}"
            raise CameraError(f"{self.host}: {self.last_error}")
        return out

    async def snapshot(self, resolution: int = 1280, verify: bool = True) -> bytes:
        """Fetch a JPEG. When the resolution changes, some models (e.g. AW-UE4)
        still send one frame at the old size and take ~5 s to switch, so when
        verify is on the frame width is checked and fetched again if it is wrong."""
        if self.max_resolution and resolution > self.max_resolution:
            resolution = self.max_resolution
        tries = 3 if verify else 1
        data = b""
        for attempt in range(tries):
            r = await self._get("/cgi-bin/camera", {"resolution": str(resolution)}, timeout=15)
            if r.status_code != 200 or not r.content.startswith(b"\xff\xd8"):
                smaller = [s for s in SNAP_RESOLUTIONS if s < resolution]
                if not self.max_resolution and smaller and r.status_code in (400, 404, 500, 501):
                    # Asked for more than the camera offers (an AW-UE4 answers 1920 with
                    # HTTP 500) before Detect has run: use the next size down from now on.
                    log.info("%s: snapshot at %s refused (HTTP %s), trying %s",
                             self.host, resolution, r.status_code, smaller[0])
                    data = await self.snapshot(smaller[0], verify)
                    self.max_resolution = min(self.max_resolution or smaller[0], smaller[0])
                    return data
                raise CameraError(f"snapshot at {resolution}: HTTP {r.status_code}"
                                  + (" (larger than this camera offers?)" if r.status_code == 500 else ""))
            data = r.content
            self._snap_res = resolution
            if not verify:
                return data
            size = jpeg_size(data)
            if size is None or abs(size[0] - resolution) <= 8 or attempt == tries - 1:
                return data
            # A 16:9 camera asked for 640 might legitimately answer 640x360; any
            # other width means a stale frame from the previous resolution.
            log.debug("%s: got %s for resolution %s, refetching", self.host, size, resolution)
        return data
