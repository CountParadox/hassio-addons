"""MQTT link to Home Assistant.

Every timelapse and camera shows up in Home Assistant as a device, using MQTT
discovery, so nothing has to be written in YAML:

  timelapse  sensors: status, last capture, next capture, frames, last error
             binary sensor: problem      image: latest frame
             switch: enabled             button: capture now
  camera     binary sensor: connection
  Panalapse  switch: pause all

The broker comes from Home Assistant's MQTT service (the Mosquitto add-on)
when the mode is "auto", or from the add-on's own settings when it's "custom".
paho-mqtt runs its network loop in its own thread; everything it hands back is
passed to the asyncio loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from datetime import datetime

import httpx
import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor")
VERSION = os.environ.get("BUILD_VERSION", "dev")

MQTT_DEFAULTS = {
    "mode": "auto",                 # auto | custom | off
    "host": "", "port": 1883, "username": "", "password": "",
    "base_topic": "panalapse",
    "discovery_prefix": "homeassistant",
}


def iso(dt: datetime | None) -> str | None:
    """Timezone-aware ISO time, as Home Assistant timestamp sensors want."""
    return dt.astimezone().isoformat(timespec="seconds") if dt else None


async def supervisor_service_mqtt() -> dict | None:
    """MQTT broker details from Home Assistant, or None outside HA / no broker."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    async with httpx.AsyncClient(timeout=10) as cl:
        r = await cl.get(f"{SUPERVISOR_URL}/services/mqtt",
                         headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        return None
    d = (r.json() or {}).get("data") or {}
    if not d.get("host"):
        return None
    return {"host": d["host"], "port": int(d.get("port") or 1883),
            "username": d.get("username") or "", "password": d.get("password") or "",
            "ssl": bool(d.get("ssl"))}


class MqttBridge:
    def __init__(self, app):
        self.app = app
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: mqtt.Client | None = None
        self.status = "off"          # off | connecting | connected | error | unavailable
        self.detail = ""
        self.broker = ""
        self._published: dict[str, str] = {}      # topic -> last payload (states)
        self._discovered: dict[str, str] = {}     # config topic -> payload
        self._task: asyncio.Task | None = None
        self._pending_images: dict[str, bytes] = {}
        self._wake = asyncio.Event()

    # ------------------------------------------------------------ settings
    @property
    def cfg(self) -> dict:
        s = dict(MQTT_DEFAULTS)
        s.update(self.app.store.data["settings"].get("mqtt") or {})
        return s

    @property
    def base(self) -> str:
        return (self.cfg.get("base_topic") or "panalapse").strip("/")

    @property
    def prefix(self) -> str:
        return (self.cfg.get("discovery_prefix") or "homeassistant").strip("/")

    def public(self) -> dict:
        c = {k: v for k, v in self.cfg.items() if k != "password"}
        c["has_password"] = bool(self.cfg.get("password"))
        return {"settings": c, "status": self.status, "detail": self.detail, "broker": self.broker,
                "supervisor": bool(os.environ.get("SUPERVISOR_TOKEN"))}

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self.loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run())

    async def restart(self):
        await self.stop()
        self.start()

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self.client:
            try:
                if self.client.is_connected():
                    self.client.publish(f"{self.base}/status", "offline", retain=True).wait_for_publish(2)
                self.client.disconnect()
                self.client.loop_stop()
            except Exception:
                pass
            self.client = None
        self.status, self.detail, self.broker = "off", "", ""

    async def _broker(self) -> dict | None:
        c = self.cfg
        if c["mode"] == "off":
            self.status, self.detail = "off", "MQTT is turned off"
            return None
        if c["mode"] == "custom":
            if not c.get("host"):
                self.status, self.detail = "error", "No broker address set"
                return None
            return {"host": c["host"], "port": int(c.get("port") or 1883),
                    "username": c.get("username") or "", "password": c.get("password") or "",
                    "ssl": False}
        try:
            b = await supervisor_service_mqtt()
        except Exception as e:     # supervisor not reachable
            b = None
            self.detail = f"Couldn't ask Home Assistant for the MQTT broker: {e}"
        if not b:
            self.status = "unavailable"
            self.detail = self.detail or ("No MQTT broker found in Home Assistant. Install the Mosquitto "
                                          "add-on, or choose a custom broker."
                                          if os.environ.get("SUPERVISOR_TOKEN")
                                          else "Not running under Home Assistant; choose a custom broker.")
        return b

    async def _run(self):
        b = await self._broker()
        if not b:
            return
        self.broker = f"{b['host']}:{b['port']}"
        self.status, self.detail = "connecting", ""
        cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"panalapse-{secrets.token_hex(3)}")
        if b["username"]:
            cl.username_pw_set(b["username"], b["password"] or None)
        if b.get("ssl"):
            cl.tls_set()
        cl.will_set(f"{self.base}/status", "offline", qos=1, retain=True)
        cl.reconnect_delay_set(1, 60)
        cl.on_connect = self._on_connect
        cl.on_disconnect = self._on_disconnect
        cl.on_connect_fail = self._on_connect_fail
        cl.on_message = self._on_message
        self.client = cl
        cl.connect_async(b["host"], b["port"], keepalive=60)
        cl.loop_start()
        try:
            while True:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                if cl.is_connected():
                    self._sync()
        except asyncio.CancelledError:
            raise

    # paho callbacks (network thread) ------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.status, self.detail = "error", f"Broker refused the connection: {reason_code}"
            log.warning("MQTT: %s", self.detail)
            return
        self.status, self.detail = "connected", ""
        log.info("MQTT connected to %s", self.broker)
        self._published.clear()
        self._discovered.clear()
        client.publish(f"{self.base}/status", "online", qos=1, retain=True)
        client.subscribe([(f"{self.base}/+/+/+/set", 1), (f"{self.base}/paused/set", 1),
                          (f"{self.prefix}/status", 1),
                          (f"{self.prefix}/+/panalapse/+/config", 1)])
        self.loop.call_soon_threadsafe(self._wake.set)

    def _on_connect_fail(self, client, userdata):
        if self.status != "off":
            self.status = "connecting"
            self.detail = f"Can't reach the broker at {self.broker}, retrying"

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if self.status == "connected":
            log.warning("MQTT disconnected (%s), retrying", reason_code)
        if self.status != "off":
            self.status = "connecting"
            self.detail = f"Disconnected: {reason_code}"

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="replace").strip()
        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self._command(msg.topic, payload)))

    # ------------------------------------------------------------ commands
    async def _command(self, topic: str, payload: str):
        if topic == f"{self.prefix}/status":
            if payload == "online":       # Home Assistant restarted: send everything again
                self._published.clear()
                self._discovered.clear()
                self._wake.set()
            return
        if topic.startswith(self.prefix + "/") and topic.endswith("/config"):
            # Retained discovery for something deleted while the add-on was stopped.
            if payload and topic not in self._build_discovery():
                log.info("MQTT: removing stale entity %s", topic)
                self.client.publish(topic, "", qos=1, retain=True)
            return
        parts = topic[len(self.base) + 1:].split("/")
        try:
            if parts == ["paused", "set"]:
                self.app.store.data["settings"]["paused"] = payload.upper() == "ON"
                self.app.store.save()
            elif len(parts) == 4 and parts[0] == "job" and parts[3] == "set":
                jid, what = parts[1], parts[2]
                job = self.app.store.get("jobs", jid)
                if not job:
                    return
                if what == "enabled":
                    job["enabled"] = payload.upper() == "ON"
                    self.app.store.save()
                    self.app.scheduler.reschedule(jid)
                elif what == "capture":
                    log.info("MQTT: capture now for %s", job["name"])
                    try:
                        await self.app.scheduler.capture(jid)
                    except Exception:
                        pass     # reported through the job's state
        finally:
            self._wake.set()

    # ------------------------------------------------------------ publishing
    def job_updated(self, job_id: str, image: bytes | None):
        """Called by the scheduler after every capture attempt."""
        if image is not None:
            self._pending_images[job_id] = image
        if self.loop:
            self._wake.set()

    def _pub(self, topic: str, payload, retain=True, cache=True):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, separators=(",", ":"))
        if cache and self._published.get(topic) == payload:
            return
        self.client.publish(topic, payload, qos=1, retain=retain)
        if cache:
            self._published[topic] = payload

    def _sync(self):
        self._discovery()
        st = self.app.store.data
        paused = bool(st["settings"].get("paused"))
        self._pub(f"{self.base}/paused/state", "ON" if paused else "OFF")
        for job in st["jobs"]:
            self._pub(f"{self.base}/job/{job['id']}/state", self.app.job_status(job))
        for cam in st["cameras"]:
            e = self.app.cameras.get(cam["id"])
            self._pub(f"{self.base}/camera/{cam['id']}/state",
                      {"online": bool(e and e.client.online),
                       "error": (e.client.last_error if e and not e.client.online else None)})
        for jid, data in list(self._pending_images.items()):
            self.client.publish(f"{self.base}/job/{jid}/image", data, qos=0, retain=True)
            self._pending_images.pop(jid, None)

    def _discovery(self):
        want = self._build_discovery()
        for topic in list(self._discovered):
            if topic not in want:
                self.client.publish(topic, "", qos=1, retain=True)
                del self._discovered[topic]
        for topic, payload in want.items():
            if self._discovered.get(topic) != payload:
                self.client.publish(topic, payload, qos=1, retain=True)
                self._discovered[topic] = payload

    def _build_discovery(self) -> dict[str, str]:
        """config topic -> JSON payload for every entity that should exist."""
        st = self.app.store.data
        base, pre = self.base, self.prefix
        avail = {"availability_topic": f"{base}/status"}
        hub = {"identifiers": ["panalapse_hub"], "name": "Panalapse", "manufacturer": "Panalapse",
               "model": "Timelapse add-on", "sw_version": VERSION}
        want: dict[str, dict] = {}

        def add(component: str, uid: str, cfg: dict):
            cfg = {**avail, "unique_id": f"panalapse_{uid}", **cfg}
            want[f"{pre}/{component}/panalapse/{uid}/config"] = cfg

        add("switch", "pause_all", {
            "name": "Pause all", "icon": "mdi:pause-circle", "device": hub,
            "state_topic": f"{base}/paused/state", "command_topic": f"{base}/paused/set"})

        for cam in st["cameras"]:
            cid = cam["id"]
            info = cam.get("info") or {}
            dev = {"identifiers": [f"panalapse_{cid}"], "name": cam.get("name") or cam["host"],
                   "manufacturer": "Panasonic", "model": info.get("model") or "AW camera",
                   "sw_version": info.get("firmware"), "via_device": "panalapse_hub",
                   "configuration_url": f"http://{cam['host']}"}
            add("binary_sensor", f"{cid}_online", {
                "name": "Connection", "device_class": "connectivity", "device": dev,
                "entity_category": "diagnostic",
                "state_topic": f"{base}/camera/{cid}/state",
                "value_template": "{{ 'ON' if value_json.online else 'OFF' }}"})

        for job in st["jobs"]:
            jid = job["id"]
            t = f"{base}/job/{jid}"
            dev = {"identifiers": [f"panalapse_{jid}"], "name": f"Timelapse {job['name']}",
                   "manufacturer": "Panalapse", "model": "Timelapse", "via_device": "panalapse_hub"}
            common = {"device": dev, "state_topic": f"{t}/state"}
            add("sensor", f"{jid}_status", {
                **common, "name": "Status", "icon": "mdi:timelapse", "device_class": "enum",
                "options": ["capturing", "scheduled", "finished", "paused", "disabled", "error"],
                "value_template": "{{ value_json.status }}",
                "json_attributes_topic": f"{t}/state"})
            add("sensor", f"{jid}_last", {
                **common, "name": "Last capture", "device_class": "timestamp",
                "value_template": "{{ value_json.last_capture or None }}"})
            add("sensor", f"{jid}_next", {
                **common, "name": "Next capture", "device_class": "timestamp",
                "value_template": "{{ value_json.next_capture or None }}"})
            add("sensor", f"{jid}_frames", {
                **common, "name": "Frames", "icon": "mdi:image-multiple", "state_class": "total_increasing",
                "value_template": "{{ value_json.frames }}"})
            add("sensor", f"{jid}_error", {
                **common, "name": "Last error", "icon": "mdi:alert-circle-outline",
                "entity_category": "diagnostic",
                "value_template": "{{ (value_json.last_error or 'None')[:250] }}"})
            add("binary_sensor", f"{jid}_problem", {
                **common, "name": "Problem", "device_class": "problem",
                "value_template": "{{ 'ON' if value_json.problem else 'OFF' }}"})
            add("image", f"{jid}_image", {
                "device": dev, "name": "Latest image", "image_topic": f"{t}/image",
                "content_type": "image/jpeg"})
            add("switch", f"{jid}_enabled", {
                **common, "name": "Enabled", "icon": "mdi:play-pause",
                "value_template": "{{ 'ON' if value_json.enabled else 'OFF' }}",
                "command_topic": f"{t}/enabled/set"})
            add("button", f"{jid}_capture", {
                "device": dev, "name": "Capture now", "icon": "mdi:camera-iris",
                "command_topic": f"{t}/capture/set", "payload_press": "PRESS"})

        return {k: json.dumps(v, separators=(",", ":")) for k, v in want.items()}
