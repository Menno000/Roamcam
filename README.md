# Roamcam

**Your Hivemapper dashcam, working for you instead of the network.**

The Hivemapper HDC is a lovely little box: a Raspberry Pi Compute Module 4, a 12&nbsp;MP Sony sensor, GPS, an accelerometer, three RGB LEDs. Out of the box it exists to feed one thing — the Hivemapper map. Roamcam turns it into a plain, private, offline **dashcam** that answers to nobody but you.

No account. No cloud. No crypto. It never phones home. Everything runs on the device and shows up in a clean web dashboard you open from your phone or laptop.

> Made in the Netherlands 🇳🇱, for anyone who has one of these cameras sitting in a drawer.

<!-- Drop a screenshot of your dashboard into docs/screenshots/dashboard.png and uncomment the next line:
![The dashboard](docs/screenshots/dashboard.png)
-->
> 📸 *Screenshots go here — see [docs/screenshots](docs/screenshots/) for what to capture.*

---

## What you get

- **Loop recording** to the internal storage — 1080p30, encoded on the Pi's hardware H.264 encoder, so it barely touches the CPU. Clips are timestamped `.mp4` files you can play in anything.
- **A live web dashboard** on `http://<device-ip>:8080` — camera view, sensors, recordings, settings, system health. No app to install.
- **Real GPS & a real G-sensor.** The u-blox module gives you position, speed, heading and satellite health; the IMU gives you a live accelerometer/gyro read-out with an oscilloscope and shock detection.
- **The clock sets itself from GPS.** No internet, no battery clock needed — the moment there's a fix, timestamps are correct.
- **The three status LEDs are yours.** Pick a function per LED: GPS-fix, motion, CPU temperature, speed, a fixed colour, or a slow *breathing* red that shows you it's recording.
- **Automatic retention.** Set a storage limit; the oldest clips are deleted to make room. Set-and-forget loop recording, exactly like a commercial dashcam.
- **It just starts.** Give it power and it boots straight into recording — perfect for wiring into a car. Survives reboots, no login, no button.

It's light: the dashboard sits around 0–6% of one CPU core and ~28&nbsp;MB of RAM.

---

## What you need

- A **Hivemapper HDC** dashcam (the Raspberry Pi CM4 based model). Root SSH is open by default on these — that's what makes this possible.
- The camera and your computer on the same network. The HDC broadcasts its own Wi-Fi access point (`dashcam`), or you can put it on your LAN.
- Five minutes.

> This is built and tested on the **HDC**. The newer *Bee* runs different firmware and isn't covered yet — see [docs/HARDWARE.md](docs/HARDWARE.md).

---

## Quick start

SSH into the camera (default user `root`, no password) and run:

```sh
# from your computer, copy the two files over
scp dashboard_server.py install.sh root@192.168.0.10:/tmp/

# then on the camera
ssh root@192.168.0.10
cd /tmp && sh install.sh
```

That's it. Open `http://192.168.0.10:8080` and you're looking at your dashcam.

The installer copies the app to the device's persistent storage, wires it into the boot process so it comes back after every power cycle, and starts it. Full walkthrough with pictures: **[docs/INSTALL.md](docs/INSTALL.md)**.

Prefer Dutch? De volledige handleiding staat in **[het Nederlands](README.nl.md)**.

---

## How it works (the short version)

The HDC's root filesystem is read-only (squashfs) with a RAM overlay, so nothing you change normally survives a reboot. Roamcam works *with* that instead of fighting it:

- The app and its settings live on `/mnt/data`, the one partition that persists.
- Autostart hooks into the camera's own boot process, so the dashboard comes up on every power-up — no systemd unit to lose, no manual step.
- Recording takes over the camera cleanly and hands it back when you stop. The GPS and autostart plumbing keep running the whole time.

There's nothing to compile. It's a single ~1300-line Python file using only the standard library, plus the `libcamera` and `ffmpeg` tools already on the device. Read it, change it, break it — it's yours.

More detail in **[docs/HOWTO.md](docs/HOWTO.md)**.

---

## The dashboard

| Tab | What's in it |
|-----|--------------|
| **Live** | Camera image, GPS/GNSS, RF & anti-jamming telemetry, and a live IMU oscilloscope with G-force peak-hold |
| **Playback** | Your clips as a clean list — play, download or delete |
| **Settings** | Start/stop recording, segment length, storage limit, and per-LED functions |
| **System** | CPU, temperature, memory, storage, services, network, firmware slots, full config |

---

## A word on safety and the law

This is a hobby project shared as-is. A few honest notes:

- Dashcam laws differ by country — filming, audio, where you may mount it, and what you may share. Check your local rules. (This hardware has **no microphone**, so it's video only.)
- Reflashing or modifying an embedded device always carries some risk. Roamcam deliberately avoids touching the firmware image for exactly that reason, but you run it at your own risk.
- Mount it so it never blocks your view or becomes a projectile in a crash.

---

## Roadmap

- [x] GPS overlay on playback via synced subtitles — an `.srt` next to every clip (plays in VLC & most dashcam viewers) and a live overlay in the dashboard's own player
- [ ] Day/night exposure profiles in the UI
- [ ] Optional push of clips/telemetry to Home Assistant when it's on a network with internet
- [ ] Motion / parking mode using the G-sensor

Ideas and pull requests welcome.

---

## Support this project ☕

I build this in my spare time and give it away for free. If Roamcam saved you buying a dashcam, or you just think it's neat, a coffee genuinely makes my day and keeps the work going:

**[☕ Buy me a coffee](https://www.buymeacoffee.com/YOUR_HANDLE)**

*(Maintainer: replace `YOUR_HANDLE` with your real link, and update `.github/FUNDING.yml`.)*

---

## Credits & license

Built by a Dutch tinkerer who didn't want a perfectly good camera to go to waste. Not affiliated with, endorsed by, or connected to Hivemapper.

Released under the [MIT License](LICENSE) — do what you like, no warranty.
