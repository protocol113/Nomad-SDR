# SPDX-License-Identifier: GPL-3.0-only
import io
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import Bridge, BridgeError
from lr1121 import Core1121, RadioError
from nomad_rx import CONTROL, HEADER, NomadReceiver, capture, descriptor, main, read_frame


def frame(kind=1, seq=0, flags=0, payload=None, ident=1, rate=62500):
    if payload is None:
        payload = bytes(750) if kind == 1 else struct.pack('<9I', seq, seq, 1, 4, 100, 16000000, 2000, 0, seq)
    header = HEADER.pack(b'LIQ1', 1, kind, flags, ident, seq, seq * 125,
                         125 if kind == 1 else 0, len(payload), rate, 2000, zlib.crc32(payload), 0)
    return header[:40] + struct.pack('<I', zlib.crc32(header[:40])) + payload


class RadioModel:
    """Independent register responses for the firmware-1.3 preconditions."""
    def __init__(self, version='22030103'):
        self.ver = version
        self.commands = []
        self.mode = 1
        self.ram = {0xF20124: 0x01500000, 0x80049C: 0x01040000, 0x800538: 0,
                    0x800580: 0x800A00}
        original = [0xBFE0FFA7, 0x901004, 0x800324, 0x800A10,
                    0xBFE0FFA7, 0x901004, 0x800324, 0x800A00]
        self.ram.update({0x800A00 + 4 * i: x for i, x in enumerate(original)})

    def info(self):
        return dict(board='radiomaster-nomad', radio=0)

    def version(self):
        return dict(data=self.ver)

    def speed(self, hz):
        self.commands.append(('speed', hz))
        return dict(spi_hz=hz)

    def reset(self):
        self.commands.append(('reset',))

    def write_command(self, command):
        self.commands.append(command)
        op, data = command[:2].hex(), command[2:]
        if op == '011c':
            self.mode = 1 + data[0]
        elif op == '0111':
            self.mode = 1
        elif op == '0209':
            self.mode = 4
        elif op == '020b':
            self.ram[0xF30058] = int.from_bytes(data, 'big') * 16384 // 15625 | 1
        elif op == '020f':
            self.ram[0xF20124] = 0x01000000 | ((5 if data[5] == 0x0D else 4) << 20)
        elif op == '010c':
            address, mask, value = struct.unpack('>3I', data)
            self.ram[address] = (self.ram.get(address, 0) & ~mask) | value
        elif op == '0105':
            address = int.from_bytes(data[:4], 'big')
            for i in range(4, len(data), 4):
                self.ram[address + i - 4] = int.from_bytes(data[i:i + 4], 'big')
        return dict(ok=True)

    def status(self):
        return dict(decoded=dict(cmd_status=2, chip_mode=self.mode))

    def read_command(self, command, count):
        if command == bytes.fromhex('010d'):
            payload = b'\0\0'
        else:
            address = int.from_bytes(command[2:6], 'big')
            payload = b''.join(self.ram.get(address + i * 4, 0).to_bytes(4, 'big')
                               for i in range(command[6]))
        return dict(cmd_status=3, data=payload.hex())

    _payload = staticmethod(Bridge._payload)


