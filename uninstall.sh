#!/bin/sh
# Roamcam uninstaller — run on the Hivemapper HDC as root.
# Stops Roamcam, removes the autostart hook, and restores the stock camera.
set -e
DATA=/mnt/data
echo "== Removing Roamcam =="

# stop the dashboard and any active recording
kill $(ps aux 2>/dev/null | grep -E 'dashboard_server.py|libcamera-vid|ffmpeg' | grep -v grep | awk '{print $1}') 2>/dev/null || true

# remove the autostart entry
python3 - <<'PY'
import json
p = "/mnt/data/cron_config"
try:
    d = json.load(open(p))
    d = [j for j in d if j.get("id") != "roamcam"]
    json.dump(d, open(p, "w"))
    print("  removed autostart entry")
except Exception:
    pass
PY

# restore the stock Hivemapper camera (in case a recording session masked services)
systemctl unmask camera-bridge object-detection 2>/dev/null || true
systemctl start api-health-manager camera-bridge object-detection 2>/dev/null || true

rm -f "$DATA/dashboard_boot.sh"
echo "  restored stock camera services"
echo ""
echo "Done. dashboard_server.py and your clips in $DATA are left untouched."
echo "Delete them by hand if you want them gone."
