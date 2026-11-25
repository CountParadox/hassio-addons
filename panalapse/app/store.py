"""Persistent JSON config: cameras, destinations, jobs, presets, counters."""
from __future__ import annotations

import json
import os
import secrets
import threading

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_FILE = os.path.join(DATA_DIR, "panalapse.json")

DEFAULT = {
    "cameras": [],
    "destinations": [],
    "jobs": [],
    "settings": {"paused": False},
    "counters": {},   # job_id -> frames captured so far (used for {seq})
}

CAMERA_DEFAULTS = {
    "name": "", "host": "", "port": 80, "username": "", "password": "",
    "preview_resolution": 0,      # 0 = camera's max snapshot size
    "stream_path": "",            # RTSP path for full-resolution stills; "" = Panasonic default
    "presets": {},                # "1" -> {"name": str, "thumb": bool}
    "info": {},                   # last probe result
}

JOB_DEFAULTS = {
    "name": "New timelapse",
    "enabled": False,
    "camera_id": "",
    "destination_id": "",
    "interval_seconds": 60,
    "days": [0, 1, 2, 3, 4, 5, 6],        # Monday = 0
    "start_time": "00:00",
    "end_time": "00:00",                  # equal to start = all day; earlier = runs overnight
    "start_date": "",
    "end_date": "",
    "preset": 0,                          # 0 = don't move
    "settle_seconds": 5,
    "exposure_mode": "leave",             # leave | auto | manual
    "exposure": {},                       # control key -> value (manual mode)
    "custom_commands": "",                # one command per line, sent before each capture
    "power_on": True,                     # wake the camera from standby if needed
    "reapply": True,                      # re-send preset + settings before every shot
    "resolution": 0,                      # 0 = largest snapshot, -1 = frame from video stream
    "folder_template": "{job}/{date}",
    "filename_template": "{job}_{datetime}.jpg",
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


class Store:
    def __init__(self):
        self._lock = threading.Lock()
        self.data = json.loads(json.dumps(DEFAULT))
        self.load()

    def load(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            for k, v in DEFAULT.items():
                loaded.setdefault(k, json.loads(json.dumps(v)))
            for c in loaded["cameras"]:
                for k, v in CAMERA_DEFAULTS.items():
                    c.setdefault(k, json.loads(json.dumps(v)))
            for j in loaded["jobs"]:
                for k, v in JOB_DEFAULTS.items():
                    j.setdefault(k, json.loads(json.dumps(v)))
            self.data = loaded

    def save(self):
        with self._lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, CONFIG_FILE)

    # ----------------------------------------------------------- lookups
    def get(self, kind: str, item_id: str) -> dict | None:
        return next((x for x in self.data[kind] if x["id"] == item_id), None)

    def upsert(self, kind: str, item: dict, defaults: dict, prefix: str,
               secret_fields: tuple = ("password",)) -> dict:
        existing = self.get(kind, item.get("id", "")) if item.get("id") else None
        if existing:
            for f in secret_fields:
                # An empty / missing password from the UI means "keep the saved one".
                if not item.get(f):
                    item.pop(f, None)
            existing.update(item)
            obj = existing
        else:
            obj = json.loads(json.dumps(defaults))
            obj.update(item)
            obj["id"] = new_id(prefix)
            self.data[kind].append(obj)
        self.save()
        return obj

    def delete(self, kind: str, item_id: str):
        self.data[kind] = [x for x in self.data[kind] if x["id"] != item_id]
        self.save()
