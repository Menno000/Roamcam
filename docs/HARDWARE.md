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

## What about the Bee?

Hivemapper's newer **Bee** camera runs different firmware and a different software stack. Roamcam is written and tested for the **HDC** and is not expected to work on the Bee as-is. If you have a Bee and want to help, open an issue.

## Access

These cameras ship with **root SSH, no password** on the development firmware, reachable over the built-in Wi-Fi access point (`dashcam`) at `192.168.0.10`, or over your LAN. That open access is exactly why repurposing them is straightforward — and also a good reason to keep the camera on an isolated network if you're not using Hivemapper's cloud.
