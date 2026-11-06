# SPDX-License-Identifier: MIT
"""ESP32 + dual-LR1121 boards served by the nomad/ firmware.

The firmware is an SPI bridge; the host sends the LR1121 board setup. The only
per-board radio difference is the SetDioAsRfSwitch (0x0112) table, taken from
ExpressLRS/targets. Firmware 0.11.0+ on non-Nomad boards also reports the table
it was built for, so a host/firmware mismatch fails instead of misrouting RF.
"""

# board id -> SetDioAsRfSwitch payload (enable, standby, RX, TX, TX_HP, TX_HF, GNSS, WiFi)
BOARDS = {
    # ELRS LR1121 driver default; the Nomad target has no override.
    'radiomaster-nomad': '0f00040808020001',
    # radiomaster.rx_dual.xr4 radio_rfsw_ctrl [15,0,12,8,8,6,0,5]
    'radiomaster-xr4': '0f000c0808060005',
}


class BoardError(RuntimeError):
    pass


def check_board(info):
    """Return the board id from a bridge `info` reply, or raise BoardError."""
    board = info.get('board')
    if board not in BOARDS:
        raise BoardError(f'Expected one of {sorted(BOARDS)}, got {board!r}')
    reported = info.get('rf_switch')
    if reported is not None and reported.lower() != BOARDS[board]:
        raise BoardError(f'{board}: firmware RF switch {reported} != host table {BOARDS[board]}')
    return board


def rf_switch_command(info):
    return '0112' + BOARDS[check_board(info)]
