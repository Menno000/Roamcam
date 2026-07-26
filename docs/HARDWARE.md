# The hardware

Roamcam runs on the **Hivemapper HDC** — the mapping dashcam built around a Raspberry Pi Compute Module 4. Underneath the branding it's a very capable little computer.

## What's inside

| Part | Detail |
|------|--------|
| Compute | Raspberry Pi CM4 — quad-core Cortex-A72, ~1.8 GB usable RAM |
| Storage | ~28 GB eMMC (`/mnt/data` is the writable, persistent partition) |
| Camera | Sony **IMX477**, 12.3 MP, wide-angle. 4K stills; 1080p30 H.264 on the Pi's hardware encoder |
| GNSS | u-blox module — position, speed, heading, satellites, plus RF/anti-jamming telemetry |
| IMU | 6-axis accelerometer + gyroscope, with its own temperature sensor |
| LEDs | 3 × RGB status LEDs, driven via `/tmp/led.json` |
| Radios | 2.4 GHz Wi-Fi (runs as an access point by default), LoRa |
| Audio | **None** — there is no microphone. Video only |
| Clock | **No battery-backed RTC** — the time comes from GPS |

## The filesystem, and why it matters

The root filesystem is a read-only **squashfs** image with a **tmpfs (RAM) overlay** on top. In plain terms: anything you write outside `/mnt/data` vanishes on reboot, and the firmware image itself can't be modified without rebuilding and reflashing it.

Roamcam is designed around this:

- All of Roamcam lives on `/mnt/data`.
- Autostart hooks into the firmware's own boot-time job runner (`/mnt/data/cron_config`) rather than adding a systemd unit, because a systemd unit would be wiped by the overlay on reboot.
- It never touches the firmware image, so there's no brick risk from Roamcam itself.

## The camera is single-owner

Only one process can hold the camera at a time. Roamcam's recorder takes it over cleanly by pausing the Hivemapper capture, and hands it back when you stop. The GPS and autostart plumbing keep running throughout — they don't touch the camera.

## Which Hivemapper do I have?

Hivemapper has sold three generations of hardware under different names. **Roamcam only works on the first two** — check which one you have before you start.

| Model | Official name | Generation | What's inside | Roamcam? |
|-------|---------------|------------|----------------|----------|
| **HDC** | Hivemapper Dashcam | 1st gen | Raspberry Pi CM4 — this is the hardware Roamcam is built and tested on | ✅ Supported |
| **HDC-S** | Hivemapper Dashcam S | 2nd gen | Also Pi CM4-based, adds 4K video for personal use | ⚠️ Same core hardware as the HDC, so it *should* work, but it hasn't been tested — see below |
| **Bee** | Bee (Wi-Fi only, or LTE + Wi-Fi) | 3rd gen, current model | Completely different hardware — stereo cameras, weatherproof, forward-mount only, no Raspberry Pi | ❌ Not supported |

Both the HDC and HDC-S were discontinued by Hivemapper in 2024; the Bee is their current product.

### How to tell which one you have

The reliable way is the **model label** on the device itself (usually on the underside or back) — it will say "Dashcam", "Dashcam S", or "Bee" outright. A few other tells if the label is worn off:

- **A Bee** is weatherproof and meant to be mounted outside the car, facing forward only. If yours is a small plastic unit that sits inside on the windshield and can face sideways too, it's an HDC or HDC-S, not a Bee.
- **HDC vs HDC-S** look near-identical from the outside; the S added 4K capture for personal video. If you're not sure which of these two you have, that's fine — both run on the same Pi CM4 hardware Roamcam expects.
- The most certain check of all: try `ssh root@192.168.0.10` (see [INSTALL.md](INSTALL.md)). If you get a shell, `cat /proc/device-tree/model` will print the exact board — Roamcam expects a Raspberry Pi Compute Module 4 to show up there.

For official product photos, see [Hivemapper's own device pages](https://docs.hivemapper.com/contribute/driving/device-models/) — we don't reproduce their marketing images here, but a device search there will show you exactly what each model looks like.

### Got an HDC-S or a Bee?

The **HDC-S** almost certainly works since it shares the HDC's Pi CM4 hardware — if you try Roamcam on one, please [open an issue](https://github.com/Menno000/Roamcam/issues) to confirm it either way, so this table can be updated.

The **Bee** would need a real hardware port — different SoC, different camera interface, different firmware entirely. If you have one and want to help figure out what's possible, open an issue.

## Access

These cameras ship with **root SSH, no password** on the development firmware, reachable over the built-in Wi-Fi access point (`dashcam`) at `192.168.0.10`, or over your LAN. That open access is exactly why repurposing them is straightforward — and also a good reason to keep the camera on an isolated network if you're not using Hivemapper's cloud.
