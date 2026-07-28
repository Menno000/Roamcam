#!/usr/bin/env python3
# Hivemapper HDC - lokaal "alles-in-1" dashboard
# Draait op het toestel zelf, serveert de pagina + proxy naar de lokale API (:5000) + systeeminfo.
import json, os, socket, subprocess, urllib.request, glob, math, sqlite3, time, threading, calendar, re
import struct, fcntl, ctypes
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import gpiod
except Exception:
    gpiod = None

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
REC_DEFAULT = {"on": False, "standalone": False, "seg": 60, "cap_gb": 15, "w": 1920, "h": 1080,
               "fps": 30, "gain": 0, "shutter": 0, "gforce": 2.0}
rec_cfg = dict(REC_DEFAULT)
rec_state = {"proc": None, "err": "", "started": 0.0}
UI_DEFAULT = {"lang": "en", "units": "kmh"}  # standaard Engels; NL/mph instelbaar in de UI
ui_cfg = dict(UI_DEFAULT)


def load_ui_settings():
    try:
        d = json.load(open(LED_SETTINGS_PATH))
        if isinstance(d.get("ui"), dict):
            for k in UI_DEFAULT:
                if k in d["ui"]:
                    ui_cfg[k] = d["ui"][k]
    except Exception:
        pass


def save_ui_settings():
    try:
        d = {}
        try:
            d = json.load(open(LED_SETTINGS_PATH))
        except Exception:
            pass
        d["ui"] = dict(ui_cfg)
        with open(LED_SETTINGS_PATH, "w") as f:
            json.dump(d, f)
    except Exception:
        pass


def clip_locked(path):
    return os.path.exists(path[:-4] + ".lock")


def lock_clip(path, why="manual"):
    try:
        with open(path[:-4] + ".lock", "w") as f:
            f.write(json.dumps({"why": why, "t": time.time()}))
        return True
    except Exception:
        return False


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


# ---- Live preview (handmatig, standaard UIT — kost dan 0% CPU) ----
LIVE_TAIL_PATTERN = "/tmp/rc_live_%d.h264"
LIVE_TAIL_GLOB = "/tmp/rc_live_*.h264"
LIVE_PREVIEW_JPG = "/tmp/rc_live_preview.jpg"
live_state = {"enabled": False}


def live_pick_source():
    files = glob.glob(LIVE_TAIL_GLOB)
    if len(files) < 2:
        return None
    files.sort(key=os.path.getmtime)
    return files[-2]  # niet de nieuwste (kan nog beschreven worden), wel de meest recente afgeronde


def live_preview_loop():
    while True:
        try:
            if live_state["enabled"] and recorder_running():
                src = live_pick_source()
                if src:
                    subprocess.run(
                        ["ffmpeg", "-y", "-c:v", "h264_v4l2m2m", "-i", src,
                         "-frames:v", "1", "-s", "480x270", "-f", "image2", LIVE_PREVIEW_JPG],
                        capture_output=True, timeout=4)
                time.sleep(2)
            else:
                time.sleep(1)
        except Exception:
            time.sleep(2)


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
    live_state["enabled"] = False
    for f in glob.glob(LIVE_TAIL_GLOB) + [LIVE_PREVIEW_JPG]:
        try:
            os.remove(f)
        except Exception:
            pass


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
    # Tweede, vrijwel gratis uitgang (pure stream-copy, geen re-encode) die doorlopend een
    # rollend 1s-venster ruwe H.264 in RAM (/tmp = tmpfs) bijhoudt. Kost 0% extra CPU (gemeten).
    # Alleen wanneer live-preview AAN staat wordt hier incidenteel 1 frame uit gedecodeerd
    # (hardware-decoder, ~0.5s CPU per keer) — zie live_preview_loop().
    for f in glob.glob(LIVE_TAIL_GLOB):
        try:
            os.remove(f)
        except Exception:
            pass
    cmd = ("libcamera-vid -t 0 --inline --nopreview --width %d --height %d --framerate %d%s -o - 2>/mnt/data/rec_vid.log "
           "| ffmpeg -probesize 2M -analyzeduration 2M -f h264 -i - "
           "-map 0:v -c copy -f segment -segment_time %d -reset_timestamps 1 -strftime 1 %s/%%Y%%m%%d_%%H%%M%%S.mp4 "
           "-map 0:v -c copy -f segment -segment_time 1 -segment_wrap 3 -reset_timestamps 1 %s "
           "2>/mnt/data/rec_ff.log"
           % (w, h, fps, opts, seg, CLIPS_DIR, LIVE_TAIL_PATTERN))
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
                # beschermde (incident-lock) clips worden nooit automatisch gewist
                deletable = [f for f in files if not clip_locked(f)]
                while total > cap and len(deletable) > 1:
                    victim = deletable.pop(0)
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


def incident_loop():
    # G-sensor bewaakt schokken; boven de drempel wordt de lopende clip beschermd
    last_hit = 0.0
    while True:
        try:
            thr = float(rec_cfg.get("gforce", 0) or 0)
            if thr > 0 and rec_cfg.get("on") and recorder_running():
                d = imu_latest(25)
                if d.get("ok"):
                    peak = d.get("peak_g", 1.0)
                    now = time.time()
                    if abs(peak - 1.0) >= (thr - 1.0) and peak >= thr and now - last_hit > 5:
                        last_hit = now
                        clips = sorted(glob.glob(CLIPS_DIR + "/*.mp4"), key=lambda f: os.path.getmtime(f))
                        for c in clips[-2:]:  # lopende + vorige clip beschermen
                            if not clip_locked(c):
                                lock_clip(c, "incident %.2fg" % peak)
        except Exception:
            pass
        time.sleep(1)


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


def _srt_time(sec):
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _load_track():
    out = []
    try:
        with open(CLIPS_DIR + "/track.ndjson") as f:
            for ln in f:
                try:
                    o = json.loads(ln)
                    out.append((o["t"], o.get("gps", {})))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def generate_srt(clip_path, start_epoch, duration, track):
    idx = 1
    cues = []
    for i in range(int(duration)):
        cue_epoch = start_epoch + i
        best = None
        bestd = 2.5
        for e, g in track:
            d = abs(e - cue_epoch)
            if d < bestd:
                bestd = d
                best = g
        if best is None:
            continue
        tstr = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(cue_epoch)) + " UTC"
        lat, lon, fix = best.get("lat"), best.get("lon"), best.get("fix")
        if lat is not None and lon is not None and fix and fix != "none":
            ms = best.get("speed") or 0
            if ui_cfg.get("units") == "mph":
                sp, su = ms * 2.23694, "mph"
            else:
                sp, su = ms * 3.6, "km/h"
            line2 = "%.5f, %.5f   %d %s" % (lat, lon, round(sp), su)
            if best.get("heading") is not None:
                line2 += "   %d deg" % round(best["heading"])
            body = tstr + "\n" + line2
        else:
            body = tstr + "\nno GPS fix"
        cues.append("%d\n%s --> %s\n%s\n" % (idx, _srt_time(i), _srt_time(i + 1), body))
        idx += 1
    try:
        with open(clip_path[:-4] + ".srt", "w") as f:
            f.write("\n".join(cues))
    except Exception:
        pass


def srt_loop():
    # maakt per afgeronde clip een .srt met tijd/positie/snelheid uit de GPS-track
    while True:
        try:
            clips = glob.glob(CLIPS_DIR + "/*.mp4")
            starts = {}
            for c in clips:
                base = os.path.basename(c)[:-4]
                try:
                    starts[c] = calendar.timegm(time.strptime(base, "%Y%m%d_%H%M%S"))
                except Exception:
                    pass
            ordered = sorted(starts, key=lambda c: starts[c])
            if len(ordered) >= 2:
                track = _load_track()
                for i in range(len(ordered) - 1):  # laatste clip = nog bezig
                    c = ordered[i]
                    if os.path.exists(c[:-4] + ".srt"):
                        continue
                    dur = starts[ordered[i + 1]] - starts[c]
                    if dur <= 0 or dur > rec_cfg.get("seg", 60) * 3:
                        dur = rec_cfg.get("seg", 60)
                    generate_srt(c, starts[c], dur, track)
            # track.ndjson bijhouden op ~2 uur
            tf = CLIPS_DIR + "/track.ndjson"
            if os.path.exists(tf):
                lines = open(tf).readlines()
                if len(lines) > 7200:
                    with open(tf, "w") as f:
                        f.writelines(lines[-7200:])
        except Exception:
            pass
        time.sleep(15)


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
    if fn == "lora":
        st = lora_state.get("status")
        if st == "joined":
            return LED_COLORS["groen"]
        if st == "searching":
            return LED_COLORS["geel"]
        return LED_COLORS["uit"]
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


# ---- LoRa / TTN (optioneel, uit tenzij de eigenaar het instelt) ----
# Puur stdlib: eigen AES-128 + AES-CMAC (LoRaWAN 1.0.3 OTAA), rechtstreeks over
# spidev + gpiod naar de SX1262 op de camera. Geen extra packages nodig.
LORA_DEFAULT = {"backend": "off", "deveui": "", "appkey": "", "joineui": "0000000000000000"}
lora_cfg = dict(LORA_DEFAULT)
lora_state = {"status": "off", "backend": "off", "devaddr": None, "attempts": 0, "uplinks": 0,
              "last_join_attempt": 0, "last_uplink": 0, "last_error": ""}

# ---- Meshtastic backend (optional second choice) ----
# Needs meshtasticd + its .so dependencies manually staged under MESHTASTICD_DIR --
# not bundled with Roamcam itself (large binary, see docs). Gracefully reports
# "not installed" if it's missing, rather than erroring.
MESHTASTICD_DIR = "/mnt/data/meshtasticd"
MESHTASTICD_BIN = MESHTASTICD_DIR + "/meshtasticd"
MESHTASTICD_LIB = MESHTASTICD_DIR + "/lib"
MESHTASTICD_CONFIG = MESHTASTICD_DIR + "/config.yaml"
MESHTASTICD_FSDIR = MESHTASTICD_DIR + "/data"
meshtasticd_proc = None


def meshtasticd_installed():
    return os.path.isfile(MESHTASTICD_BIN) and os.path.isdir(MESHTASTICD_LIB) and os.path.isfile(MESHTASTICD_CONFIG)


def _meshtasticd_hwid():
    # stabiele, unieke identiteit per toestel: afgeleid van de eigen wifi-MAC
    for ifc in ("wlan0", "eth0"):
        mac = read("/sys/class/net/%s/address" % ifc)
        if mac and mac != "00:00:00:00:00:00":
            return mac
    return "02:00:00:00:00:01"


def meshtasticd_start():
    global meshtasticd_proc
    if meshtasticd_proc is not None and meshtasticd_proc.poll() is None:
        return
    # Als het dashboard herstart (crash/kill) zonder dat zijn vorige meshtasticd-kind
    # netjes meeging, blijft die wees de GPIO-pinnen vasthouden en faalt de nieuwe start.
    # killall is idempotent -- geen probleem als er toch niks draait.
    sh("killall meshtasticd 2>/dev/null")
    time.sleep(1)
    os.makedirs(MESHTASTICD_FSDIR, exist_ok=True)
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = MESHTASTICD_LIB
    log_path = MESHTASTICD_DIR + "/run.log"
    logf = open(log_path, "w")  # vers logje per start, anders leest de statuscheck oude regels
    meshtasticd_proc = subprocess.Popen(
        [MESHTASTICD_BIN, "--config", MESHTASTICD_CONFIG, "--fsdir", MESHTASTICD_FSDIR,
         "--port", "4403", "--hwid", _meshtasticd_hwid()],
        env=env, stdout=logf, stderr=subprocess.STDOUT)


def meshtasticd_stop():
    global meshtasticd_proc
    if meshtasticd_proc is not None:
        try:
            meshtasticd_proc.terminate()
            meshtasticd_proc.wait(timeout=5)
        except Exception:
            try:
                meshtasticd_proc.kill()
            except Exception:
                pass
        meshtasticd_proc = None


