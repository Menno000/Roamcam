# How to use Roamcam

Everything lives in the web dashboard at `http://<device-ip>:8080`. Four tabs.

## Live

- **Camera** — the current view. In stock (Hivemapper) mode this is the firmware's frame; while recording it's your own feed.
- **GPS / GNSS** — fix type (2D/3D), position, speed, heading, altitude, satellites, accuracy and dilution-of-precision. There's a link to the position on a map.
- **RF / anti-jamming** — the u-blox module's own signal-health read-out: jamming state, antenna status, noise, AGC.
- **Live IMU / G-sensor** — a rolling oscilloscope of the three accelerometer axes, plus gyro, chip temperature, tilt, and a G-force peak-hold. Tap the device and watch it jump.

## Recording

Go to **Settings → Recorder**.

- **Start recording** puts the camera into standalone dashcam mode: the Hivemapper capture is paused and Roamcam records to `/mnt/data/clips` as timestamped `.mp4` files.
- **Camera off** stops recording but stays standalone (camera idle, Hivemapper not brought back). Handy for privacy or when parked.
- **Segment length** — how long each clip is (30 s to 5 min). Shorter clips are easier to manage; longer ones mean fewer files.
- **Storage limit** — the oldest clips are automatically deleted once total size passes this. Classic loop recording.
- **Incident lock** — pick a G-force threshold (or Off). When the accelerometer sees an impact or hard braking above it, the current and previous clip are protected 🔒 and excluded from automatic deletion. Protected clips are highlighted in the Playback list; use the 🔒/🔓 button to protect or release any clip yourself.

## Language and units

**Settings → Preferences** switches the interface between **English and Dutch**, and between **km/h · metres · °C** and **mph · feet · °F**. Both are stored on the device, and the units also apply to the GPS overlay written into new recordings.

Recording is **persistent**: turn it on, and the device resumes recording on every power-up. Wired into a car, it records whenever the ignition powers it and stops when you switch off. No button, no app.

GPS position and G-sensor data are logged alongside the video in `clips/track.ndjson`, and every finished clip gets a matching `.srt` subtitle with the time, position, speed and heading for each second.

## Playback

**Playback** lists your clips, newest first, with time and size. Hit ▶ to play one in the player at the top, or download / delete it. Delete asks first.

With more than a handful of clips, one-by-one deleting gets old fast, so Playback also has:

- **Filter** — All / 🔒 Locked / Unlocked, to quickly narrow a long list
- **Select all** + per-clip checkboxes, then **delete selected**
- **Delete all** — removes every *unlocked* clip in one go (locked ones are always kept); asks for confirmation with a count
- **Show more** — the list loads 50 at a time so it stays fast even with hundreds of recordings

Each clip has a synced **GPS overlay** — time, position, speed and heading — shown over the video in the dashboard player (toggle it with the CC button). The same data is saved as a standard `.srt` next to the clip, so VLC and most dashcam viewers show it too when you download a clip.

## The status LEDs

The HDC has three RGB LEDs. Under **Settings → LED control** you assign a function to each one:

| Function | Behaviour |
|----------|-----------|
| Off | Dark |
| Recording (breathing red) | Slowly fades in and out while recording — a calm heartbeat, not a blink |
| GPS fix | Green on a 3D lock, amber on 2D, red when searching |
| Motion / shock | Flashes red on a G-force spike (hard braking, a bump) |
| CPU temperature | Green → amber → red as it heats up |
| Speed | Colour by km/h |
| Fixed colour | Red, green, blue, yellow, white or purple |

Switch the whole set back to the camera's original status behaviour any time with the **Dashcam** toggle.

## System

Everything about the device: CPU load per core, temperature, memory, storage, running services, network, firmware slots, and the full configuration dump. Useful for a quick health check.

## Live preview

While recording, the camera is normally left completely alone — the Live tab shows an overlay instead of an image, and that costs 0% extra CPU. Flip the **Live preview** button on and the dashboard starts polling a live, low-resolution feed; flip it off (or leave the Live tab) and it stops immediately. It's manual and it resets to *off* every time you load the dashboard — it never keeps running unattended.

**Why manual, and why it costs something:** the camera can only be read by one process, so the preview has to tap the same H.264 stream the recorder is already producing. A small, essentially free side-channel (plain stream-copy, no re-encoding — measured at 0% extra CPU) keeps the last second of raw video in RAM. Turning preview on makes the dashboard periodically decode one frame from that buffer using the Pi's **hardware** H.264 decoder — measured at roughly +10–15 percentage points of total CPU while it's on, dropping straight back to baseline the instant it's off. That's a fair trade for an occasional manual check, but not something you'd want running for hours unattended, which is exactly why it defaults off.

### Toward a companion app (not built yet — the groundwork)

A phone app for live video (and push notifications on an incident lock, say) is a natural next step, but it deserves proper thought before writing code, so here's what's already been worked out:

- **The hard part is solved**: hardware-decoded frame extraction from the live recording stream at near-zero idle cost. Whatever consumes it next — a web view, a native app, a Home Assistant camera entity — can build on the same `/live_preview.jpg` mechanism or a proper MJPEG/RTSP stream derived from the same buffer.
- **Local network vs. remote access are different problems.** On the same Wi-Fi as the camera, an app can hit the dashboard directly, same as a browser does now. Watching live video from *outside* the car's network needs either the device having its own internet connection (a SIM/hotspot — a deliberate choice this project has avoided so far, see the main README) or relaying through something that does, e.g. Home Assistant once the device is home on a network with internet.
- **Push notifications** (incident lock triggered, recording stopped unexpectedly) would need *some* server or service the phone can be reached through — plain local HTTP polling doesn't reach a phone that's off the local network. Home Assistant is the natural fit here too, since it already does notifications well and Roamcam is designed to hand it data once there's a network path.
- **Bandwidth and battery** matter more on a phone/cellular than on a browser tab on the same LAN — a companion app would want a deliberately low frame rate and resolution, more like a security-camera app than a video call.

None of this needs solving before Roamcam is useful today — it's here so the shape of "an app later" is thought through rather than bolted on.

## The clock

There's no battery-backed clock in this hardware, and no internet in standalone use — so Roamcam sets the system clock from **GPS**. As soon as there's a fix, timestamps (and clip filenames) are correct, to the second. Nothing to configure.

## Coexisting with Hivemapper

Roamcam never deletes or reflashes the Hivemapper software. "Standalone" mode simply pauses the camera capture and the ML privacy blur while it records, and keeps the rest running (that's what provides GPS and the autostart). The **↩ Restore Hivemapper** link in Settings brings the stock camera fully back whenever you want it.
