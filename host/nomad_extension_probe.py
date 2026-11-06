#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Receive-only experimental probe of a verified FIFO1 extension on Nomad.

Requires install.py's successful per-chip verify.json. Does not flash firmware.
Known-vector DSP checks run in LR1121 RAM; optional short RF snapshot uses a
16-bank packed ring. This is not an indefinite streaming implementation.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lr1121'))
import install as extension
from bridge import Bridge
from nomad_rx import NomadReceiver, descriptor
from frequency import valid_frequency
from boards import BOARDS


def write_words(b, address, values):
    values = list(values)
    if address % 4 or not 0x800000 <= address < address + 4*len(values) <= 0x810000:
        raise ValueError('Write outside LR1121 RAM')
    for offset in range(0, len(values), 64):
        chunk = values[offset:offset+64]
        command = b'\x01\x05' + (address+4*offset).to_bytes(4, 'big')
        extension.checked_write(b, command + struct.pack('>'+'I'*len(chunk), *chunk))


def wake_callback(b):
    NomadReceiver(b).masked(0x8004bc, 2, 2)


def start_packer(b, callback, bits, shift=None):
    state = [0x49515033, callback, 1, 0, 0, bits, 0,
             0 if shift is None else 256 | shift] + [0]*24
    state[30] = 0x46494631
    write_words(b, 0x80f000, state)
    write_words(b, 0x80f100, [0]*16)
    extension.mask(b, 0x8004ac, extension.ENTRY)
    wake_callback(b)


def expected_packing(values, bits, shift):
    if bits == 8:
        return bytes(max(-128, min(127, value >> shift)) & 255 for value in values)
    payload = bytearray()
    for i, q in zip(values[::2], values[1::2]):
        i >>= shift; q >>= shift
        payload.extend((i & 255, ((i >> 8) & 15) | ((q << 4) & 240), (q >> 4) & 255))
    return bytes(payload)


