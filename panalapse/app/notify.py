"""Tell Home Assistant when a timelapse starts failing, and when it recovers.

Works without MQTT, through the Supervisor's Home Assistant API:
  - fires the events panalapse_capture_failed / panalapse_capture_recovered,
    which automations can trigger on (e.g. to send a phone notification)
  - optionally shows a persistent notification in Home Assistant, which is
    dismissed again when the timelapse recovers

Only the change from working to failing (and back) is reported, not every
failed shot.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)
SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor")


class HaNotifier:
    def __init__(self, app):
        self.app = app
        self.failing: dict[str, bool] = {}
        self.last_result: str | None = None

    @property
    def available(self) -> bool:
        return bool(os.environ.get("SUPERVISOR_TOKEN"))

    async def _call(self, path: str, body: dict):
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as cl:
                r = await cl.post(f"{SUPERVISOR_URL}/core/api/{path}", json=body,
                                  headers={"Authorization": f"Bearer {token}"})
            self.last_result = f"{path}: HTTP {r.status_code}"
            if r.status_code >= 300:
                log.warning("Home Assistant API %s answered %s: %s", path, r.status_code, r.text[:200])
        except httpx.HTTPError as e:
            self.last_result = f"{path}: {e}"
            log.warning("Couldn't reach Home Assistant API (%s): %s", path, e)

    async def job_result(self, job: dict, ok: bool, error: str | None):
        jid = job["id"]
        was_failing = self.failing.get(jid, False)
        self.failing[jid] = not ok
        if ok == (not was_failing):
            return                      # no change
        settings = self.app.store.data["settings"]
        cam = self.app.store.get("cameras", job.get("camera_id", "")) or {}
        data = {"job_id": jid, "job": job["name"], "camera": cam.get("name"),
                "error": error}
        notif_id = f"panalapse_{jid}"
        if not ok:
            log.warning("%s: now failing (%s)", job["name"], error)
            await self._call("events/panalapse_capture_failed", data)
            if settings.get("ha_notifications", True):
                await self._call("services/persistent_notification/create", {
                    "notification_id": notif_id,
                    "title": f"Panalapse: {job['name']} is failing",
                    "message": f"Capturing from **{cam.get('name') or 'the camera'}** failed:\n\n"
                               f"`{error}`\n\nThis clears itself when a shot succeeds again."})
        else:
            log.info("%s: working again", job["name"])
            await self._call("events/panalapse_capture_recovered", data)
            if settings.get("ha_notifications", True):
                await self._call("services/persistent_notification/dismiss",
                                 {"notification_id": notif_id})
