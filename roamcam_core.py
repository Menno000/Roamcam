# -*- coding: utf-8 -*-
"""Gedeelde kern voor alle Roamcam-programma's op de Hivemapper HDC.

Waarom dit bestand bestaat
--------------------------
Dezelfde functies stonden tot november 2026 in drie programma's naast elkaar, en ze waren uit
elkaar gegroeid. Dat kostte ons echt tijd:

* `sh()` bestond vier keer met vier verschillende timeouts (6, 10, 10 vast, 30 s). Een wifi-scan
  duurt regelmatig langer dan zes seconden, waarna `sh()` stil een leeg resultaat teruggaf en de
  interface "geen netwerken gevonden" meldde. De fix ging toen in één kopie.
* Een complete, met de hand geschreven AES-128 stond twee keer (69 regels). Een fout in de ene
  helft valt niet op zolang de andere werkt.
* Acht functies deden hetzelfde instellingen-lees-en-schrijfwerk met een andere bestandsnaam.

Eén implementatie betekent: een les hoeft nog maar één keer geleerd te worden.

Opzet
-----
Eén bestand, alleen de standaardbibliotheek, geen vaste paden. Alles wat met het toestel te maken
heeft krijgt het pad als argument, zodat dit bestand op een laptop te testen is. De tests staan in
`roles/core/test_core.py` en draaien zonder camera.

Op het toestel staat dit als `/mnt/data/roamcam_core.py`. Programma's halen het binnen met:

    import sys
    sys.path.insert(0, "/mnt/data")
    import roamcam_core as core

Bronbestand is `roamcam-lab/roles/core/roamcam_core.py`. De kopie in de repo `roamcam` moet
byte-identiek zijn; `test_core.py` controleert dat als beide repo's naast elkaar staan.
"""

import json
import math
import os
import signal
import struct
import subprocess
import time

VERSIE = "1.0.0"


# --------------------------------------------------------------------------- shell

def sh(cmd, timeout=10):
    """Voer een shellopdracht uit en geef de uitvoer terug, of een lege string bij een fout.

    De timeout is bewust een argument en niet een vaste waarde: lange opdrachten zoals
    `iwlist wlan0 scan` hebben er dertig nodig, een `cat` van sysfs is in milliseconden klaar.
    Een te korte timeout geeft stil een leeg resultaat, en dat is lastig te vinden.

    Waarom hier geen `subprocess.run(..., timeout=)` staat
    -----------------------------------------------------
    Dat was de oude implementatie, in alle vier de kopieën, en die **houdt zich niet aan zijn
    eigen timeout**. `run()` breekt bij een timeout alleen het directe kind af -- de shell. Een
    kleinkind blijft dan leven en houdt de pijpen open, waarna `run()` alsnog blijft wachten tot
    dat kleinkind klaar is. Aangetoond in `test_core.py`: een timeout van 1 seconde op een
    opdracht van 5 seconden gaf pas na 5,1 seconde antwoord.

    Dat raakt ons echt, want onze opdrachten hebben vaak kleinkinderen: alles met een pijp of een
    omleiding, zoals `iwlist wlan0 scan 2>/dev/null`. Vandaar een eigen procesgroep en bij een
    timeout de hele groep afbreken.
    """
    opties = {}
    if os.name == "posix":
        opties["start_new_session"] = True  # eigen procesgroep, zodat we alles kunnen afbreken
    try:
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, **opties)
    except Exception:
        return ""
    try:
        uit, _ = p.communicate(timeout=timeout)
        return (uit or "").strip()
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        return ""
    except Exception:
        _kill_tree(p)
        return ""


def _kill_tree(p):
    """Breek een proces en alles wat het gestart heeft af, en wacht niet eeuwig op de pijpen."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        else:
            # alleen voor de tests op een laptop; cmd.exe doodmaken laat het kleinkind leven
            subprocess.run("taskkill /T /F /PID %d" % p.pid, shell=True,
                           capture_output=True, timeout=5)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
    try:
        p.communicate(timeout=2)
    except Exception:
        pass


# --------------------------------------------------------------------------- bestanden

def read(path, default=""):
    """Lees een bestand als tekst; geef `default` terug als dat niet kan."""
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default


def write_text(path, data):
    """Schrijf tekst weg. Geeft True of False terug in plaats van een uitzondering."""
    try:
        with open(path, "w") as f:
            f.write(data)
        return True
    except Exception:
        return False


def load_json(path, default=None):
    """Lees JSON; bij een ontbrekend of kapot bestand `default` (standaard een lege dict)."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def save_json(path, obj):
    """Schrijf JSON weg.

    Let op: bewust NIET via een tijdelijk bestand met rename. Op dit toestel volgt de
    vendor-`led-controller` `/tmp/led.json` met inotify, en een rename geeft een nieuw inode
    waarna die driver niets meer ziet. Daarom overal op dezelfde plek overschrijven.
    """
    try:
        with open(path, "w") as f:
            json.dump(obj, f)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- instellingen

