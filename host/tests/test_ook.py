# SPDX-License-Identifier: MIT
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ook


class OokHostTest(unittest.TestCase):
    def test_pulses_hex_roundtrip(self):
        pulses = [300, 1000, 32768, 1]
        self.assertEqual(ook.pulses_to_hex(pulses), '012c03e880000001')
        self.assertEqual(ook.hex_to_pulses('012c03e880000001'), pulses)

    def test_pulse_bounds(self):
        with self.assertRaises(ValueError):
            ook.pulses_to_hex([0, 300])            # zero not allowed
        with self.assertRaises(ValueError):
            ook.pulses_to_hex([300, 40000])        # over 32768
        with self.assertRaises(ValueError):
            ook.pulses_to_hex([300])               # need on and off

    def test_ramp_and_bw_codes(self):
        self.assertEqual(ook.ramp_code(48), 2)
        self.assertEqual(ook.ramp_code(50), 3)     # rounds up to next available
        self.assertEqual(ook.ramp_code(1000), 15)  # clamps to max
        self.assertEqual(ook.bw_code(467000), 9)   # widest
        self.assertEqual(ook.bw_code(10_000_000), 9)

    def test_tx_command_shape(self):
        cmd = ook.tx_command(303875000, 'lp', -9, 48, 5, 10000, [350, 1050])
        self.assertEqual(cmd, 'ooktx 303875000 lp -9 2 5 10000 015e041a')

    def test_tx_power_range_enforced(self):
        with self.assertRaises(ValueError):
            ook.tx_command(303875000, 'lp', 20, 48, 1, 0, [350, 1050])  # lp max 14

    def test_tx_total_time_ceiling(self):
        with self.assertRaises(ValueError):
            ook.tx_command(303875000, 'lp', -9, 48, 10000, 0, [32768, 32768])

    def test_rx_command_threshold_doubles(self):
        self.assertEqual(ook.rx_command(433920000, 467000, 2000, -70.0, 30),
                         'ookrx 433920000 9 2000 -140 30')

    def test_sub_roundtrip(self):
        text = ('Filetype: Flipper SubGhz RAW File\nVersion: 1\n'
                'Frequency: 303875000\nPreset: FuriHalSubGhzPresetOok650Async\n'
                'Protocol: RAW\nRAW_Data: 350 -1050 350 -1050\n')
        freq, durations, first = ook.parse_sub(text)
        self.assertEqual((freq, durations, first), (303875000, [350, 1050, 350, 1050], 1))
        rendered = ook.durations_to_sub(freq, durations, first)
        self.assertIn('RAW_Data: 350 -1050 350 -1050', rendered)
        self.assertIn('Frequency: 303875000', rendered)

    def test_sub_merges_consecutive_same_sign(self):
        text = ('Frequency: 303875000\nRAW_Data: 100 200 -300 -50\n')
        freq, durations, first = ook.parse_sub(text)
        self.assertEqual((freq, durations, first), (303875000, [300, 350], 1))


if __name__ == '__main__':
    unittest.main()
