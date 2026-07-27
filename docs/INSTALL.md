# Installing Roamcam

This takes about five minutes. You need the camera powered on and reachable over the network.

> **New to typing commands?** Every step below happens in a **terminal** — a plain text window built into every computer. Open one first: on Windows press the ⊞ key and type `PowerShell`, on Mac press `Cmd + Space` and type `Terminal`. You'll copy each grey code block and paste it in (Windows: right-click to paste; Mac: `Cmd + V`), then press Enter and wait for it to finish before pasting the next one.

## 1. Connect to the camera

The HDC has **root SSH open with no password** on its development firmware — that's what makes Roamcam possible without reflashing.

It's reachable in one of two ways:

- **Its own Wi-Fi.** The HDC broadcasts an access point named `dashcam` (default password `hivemapper`). Join it and the camera is at `192.168.0.10`.
- **Your own network.** If you've put it on your LAN, use the IP your router gave it.

Test the connection:

```sh
ssh root@192.168.0.10
```

If you get a shell, you're good. Type `exit` to come back.

## 2. Copy the files over

From the folder where you cloned/downloaded Roamcam:

```sh
scp dashboard_server.py install.sh uninstall.sh root@192.168.0.10:/tmp/
```

## 3. Run the installer

```sh
ssh root@192.168.0.10 'cd /tmp && sh install.sh'
```

You'll see something like:

```
== Roamcam installer ==
  installed dashboard_server.py
  installed dashboard_boot.sh
  registered autostart in cron_config
Done.
  Dashboard:  http://192.168.0.10:8080
  It comes back automatically after every reboot.
```

## 4. Open the dashboard

Point a browser (phone or laptop, same network) at:

```
http://192.168.0.10:8080
```

That's it. The **Live** tab shows the camera and sensors. Go to **Settings** to start recording.

*If Roamcam just saved you from buying a dashcam, there's a Sponsor button at the top of the repo — entirely optional, always appreciated.*

## What the installer actually did

- Copied `dashboard_server.py` to `/mnt/data` — the only partition that survives a reboot on this device.
- Wrote a small `dashboard_boot.sh` launcher next to it.
- Added one line to the camera's own `/mnt/data/cron_config`, which the firmware reads at every boot. That line restarts the dashboard every 30 seconds, so it's both **boot-persistent** and **self-healing** — no systemd unit that the read-only-root overlay would wipe.
- Started it, and reloaded the cron so autostart is live immediately.

Nothing was flashed. Nothing was permanently changed in the firmware. See [HOWTO.md](HOWTO.md) for how it all fits together.

## Updating

Copy a newer `dashboard_server.py` over the old one and restart it:

```sh
scp dashboard_server.py root@192.168.0.10:/mnt/data/dashboard_server.py
ssh root@192.168.0.10 "kill \$(ps aux | grep dashboard_server.py | grep -v grep | awk '{print \$1}'); sleep 1; sh /mnt/data/dashboard_boot.sh"
```

## Removing

```sh
scp uninstall.sh root@192.168.0.10:/tmp/
ssh root@192.168.0.10 'cd /tmp && sh uninstall.sh'
```

This stops Roamcam, removes the autostart hook, and restores the stock Hivemapper camera. Your clips are left where they are.

## Troubleshooting

**The dashboard doesn't load.**
Check it's running:
```sh
ssh root@192.168.0.10 'ps aux | grep dashboard_server.py | grep -v grep'
```
If nothing shows, look at the log:
```sh
ssh root@192.168.0.10 'tail -30 /mnt/data/dashboard.log'
```

**After a reboot the camera is reachable but the dashboard is late.**
That's normal — the camera's firmware boots first, then the cron starts the dashboard within ~30 seconds.

**I connect over the camera's own Wi-Fi and it drops after a reboot.**
Your computer has to re-join the `dashcam` access point after the camera restarts its Wi-Fi. Reconnect and try again.
