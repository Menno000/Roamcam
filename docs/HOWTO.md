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

## The clock

There's no battery-backed clock in this hardware, and no internet in standalone use — so Roamcam sets the system clock from **GPS**. As soon as there's a fix, timestamps (and clip filenames) are correct, to the second. Nothing to configure.

## Coexisting with Hivemapper

Roamcam never deletes or reflashes the Hivemapper software. "Standalone" mode simply pauses the camera capture and the ML privacy blur while it records, and keeps the rest running (that's what provides GPS and the autostart). The **↩ Restore Hivemapper** link in Settings brings the stock camera fully back whenever you want it.
