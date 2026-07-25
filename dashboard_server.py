#!/usr/bin/env python3
# Hivemapper HDC - lokaal "alles-in-1" dashboard
# Draait op het toestel zelf, serveert de pagina + proxy naar de lokale API (:5000) + systeeminfo.
import json, os, socket, subprocess, urllib.request, glob, math, sqlite3, time, threading, calendar
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "http://127.0.0.1:5000"
PORT = 8080

SERVICES = [
    "api-health-manager", "camera-node", "camera-bridge", "object-detection",
    "data-logger", "folder_purger", "lorawan-logger", "led-controller",
    "sshd", "hostapd", "dnsmasq", "rauc",
]


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=6).stdout.strip()
    except Exception:
        return ""


def cpu_usage():
    def snap():
        out = {}
        for line in read("/proc/stat").splitlines():
            if line.startswith("cpu"):
                p = line.split()
                vals = list(map(int, p[1:]))
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                out[p[0]] = (idle, sum(vals))
        return out
    a = snap(); time.sleep(0.15); b = snap()
    res = {}
    for k in a:
        if k in b:
            di = b[k][0] - a[k][0]; dt = b[k][1] - a[k][1]
            res[k] = round(100 * (1 - di / dt), 1) if dt > 0 else 0.0
    return res


def sysinfo():
    d = {}
    d["hostname"] = socket.gethostname()
    d["cpu"] = cpu_usage()
    d["governor"] = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    d["model"] = read("/proc/device-tree/model").replace("\x00", "").strip()
    d["kernel"] = " ".join(os.uname())
    try:
        up = float(read("/proc/uptime", "0").split()[0]); d["uptime_s"] = up
    except Exception:
        d["uptime_s"] = 0
    d["loadavg"] = read("/proc/loadavg")
    d["cpu_count"] = os.cpu_count()
    try:
        d["temp_c"] = round(int(read("/sys/class/thermal/thermal_zone0/temp", "0")) / 1000.0, 1)
    except Exception:
        d["temp_c"] = None
    try:
        d["cpu_mhz"] = round(int(read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "0")) / 1000.0)
    except Exception:
        d["cpu_mhz"] = None
    # geheugen
    mem = {}
    for line in read("/proc/meminfo").splitlines():
        parts = line.split(":")
        if len(parts) == 2:
            mem[parts[0].strip()] = int(parts[1].strip().split()[0]) * 1024
    d["mem_total"] = mem.get("MemTotal", 0)
    d["mem_avail"] = mem.get("MemAvailable", 0)
    d["mem_used"] = d["mem_total"] - d["mem_avail"]
    d["swap_total"] = mem.get("SwapTotal", 0)
    d["swap_free"] = mem.get("SwapFree", 0)
    # schijven
    disks = {}
    for label, mnt in [("root /", "/"), ("firmware /ro", "/ro"), ("data /mnt/data", "/mnt/data")]:
        try:
            s = os.statvfs(mnt)
            total = s.f_blocks * s.f_frsize
            free = s.f_bavail * s.f_frsize
            disks[label] = {"total": total, "used": total - free, "free": free}
        except Exception:
            pass
    d["disks"] = disks
    # processen
    d["procs"] = len([p for p in os.listdir("/proc") if p.isdigit()])
    # services
    out = sh("systemctl is-active " + " ".join(SERVICES))
    states = out.splitlines()
    d["services"] = {SERVICES[i]: (states[i] if i < len(states) else "?") for i in range(len(SERVICES))}
    # netwerk
    d["ips"] = sh("hostname -I").split()
    nets = {}
    try:
        for ifc in sorted(os.listdir("/sys/class/net")):
            if ifc == "lo":
                continue
            nets[ifc] = {
                "state": read("/sys/class/net/%s/operstate" % ifc),
                "mac": read("/sys/class/net/%s/address" % ifc),
            }
    except Exception:
        pass
    d["nets"] = nets
    d["wifi_mode"] = read("/mnt/data/wifi.cfg")
    # firmware slots (rauc.status)
    d["rauc"] = read("/mnt/data/rauc.status")
    # db-bestanden in /mnt/data
    dbs = []
    try:
        for fn in sorted(os.listdir("/mnt/data")):
            if fn.endswith(".db"):
                try:
                    dbs.append({"name": fn, "size": os.path.getsize("/mnt/data/" + fn)})
                except Exception:
                    pass
    except Exception:
        pass
    d["dbs"] = dbs
    return d


def proxy(path):
    """Haal path op van de lokale API via wget (betrouwbaar met de IPv6-binding
    van de node-API, waar Python's urllib op afketst). Retourneert (status, ctype, bytes)."""
    ctype = "image/jpeg" if "/pic/" in path else "application/json"
    try:
        p = subprocess.run(["wget", "-qO-", "--timeout=6", API + path],
                           capture_output=True, timeout=9)
        if p.returncode == 0 and p.stdout:
            return 200, ctype, p.stdout
        return 502, "application/json", json.dumps({"error": "api", "rc": p.returncode}).encode()
    except Exception as e:
        return 502, "application/json", json.dumps({"error": str(e)}).encode()


_imu_db = [None]


def imu_db():
    if _imu_db[0] is None:
        dbs = sorted(glob.glob("/mnt/data/data-logger.v*.db"))
        _imu_db[0] = dbs[-1] if dbs else ""
    return _imu_db[0]


def imu_latest(n=60):
    db = imu_db()
    if not db:
        return {"ok": False}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
        try:
            rows = c.execute(
                "select id,time,acc_x,acc_y,acc_z,gyro_x,gyro_y,gyro_z,temperature "
                "from imu order by id desc limit ?", (n,)).fetchall()
        finally:
            c.close()
    except Exception as e:
        return {"ok": False, "err": str(e)}
    if not rows:
        return {"ok": False}
    rows = rows[::-1]  # oud -> nieuw
    hist = []
    peak_g = 0.0
    peak_gyro = 0.0
    for r in rows:
        ax, ay, az = r[2], r[3], r[4]
        gx, gy, gz = r[5], r[6], r[7]
        g = math.sqrt(ax * ax + ay * ay + az * az)
        gyro_mag = math.sqrt(gx * gx + gy * gy + gz * gz)
        peak_g = max(peak_g, g)
        peak_gyro = max(peak_gyro, gyro_mag)
        hist.append({"ax": ax, "ay": ay, "az": az, "gx": gx, "gy": gy, "gz": gz, "g": g})
    last = rows[-1]
    ax, ay, az = last[2], last[3], last[4]
    pitch = math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az)))
    roll = math.degrees(math.atan2(ay, az))
    # samplerate uit id/tijd-delta
    rate = None
    try:
        import datetime
        t0 = rows[0][1]; t1 = rows[-1][1]
        did = rows[-1][0] - rows[0][0]
        f0 = datetime.datetime.fromisoformat(t0.split(".")[0])
        f1 = datetime.datetime.fromisoformat(t1.split(".")[0])
        dt = (f1 - f0).total_seconds()
        if dt > 0:
            rate = round(did / dt)
    except Exception:
        pass
    return {
        "ok": True,
        "id": last[0],
        "accel": {"x": ax, "y": ay, "z": az},
        "gyro": {"x": last[5], "y": last[6], "z": last[7]},
        "temp": last[8],
        "g": math.sqrt(ax * ax + ay * ay + az * az),
        "pitch": pitch, "roll": roll,
        "peak_g": peak_g, "peak_gyro": peak_gyro,
        "rate": rate, "hist": hist,
    }


# ---- Eigen dashcam-recorder ----
CLIPS_DIR = "/mnt/data/clips"
REC_DEFAULT = {"on": False, "standalone": False, "seg": 60, "cap_gb": 15, "w": 1920, "h": 1080, "fps": 30, "gain": 0, "shutter": 0}
rec_cfg = dict(REC_DEFAULT)
rec_state = {"proc": None, "err": "", "started": 0.0}


def load_rec_settings():
    try:
        d = json.load(open(LED_SETTINGS_PATH))
        if isinstance(d.get("rec"), dict):
            for k in REC_DEFAULT:
                if k in d["rec"]:
                    rec_cfg[k] = d["rec"][k]
    except Exception:
        pass


def save_rec_settings():
    try:
        d = {}
        try:
            d = json.load(open(LED_SETTINGS_PATH))
        except Exception:
            pass
        d["rec"] = {k: rec_cfg[k] for k in REC_DEFAULT}
        with open(LED_SETTINGS_PATH, "w") as f:
            json.dump(d, f)
    except Exception:
        pass


