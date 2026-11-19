#!/bin/sh
# Roamcam installer — run this ON a Hivemapper HDC, as root.
#
#   scp dashboard_server.py install.sh root@192.168.0.10:/tmp/
#   ssh root@192.168.0.10 'cd /tmp && sh install.sh'
#
set -e
SRC="$(dirname "$0")"
DATA=/mnt/data

echo "== Roamcam installer =="

if [ ! -d "$DATA" ]; then
  echo "ERROR: $DATA not found. This does not look like a Hivemapper HDC." >&2
  exit 1
fi
if [ ! -f "$SRC/dashboard_server.py" ]; then
  echo "ERROR: dashboard_server.py not found next to install.sh." >&2
  exit 1
fi
if [ ! -f "$SRC/roamcam_core.py" ]; then
  echo "ERROR: roamcam_core.py not found next to install.sh. The dashboard needs it." >&2
  exit 1
fi

# 1. the app and the shared core -> persistent storage
#    The core must sit in /mnt/data: that is where the dashboard looks for it, and it is the
#    only place on this device that survives a reboot.
cp "$SRC/dashboard_server.py" "$DATA/dashboard_server.py"
cp "$SRC/roamcam_core.py" "$DATA/roamcam_core.py"
echo "  installed dashboard_server.py + roamcam_core.py"

# 2. boot/watchdog script: starts the dashboard if it isn't already running
cat > "$DATA/dashboard_boot.sh" <<'EOF'
#!/bin/sh
if ! ps aux 2>/dev/null | grep -v grep | grep -q dashboard_server.py; then
  setsid python3 /mnt/data/dashboard_server.py >/mnt/data/dashboard.log 2>&1 </dev/null &
fi
EOF
chmod +x "$DATA/dashboard_boot.sh"
echo "  installed dashboard_boot.sh"

# 3. autostart. The HDC firmware reads /mnt/data/cron_config at every boot and runs
#    the listed jobs. We register one that (re)starts the dashboard every 30s: this
#    makes it boot-persistent AND self-healing, without a systemd unit (which would be
#    wiped by the read-only-root overlay on reboot).
#
#    NOTE: no "device" key on purpose. The firmware checks it as
#      if (config.device && config.device !== CAMERA_TYPE) return;
#    so a value that doesn't match the camera makes the job silently never run. The stock
#    cron_config on our HDC contains only "device":"hdc-s" entries, which is exactly why
#    those never fire there. Leaving the key out skips the check entirely, so this works on
#    an HDC and an HDC-S alike. (We shipped "device":"hdc" until 2026-11; on any camera whose
#    type string isn't exactly "hdc", the dashboard never started by itself.)
python3 - <<'PY'
import json
p = "/mnt/data/cron_config"
try:
    d = json.load(open(p))
    if not isinstance(d, list):
        d = []
except Exception:
    d = []
d = [j for j in d if j.get("id") != "roamcam"]
d.append({"id": "roamcam", "cmd": "sh /mnt/data/dashboard_boot.sh",
          "frequency": {"interval": 30000}})
json.dump(d, open(p, "w"))
print("  registered autostart in cron_config")
PY

# 4. start it now
sh "$DATA/dashboard_boot.sh"
sleep 2

# 5. reload cron_config so autostart is active without waiting for a reboot
systemctl restart camera-node 2>/dev/null || true

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "Done."
echo "  Dashboard:  http://${IP:-<device-ip>}:8080"
echo "  It comes back automatically after every reboot."
echo ""
echo "To remove later: sh uninstall.sh"
