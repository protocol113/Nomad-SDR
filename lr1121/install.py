#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""LR1121 1.3 application-only installer and FIFO1 extension builder.

CBC oracle construction adapted from radioegor146/lr-semtech-research;
see ../THIRD_PARTY_NOTICES.md. No bootloader-update opcode is implemented.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'host'))
from bridge import Bridge
from lr1121 import Core1121

VENDOR = ROOT / 'external/radio-firmware-images/lr1121/transceiver/lr1121_transceiver_0103.bin'
VENDOR_SHA = 'd7e199871884a52666c49cd443c94318ee3fabceb8563548f41ebbb4458e9645'
VENDOR_SIZE = 66592
BOOT = '22df2100'
APP = '22030103'
ELRS_APP = '22f30104'
ELRS_SIZE = 67648
ELRS_SHA = '5e58718f4db8927c3f626bcefbabdf4f4802984c92f3f4a5a8f8c10eafeb6acd'
FLASH_BASE, BASE, ENTRY = 0x84000, 0x94500, 0x94510


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def stock_image(path):
    data = path.read_bytes()
    if len(data) != VENDOR_SIZE or digest(data) != VENDOR_SHA:
        raise ValueError('Expected the checksum-pinned LR1121 transceiver 0x0103 image')
    return data


def payload_layout(code):
    if not 0 < len(code) <= 4080:
        raise ValueError('Extension must fit 16 encrypted pages')
    wanted = bytes(16) + code
    wanted += bytes((-len(wanted)) % 256)
    for base in range(256, len(wanted), 256):
        if any(wanted[base:base+16]):
            raise ValueError('Executable bytes in uncontrolled CBC hole')
    return wanted


def swap_words(data):
    if len(data) % 4:
        raise ValueError('Incomplete 32-bit word')
    return b''.join(data[i:i+4][::-1] for i in range(0, len(data), 4))


def read_words(b, address, count):
    if address % 4 or not 1 <= count <= 64 or not 0x800000 <= address < address+4*count <= 0x810000:
        raise ValueError('Read outside LR1121 RAM')
    r = b.read_command(b'\x01\x06' + address.to_bytes(4, 'big') + bytes([count]), 4*count)
    return b._payload(r, 4*count)


def word(b, address):
    return int.from_bytes(read_words(b, address, 1), 'big')


def checked_write(b, command):
    b.write_command(command)
    if b.status()['decoded']['cmd_status'] != 2:
        raise RuntimeError(f'Radio rejected opcode {command[:2].hex()}')


def mask(b, address, value):
    checked_write(b, b'\x01\x0c' + address.to_bytes(4, 'big') + bytes.fromhex('ffffffff') + value.to_bytes(4, 'big'))


def enter_bootloader(b):
    version = b.version()['data']
    if version == BOOT:
        return version  # Retry an interrupted application update.
    if version not in ('22030101', APP, ELRS_APP):
        raise RuntimeError('Unexpected application identity; no erase sent')
    b.write_command(bytes.fromhex('011803'))
    time.sleep(.1)
    version = b.version()['data']
    if version != BOOT:
        raise RuntimeError(f'Unexpected bootloader {version}; no erase sent')
    return version


def oracle(b, cipher, iv=bytes(16)):
    r = b.read_command(bytes.fromhex('050f00000000') + iv + cipher, 3)
    if r['cmd_status'] != 0:
        raise RuntimeError('Unexpected oracle status')
    return read_words(b, 0x805ae0, 64)[0x14:0x14+len(cipher)]


def forge(b, original, code, out):
    wanted = payload_layout(code)
    b.speed(500000); b.reset()
    if b.version()['data'] != APP:
        raise RuntimeError('CBC oracle requires application 1.3')
    cipher = bytearray(len(wanted)); recovered = bytearray()
    try:
        for base in range(0, len(wanted), 256):
            previous = bytes([0xaa])*16
            for offset in range(base+240, base, -16):
                cipher[offset:offset+16] = previous
                previous = swap_words(bytes(x ^ y for x, y in zip(oracle(b, previous), wanted[offset:offset+16])))
            cipher[base:base+16] = previous
            iv = (BASE-FLASH_BASE+base).to_bytes(4, 'little') + bytes(12)
            plain = oracle(b, cipher[base:base+128], iv) + oracle(b, cipher[base+128:base+256], cipher[base+112:base+128])
            if plain[16:] != wanted[base+16:base+256]:
                raise RuntimeError('Forged plaintext verification failed')
            recovered.extend(plain)
        image = original + bytes([0xaa])*(BASE-FLASH_BASE-len(original)) + cipher
        (out/'custom-app.encrypted.bin').write_bytes(image)
        (out/'payload-verified.bin').write_bytes(recovered)
        save(out/'image.json', dict(vendor_sha256=digest(original), image_sha256=digest(image),
             payload_sha256=digest(code), entry=hex(ENTRY), bytes=len(image), verified=True))
    finally:
        b.reset()


def validated_custom(original, image, meta, code):
    payload_layout(code)
    expected_size = BASE-FLASH_BASE+len(payload_layout(code))
    if (image[:len(original)] != original or len(image) != expected_size or
        image[len(original):BASE-FLASH_BASE] != bytes([0xaa])*(BASE-FLASH_BASE-len(original)) or
        meta.get('verified') is not True or meta.get('entry') != hex(ENTRY) or
        meta.get('vendor_sha256') != digest(original) or meta.get('payload_sha256') != digest(code) or
        meta.get('image_sha256') != digest(image) or meta.get('bytes') != len(image)):
        raise ValueError('Image/manifest/payload mismatch; no erase sent')
    return image


