#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Update Nomad/XR4 ESP32 firmware over the control UART, without the boot button.

Works whenever the SDR firmware (0.11.0+ with fwupdate) is running, including
safe mode and when Wi-Fi is down. The device verifies the MD5 before switching
to the new image; any failure leaves the running firmware in place.

    python tools/serial_update.py --port /dev/cu.usbserial-0001 nomad/.pio/build/radiomaster_xr4/firmware.bin
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import serial


def reply(port, timeout=10.0):
    """Next JSON line from the device; skips non-JSON console text."""
    deadline = time.monotonic() + timeout
    line = b''
    while time.monotonic() < deadline:
        chunk = port.readline()
        if not chunk:
            continue
        line += chunk
        if not line.endswith(b'\n'):
            continue
        text = line.decode('latin1').strip()
        line = b''
        if text.startswith('{'):
            return json.loads(text)
    raise TimeoutError('No reply from device')


def update(port_name, image, baud=921600):
    data = Path(image).read_bytes()
    md5 = hashlib.md5(data).hexdigest()
    with serial.Serial(port_name, baud, timeout=0.5) as port:
        port.dtr = False
        port.rts = False
        time.sleep(0.1)
        port.reset_input_buffer()
        port.write(f'91 fwupdate {len(data)} {md5}\n'.encode())
        ready = reply(port)
        if not ready.get('ready'):
            raise RuntimeError(f'Device refused update: {ready}')
        chunk = int(ready.get('chunk', 4096))
        start = time.monotonic()
        for offset in range(0, len(data), chunk):
            port.write(data[offset:offset + chunk])
            ack = reply(port)
            if not ack.get('ok') or ack.get('offset') != min(offset + chunk, len(data)):
                raise RuntimeError(f'Update failed at {offset}: {ack}')
            print(f'\r{ack["offset"]}/{len(data)} bytes', end='', flush=True)
        done = reply(port, timeout=30)
        print()
        if not done.get('done'):
            raise RuntimeError(f'Device rejected image: {done}')
        return dict(bytes=len(data), md5=md5, seconds=round(time.monotonic() - start, 1))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--port', required=True, help='Control UART, e.g. COM3 or /dev/cu.usbserial-0001')
    p.add_argument('--baud', type=int, default=921600)
    p.add_argument('image', type=Path, help='Application image (firmware.bin, not the merged image)')
    a = p.parse_args(argv)
    try:
        print(json.dumps(update(a.port, a.image, a.baud)))
        print('Device is rebooting into the new firmware.')
        return 0
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f'\nUpdate failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