def recorder_running():
    p = rec_state.get("proc")
    return p is not None and p.poll() is None


def suppress_hivemapper():
    # Hivemapper-camera uitzetten: watchdog uit, camera-bridge+object-detection maskeren+stoppen
    # (camera-node blijft draaien -> GPS-API + cron-autostart intact)
    sh("systemctl stop api-health-manager 2>/dev/null")
    sh("systemctl mask camera-bridge object-detection 2>/dev/null")
    sh("systemctl stop camera-bridge object-detection 2>/dev/null")


def stop_vid():
    p = rec_state.get("proc")
    if p:
        try:
            p.terminate()
        except Exception:
            pass
    sh("kill $(ps aux 2>/dev/null | grep -E 'libcamera-vid|ffmpeg' | grep -v grep | awk '{print $1}') 2>/dev/null")
    rec_state["proc"] = None


def start_recorder():
    if recorder_running():
        return
    os.makedirs(CLIPS_DIR, exist_ok=True)
    rec_cfg["standalone"] = True
    suppress_hivemapper()
    time.sleep(4)
    w, h, fps, seg = rec_cfg["w"], rec_cfg["h"], rec_cfg["fps"], rec_cfg["seg"]
    opts = ""
    if rec_cfg.get("gain"):
        opts += " --gain %d" % rec_cfg["gain"]
    if rec_cfg.get("shutter"):
        opts += " --shutter %d" % rec_cfg["shutter"]
    open("/mnt/data/rec_vid.log", "w").close()
    cmd = ("libcamera-vid -t 0 --inline --nopreview --width %d --height %d --framerate %d%s -o - 2>/mnt/data/rec_vid.log "
           "| ffmpeg -probesize 2M -analyzeduration 2M -f h264 -i - -c copy -f segment -segment_time %d "
           "-reset_timestamps 1 -strftime 1 %s/%%Y%%m%%d_%%H%%M%%S.mp4 2>/mnt/data/rec_ff.log"
           % (w, h, fps, opts, seg, CLIPS_DIR))
    rec_state["proc"] = subprocess.Popen(["sh", "-c", cmd])
    rec_state["started"] = time.time()
    rec_cfg["on"] = True
    rec_state["err"] = ""
    save_rec_settings()
    time.sleep(3)
    alive = sh("ps aux 2>/dev/null | grep libcamera-vid | grep -v grep | wc -l").strip()
    vidlog = ""
    try:
        vidlog = open("/mnt/data/rec_vid.log").read()
    except Exception:
        pass
    if alive == "0" or "failed to acquire" in vidlog:
        rec_state["err"] = "camera niet vrij te krijgen"
        stop_vid()
        rec_cfg["on"] = False
        save_rec_settings()


def stop_recording():
    # "Camera uit": opname stoppen, camera idle, MAAR standalone blijven (Hivemapper NIET terug)
    stop_vid()
    rec_cfg["on"] = False
    save_rec_settings()


def restore_hivemapper():
    stop_vid()
    rec_cfg["on"] = False
    rec_cfg["standalone"] = False
    save_rec_settings()
    sh("systemctl unmask camera-bridge object-detection 2>/dev/null")
    sh("systemctl start api-health-manager camera-bridge object-detection 2>/dev/null")


# compat-alias
def stop_recorder(restore=True):
    if restore:
        restore_hivemapper()
    else:
        stop_recording()


def retention_loop():
    while True:
        try:
            if rec_cfg.get("on"):
                cap = rec_cfg.get("cap_gb", 15) * 1000000000
                files = sorted(glob.glob(CLIPS_DIR + "/*.mp4"), key=lambda f: os.path.getmtime(f))
                total = sum(os.path.getsize(f) for f in files if os.path.exists(f))
                while total > cap and len(files) > 1:
                    victim = files.pop(0)
                    try:
                        total -= os.path.getsize(victim)
                        os.remove(victim)
                        srt = victim[:-4] + ".srt"
                        if os.path.exists(srt):
                            os.remove(srt)
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(30)


