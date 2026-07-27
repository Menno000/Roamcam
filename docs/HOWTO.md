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

### Toward a self-update mechanism (not built yet — the groundwork)

Right now, updating means copying a new `dashboard_server.py` over SSH by hand. A "check for updates" button in the dashboard is a natural next step, but it runs into a real constraint worth thinking through first:

- **The device needs internet to check anything**, and by design it usually doesn't have any — it either broadcasts its own isolated `dashcam` Wi-Fi (no uplink) or sits on your LAN, which may or may not have internet. An update check can only ever happen when the camera is, at that moment, on a network with a route out.
- **Silent background phoning-home is off the table** — it would work against the whole "nothing leaves this device" premise the project is built on. Any update check has to be something the owner triggers or explicitly configures, never automatic in the background over the isolated AP.
- The most promising shape: an **opt-in "home network"** the owner configures once (SSID + password, entered in the dashboard, same as any other device), combined with either — if the HDC's Wi-Fi chip supports it — running the camera's own AP *and* a home-network client connection at the same time (concurrent AP+STA), so it checks automatically only while parked at home; or, if that's not supported, an explicit "Update now" button that only lights up once the device happens to be on a network with a route out.
- **Verification matters more than the mechanism.** Whatever fetches a new version needs a published checksum (and ideally a signature) to check before installing it — an update channel is, by definition, a way to push code onto every running Roamcam, so it has to refuse anything that doesn't verify. A failed-to-start new version should automatically roll back to the last-known-good file.

None of this is hard, but it deserves the AP+STA question answered on real hardware before deciding between "automatic at home" and "manual button".

### Toward LoRa mesh networking (not built yet — the groundwork)

The HDC has a LoRa radio on board — stock firmware uses it to relay small data packets to Hivemapper's Helium network when there's no Wi-Fi. Roamcam doesn't touch it today, but repurposing it fits the project's own philosophy well: point-to-point or mesh communication that never touches anyone else's server.

- **[Meshtastic](https://meshtastic.org/)** has a Linux-native daemon, `meshtasticd`, built to run directly on a Raspberry Pi and drive an SPI-connected LoRa radio chip (SX126x/SX127x) — no separate microcontroller needed. If the HDC's LoRa module is wired the same way (a bare radio chip on SPI, not a pre-programmed sub-module with its own firmware), `meshtasticd` could plausibly take it over the same way Roamcam already takes over the camera from the stock capture process.
- **The open question is the wiring**, not the software: is the chip reachable directly over SPI, or does it sit behind its own co-processor exposing only a higher-level interface? The stock firmware runs a `lorawan-logger` service, which is a good sign — talking to Helium usually means direct register-level access to a Semtech-family chip, which is exactly what `meshtasticd` needs too.
- **Why this beats a custom radio protocol**: Meshtastic (and its sibling, [MeshCore](https://meshcore.co.uk/), same radios/different protocol) already have free phone apps, existing mesh networks in many areas, GPS position sharing and text messaging built in. Getting Roamcam onto one of them means an incident alert or a location beacon rides on infrastructure that already exists, instead of needing a bespoke receiver.
- **What it could unlock**: an incident-lock trigger sent as a mesh message the moment it happens, even with zero Wi-Fi in range; a periodic position beacon useful as a private, subscription-free "where's the car" tracker; not a substitute for the dashboard, just an out-of-band channel for the handful of things worth knowing about immediately.

#### Which network — and why this should be the owner's choice, not ours

Meshtastic/MeshCore aren't the only option, and "LoRa" isn't one network — it's a radio that can talk to several completely different backends, each with a different cost and trust trade-off:

| Option | Coverage | Cost | Who's in the loop | Note |
|---|---|---|---|---|
| **Meshtastic / MeshCore** (own mesh) | As far as your own + nearby community nodes reach | One-time, ~€20-50 per extra node | Nobody — no account, anywhere | No coverage guarantee, density-dependent |
| **The Things Network (TTN)** | Global, dense in EU/NL cities, thin elsewhere | Free, fair-use capped (30 s airtime + 10 downlinks/day) | Non-profit community network, free account required | Best-effort, but nobody's monetizing the data |
| **Helium IoT Network** | Largest LoRaWAN network worldwide (250k+ gateways), coverage varies a lot by region | Free where coverage exists, needs a Helium Console account | Decentralized (independent gateway operators) | The HDC already talks to this on stock firmware — same SPI-access question applies |
| **Self-hosted ChirpStack + own gateway** | As far as your own antenna reaches (a few km, more with line of sight) | One-time gateway (~€100-300), near-free to run at hobby scale | Nobody — fully self-hosted | Purest fit for Roamcam's philosophy; coverage is entirely your own responsibility |
| **KPN Things (Netherlands)** | All of NL | Free for 1 year, then from €1.33/month/license | KPN, business-oriented product | See the trade-offs above — worth reading the actual terms first |
| **Other national telcos** (Proximus/BE, Swisscom/CH, Orange/FR, Digita/FI, Netzikon/DE, …) | Own country, roaming-interconnected via Actility's hub (25+ countries) | Similar to KPN — commercial, paid after a trial | A telco per country | Only relevant if this goes properly international |
| **Satellite LoRaWAN** (Lacuna Space, Plan-S, EchoStar) | Everywhere, including ocean and desert — no gateway needed at all | Paid, commercial (no free tier found) | A commercial satellite operator | The "truly anywhere" option, but priced and scoped for industrial IoT, not a dashcam |

No single one of these fits everybody, and picking one for all Roamcam users would mean baking in a specific trust relationship (an account somewhere, a coverage assumption) that not everyone wants. The right shape is almost certainly a **backend the owner selects**, not something Roamcam decides for them — someone who wants zero third parties picks Meshtastic or their own ChirpStack gateway; someone who wants guaranteed national coverage and is fine with an account picks TTN or a local telco. The uplink payload (a tiny incident/position packet) is small and generic enough that swapping the backend shouldn't mean rewriting the feature — just where the packet goes.

Next real step is still hands-on and backend-independent: identify the exact LoRa chip and confirm whether the SPI bus is exposed, before any code gets written.

## The clock

There's no battery-backed clock in this hardware, and no internet in standalone use — so Roamcam sets the system clock from **GPS**. As soon as there's a fix, timestamps (and clip filenames) are correct, to the second. Nothing to configure.

## Coexisting with Hivemapper

Roamcam never deletes or reflashes the Hivemapper software. "Standalone" mode simply pauses the camera capture and the ML privacy blur while it records, and keeps the rest running (that's what provides GPS and the autostart). The **↩ Restore Hivemapper** link in Settings brings the stock camera fully back whenever you want it.
