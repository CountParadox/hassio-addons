# Changelog

## 0.2.0
- New **Images** tab: browse saved stills by day, open full size, step with the arrow keys or by swiping, play a day back as a preview, download originals.
- **MQTT** with Home Assistant discovery: a device per timelapse (status, last / next capture, frames, problem, last error, latest image, enable switch, capture-now button), a connection sensor per camera and a pause-all switch. Uses the Mosquitto add-on automatically, or any broker.
- **Failure alerts**: a Home Assistant notification plus `panalapse_capture_failed` / `panalapse_capture_recovered` events when a timelapse starts failing or recovers.
- Cameras are checked every 30 seconds, so "offline" shows up even when nothing is being captured.
- Clearer camera errors ("no answer (timed out)").
- Listing large folders on a network share is much faster.
- The browser always loads the current UI after an update.

## 0.1.1
- Live page: one-touch auto focus, white balance set and black balance set ("Auto once").
- Removed the manual power on / standby buttons. Timelapses can still wake a camera from standby.

## 0.1.0
- First release: multi-camera live preview, PTZ, presets, scheduled timelapses to SMB/local storage, MP4 rendering.