def gnss_latest():
    db = imu_db()
    if not db:
        return {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
        try:
            r = c.execute("select time,fix,latitude,longitude,altitude,speed,heading,satellites_used "
                          "from gnss order by id desc limit 1").fetchone()
        finally:
            c.close()
        if r:
            return {"time": r[0], "fix": r[1], "lat": r[2], "lon": r[3], "alt": r[4],
                    "speed": r[5], "heading": r[6], "sats": r[7]}
    except Exception:
        pass
    return {}


def gps_time_sync():
    # zet de systeemklok uit de GPS-tijd (u-blox UTC) zodra er een geldige fix is; houdt 'm gelijk
    synced = False
    while True:
        try:
            g = gnss_latest()
            t = g.get("time")
            if t and len(t) >= 19:
                ts = t[:19]  # "YYYY-MM-DD HH:MM:SS"
                if int(ts[:4]) >= 2024:
                    gps_epoch = calendar.timegm(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
                    if abs(gps_epoch - time.time()) > 3:
                        sh('date -u -s "%s" 2>/dev/null' % ts)
                        synced = True
        except Exception:
            pass
        time.sleep(15 if not synced else 120)


def track_loop():
    # continue GPS+IMU-track (NDJSON) zolang er opgenomen wordt
    while True:
        try:
            if rec_cfg.get("on") and recorder_running():
                g = gnss_latest()
                im = imu_latest(3)
                line = {"t": time.time(), "gps": g,
                        "g": round(im.get("g", 0), 3) if im.get("ok") else None,
                        "peak_g": round(im.get("peak_g", 0), 3) if im.get("ok") else None}
                with open(CLIPS_DIR + "/track.ndjson", "a") as f:
                    f.write(json.dumps(line) + "\n")
        except Exception:
            pass
        time.sleep(1)


# ---- LED-aansturing (continue driver: wint van de dashcam die zichzelf herstart) ----
LED_SETTINGS_PATH = "/mnt/data/dashboard_settings.json"
# kleuren op de 0-25 hardware-schaal (kanalen kalibreren we nog fysiek)
LED_COLORS = {
    "uit":   (0, 0, 0, False),
    "rood":  (25, 0, 0, True),
    "groen": (0, 25, 0, True),
    "blauw": (0, 0, 25, True),
    "geel":  (18, 12, 2, True),
    "wit":   (20, 20, 20, True),
    "paars": (15, 5, 25, True),
}
led_state = {"mode": "dashcam", "fns": ["uit", "uit", "uit"], "raw": None}
_led_cache = {"gps": {}, "imu": {}, "t": 0}


def load_led_settings():
    try:
        d = json.load(open(LED_SETTINGS_PATH))
        if isinstance(d.get("led"), dict):
            for k in ("mode", "fns"):
                if k in d["led"]:
                    led_state[k] = d["led"][k]
    except Exception:
        pass


def save_led_settings():
    try:
        d = {}
        try:
            d = json.load(open(LED_SETTINGS_PATH))
        except Exception:
            pass
        d["led"] = {"mode": led_state["mode"], "fns": led_state["fns"]}
        with open(LED_SETTINGS_PATH, "w") as f:
            json.dump(d, f)
    except Exception:
        pass


def _led_refresh_data():
    now = time.time()
    if now - _led_cache["t"] < 1.5:
        return
    # alleen data ophalen die een actieve LED-functie echt nodig heeft
    fns = set(led_state.get("fns", []))
    need_gps = bool(fns & {"gps", "snelheid"})
    need_imu = "beweging" in fns
    if not (need_gps or need_imu):
        return
    _led_cache["t"] = now
    if need_gps:
        try:
            st, ct, body = proxy("/api/1/gps/sample")
            if st == 200:
                _led_cache["gps"] = json.loads(body)
        except Exception:
            pass
    if need_imu:
        try:
            _led_cache["imu"] = imu_latest(20)
        except Exception:
            pass


def led_color_for(fn):
    if fn in LED_COLORS:
        return LED_COLORS[fn]
    if fn == "opname":
        # ademend rood zolang er opgenomen wordt (langzaam aan/uit, geen geknipper)
        if recorder_running():
            phase = (math.sin(2 * math.pi * (time.time() % 4.0) / 4.0) + 1) / 2  # 0..1
            v = max(1, int(round(25 * (0.08 + 0.92 * phase))))
            return (v, 0, 0, True)
        return (0, 0, 0, False)
    if fn == "gps":
        fix = _led_cache["gps"].get("fix")
        return LED_COLORS["groen"] if fix == "3D" else (LED_COLORS["geel"] if fix == "2D" else LED_COLORS["rood"])
    if fn == "beweging":
        pk = _led_cache["imu"].get("peak_g", 1) if _led_cache["imu"].get("ok") else 1
        return LED_COLORS["rood"] if abs(pk - 1) > 0.3 else LED_COLORS["uit"]
    if fn == "temp":
        try:
            t = int(read("/sys/class/thermal/thermal_zone0/temp", "0")) / 1000.0
        except Exception:
            t = 0
        return LED_COLORS["rood"] if t > 70 else (LED_COLORS["geel"] if t > 60 else LED_COLORS["groen"])
    if fn == "snelheid":
        sp = (_led_cache["gps"].get("speed", 0) or 0) * 3.6
        if sp > 100:
            return LED_COLORS["rood"]
        if sp > 50:
            return LED_COLORS["geel"]
        if sp > 5:
            return LED_COLORS["groen"]
        return LED_COLORS["blauw"]
    return LED_COLORS["uit"]


def led_driver():
    last_manual = 0.0
    while True:
        try:
            if led_state["mode"] == "dashboard":
                now = time.time()
                if now - last_manual > 4:
                    proxy("/api/1/led/manual")
                    last_manual = now
                _led_refresh_data()
                leds = []
                for i in range(3):
                    if led_state.get("raw"):
                        r, g, b, on = led_state["raw"][i]
                    else:
                        r, g, b, on = led_color_for(led_state["fns"][i])
                    leds.append({"index": i, "red": r, "green": g, "blue": b, "on": bool(on)})
                try:
                    with open("/tmp/led.json", "w") as f:
                        f.write(json.dumps({"leds": leds}))
                except Exception:
                    pass
        except Exception:
            pass
        # soepeler verversen als er een ademende opname-LED actief is
        breathing = led_state.get("mode") == "dashboard" and "opname" in led_state.get("fns", [])
        time.sleep(0.12 if breathing else 0.25)


def latest_frame():
    st, ct, body = proxy("/api/1/recordings/last")
    if st != 200:
        return None
    try:
        name = json.loads(body).get("path")
    except Exception:
        return None
    if not name:
        return None
    st, ct, body = proxy("/api/1/recordings/pic/" + name)
    if st != 200 or not body:
        return None
    # De API levert de JPEG als JSON: {"binary":{"type":"Buffer","data":[255,216,...]}}
    try:
        obj = json.loads(body)
        data = obj.get("binary", {}).get("data")
        if data:
            return bytes(data)
    except Exception:
        pass
    # fallback: misschien tóch kale bytes
    if body[:2] == b"\xff\xd8":
        return body
    return None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, ctype, body):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/" or p == "/index.html":
            return self._send(200, "text/html; charset=utf-8", PAGE)
        if p == "/sys.json":
            return self._send(200, "application/json", json.dumps(sysinfo()))
        if p == "/imu.json":
            return self._send(200, "application/json", json.dumps(imu_latest()))
        if p == "/led/state":
            return self._send(200, "application/json", json.dumps(led_state))
        if p == "/led/mode":
            q = parse_qs(urlparse(self.path).query)
            m = q.get("set", ["dashcam"])[0]
            led_state["mode"] = "dashboard" if m == "dashboard" else "dashcam"
            led_state["raw"] = None
            save_led_settings()
            if led_state["mode"] == "dashcam":
                proxy("/api/1/led/auto")
            return self._send(200, "application/json", json.dumps(led_state))
        if p == "/led/fn":
            q = parse_qs(urlparse(self.path).query)
            try:
                i = int(q.get("i", ["0"])[0])
            except Exception:
                i = 0
            fn = q.get("fn", ["uit"])[0]
            if 0 <= i < 3:
                led_state["fns"][i] = fn
                led_state["raw"] = None
                led_state["mode"] = "dashboard"
                save_led_settings()
            return self._send(200, "application/json", json.dumps(led_state))
        if p == "/led/raw":
            # calibratie: /led/raw?c=25,0,0;0,25,0;0,0,25
            q = parse_qs(urlparse(self.path).query)
            spec = q.get("c", [""])[0]
            try:
                raw = []
                for part in spec.split(";"):
                    r, g, b = [int(x) for x in part.split(",")]
                    raw.append((r, g, b, (r + g + b) > 0))
                if len(raw) == 3:
                    led_state["raw"] = raw
                    led_state["mode"] = "dashboard"
            except Exception:
                pass
            return self._send(200, "application/json", json.dumps(led_state))
        if p == "/frame.jpg":
            f = latest_frame()
            if f:
                return self._send(200, "image/jpeg", f)
            return self._send(404, "text/plain", "no frame")
        if p == "/recframe":
            q = parse_qs(urlparse(self.path).query)
            name = q.get("name", [""])[0]
            if name and "/" not in name and ".." not in name:
                st, ct, body = proxy("/api/1/recordings/pic/" + name)
                if st == 200 and body:
                    try:
                        data = json.loads(body).get("binary", {}).get("data")
                        if data:
                            return self._send(200, "image/jpeg", bytes(data))
                    except Exception:
                        pass
            return self._send(404, "text/plain", "no frame")
        if p == "/rec_del":
            q = parse_qs(urlparse(self.path).query)
            name = q.get("name", [""])[0]
            if name and "/" not in name and ".." not in name:
                removed = False
                for d in ("/tmp/recording/pic/", "/mnt/data/pic/"):
                    try:
                        os.remove(d + name)
                        removed = True
                    except Exception:
                        pass
                return self._send(200, "application/json", json.dumps({"ok": removed}))
            return self._send(400, "application/json", json.dumps({"ok": False}))
        if p == "/rec/status":
            files = glob.glob(CLIPS_DIR + "/*.mp4")
            return self._send(200, "application/json", json.dumps({
                "on": rec_cfg.get("on", False), "running": recorder_running(),
                "standalone": rec_cfg.get("standalone", False), "err": rec_state.get("err", ""),
                "seg": rec_cfg["seg"], "cap_gb": rec_cfg["cap_gb"], "w": rec_cfg["w"],
                "h": rec_cfg["h"], "fps": rec_cfg["fps"], "gain": rec_cfg["gain"], "shutter": rec_cfg["shutter"],
                "clips": len(files), "bytes": sum(os.path.getsize(f) for f in files if os.path.exists(f)),
            }))
        if p == "/rec/start":
            try:
                start_recorder()
            except Exception as e:
                rec_state["err"] = str(e)
            return self._send(200, "application/json", json.dumps({"ok": True, "running": recorder_running()}))
        if p == "/rec/stop":
            stop_recording()
            return self._send(200, "application/json", json.dumps({"ok": True, "running": recorder_running()}))
        if p == "/rec/hivemapper":
            restore_hivemapper()
            return self._send(200, "application/json", json.dumps({"ok": True, "standalone": rec_cfg.get("standalone")}))
        if p == "/rec/set":
            q = parse_qs(urlparse(self.path).query)
            for k, cast in (("seg", int), ("cap_gb", int), ("w", int), ("h", int), ("fps", int), ("gain", int), ("shutter", int)):
                if k in q:
                    try:
                        rec_cfg[k] = cast(q[k][0])
                    except Exception:
                        pass
            save_rec_settings()
            return self._send(200, "application/json", json.dumps(rec_cfg))
        if p == "/clips.json":
            out = []
            for f in sorted(glob.glob(CLIPS_DIR + "/*.mp4"), key=lambda x: os.path.getmtime(x), reverse=True):
                try:
                    out.append({"name": os.path.basename(f), "size": os.path.getsize(f), "mtime": int(os.path.getmtime(f) * 1000)})
                except Exception:
                    pass
            return self._send(200, "application/json", json.dumps(out))
        if p == "/clip":
            q = parse_qs(urlparse(self.path).query)
            name = q.get("name", [""])[0]
            fp = os.path.join(CLIPS_DIR, name)
            if name and "/" not in name and ".." not in name and os.path.exists(fp):
                try:
                    sz = os.path.getsize(fp)
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(sz))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    with open(fp, "rb") as vf:
                        while True:
                            chunk = vf.read(262144)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except Exception:
                    pass
                return
            return self._send(404, "text/plain", "no clip")
        if p == "/clip_del":
            q = parse_qs(urlparse(self.path).query)
            name = q.get("name", [""])[0]
            if name and "/" not in name and ".." not in name:
                ok = False
                for ext in (".mp4", ".srt"):
                    fp = os.path.join(CLIPS_DIR, name[:-4] + ext if name.endswith(".mp4") else name + ext)
                    try:
                        os.remove(fp)
                        ok = True
                    except Exception:
                        pass
                return self._send(200, "application/json", json.dumps({"ok": ok}))
            return self._send(400, "application/json", json.dumps({"ok": False}))
        if p.startswith("/api/"):
            st, ct, body = proxy(self.path)
            # frames worden als application/json geserveerd maar zijn jpeg
            if "/pic/" in p:
                ct = "image/jpeg"
            return self._send(st, ct, body)
        return self._send(404, "text/plain", "not found")


