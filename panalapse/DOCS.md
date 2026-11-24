# Panalapse

Timelapse capture for Panasonic AW-series PTZ cameras, all over IP:

- **Live preview** with pan / tilt / zoom / focus controls (hold a button, or use the arrow keys), plus one-touch auto focus, white balance and black balance
- **Presets**: save the current position to any slot (1–100) with a name and a thumbnail, and recall it
- **Several cameras**, each with a nickname, and a network scan that finds them for you
- **Scheduled timelapses**: interval, start / end time (overnight windows allowed), days of the week, optional date range
- **Before each shot**: go to a preset and wait for it to settle, apply auto or manual exposure, send extra commands, wake the camera from standby
- **Saves stills straight to a network share** (UNC path such as `\\nas\timelapse\site1`, over SMB 2/3), or to `/share` / `/media`
- **Turns a folder of stills into an MP4** (with optional flicker reduction), and copies it back to the share
- **Images tab**: browse the saved stills day by day, open any frame full size, and play a day back as a quick preview
- **Home Assistant devices over MQTT**: status, last / next capture, frame count, latest image, on/off switch and a "Capture now" button for each timelapse
- **Failure alerts**: a Home Assistant notification and an event when a timelapse starts failing, cleared when it recovers

## Getting started

1. Open **Panalapse** from the sidebar.
2. **Cameras & storage → Find cameras** scans your network and lists the AW cameras it finds; press **Add** on one. Or use **Add camera**: give it a nickname, its IP address, and the user name / password (only needed if the camera asks for them). The add-on works out the model, the largest snapshot size and which image settings the camera supports.
3. **Add storage**: pick *Network share*, enter the UNC path (for example `\\192.168.1.10\timelapse`) and a user who can write to it. Saving runs a write test.
4. **Live**: frame the shot, then save it as a preset.
5. **Timelapses → New timelapse**: choose the camera, the storage, how often and when, and the preset. Use **Test shot** to check it, then **Save**.

## Finding cameras

**Find cameras** asks every address on the networks you list for a Panasonic camera model, using the same command Detect uses. It fills in the network Home Assistant is on and those of cameras you've already added; you can type others:

- a subnet: `192.168.1.0/24`
- a range: `10.0.0.10-40`
- several, separated by commas

Each address gets one short request, so it won't swamp the cameras. A /24 (254 addresses) takes about 5–10 seconds, and one scan covers at most 1024 addresses.

If your cameras need a login to answer, enter the user name and password before scanning. Without one, a camera that needs a login shows as **Login needed**. So can other devices with a web login, such as a router; scanning again with the camera's login confirms which ones are cameras. Cameras you've already added are marked and aren't contacted.

Panasonic's own *Easy IP Setup* tool uses a broadcast that doesn't get out of an add-on's network, which is why Panalapse scans instead. It only finds cameras on the normal web port (80).

## File names

Folder and file names are built from placeholders:

| Placeholder | Example |
|---|---|
| `{job}` | timelapse name |
| `{camera}` | camera nickname |
| `{date}` | `2026-11-23` |
| `{time}` | `093000` |
| `{datetime}` | `20261123_093000` |
| `{year}` `{month}` `{day}` `{hour}` | `2026` `11` `23` `09` |
| `{seq:06d}` | `000042` (frame counter for that timelapse) |

The defaults, folder `{job}/{date}` and file `{job}_{datetime}.jpg`, keep one folder per day and sort correctly when you make a video.

## Exposure

Each timelapse can either **leave the camera alone**, set it to **Auto** (iris, gain, white balance and so on to auto, as far as the model allows) or apply **Manual** values. Only the settings your camera reported when it was detected are shown. Settings are re-sent whenever something else (another timelapse, or you on the Live page) has touched the camera.

For anything the form doesn't cover, add raw Panasonic commands under *Extra commands*, one per line, e.g. `OSD:48:3A`. Commands starting with `#` go to `aw_ptz`, the rest to `aw_cam`. You can try commands on the Live page under *Send a raw command*.

### Auto once

The **Auto once** buttons on the Live page run the camera's one-shot functions:

- **Focus**: one-touch auto focus (`OSE:69:1`). The camera focuses once and then stays put, so the focus mode needs to be *Manual*.
- **White**: auto white balance set (`OWS`). It stores the result in the AWB A or AWB B memory, so set white balance to one of those first.
- **Black**: auto black balance set (`OAS`).

If the camera doesn't support a function, or it isn't allowed in the current mode, you'll see a message saying which.

### Model notes

- **AW-UE4** (tested, firmware V01.80):
  - Snapshots go up to 1280×720. Changing snapshot size takes the camera about 5 seconds, so keep the preview size the same as the timelapse size (the default, "largest", does that).
  - Over IP it lets you change **brightness (−4…+4)** and **white balance** (ATW, AWB A/B, 3200K, 5600K). Iris, gain and shutter are read-only.
  - Pan and tilt move within the picture, so they only work once you've zoomed in.
  - Focus can't be controlled over IP: **Auto once → Focus** reports "not supported".
  - **Auto once → White** works when white balance is set to AWB A or AWB B.