class NomadTests(unittest.TestCase):
    def test_wrong_lr_firmware_fails_before_reset_or_ram_write(self):
        for version in ('22f30104', '22030101', '22030104'):
            b = RadioModel(version)
            with self.assertRaisesRegex(RadioError, 'stock LR1121 transceiver 1.3'):
                NomadReceiver(b).configure(915000000, 62500)
            self.assertEqual(b.commands, [])

    def test_nomad_never_uses_waveshare_tcxo_or_switch_profile(self):
        b = RadioModel()
        with patch('nomad_rx.time.sleep'):
            result = NomadReceiver(b).configure(915000000, 62500)
        self.assertEqual(result['sample_rate'], 62500)
        self.assertIn(bytes.fromhex('01120f00040808020001'), b.commands)
        self.assertFalse(any(isinstance(c, bytes) and c[:2] in (b'\x01\x17', b'\x01\x16') for c in b.commands))
        for i in range(32):
            addr = descriptor(i)
            self.assertEqual(b.ram[addr], CONTROL)
            self.assertEqual(b.ram[addr + 12], descriptor((i + 1) % 32))

    def test_hf_125k_configuration(self):
        b = RadioModel()
        with patch('nomad_rx.time.sleep'):
            result = NomadReceiver(b).configure(2440000000, 125000)
        self.assertEqual(result['frequency_hz'], 2440000000)
        self.assertEqual(b.ram[0xF20124], 0x01400000)
        self.assertNotIn(bytes.fromhex('0111'), b.commands)

    def test_unknown_dma_layout_prevents_ring_writes(self):
        b = RadioModel()
        b.ram[0x800A00] = 0
        with patch('nomad_rx.time.sleep'), self.assertRaisesRegex(RadioError, 'layout differs'):
            NomadReceiver(b).configure(915000000, 62500)
        self.assertFalse(any(isinstance(c, bytes) and c[:2] in (b'\x01\x05', b'\x01\x0c') for c in b.commands))

    def test_nomad_rejects_waveshare_helper_before_writes(self):
        b = RadioModel()
        with self.assertRaisesRegex(RadioError, 'Waveshare profile'):
            Core1121(b).initialize()
        self.assertEqual(b.commands, [])

    def test_invalid_profile_fails_before_device_access(self):
        for frequency, rate in ((100000000, 62500), (915000000, 500000)):
            b = RadioModel()
            with self.assertRaises(ValueError):
                NomadReceiver(b).configure(frequency, rate)
            self.assertEqual(b.commands, [])

    def test_wide_synthesizer_range_and_calibration(self):
        for frequency in (150000000,300000000,433000000,960000000,1090000000,1550000000,1900000000,2200000000,2500000000):
            b=RadioModel()
            with patch('nomad_rx.time.sleep'):
                result=NomadReceiver(b).configure(frequency,62500)
            self.assertEqual(result['frequency_hz'],frequency)
            calibrations=[c for c in b.commands if isinstance(c,bytes) and c[:2]==bytes.fromhex('0111')]
            self.assertEqual(bool(calibrations),400000000<=frequency<=1000000000)
        b=RadioModel()
        with patch('nomad_rx.time.sleep'):
            result=NomadReceiver(b).configure(1090000000,62500)
        self.assertEqual(result['receiver_band'],'experimental_synth_range')
        self.assertNotIn(bytes.fromhex('02292232434564556676060000'),b.commands)

    def test_synthesizer_edges_fail_before_any_write(self):
        for frequency in (98000000,149999999,2500000001,4294967295):
            b=RadioModel()
            with self.assertRaises(ValueError):NomadReceiver(b).configure(frequency,62500)
            self.assertEqual(b.commands,[])

    def test_iq_sign_and_float_scaling(self):
        values = [0, 0x3FFFFF, 0x400000, 0x7FFFFF] + [0] * 246
        payload = b''.join(v.to_bytes(3, 'little') for v in values)
        data, stats = read_frame(io.BytesIO(frame(payload=payload)), 1, 62500, 0)
        self.assertIsNone(stats)
        self.assertEqual(struct.unpack('<4f', data[:16]), (0., 1. - 2**-22, -1., -2**-22))

    def test_bad_header_crc_rejected_before_payload_length(self):
        wire = bytearray(frame())
        wire[24:28] = b'\xff' * 4
        with self.assertRaisesRegex(BridgeError, 'header/CRC'):
            read_frame(io.BytesIO(wire), 1, 62500, 0)

    def test_bad_payload_crc_rejected(self):
        wire = bytearray(frame())
        wire[-1] ^= 1
        with self.assertRaisesRegex(BridgeError, 'payload CRC'):
            read_frame(io.BytesIO(wire), 1, 62500, 0)

    def test_sequence_id_and_rate_must_match(self):
        for ident, rate, seq in ((2, 62500, 0), (1, 125000, 0), (1, 62500, 1)):
            with self.assertRaisesRegex(BridgeError, 'sequence gap'):
                read_frame(io.BytesIO(frame(ident=ident, rate=rate, seq=seq)), 1, 62500, 0)

    def test_invalid_23bit_container_rejected(self):
        with self.assertRaisesRegex(BridgeError, 'sample container'):
            read_frame(io.BytesIO(frame(payload=b'\0\0\x80' + bytes(747))), 1, 62500, 0)

    def test_terminal_counts_and_error_flags(self):
        data, stats = read_frame(io.BytesIO(frame(kind=2, seq=2, flags=1)), 1, 62500, 2)
        self.assertIsNone(data)
        self.assertEqual(stats['flags'], 1)
        bad = struct.pack('<9I', 3, 2, 0, 0, 0, 16000000, 0, 0, 2)
        with self.assertRaisesRegex(BridgeError, 'counts disagree'):
            read_frame(io.BytesIO(frame(kind=2, seq=2, payload=bad)), 1, 62500, 2)

    def test_truncated_stream_times_out(self):
        with patch('nomad_rx.time.monotonic', side_effect=[0, 6]):
            with self.assertRaisesRegex(BridgeError, 'timeout'):
                read_frame(io.BytesIO(b''), 1, 62500, 0)

    def test_dual_125k_rejected_before_connect(self):
        with patch('nomad_rx.Bridge') as bridge, patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            main(['--radio', 'both', '--rate', '125000', '--output', 'unused.cf32'])
        bridge.assert_not_called()

    def test_uart_large_capture_rejected_before_connect(self):
        with patch('nomad_rx.Bridge') as bridge, self.assertRaisesRegex(ValueError, '32 blocks'):
            capture('localhost', 0, 915000000, 62500, 2, 'unused.cf32', port='COM3')
        self.assertFalse(bridge.called)
        with tempfile.TemporaryDirectory() as folder, patch('nomad_rx.Bridge', side_effect=RuntimeError('opened')):
            with self.assertRaisesRegex(RuntimeError, 'opened'):
                capture('localhost', 0, 915000000, 62500, 2, str(Path(folder) / 'usb.cf32'), port='COM3', compact=True)
        bridge.assert_not_called()

    def test_uart_dual_rejected_before_connect(self):
        with patch('nomad_rx.Bridge') as bridge, patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            main(['--port', 'COM3', '--radio', 'both', '--output', 'unused.cf32'])
        bridge.assert_not_called()

    def test_uart_decimation_implies_compact_before_block_limit(self):
        with tempfile.TemporaryDirectory() as folder, patch('nomad_rx.Bridge', side_effect=RuntimeError('opened')) as bridge:
            with self.assertRaisesRegex(RuntimeError, 'opened'):
                capture('localhost', 0, 915000000, 62500, 2,
                        Path(folder) / 'decimated.cf32', port='COM3', decimate=True)
            bridge.assert_called_once()

    def test_capture_metadata_reports_loss_and_does_not_overwrite(self):
        b = Bridge(transport=io.BytesIO())
        b._id = 1
        b.io = io.BytesIO(frame(kind=2, flags=1))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.cf32'
            with patch('nomad_rx.Bridge', return_value=b), patch.object(NomadReceiver, 'configure', return_value={}), patch.object(b, 'request', return_value=dict(stream='LIQ1', format='iq23le')):
                with self.assertRaisesRegex(BridgeError, 'loss/error'):
                    capture('localhost', 0, 915000000, 62500, .002, path)
            self.assertFalse(json.loads(path.with_suffix('.cf32.json').read_text())['complete'])
            with self.assertRaises(FileExistsError):
                capture('localhost', 0, 915000000, 62500, .002, path)

    def compact_stream(self, data_frames, terminal_flags, rate=31250):
        wire = b''
        for seq in range(data_frames):
            payload = bytes([0]) + bytes(250)
            head = HEADER.pack(b'LIQ8', 1, 1, 0, 1, seq, seq * 125, 125, len(payload), rate, 2000, zlib.crc32(payload), 0)
            wire += head[:40] + struct.pack('<I', zlib.crc32(head[:40])) + payload
        stats = struct.pack('<9I', data_frames, data_frames, 1, 4, 100, 16000000, 2000, 0, 0)
        head = HEADER.pack(b'LIQ8', 1, 2, terminal_flags, 1, data_frames, data_frames * 125, 0, len(stats), rate, 2000, zlib.crc32(stats), 0)
        return wire + head[:40] + struct.pack('<I', zlib.crc32(head[:40])) + stats

    def test_long_capture_streams_open_ended_and_cancels_at_target(self):
        # Beyond the fixed-length request limit: 0 blocks, host cancels (0x03)
        # once the target arrives, extra in-flight frames are discarded, and
        # only a clean abort (flags == IQ_ABORT, 0 lost) counts as complete.
        transport = io.BytesIO(self.compact_stream(4, terminal_flags=4))
        b = Bridge(transport=transport)
        b._id = 1
        written = []
        b.io.write = lambda data: written.append(bytes(data)) or len(data)
        b.io.flush = lambda: None
        with tempfile.TemporaryDirectory() as folder, patch('nomad_rx.MAX_FIXED_BLOCKS', 2), \
                patch('nomad_rx.Bridge', return_value=b), patch.object(NomadReceiver, 'configure', return_value={}), \
                patch.object(b, 'request', return_value=dict(stream='LIQ8', format='iq8_block_float')) as request:
            path = Path(folder) / 'long.cf32'
            result = capture('localhost', 0, 915000000, 31250, .012, path)
            request.assert_called_once_with('stream8 31250 0')
            self.assertEqual(written, [b'\x03'])
            self.assertTrue(result['complete'])
            self.assertEqual(result['samples'], 375)
            self.assertEqual(path.stat().st_size, 3 * 125 * 8)

    def test_long_capture_rejects_abort_with_loss(self):
        b = Bridge(transport=io.BytesIO(self.compact_stream(3, terminal_flags=4 | 1)))
        b._id = 1
        b.io.write = lambda data: len(data)
        b.io.flush = lambda: None
        with tempfile.TemporaryDirectory() as folder, patch('nomad_rx.MAX_FIXED_BLOCKS', 2), \
                patch('nomad_rx.Bridge', return_value=b), patch.object(NomadReceiver, 'configure', return_value={}), \
                patch.object(b, 'request', return_value=dict(stream='LIQ8', format='iq8_block_float')):
            with self.assertRaisesRegex(BridgeError, 'loss/error'):
                capture('localhost', 0, 915000000, 31250, .012, Path(folder) / 'bad.cf32')

    def test_complete_capture_writes_cf32_and_metadata(self):
        b = Bridge(transport=io.BytesIO(frame() + frame(kind=2, seq=1)))
        b._id = 1
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.cf32'
            with patch('nomad_rx.Bridge', return_value=b), patch.object(NomadReceiver, 'configure', return_value={}), patch.object(b, 'request', return_value=dict(stream='LIQ1', format='iq23le')):
                result = capture('localhost', 0, 915000000, 62500, .002, path)
            self.assertTrue(result['complete'])
            self.assertEqual(result['samples'], 125)
            self.assertEqual(path.stat().st_size, 1000)
            self.assertEqual(json.loads(path.with_suffix('.cf32.json').read_text()), result)

    def test_real_socket_transport_fragmented_json_and_binary(self):
        # Exercise pyserial's socket:// backend on this OS, including binary
        # bytes buffered after the ASCII acknowledgement (no hardware needed).
        errors = []
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0)); server.listen(1); server.settimeout(5)
            port = server.getsockname()[1]
            def serve():
                try:
                    conn, _ = server.accept()
                    with conn, conn.makefile('rb') as incoming:
                        while True:
                            line = incoming.readline().strip()
                            if not line:
                                continue
                            ident, command = line.split(b' ', 1)
                            if command == b'info':
                                reply = dict(id=int(ident), ok=True, bridge='lr1121-spi', protocol=1)
                            else:
                                reply = dict(id=int(ident), ok=True, stream='LIQ1', format='iq23le')
                            wire = (json.dumps(reply) + '\n').encode()
                            conn.sendall(wire[:5]); conn.sendall(wire[5:])
                            if command.startswith(b'stream2'):
                                conn.sendall(frame(ident=int(ident)) + frame(kind=2, seq=1, ident=int(ident)))
                                incoming.readline() # Keep socket open until host closes.
                                break
                except BaseException as exc:
                    errors.append(exc)
            thread = threading.Thread(target=serve, daemon=True); thread.start()
            try:
                with Bridge(f'socket://127.0.0.1:{port}') as b:
                    self.assertEqual(b.info()['protocol'], 1)
                    b.request('stream2 62500 1')
                    data, _ = read_frame(b.io, b._id, 62500, 0)
                    self.assertEqual(len(data), 1000)
                    _, stats = read_frame(b.io, b._id, 62500, 1)
                    self.assertEqual(stats['sent'], 1)
            finally:
                thread.join(timeout=6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