def load_section(path, key, defaults, target=None):
    """Lees één sectie uit een gedeeld JSON-bestand over `defaults` heen.

    Vervangt acht bijna identieke functies (`load_ui_settings`, `load_lora_settings`, ...).
    Alleen sleutels die in `defaults` staan worden overgenomen, zodat een oud of rommelig
    bestand geen onbekende velden binnensmokkelt.

    Geeft de bijgewerkte dict terug. Met `target` wordt die dict ter plekke bijgewerkt, wat
    nodig is voor de bestaande code die een modulevariabele deelt (`ui_cfg`, `wifi_cfg`, ...).
    """
    cfg = target if target is not None else dict(defaults)
    if target is not None:
        for k, v in defaults.items():
            cfg.setdefault(k, v)
    try:
        d = load_json(path)
        sectie = d.get(key)
        if isinstance(sectie, dict):
            for k in defaults:
                if k in sectie:
                    cfg[k] = sectie[k]
    except Exception:
        pass
    return cfg


def save_section(path, key, cfg):
    """Schrijf één sectie terug zonder de andere secties in het bestand kwijt te raken."""
    try:
        d = load_json(path)
        if not isinstance(d, dict):
            d = {}
        d[key] = dict(cfg)
        return save_json(path, d)
    except Exception:
        return False


# --------------------------------------------------------------------------- afstand

def haversine(lat1, lon1, lat2, lon2):
    """Afstand tussen twee punten in METERS.

    Let op de argumentvolgorde: lat, lon, lat, lon. De oude `haversine(a, b, c, d)` in de
    reis-rol gebruikte dezelfde volgorde, maar met namen waaruit dat niet bleek.
    """
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


# --------------------------------------------------------------------------- AES-128

_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11b
    return a & 0xff


