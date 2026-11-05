#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""OOK (on-off keying) transmit and receive for the Nomad/XR4 bridge firmware.

The LR1121 has no native OOK modem. The firmware keys an unmodulated carrier
(SetTxCw/SetStandby) on a precise ESP32 timer for TX, and polls instantaneous
RSSI for an RX envelope. This host tool builds the wire commands, converts pulse
lists to/from Flipper Zero `.sub` RAW files, and talks to the board over USB/TCP.

Frequency is the chip's full synthesizer range (150..2500 MHz). On-air power,
duty cycle and band legality at the chosen frequency are the operator's
responsibility (e.g. FCC Part 15.231 for 300/433 MHz control devices).
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

# LR1121 PA ramp times (µs); index is the on-wire ramp code. Matches host/tx.py.
RAMP_US = (16, 32, 48, 64, 80, 96, 112, 128, 144, 160, 176, 192, 208, 240, 272, 304)
# PA profile power ranges (dBm), mirrored from nomad/src/ook.c.
PA_PROFILES = {'lp': (-17, 14), 'hp': (-9, 22), 'hf': (-18, 13)}
# GFSK RX bandwidth (Hz) -> LR1121 code, from host/nomad_control.py GFSK_BW.
GFSK_BW = {4800: 31, 5800: 23, 7300: 15, 9700: 30, 11700: 22, 14600: 14, 19500: 29,
           23400: 21, 29300: 13, 39000: 28, 46900: 20, 58600: 12, 78200: 27, 93800: 19,
           117300: 11, 156200: 26, 187200: 18, 234300: 10, 312000: 25, 373600: 17, 467000: 9}
WIDEST_BW_HZ = 467000
MAX_PULSES = 2048
MAX_PULSE_US = 32768
MAX_TX_TOTAL_US = 2_000_000


def ramp_code(us: int) -> int:
    """Nearest ramp code at or above the requested microseconds."""
    for code, value in enumerate(RAMP_US):
        if value >= us:
            return code
    return len(RAMP_US) - 1


def bw_code(hz: int) -> int:
    """Smallest GFSK bandwidth code whose bandwidth is >= hz (widest if none)."""
    for width in sorted(GFSK_BW):
        if width >= hz:
            return GFSK_BW[width]
    return GFSK_BW[WIDEST_BW_HZ]


def validate_pulses(pulses):
    if not pulses or len(pulses) < 2:
        raise ValueError('Need at least two pulses (one on, one off)')
    if len(pulses) > MAX_PULSES:
        raise ValueError(f'At most {MAX_PULSES} pulses')
    for d in pulses:
        if type(d) is not int or not 1 <= d <= MAX_PULSE_US:
            raise ValueError(f'Each pulse must be 1..{MAX_PULSE_US} µs, got {d!r}')
    return pulses


def pulses_to_hex(pulses) -> str:
    """Big-endian u16 microseconds, alternating on/off starting with on."""
    return ''.join('%04x' % d for d in validate_pulses(pulses))


def hex_to_pulses(text: str):
    if len(text) % 4:
        raise ValueError('Pulse hex must be a multiple of 4 characters (u16 each)')
    return [int(text[i:i + 4], 16) for i in range(0, len(text), 4)]


def parse_sub(text: str):
    """Parse a Flipper Zero RAW .sub file.

    Returns (frequency_hz, durations, first_level) where durations are absolute
    microseconds alternating on/off and first_level is 1 if the file starts with
    a carrier-on (positive) sample. Consecutive same-sign samples are merged.
    """
    frequency = None
    raw = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('Frequency:'):
            frequency = int(line.split(':', 1)[1].strip())
        elif line.startswith('RAW_Data:'):
            raw.extend(int(v) for v in line.split(':', 1)[1].split())
    if frequency is None:
        raise ValueError('No Frequency: field in .sub file')
    if not raw:
        raise ValueError('No RAW_Data in .sub file')
    first_level = 1 if raw[0] > 0 else 0
    durations, sign = [], None
    for v in raw:
        s = v > 0
        if sign is None or s != sign:
            durations.append(abs(v))
            sign = s
        else:
            durations[-1] += abs(v)   # merge consecutive same-sign samples
    return frequency, durations, first_level


def durations_to_sub(frequency_hz: int, durations, first_level: int = 1,
                     preset: str = 'FuriHalSubGhzPresetOok650Async') -> str:
    """Render durations as a Flipper RAW .sub file (on positive, off negative)."""
    signed, level = [], first_level
    for d in durations:
        signed.append(d if level else -d)
        level ^= 1
    body = ' '.join(str(v) for v in signed)
    return (f'Filetype: Flipper SubGhz RAW File\nVersion: 1\n'
            f'Frequency: {frequency_hz}\nPreset: {preset}\nProtocol: RAW\n'
            f'RAW_Data: {body}\n')


