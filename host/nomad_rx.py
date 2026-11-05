#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""RadioMaster Nomad SDR capture (Windows/Linux/macOS).

Receive configuration/ring logic adapted from sdrpp/lr1121_source/src/device.cpp
(GPL-3.0-only). Firmware streaming remains MIT; see docs/licensing.md.
"""
from __future__ import annotations

import argparse
import array
import concurrent.futures
import json
import math
from pathlib import Path
import struct
import sys
import time
import zlib

from bridge import Bridge, BridgeError
from lr1121 import RadioError
from frequency import valid_frequency, receiver_band
from boards import BoardError, rf_switch_command

HEADER = struct.Struct('<4sBBH9I')
CONTROL = 0x9FE3E7A7
RATES = (62500, 125000)


def descriptor(index):
    return 0x800A00 + index * 16 if index < 2 else 0x80E000 + index * 16


class NomadReceiver:
    def __init__(self, bridge):
        self.b = bridge

    def write(self, command, mode=None):
        self.b.write_command(bytes.fromhex(command))
        state = self.b.status()['decoded']
        if state['cmd_status'] != 2 or (mode is not None and state['chip_mode'] != mode):
            raise RadioError(f'Radio rejected configuration {command}', state)

    def memory(self, address, count=1):
        command = b'\x01\x06' + address.to_bytes(4, 'big') + bytes([count])
        payload = self.b._payload(self.b.read_command(command, count * 4), count * 4)
        return list(struct.unpack(f'>{count}I', payload))

    def masked(self, address, mask, value):
        self.write(f'010c{address:08x}{mask:08x}{value:08x}')

    def configure(self, frequency, rate):
        # Expose the synthesizer's range; this is not a claim of uniform RF
        # coverage through the Nomad's 900 MHz / 2.4 GHz matching network.
        if not valid_frequency(frequency):
            raise ValueError('LR1121 synthesizer frequency must be 150..2500 MHz')
        if rate not in RATES:
            raise ValueError('Nomad rate must be 62500 or 125000')
        info = self.b.info()
        try:
            rf_switch = rf_switch_command(info)
        except BoardError as error:
            raise RadioError(str(error), info) from None
        if info.get('radio') not in (0, 1):
            raise RadioError('Expected a dual-LR1121 bridge radio 0 or 1', info)
        version = self.b.version()
        if version.get('data') != '22030103':
            raise RadioError('SDR requires stock LR1121 transceiver 1.3 (22030103); '
                             'no RAM patches written. See docs/nomad.md for provisioning.', version)
        self.b.speed(500000)
        self.b.reset()
        self.write('011c00', 1)
        self.write('011001', 1)
        # Per-board ELRS switch table (host/boards.py), NOT the Waveshare one.
        # No TCXO or external 32 kHz clock setup: absent in Nomad and XR4 targets.
        self.write(rf_switch, 1)
        self.write('010e', 1)
        self.write('010f3f', 1)
        self.write('011400800000', 1)
        self.write('011c01', 2)
        # Follow the upstream tested image-calibration range. Lower frequencies
        # retain reset calibration; the 150 MHz calibration command is rejected.
        if 400_000_000 <= frequency <= 1_000_000_000:
            low = (frequency // 1_000_000 - 4) // 4
            high = (frequency // 1_000_000 + 7) // 4
            self.write(f'0111{low:02x}{high:02x}', 1)
            self.write('011c01', 2)
        self.write('020e01', 2)
        self.write(f'020b{frequency:08x}', 2)
        if frequency >= 2_300_000_000:
            self.write('0229764643cececc8c980907ee',2)
        elif 850_000_000 <= frequency <= 930_000_000:
            self.write('02292232434564556676060000',2)
        elif 410_000_000 <= frequency <= 510_000_000:
            self.write('0229cc0e314463666666060000',2)
        # GFSK filter selects the native 62.5/125 kS/s decimation setting.
        self.write('020f0000271009' + ('0d' if rate == 62500 else '0c') + '00001388', 2)
        self.write('0210002004200001ff0101', 2)
        self.write('02061234567800000000', 2)
        value = self.memory(0xF20124)[0]
        if ((value >> 20) & 7) != (5 if rate == 62500 else 4) or ((value >> 24) & 3) != 1:
            raise RadioError('Unexpected native rate; no ring written')
        self.write('0209ffffff', 4)
        time.sleep(.03)
        if self.b._payload(self.b.read_command(bytes.fromhex('010d'), 2), 2) != b'\0\0':
            raise RadioError('Radio error after receive setup')
        if self.memory(0xF30058)[0] != ((frequency * 16384 // 15625) | 1):
            raise RadioError('PLL frequency readback mismatch')
        self.install_ring()
        speed = self.b.speed(16_000_000)
        if speed['spi_hz'] != 16_000_000:
            raise RadioError('Unexpected ESP32 SPI clock', speed)
        return dict(board=info['board'], radio=info['radio'], frequency_hz=frequency,
                    sample_rate=rate, format='cf32_le', filter='wide_gfsk',
                    lr1121_version=version['data'], spi_hz=speed['spi_hz'],
                    receiver_band=receiver_band(frequency),
                    image_calibrated=400_000_000 <= frequency <= 1_000_000_000)

    def install_ring(self):
        gp = self.memory(0x800498, 6)
        state = self.memory(0x800534, 3)
        original = self.memory(0x800A00, 8)
        expected = [0xBFE0FFA7, 0x901004, 0x800324, 0x800A10,
                    0xBFE0FFA7, 0x901004, 0x800324, 0x800A00]
        if gp[1] >> 16 != 0x0104 or (state[1] >> 16) & 255 or original != expected:
            raise RadioError('RX DMA layout differs; no ring installed')
        self.masked(0xF20014, 0x30000010, 0x30000000)
        ring = []
        for i in range(32):
            ring.extend([CONTROL, 0x901004, 0x806000 + (i + 1) * 1000 - 4,
                         descriptor((i + 1) % 32)])
        for start in (0, 64):
            self.write(f'0105{0x80E000 + start * 4:08x}' +
                       ''.join(f'{x:08x}' for x in ring[start:start + 64]))
        for start in (0, 64):
            if self.memory(0x80E000 + start * 4, 64) != ring[start:start + 64]:
                raise RadioError('Ring scratch readback mismatch')
        for i in range(8):
            self.masked(0x800A00 + i * 4, 0xFFFFFFFF, ring[i])
        if self.memory(0x800A00, 8) != ring[:8] or self.memory(0x800580)[0] != 0x800A00:
            raise RadioError('Ring head readback mismatch')


def read_exact(io, count):
    result = bytearray()
    deadline = time.monotonic() + 5
    while len(result) < count:
        result.extend(io.read(count - len(result)))
        if time.monotonic() >= deadline:
            raise BridgeError('I/Q transport timeout; capture incomplete')
    return bytes(result)


def read_frame(io, job_id, rate, sequence, compact=False):
    header = read_exact(io, HEADER.size)
    magic, version, kind, flags, ident, seq, index, samples, length, sample_rate, stamp, crc, hcrc = HEADER.unpack(header)
    decimated=compact and magic==b'LID8'
    if magic not in ((b'LIQ8',b'LID8') if compact else (b'LIQ1',)) or version != 1 or kind not in (1, 2) or zlib.crc32(header[:40]) != hcrc:
        raise BridgeError('Invalid LIQ1 header/CRC')
    expected_index=((sequence*125+1)//2 if decimated else sequence*125)&0xffffffff
    count=(63 if not sequence%2 else 62) if decimated else 125
    if (ident, sample_rate, seq, index) != (job_id, rate, sequence, expected_index):
        raise BridgeError('I/Q stream ID, rate, or sequence gap')
    if samples != (count if kind == 1 else 0) or length != ((1+count*2 if compact else 750) if kind == 1 else 36):
        raise BridgeError('Invalid LIQ1 dimensions')
    if kind == 1 and flags:
        raise BridgeError('Unexpected LIQ1 data flags')
    payload = read_exact(io, length)
    if zlib.crc32(payload) != crc:
        raise BridgeError('I/Q payload CRC mismatch')
    if kind == 2:
        counters = struct.unpack('<9I', payload)
        if counters[:2] != (sequence, sequence):
            raise BridgeError('Terminal frame counts disagree')
        return None, dict(flags=flags, produced=counters[0], sent=counters[1],
                          max_queue=counters[2], read_ops=counters[3], max_block_us=counters[4],
                          spi_hz=counters[5], elapsed_us=counters[6], lost_samples=counters[7],
                          requested_blocks=counters[8], timestamp_us=stamp)
    components = array.array('f')
    if compact:
        shift=payload[0]
        if shift>16:
            raise BridgeError('Invalid LIQ8 scale')
        scale=2.0**(shift-22)
        components.extend(v*scale for v in array.array('b',payload[1:]))
        if sys.byteorder!='little':
            components.byteswap()
        return components.tobytes(),None
    for offset in range(0, length, 3):
        value = int.from_bytes(payload[offset:offset + 3], 'little')
        if value & 0x800000:
            raise BridgeError('Invalid signed-23 sample container')
        components.append(((value & 0x3FFFFF) - (value & 0x400000)) / 4194304.0)
    if sys.byteorder != 'little':
        components.byteswap()
    return components.tobytes(), None


def capture(host, radio, frequency, rate, seconds, output, barrier=None, port=None, compact=False,decimate=False,packed_verification=None):
    compact = compact or decimate or packed_verification is not None
    verified = json.loads(Path(packed_verification).read_text()) if packed_verification is not None else None
    if rate not in RATES or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('Select a supported rate and positive finite duration')
    blocks = math.ceil(seconds * rate / 125)
    if not 1 <= blocks <= 8192:
        raise ValueError('Capture must fit 1..8192 blocks; shorten --seconds')
    if port is not None and not compact and blocks > 32:
        raise ValueError('UART LIQ1 smoke capture is limited to 32 blocks; use --compact or --seconds 0.032 at 62500 S/s')
    if verified is not None and port is not None and (rate==125000 or not decimate) and blocks>128:
        raise ValueError('Packed UART capture above transport capacity is limited to 128 blocks; use --decimate at 62500 or TCP')
    endpoint = port or f'socket://{host}:{1121 + radio}'
    metadata = dict(complete=False, endpoint=endpoint, blocks=blocks)
    if compact:
        metadata['wire_format']='LID8' if decimate else 'LIQ8'
    output = Path(output)
    sidecar = output.with_suffix(output.suffix + '.json')
    # Reserve both files before touching the radio; never overwrite a capture.
    try:
        with output.open('xb') as data, sidecar.open('x', encoding='utf-8') as report:
            try:
                kwargs = dict(nomad_radio=radio) if port is not None else {}
                with Bridge(endpoint, timeout_ms=1500, **kwargs) as bridge:
                    if verified is not None:
                        from nomad_extension_probe import prepare_packed
                        metadata.update(prepare_packed(bridge,frequency,rate,verified))
                    else:
                        metadata.update(NomadReceiver(bridge).configure(frequency, rate))
                    if decimate:
                        compact=True
                        metadata.update(wire_format='LID8',acquisition_rate=rate,sample_rate=rate//2)
                    if barrier is not None:
                        barrier.wait(timeout=60)
                    command = 'streampd' if decimate else 'streamp8'
                    wire = f'{command} {rate} {blocks} {verified["uid"]}' if verified is not None else f'{"streamd" if decimate else "stream8" if compact else "stream2"} {rate} {blocks}'
                    ack = bridge.request(wire)
                    if verified is not None and ack.get('acquisition')!='arc8':
                        raise BridgeError('Expected ARC packed acquisition acknowledgement',ack)
                    if ack.get('stream') != ('LID8' if decimate else 'LIQ8' if compact else 'LIQ1') or ack.get('format') != ('iq8_block_float' if compact else 'iq23le'):
                        raise BridgeError('Invalid Nomad stream acknowledgement', ack)
                    sequence = 0
                    while True:
                        samples, stats = read_frame(bridge.io, bridge._id, rate//2 if decimate else rate, sequence, compact=compact)
                        if stats is not None:
                            metadata['stats'] = stats
                            if stats['flags'] or stats['lost_samples'] or sequence != blocks:
                                raise BridgeError(f'Capture stopped with loss/error: {stats}')
                            metadata['complete'] = True
                            metadata['samples'] = (sequence*125+1)//2 if decimate else sequence*125
                            break
                        data.write(samples)
                        sequence += 1
            except BaseException as exc:
                metadata['error'] = str(exc)
                if barrier is not None:
                    barrier.abort()
                raise
            finally:
                json.dump(metadata, report, indent=2)
                report.write('\n')
    except BaseException:
        if barrier is not None:
            barrier.abort()
        raise
    return metadata


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', default='192.168.4.1')
    p.add_argument('--compact', action='store_true', help='Use LIQ8 block floating I/Q for live networking')
    p.add_argument('--decimate', action='store_true', help='Anti-alias FIR /2 and compact LID8 networking')
    p.add_argument('--packed-verification',type=Path,help='Experimental ARC acquisition: per-chip successful FIFO1 verify.json')
    p.add_argument('--port', help='Main ESP32 USB UART, e.g. COM3; compact streams are USB native mode')
    p.add_argument('--radio', choices=('0', '1', 'both'), default='0')
    p.add_argument('--frequency', type=int, default=915_000_000)
    p.add_argument('--frequency2', type=int, default=2_440_000_000, help='Radio 1 frequency in both mode')
    p.add_argument('--rate', type=int, choices=RATES, default=62500)
    p.add_argument('--seconds', type=float, default=2)
    p.add_argument('--output', type=Path, required=True, help='cf32_le file; both mode adds .radio0/.radio1')
    a = p.parse_args(argv)
    if a.packed_verification is not None and a.radio=='both':
        p.error('Experimental packed capture requires selecting one verified radio')
    if a.radio == 'both' and a.rate != 62500:
        p.error('Dual capture supports 62500 S/s per radio; SPI bandwidth is shared')
    if a.port is not None and a.radio == 'both':
        p.error('UART uses one radio per capture; use TCP for simultaneous capture')
    try:
        if a.radio == 'both':
            import threading
            barrier = threading.Barrier(2)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(capture, a.host, i, a.frequency if i == 0 else a.frequency2,
                                    a.rate, a.seconds,
                                    a.output.with_name(a.output.stem + f'.radio{i}' + a.output.suffix),
                                    barrier,compact=a.compact,decimate=a.decimate) for i in range(2)]
                results = [job.result() for job in jobs]
        else:
            results = [capture(a.host, int(a.radio), a.frequency, a.rate, a.seconds, a.output, port=a.port,compact=a.compact,decimate=a.decimate,packed_verification=a.packed_verification)]
        print(json.dumps(results, indent=2))
        return 0
    except (BridgeError, RadioError, OSError, ValueError, RuntimeError) as exc:
        print(f'Capture failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
