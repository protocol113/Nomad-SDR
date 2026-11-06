# SPDX-License-Identifier: MIT
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from boards import BOARDS, BoardError, check_board, rf_switch_command


class BoardTest(unittest.TestCase):
    def test_nomad_keeps_elrs_default_table(self):
        self.assertEqual(rf_switch_command({'board': 'radiomaster-nomad'}), '01120f00040808020001')

    def test_xr4_uses_target_overlay(self):
        # ExpressLRS/targets radiomaster.rx_dual.xr4 radio_rfsw_ctrl [15,0,12,8,8,6,0,5]
        self.assertEqual(bytes([15, 0, 12, 8, 8, 6, 0, 5]).hex(), BOARDS['radiomaster-xr4'])
        info = {'board': 'radiomaster-xr4', 'rf_switch': '0f000c0808060005'}
        self.assertEqual(rf_switch_command(info), '01120f000c0808060005')

    def test_unknown_board_rejected(self):
        with self.assertRaises(BoardError):
            check_board({'board': 'waveshare-core1121'})

    def test_firmware_host_mismatch_rejected(self):
        with self.assertRaises(BoardError):
            check_board({'board': 'radiomaster-xr4', 'rf_switch': '0f00040808020001'})


if __name__ == '__main__':
    unittest.main()
