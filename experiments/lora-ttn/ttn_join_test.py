#!/usr/bin/env python3
"""
Minimal, dependency-free LoRaWAN 1.0.3 OTAA join test against The Things Network,
driving the SX1262 directly over raw spidev + gpiod (same pins lorawan-logger uses).

Pure stdlib only (own AES-128 + AES-CMAC implementation) -- nothing installed,
nothing changed on the device beyond this one script.
"""
import gpiod
import fcntl
import struct
import time
import ctypes
import sys
import os
import binascii

# ============================================================
# Device identity -- fill these in from your own TTN console
# (Applications -> your app -> your device -> Overview / Provisioning)
# Never commit real values here -- these are per-device secrets.
# ============================================================
DEVEUI = bytes.fromhex("0000000000000000")    # as shown in the console, MSB order
JOINEUI = bytes.fromhex("0000000000000000")   # usually all-zero for DIY devices
APPKEY = bytes.fromhex("00" * 16)  # 16 bytes

if len(APPKEY) != 16:
    print("FATAL: APPKEY must be 16 bytes -- check the hex string", file=sys.stderr)
    sys.exit(2)

# ============================================================
# Pure-python AES-128 (encrypt only -- that's all LoRaWAN needs)
# ============================================================
Sbox = [
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

Rcon = [0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36]


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11b
    return a & 0xff


def key_expansion(key):
    Nk, Nb, Nr = 4, 4, 10
    w = [list(key[4*i:4*i+4]) for i in range(Nk)]
    for i in range(Nk, Nb * (Nr + 1)):
        temp = list(w[i - 1])
        if i % Nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [Sbox[b] for b in temp]
            temp[0] ^= Rcon[i // Nk - 1]
        w.append([w[i - Nk][j] ^ temp[j] for j in range(4)])
    return w


def aes128_encrypt_block(key, block16):
    w = key_expansion(key)
    state = [[block16[r + 4 * c] for c in range(4)] for r in range(4)]

    def add_round_key(state, rk):
        for c in range(4):
            for r in range(4):
                state[r][c] ^= rk[c][r]

    def sub_bytes(state):
        for r in range(4):
            for c in range(4):
                state[r][c] = Sbox[state[r][c]]

    def shift_rows(state):
        for r in range(1, 4):
            state[r] = state[r][r:] + state[r][:r]

    def mix_columns(state):
        for c in range(4):
            a = [state[r][c] for r in range(4)]
            state[0][c] = _xtime(a[0]) ^ (_xtime(a[1]) ^ a[1]) ^ a[2] ^ a[3]
            state[1][c] = a[0] ^ _xtime(a[1]) ^ (_xtime(a[2]) ^ a[2]) ^ a[3]
            state[2][c] = a[0] ^ a[1] ^ _xtime(a[2]) ^ (_xtime(a[3]) ^ a[3])
            state[3][c] = (_xtime(a[0]) ^ a[0]) ^ a[1] ^ a[2] ^ _xtime(a[3])

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


def _shift_left_1(b):
    n = int.from_bytes(b, "big") << 1
    n &= (1 << (8 * len(b))) - 1
    return n.to_bytes(len(b), "big")


def aes_cmac(key, msg):
    const_Rb = 0x87
    L = aes128_encrypt_block(key, b"\x00" * 16)
    K1 = _shift_left_1(L)
    if L[0] & 0x80:
        K1 = (int.from_bytes(K1, "big") ^ const_Rb).to_bytes(16, "big")
    K2 = _shift_left_1(K1)
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
        x = aes128_encrypt_block(key, bytes(a ^ c for a, c in zip(x, b)))
    x = aes128_encrypt_block(key, bytes(a ^ c for a, c in zip(x, M_last)))
    return x


# ============================================================
# SX1262 raw driver (spidev + gpiod), same pins as lorawan-logger
# ============================================================
GPIOCHIP = "gpiochip0"
CS_OFFSET = 18
BUSY_OFFSET = 25
NRST_OFFSET = 24
DIO1_OFFSET = 7
SPI_DEV = "/dev/spidev1.0"

SPI_IOC_MAGIC = ord('k')
_IOC_WRITE = 1


def _IOC(direction, type_, nr, size):
    return (direction << 30) | (type_ << 8) | (nr << 0) | (size << 16)


def SPI_IOC_MESSAGE(n):
    return _IOC(_IOC_WRITE, SPI_IOC_MAGIC, 0, n * 32)


SPI_IOC_WR_MODE = _IOC(_IOC_WRITE, SPI_IOC_MAGIC, 1, 1)
SPI_IOC_WR_BITS_PER_WORD = _IOC(_IOC_WRITE, SPI_IOC_MAGIC, 3, 1)
SPI_IOC_WR_MAX_SPEED_HZ = _IOC(_IOC_WRITE, SPI_IOC_MAGIC, 4, 4)


class SX1262:
    def __init__(self):
        self.chip = gpiod.Chip(GPIOCHIP)
        self.cs = self.chip.get_line(CS_OFFSET)
        self.cs.request(consumer="ttn_join", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[1])
        self.busy = self.chip.get_line(BUSY_OFFSET)
        self.busy.request(consumer="ttn_join", type=gpiod.LINE_REQ_DIR_IN)
        self.nrst = self.chip.get_line(NRST_OFFSET)
        self.nrst.request(consumer="ttn_join", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[1])
        self.dio1 = self.chip.get_line(DIO1_OFFSET)
        self.dio1.request(consumer="ttn_join", type=gpiod.LINE_REQ_DIR_IN)

        self.fd = open(SPI_DEV, "r+b", buffering=0)
        fcntl.ioctl(self.fd, SPI_IOC_WR_MODE, struct.pack("B", 0))
        fcntl.ioctl(self.fd, SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))
        fcntl.ioctl(self.fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("<I", 2000000))

    def _xfer(self, tx_bytes):
        n = len(tx_bytes)
        tx_buf = ctypes.create_string_buffer(bytes(tx_bytes), n)
        rx_buf = ctypes.create_string_buffer(n)
        packed = struct.pack("<QQIIHBBI", ctypes.addressof(tx_buf), ctypes.addressof(rx_buf),
                              n, 0, 0, 8, 0, 0)
        fcntl.ioctl(self.fd, SPI_IOC_MESSAGE(1), packed)
        return bytes(rx_buf.raw)

    def wait_not_busy(self, timeout=1.0):
        t0 = time.time()
        while self.busy.get_value() == 1:
            if time.time() - t0 > timeout:
                raise TimeoutError("BUSY stuck high")
            time.sleep(0.0005)

    def cmd(self, opcode, params=b"", read_len=0):
        self.wait_not_busy()
        self.cs.set_value(0)
        tx = bytes([opcode]) + bytes(params) + bytes(read_len)
        rx = self._xfer(tx)
        self.cs.set_value(1)
        return rx[1 + len(params):]

    def reset(self):
        self.nrst.set_value(0)
        time.sleep(0.001)
        self.nrst.set_value(1)
        self.wait_not_busy(timeout=1.0)

    def set_standby_rc(self):
        self.cmd(0x80, [0x00])

    def set_packet_type_lora(self):
        self.cmd(0x8A, [0x01])

    def set_rf_frequency(self, freq_hz):
        # SX126x: freq_reg = freq_hz * 2^25 / 32e6
        freq_reg = int(freq_hz * (1 << 25) / 32000000)
        b = struct.pack(">I", freq_reg)
        self.cmd(0x86, list(b))

    def set_buffer_base_address(self, tx=0, rx=0):
        self.cmd(0x8F, [tx, rx])

    def set_modulation_params_lora(self, sf=7, bw=0x04, cr=1, ldro=0):
        # bw 0x04 = 125kHz, cr 1 = 4/5
        self.cmd(0x8B, [sf, bw, cr, ldro])

    def set_packet_params_lora(self, preamble_len=8, header_type=0, payload_len=0, crc_on=1, invert_iq=0):
        b = struct.pack(">H", preamble_len) + bytes([header_type, payload_len, crc_on, invert_iq])
        self.cmd(0x8C, list(b))

    def set_tx_params(self, power_dbm=14, ramp=0x04):
        self.cmd(0x8E, [power_dbm & 0xFF, ramp])

    def set_dio_irq_params(self, irq_mask, dio1_mask):
        b = struct.pack(">HHHH", irq_mask, dio1_mask, 0, 0)
        self.cmd(0x08, list(b))

    def clear_irq_status(self, mask=0xFFFF):
        self.cmd(0x02, list(struct.pack(">H", mask)))

    def get_irq_status(self):
        r = self.cmd(0x12, [0x00], read_len=2)
        return struct.unpack(">H", r[:2])[0]

    def write_buffer(self, offset, data):
        self.cmd(0x0E, [offset] + list(data))

    def read_buffer(self, offset, length):
        r = self.cmd(0x1E, [offset, 0x00], read_len=length)
        return r

    def set_tx(self, timeout_ms=0):
        # timeout in units of 15.625us; 0 = no timeout (we manage our own via IRQ poll+deadline)
        t = int(timeout_ms * 1000 / 15.625) if timeout_ms else 0
        self.cmd(0x83, list(struct.pack(">I", t)[1:4]))

    def set_rx(self, timeout_ms):
        t = int(timeout_ms * 1000 / 15.625) if timeout_ms else 0xFFFFFF
        self.cmd(0x82, list(struct.pack(">I", t)[1:4]))

    def get_rx_buffer_status(self):
        r = self.cmd(0x13, [0x00], read_len=2)
        return r[0], r[1]  # payload_len, rx_start_ptr

    def get_status(self):
        r = self.cmd(0xC0, [], read_len=1)
        return r[0]

    def close(self):
        self.fd.close()
        self.cs.release(); self.busy.release(); self.nrst.release(); self.dio1.release()


IRQ_TX_DONE = 1 << 0
IRQ_RX_DONE = 1 << 1
IRQ_TIMEOUT = 1 << 9


def configure_for(radio, freq_hz, sf=7, bw=0x04, power=14):
    radio.set_standby_rc()
    radio.set_packet_type_lora()
    radio.set_rf_frequency(freq_hz)
    radio.set_buffer_base_address(0, 0)
    radio.set_modulation_params_lora(sf=sf, bw=bw, cr=1, ldro=0)
    radio.set_tx_params(power_dbm=power, ramp=0x04)


def lora_tx(radio, freq_hz, sf, payload):
    configure_for(radio, freq_hz, sf=sf, power=14)
    radio.set_packet_params_lora(preamble_len=8, header_type=0, payload_len=len(payload), crc_on=1, invert_iq=0)
    radio.write_buffer(0, payload)
    radio.clear_irq_status(0xFFFF)
    radio.set_dio_irq_params(IRQ_TX_DONE | IRQ_TIMEOUT, IRQ_TX_DONE | IRQ_TIMEOUT)
    t0 = time.time()
    radio.set_tx(timeout_ms=4000)
    while True:
        irq = radio.get_irq_status()
        if irq & IRQ_TX_DONE:
            radio.clear_irq_status(0xFFFF)
            return time.time()
        if irq & IRQ_TIMEOUT or time.time() - t0 > 4.0:
            radio.clear_irq_status(0xFFFF)
            raise TimeoutError("TX did not complete")
        time.sleep(0.005)


def lora_rx_window(radio, freq_hz, sf, window_s, invert_iq=1):
    radio.set_standby_rc()
    radio.set_packet_type_lora()
    radio.set_rf_frequency(freq_hz)
    radio.set_buffer_base_address(0, 0)
    radio.set_modulation_params_lora(sf=sf, bw=0x04, cr=1, ldro=0)
    radio.set_packet_params_lora(preamble_len=8, header_type=0, payload_len=255, crc_on=0, invert_iq=invert_iq)
    radio.clear_irq_status(0xFFFF)
    radio.set_dio_irq_params(IRQ_RX_DONE | IRQ_TIMEOUT, IRQ_RX_DONE | IRQ_TIMEOUT)
    radio.set_rx(timeout_ms=int(window_s * 1000))
    t0 = time.time()
    while time.time() - t0 < window_s + 0.5:
        irq = radio.get_irq_status()
        if irq & IRQ_RX_DONE:
            plen, start = radio.get_rx_buffer_status()
            data = radio.read_buffer(start, plen)
            radio.clear_irq_status(0xFFFF)
            return data
        if irq & IRQ_TIMEOUT:
            radio.clear_irq_status(0xFFFF)
            return None
        time.sleep(0.01)
    return None


# ============================================================
# LoRaWAN join-request / join-accept framing
# ============================================================
def build_join_request(appkey, joineui, deveui, devnonce):
    mhdr = bytes([0x00])
    payload = mhdr + joineui[::-1] + deveui[::-1] + struct.pack("<H", devnonce)
    mic = aes_cmac(appkey, payload)[:4]
    return payload + mic


def try_parse_join_accept(appkey, raw, devnonce):
    if raw is None or len(raw) < 12:
        return None
    mhdr = raw[0:1]
    if mhdr[0] != 0x20:
        return None
    encrypted = raw[1:]
    # pad to multiple of 16 for our block encrypt (join accept body is 12 or 28 bytes)
    pt = b""
    for i in range(0, len(encrypted), 16):
        block = encrypted[i:i+16]
        if len(block) < 16:
            break
        pt += aes128_encrypt_block(appkey, block)
    body = mhdr + pt
    if len(body) < 12:
        return None
    mic_calc = aes_cmac(appkey, body[:-4])[:4]
    mic_recv = body[-4:]
    appnonce = body[1:4]
    netid = body[4:7]
    devaddr = body[7:11]
    dlsettings = body[11]
    rxdelay = body[12] if len(body) > 12 else 0
    ok = (mic_calc == mic_recv)
    return {
        "ok": ok, "appnonce": appnonce, "netid": netid, "devaddr": devaddr,
        "dlsettings": dlsettings, "rxdelay": rxdelay, "raw": body,
    }


def derive_session_keys(appkey, appnonce, netid, devnonce):
    pad = appnonce + netid + struct.pack("<H", devnonce) + b"\x00" * 7
    nwkskey = aes128_encrypt_block(appkey, b"\x01" + pad)
    appskey = aes128_encrypt_block(appkey, b"\x02" + pad)
    return nwkskey, appskey


# ============================================================
# Main
# ============================================================
JOIN_FREQ = 868100000  # EU868 default join channel 1
JOIN_SF = 7
RX2_FREQ = 869525000
RX2_SF = 9


def main():
    devnonce = struct.unpack("<H", os.urandom(2))[0]
    print(f"DevEUI={DEVEUI.hex()} JoinEUI={JOINEUI.hex()} DevNonce={devnonce}")

    radio = SX1262()
    try:
        radio.reset()
        status = radio.get_status()
        print(f"Post-reset status: 0x{status:02x}")

        jreq = build_join_request(APPKEY, JOINEUI, DEVEUI, devnonce)
        print(f"JoinRequest ({len(jreq)} bytes): {jreq.hex()}")

        tx_done_t = lora_tx(radio, JOIN_FREQ, JOIN_SF, jreq)
        print(f"TX done at t={tx_done_t:.3f} on {JOIN_FREQ/1e6:.3f} MHz SF{JOIN_SF} -- check TTN console now")

        # RX1: same freq/SF, JOIN_ACCEPT_DELAY1 = 5s after TX done
        rx1_start = tx_done_t + 5.0
        sleep_for = rx1_start - time.time()
        if sleep_for > 0:
            time.sleep(sleep_for)
        print("Opening RX1 window...")
        raw = lora_rx_window(radio, JOIN_FREQ, JOIN_SF, window_s=1.5)

        if raw is None:
            rx2_start = tx_done_t + 6.0
            sleep_for = rx2_start - time.time()
            if sleep_for > 0:
                time.sleep(sleep_for)
            print("RX1 empty, opening RX2 window (869.525 MHz SF9)...")
            raw = lora_rx_window(radio, RX2_FREQ, RX2_SF, window_s=2.0)

        if raw is None:
            print("RESULT: no downlink received in RX1 or RX2 (no join-accept heard).")
            return

        print(f"Raw downlink ({len(raw)} bytes): {raw.hex()}")
        result = try_parse_join_accept(APPKEY, raw, devnonce)
        if not result:
            print("RESULT: received something, but it doesn't look like a valid join-accept frame.")
            return
        if not result["ok"]:
            print("RESULT: join-accept received but MIC verification FAILED (crypto bug or wrong key).")
            print(result)
            return

        nwkskey, appskey = derive_session_keys(APPKEY, result["appnonce"], result["netid"], devnonce)
        print("RESULT: JOIN SUCCESSFUL")
        print(f"  DevAddr = {result['devaddr'].hex()}")
        print(f"  NetID   = {result['netid'].hex()}")
        print(f"  NwkSKey = {nwkskey.hex()}")
        print(f"  AppSKey = {appskey.hex()}")
    finally:
        radio.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
