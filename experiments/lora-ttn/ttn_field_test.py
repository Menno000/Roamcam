#!/usr/bin/env python3
"""
Field test: repeatedly try to join TTN, and once joined, send periodic uplinks.
Meant to run unattended in the car while checking TTN Console -> Live data on a phone.

Runs for a bounded total time (default 45 min), then exits on its own.
Always restarts lorawan-logger on exit, even on error/Ctrl-C.
"""
import gpiod, fcntl, struct, time, ctypes, sys, os, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ttn_join_test import (
    DEVEUI, JOINEUI, APPKEY, SX1262, lora_tx, lora_rx_window,
    build_join_request, try_parse_join_accept, derive_session_keys,
    aes128_encrypt_block, aes_cmac,
    JOIN_FREQ, JOIN_SF, RX2_FREQ, RX2_SF,
)

TOTAL_RUNTIME_S = 45 * 60      # hard stop after this long
JOIN_RETRY_EVERY_S = 45        # gap between join attempts while unjoined
UPLINK_EVERY_S = 90            # gap between uplinks once joined
LOG_PATH = "/mnt/data/meshtasticd/ttn_field_test.log"


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def encrypt_payload(key, devaddr, fcnt, direction, payload):
    out = bytearray()
    i = 1
    for off in range(0, len(payload), 16):
        a = bytes([0x01, 0, 0, 0, 0, direction]) + devaddr[::-1] + \
            struct.pack("<I", fcnt) + bytes([0x00, i])
        s = aes128_encrypt_block(key, a)
        block = payload[off:off+16]
        out += bytes(p ^ k for p, k in zip(block, s))
        i += 1
    return bytes(out)


def build_uplink(nwkskey, appskey, devaddr, fcnt, fport, payload):
    mhdr = bytes([0x40])  # unconfirmed data up
    fctrl = bytes([0x00])
    fhdr = devaddr[::-1] + fctrl + struct.pack("<H", fcnt)
    enc_payload = encrypt_payload(appskey, devaddr, fcnt, 0, payload)
    msg = mhdr + fhdr + bytes([fport]) + enc_payload
    b0 = bytes([0x49, 0, 0, 0, 0, 0]) + devaddr[::-1] + struct.pack("<I", fcnt) + \
        bytes([0x00, len(msg)])
    mic = aes_cmac(nwkskey, b0 + msg)[:4]
    return msg + mic


def do_join_attempt(radio, attempt_no):
    devnonce = struct.unpack("<H", os.urandom(2))[0]
    log(f"Join attempt #{attempt_no} (DevNonce={devnonce})")
    radio.reset()
    jreq = build_join_request(APPKEY, JOINEUI, DEVEUI, devnonce)
    tx_done_t = lora_tx(radio, JOIN_FREQ, JOIN_SF, jreq)
    log(f"  TX done on {JOIN_FREQ/1e6:.3f} MHz SF{JOIN_SF}")

    sleep_for = (tx_done_t + 5.0) - time.time()
    if sleep_for > 0:
        time.sleep(sleep_for)
    raw = lora_rx_window(radio, JOIN_FREQ, JOIN_SF, window_s=1.5)

    if raw is None:
        sleep_for = (tx_done_t + 6.0) - time.time()
        if sleep_for > 0:
            time.sleep(sleep_for)
        raw = lora_rx_window(radio, RX2_FREQ, RX2_SF, window_s=2.0)

    if raw is None:
        log("  no downlink heard (RX1+RX2 empty)")
        return None

    result = try_parse_join_accept(APPKEY, raw, devnonce)
    if not result or not result["ok"]:
        log(f"  got a downlink but it didn't validate: {raw.hex()}")
        return None

    nwkskey, appskey = derive_session_keys(APPKEY, result["appnonce"], result["netid"], devnonce)
    log(f"  JOINED! DevAddr={result['devaddr'].hex()} NetID={result['netid'].hex()}")
    return {"devaddr": result["devaddr"], "nwkskey": nwkskey, "appskey": appskey}


def main():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    log("=== field test starting ===")
    log(f"DevEUI={DEVEUI.hex()} total runtime budget={TOTAL_RUNTIME_S}s")

    t_start = time.time()
    session = None
    fcnt = 0
    attempt = 0
    radio = SX1262()
    try:
        while time.time() - t_start < TOTAL_RUNTIME_S:
            if session is None:
                attempt += 1
                try:
                    session = do_join_attempt(radio, attempt)
                except Exception as e:
                    log(f"  join attempt error: {e}")
                if session is None:
                    time.sleep(JOIN_RETRY_EVERY_S)
                continue

            # joined: send a small uplink
            try:
                payload = struct.pack(">H", fcnt)  # trivial 2-byte counter payload
                frame = build_uplink(session["nwkskey"], session["appskey"],
                                      session["devaddr"], fcnt, 1, payload)
                lora_tx(radio, JOIN_FREQ, JOIN_SF, frame)
                log(f"  uplink sent, FCnt={fcnt}, payload={payload.hex()}")
                fcnt += 1
            except Exception as e:
                log(f"  uplink error: {e}")
            time.sleep(UPLINK_EVERY_S)

        log("=== runtime budget reached, stopping ===")
    except KeyboardInterrupt:
        log("=== interrupted, stopping ===")
    finally:
        radio.close()
        log("Restoring lorawan-logger...")
        subprocess.run(["systemctl", "start", "lorawan-logger"])
        log("=== done ===")


if __name__ == "__main__":
    main()