def flash(b, image, out, expected_app=APP):
    report = dict(complete=False, image_sha256=digest(image))
    try:
        b.speed(500000)
        # Do not reset first: a valid bootloader is also a recovery entry point.
        report['boot_before'] = enter_bootloader(b)
        checked_write(b, bytes.fromhex('8000'))  # Erase application only.
        for offset in range(0, len(image), 256):
            checked_write(b, bytes.fromhex('8003') + offset.to_bytes(4, 'big') + image[offset:offset+256])
        b.write_command(bytes.fromhex('800500')); time.sleep(.1)
        report['version'] = b.version()['data']
        if report['version'] != expected_app:
            raise RuntimeError(f'Application did not boot as expected identity {expected_app}')
        report['boot_after'] = enter_bootloader(b)
        b.write_command(bytes.fromhex('800500')); time.sleep(.1)
        report['complete'] = report['boot_before'] == report['boot_after'] == BOOT
        if not report['complete']:
            raise RuntimeError('Bootloader identity changed')
    finally:
        save(out/'flash-result.json', report)


def initialize_verification_radio(b):
    info = b.info()
    # Nomad and XR4 are crystal boards served by nomad/ firmware; both use the
    # same setup, differing only in the SetDioAsRfSwitch (0x0112) table. Only a
    # genuinely unknown board falls back to the Waveshare TCXO/LF-clock profile.
    from boards import BOARDS, rf_switch_command
    if info.get('board') not in BOARDS:
        return Core1121(b).initialize()
    if info.get('radio') not in (0, 1):
        raise RuntimeError('Invalid dual-LR1121 radio identity')
    # Callback self-test needs crystal standby, not RX or a DMA ring.
    from nomad_rx import NomadReceiver
    receiver = NomadReceiver(b)
    for command, mode in (('011c00', 1), ('011001', 1),
                          (rf_switch_command(info), 1), ('010e', 1),
                          ('010f3f', 1), ('011400800000', 1), ('011c01', 2)):
        receiver.write(command, mode)


def verify(b, out):
    result = dict(success=False)
    (out/'binding.cmake').unlink(missing_ok=True)
    try:
        b.speed(500000); b.reset()
        if b.version()['data'] != APP:
            raise RuntimeError('Extension requires firmware 1.3')
        initialize_verification_radio(b)
        uid = b._payload(b.read_command(bytes.fromhex('0125'), 8), 8).hex()
        callback = word(b, 0x8004ac)
        for address, value in ((0x80f000,0),(0x80f004,callback),(0x80f008,0),(0x80f078,0),
                               (0x8004ac,ENTRY),(0x8004bc,2)):
            mask(b, address, value)
        result.update(uid=uid, version=b.version()['data'], marker=hex(word(b,0x80f000)),
                      fifo_marker=hex(word(b,0x80f078)), callback_restored=word(b,0x8004ac)==callback)
        result['success'] = (result['version']==APP and result['marker']=='0x49515033' and
                             result['fifo_marker']=='0x46494631' and result['callback_restored'])
        if not result['success']:
            raise RuntimeError('FIFO1 extension self-test failed')
        (out/'binding.cmake').write_text(f'set(LR1121_VERIFIED_UID "{uid}" CACHE STRING "Verified LR1121 UID" FORCE)\n')
    finally:
        b.reset(); save(out/'verify.json', result)
    print(json.dumps(result))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('stock','forge','install','verify','restore-elrs'))
    p.add_argument('--port'); p.add_argument('--vendor',type=Path,default=VENDOR)
    p.add_argument('--radio', type=int, choices=(0, 1), help='Nomad radio on main ESP32 UART')
    p.add_argument('--elrs-image', type=Path, default=ROOT/'build/backups/lr1121-elrs-f30104.bin')
    p.add_argument('--payload',type=Path,default=ROOT/'build/lr1121/stream.bin')
    p.add_argument('--out',type=Path,default=ROOT/'build/lr1121')
    a=p.parse_args(); a.out.mkdir(parents=True,exist_ok=True)
    # Validate every local input before opening the device or issuing writes.
    original=stock_image(a.vendor) if a.action!='restore-elrs' else None
    code=a.payload.read_bytes() if a.action in ('forge','install') else None
    if code is not None:payload_layout(code)
    image=original
    if a.action=='restore-elrs':
        image=a.elrs_image.read_bytes()
        if len(image)!=ELRS_SIZE or digest(image)!=ELRS_SHA:
            raise ValueError('Expected checksum-pinned ELRS F30104 image; no device opened')
    if a.action=='install':
        image=validated_custom(original,(a.out/'custom-app.encrypted.bin').read_bytes(),
                               json.loads((a.out/'image.json').read_text()),code)
    with Bridge(port=a.port,timeout_ms=10000,log=a.out/(a.action+'-session.jsonl'),nomad_radio=a.radio) as b:
        if a.action=='forge':forge(b,original,code,a.out)
        elif a.action=='stock':flash(b,original,a.out)
        elif a.action=='restore-elrs':flash(b,image,a.out,expected_app=ELRS_APP)
        elif a.action=='install':flash(b,image,a.out);verify(b,a.out)
        else:verify(b,a.out)

if __name__=='__main__':main()
