# OOK transmit and receive

On-off keying (OOK/ASK) for the Nomad and XR4: transmit and receive across the
LR1121's full synthesizer range (150–2500 MHz). Confirmed on XR4 hardware
2026-11-05, including the requested **303.875 MHz** transmit.

The LR1121 has no native OOK modem. This feature builds it from stock commands:

- **TX** keys an unmodulated carrier — `SetTxCw` (on) / `SetStandby` (off) —
  from a microsecond timer on the ESP32, playing back a pulse list.
- **RX** puts the chip in a wide GFSK receive bandwidth and polls instantaneous
  RSSI (`GetRssiInst`), thresholding the reading into an on/off envelope.

Opcodes are verified against Semtech SWDR001. No custom LR1121 firmware is
needed; on-chip firmware would only help fast, short-pulse protocols (see
[Timing](#timing)).

> **On-air responsibility.** You pick the frequency; you own its legality.
> 300/315/433 MHz control transmission in the US falls under FCC Part 15.231
> (power, duty-cycle and purpose limits). The firmware caps a single burst at 2 s
> of on-air time but enforces no band or power policy.

## Transmit

```sh
# Replay an explicit pulse list (microseconds, carrier-on first), 5 repeats
python host/ook.py --port PORT --radio 0 tx \
  --frequency-hz 303875000 --pa lp --power-dbm -17 \
  --repeat 5 --gap-us 10000 --pulses 350,1050,350,1050,700,700

# Replay a Flipper Zero RAW .sub file (its Frequency: field is used)
python host/ook.py --port PORT --radio 0 tx --sub garage.sub --repeat 5
```

- `--pa` `lp` (-17..14 dBm), `hp` (-9..22), `hf` (-18..13, the 2.4 GHz PA).
  Power must sit inside the chosen PA's range.
- `--ramp-us` PA ramp (default 48). `--gap-us` idle between repeats.
- Pulses: 1..32768 µs each, up to 2048, alternating on/off. Total on-air time
  (`(sum+gap)*repeat`) must stay under 2 s.
- The reply reports `total_us` and `max_edge_jitter_us` measured on-device.

## Receive

```sh
python host/ook.py --port PORT --radio 1 rx \
  --frequency-hz 303875000 --duration-ms 2000 \
  --threshold-dbm -80 --save-sub capture.sub
```

- `--bandwidth-hz` receive bandwidth (default 467 kHz, the widest — best for the
  frequency drift of cheap remotes).
- `--threshold-dbm` RSSI above which the carrier counts as "on". Start near the
  noise floor shown by a quiet capture and raise it until only the signal keys.
- `--save-sub` writes a Flipper RAW `.sub` you can inspect or replay with `tx`.
- The reply gives the pulse `durations_us`, `first_level`, and the achieved
  `poll_min_us`/`poll_max_us` (your timing resolution).

## Wire protocol (direct bridge users)

```
<id> ooktx <freq_hz> <lp|hp|hf> <power_dbm> <ramp_code> <repeat> <gap_us> <pulses_hex>
<id> ookrx <freq_hz> <bw_code> <duration_ms> <threshold_dbm_x2> <poll_us>
```

`pulses_hex` is big-endian u16 microseconds, on first. `threshold_dbm_x2` is
twice the dBm (RSSI is reported in half-dB). Both commands run on either radio,
over USB or TCP, and use the board's per-chip RF-switch table.

## Timing

Edge timing is set by the SPI command rate, so both paths run the bus at 16 MHz
(`host/ook.py` sets this automatically; firmware ≥ this build also raises it
internally). Measured on the XR4:

| Path | Default 500 kHz SPI | 16 MHz SPI |
| --- | --- | --- |
| TX max edge jitter | 77–92 µs | **29–49 µs** |
| RX poll interval | ~150 µs | **~83 µs** |

~30 µs jitter is under 10% of a 350 µs pulse, fine for typical remotes, gates
and sensors. Sub-10 µs edges or envelope sampling faster than ~80 µs would need
on-chip LR1121 code (not built — large, fragile, and unnecessary for these
protocols). A retune or PA ramp (`--ramp-us`) rounds the first edge of a burst.

## Not yet characterized

- Over-the-air sensitivity and range per band (matching is tuned for 868/915 MHz
  and 2.4 GHz; 300/433 MHz will be weaker). The RX pipeline and timing are
  verified; absolute sensitivity is not.
- A two-radio over-the-air loopback (TX on one chip, RX on the other) needs the
  board on Wi-Fi so both chips stream at once over TCP 1121/1122. Over a single
  USB-serial link only one runs at a time.