- Larger models (UE150, UE100, HE130 …) support 1920 snapshots and the full manual exposure set.

**Detect** (Cameras & storage) works out what your camera allows. It tries each setting and then puts the original value back, so the picture changes briefly for about 30 seconds. It runs when you add a camera or press Detect. When the add-on starts it only re-reads the model and the current values.

Each timelapse re-sends its preset and settings before every shot by default, so it recovers if a control panel or someone on the camera's web page changes things. If nothing else ever touches the camera, you can turn that off to save a few seconds per shot.

## Browsing images

**Images** lists the stills a timelapse has saved, read straight from its storage. Pick a timelapse and a day (today opens on the newest frames). *Show every Nth* thins out a long day.

Click a frame to open it full size:

- **← / →** (or swipe on a phone) step through the day.
- **Play** (or the space bar) runs the day as a quick preview at 4, 8 or 15 frames a second.
- **Download** saves the original file.

Thumbnails are made on first view and cached in the add-on (up to about 300 MB, oldest removed first). The list is re-read every minute, and straight away when you press **Refresh**. New captures show up as they're taken.

## Home Assistant

### MQTT devices

With the **Mosquitto broker** add-on installed (Settings → Add-ons), Panalapse finds it by itself. Nothing needs setting up. Each timelapse then appears under Settings → Devices as **Timelapse *name***, with:

| Entity | What it is |
|---|---|
| Status | capturing, scheduled, paused, disabled, finished or error. Its attributes hold the full state. |
| Last capture / Next capture | timestamps |
| Frames | total frames saved |
| Problem | on while the last shot failed |
| Last error | the error text (diagnostic) |
| Latest image | the most recent frame, for dashboards |
| Enabled | switch to turn the timelapse on and off |
| Capture now | button that takes and saves a shot immediately |

Each camera gets a **Connection** sensor, and the **Panalapse** device has a **Pause all** switch.

To use a different broker, or to turn MQTT off, go to **Cameras & storage → Home Assistant**. The topic prefix (default `panalapse`) and discovery prefix (default `homeassistant`) can be changed there too. Entities for deleted timelapses and cameras are removed automatically.

### Failure alerts

When a timelapse goes from working to failing, Panalapse:

- shows a notification in Home Assistant (you can turn this off on the Home Assistant card), and
- fires the event `panalapse_capture_failed` with `job`, `job_id`, `camera` and `error`.

When it works again, the notification is dismissed and `panalapse_capture_recovered` is fired. Each change is reported once, not every failed shot. This doesn't need MQTT.

To get failures on your phone, add an automation like this:

```yaml
alias: Panalapse failure to phone
triggers:
  - trigger: event
    event_type: panalapse_capture_failed
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: "Timelapse {{ trigger.event.data.job }} is failing"
      message: "{{ trigger.event.data.error }}"
```

With MQTT you can trigger on the timelapse's **Problem** sensor instead, for example "on for 10 minutes".

## Making videos

**Videos → Make a video**: pick a timelapse (or type a folder), a frame rate, and optionally a date range or "every Nth image" to speed it up. Images are streamed from the share into ffmpeg, so they aren't copied onto the Home Assistant disk. The finished MP4 is kept in the add-on, where you can play or download it, and is also copied to `<folder>/videos/` on the share.

## Network and security

- Cameras are controlled over HTTP with the Panasonic CGI interface (`/cgi-bin/aw_ptz`, `/cgi-bin/aw_cam`, `/cgi-bin/camera`). Basic and digest auth are both handled.
- The UI is normally reached through the Home Assistant sidebar (ingress), which uses your HA login. You can also map port 8099 in the add-on's *Network* settings for direct access, but that port has **no login**, so only do it on a trusted network.
- Camera and share passwords are stored in the add-on's private `/data` folder and are never sent back to the browser.

## Troubleshooting

- **Camera shows offline**: check you can open `http://<camera-ip>/` from the HA host's network. These cameras' web servers can stop answering for a short while if they get too many requests too fast; Panalapse spaces its requests out, but other software polling the camera at the same time can still trigger it.
- **Live picture doesn't load**: the message under the picture now says why. "Larger than this camera offers" means the camera's *Preview size* (Cameras & storage → Edit) is bigger than it supports; set it to *Largest the camera offers*.
- **"ER3" errors** when applying settings: the camera refused that value in its current mode (for example, setting the iris position while the iris is on auto).
- **MQTT shows "no broker"**: install and start the Mosquitto broker add-on, then press **Save** on the Home Assistant card (or restart Panalapse).
- **Storage test fails**: use the server's IP instead of its name, check that the share allows SMB2 or SMB3, and for a domain account fill in *Domain*.
