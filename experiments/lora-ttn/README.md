# LoRa / TTN experiment (work in progress)

Status: actively being field-tested, not part of the main dashboard yet. Lives on the `beta`
branch on purpose -- nothing here is wired into `dashboard_server.py`, and nothing here is
needed to install and use Roamcam normally.

## What this is

The HDC has an unused LoRa radio (a Semtech SX1262, confirmed by reading the stock
`lorawan-logger` service's own config and probing the chip directly over SPI). These two
scripts test whether it can join [The Things Network](https://www.thethingsnetwork.org/) --
a free, community-run LoRaWAN network -- as a way to eventually carry small things like an
incident-lock alert or a position beacon with zero Wi-Fi in range, without any cloud
dependency beyond TTN itself.

Both scripts implement LoRaWAN 1.0.3 OTAA (join procedure, AES-128 + AES-CMAC) from scratch
in pure Python -- no packages installed on the device, nothing beyond the standard library
plus the `gpiod` module that's already present on the HDC.

- **`ttn_join_test.py`** -- one-shot: sends a single join-request, listens for a join-accept
  in the RX1/RX2 windows, reports the result.
- **`ttn_field_test.py`** -- unattended field test: retries joining every 45s, and once
  joined, sends a small uplink every 90s, for up to 45 minutes, then stops itself and
  restarts `lorawan-logger` automatically. Meant to run in the background while the camera
  is out of Wi-Fi range (e.g. in the car), checked from a phone via the TTN Console's
  "Live data" view.

## Using it

1. Register a device on [the TTN Console](https://console.cloud.thethings.network/), OTAA
   activation, JoinEUI `00-00-00-00-00-00-00-00` (standard for DIY devices with no
   manufacturer-assigned EUI). Fill the generated DevEUI/AppKey into the top of
   `ttn_join_test.py`.
2. Copy both files to the device and stop the stock `lorawan-logger` service first --
   the radio can only be used by one process at a time (`systemctl stop lorawan-logger`,
   and `systemctl start lorawan-logger` when done -- both scripts restore it on exit, but
   good to know if a test gets killed uncleanly).
3. Run `ttn_join_test.py` for a quick single attempt, or `ttn_field_test.py` in the
   background (`nohup python3 ttn_field_test.py &`) for an unattended multi-attempt test.

## Known open questions

- Not yet confirmed end-to-end: an indoor test produced no downlink (unclear whether that's
  coverage/RF or a bug in this implementation) -- a field test with better sky visibility is
  the next data point.
- Frequency/data-rate is currently fixed to the EU868 default join channel (868.1 MHz, SF7)
  rather than implementing the full channel plan.