def tx_command(frequency_hz, pa, power_dbm, ramp_us, repeat, gap_us, pulses) -> str:
    if pa not in PA_PROFILES:
        raise ValueError('pa must be lp, hp or hf')
    lo, hi = PA_PROFILES[pa]
    if not lo <= power_dbm <= hi:
        raise ValueError(f'{pa} PA power range is {lo}..{hi} dBm')
    hexp = pulses_to_hex(pulses)
    total = (sum(pulses) + gap_us) * repeat
    if total > MAX_TX_TOTAL_US:
        raise ValueError(f'Total on-air time {total} µs exceeds {MAX_TX_TOTAL_US} µs ceiling')
    return f'ooktx {frequency_hz} {pa} {power_dbm} {ramp_code(ramp_us)} {repeat} {gap_us} {hexp}'


def rx_command(frequency_hz, bandwidth_hz, duration_ms, threshold_dbm, poll_us) -> str:
    return (f'ookrx {frequency_hz} {bw_code(bandwidth_hz)} {duration_ms} '
            f'{round(threshold_dbm * 2)} {poll_us}')


def _bridge_timeout_ms(firmware_us: int) -> int:
    # Bridge deadline = 2*timeout_ms/1000 + 3 s; leave margin over firmware time.
    return max(1000, int(firmware_us / 1000 / 2) + 2000)


def _connect(args, firmware_us):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from bridge import Bridge
    from boards import check_board
    b = Bridge(args.port, timeout_ms=_bridge_timeout_ms(firmware_us),
               nomad_radio=args.radio, transport=None)
    check_board(b.info())
    # Key/poll at 16 MHz SPI so a command frame is ~1 µs; this is the edge-timing
    # floor. Firmware >= this build also raises it internally, so this is a no-op
    # there and a large win on any build that defaulted to 500 kHz.
    b.speed(16000000)
    return b


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port'); p.add_argument('--radio', type=int, choices=(0, 1), default=0)
    sub = p.add_subparsers(dest='action', required=True)

    t = sub.add_parser('tx', help='transmit a pulse list or a .sub file')
    t.add_argument('--frequency-hz', type=int, required=True)
    t.add_argument('--pa', choices=('lp', 'hp', 'hf'), default='lp')
    t.add_argument('--power-dbm', type=int, default=-9)
    t.add_argument('--ramp-us', type=int, default=48)
    t.add_argument('--repeat', type=int, default=5)
    t.add_argument('--gap-us', type=int, default=10000)
    t.add_argument('--pulses', help='comma-separated microseconds, on first')
    t.add_argument('--sub', type=Path, help='Flipper RAW .sub to replay (sets frequency)')

    r = sub.add_parser('rx', help='capture an OOK envelope')
    r.add_argument('--frequency-hz', type=int, required=True)
    r.add_argument('--bandwidth-hz', type=int, default=WIDEST_BW_HZ)
    r.add_argument('--duration-ms', type=int, default=2000)
    r.add_argument('--threshold-dbm', type=float, default=-90.0)
    r.add_argument('--poll-us', type=int, default=30)
    r.add_argument('--save-sub', type=Path, help='write the capture as a .sub file')

    a = p.parse_args(argv)
    if a.action == 'tx':
        freq = a.frequency_hz
        if a.sub:
            freq, pulses, _ = parse_sub(a.sub.read_text())
        elif a.pulses:
            pulses = [int(x) for x in a.pulses.split(',')]
        else:
            p.error('tx needs --pulses or --sub')
        cmd = tx_command(freq, a.pa, a.power_dbm, a.ramp_us, a.repeat, a.gap_us, pulses)
        firmware_us = (sum(pulses) + a.gap_us) * a.repeat
        with _connect(a, firmware_us) as b:
            print(b.request(cmd))
    else:
        cmd = rx_command(a.frequency_hz, a.bandwidth_hz, a.duration_ms, a.threshold_dbm, a.poll_us)
        with _connect(a, a.duration_ms * 1000) as b:
            result = b.request(cmd)
        print(result)
        if a.save_sub:
            a.save_sub.write_text(durations_to_sub(
                a.frequency_hz, result['durations_us'], result.get('first_level', 1)))
            print(f'wrote {a.save_sub}')


if __name__ == '__main__':
    main()