def vector_test(b, bits):
    b.reset(); extension.initialize_verification_radio(b)
    callback = extension.word(b, 0x8004ac)
    values = ([-4194304, -32768, -256, -1, 0, 1, 255, 256, 32767, 4194303]*25)
    write_words(b, 0x806000, [value & 0x7fffff for value in values])
    extension.mask(b, descriptor(0), 0)  # Completed bank, with ADC/DMA idle.
    start_packer(b, callback, bits, shift=15 if bits == 8 else None)
    deadline = time.monotonic()+2
    while not extension.word(b, 0x80f100) & 0x80000000:
        if time.monotonic() >= deadline:
            raise RuntimeError('ARC packer did not complete the vector bank')
    header = extension.word(b, 0x80f100)
    shift = (header >> 24) & 31
    expected = expected_packing(values, bits, shift)
    packed = bytearray()
    for offset in range(0, (4+len(expected)+3)//4, 64):
        count = min(64, (4+len(expected)+3)//4-offset)
        packed.extend(extension.swap_words(extension.read_words(b, 0x806000+4*offset, count)))
    if struct.unpack_from('<I', packed)[0] != header or packed[4:4+len(expected)] != expected:
        raise RuntimeError('ARC output differs from signed-vector reference')
    return dict(bits=bits, shift=shift, components=len(values), matched=True,
                sequence=header & 0xffffff, bytes=len(expected),
                sha256=hashlib.sha256(expected).hexdigest())


def prepare_packed(b, frequency, rate, verified):
    if (verified.get('success') is not True or verified.get('version') != extension.APP
            or verified.get('marker') != '0x49515033' or verified.get('fifo_marker') != '0x46494631'
            or verified.get('callback_restored') is not True):
        raise RuntimeError('Successful FIFO1 verification is required')
    uid = b._payload(b.read_command(bytes.fromhex('0125'), 8), 8).hex()
    if uid != verified.get('uid'):
        raise RuntimeError('Extension verification belongs to a different chip')
    receiver = NomadReceiver(b)
    metadata = receiver.configure(frequency, rate)  # Original-layout guards, gate off.
    b.speed(500000)
    callback = extension.word(b, 0x8004ac)
    # Recheck the installed executable before enabling its continuous callback.
    for address, value in ((0x80f000,0),(0x80f004,callback),(0x80f008,0),(0x80f078,0),
                           (0x8004ac,extension.ENTRY)):
        extension.mask(b,address,value)
    wake_callback(b)
    if (extension.word(b,0x80f000)!=0x49515033 or extension.word(b,0x80f078)!=0x46494631
            or extension.word(b,0x8004ac)!=callback):
        raise RuntimeError('Installed ARC callback self-test failed')
    ring = []
    for bank in range(16):
        ring.extend((0x9fe3e7a7, 0x901004, 0x806000+bank*2048+996, descriptor((bank+1)%16)))
        write_words(b, 0x806000+bank*2048, [0])
    write_words(b, 0x80e000, ring)
    if receiver.memory(0x80e000, 64) != ring:
        raise RuntimeError('Packed ring scratch readback mismatch')
    for index, value in enumerate(ring[:8]):
        extension.mask(b, 0x800a00+4*index, value)
    if receiver.memory(0x800a00, 8) != ring[:8] or receiver.memory(0x800580)[0] != 0x800a00:
        raise RuntimeError('Packed ring head mismatch')
    start_packer(b, callback, 8)  # Adaptive block scaling.
    b.speed(16000000)
    metadata.update(acquisition='arc8',packed_radio_uid=uid,packed_banks=16)
    return metadata


def rf_snapshot(b, frequency, out, verified):
    prepare_packed(b,frequency,62500,verified)
    receiver=NomadReceiver(b)
    receiver.masked(0xf20014, 0x30000010, 0x30000010)
    time.sleep(.008)
    receiver.masked(0xf20014, 0x30000010, 0x30000000)
    time.sleep(.01)
    ready = receiver.memory(0x80f100, 16)
    chunks = []
    sequences = []
    shifts = []
    for bank, marker in enumerate(ready):
        if not marker & 0x80000000:
            continue
        packed = extension.swap_words(extension.read_words(b, 0x806000+bank*2048, 64))
        if struct.unpack_from('<I', packed)[0] != marker:
            raise RuntimeError('Packed bank marker changed during read')
        sequence = marker & 0xffffff
        shift = (marker >> 24) & 31
        if shift > 16 or sequence != len(sequences)+1:
            raise RuntimeError('Unexpected sequence or scale in packed RF bank')
        components = struct.unpack('250b', packed[4:254])
        chunks.append(struct.pack('<250f', *(v*2.0**(shift-22) for v in components)))
        sequences.append(sequence); shifts.append(shift)
    if not chunks:
        raise RuntimeError('No packed RF banks were produced')
    if len(chunks) == 16:
        raise RuntimeError('Entire ring filled; shorten snapshot to avoid unverified DMA stall')
    with out.open('xb') as stream:
        stream.write(b''.join(chunks))
    return dict(frequency_hz=frequency, sample_rate=62500, format='cf32_le',
                banks=len(chunks), samples=len(chunks)*125, shifts=shifts,
                sequences=sequences, file=str(out), finite_snapshot=True,
                rf_sensitivity_verified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='COM3')
    parser.add_argument('--radio', type=int, choices=(0, 1), required=True)
    parser.add_argument('--verification', type=Path, required=True)
    parser.add_argument('--frequency', type=int, default=915000000)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if not valid_frequency(args.frequency):
        parser.error('Frequency must be within 150..2500 MHz')
    verified = json.loads(args.verification.read_text())
    if verified.get('success') is not True or verified.get('version') != extension.APP:
        raise RuntimeError('Successful extension verification is required')
    args.out.mkdir(parents=True, exist_ok=True)
    report_path = args.out/'probe.json'
    iq_path = args.out/'snapshot.cf32'
    if report_path.exists() or iq_path.exists():
        raise FileExistsError('Choose a new output directory; experiment results are never overwritten')
    report = dict(complete=False, radio=args.radio, flash_modified=False)
    try:
        with Bridge(args.port, nomad_radio=args.radio, log=args.out/'session.jsonl') as b:
            try:
                b.speed(500000); b.reset()
                info = b.info()
                if info.get('board') not in BOARDS or info.get('radio') != args.radio:
                    raise RuntimeError('Expected the selected dual-LR1121 board radio')
                if b.version()['data'] != extension.APP:
                    raise RuntimeError('Unexpected radio firmware')
                uid = b._payload(b.read_command(bytes.fromhex('0125'), 8), 8).hex()
                if uid != verified.get('uid'):
                    raise RuntimeError('Extension verification belongs to a different chip')
                report['vectors'] = [vector_test(b, bits) for bits in (8, 12)]
                b.reset()
                report['snapshot'] = rf_snapshot(b, args.frequency, iq_path, verified)
                report['complete'] = True
            finally:
                b.reset()
                report['version_after'] = b.version()['data']
    except Exception as exc:
        report['complete'] = False
        report['error'] = str(exc)
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
