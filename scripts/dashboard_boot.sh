#!/bin/sh
# Reference copy — install.sh writes this to /mnt/data/dashboard_boot.sh.
# Started every 30s by the HDC's cron; launches the dashboard if it isn't running.
if ! ps aux 2>/dev/null | grep -v grep | grep -q dashboard_server.py; then
  setsid python3 /mnt/data/dashboard_server.py >/mnt/data/dashboard.log 2>&1 </dev/null &
fi