def _key_expansion(key):
    nk = 4
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 44):
        temp = list(w[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // nk - 1]
        w.append([w[i - nk][j] ^ temp[j] for j in range(4)])
    return w


def aes128_encrypt_block(key, block16):
    """Eén AES-128-blok versleutelen. Alleen de standaardbibliotheek.

    Gecontroleerd tegen de officiele testvector uit FIPS-197 bijlage B/C, zie `test_core.py`.
    Gebruikt voor LoRaWAN (CMAC, sessiesleutels, payload) en voor Meshtastic (AES-CTR).
    """
    w = _key_expansion(key)
    state = [[block16[r + 4 * c] for c in range(4)] for r in range(4)]

    def add_round_key(st, rk):
        for c in range(4):
            for r in range(4):
                st[r][c] ^= rk[c][r]

    def sub_bytes(st):
        for r in range(4):
            for c in range(4):
                st[r][c] = _SBOX[st[r][c]]

    def shift_rows(st):
        for r in range(1, 4):
            st[r] = st[r][r:] + st[r][:r]

    def mix_columns(st):
        for c in range(4):
            a = [st[r][c] for r in range(4)]
            st[0][c] = _xtime(a[0]) ^ (_xtime(a[1]) ^ a[1]) ^ a[2] ^ a[3]
            st[1][c] = a[0] ^ _xtime(a[1]) ^ (_xtime(a[2]) ^ a[2]) ^ a[3]
            st[2][c] = a[0] ^ a[1] ^ _xtime(a[2]) ^ (_xtime(a[3]) ^ a[3])
            st[3][c] = (_xtime(a[0]) ^ a[0]) ^ a[1] ^ a[2] ^ _xtime(a[3])

    add_round_key(state, w[0:4])
    for rnd in range(1, 10):
        sub_bytes(state)
        shift_rows(state)
        mix_columns(state)
        add_round_key(state, w[4 * rnd:4 * rnd + 4])
    sub_bytes(state)
    shift_rows(state)
    add_round_key(state, w[40:44])

    out = bytearray(16)
    for c in range(4):
        for r in range(4):
            out[r + 4 * c] = state[r][c]
    return bytes(out)


def aes_ctr(key, nonce16, data):
    """AES-128 in CTR-modus, teller big-endian over het hele nonce-blok (zoals Meshtastic)."""
    out = bytearray()
    ctr = int.from_bytes(nonce16, "big")
    for i in range(0, len(data), 16):
        ks = aes128_encrypt_block(key, ctr.to_bytes(16, "big"))
        out += bytes(a ^ b for a, b in zip(data[i:i + 16], ks))
        ctr = (ctr + 1) & ((1 << 128) - 1)
    return bytes(out)


def meshtastic_nonce(packet_id, sender):
    """Nonce zoals Meshtastic die opbouwt: pakket-id en afzender little-endian, dan nullen."""
    return struct.pack("<QI", packet_id, sender) + bytes(4)


# --------------------------------------------------------------------------- klok en tijdzone

CLOCK_SANE_YEAR = 2024


def clock_is_sane(min_year=CLOCK_SANE_YEAR):
    """Staat de klok op iets geloofwaardigs?

    Dit toestel heeft geen RTC en begint na elke stroomonderbreking in december 2022. Alles wat
    verstreken tijd meet moet `time.monotonic()` gebruiken; dit is alleen om te zien of de
    wandklok bruikbaar is.
    """
    return time.gmtime().tm_year >= min_year


def clock_set_utc(epoch):
    """Zet de systeemklok op een UTC-tijdstempel. Vereist root."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))
    sh('date -u -s "%s" 2>/dev/null' % stamp)
    return stamp


def clock_save(path):
    """Bewaar de huidige tijd, zodat de volgende start niet in 2022 begint."""
    if not clock_is_sane():
        return False
    return write_text(path, "%d" % int(time.time()))


def clock_restore(path):
    """Zet de bewaarde tijd terug, maar nooit over een al goede klok heen.

    Alleen vooruit: een bewaarde tijd die ouder is dan de huidige klok wordt genegeerd.
    """
    if clock_is_sane():
        return False
    try:
        saved = float(read(path, ""))
    except Exception:
        return False
    if saved > time.time():
        clock_set_utc(saved)
        return True
    return False


def apply_tz(tz):
    """Zet de tijdzone voor dit proces en alles wat het start.

    Het toestel staat af-fabriek op UTC, dus alles wat het zelf schrijft (clipnamen, ritnamen, de
    tijd in de GPS-ondertitels) liep in de zomer twee uur achter op de klok in de auto. Via de
    TZ-variabele erven ook libcamera en ffmpeg dit, zodat bestandsnamen meteen kloppen. `/etc`
    staat op een RAM-overlay en is na een herstart weer leeg, vandaar bij elke start opnieuw.
    """
    tz = tz or "UTC"
    os.environ["TZ"] = tz
    try:
        time.tzset()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- meeliften op de vendor

GPS_LATEST = "/mnt/data/gps/latest.log"
IMU_LATEST = "/mnt/data/imu/latest.log"
FRAMES_DIR = "/tmp/recording/pic"


def gps_read(path=GPS_LATEST):
    """Lees de laatste GPS-regel die de vendor-datalogger wegschreef (2x per seconde)."""
    return load_json(path, {})


def gps_is_live(path=GPS_LATEST, tries=6, pause=0.4, reader=None):
    """Geeft een VERSE GPS-regel terug, of None.

    Nodig omdat `latest.log` een stroomonderbreking overleeft: vlak na het opstarten lees je de
    positie en de tijd van vóór de vorige keer uitzetten. Dat kostte ons een klok die 25 seconden
    verkeerd stond. Twee keer lezen en eisen dat het tijdstempel verandert bewijst dat de
    datalogger echt aan het schrijven is.

    `reader` is er voor programma's die hun eigen verrijkte lezer hebben (de reis-rol zet er
    `fix_ok`, `kmh` en het aantal satellieten bij). Die krijgt het pad als enige argument.
    """
    lees = reader or (lambda p: gps_read(p))
    eerst = lees(path)
    if not eerst:
        return None
    for _ in range(tries):
        time.sleep(pause)
        nu = lees(path)
        if nu and nu.get("timestamp") != eerst.get("timestamp"):
            return nu
    return None


def gps_has_fix(sample):
    """Heeft deze GPS-regel een bruikbare fix? Zonder deze controle telt ruis als beweging.

    Een bureau zonder fix leverde ons eerder een rit van 7,8 km op.
    """
    if not sample:
        return False
    if sample.get("fix") in ("3D", "2D"):
        return True
    return bool(sample.get("fix_ok"))


def newest_frame(directory=FRAMES_DIR):
    """Pad naar het nieuwste JPEG-beeldje in de RAM-buffer van de vendor, of None.

    Twee details die anders geld kosten: de bestandsnaam is `<epoch>_<microseconden>.jpg`, dus
    alfabetisch sorteren is ook chronologisch sorteren (geen stat-aanroepen nodig). En de
    vendor schrijft eerst `.jpg.tmp` en hernoemt daarna, dus filteren op `.jpg` geeft altijd een
    compleet bestand.
    """
    try:
        namen = [n for n in os.listdir(directory) if n.endswith(".jpg")]
    except Exception:
        return None
    if not namen:
        return None
    return os.path.join(directory, max(namen))