def meshtasticd_check_log():
    # geen protobuf-API-client hier -- we lezen simpelweg de laatste regels van het eigen logje
    try:
        with open(MESHTASTICD_DIR + "/run.log", "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4000))
            tail = f.read().decode(errors="ignore")
        if "sx1262 init success" in tail or "API server listen" in tail:
            return "joined"
        if "Failed" in tail or "error" in tail.lower():
            return "searching"
    except Exception:
        pass
    return "searching"


# Minimal, dependency-free encoder for meshtasticd's local client TCP API (port 4403),
# just enough to send a text message -- no pip package, matches this project's
# stdlib-only design. Field numbers/wire types verified against meshtastic/protobufs.
def _mesh_pb_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _mesh_pb_tag(field_num, wire_type):
    return _mesh_pb_varint((field_num << 3) | wire_type)


def _mesh_pb_varint_field(field_num, value):
    return _mesh_pb_tag(field_num, 0) + _mesh_pb_varint(value)


def _mesh_pb_fixed32_field(field_num, value):
    return _mesh_pb_tag(field_num, 5) + struct.pack("<I", value)


def _mesh_pb_bytes_field(field_num, data):
    return _mesh_pb_tag(field_num, 2) + _mesh_pb_varint(len(data)) + data


def meshtastic_send_text(text, channel=0):
    data_msg = _mesh_pb_varint_field(1, 1) + _mesh_pb_bytes_field(2, text.encode("utf-8"))  # portnum=TEXT_MESSAGE_APP
    pkt_id = int.from_bytes(os.urandom(4), "little") or 1
    mesh_packet = (
        _mesh_pb_fixed32_field(2, 0xFFFFFFFF)  # to = broadcast
        + _mesh_pb_varint_field(3, channel)
        + _mesh_pb_bytes_field(4, data_msg)  # decoded
        + _mesh_pb_fixed32_field(6, pkt_id)  # id
    )
    to_radio = _mesh_pb_bytes_field(1, mesh_packet)  # ToRadio.packet
    header = bytes([0x94, 0xC3, (len(to_radio) >> 8) & 0xFF, len(to_radio) & 0xFF])
    s = socket.create_connection(("127.0.0.1", 4403), timeout=5)
    try:
        s.sendall(header + to_radio)
    finally:
        s.close()


_LORA_SBOX = [
0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16]
_LORA_RCON = [0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36]


def _lora_xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11b
    return a & 0xff


def _lora_key_expansion(key):
    Nk = 4
    w = [list(key[4*i:4*i+4]) for i in range(Nk)]
    for i in range(Nk, 44):
        temp = list(w[i - 1])
        if i % Nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_LORA_SBOX[b] for b in temp]
            temp[0] ^= _LORA_RCON[i // Nk - 1]
        w.append([w[i - Nk][j] ^ temp[j] for j in range(4)])
    return w


def lora_aes128_encrypt_block(key, block16):
    w = _lora_key_expansion(key)
    state = [[block16[r + 4 * c] for c in range(4)] for r in range(4)]

    def add_round_key(state, rk):
        for c in range(4):
            for r in range(4):
                state[r][c] ^= rk[c][r]

    def sub_bytes(state):
        for r in range(4):
            for c in range(4):
                state[r][c] = _LORA_SBOX[state[r][c]]

    def shift_rows(state):
        for r in range(1, 4):
            state[r] = state[r][r:] + state[r][:r]

    def mix_columns(state):
        for c in range(4):
            a = [state[r][c] for r in range(4)]
            state[0][c] = _lora_xtime(a[0]) ^ (_lora_xtime(a[1]) ^ a[1]) ^ a[2] ^ a[3]
            state[1][c] = a[0] ^ _lora_xtime(a[1]) ^ (_lora_xtime(a[2]) ^ a[2]) ^ a[3]
            state[2][c] = a[0] ^ a[1] ^ _lora_xtime(a[2]) ^ (_lora_xtime(a[3]) ^ a[3])
            state[3][c] = (_lora_xtime(a[0]) ^ a[0]) ^ a[1] ^ a[2] ^ _lora_xtime(a[3])

    add_round_key(state, w[0:4])
    for rnd in range(1, 10):
        sub_bytes(state); shift_rows(state); mix_columns(state)
        add_round_key(state, w[4*rnd:4*rnd+4])
    sub_bytes(state); shift_rows(state)
    add_round_key(state, w[40:44])

    out = bytearray(16)
    for c in range(4):
        for r in range(4):
            out[r + 4*c] = state[r][c]
    return bytes(out)


def _lora_shift_left_1(b):
    n = int.from_bytes(b, "big") << 1
    n &= (1 << (8 * len(b))) - 1
    return n.to_bytes(len(b), "big")


def lora_aes_cmac(key, msg):
    const_Rb = 0x87
    L = lora_aes128_encrypt_block(key, b"\x00" * 16)
    K1 = _lora_shift_left_1(L)
    if L[0] & 0x80:
        K1 = (int.from_bytes(K1, "big") ^ const_Rb).to_bytes(16, "big")
    K2 = _lora_shift_left_1(K1)
    if K1[0] & 0x80:
        K2 = (int.from_bytes(K2, "big") ^ const_Rb).to_bytes(16, "big")
    if len(msg) == 0 or len(msg) % 16 != 0:
        padded = msg + b"\x80" + b"\x00" * (15 - (len(msg) % 16))
        M_last = bytes(a ^ b for a, b in zip(padded, K2))
    else:
        M_last = bytes(a ^ b for a, b in zip(msg[-16:], K1))
    blocks = [msg[i:i+16] for i in range(0, len(msg) - 16, 16)] if len(msg) > 16 else []
    x = b"\x00" * 16
    for b in blocks:
        x = lora_aes128_encrypt_block(key, bytes(a ^ c for a, c in zip(x, b)))
    x = lora_aes128_encrypt_block(key, bytes(a ^ c for a, c in zip(x, M_last)))
    return x


# SX1262 raw driver -- exact pins the stock lorawan-logger uses (/opt/dashcam/cfg/lorawan.conf)
LORA_GPIOCHIP = "gpiochip0"
LORA_CS_OFFSET, LORA_BUSY_OFFSET, LORA_NRST_OFFSET, LORA_DIO1_OFFSET = 18, 25, 24, 7
LORA_SPI_DEV = "/dev/spidev1.0"
_LORA_SPI_IOC_MAGIC = ord('k')


def _lora_IOC(direction, type_, nr, size):
    return (direction << 30) | (type_ << 8) | (nr << 0) | (size << 16)


def _lora_SPI_IOC_MESSAGE(n):
    return _lora_IOC(1, _LORA_SPI_IOC_MAGIC, 0, n * 32)


_LORA_SPI_IOC_WR_MODE = _lora_IOC(1, _LORA_SPI_IOC_MAGIC, 1, 1)
_LORA_SPI_IOC_WR_BITS = _lora_IOC(1, _LORA_SPI_IOC_MAGIC, 3, 1)
_LORA_SPI_IOC_WR_SPEED = _lora_IOC(1, _LORA_SPI_IOC_MAGIC, 4, 4)

LORA_IRQ_TX_DONE = 1 << 0
LORA_IRQ_RX_DONE = 1 << 1
LORA_IRQ_TIMEOUT = 1 << 9
LORA_JOIN_FREQ, LORA_JOIN_SF = 868100000, 7
LORA_RX2_FREQ, LORA_RX2_SF = 869525000, 9


class LoraRadio:
    def __init__(self):
        self.chip = gpiod.Chip(LORA_GPIOCHIP)
        self.cs = self.chip.get_line(LORA_CS_OFFSET)
        self.cs.request(consumer="roamcam_lora", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[1])
        self.busy = self.chip.get_line(LORA_BUSY_OFFSET)
        self.busy.request(consumer="roamcam_lora", type=gpiod.LINE_REQ_DIR_IN)
        self.nrst = self.chip.get_line(LORA_NRST_OFFSET)
        self.nrst.request(consumer="roamcam_lora", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[1])
        self.fd = open(LORA_SPI_DEV, "r+b", buffering=0)
        fcntl.ioctl(self.fd, _LORA_SPI_IOC_WR_MODE, struct.pack("B", 0))
        fcntl.ioctl(self.fd, _LORA_SPI_IOC_WR_BITS, struct.pack("B", 8))
        fcntl.ioctl(self.fd, _LORA_SPI_IOC_WR_SPEED, struct.pack("<I", 2000000))

    def _xfer(self, tx_bytes):
        n = len(tx_bytes)
        tx_buf = ctypes.create_string_buffer(bytes(tx_bytes), n)
        rx_buf = ctypes.create_string_buffer(n)
        packed = struct.pack("<QQIIHBBI", ctypes.addressof(tx_buf), ctypes.addressof(rx_buf),
                              n, 0, 0, 8, 0, 0)
        fcntl.ioctl(self.fd, _lora_SPI_IOC_MESSAGE(1), packed)
        return bytes(rx_buf.raw)

    def _wait_not_busy(self, timeout=1.0):
        t0 = time.time()
        while self.busy.get_value() == 1:
            if time.time() - t0 > timeout:
                raise TimeoutError("SX1262 BUSY stuck high")
            time.sleep(0.0005)

    def cmd(self, opcode, params=b"", read_len=0):
        self._wait_not_busy()
        self.cs.set_value(0)
        tx = bytes([opcode]) + bytes(params) + bytes(read_len)
        rx = self._xfer(tx)
        self.cs.set_value(1)
        return rx[1 + len(params):]

    def reset(self):
        self.nrst.set_value(0); time.sleep(0.001); self.nrst.set_value(1)
        self._wait_not_busy(timeout=1.0)

    def set_standby_rc(self): self.cmd(0x80, [0x00])
    def set_packet_type_lora(self): self.cmd(0x8A, [0x01])

    def set_rf_frequency(self, freq_hz):
        freq_reg = int(freq_hz * (1 << 25) / 32000000)
        self.cmd(0x86, list(struct.pack(">I", freq_reg)))

    def set_buffer_base_address(self, tx=0, rx=0): self.cmd(0x8F, [tx, rx])

    def set_modulation_params_lora(self, sf=7, bw=0x04, cr=1, ldro=0):
        self.cmd(0x8B, [sf, bw, cr, ldro])

    def set_packet_params_lora(self, preamble_len=8, header_type=0, payload_len=0, crc_on=1, invert_iq=0):
        b = struct.pack(">H", preamble_len) + bytes([header_type, payload_len, crc_on, invert_iq])
        self.cmd(0x8C, list(b))

    def set_tx_params(self, power_dbm=14, ramp=0x04): self.cmd(0x8E, [power_dbm & 0xFF, ramp])

    def set_dio_irq_params(self, irq_mask, dio1_mask):
        self.cmd(0x08, list(struct.pack(">HHHH", irq_mask, dio1_mask, 0, 0)))

    def clear_irq_status(self, mask=0xFFFF): self.cmd(0x02, list(struct.pack(">H", mask)))

    def get_irq_status(self):
        r = self.cmd(0x12, [0x00], read_len=2)
        return struct.unpack(">H", r[:2])[0]

    def write_buffer(self, offset, data): self.cmd(0x0E, [offset] + list(data))

    def read_buffer(self, offset, length):
        return self.cmd(0x1E, [offset, 0x00], read_len=length)

    def set_tx(self, timeout_ms=4000):
        t = int(timeout_ms * 1000 / 15.625)
        self.cmd(0x83, list(struct.pack(">I", t)[1:4]))

    def set_rx(self, timeout_ms):
        t = int(timeout_ms * 1000 / 15.625) if timeout_ms else 0xFFFFFF
        self.cmd(0x82, list(struct.pack(">I", t)[1:4]))

    def get_rx_buffer_status(self):
        r = self.cmd(0x13, [0x00], read_len=2)
        return r[0], r[1]

    def close(self):
        self.fd.close()
        self.cs.release(); self.busy.release(); self.nrst.release()


def _lora_configure(radio, freq_hz, sf, power=14):
    radio.set_standby_rc()
    radio.set_packet_type_lora()
    radio.set_rf_frequency(freq_hz)
    radio.set_buffer_base_address(0, 0)
    radio.set_modulation_params_lora(sf=sf, bw=0x04, cr=1, ldro=0)
    radio.set_tx_params(power_dbm=power, ramp=0x04)


def _lora_tx(radio, freq_hz, sf, payload):
    _lora_configure(radio, freq_hz, sf)
    radio.set_packet_params_lora(preamble_len=8, header_type=0, payload_len=len(payload), crc_on=1, invert_iq=0)
    radio.write_buffer(0, payload)
    radio.clear_irq_status(0xFFFF)
    radio.set_dio_irq_params(LORA_IRQ_TX_DONE | LORA_IRQ_TIMEOUT, LORA_IRQ_TX_DONE | LORA_IRQ_TIMEOUT)
    t0 = time.time()
    radio.set_tx(timeout_ms=4000)
    while True:
        irq = radio.get_irq_status()
        if irq & LORA_IRQ_TX_DONE:
            radio.clear_irq_status(0xFFFF)
            return time.time()
        if irq & LORA_IRQ_TIMEOUT or time.time() - t0 > 4.0:
            radio.clear_irq_status(0xFFFF)
            raise TimeoutError("LoRa TX timed out")
        time.sleep(0.005)


def _lora_rx_window(radio, freq_hz, sf, window_s, invert_iq=1):
    radio.set_standby_rc()
    radio.set_packet_type_lora()
    radio.set_rf_frequency(freq_hz)
    radio.set_buffer_base_address(0, 0)
    radio.set_modulation_params_lora(sf=sf, bw=0x04, cr=1, ldro=0)
    radio.set_packet_params_lora(preamble_len=8, header_type=0, payload_len=255, crc_on=0, invert_iq=invert_iq)
    radio.clear_irq_status(0xFFFF)
    radio.set_dio_irq_params(LORA_IRQ_RX_DONE | LORA_IRQ_TIMEOUT, LORA_IRQ_RX_DONE | LORA_IRQ_TIMEOUT)
    radio.set_rx(timeout_ms=int(window_s * 1000))
    t0 = time.time()
    while time.time() - t0 < window_s + 0.5:
        irq = radio.get_irq_status()
        if irq & LORA_IRQ_RX_DONE:
            plen, start = radio.get_rx_buffer_status()
            data = radio.read_buffer(start, plen)
            radio.clear_irq_status(0xFFFF)
            return data
        if irq & LORA_IRQ_TIMEOUT:
            radio.clear_irq_status(0xFFFF)
            return None
        time.sleep(0.01)
    return None


def _lora_build_join_request(appkey, joineui, deveui, devnonce):
    payload = bytes([0x00]) + joineui[::-1] + deveui[::-1] + struct.pack("<H", devnonce)
    return payload + lora_aes_cmac(appkey, payload)[:4]


def _lora_parse_join_accept(appkey, raw, devnonce):
    if raw is None or len(raw) < 12 or raw[0] != 0x20:
        return None
    pt = b""
    encrypted = raw[1:]
    for i in range(0, len(encrypted), 16):
        block = encrypted[i:i+16]
        if len(block) < 16:
            break
        pt += lora_aes128_encrypt_block(appkey, block)
    body = raw[0:1] + pt
    if len(body) < 12:
        return None
    ok = lora_aes_cmac(appkey, body[:-4])[:4] == body[-4:]
    return {"ok": ok, "appnonce": body[1:4], "netid": body[4:7], "devaddr": body[7:11]}


def _lora_derive_session_keys(appkey, appnonce, netid, devnonce):
    pad = appnonce + netid + struct.pack("<H", devnonce) + b"\x00" * 7
    return (lora_aes128_encrypt_block(appkey, b"\x01" + pad),
            lora_aes128_encrypt_block(appkey, b"\x02" + pad))


def _lora_encrypt_payload(key, devaddr, fcnt, payload):
    out = bytearray()
    i = 1
    for off in range(0, len(payload), 16):
        a = bytes([0x01, 0, 0, 0, 0, 0]) + devaddr[::-1] + struct.pack("<I", fcnt) + bytes([0x00, i])
        s = lora_aes128_encrypt_block(key, a)
        out += bytes(p ^ k for p, k in zip(payload[off:off+16], s))
        i += 1
    return bytes(out)


def _lora_build_uplink(nwkskey, appskey, devaddr, fcnt, fport, payload):
    mhdr = bytes([0x40])
    fhdr = devaddr[::-1] + bytes([0x00]) + struct.pack("<H", fcnt)
    enc = _lora_encrypt_payload(appskey, devaddr, fcnt, payload)
    msg = mhdr + fhdr + bytes([fport]) + enc
    b0 = bytes([0x49, 0, 0, 0, 0, 0]) + devaddr[::-1] + struct.pack("<I", fcnt) + bytes([0x00, len(msg)])
    return msg + lora_aes_cmac(nwkskey, b0 + msg)[:4]


def load_lora_settings():
    try:
        d = json.load(open(LED_SETTINGS_PATH))
        if isinstance(d.get("lora"), dict):
            for k in LORA_DEFAULT:
                if k in d["lora"]:
                    lora_cfg[k] = d["lora"][k]
    except Exception:
        pass


def save_lora_settings():
    try:
        d = {}
        try:
            d = json.load(open(LED_SETTINGS_PATH))
        except Exception:
            pass
        d["lora"] = dict(lora_cfg)
        with open(LED_SETTINGS_PATH, "w") as f:
            json.dump(d, f)
    except Exception:
        pass


def lora_loop():
    # Eén achtergrondlus die schakelt tussen backends (uit / TTN / Meshtastic).
    # De radio heeft maar één eigenaar tegelijk, dus lorawan-logger gaat uit zodra
    # een backend actief wordt, en komt terug zodra alles weer op "uit" staat.
    radio = None
    session = None
    fcnt = 0
    current_backend = "off"
    next_join_attempt = 0.0
    next_uplink = 0.0
    while True:
        try:
            wanted = lora_cfg.get("backend", "off")
            if wanted == "ttn" and not (lora_cfg.get("deveui") and lora_cfg.get("appkey")):
                wanted = "off"  # TTN gekozen maar nog niet geconfigureerd
            if wanted == "meshtastic" and not meshtasticd_installed():
                wanted = "off"  # meshtasticd niet (handmatig) geïnstalleerd
            if gpiod is None:
                wanted = "off"

            if wanted != current_backend:
                # backend wisselt: alles van de vorige backend netjes opruimen
                if current_backend == "ttn" and radio is not None:
                    try:
                        radio.close()
                    except Exception:
                        pass
                    radio = None
                    session = None
                if current_backend == "meshtastic":
                    meshtasticd_stop()
                if wanted == "off":
                    sh("systemctl start lorawan-logger 2>/dev/null")
                    lora_state["status"] = "off"
                else:
                    sh("systemctl stop lorawan-logger 2>/dev/null")
                    time.sleep(2)
                    lora_state["status"] = "searching"
                lora_state["backend"] = wanted
                lora_state["last_error"] = ""
                lora_state["devaddr"] = None
                next_join_attempt = 0.0
                next_uplink = 0.0
                current_backend = wanted

            if current_backend == "off":
                time.sleep(2)
                continue

            if current_backend == "meshtastic":
                if meshtasticd_proc is None or meshtasticd_proc.poll() is not None:
                    meshtasticd_start()
                lora_state["status"] = meshtasticd_check_log()
                time.sleep(3)
                continue

            # --- TTN backend ---
            if radio is None:
                radio = LoraRadio()

            if session is None:
                if time.time() < next_join_attempt:
                    time.sleep(1)
                    continue
                next_join_attempt = time.time() + 60
                lora_state["attempts"] += 1
                lora_state["last_join_attempt"] = time.time()
                try:
                    deveui = bytes.fromhex(lora_cfg["deveui"])
                    appkey = bytes.fromhex(lora_cfg["appkey"])
                    joineui = bytes.fromhex(lora_cfg.get("joineui") or "0000000000000000")
                    devnonce = struct.unpack("<H", os.urandom(2))[0]
                    radio.reset()
                    jreq = _lora_build_join_request(appkey, joineui, deveui, devnonce)
                    tx_done_t = _lora_tx(radio, LORA_JOIN_FREQ, LORA_JOIN_SF, jreq)
                    sleep_for = (tx_done_t + 5.0) - time.time()
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                    raw = _lora_rx_window(radio, LORA_JOIN_FREQ, LORA_JOIN_SF, window_s=1.5)
                    if raw is None:
                        sleep_for = (tx_done_t + 6.0) - time.time()
                        if sleep_for > 0:
                            time.sleep(sleep_for)
                        raw = _lora_rx_window(radio, LORA_RX2_FREQ, LORA_RX2_SF, window_s=2.0)
                    result = _lora_parse_join_accept(appkey, raw, devnonce)
                    if result and result["ok"]:
                        nwkskey, appskey = _lora_derive_session_keys(appkey, result["appnonce"], result["netid"], devnonce)
                        session = {"devaddr": result["devaddr"], "nwkskey": nwkskey, "appskey": appskey}
                        fcnt = 0
                        lora_state["status"] = "joined"
                        lora_state["devaddr"] = result["devaddr"].hex()
                        next_uplink = 0.0
                    else:
                        lora_state["status"] = "searching"
                except Exception as e:
                    lora_state["last_error"] = str(e)
                continue

            # sessie actief: periodiek een klein positiebakentje sturen
            if time.time() < next_uplink:
                time.sleep(1)
                continue
            next_uplink = time.time() + 300
            try:
                lat, lon = 0.0, 0.0
                try:
                    st, ct, body = proxy("/api/1/gps/sample")
                    if st == 200:
                        g = json.loads(body)
                        lat, lon = float(g.get("lat", 0) or 0), float(g.get("lng", g.get("lon", 0)) or 0)
                except Exception:
                    pass
                payload = struct.pack(">ff", lat, lon)
                frame = _lora_build_uplink(session["nwkskey"], session["appskey"], session["devaddr"], fcnt, 1, payload)
                _lora_tx(radio, LORA_JOIN_FREQ, LORA_JOIN_SF, frame)
                fcnt += 1
                lora_state["uplinks"] += 1
                lora_state["last_uplink"] = time.time()
            except Exception as e:
                lora_state["last_error"] = str(e)
        except Exception as e:
            lora_state["last_error"] = str(e)
            time.sleep(5)


# ---- Thuisnetwerk (STA-modus) -- wisselt met de eigen AP, kan hardware-matig niet tegelijk.
# Alleen geprobeerd bij het opstarten (niet doorlopend, anders knippert de AP telkens weg voor
# iedereen die er direct op zit). Lukt het niet: gewoon de vertrouwde AP, de rest van deze sessie.
WIFI_DEFAULT = {"home_ssid": "", "home_psk": "", "ap_psk": ""}
wifi_cfg = dict(WIFI_DEFAULT)
wifi_state = {"mode": "ap", "home_ip": "", "last_error": "", "tried_boot": False}
WPA_CONF_PATH = "/tmp/roamcam_wpa.conf"
WIFI_BOOT_TIMEOUT_S = 25


def load_wifi_settings():
    try:
        d = json.load(open(LED_SETTINGS_PATH))
        if isinstance(d.get("wifi"), dict):
            for k in WIFI_DEFAULT:
                if k in d["wifi"]:
                    wifi_cfg[k] = d["wifi"][k]
    except Exception:
        pass


def save_wifi_settings():
    try:
        d = {}
        try:
            d = json.load(open(LED_SETTINGS_PATH))
        except Exception:
            pass
        d["wifi"] = dict(wifi_cfg)
        with open(LED_SETTINGS_PATH, "w") as f:
            json.dump(d, f)
    except Exception:
        pass


def _wifi_ap_up():
    # Interface eerst hard resetten -- na een STA-poging staat wlan0 nog in client-modus,
    # en de brcmfmac-firmware op dit toestel wisselt niet betrouwbaar zonder een down/up.
    sh("pkill -f roamcam_wpa.conf 2>/dev/null; pkill udhcpc 2>/dev/null; pkill wpa_supplicant 2>/dev/null")
    sh("ip addr flush dev wlan0 2>/dev/null")
    sh("ip link set wlan0 down 2>/dev/null")
    time.sleep(1)
    sh("ip link set wlan0 up 2>/dev/null")
    time.sleep(1)
    sh("systemctl start hostapd 2>/dev/null")
    wifi_state["mode"] = "ap"
    wifi_state["home_ip"] = ""


def _wifi_home_ip():
    ip = read("/tmp/roamcam_sta.ip")
    return ip if ip else ""


def wifi_try_home_once():
    # Éénmalige, tijdgebonden poging -- met een garandeerde terugval naar de AP, wat er ook gebeurt.
    if not (wifi_cfg.get("home_ssid") and wifi_cfg.get("home_psk")):
        return
    try:
        sh("systemctl stop hostapd 2>/dev/null")
        sh("pkill -f roamcam_wpa.conf 2>/dev/null; pkill udhcpc 2>/dev/null")
        time.sleep(1)
        conf = ('ctrl_interface=/var/run/wpa_supplicant\nnetwork={\n  ssid="%s"\n  psk="%s"\n}\n'
                % (wifi_cfg["home_ssid"].replace('"', ""), wifi_cfg["home_psk"].replace('"', "")))
        with open(WPA_CONF_PATH, "w") as f:
            f.write(conf)
        sh("wpa_supplicant -B -i wlan0 -c %s 2>/mnt/data/wifi_sta.log" % WPA_CONF_PATH)
        t0 = time.time()
        connected = False
        while time.time() - t0 < WIFI_BOOT_TIMEOUT_S:
            st = sh("wpa_cli -i wlan0 status 2>/dev/null")
            if "wpa_state=COMPLETED" in st:
                connected = True
                break
            time.sleep(1)
        if not connected:
            wifi_state["last_error"] = "kon niet verbinden (SSID niet in bereik of verkeerd wachtwoord)"
            raise RuntimeError("sta join failed")
        sh("rm -f /tmp/roamcam_sta.ip")
        # -s /bin/true deed NIETS -- dat script is juist verantwoordelijk voor het echt
        # toepassen van het IP op de interface. udhcpc kreeg keurig een lease van de router
        # (vandaar zichtbaar in de Deco-app), maar wlan0 zelf kreeg 'm nooit. Fix: een echt
        # bind-script dat "ip addr add ... dev wlan0" uitvoert op het "bound"-event.
        bind_script = "/tmp/roamcam_udhcpc_bind.sh"
        with open(bind_script, "w") as f:
            f.write("#!/bin/sh\n"
                    "[ \"$1\" = bound -o \"$1\" = renew ] || exit 0\n"
                    "ip addr flush dev wlan0\n"
                    "ip addr add $ip/${mask:-24} dev wlan0\n"
                    "[ -n \"$router\" ] && ip route replace default via $router dev wlan0\n")
        sh("chmod +x %s" % bind_script)
        sh("udhcpc -i wlan0 -n -q -s %s 2>/mnt/data/wifi_dhcp.log && "
           "ip -4 -o addr show wlan0 | awk '{print $4}' | cut -d/ -f1 > /tmp/roamcam_sta.ip" % bind_script)
        time.sleep(1)
        ip = _wifi_home_ip()
        if not ip:
            wifi_state["last_error"] = "verbonden maar geen IP gekregen (DHCP)"
            raise RuntimeError("dhcp failed")
        wifi_state["mode"] = "home"
        wifi_state["home_ip"] = ip
        wifi_state["last_error"] = ""
    except Exception as e:
        if not wifi_state.get("last_error"):
            wifi_state["last_error"] = str(e)
        sh("pkill -f roamcam_wpa.conf 2>/dev/null; pkill udhcpc 2>/dev/null")
        _wifi_ap_up()


def wifi_loop():
    # Alleen bij opstarten proberen; daarna passief bewaken of de thuisverbinding nog leeft.
    time.sleep(8)  # even wachten tot het systeem verder gebooot is
    if not wifi_state["tried_boot"]:
        wifi_state["tried_boot"] = True
        wifi_try_home_once()
    while True:
        try:
            if wifi_state["mode"] == "home":
                st = sh("wpa_cli -i wlan0 status 2>/dev/null")
                if "wpa_state=COMPLETED" not in st:
                    wifi_state["last_error"] = "thuisnetwerk kwijtgeraakt, terug naar eigen AP"
                    sh("pkill -f roamcam_wpa.conf 2>/dev/null; pkill udhcpc 2>/dev/null")
                    _wifi_ap_up()
        except Exception:
            pass
        time.sleep(15)


def wifi_set_ap_password(new_psk):
    # Verandert het AP-wachtwoord voor de huidige sessie. /etc staat op de RAM-overlay,
    # dus dit overleeft nog GEEN reboot (valt dan terug op het stock-wachtwoord) --
    # bewuste, gedocumenteerde beperking, geen bug.
    try:
        conf = read("/etc/hostapd.conf")
        if "wpa_passphrase=" in conf:
            conf = re.sub(r"wpa_passphrase=.*", "wpa_passphrase=" + new_psk, conf)
        else:
            conf += "\nwpa_passphrase=%s\n" % new_psk
        with open("/etc/hostapd.conf", "w") as f:
            f.write(conf)
        sh("systemctl restart hostapd 2>/dev/null")
        return True
    except Exception:
        return False


# ---- Simpele beveiliging (HTTP Basic Auth) -- uit tenzij je zelf een wachtwoord instelt ----
import hashlib, hmac, base64
SECURITY_DEFAULT = {"dash_pw_hash": ""}
security_cfg = dict(SECURITY_DEFAULT)


def hash_dash_password(pw):
    return hashlib.sha256(("roamcam:" + pw).encode()).hexdigest()


def load_security_settings():
    try:
        d = json.load(open(LED_SETTINGS_PATH))
        if isinstance(d.get("security"), dict) and "dash_pw_hash" in d["security"]:
            security_cfg["dash_pw_hash"] = d["security"]["dash_pw_hash"]
    except Exception:
        pass


def save_security_settings():
    try:
        d = {}
        try:
            d = json.load(open(LED_SETTINGS_PATH))
        except Exception:
            pass
        d["security"] = dict(security_cfg)
        with open(LED_SETTINGS_PATH, "w") as f:
            json.dump(d, f)
    except Exception:
        pass


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

    def _check_auth(self):
        if not security_cfg.get("dash_pw_hash"):
            return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            user_pw = base64.b64decode(hdr[6:]).decode()
            pw = user_pw.split(":", 1)[1] if ":" in user_pw else ""
        except Exception:
            return False
        return hmac.compare_digest(hash_dash_password(pw), security_cfg["dash_pw_hash"])

    def do_GET(self):
        if not self._check_auth():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Roamcam"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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
        if p == "/ui/get":
            return self._send(200, "application/json", json.dumps(ui_cfg))
        if p == "/ui/set":
            q = parse_qs(urlparse(self.path).query)
            if "lang" in q and q["lang"][0] in ("nl", "en"):
                ui_cfg["lang"] = q["lang"][0]
            if "units" in q and q["units"][0] in ("kmh", "mph"):
                ui_cfg["units"] = q["units"][0]
            save_ui_settings()
            return self._send(200, "application/json", json.dumps(ui_cfg))
        if p == "/lora/status":
            out = dict(lora_state)
            out["backend_wanted"] = lora_cfg.get("backend", "off")
            out["configured"] = bool(lora_cfg.get("deveui") and lora_cfg.get("appkey"))
            out["deveui"] = lora_cfg.get("deveui", "")
            out["available"] = gpiod is not None
            out["meshtastic_installed"] = meshtasticd_installed()
            return self._send(200, "application/json", json.dumps(out))
        if p == "/lora/mesh_test":
            if lora_cfg.get("backend") != "meshtastic" or meshtasticd_proc is None:
                return self._send(200, "application/json", json.dumps({"ok": False, "error": "meshtastic backend niet actief"}))
            try:
                meshtastic_send_text("Roamcam test " + time.strftime("%H:%M:%S"))
                lora_state["uplinks"] = lora_state.get("uplinks", 0) + 1
                lora_state["last_uplink"] = time.time()
                return self._send(200, "application/json", json.dumps({"ok": True}))
            except Exception as e:
                lora_state["last_error"] = str(e)
                return self._send(200, "application/json", json.dumps({"ok": False, "error": str(e)}))
        if p == "/lora/set":
            q = parse_qs(urlparse(self.path).query)
            if "backend" in q and q["backend"][0] in ("off", "ttn", "meshtastic"):
                lora_cfg["backend"] = q["backend"][0]
            if "deveui" in q:
                v = re.sub(r"[^0-9a-fA-F]", "", q["deveui"][0])
                if len(v) == 16:
                    lora_cfg["deveui"] = v.lower()
            if "appkey" in q:
                v = re.sub(r"[^0-9a-fA-F]", "", q["appkey"][0])
                if len(v) == 32:
                    lora_cfg["appkey"] = v.lower()
            save_lora_settings()
            return self._send(200, "application/json", json.dumps({"ok": True}))
        if p == "/wifi/status":
            out = dict(wifi_state)
            out["home_ssid"] = wifi_cfg.get("home_ssid", "")
            out["ap_ssid"] = read("/mnt/data/wifi.cfg").split(",")[0] if os.path.exists("/mnt/data/wifi.cfg") else ""
            out["dash_pw_set"] = bool(security_cfg.get("dash_pw_hash"))
            out["current_ip"] = sh("hostname -I").split()[0] if sh("hostname -I") else ""
            return self._send(200, "application/json", json.dumps(out))
        if p == "/wifi/set":
            # keep_blank_values=True -- anders negeert parse_qs lege waardes stilletjes,
            # en kun je een ingevuld veld nooit meer leegmaken (kostte ons eerder een lockout)
            q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            if "home_ssid" in q:
                wifi_cfg["home_ssid"] = q["home_ssid"][0][:64]
            if "home_psk" in q and q["home_psk"][0]:
                wifi_cfg["home_psk"] = q["home_psk"][0][:64]
            save_wifi_settings()
            if "ap_psk" in q and q["ap_psk"][0]:
                wifi_set_ap_password(q["ap_psk"][0])
            return self._send(200, "application/json", json.dumps({"ok": True}))
        if p == "/wifi/retry":
            threading.Thread(target=wifi_try_home_once, daemon=True).start()
            return self._send(200, "application/json", json.dumps({"ok": True}))
        if p == "/security/set":
            q = parse_qs(urlparse(self.path).query)
            if "password" in q:
                pw = q["password"][0]
                security_cfg["dash_pw_hash"] = hash_dash_password(pw) if pw else ""
                save_security_settings()
            return self._send(200, "application/json", json.dumps({"ok": True}))
        if p == "/rec/status":
            files = glob.glob(CLIPS_DIR + "/*.mp4")
            return self._send(200, "application/json", json.dumps({
                "on": rec_cfg.get("on", False), "running": recorder_running(),
                "standalone": rec_cfg.get("standalone", False), "err": rec_state.get("err", ""),
                "seg": rec_cfg["seg"], "cap_gb": rec_cfg["cap_gb"], "w": rec_cfg["w"],
                "h": rec_cfg["h"], "fps": rec_cfg["fps"], "gain": rec_cfg["gain"], "shutter": rec_cfg["shutter"],
                "gforce": rec_cfg.get("gforce", 2.0),
                "clips": len(files), "bytes": sum(os.path.getsize(f) for f in files if os.path.exists(f)),
                "locked": sum(1 for f in files if clip_locked(f)),
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
        if p == "/live/status":
            return self._send(200, "application/json", json.dumps({
                "enabled": live_state["enabled"], "running": recorder_running(),
                "hasFrame": os.path.exists(LIVE_PREVIEW_JPG)}))
        if p == "/live/toggle":
            q = parse_qs(urlparse(self.path).query)
            want_on = q.get("on", ["0"])[0] == "1"
            if want_on and not recorder_running():
                return self._send(200, "application/json", json.dumps({"ok": False, "enabled": False}))
            live_state["enabled"] = want_on
            if not want_on:
                try:
                    os.remove(LIVE_PREVIEW_JPG)
                except Exception:
                    pass
            return self._send(200, "application/json", json.dumps({"ok": True, "enabled": live_state["enabled"]}))
        if p == "/live_preview.jpg":
            if not live_state["enabled"]:
                return self._send(404, "text/plain", "preview off")
            try:
                with open(LIVE_PREVIEW_JPG, "rb") as f:
                    return self._send(200, "image/jpeg", f.read())
            except Exception:
                return self._send(404, "text/plain", "no frame yet")
        if p == "/rec/hivemapper":
            restore_hivemapper()
            return self._send(200, "application/json", json.dumps({"ok": True, "standalone": rec_cfg.get("standalone")}))
        if p == "/rec/set":
            q = parse_qs(urlparse(self.path).query)
            for k, cast in (("seg", int), ("cap_gb", int), ("w", int), ("h", int), ("fps", int),
                            ("gain", int), ("shutter", int), ("gforce", float)):
                if k in q:
                    try:
                        rec_cfg[k] = cast(q[k][0])
                    except Exception:
                        pass
            save_rec_settings()
            return self._send(200, "application/json", json.dumps(rec_cfg))
        if p in ("/clip_lock", "/clip_unlock"):
            q = parse_qs(urlparse(self.path).query)
            name = q.get("name", [""])[0]
            if name and "/" not in name and ".." not in name and name.endswith(".mp4"):
                fp = os.path.join(CLIPS_DIR, name)
                if p == "/clip_lock":
                    ok = lock_clip(fp, "manual")
                else:
                    try:
                        os.remove(fp[:-4] + ".lock")
                        ok = True
                    except Exception:
                        ok = False
                return self._send(200, "application/json", json.dumps({"ok": ok}))
            return self._send(400, "application/json", json.dumps({"ok": False}))
        if p == "/clips.json":
            out = []
            for f in sorted(glob.glob(CLIPS_DIR + "/*.mp4"), key=lambda x: os.path.getmtime(x), reverse=True):
                try:
                    out.append({"name": os.path.basename(f), "size": os.path.getsize(f),
                                "mtime": int(os.path.getmtime(f) * 1000), "locked": clip_locked(f)})
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
        if p == "/clipvtt":
            q = parse_qs(urlparse(self.path).query)
            name = q.get("name", [""])[0]
            if name and "/" not in name and ".." not in name:
                base = name[:-4] if name.endswith(".mp4") else name
                srt = os.path.join(CLIPS_DIR, base + ".srt")
                try:
                    body = open(srt).read()
                    vtt = "WEBVTT\n\n" + re.sub(r"(\d\d:\d\d:\d\d),(\d\d\d)", r"\1.\2", body)
                    return self._send(200, "text/vtt", vtt)
                except Exception:
                    pass
            return self._send(404, "text/plain", "no subtitles")
        if p == "/clips/delete_all":
            q = parse_qs(urlparse(self.path).query)
            only = q.get("filter", ["all"])[0]  # all | unlocked
            removed = 0
            for f in glob.glob(CLIPS_DIR + "/*.mp4"):
                if clip_locked(f):
                    continue
                try:
                    os.remove(f)
                    for ext in (".srt", ".lock"):
                        fp = f[:-4] + ext
                        if os.path.exists(fp):
                            os.remove(fp)
                    removed += 1
                except Exception:
                    pass
            return self._send(200, "application/json", json.dumps({"ok": True, "removed": removed}))
        if p == "/clips/delete_many":
            q = parse_qs(urlparse(self.path).query)
            names = q.get("names", [""])[0].split(",")
            removed = 0
            for name in names:
                if name and "/" not in name and ".." not in name and name.endswith(".mp4"):
                    fp = os.path.join(CLIPS_DIR, name)
                    if clip_locked(fp):
                        continue
                    try:
                        os.remove(fp)
                        for ext in (".srt", ".lock"):
                            fp2 = fp[:-4] + ext
                            if os.path.exists(fp2):
                                os.remove(fp2)
                        removed += 1
                    except Exception:
                        pass
            return self._send(200, "application/json", json.dumps({"ok": True, "removed": removed}))
        if p == "/clip_del":
            q = parse_qs(urlparse(self.path).query)
            name = q.get("name", [""])[0]
            if name and "/" not in name and ".." not in name:
                ok = False
                for ext in (".mp4", ".srt", ".lock"):
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
.frameoverlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(8,11,17,.75);color:var(--mut);font-size:13px;text-align:center;padding:20px}
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
.clipbtn.lockon{background:#3b2f0c;border-color:#5c4a12}
.cliprow.lockedrow{background:linear-gradient(90deg,rgba(210,153,34,.10),transparent 60%);border-left:3px solid var(--warn)}
.cliptoolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--bd)}
.segbtns{display:flex;gap:4px;background:var(--card2);padding:3px;border-radius:8px}
.segbtn{background:transparent;border:0;color:var(--mut);padding:5px 10px;border-radius:6px;cursor:pointer;font-size:12px}
.segbtn.on{background:#0d3a63;color:#9fd0ff}
.selall{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--mut);cursor:pointer}
.cliprow .selchk{margin-right:2px}
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
  <div class="hstat"><b id="clock">–</b><span data-t="hDeviceTime">device tijd</span></div>
  <div class="hstat" style="margin-left:auto"><b id="conn"><span class="dot ok"></span>live</b><span>poll 1.5s</span></div>
</header>
<nav class="tabbar">
  <button class="tabbtn on" data-tab="live" data-t="tabLive">Live</button>
  <button class="tabbtn" data-tab="terug" data-t="tabPlayback">Terugkijken</button>
  <button class="tabbtn" data-tab="settings" data-t="tabSettings">Instellingen</button>
  <button class="tabbtn" data-tab="sys" data-t="tabSystem">Systeem</button>
</nav>
<div class="grid">

  <div class="card span2" id="imuCard" data-tab="live"><h2><span data-t="cImu">Live IMU / G-sensor</span>
      <span><span id="imuRate" class="muted" style="font-weight:400">– Hz</span> &nbsp;<span id="shockPill" class="pill g">–</span></span></h2>
    <div class="body">
      <canvas id="scope" height="150" style="width:100%;height:150px;background:#080b11;border-radius:8px;display:block"></canvas>
      <div style="display:flex;gap:8px;font-size:11px;margin:6px 2px 12px" class="muted">
        <span style="color:#ff6b6b">■ X</span><span style="color:#51cf66">■ Y</span><span style="color:#4dabf7">■ Z</span>
        <span style="margin-left:auto" data-t="imuScope">accelerometer (g) · laatste ~1,5 s</span>
      </div>
      <div class="imugrid">
        <div class="acol">
          <div class="atitle" data-t="imuAccel">Versnelling (g)</div>
          <div class="arow"><span>X</span><div class="abar"><i id="axb"></i></div><b id="axv">–</b></div>
          <div class="arow"><span>Y</span><div class="abar"><i id="ayb"></i></div><b id="ayv">–</b></div>
          <div class="arow"><span>Z</span><div class="abar"><i id="azb"></i></div><b id="azv">–</b></div>
        </div>
        <div class="acol">
          <div class="atitle" data-t="imuGyro">Rotatie (°/s)</div>
          <div class="arow"><span>X</span><div class="abar g"><i id="gxb"></i></div><b id="gxv">–</b></div>
          <div class="arow"><span>Y</span><div class="abar g"><i id="gyb"></i></div><b id="gyv">–</b></div>
          <div class="arow"><span>Z</span><div class="abar g"><i id="gzb"></i></div><b id="gzv">–</b></div>
        </div>
        <div class="acol center">
          <div class="atitle" data-t="imuG">G-kracht</div>
          <div class="big" id="gforce">–</div>
          <div class="muted" style="font-size:11px"><span data-t="imuPeak">piek</span> <b id="gpeak" style="color:var(--warn)">–</b> · <span id="gpeakReset" style="cursor:pointer;color:var(--acc)" data-t="reset">reset</span></div>
          <div class="atitle" style="margin-top:12px" data-t="imuTilt">Stand</div>
          <div class="mono" id="tilt">–</div>
          <div class="muted" style="font-size:11px">chip <b id="imuTemp">–</b></div>
        </div>
      </div>
    </div>
  </div>

  <div class="card span2" data-tab="live"><h2><span data-t="cCamera">Live camera</span> <span id="frameInfo" class="muted"></span></h2>
    <div style="position:relative">
      <img class="frame" id="frame" alt="camera" src="/frame.jpg">
      <div id="frameOverlay" class="frameoverlay" style="display:none"></div>
    </div>
    <div style="display:flex;align-items:center;gap:10px;padding:10px 15px;border-top:1px solid var(--bd)">
      <button class="ledbtn" id="liveToggle" style="flex:none;padding:6px 14px" data-t="previewOff">Live preview: uit</button>
      <span class="muted" style="font-size:11px" id="liveNote" data-t="previewNote">Handmatig aan/uit — kost alleen CPU zolang je meekijkt.</span>
    </div>
    <div class="body kv">
      <div class="k" data-t="lastFrame">Laatste frame</div><div class="v mono" id="frameTs">–</div>
      <div class="k" data-t="framesBuf">Frames in buffer</div><div class="v" id="frameCount">–</div>
      <div class="k" data-t="resolution">Resolutie (config)</div><div class="v" id="res">–</div>
    </div>
  </div>

  <div class="card span2" data-tab="terug"><h2><span data-t="cClips">Video-opnames</span> <span id="clipCount" class="muted" style="font-weight:400"></span></h2><div class="body">
    <div id="clipPlayer" style="display:none;margin-bottom:12px">
      <video id="clipVideo" controls autoplay playsinline style="width:100%;max-height:52vh;background:#000;border-radius:8px;display:block"></video>
      <div class="muted" id="clipNow" style="font-size:11px;margin-top:4px"></div>
    </div>
    <div class="cliptoolbar">
      <div class="segbtns">
        <button class="segbtn on" id="filtAll" data-t="filterAll">Alles</button>
        <button class="segbtn" id="filtLocked" data-t="filterLocked">🔒 Vergrendeld</button>
        <button class="segbtn" id="filtUnlocked" data-t="filterUnlocked">Onvergrendeld</button>
      </div>
      <div style="flex:1"></div>
      <label class="selall"><input type="checkbox" id="selAll"> <span data-t="selectAll">selecteer alles</span></label>
      <button class="clipbtn del" id="btnDelSel" style="display:none" data-t="delSelected">wis selectie</button>
      <button class="clipbtn del" id="btnDelAll" data-t="delAll">wis alles</button>
    </div>
    <div id="clipGallery"></div>
    <div style="text-align:center;margin-top:10px"><button class="ledbtn" id="clipMore" style="display:none" data-t="showMore">Toon meer</button></div>
  </div></div>

  <div class="card" data-tab="settings"><h2><span data-t="cRecorder">Opname-recorder</span> <span id="recPill" class="pill b">–</span></h2><div class="body">
    <div style="display:flex;gap:8px;margin-bottom:12px">
      <button class="ledbtn" id="recStart" data-t="btnStart">Start opname</button>
      <button class="ledbtn" id="recStop" data-t="btnCamOff">Camera uit</button>
    </div>
    <div id="recErr" class="err-line" style="margin-bottom:8px;display:none"></div>
    <div class="kv">
      <div class="k" data-t="status">Status</div><div class="v" id="recStat">–</div>
      <div class="k">Clips</div><div class="v" id="recClips">–</div>
      <div class="k" data-t="segLen">Segmentduur</div><div class="v"><select class="ledsel" id="recSeg" style="width:auto"><option value="30">30 s</option><option value="60">1 min</option><option value="180">3 min</option><option value="300">5 min</option></select></div>
      <div class="k" data-t="storeLimit">Bewaarlimiet</div><div class="v"><select class="ledsel" id="recCap" style="width:auto"><option value="5">5 GB</option><option value="10">10 GB</option><option value="15">15 GB</option><option value="20">20 GB</option></select></div>
      <div class="k" data-t="incLock">Incident-lock</div><div class="v"><select class="ledsel" id="recG" style="width:auto">
        <option value="0" data-t="off">Uit</option><option value="1.5" data-t="sensHigh">Gevoelig (1.5 g)</option><option value="2" data-t="sensMed">Normaal (2.0 g)</option><option value="3" data-t="sensLow">Ongevoelig (3.0 g)</option></select></div>
    </div>
    <div class="muted" style="font-size:11px;margin-top:10px" data-t="recNote">Standalone dashcam-modus: neemt op naar /mnt/data/clips (1080p30, hardware-H.264), oudste clips worden gewist boven de limiet, GPS + beweging meegelogd. "Camera uit" stopt de opname maar blijft standalone. Overleeft een reboot — werkt in de auto vanzelf zodra hij stroom krijgt.</div>
    <div class="muted" style="font-size:11px;margin-top:6px" data-t="lockNote">Incident-lock: bij een klap of noodstop boven de drempel wordt de clip beschermd 🔒 en nooit automatisch gewist.</div>
    <div style="margin-top:6px"><a id="recHive" style="font-size:11px;color:var(--mut);cursor:pointer" data-t="restoreHive">↩ Hivemapper-camera herstellen</a></div>
  </div></div>

  <div class="card" data-tab="settings"><h2 data-t="cPrefs">Voorkeuren</h2><div class="body">
    <div class="ledrow"><label data-t="language">Taal</label><select class="ledsel" id="uiLang">
      <option value="nl">Nederlands</option><option value="en">English</option></select></div>
    <div class="ledrow"><label data-t="units">Eenheden</label><select class="ledsel" id="uiUnits">
      <option value="kmh" data-t="unitMetric">km/u · meter · °C</option><option value="mph" data-t="unitImperial">mph · feet · °F</option></select></div>
    <div class="muted" style="font-size:11px;margin-top:8px" data-t="prefsNote">Geldt voor het dashboard én de GPS-overlay in nieuwe opnames. Wordt bewaard op het toestel.</div>
  </div></div>

  <div class="card" data-tab="settings"><h2><span data-t="cLed">LED-bediening</span> <span id="ledModeState" class="pill b">–</span></h2><div class="body">
    <div style="display:flex;gap:8px;margin-bottom:14px">
      <button class="ledbtn" id="ledDashcam" data-t="ledStock">Dashcam-status</button>
      <button class="ledbtn" id="ledSelf" data-t="ledSelf">Zelf instellen</button>
    </div>
    <div class="ledrow"><label data-t="ledTop">LED boven</label><span class="ledswatch" id="sw0"></span><select class="ledsel" id="ledfn0"></select></div>
    <div class="ledrow"><label data-t="ledMid">LED midden</label><span class="ledswatch" id="sw1"></span><select class="ledsel" id="ledfn1"></select></div>
    <div class="ledrow"><label data-t="ledBot">LED onder</label><span class="ledswatch" id="sw2"></span><select class="ledsel" id="ledfn2"></select></div>
    <div class="muted" style="font-size:11px;margin-top:8px" data-t="ledNote">Bij "zelf instellen" stuurt het dashboard de LEDs aan (blijft stabiel, ook na een API-herstart). GPS-fix / beweging / temp / snelheid updaten live mee.</div>
  </div></div>

  <div class="card" data-tab="settings"><h2><span data-t="cLora">LoRa (experimenteel)</span> <span id="loraPill" class="pill b">–</span></h2><div class="body">
    <div class="ledrow"><label data-t="loraBackend">Netwerk</label><select class="ledsel" id="loraBackend">
      <option value="off" data-t="off">Uit</option>
      <option value="ttn">The Things Network</option>
      <option value="meshtastic">Meshtastic</option>
    </select></div>
    <div id="loraTtnFields">
      <div class="kv" style="margin-top:8px">
        <div class="k">DevEUI</div><div class="v"><input class="mono" id="loraDevEui" placeholder="70b3d57ed00788c9" style="width:100%;background:transparent;color:inherit;border:1px solid var(--bd);border-radius:6px;padding:4px 6px;font-size:12px"></div>
        <div class="k">AppKey</div><div class="v"><input class="mono" id="loraAppKey" placeholder="32 hex tekens" style="width:100%;background:transparent;color:inherit;border:1px solid var(--bd);border-radius:6px;padding:4px 6px;font-size:12px"></div>
      </div>
      <div class="muted" style="font-size:11px;margin-top:8px" data-t="loraNote">Stuurt een klein GPS-positiebakentje via The Things Network (LoRaWAN OTAA). Vereist een gratis account op console.cloud.thethings.network. DevEUI/AppKey haal je daar op.</div>
    </div>
    <div id="loraMeshFields" style="display:none">
      <div class="muted" style="font-size:11px;margin-top:8px" data-t="loraMeshNote">Draait meshtasticd op deze camera als een mesh-node. Verbind de gratis Meshtastic-app (Android/iOS) via "TCP" met dit toestel op poort 4403. Vereist dat meshtasticd handmatig op het toestel is geïnstalleerd — zie docs/HOWTO.md.</div>
      <div class="err-line" id="loraMeshMissing" style="display:none;margin-top:6px" data-t="loraMeshMissing">meshtasticd niet gevonden op dit toestel — nog niet (handmatig) geïnstalleerd.</div>
      <button class="ledbtn" id="loraMeshTest" style="margin-top:8px" data-t="loraMeshTestBtn">Stuur testbericht nu</button>
    </div>
    <div class="kv" style="margin-top:8px">
      <div class="k" data-t="status">Status</div><div class="v" id="loraStatus">–</div>
      <div class="k" data-t="loraLastUplink">Laatste bericht</div><div class="v" id="loraLastUplink">–</div>
      <div class="k" data-t="loraAttempts">Pogingen</div><div class="v" id="loraAttempts">–</div>
      <div class="k" data-t="loraLastError">Laatste fout</div><div class="v" id="loraLastError">–</div>
    </div>
    <button class="ledbtn" id="loraSave" style="margin-top:10px" data-t="loraSaveBtn">Opslaan</button>
    <div class="muted" style="font-size:11px;margin-top:8px" data-t="loraGenNote">Experimenteel. Pauzeert de stock LoRa/Helium-service zolang een van beide aan staat — komt vanzelf terug zodra je "Uit" kiest.</div>
  </div></div>

  <div class="card" data-tab="settings"><h2><span data-t="cWifi">Thuisnetwerk</span> <span id="wifiPill" class="pill b">–</span></h2><div class="body">
    <div class="kv">
      <div class="k" data-t="wifiHomeSsid">Thuis-wifi naam</div><div class="v"><input class="mono" id="wifiHomeSsid" style="width:100%;background:transparent;color:inherit;border:1px solid var(--bd);border-radius:6px;padding:4px 6px;font-size:12px"></div>
      <div class="k" data-t="wifiHomePsk">Thuis-wifi wachtwoord</div><div class="v"><input type="password" class="mono" id="wifiHomePsk" style="width:100%;background:transparent;color:inherit;border:1px solid var(--bd);border-radius:6px;padding:4px 6px;font-size:12px"></div>
      <div class="k" data-t="status">Status</div><div class="v" id="wifiStatus">–</div>
      <div class="k" data-t="wifiApPsk">Nieuw AP-wachtwoord</div><div class="v"><input type="password" class="mono" id="wifiApPsk" placeholder="leeg = niet wijzigen" style="width:100%;background:transparent;color:inherit;border:1px solid var(--bd);border-radius:6px;padding:4px 6px;font-size:12px"></div>
    </div>
    <div style="display:flex;gap:8px;margin-top:10px">
      <button class="ledbtn" id="wifiSave" data-t="loraSaveBtn">Opslaan</button>
      <button class="ledbtn" id="wifiRetry" data-t="wifiRetryBtn">Nu proberen</button>
    </div>
    <div class="muted" style="font-size:11px;margin-top:8px" data-t="wifiNote">Probeert alleen bij het opstarten thuis-wifi te pakken (niet doorlopend, anders valt de eigen AP telkens weg). Lukt dat, dan blijft hij daarop tot je uit bereik rijdt — dan valt hij vanzelf terug op de eigen AP. AP-wachtwoord wijzigen breekt je huidige AP-verbinding meteen als je daarop zit, en overleeft nog geen reboot.</div>
  </div></div>

  <div class="card" data-tab="settings"><h2 data-t="cSecurity">Dashboard-wachtwoord</h2><div class="body">
    <div class="kv">
      <div class="k" data-t="wifiDashPw">Nieuw wachtwoord</div><div class="v"><input type="password" class="mono" id="secPw" placeholder="leeg = geen wachtwoord" style="width:100%;background:transparent;color:inherit;border:1px solid var(--bd);border-radius:6px;padding:4px 6px;font-size:12px"></div>
    </div>
    <button class="ledbtn" id="secSave" style="margin-top:10px" data-t="loraSaveBtn">Opslaan</button>
    <div class="muted" style="font-size:11px;margin-top:8px" data-t="secNote">Aan te raden zodra je "Thuisnetwerk" gebruikt — dan is het dashboard ineens bereikbaar voor iedereen op dat netwerk, niet alleen wie het AP-wachtwoord kent.</div>
  </div></div>

  <div class="card" data-tab="live"><h2>GPS / GNSS <span id="fixPill" class="pill b">–</span></h2><div class="body">
    <div class="kv">
      <div class="k">Latitude</div><div class="v mono" id="lat">–</div>
      <div class="k">Longitude</div><div class="v mono" id="lon">–</div>
      <div class="k" data-t="sats">Satellieten</div><div class="v" id="sats">–</div>
      <div class="k" data-t="speed">Snelheid</div><div class="v" id="speed">–</div>
      <div class="k" data-t="heading">Koers</div><div class="v" id="heading">–</div>
      <div class="k" data-t="altitude">Hoogte</div><div class="v" id="alt">–</div>
      <div class="k" data-t="accuracy">Nauwkeurigh. (H/V)</div><div class="v" id="acc">–</div>
      <div class="k">TTFF</div><div class="v" id="ttff">–</div>
    </div>
    <div style="margin:10px 0 6px" class="muted" id="mapLink"></div>
    <div class="dop" id="dop"></div>
    <details><summary data-t="rawNmea">Ruwe NMEA (GGA)</summary><pre id="gga">–</pre></details>
  </div></div>

  <div class="card" data-tab="live"><h2 data-t="cRf">GNSS RF / anti-jamming</h2><div class="body kv">
    <div class="k" data-t="jamState">Jamming-status</div><div class="v" id="jam">–</div>
    <div class="k" data-t="jamInd">Jam-indicator</div><div class="v" id="jamind">–</div>
    <div class="k" data-t="antenna">Antenne</div><div class="v" id="ant">–</div>
    <div class="k" data-t="antPower">Antenne-voeding</div><div class="v" id="antpwr">–</div>
    <div class="k" data-t="noise">Ruis / ms</div><div class="v" id="noise">–</div>
    <div class="k">AGC</div><div class="v" id="agc">–</div>
    <div class="k">C/No</div><div class="v" id="cno">–</div>
    <div class="k" data-t="spoof">Spoof-detectie</div><div class="v" id="spoof">–</div>
  </div></div>

  <div class="card" data-tab="sys"><h2 data-t="cSystem">Systeem</h2><div class="body">
    <div><div class="k muted"><span data-t="cpuUsage">CPU-gebruik</span> <span style="float:right" id="cpuAll">–</span></div><div class="bar" id="cpuAllWrap"><i id="cpuAllBar"></i></div></div>
    <div id="cores" style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:8px 0 4px"></div>
    <div class="kv" style="margin-top:8px">
      <div class="k" data-t="cpuTemp">CPU-temp</div><div class="v" id="temp">–</div>
      <div class="k" data-t="cpuClock">CPU-klok / gov.</div><div class="v" id="mhz">–</div>
      <div class="k">Load (1/5/15m)</div><div class="v mono" id="load">–</div>
      <div class="k" data-t="coresProcs">Cores / processen</div><div class="v" id="cpuproc">–</div>
    </div>
    <div style="margin-top:10px"><div class="k muted"><span data-t="memory">Geheugen</span> <span id="memTxt" style="float:right"></span></div><div class="bar" id="memBarWrap"><i id="memBar"></i></div></div>
    <div style="margin-top:8px"><div class="k muted">Swap <span id="swapTxt" style="float:right"></span></div><div class="bar"><i id="swapBar"></i></div></div>
  </div></div>

  <div class="card" data-tab="sys"><h2 data-t="cStorage">Opslag</h2><div class="body" id="disks"></div></div>

  <div class="card" data-tab="sys"><h2 data-t="cServices">Services</h2><div class="body"><div class="svcgrid" id="svc"></div></div></div>

  <div class="card" data-tab="sys"><h2 data-t="cLeds">Status-LEDs</h2><div class="body">
    <div class="leds" id="leds"></div>
    <div class="kv" style="margin-top:12px">
      <div class="k" data-t="sessionId">Sessie-ID</div><div class="v mono" id="session">–</div>
      <div class="k" data-t="serialNo">Serienr.</div><div class="v mono" id="serial">–</div>
      <div class="k">Board</div><div class="v mono" id="board">–</div>
      <div class="k">Device-ID</div><div class="v mono" id="devid">–</div>
      <div class="k">SSID</div><div class="v mono" id="ssid">–</div>
    </div>
  </div></div>

  <div class="card" data-tab="sys"><h2 data-t="cNetwork">Netwerk</h2><div class="body">
    <div class="kv"><div class="k" data-t="ipAddr">IP-adres(sen)</div><div class="v mono" id="ips">–</div>
    <div class="k" data-t="wifiMode">WiFi-modus</div><div class="v" id="wifimode">–</div></div>
    <div id="nets" style="margin-top:8px"></div>
  </div></div>

  <div class="card" data-tab="sys"><h2 data-t="cData">Data & opnames</h2><div class="body kv" id="datacounts">
    <div class="k">FrameKM buffer</div><div class="v" id="fkm">–</div>
    <div class="k" data-t="gpsLogs">GPS-logs</div><div class="v" id="gpslogs">–</div>
    <div class="k" data-t="imuLogs">IMU-logs</div><div class="v" id="imulogs">–</div>
  </div><div class="body" style="border-top:1px solid var(--bd)" id="dbs"></div></div>

  <div class="card" data-tab="sys"><h2 data-t="cFirmware">Firmware-slots (RAUC)</h2><div class="body"><pre id="rauc">–</pre></div></div>

  <div class="card span2" data-tab="sys"><h2 data-t="cConfig">Volledige configuratie</h2><div class="body">
    <details open><summary id="cfgSummary">–</summary><pre id="config">–</pre></details>
  </div></div>

</div>
<div class="foot" id="foot" data-t="foot">Hivemapper HDC lokaal dashboard &middot; data van API :5000 + systeem</div>
</div>
<script>
const $=id=>document.getElementById(id);
// ---- i18n + eenheden ----
const I18N={nl:{},en:{
 hDeviceTime:'device time',tabLive:'Live',tabPlayback:'Playback',tabSettings:'Settings',tabSystem:'System',
 cImu:'Live IMU / G-sensor',imuScope:'accelerometer (g) · last ~1.5 s',imuAccel:'Acceleration (g)',imuGyro:'Rotation (°/s)',
 imuG:'G-force',imuPeak:'peak',reset:'reset',imuTilt:'Attitude',calm:'calm',moving:'MOTION!',
 cCamera:'Live camera',lastFrame:'Last frame',framesBuf:'Frames buffered',resolution:'Resolution (config)',
 cClips:'Recordings',cRecorder:'Recorder',btnStart:'Start recording',btnCamOff:'Camera off',
 status:'Status',segLen:'Segment length',storeLimit:'Storage limit',incLock:'Incident lock',
 off:'Off',sensHigh:'Sensitive (1.5 g)',sensMed:'Normal (2.0 g)',sensLow:'Low (3.0 g)',
 recNote:'Standalone dashcam mode: records to /mnt/data/clips (1080p30, hardware H.264), oldest clips are deleted past the limit, GPS + motion logged alongside. "Camera off" stops recording but stays standalone. Survives a reboot — in a car it just runs whenever it has power.',
 lockNote:'Incident lock: on an impact or hard stop above the threshold the clip is protected 🔒 and never auto-deleted.',
 restoreHive:'↩ Restore Hivemapper camera',
 cPrefs:'Preferences',language:'Language',units:'Units',prefsNote:'Applies to the dashboard and to the GPS overlay in new recordings. Stored on the device.',
 cLed:'LED control',ledStock:'Dashcam status',ledSelf:'Custom',ledTop:'Top LED',ledMid:'Middle LED',ledBot:'Bottom LED',
 ledNote:'With "Custom" the dashboard drives the LEDs (stays stable, even after an API restart). GPS fix / motion / temp / speed update live.',
 sats:'Satellites',speed:'Speed',heading:'Heading',altitude:'Altitude',accuracy:'Accuracy (H/V)',rawNmea:'Raw NMEA (GGA)',
 cRf:'GNSS RF / anti-jamming',jamState:'Jamming state',jamInd:'Jam indicator',antenna:'Antenna',antPower:'Antenna power',noise:'Noise / ms',spoof:'Spoof detection',
 cSystem:'System',cpuUsage:'CPU usage',cpuTemp:'CPU temp',cpuClock:'CPU clock / gov.',coresProcs:'Cores / processes',memory:'Memory',
 cStorage:'Storage',cServices:'Services',cLeds:'Status LEDs',sessionId:'Session ID',serialNo:'Serial no.',
 cNetwork:'Network',ipAddr:'IP address(es)',wifiMode:'WiFi mode',cData:'Data & recordings',gpsLogs:'GPS logs',imuLogs:'IMU logs',
 cFirmware:'Firmware slots (RAUC)',cConfig:'Full configuration',allSettings:'All settings',
 foot:'Hivemapper HDC local dashboard · data from API :5000 + system',
 noFix:'no fix',searching:'searching',recRunning:'recording',camOff:'camera off',stock:'Hivemapper',
 standaloneIdle:'standalone, not recording',stockActive:'Hivemapper active',recordingAt:'recording',
 clips:'clips',noClips:'no recordings yet — start recording in Settings',
 play:'play',download:'download',del:'delete',lock:'lock',unlock:'unlock',locked:'protected',
 confirmDel:'Delete this clip?',confirmHive:'Restore the Hivemapper camera (leave standalone)?',
 freeing:'freeing camera…',stopping:'stopping…',restoring:'restoring…',
 noPos:'no position fix (indoors / poor sky view)',showMap:'Show on map',ofWhich:'used',visible:'visible',
 subsOn:'GPS overlay on (CC button toggles)',none:'none',
 healthy:'healthy',problem:'problem',unitMetric:'km/h · metres · °C',unitImperial:'mph · feet · °F',
 filterAll:'All',filterLocked:'🔒 Locked',filterUnlocked:'Unlocked',selectAll:'select all',
 delSelected:'delete selected',delAll:'delete all',showMore:'Show more',
 confirmDelAll:'Delete all unlocked clips? Locked clips are kept.',confirmDelSel:'Delete the selected clips?',
 noneMatch:'no recordings match this filter',deleted:'deleted',
 previewOff:'Live preview: off',previewOn:'Live preview: on',
 previewNote:'Manual on/off — only uses CPU while watching.',
 previewLoading:'Starting preview…',previewCamOff:'Camera is off — nothing to preview',
 previewStock:'Hivemapper is active — this shows its own captured frames',
 cLora:'LoRa (experimental)',loraBackend:'Network',loraSaveBtn:'Save',ago:'ago',
 loraMeshTestBtn:'Send test message now',loraAttempts:'Attempts',loraLastError:'Last error',
 loraLastUplink:'Last message',
 loraNote:'Sends a small GPS position beacon over The Things Network (LoRaWAN OTAA). Needs a free account at console.cloud.thethings.network — get the DevEUI/AppKey there.',
 loraMeshNote:'Runs meshtasticd on this camera as a mesh node. Connect the free Meshtastic app (Android/iOS) via "TCP" to this device on port 4403. Needs meshtasticd manually installed on the device first — see docs/HOWTO.md.',
 loraMeshMissing:'meshtasticd not found on this device — not (manually) installed yet.',
 loraGenNote:'Experimental. Pauses the stock LoRa/Helium service while either of these is on — comes back automatically once you pick "Off".',
 cWifi:'Home network',wifiHomeSsid:'Home Wi-Fi name',wifiHomePsk:'Home Wi-Fi password',wifiApPsk:'New AP password',
 wifiRetryBtn:'Try now',wifiOnHome:'on home network',wifiOnAp:'on own AP',
 wifiNote:'Only tries the home network at startup (not continuously, or the AP would keep flickering off for anyone on it directly). If it works, it stays there until you drive out of range — then falls back to the AP automatically. Changing the AP password disconnects you immediately if you’re on it, and doesn’t survive a reboot yet.',
 cSecurity:'Dashboard password',wifiDashPw:'New password',
 secNote:'Worth setting once you use "Home network" — the dashboard suddenly becomes reachable by anyone on that network, not just people who know the AP password.',
 secSaved:'Saved. The browser will ask you to log in on the next visit if a password is set.'
}};
let LANG='nl',UNITS='kmh';
const T=k=>(LANG==='en'&&I18N.en[k])?I18N.en[k]:null;
function applyLang(){document.querySelectorAll('[data-t]').forEach(el=>{const v=T(el.dataset.t);if(v!==null)el.textContent=v;else if(el.dataset.orig!==undefined)el.textContent=el.dataset.orig;});
  document.documentElement.lang=LANG;}
function snapOrig(){document.querySelectorAll('[data-t]').forEach(el=>{if(el.dataset.orig===undefined)el.dataset.orig=el.textContent;});}
const tt=(k,nl)=>{const v=T(k);return v!==null?v:nl;};
// eenheden
const spd=ms=>UNITS==='mph'?{v:ms*2.23694,u:'mph'}:{v:ms*3.6,u:'km/u'};
const dist=m=>UNITS==='mph'?{v:m*3.28084,u:'ft'}:{v:m,u:'m'};
const tempU=c=>UNITS==='mph'?{v:c*9/5+32,u:'°F'}:{v:c,u:'°C'};
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
    $('healthDot').innerHTML='<span class="dot '+(healthy?'ok':'err')+'"></span>'+(healthy?tt('healthy','gezond'):tt('problem','probleem'));
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
    const fp=$('fixPill');fp.textContent=fix==='none'?tt('noFix','geen fix'):fix;
    fp.className='pill '+(fix==='3D'?'g':fix==='2D'?'y':'r');
    $('lat').textContent=gps.latitude!=null?gps.latitude.toFixed(7):'–';
    $('lon').textContent=gps.longitude!=null?gps.longitude.toFixed(7):'–';
    if(gps.satellites)$('sats').textContent=(gps.satellites.used??'?')+' '+tt('ofWhich','gebruikt')+' / '+(gps.satellites.seen??'?')+' '+tt('visible','zichtbaar');
    if(gps.speed!=null){const s=spd(gps.speed);$('speed').textContent=s.v.toFixed(1)+' '+s.u;}else $('speed').textContent='–';
    $('heading').textContent=gps.heading!=null?gps.heading.toFixed(0)+'°':'–';
    if(gps.height!=null){const a=dist(gps.height);$('alt').textContent=a.v.toFixed(0)+' '+a.u;}else $('alt').textContent='–';
    const ah=gps.horizontal_accuracy!=null?dist(gps.horizontal_accuracy):null,av=gps.vertical_accuracy!=null?dist(gps.vertical_accuracy):null;
    $('acc').textContent=(ah?ah.v.toFixed(1):'?')+' / '+(av?av.v.toFixed(1):'?')+' '+(ah?ah.u:'m');
    $('ttff').textContent=(gps.ttff&&gps.ttff<1e12)?(gps.ttff/1000).toFixed(0)+' s':tt('none','geen');
    if(gps.latitude&&gps.longitude&&fix!=='none')
      $('mapLink').innerHTML='📍 <a href="https://www.openstreetmap.org/?mlat='+gps.latitude+'&mlon='+gps.longitude+'#map=17/'+gps.latitude+'/'+gps.longitude+'" target="_blank">'+tt('showMap','Toon op kaart')+'</a>';
    else $('mapLink').innerHTML='<span class="muted">'+tt('noPos','geen positie-fix (binnen / weinig zicht)')+'</span>';
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

// ---- Live preview (standalone-opname): handmatig, standaard uit ----
let PREVIEW_ON=false, RECORDER_RUNNING=false, RECORDER_STANDALONE=false;
function updateLiveToggleUI(){const b=$('liveToggle');
  b.textContent=tt(PREVIEW_ON?'previewOn':'previewOff',PREVIEW_ON?'Live preview: aan':'Live preview: uit');
  b.className='ledbtn'+(PREVIEW_ON?' on':'');}
async function setPreview(on){if(on&&!RECORDER_RUNNING)return;PREVIEW_ON=on;updateLiveToggleUI();
  try{await fetch('/live/toggle?on='+(on?1:0));}catch(e){}}
$('liveToggle').onclick=()=>setPreview(!PREVIEW_ON);
// bij elke paginalaad staat preview server-side altijd uit; forceer dat ook lokaal (nooit "aan blijven staan")
setPreview(false);

async function tickFrame(){
  const img=$('frame'),ov=$('frameOverlay');
  if(RECORDER_RUNNING){
    $('liveToggle').style.display='';$('liveNote').style.display='';
    if(PREVIEW_ON){
      ov.style.display='none';
      img.src='/live_preview.jpg?t='+Date.now();
    } else {
      ov.style.display='flex';ov.textContent='';
    }
  } else if(RECORDER_STANDALONE){
    $('liveToggle').style.display='none';$('liveNote').style.display='none';
    ov.style.display='flex';ov.textContent=tt('previewCamOff','Camera staat uit — niets om te tonen');
  } else {
    $('liveToggle').style.display='none';$('liveNote').style.display='none';
    ov.style.display='none';
    img.src='/frame.jpg?t='+Date.now();
  }
}

async function tickSys(){
  try{
    const s=await jget('/sys.json');
    $('uptime').textContent=fmtDur(s.uptime_s);
    if(s.temp_c!=null){const t=tempU(s.temp_c);$('temp').textContent=$('temp2').textContent=t.v.toFixed(1)+' '+t.u;}else $('temp').textContent=$('temp2').textContent='–';
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
    $('cfgSummary').textContent=tt('allSettings','Alle instellingen')+' ('+Object.keys(cfg).length+')';
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
    if(d.temp!=null){const t=tempU(d.temp);$('imuTemp').textContent=t.v.toFixed(1)+' '+t.u;}else $('imuTemp').textContent='–';
    // schok-detectie: afwijking van 1g of hoge rotatie
    const dev=Math.abs(d.peak_g-1);
    const shock=dev>0.35||d.peak_gyro>60;
    const sp=$('shockPill');
    if(shock){sp.textContent=tt('moving','BEWEGING!');sp.className='pill r';$('imuCard').classList.remove('shock');void $('imuCard').offsetWidth;$('imuCard').classList.add('shock');}
    else{sp.textContent=tt('calm','rustig');sp.className='pill g';}
    drawScope(d.hist);
  }catch(e){}
}

// ---- LED-bediening ----
const LED_NL=[['uit','Uit'],['opname','Opname (ademend rood)'],['gps','GPS-fix (groen=3D)'],['beweging','Beweging / schok'],['temp','CPU-temp'],['snelheid','Snelheid'],['lora','LoRa-status (groen=verbonden)'],['rood','Vaste kleur: rood'],['groen','Vaste kleur: groen'],['blauw','Vaste kleur: blauw'],['geel','Vaste kleur: geel'],['wit','Vaste kleur: wit'],['paars','Vaste kleur: paars']];
const LED_EN=[['uit','Off'],['opname','Recording (breathing red)'],['gps','GPS fix (green=3D)'],['beweging','Motion / shock'],['temp','CPU temp'],['snelheid','Speed'],['lora','LoRa status (green=joined)'],['rood','Fixed colour: red'],['groen','Fixed colour: green'],['blauw','Fixed colour: blue'],['geel','Fixed colour: yellow'],['wit','Fixed colour: white'],['paars','Fixed colour: purple']];
const LED_FNS=LED_NL;
const LED_SW={uit:'#222',opname:'#ff4d4d',rood:'#ff4d4d',groen:'#3fb950',blauw:'#4dabf7',geel:'#f5d90a',wit:'#eee',paars:'#b197fc',gps:'#3fb950',beweging:'#ff4d4d',temp:'#f5d90a',snelheid:'#4dabf7'};
function fillLedSelects(){
  const FNS=LANG==='en'?LED_EN:LED_NL;
  for(let i=0;i<3;i++){const s=$('ledfn'+i);s.innerHTML=FNS.map(([v,t])=>'<option value="'+v+'">'+t+'</option>').join('');
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
function assignTabs(){document.querySelectorAll('.card').forEach(c=>{if(!c.dataset.tab)c.dataset.tab='live';});}
function showTab(tab){const leavingLive=(document.querySelector('.tabbtn.on')?.dataset.tab==='live')&&tab!=='live';
  document.querySelectorAll('.card').forEach(c=>c.classList.toggle('hidden',(c.dataset.tab||'live')!==tab));
  document.querySelectorAll('.tabbtn').forEach(b=>b.classList.toggle('on',b.dataset.tab===tab));
  if(leavingLive&&PREVIEW_ON)setPreview(false);
  if(tab==='terug'){loadClips();}
  if(tab==='settings'){loadRec();loadLedState();loadLora();loadWifi();}}
document.querySelectorAll('.tabbtn').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));

// ---- Recorder ----
async function loadRec(){try{const d=await jget('/rec/status');const run=d.running;
  RECORDER_RUNNING=run;RECORDER_STANDALONE=d.standalone;
  if(!run&&PREVIEW_ON)setPreview(false);
  $('recPill').textContent=run?tt('recRunning','opname loopt'):(d.standalone?tt('camOff','camera uit'):tt('stock','Hivemapper'));
  $('recPill').className='pill '+(run?'g':(d.standalone?'y':'b'));
  $('recStat').textContent=run?(tt('recordingAt','opnemen')+' '+d.w+'×'+d.h+' @'+d.fps):(d.standalone?tt('standaloneIdle','standalone, opname uit'):tt('stockActive','Hivemapper actief'));
  $('recClips').textContent=d.clips+' '+tt('clips','clips')+' · '+fmtBytes(d.bytes)+(d.locked?'  ·  🔒 '+d.locked:'');
  $('recStart').className='ledbtn'+(run?' on':'');$('recStop').className='ledbtn'+(!run&&d.standalone?' on':'');
  if(d.err){$('recErr').style.display='block';$('recErr').textContent='⚠ '+d.err;}else{$('recErr').style.display='none';}
  if($('recSeg').dataset.init!=='1'){$('recSeg').value=d.seg;$('recCap').value=d.cap_gb;$('recG').value=String(d.gforce??2);$('recSeg').dataset.init='1';}
}catch(e){}}
$('recStart').onclick=async()=>{$('recStat').textContent=tt('freeing','camera vrijmaken…');await fetch('/rec/start');setTimeout(loadRec,8000);};
$('recStop').onclick=async()=>{$('recStat').textContent=tt('stopping','opname stoppen…');await fetch('/rec/stop');setTimeout(loadRec,2500);};
$('recHive').onclick=async()=>{if(!confirm(tt('confirmHive','Hivemapper-camera herstellen (standalone verlaten)?')))return;$('recStat').textContent=tt('restoring','herstellen…');await fetch('/rec/hivemapper');setTimeout(loadRec,3000);};
$('recSeg').onchange=()=>fetch('/rec/set?seg='+$('recSeg').value);
$('recCap').onchange=()=>fetch('/rec/set?cap_gb='+$('recCap').value);
$('recG').onchange=()=>fetch('/rec/set?gforce='+$('recG').value);

// ---- LoRa (TTN / Meshtastic, experimenteel) ----
const LORA_STATUS_LABEL={off:['uit','off'],searching:['zoekt verbinding…','searching…'],joined:['verbonden','joined']};
function fmtAgo(ts){if(!ts)return'–';const s=Math.max(0,Math.floor(Date.now()/1000-ts));if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m';return Math.floor(s/3600)+'u'}
function loraShowFields(){const b=$('loraBackend').value;
  $('loraTtnFields').style.display=b==='ttn'?'':'none';
  $('loraMeshFields').style.display=b==='meshtastic'?'':'none';}
$('loraBackend').onchange=loraShowFields;
let LORA_LOADED_ONCE=false;
async function loadLora(){try{const d=await jget('/lora/status');
  const st=d.status||'off';const lbl=LORA_STATUS_LABEL[st]||[st,st];
  $('loraStatus').textContent=LANG==='en'?lbl[1]:lbl[0];
  $('loraPill').textContent=$('loraStatus').textContent;
  $('loraPill').className='pill '+(st==='joined'?'g':(st==='searching'?'y':'b'));
  $('loraLastUplink').textContent=d.last_uplink?(fmtAgo(d.last_uplink)+' '+tt('ago','geleden')+(d.devaddr?'  ·  '+d.devaddr:'')):'–';
  $('loraAttempts').textContent=(d.attempts||0)+(d.uplinks?' ('+d.uplinks+' verzonden)':'');
  $('loraLastError').textContent=d.last_error||'–';
  $('loraMeshMissing').style.display=(d.backend_wanted==='meshtastic'&&!d.meshtastic_installed)?'block':'none';
  if(!LORA_LOADED_ONCE){$('loraBackend').value=d.backend_wanted||'off';$('loraDevEui').value=d.deveui||'';loraShowFields();LORA_LOADED_ONCE=true;}
}catch(e){}}
$('loraSave').onclick=async()=>{
  const params=new URLSearchParams();
  params.set('backend',$('loraBackend').value);
  if($('loraDevEui').value.trim())params.set('deveui',$('loraDevEui').value.trim());
  if($('loraAppKey').value.trim())params.set('appkey',$('loraAppKey').value.trim());
  await fetch('/lora/set?'+params.toString());
  $('loraAppKey').value='';
  setTimeout(loadLora,500);
};
$('loraMeshTest').onclick=async()=>{
  $('loraMeshTest').disabled=true;
  try{await fetch('/lora/mesh_test');}catch(e){}
  setTimeout(()=>{$('loraMeshTest').disabled=false;loadLora();},800);
};
setInterval(()=>{if(document.querySelector('.tabbtn[data-tab="settings"]').classList.contains('on'))loadLora();},4000);

// ---- Thuisnetwerk + dashboard-wachtwoord ----
let WIFI_LOADED_ONCE=false;
async function loadWifi(){try{const d=await jget('/wifi/status');
  const home=d.mode==='home';
  $('wifiStatus').textContent=(home?tt('wifiOnHome','op thuisnetwerk'):tt('wifiOnAp','op eigen AP'))+' · IP '+(d.current_ip||'–')+(!home&&d.last_error?' — '+d.last_error:'');
  $('wifiPill').textContent=home?tt('wifiOnHome','thuis'):tt('wifiOnAp','AP');
  $('wifiPill').className='pill '+(home?'g':'b');
  if(!WIFI_LOADED_ONCE){$('wifiHomeSsid').value=d.home_ssid||'';WIFI_LOADED_ONCE=true;}
}catch(e){}}
$('wifiSave').onclick=async()=>{
  const params=new URLSearchParams();
  if($('wifiHomeSsid').value.trim())params.set('home_ssid',$('wifiHomeSsid').value.trim());
  if($('wifiHomePsk').value)params.set('home_psk',$('wifiHomePsk').value);
  if($('wifiApPsk').value)params.set('ap_psk',$('wifiApPsk').value);
  await fetch('/wifi/set?'+params.toString());
  $('wifiHomePsk').value='';$('wifiApPsk').value='';
  setTimeout(loadWifi,500);
};
$('wifiRetry').onclick=async()=>{$('wifiStatus').textContent=tt('freeing','bezig…');await fetch('/wifi/retry');setTimeout(loadWifi,3000);};
$('secSave').onclick=async()=>{await fetch('/security/set?password='+encodeURIComponent($('secPw').value));$('secPw').value='';alert(tt('secSaved','Opgeslagen. Bij het volgende bezoek vraagt de browser om in te loggen als er een wachtwoord is ingesteld.'));};
setInterval(()=>{if(document.querySelector('.tabbtn[data-tab="settings"]').classList.contains('on'))loadWifi();},5000);

// ---- Voorkeuren (taal + eenheden) ----
async function loadPrefs(){try{const d=await jget('/ui/get');LANG=d.lang||'en';UNITS=d.units||'kmh';
  $('uiLang').value=LANG;$('uiUnits').value=UNITS;applyLang();fillLedSelects();loadLedState();
}catch(e){}}
$('uiLang').onchange=async()=>{LANG=$('uiLang').value;applyLang();fillLedSelects();loadLedState();
  await fetch('/ui/set?lang='+LANG);loadRec();loadClips();tick();};
$('uiUnits').onchange=async()=>{UNITS=$('uiUnits').value;await fetch('/ui/set?units='+UNITS);tick();tickSys();};

// ---- Video-clips ----
function playClip(n,label){const v=$('clipVideo');v.pause();v.innerHTML='';v.removeAttribute('src');
  v.src='/clip?name='+encodeURIComponent(n);
  const tr=document.createElement('track');tr.kind='subtitles';tr.label='GPS';tr.srclang='nl';tr.default=true;tr.src='/clipvtt?name='+encodeURIComponent(n);v.appendChild(tr);
  const showSubs=()=>{try{if(v.textTracks&&v.textTracks[0])v.textTracks[0].mode='showing';}catch(e){}};
  v.addEventListener('loadeddata',showSubs,{once:true});
  v.load();v.play().then(showSubs).catch(()=>{});
  $('clipPlayer').style.display='block';$('clipNow').textContent='▶ '+(label||n)+'  ·  '+tt('subsOn','GPS-overlay aan (CC-knop = aan/uit)');
  $('clipPlayer').scrollIntoView({behavior:'smooth',block:'nearest'});}

// ---- Terugkijken: filter, selectie, paginering, bulkacties ----
let CLIPS_ALL=[],CLIP_FILTER='all',CLIP_SHOWN=50,CLIP_SEL=new Set();
const CLIP_PAGE=50;
function clipsFiltered(){if(CLIP_FILTER==='locked')return CLIPS_ALL.filter(c=>c.locked);
  if(CLIP_FILTER==='unlocked')return CLIPS_ALL.filter(c=>!c.locked);return CLIPS_ALL;}
async function loadClips(){try{CLIPS_ALL=await jget('/clips.json');renderClips();}catch(e){}}
function renderClips(){
  const list=clipsFiltered();
  const tot=CLIPS_ALL.reduce((a,c)=>a+(c.size||0),0);
  const loc=LANG==='en'?'en-GB':'nl-NL';
  $('clipCount').textContent=(CLIPS_ALL.length||0)+' '+tt('clips','clips')+' · '+fmtBytes(tot);
  const unlockedCount=CLIPS_ALL.filter(c=>!c.locked).length;
  $('btnDelAll').style.display=unlockedCount?'':'none';
  if(!list.length){$('clipGallery').innerHTML='<span class="muted">'+(CLIPS_ALL.length?tt('noneMatch','geen opnames binnen dit filter'):tt('noClips','nog geen video-clips — start de opname in Instellingen'))+'</span>';
    $('clipMore').style.display='none';return;}
  const shown=list.slice(0,CLIP_SHOWN);
  $('clipGallery').innerHTML=shown.map(c=>{const dt=new Date(c.mtime);
    const d=dt.toLocaleDateString(loc,{weekday:'short',day:'2-digit',month:'short'});
    const t=dt.toLocaleTimeString(loc,{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    const lbl=d+' '+t;
    return '<div class="cliprow'+(c.locked?' lockedrow':'')+'">'+
      (c.locked?'<span style="width:16px;display:inline-block"></span>':'<input type="checkbox" class="selchk" data-name="'+c.name+'" '+(CLIP_SEL.has(c.name)?'checked':'')+'>')+
      '<div class="clipmeta">'+(c.locked?'<span title="'+tt('locked','beschermd')+'">🔒 </span>':'')+
      '<b>'+t+'</b> <span class="muted">· '+d+' · '+fmtBytes(c.size)+'</span></div>'+
      '<div class="clipbtns">'+
      '<button class="clipbtn play" onclick="playClip(\''+c.name+'\',\''+lbl+'\')">▶</button>'+
      '<button class="clipbtn'+(c.locked?' lockon':'')+'" onclick="toggleLock(\''+c.name+'\','+(c.locked?1:0)+')" title="'+tt('lock','lock')+'">'+(c.locked?'🔓':'🔒')+'</button>'+
      '<a class="clipbtn" href="/clip?name='+encodeURIComponent(c.name)+'" download="'+c.name+'">'+tt('download','download')+'</a>'+
      '<button class="clipbtn del" onclick="delClip(\''+c.name+'\')">'+tt('del','wis')+'</button>'+
      '</div></div>';}).join('');
  $('clipMore').style.display=(list.length>CLIP_SHOWN)?'':'none';
  document.querySelectorAll('.selchk').forEach(cb=>cb.onchange=()=>{cb.checked?CLIP_SEL.add(cb.dataset.name):CLIP_SEL.delete(cb.dataset.name);updateSelBar();});
  updateSelBar();
}
function updateSelBar(){const n=CLIP_SEL.size;$('btnDelSel').style.display=n?'':'none';
  if(n)$('btnDelSel').textContent=tt('delSelected','wis selectie')+' ('+n+')';}
$('clipMore').onclick=()=>{CLIP_SHOWN+=CLIP_PAGE;renderClips();};
['filtAll','filtLocked','filtUnlocked'].forEach(id=>{$(id).onclick=()=>{
  document.querySelectorAll('.segbtn').forEach(b=>b.classList.remove('on'));$(id).classList.add('on');
  CLIP_FILTER=id==='filtAll'?'all':(id==='filtLocked'?'locked':'unlocked');CLIP_SHOWN=CLIP_PAGE;CLIP_SEL.clear();renderClips();};});
$('selAll').onchange=()=>{const list=clipsFiltered().slice(0,CLIP_SHOWN).filter(c=>!c.locked);
  if($('selAll').checked)list.forEach(c=>CLIP_SEL.add(c.name));else CLIP_SEL.clear();renderClips();};
$('btnDelSel').onclick=async()=>{if(!CLIP_SEL.size)return;if(!confirm(tt('confirmDelSel','De geselecteerde clips verwijderen?')))return;
  await fetch('/clips/delete_many?names='+encodeURIComponent(Array.from(CLIP_SEL).join(',')));CLIP_SEL.clear();loadClips();};
$('btnDelAll').onclick=async()=>{if(!confirm(tt('confirmDelAll','Alle onvergrendelde clips verwijderen? Vergrendelde clips blijven staan.')))return;
  await fetch('/clips/delete_all');CLIP_SEL.clear();loadClips();};
async function delClip(n){if(!confirm(tt('confirmDel','Deze clip verwijderen?')))return;await fetch('/clip_del?name='+encodeURIComponent(n));loadClips();}
async function toggleLock(n,isLocked){await fetch((isLocked?'/clip_unlock?name=':'/clip_lock?name=')+encodeURIComponent(n));loadClips();loadRec();}

function clock(){$('clock').textContent=new Date().toLocaleTimeString(LANG==='en'?'en-GB':'nl-NL');}
snapOrig();assignTabs();showTab('live');
fillLedSelects();loadLedState();loadRec();loadPrefs();
tick();tickSys();tickSlow();tickImu();clock();
setInterval(tick,1500);setInterval(tickFrame,1500);setInterval(tickSys,4000);setInterval(tickSlow,15000);setInterval(clock,1000);
setInterval(tickImu,200);setInterval(loadRec,5000);
</script></body></html>"""


if __name__ == "__main__":
    load_led_settings()
    load_rec_settings()
    load_ui_settings()
    load_lora_settings()
    load_wifi_settings()
    load_security_settings()
    threading.Thread(target=led_driver, daemon=True).start()
    threading.Thread(target=retention_loop, daemon=True).start()
    threading.Thread(target=incident_loop, daemon=True).start()
    threading.Thread(target=live_preview_loop, daemon=True).start()
    threading.Thread(target=track_loop, daemon=True).start()
    threading.Thread(target=srt_loop, daemon=True).start()
    threading.Thread(target=gps_time_sync, daemon=True).start()
    threading.Thread(target=lora_loop, daemon=True).start()
    threading.Thread(target=wifi_loop, daemon=True).start()
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