PAGE = r"""<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HDC Dashboard</title>
<style>
:root{--bg:#0b0e14;--card:#141925;--card2:#1a2130;--bd:#232c3d;--tx:#e6edf3;--mut:#8b98ac;--acc:#4da3ff;--ok:#3fb950;--warn:#d29922;--err:#f85149;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;line-height:1.45}
a{color:var(--acc)}
.wrap{max-width:1400px;margin:0 auto;padding:16px}
header{display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px;padding:14px 18px;background:linear-gradient(180deg,#161c2b,#111622);border:1px solid var(--bd);border-radius:14px;margin-bottom:16px}
header h1{font-size:18px;margin:0;font-weight:700;letter-spacing:.2px}
.badge{font-size:11px;font-weight:700;padding:3px 9px;border-radius:20px;background:#0d3a63;color:#9fd0ff;text-transform:uppercase;letter-spacing:.5px}
.hstat{display:flex;flex-direction:column;line-height:1.2}
.hstat b{font-size:13px}.hstat span{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:5px;vertical-align:middle}
.dot.ok{background:var(--ok);box-shadow:0 0 6px var(--ok)}.dot.err{background:var(--err);box-shadow:0 0 6px var(--err)}
.dot.warn{background:var(--warn)}.dot.dim{background:#39506e}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;align-items:start}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;overflow:hidden}
.card.span2{grid-column:span 2}
.card h2{margin:0;padding:11px 15px;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--mut);border-bottom:1px solid var(--bd);display:flex;justify-content:space-between;align-items:center}
.card .body{padding:14px 15px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:5px 14px}
.kv .k{color:var(--mut)}.kv .v{text-align:right;font-variant-numeric:tabular-nums}
.mono{font-family:var(--mono);font-size:12.5px}
.big{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
.bar{height:8px;background:#0c1017;border-radius:6px;overflow:hidden;margin-top:5px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#2ea043,#3fb950)}
.bar.hot>i{background:linear-gradient(90deg,#d29922,#f85149)}
.frame{width:100%;display:block;background:#000;aspect-ratio:16/9;object-fit:cover}
.pill{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:700}
.pill.g{background:#0f3d22;color:#4ade80}.pill.r{background:#3d1414;color:#f87171}.pill.y{background:#3d3110;color:#fbbf24}.pill.b{background:#10233d;color:#7cc4ff}
.svcgrid{display:grid;grid-template-columns:1fr 1fr;gap:6px 14px}
.svc{display:flex;align-items:center;gap:7px;font-size:12.5px}
.dop{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;text-align:center}
.dop div{background:var(--card2);border-radius:7px;padding:6px 2px}
.dop b{display:block;font-size:14px;font-variant-numeric:tabular-nums}.dop span{font-size:9px;color:var(--mut);text-transform:uppercase}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font-family:var(--mono);font-size:11.5px;color:#b7c3d6;max-height:340px;overflow:auto}
details summary{cursor:pointer;color:var(--acc);font-size:12px;margin-top:4px}
.muted{color:var(--mut)}
.leds{display:flex;gap:18px}
.led{text-align:center}.led .ring{width:26px;height:26px;border-radius:50%;margin:0 auto 4px;border:2px solid #263248}.led span{font-size:10px;color:var(--mut);text-transform:uppercase}
.foot{color:var(--mut);font-size:11px;text-align:center;padding:14px}
.err-line{color:var(--err);font-size:11px}
.imugrid{display:grid;grid-template-columns:1fr 1fr 0.8fr;gap:18px}
.acol .atitle{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut);margin-bottom:8px}
.acol.center{text-align:center;border-left:1px solid var(--bd);padding-left:14px}
.arow{display:flex;align-items:center;gap:8px;margin-bottom:7px;font-size:12px}
.arow span{width:12px;color:var(--mut)}
.arow b{width:56px;text-align:right;font-variant-numeric:tabular-nums;font-family:var(--mono);font-size:12px}
.abar{flex:1;height:10px;background:#0c1017;border-radius:5px;position:relative;overflow:hidden}
.abar::before{content:"";position:absolute;left:50%;top:0;bottom:0;width:1px;background:#334155}
.abar>i{position:absolute;top:0;bottom:0;left:50%;width:0;background:linear-gradient(90deg,#4dabf7,#74c0fc);border-radius:5px}
.abar.g>i{background:linear-gradient(90deg,#b197fc,#d0bfff)}
#imuCard.shock{animation:flash .4s}
@keyframes flash{0%{background:#3d1414}100%{background:var(--card)}}
.ledbtn{flex:1;padding:8px;border-radius:8px;border:1px solid var(--bd);background:var(--card2);color:var(--tx);cursor:pointer;font-size:12px}
.ledbtn.on{background:#0d3a63;border-color:var(--acc);color:#9fd0ff}
.ledsel{width:100%;padding:6px 8px;border-radius:7px;background:var(--card2);color:var(--tx);border:1px solid var(--bd);font-size:12px}
.ledrow{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.ledrow>label{width:90px;color:var(--mut);font-size:12px}
.tabbar{display:flex;gap:4px;margin-bottom:14px;background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:5px;overflow-x:auto}
.tabbtn{flex:1;min-width:90px;padding:10px 12px;border:0;background:transparent;color:var(--mut);border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;white-space:nowrap}
.tabbtn.on{background:#0d3a63;color:#9fd0ff}
.card.hidden{display:none}
.recdel{flex:1;background:#3d1414;color:#f87171;border:0;border-radius:5px;padding:4px;cursor:pointer;font-size:11px}
.recdl{flex:1;text-align:center;background:#10233d;color:#7cc4ff;border-radius:5px;padding:4px;text-decoration:none;font-size:11px}
.cliprow{display:flex;align-items:center;gap:12px;padding:9px 12px;border-bottom:1px solid var(--bd)}
.cliprow:hover{background:var(--card2)}
.cliprow:last-child{border-bottom:0}
.clipmeta{flex:1;min-width:0;font-size:13px;font-variant-numeric:tabular-nums}
.clipmeta b{font-family:var(--mono)}
.clipbtns{display:flex;gap:6px;flex:none}
.clipbtn{background:#182236;color:var(--tx);border:1px solid var(--bd);border-radius:6px;padding:5px 10px;cursor:pointer;font-size:12px;text-decoration:none;white-space:nowrap}
.clipbtn.play{background:#0d3a63;color:#9fd0ff;border-color:#164a7a}
.clipbtn.del{background:#3d1414;color:#f87171;border-color:#5a1c1c}
@media(max-width:600px){.clipbtn{padding:5px 7px;font-size:11px}}
.ledswatch{width:14px;height:14px;border-radius:50%;border:1px solid #3a465c;flex:none}
@media(max-width:700px){.card.span2{grid-column:span 1}.imugrid{grid-template-columns:1fr}.acol.center{border-left:0;padding-left:0;border-top:1px solid var(--bd);padding-top:10px}}
</style></head><body><div class="wrap">
<header>
  <span class="badge" id="dashcamType">HDC</span>
  <h1 id="hostname">dashcam</h1>
  <div class="hstat"><b id="healthDot"><span class="dot dim"></span><span id="healthTxt">…</span></b><span>status</span></div>
  <div class="hstat"><b id="fw">–</b><span>firmware</span></div>
  <div class="hstat"><b id="apiv">–</b><span>api</span></div>
  <div class="hstat"><b id="uptime">–</b><span>uptime</span></div>
  <div class="hstat"><b id="temp2">–</b><span>cpu temp</span></div>
  <div class="hstat"><b id="clock">–</b><span>device tijd</span></div>
  <div class="hstat" style="margin-left:auto"><b id="conn"><span class="dot ok"></span>live</b><span>poll 1.5s</span></div>
</header>
<nav class="tabbar">
  <button class="tabbtn on" data-tab="live">Live</button>
  <button class="tabbtn" data-tab="terug">Terugkijken</button>
  <button class="tabbtn" data-tab="settings">Instellingen</button>
  <button class="tabbtn" data-tab="sys">Systeem</button>
</nav>
<div class="grid">

  <div class="card span2" id="imuCard"><h2>Live IMU / G-sensor
      <span><span id="imuRate" class="muted" style="font-weight:400">– Hz</span> &nbsp;<span id="shockPill" class="pill g">rustig</span></span></h2>
    <div class="body">
      <canvas id="scope" height="150" style="width:100%;height:150px;background:#080b11;border-radius:8px;display:block"></canvas>
      <div style="display:flex;gap:8px;font-size:11px;margin:6px 2px 12px" class="muted">
        <span style="color:#ff6b6b">■ X</span><span style="color:#51cf66">■ Y</span><span style="color:#4dabf7">■ Z</span>
        <span style="margin-left:auto">accelerometer (g) · laatste ~1,5 s</span>
      </div>
      <div class="imugrid">
        <div class="acol">
          <div class="atitle">Versnelling (g)</div>
          <div class="arow"><span>X</span><div class="abar"><i id="axb"></i></div><b id="axv">–</b></div>
          <div class="arow"><span>Y</span><div class="abar"><i id="ayb"></i></div><b id="ayv">–</b></div>
          <div class="arow"><span>Z</span><div class="abar"><i id="azb"></i></div><b id="azv">–</b></div>
        </div>
        <div class="acol">
          <div class="atitle">Rotatie (°/s)</div>
          <div class="arow"><span>X</span><div class="abar g"><i id="gxb"></i></div><b id="gxv">–</b></div>
          <div class="arow"><span>Y</span><div class="abar g"><i id="gyb"></i></div><b id="gyv">–</b></div>
          <div class="arow"><span>Z</span><div class="abar g"><i id="gzb"></i></div><b id="gzv">–</b></div>
        </div>
        <div class="acol center">
          <div class="atitle">G-kracht</div>
          <div class="big" id="gforce">–</div>
          <div class="muted" style="font-size:11px">piek <b id="gpeak" style="color:var(--warn)">–</b> · <span id="gpeakReset" style="cursor:pointer;color:var(--acc)">reset</span></div>
          <div class="atitle" style="margin-top:12px">Stand</div>
          <div class="mono" id="tilt">–</div>
          <div class="muted" style="font-size:11px">chip <b id="imuTemp">–</b></div>
        </div>
      </div>
    </div>
  </div>

  <div class="card span2"><h2>Live camera <span id="frameInfo" class="muted"></span></h2>
    <img class="frame" id="frame" alt="camera" src="/frame.jpg">
    <div class="body kv">
      <div class="k">Laatste frame</div><div class="v mono" id="frameTs">–</div>
      <div class="k">Frames in buffer</div><div class="v" id="frameCount">–</div>
      <div class="k">Resolutie (config)</div><div class="v" id="res">–</div>
    </div>
  </div>

  <div class="card span2" style="display:none"><h2>Opnames <span id="recCount" class="muted" style="font-weight:400"></span></h2><div class="body">
    <div class="muted" style="font-size:12px;margin-bottom:10px">De meest recente camerabeelden. Losse video-clips verschijnen hier zodra we de eigen recorder bouwen.</div>
    <div style="display:flex;gap:8px;margin-bottom:10px"><button class="ledbtn" id="recRefresh" style="flex:none;padding:6px 12px">Vernieuwen</button></div>
    <div id="recGallery" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px"></div>
  </div></div>

  <div class="card span2"><h2>Video-opnames <span id="clipCount" class="muted" style="font-weight:400"></span></h2><div class="body">
    <div id="clipPlayer" style="display:none;margin-bottom:12px">
      <video id="clipVideo" controls autoplay playsinline style="width:100%;max-height:52vh;background:#000;border-radius:8px;display:block"></video>
      <div class="muted" id="clipNow" style="font-size:11px;margin-top:4px"></div>
    </div>
    <div id="clipGallery"></div>
  </div></div>

  <div class="card"><h2>Opname-recorder <span id="recPill" class="pill b">–</span></h2><div class="body">
    <div style="display:flex;gap:8px;margin-bottom:12px">
      <button class="ledbtn" id="recStart">Start opname</button>
      <button class="ledbtn" id="recStop">Camera uit</button>
    </div>
    <div id="recErr" class="err-line" style="margin-bottom:8px;display:none"></div>
    <div class="kv">
      <div class="k">Status</div><div class="v" id="recStat">–</div>
      <div class="k">Clips</div><div class="v" id="recClips">–</div>
      <div class="k">Segmentduur</div><div class="v"><select class="ledsel" id="recSeg" style="width:auto"><option value="30">30 s</option><option value="60">1 min</option><option value="180">3 min</option><option value="300">5 min</option></select></div>
      <div class="k">Bewaarlimiet</div><div class="v"><select class="ledsel" id="recCap" style="width:auto"><option value="5">5 GB</option><option value="10">10 GB</option><option value="15">15 GB</option><option value="20">20 GB</option></select></div>
    </div>
    <div class="muted" style="font-size:11px;margin-top:10px">Standalone dashcam-modus: neemt op naar /mnt/data/clips (1080p30, hardware-H.264), oudste clips worden gewist boven de limiet, GPS + beweging meegelogd. "Camera uit" stopt de opname maar blijft standalone. Overleeft een reboot — werkt in de auto vanzelf zodra hij stroom krijgt.</div>
    <div style="margin-top:6px"><a id="recHive" style="font-size:11px;color:var(--mut);cursor:pointer">↩ Hivemapper-camera herstellen</a></div>
  </div></div>

  <div class="card"><h2>LED-bediening <span id="ledModeState" class="pill b">–</span></h2><div class="body">
    <div style="display:flex;gap:8px;margin-bottom:14px">
      <button class="ledbtn" id="ledDashcam">Dashcam-status</button>
      <button class="ledbtn" id="ledSelf">Zelf instellen</button>
    </div>
    <div class="ledrow"><label>LED boven</label><span class="ledswatch" id="sw0"></span><select class="ledsel" id="ledfn0"></select></div>
    <div class="ledrow"><label>LED midden</label><span class="ledswatch" id="sw1"></span><select class="ledsel" id="ledfn1"></select></div>
    <div class="ledrow"><label>LED onder</label><span class="ledswatch" id="sw2"></span><select class="ledsel" id="ledfn2"></select></div>
    <div class="muted" style="font-size:11px;margin-top:8px">Bij "zelf instellen" stuurt het dashboard de LEDs aan (blijft stabiel, ook na een API-herstart). GPS-fix / beweging / temp / snelheid updaten live mee.</div>
  </div></div>

  <div class="card"><h2>GPS / GNSS <span id="fixPill" class="pill b">–</span></h2><div class="body">
    <div class="kv">
      <div class="k">Latitude</div><div class="v mono" id="lat">–</div>
      <div class="k">Longitude</div><div class="v mono" id="lon">–</div>
      <div class="k">Satellieten</div><div class="v" id="sats">–</div>
      <div class="k">Snelheid</div><div class="v" id="speed">–</div>
      <div class="k">Koers</div><div class="v" id="heading">–</div>
      <div class="k">Hoogte</div><div class="v" id="alt">–</div>
      <div class="k">Nauwkeurigh. (H/V)</div><div class="v" id="acc">–</div>
      <div class="k">TTFF</div><div class="v" id="ttff">–</div>
    </div>
    <div style="margin:10px 0 6px" class="muted" id="mapLink"></div>
    <div class="dop" id="dop"></div>
    <details><summary>Ruwe NMEA (GGA)</summary><pre id="gga">–</pre></details>
  </div></div>

  <div class="card"><h2>GNSS RF / anti-jamming</h2><div class="body kv">
    <div class="k">Jamming-status</div><div class="v" id="jam">–</div>
    <div class="k">Jam-indicator</div><div class="v" id="jamind">–</div>
    <div class="k">Antenne</div><div class="v" id="ant">–</div>
    <div class="k">Antenne-voeding</div><div class="v" id="antpwr">–</div>
    <div class="k">Ruis / ms</div><div class="v" id="noise">–</div>
    <div class="k">AGC</div><div class="v" id="agc">–</div>
    <div class="k">C/No</div><div class="v" id="cno">–</div>
    <div class="k">Spoof-detectie</div><div class="v" id="spoof">–</div>
  </div></div>

  <div class="card"><h2>Systeem</h2><div class="body">
    <div><div class="k muted">CPU-gebruik <span style="float:right" id="cpuAll">–</span></div><div class="bar" id="cpuAllWrap"><i id="cpuAllBar"></i></div></div>
    <div id="cores" style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:8px 0 4px"></div>
    <div class="kv" style="margin-top:8px">
      <div class="k">CPU-temp</div><div class="v" id="temp">–</div>
      <div class="k">CPU-klok / gov.</div><div class="v" id="mhz">–</div>
      <div class="k">Load (1/5/15m)</div><div class="v mono" id="load">–</div>
      <div class="k">Cores / processen</div><div class="v" id="cpuproc">–</div>
    </div>
    <div style="margin-top:10px"><div class="k muted">Geheugen <span id="memTxt" style="float:right"></span></div><div class="bar" id="memBarWrap"><i id="memBar"></i></div></div>
    <div style="margin-top:8px"><div class="k muted">Swap <span id="swapTxt" style="float:right"></span></div><div class="bar"><i id="swapBar"></i></div></div>
  </div></div>

  <div class="card"><h2>Opslag</h2><div class="body" id="disks"></div></div>

  <div class="card"><h2>Services</h2><div class="body"><div class="svcgrid" id="svc"></div></div></div>

  <div class="card"><h2>Status-LEDs</h2><div class="body">
    <div class="leds" id="leds"></div>
    <div class="kv" style="margin-top:12px">
      <div class="k">Sessie-ID</div><div class="v mono" id="session">–</div>
      <div class="k">Serienr.</div><div class="v mono" id="serial">–</div>
      <div class="k">Board</div><div class="v mono" id="board">–</div>
      <div class="k">Device-ID</div><div class="v mono" id="devid">–</div>
      <div class="k">SSID</div><div class="v mono" id="ssid">–</div>
    </div>
  </div></div>

  <div class="card"><h2>Netwerk</h2><div class="body">
    <div class="kv"><div class="k">IP-adres(sen)</div><div class="v mono" id="ips">–</div>
    <div class="k">WiFi-modus</div><div class="v" id="wifimode">–</div></div>
    <div id="nets" style="margin-top:8px"></div>
  </div></div>

  <div class="card"><h2>Data & opnames</h2><div class="body kv" id="datacounts">
    <div class="k">FrameKM buffer</div><div class="v" id="fkm">–</div>
    <div class="k">GPS-logs</div><div class="v" id="gpslogs">–</div>
    <div class="k">IMU-logs</div><div class="v" id="imulogs">–</div>
  </div><div class="body" style="border-top:1px solid var(--bd)" id="dbs"></div></div>

  <div class="card"><h2>Firmware-slots (RAUC)</h2><div class="body"><pre id="rauc">–</pre></div></div>

  <div class="card span2"><h2>Volledige configuratie</h2><div class="body">
    <details open><summary id="cfgSummary">Alle instellingen</summary><pre id="config">–</pre></details>
  </div></div>

</div>
<div class="foot" id="foot">Hivemapper HDC lokaal dashboard &middot; data van API :5000 + systeem</div>
</div>
<script>
const $=id=>document.getElementById(id);
const fmtBytes=b=>{if(b==null)return'–';const u=['B','KB','MB','GB','TB'];let i=0;b=+b;while(b>=1024&&i<u.length-1){b/=1024;i++}return b.toFixed(b<10&&i>0?1:0)+' '+u[i]};
const fmtDur=s=>{s=Math.floor(s);const d=Math.floor(s/86400);s%=86400;const h=Math.floor(s/3600);s%=3600;const m=Math.floor(s/60);return(d?d+'d ':'')+h+'u '+m+'m'};
async function jget(u){const r=await fetch(u,{cache:'no-store'});if(!r.ok)throw new Error(r.status);return r.json()}

async function tick(){
  try{
    const [info,ping,gps,rec]=await Promise.all([
      jget('/api/1/info').catch(()=>({})),
      jget('/api/1/ping').catch(()=>({})),
      jget('/api/1/gps/sample').catch(()=>({})),
      jget('/api/1/recordings').catch(()=>[])
    ]);
    // header / info
    $('hostname').textContent=info.ssid?('dashcam-'+info.ssid):(ping.ssid?'dashcam-'+ping.ssid:'dashcam');
    $('dashcamType').textContent=(info.dashcam||ping.dashcam||'hdc').toUpperCase();
    $('fw').textContent=info.api_version||'–';
    $('apiv').textContent=info.api_version||'–';
    const healthy=ping.healthy;
    $('healthDot').innerHTML='<span class="dot '+(healthy?'ok':'err')+'"></span>'+(healthy?'gezond':'probleem');
    // ping-details
    $('session').textContent=ping.sessionId||'–';
    $('serial').textContent=(ping.serial||'').replace(/_+$/,'')||'–';
    $('board').textContent=ping.boardConfig||'–';
    $('devid').textContent=info.deviceId||'–';
    $('ssid').textContent=info.ssid||ping.ssid||'–';
    // leds
    if(ping.leds){const col={GREEN:'#3fb950',RED:'#f85149',DIM:'#39506e',OFF:'#39506e',YELLOW:'#d29922',AMBER:'#d29922'};
      $('leds').innerHTML=Object.entries(ping.leds).map(([k,v])=>
        '<div class="led"><div class="ring" style="background:'+(col[v]||'#39506e')+';border-color:'+(col[v]||'#263248')+'"></div><span>'+k.replace('LED','')+'</span></div>').join('');}
    // gps
    const fix=(gps.fix||'none');
    const fp=$('fixPill');fp.textContent=fix==='none'?'geen fix':fix;
    fp.className='pill '+(fix==='3D'?'g':fix==='2D'?'y':'r');
    $('lat').textContent=gps.latitude!=null?gps.latitude.toFixed(7):'–';
    $('lon').textContent=gps.longitude!=null?gps.longitude.toFixed(7):'–';
    if(gps.satellites)$('sats').textContent=(gps.satellites.used??'?')+' gebruikt / '+(gps.satellites.seen??'?')+' zichtbaar';
    $('speed').textContent=gps.speed!=null?(gps.speed*3.6).toFixed(1)+' km/u':'–';
    $('heading').textContent=gps.heading!=null?gps.heading.toFixed(0)+'°':'–';
    $('alt').textContent=gps.height!=null?gps.height.toFixed(0)+' m':'–';
    $('acc').textContent=(gps.horizontal_accuracy!=null?gps.horizontal_accuracy.toFixed(1):'?')+' / '+(gps.vertical_accuracy!=null?gps.vertical_accuracy.toFixed(1):'?')+' m';
    $('ttff').textContent=(gps.ttff&&gps.ttff<1e12)?(gps.ttff/1000).toFixed(0)+' s':'geen';
    if(gps.latitude&&gps.longitude&&fix!=='none')
      $('mapLink').innerHTML='📍 <a href="https://www.openstreetmap.org/?mlat='+gps.latitude+'&mlon='+gps.longitude+'#map=17/'+gps.latitude+'/'+gps.longitude+'" target="_blank">Toon op kaart</a>';
    else $('mapLink').innerHTML='<span class="muted">geen positie-fix (binnen / weinig zicht)</span>';
    if(gps.dop)$('dop').innerHTML=['hdop','vdop','pdop','gdop'].map(k=>'<div><b>'+(gps.dop[k]>=99?'∞':gps.dop[k].toFixed(1))+'</b><span>'+k+'</span></div>').join('');
    $('gga').textContent=gps.gga||'–';
    // rf
    const rf=gps.rf||{};
    $('jam').textContent=rf.jamming_state||'–';
    $('jamind').textContent=rf.jam_ind!=null?rf.jam_ind:'–';
    $('ant').textContent=rf.ant_status||'–';
    $('antpwr').textContent=rf.ant_power||'–';
    $('noise').textContent=rf.noise_per_ms!=null?rf.noise_per_ms:'–';
    $('agc').textContent=rf.agc_cnt!=null?rf.agc_cnt:'–';
    $('cno').textContent=gps.cno!=null?gps.cno.toFixed(0)+' dBHz':'–';
    $('spoof').textContent=(gps.spoof_state||'ok');
    // frames
    $('frameCount').textContent=Array.isArray(rec)?rec.length:'–';
    if(Array.isArray(rec)&&rec.length){const last=rec[rec.length-1];$('frameTs').textContent=new Date(last.date).toLocaleTimeString('nl-NL');}
    $('conn').innerHTML='<span class="dot ok"></span>live';
  }catch(e){$('conn').innerHTML='<span class="dot err"></span>offline';}
}

async function tickFrame(){const img=$('frame');img.src='/frame.jpg?t='+Date.now();}

async function tickSys(){
  try{
    const s=await jget('/sys.json');
    $('uptime').textContent=fmtDur(s.uptime_s);
    $('temp').textContent=$('temp2').textContent=(s.temp_c!=null?s.temp_c+' °C':'–');
    $('mhz').textContent=(s.cpu_mhz?s.cpu_mhz+' MHz':'–')+(s.governor?' · '+s.governor:'');
    // CPU-gebruik totaal + per core
    if(s.cpu){const all=s.cpu.cpu;if(all!=null){$('cpuAll').textContent=all+' %';$('cpuAllBar').style.width=all+'%';$('cpuAllWrap').className='bar'+(all>85?' hot':'');}
      const cores=Object.keys(s.cpu).filter(k=>k!=='cpu').sort();
      $('cores').innerHTML=cores.map(k=>{const v=s.cpu[k];return '<div style="text-align:center"><div class="bar" style="height:34px;display:flex;align-items:flex-end"><i style="width:100%;height:'+v+'%;align-self:flex-end"></i></div><span class="muted" style="font-size:9px">'+k.replace('cpu','C')+' '+Math.round(v)+'%</span></div>';}).join('');}
    $('load').textContent=(s.loadavg||'').split(' ').slice(0,3).join('  ');
    $('cpuproc').textContent=(s.cpu_count||'?')+' cores · '+(s.procs||'?')+' procs';
    // mem
    if(s.mem_total){const p=100*s.mem_used/s.mem_total;$('memBar').style.width=p+'%';$('memBarWrap').className='bar'+(p>85?' hot':'');
      $('memTxt').textContent=fmtBytes(s.mem_used)+' / '+fmtBytes(s.mem_total);}
    if(s.swap_total){const su=s.swap_total-s.swap_free;$('swapBar').style.width=(100*su/s.swap_total)+'%';$('swapTxt').textContent=fmtBytes(su)+' / '+fmtBytes(s.swap_total);}
    else $('swapTxt').textContent='—';
    // disks
    $('disks').innerHTML=Object.entries(s.disks||{}).map(([k,v])=>{const p=100*v.used/v.total;
      return '<div style="margin-bottom:10px"><div class="k muted">'+k+' <span style="float:right">'+fmtBytes(v.used)+' / '+fmtBytes(v.total)+'</span></div><div class="bar'+(p>90?' hot':'')+'"><i style="width:'+p+'%"></i></div></div>';}).join('');
    // services
    $('svc').innerHTML=Object.entries(s.services||{}).map(([k,v])=>{const ok=v==='active';
      return '<div class="svc"><span class="dot '+(ok?'ok':(v==='inactive'?'dim':'err'))+'"></span>'+k+' <span class="muted" style="margin-left:auto;font-size:11px">'+v+'</span></div>';}).join('');
    // netwerk
    $('ips').textContent=(s.ips||[]).join(', ')||'–';
    $('wifimode').textContent=(s.wifi_mode||'').startsWith('AP')?'Access Point':(s.wifi_mode||'–');
    $('nets').innerHTML=Object.entries(s.nets||{}).map(([k,v])=>
      '<div class="svc"><span class="dot '+(v.state==='up'?'ok':'dim')+'"></span>'+k+' <span class="muted mono" style="margin-left:auto;font-size:11px">'+(v.mac||'')+' · '+v.state+'</span></div>').join('');
    // dbs
    $('dbs').innerHTML='<div class="k muted" style="margin-bottom:6px">Databases</div>'+(s.dbs||[]).map(d=>
      '<div class="svc mono" style="font-size:11.5px">'+d.name+'<span class="muted" style="margin-left:auto">'+fmtBytes(d.size)+'</span></div>').join('');
    // rauc
    $('rauc').textContent=s.rauc||'–';
  }catch(e){}
}

async function tickSlow(){
  try{
    const [cfg,gpsL,imuL,fkm]=await Promise.all([
      jget('/api/1/config').catch(()=>({})),
      jget('/api/1/gps').catch(()=>[]),
      jget('/api/1/imu').catch(()=>[]),
      jget('/api/1/framekm/total').catch(()=>({}))
    ]);
    $('res').textContent=cfg.resolution||'–';
    $('config').textContent=JSON.stringify(cfg,null,2);
    $('cfgSummary').textContent='Alle instellingen ('+Object.keys(cfg).length+' sleutels)';
    $('gpslogs').textContent=Array.isArray(gpsL)?gpsL.length+' bestanden':'–';
    $('imulogs').textContent=Array.isArray(imuL)?imuL.length+' bestanden':'–';
    $('fkm').textContent=fkm.bytes!=null?fmtBytes(fkm.bytes):'–';
  }catch(e){}
}

// ---- Live IMU / G-sensor ----
let gPeakHold=0;
$('gpeakReset').onclick=()=>{gPeakHold=0;};
const scope=$('scope'),sctx=scope.getContext('2d');
function drawScope(hist){
  const w=scope.width=scope.clientWidth*devicePixelRatio, h=scope.height=150*devicePixelRatio;
  sctx.clearRect(0,0,w,h);
  // nul-lijn
  sctx.strokeStyle='#1c2432';sctx.lineWidth=1;
  sctx.beginPath();sctx.moveTo(0,h/2);sctx.lineTo(w,h/2);sctx.stroke();
  if(!hist||!hist.length)return;
  const range=2.0; // ±2g op volle hoogte
  const map=v=>h/2-(v/range)*(h/2*0.9);
  const axes=[['ax','#ff6b6b'],['ay','#51cf66'],['az','#4dabf7']];
  for(const [k,col] of axes){
    sctx.strokeStyle=col;sctx.lineWidth=1.5*devicePixelRatio;sctx.beginPath();
    hist.forEach((p,i)=>{const x=i/(hist.length-1)*w,y=map(p[k]);i?sctx.lineTo(x,y):sctx.moveTo(x,y);});
    sctx.stroke();
  }
}
async function tickImu(){
  try{
    const d=await jget('/imu.json');
    if(!d.ok)return;
    $('imuRate').textContent=(d.rate||'–')+' Hz';
    const a=d.accel,g=d.gyro;
    const setBar=(bid,vid,val,scale,dec)=>{const pct=Math.max(-1,Math.min(1,val/scale));
      const el=$(bid);el.style.width=Math.abs(pct)*50+'%';el.style.left=pct>=0?'50%':(50+pct*50)+'%';
      $(vid).textContent=val.toFixed(dec);};
    setBar('axb','axv',a.x,2,3);setBar('ayb','ayv',a.y,2,3);setBar('azb','azv',a.z,2,3);
    setBar('gxb','gxv',g.x,250,1);setBar('gyb','gyv',g.y,250,1);setBar('gzb','gzv',g.z,250,1);
    $('gforce').textContent=d.g.toFixed(3)+' g';
    if(d.peak_g>gPeakHold)gPeakHold=d.peak_g;
    $('gpeak').textContent=gPeakHold.toFixed(2)+' g';
    $('tilt').textContent='pitch '+d.pitch.toFixed(1)+'°  roll '+d.roll.toFixed(1)+'°';
    $('imuTemp').textContent=(d.temp!=null?d.temp.toFixed(1)+' °C':'–');
    // schok-detectie: afwijking van 1g of hoge rotatie
    const dev=Math.abs(d.peak_g-1);
    const shock=dev>0.35||d.peak_gyro>60;
    const sp=$('shockPill');
    if(shock){sp.textContent='BEWEGING!';sp.className='pill r';$('imuCard').classList.remove('shock');void $('imuCard').offsetWidth;$('imuCard').classList.add('shock');}
    else{sp.textContent='rustig';sp.className='pill g';}
    drawScope(d.hist);
  }catch(e){}
}

// ---- LED-bediening ----
const LED_FNS=[['uit','Uit'],['opname','Opname (ademend rood)'],['gps','GPS-fix (groen=3D)'],['beweging','Beweging / schok'],['temp','CPU-temp'],['snelheid','Snelheid'],['rood','Vaste kleur: rood'],['groen','Vaste kleur: groen'],['blauw','Vaste kleur: blauw'],['geel','Vaste kleur: geel'],['wit','Vaste kleur: wit'],['paars','Vaste kleur: paars']];
const LED_SW={uit:'#222',opname:'#ff4d4d',rood:'#ff4d4d',groen:'#3fb950',blauw:'#4dabf7',geel:'#f5d90a',wit:'#eee',paars:'#b197fc',gps:'#3fb950',beweging:'#ff4d4d',temp:'#f5d90a',snelheid:'#4dabf7'};
function fillLedSelects(){
  for(let i=0;i<3;i++){const s=$('ledfn'+i);s.innerHTML=LED_FNS.map(([v,t])=>'<option value="'+v+'">'+t+'</option>').join('');
    s.onchange=()=>{$('sw'+i).style.background=LED_SW[s.value]||'#222';fetch('/led/fn?i='+i+'&fn='+encodeURIComponent(s.value)).then(loadLedState);};}
  $('ledDashcam').onclick=()=>fetch('/led/mode?set=dashcam').then(loadLedState);
  $('ledSelf').onclick=()=>fetch('/led/mode?set=dashboard').then(loadLedState);
}
async function loadLedState(){try{const d=await jget('/led/state');
  const self=d.mode==='dashboard';
  $('ledModeState').textContent=self?'zelf':'dashcam-status';$('ledModeState').className='pill '+(self?'g':'b');
  $('ledDashcam').className='ledbtn'+(!self?' on':'');$('ledSelf').className='ledbtn'+(self?' on':'');
  for(let i=0;i<3;i++){const v=(d.fns&&d.fns[i])||'uit';$('ledfn'+i).value=v;$('sw'+i).style.background=LED_SW[v]||'#222';}
}catch(e){}}

// ---- Tabbladen ----
const TABMAP={'Systeem':'sys','Opslag':'sys','Services':'sys','Status-LEDs':'sys','Netwerk':'sys','Data & opnames':'sys','Firmware-slots':'sys','Volledige configuratie':'sys','LED-bediening':'settings','Opname-recorder':'settings','Opnames':'terug','Video-opnames':'terug'};
function assignTabs(){document.querySelectorAll('.card').forEach(c=>{const h=c.querySelector('h2');const t=h?h.textContent.trim():'';let tab='live';for(const k in TABMAP){if(t.indexOf(k)===0){tab=TABMAP[k];break;}}c.dataset.tab=tab;});}
function showTab(tab){document.querySelectorAll('.card').forEach(c=>c.classList.toggle('hidden',(c.dataset.tab||'live')!==tab));
  document.querySelectorAll('.tabbtn').forEach(b=>b.classList.toggle('on',b.dataset.tab===tab));
  if(tab==='terug'){loadRecordings();loadClips();}
  if(tab==='settings'){loadRec();loadLedState();}}
document.querySelectorAll('.tabbtn').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));

// ---- Recorder ----
async function loadRec(){try{const d=await jget('/rec/status');const run=d.running;
  $('recPill').textContent=run?'opname loopt':(d.standalone?'camera uit':'Hivemapper');
  $('recPill').className='pill '+(run?'g':(d.standalone?'y':'b'));
  $('recStat').textContent=run?('opnemen '+d.w+'×'+d.h+' @'+d.fps):(d.standalone?'standalone, opname uit':'Hivemapper actief');
  $('recClips').textContent=d.clips+' clips · '+fmtBytes(d.bytes);
  $('recStart').className='ledbtn'+(run?' on':'');$('recStop').className='ledbtn'+(!run&&d.standalone?' on':'');
  if(d.err){$('recErr').style.display='block';$('recErr').textContent='⚠ '+d.err;}else{$('recErr').style.display='none';}
  if($('recSeg').dataset.init!=='1'){$('recSeg').value=d.seg;$('recCap').value=d.cap_gb;$('recSeg').dataset.init='1';}
}catch(e){}}
$('recStart').onclick=async()=>{$('recStat').textContent='camera vrijmaken…';await fetch('/rec/start');setTimeout(loadRec,8000);};
$('recStop').onclick=async()=>{$('recStat').textContent='opname stoppen…';await fetch('/rec/stop');setTimeout(loadRec,2500);};
$('recHive').onclick=async()=>{if(!confirm('Hivemapper-camera herstellen (standalone verlaten)?'))return;$('recStat').textContent='herstellen…';await fetch('/rec/hivemapper');setTimeout(loadRec,3000);};
$('recSeg').onchange=()=>fetch('/rec/set?seg='+$('recSeg').value);
$('recCap').onchange=()=>fetch('/rec/set?cap_gb='+$('recCap').value);

// ---- Video-clips ----
function playClip(n,label){$('clipVideo').src='/clip?name='+encodeURIComponent(n);$('clipPlayer').style.display='block';$('clipNow').textContent='▶ '+(label||n);$('clipPlayer').scrollIntoView({behavior:'smooth',block:'nearest'});}
async function loadClips(){try{const list=await jget('/clips.json');
  const tot=list.reduce((a,c)=>a+(c.size||0),0);
  $('clipCount').textContent=(list.length||0)+' clips · '+fmtBytes(tot);
  if(!list.length){$('clipGallery').innerHTML='<span class="muted">nog geen video-clips — start de opname in Instellingen</span>';return;}
  $('clipGallery').innerHTML=list.map(c=>{const dt=new Date(c.mtime);
    const d=dt.toLocaleDateString('nl-NL',{weekday:'short',day:'2-digit',month:'short'});
    const t=dt.toLocaleTimeString('nl-NL',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    const lbl=d+' '+t;
    return '<div class="cliprow">'+
      '<div class="clipmeta"><b>'+t+'</b> <span class="muted">· '+d+' · '+fmtBytes(c.size)+'</span></div>'+
      '<div class="clipbtns">'+
      '<button class="clipbtn play" onclick="playClip(\''+c.name+'\',\''+lbl+'\')">▶</button>'+
      '<a class="clipbtn" href="/clip?name='+encodeURIComponent(c.name)+'" download="'+c.name+'">download</a>'+
      '<button class="clipbtn del" onclick="delClip(\''+c.name+'\')">wis</button>'+
      '</div></div>';}).join('');
}catch(e){}}
async function delClip(n){if(!confirm('Deze clip verwijderen?'))return;await fetch('/clip_del?name='+encodeURIComponent(n));loadClips();}

// ---- Opnames-galerij ----
async function loadRecordings(){try{const list=await jget('/api/1/recordings');
  if(!Array.isArray(list)){$('recGallery').innerHTML='<span class="muted">geen opnames</span>';return;}
  $('recCount').textContent=list.length+' beelden';
  const recent=list.slice(-16).reverse();
  if(!recent.length){$('recGallery').innerHTML='<span class="muted">nog geen beelden (camera neemt op bij GPS-lock)</span>';return;}
  $('recGallery').innerHTML=recent.map(r=>{const n=r.path,ts=new Date(r.date).toLocaleTimeString('nl-NL');
    return '<div style="background:var(--card2);border-radius:8px;overflow:hidden">'+
      '<img src="/recframe?name='+encodeURIComponent(n)+'" style="width:100%;aspect-ratio:2/1;object-fit:cover;background:#000" loading="lazy">'+
      '<div style="padding:6px 8px"><div class="muted" style="font-size:11px">'+ts+'</div>'+
      '<div style="display:flex;gap:6px;margin-top:5px">'+
      '<a class="recdl" href="/recframe?name='+encodeURIComponent(n)+'" download="'+n+'">download</a>'+
      '<button class="recdel" onclick="delRec(\''+n+'\')">wis</button></div></div></div>';}).join('');
}catch(e){$('recGallery').innerHTML='<span class="err-line">fout bij laden</span>';}}
async function delRec(n){if(!confirm('Dit beeld verwijderen?'))return;await fetch('/rec_del?name='+encodeURIComponent(n));loadRecordings();}
$('recRefresh').onclick=loadRecordings;

function clock(){$('clock').textContent=new Date().toLocaleTimeString('nl-NL');}
assignTabs();showTab('live');
fillLedSelects();loadLedState();loadRec();
tick();tickSys();tickSlow();tickImu();clock();
setInterval(tick,1500);setInterval(tickFrame,1500);setInterval(tickSys,4000);setInterval(tickSlow,15000);setInterval(clock,1000);
setInterval(tickImu,200);setInterval(loadRec,5000);
</script></body></html>"""


if __name__ == "__main__":
    load_led_settings()
    load_rec_settings()
    threading.Thread(target=led_driver, daemon=True).start()
    threading.Thread(target=retention_loop, daemon=True).start()
    threading.Thread(target=track_loop, daemon=True).start()
    threading.Thread(target=gps_time_sync, daemon=True).start()
    # standalone-modus + recorder hervatten na reboot
    if rec_cfg.get("standalone"):
        try:
            if rec_cfg.get("on"):
                start_recorder()
            else:
                suppress_hivemapper()
        except Exception:
            pass
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print("dashboard op :%d" % PORT)
    srv.serve_forever()
