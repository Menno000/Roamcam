# Roamcam

**Your Hivemapper dashcam, working for you instead of the network.**

The Hivemapper HDC is a lovely little box: a Raspberry Pi Compute Module 4, a 12&nbsp;MP Sony sensor, GPS, an accelerometer, three RGB LEDs. Out of the box it exists to feed one thing — the Hivemapper map. Roamcam turns it into a plain, private, offline **dashcam** that answers to nobody but you.

No account. No cloud. No crypto. It never phones home. Everything runs on the device and shows up in a clean web dashboard you open from your phone or laptop.

> Made in the Netherlands 🇳🇱, for anyone who has one of these cameras sitting in a drawer.

### 🖥️ Try it without the hardware

Open **[`demo.html`](demo.html)** in any browser (download it, or clone and double-click it) to click through the whole dashboard with sample data — every tab, live-updating, no device needed. It's the fastest way to see what you're getting.

### 📸 Screenshots

<p>
  <img src="docs/screenshots/dashboard.png" width="49%" alt="Live tab: camera, IMU oscilloscope, GPS/GNSS and RF telemetry">
  <img src="docs/screenshots/playback.png" width="49%" alt="Playback tab: clip list with lock, filter and bulk delete">
</p>
<p>
  <img src="docs/screenshots/settings.png" width="49%" alt="Settings tab: recorder, preferences and per-LED control">
  <img src="docs/screenshots/system.png" width="49%" alt="System tab: CPU, storage, services, network, firmware">
</p>

*All shown with `demo.html`'s sample data — demo location (Amsterdam), fake device IDs. Never a real device or a real location.*

---

## What you get

- **Loop recording** to the internal storage — 1080p30, encoded on the Pi's hardware H.264 encoder, so it barely touches the CPU. Clips are timestamped `.mp4` files you can play in anything.
- **A live web dashboard** on `http://<device-ip>:8080` — camera view, sensors, recordings, settings, system health. No app to install.
- **Real GPS & a real G-sensor.** The u-blox module gives you position, speed, heading and satellite health; the IMU gives you a live accelerometer/gyro read-out with an oscilloscope and shock detection.
- **The clock sets itself from GPS.** No internet, no battery clock needed — the moment there's a fix, timestamps are correct.
- **The three status LEDs are yours.** Pick a function per LED: GPS-fix, motion, CPU temperature, speed, a fixed colour, or a slow *breathing* red that shows you it's recording.
- **Automatic retention.** Set a storage limit; the oldest clips are deleted to make room. Set-and-forget loop recording, exactly like a commercial dashcam.
- **Incident lock.** An impact or hard stop above your chosen G-threshold automatically protects that clip 🔒 — it's never overwritten by the loop. You can lock or unlock any clip by hand too.
- **English or Dutch, metric or imperial.** Switch the whole interface between EN/NL and between km/h·m·°C and mph·ft·°F. The GPS overlay in new recordings follows your choice.
- **Live preview, on demand.** While recording, the camera is normally left alone (0% extra CPU) — flip on "Live preview" in the Live tab to watch along, using the Pi's hardware decoder for a low-resolution feed. It defaults to *off* every time you open the dashboard and turns itself off if you leave the Live tab, so it never runs unattended.
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

Never typed a command in your life? You're copying two files onto the camera and running one line — here's exactly where to click and type.

**1. Open a terminal.** This is just a plain black text window that comes with every computer:

- **Windows:** press the ⊞ Windows key, type `PowerShell`, press Enter.
- **Mac:** press `Cmd + Space`, type `Terminal`, press Enter.
- **Linux:** you know where it is.

**2. Connect to the camera's Wi-Fi.** The HDC broadcasts its own network named `dashcam` (password `hivemapper`) — join it exactly like you'd join any Wi-Fi network, from your phone/laptop's normal Wi-Fi settings.

**3. In the terminal**, type `cd ` (with a space after it), drag the folder where you downloaded Roamcam into the window, and press Enter — that moves you into it. Then paste this and press Enter:

```sh
scp dashboard_server.py install.sh root@192.168.0.10:/tmp/
```

*(It may ask "are you sure you want to continue connecting?" — type `yes` and press Enter. It won't ask for a password.)*

**4. Paste this and press Enter** — this installs and starts everything on the camera:

```sh
ssh root@192.168.0.10 "cd /tmp && sh install.sh"
```

**5. Open a normal browser window** (Chrome, Safari, Edge, whatever) and go to:

```
http://192.168.0.10:8080
```

That's your dashboard. Nothing else to install, no account to make.

Stuck at any step, or want to know what each command actually does? **[docs/INSTALL.md](docs/INSTALL.md)** has the same steps with pictures and a troubleshooting section.

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
| **Playback** | Your clips as a clean list — play, download, lock/unlock, filter, multi-select and bulk-delete |
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
- [x] Incident lock — protect clips automatically on impact (G-sensor) or by hand
- [x] English/Dutch interface and metric/imperial units
- [x] Live preview while recording — manual toggle, hardware-decoded, off by default
- [ ] Day/night exposure profiles in the UI
- [ ] Optional push of clips/telemetry to Home Assistant when it's on a network with internet
- [ ] Motion / parking mode using the G-sensor
- [ ] A companion app for live video and notifications — worth designing properly before building; see [docs/HOWTO.md](docs/HOWTO.md#live-preview) for the groundwork already laid

Ideas and pull requests welcome.

---

## Support this project ❤️

I build this in my spare time and give it away for free. If Roamcam saved you buying a dashcam, or you just think it's neat, sponsoring the project genuinely helps and keeps the work going:

**[❤️ Sponsor on GitHub](https://github.com/sponsors/Menno000)**

There's also a **Sponsor** button at the top of this repository.

---

## Credits & license

Built by a Dutch tinkerer who didn't want a perfectly good camera to go to waste. Not affiliated with, endorsed by, or connected to Hivemapper.

Released under the [MIT License](LICENSE) — do what you like, no warranty.
