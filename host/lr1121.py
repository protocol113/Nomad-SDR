# SPDX-License-Identifier: MIT
"""Core1121-XF board setup and receive-only 2.4 GHz RSSI helpers.

Board constants come from Waveshare's Core1121_XF_Demo Pico example;
command encodings and modes follow Semtech's LR1121 user manual/driver.
"""
import statistics
import time


class RadioError(RuntimeError):
    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


class Core1121:
    # enable, standby, RX, TX_LP, TX_HP, TX_HF, GNSS, Wi-Fi.
    RF_SWITCH = bytes.fromhex("0300010202000000")
    # g4..g13, then g13hp1..g13hp7. Each field is four bits.
    RSSI_GAIN_TUNES_2G4 = (6, 7, 6, 4, 3, 4, 14, 12, 14, 12, 12, 12, 12, 8, 8, 9, 9)
    RSSI_GAIN_OFFSET_2G4 = 2030
    # Historical SDK enum names 200/400/800 correspond to 203/406/812 kHz.
    BANDWIDTHS = {200: (0x0D, 203000), 400: (0x0E, 406000), 800: (0x0F, 812000)}

    def __init__(self, bridge):
        self.bridge = bridge
        self.steps = []

    def _write(self, name, opcode, data=b"", expected_mode=None):
        command = opcode.to_bytes(2, "big") + bytes(data)
        sent = self.bridge.write_command(command)
        state = self.bridge.status()
        step = dict(step=name, command=command.hex(), transfer=sent, status=state)
        self.steps.append(step)
        decoded = state["decoded"]
        if decoded["cmd_status"] != 2:
            raise RadioError(f"Radio rejected {name}", step)
        if expected_mode is not None and decoded["chip_mode"] != expected_mode:
            raise RadioError(f"Unexpected chip mode after {name}", step)
        return step

    def _read(self, opcode, length):
        result = self.bridge.read_command(opcode.to_bytes(2, "big"), length)
        return result, self.bridge._payload(result, length)

    def _check_errors(self, stage):
        result = self.bridge.errors()
        self.steps.append(dict(step=stage, result=result))
        if int(result["error_flags"], 16):
            raise RadioError(f"Radio error flags at {stage}", result)
        return result

    def initialize(self):
        """Reset and configure the board, ending in verified crystal standby."""
        self.steps = []
        info = self.bridge.info()
        if str(info.get('board', '')).startswith('radiomaster-'):
            raise RadioError('Core1121 is a Waveshare profile; use host/nomad_rx.py for Nomad/XR4', info)
        report = dict(bridge=info, version=self.bridge.version(),
                      errors_before_reset=self.bridge.errors(), steps=self.steps)
        if not report["version"]["version"]["is_lr1121_transceiver"]:
            raise RadioError("Expected LR1121 transceiver firmware", report["version"])
        report["reset"] = self.bridge.reset()
        report["errors_after_reset"] = self.bridge.errors()
        self._write("standby_rc", 0x011C, b"\x00", 1)
        # 3.0 V, 300 RTC ticks (~9.16 ms); TCXO configuration requires RC standby.
        self._write("tcxo_3v0_300_ticks", 0x0117, bytes.fromhex("0600012c"), 1)
        self._write("regulator_dcdc", 0x0110, b"\x01", 1)
        self._write("rf_switch_waveshare", 0x0112, self.RF_SWITCH, 1)
        self._write("lf_clock_xtal_wait_ready", 0x0116, b"\x05", 1)
        report["errors_before_clear"] = self.bridge.errors()
        self._write("clear_latched_errors", 0x010E, expected_mode=1)
        self._check_errors("errors_after_clear")
        self._write("calibrate_all", 0x010F, b"\x3f", 1)
        self._check_errors("errors_after_calibration")
        self._write("clear_old_error_irq", 0x0114, bytes.fromhex("00800000"), 1)
        self._write("standby_xosc", 0x011C, b"\x01", 2)
        report["errors_in_xosc"] = self._check_errors("errors_in_xosc")
        report["configured"] = True
        return report

    @classmethod
    def rssi_calibration_payload(cls):
        tunes = cls.RSSI_GAIN_TUNES_2G4
        packed = bytes(tunes[i] | (tunes[i + 1] << 4) for i in range(0, 16, 2))
        return packed + bytes([tunes[16]]) + cls.RSSI_GAIN_OFFSET_2G4.to_bytes(2, "big", signed=True)

    def start_rx(self, frequency_hz=2440000000, bandwidth=800):
        """Initialize, configure LoRa, and leave the radio in continuous RX."""
        if not isinstance(frequency_hz, int) or not 2400000000 <= frequency_hz <= 2500000000:
            raise ValueError("This receive helper supports 2400000000..2500000000 Hz")
        if bandwidth not in self.BANDWIDTHS:
            raise ValueError("2.4 GHz bandwidth must be 200, 400 or 800")
        bw_code, bw_hz = self.BANDWIDTHS[bandwidth]
        report = self.initialize()
        report["rx_profile"] = dict(frequency_hz=frequency_hz, modem="LoRa", sf=7,
                                    bandwidth_hz=bw_hz, coding_rate="4/5", ldro=False,
                                    sync_word="0x12", header="explicit", crc=True,
                                    iq="standard", continuous=True)
        self._write("packet_type_lora", 0x020E, b"\x02", 2)
        self._write("rf_frequency_hz", 0x020B, frequency_hz.to_bytes(4, "big"), 2)
        self._write("rssi_calibration_2g4", 0x0229, self.rssi_calibration_payload(), 2)
        self._write("lora_modulation", 0x020F, bytes([7, bw_code, 1, 0]), 2)
        # 8-symbol preamble, explicit header, max 255-byte payload, CRC, normal IQ.
        self._write("lora_packet", 0x0210, bytes.fromhex("000800ff0100"), 2)
        self._write("lora_sync_word", 0x022B, b"\x12", 2)
        self._write("rx_continuous", 0x0209, b"\xff\xff\xff", 4)
        # Allow the analog receiver/RSSI estimator to settle.
        time.sleep(0.03)
        report["errors_in_rx"] = self._check_errors("errors_in_rx")
        report["rx_status"] = self.bridge.status()
        if report["rx_status"]["decoded"]["chip_mode"] != 4:
            raise RadioError("Receiver did not stay in RX", report["rx_status"])
        return report

    def sample_rssi(self, samples=100, interval_ms=20):
        if not isinstance(samples, int) or not 1 <= samples <= 10000:
            raise ValueError("samples must be 1..10000")
        if not 0 <= interval_ms <= 1000:
            raise ValueError("interval_ms must be 0..1000")
        before = self.bridge.status()
        if before["decoded"]["chip_mode"] != 4:
            raise RadioError("Radio is not in RX; run rx-start first", before)
        self._check_errors("errors_before_rssi")
        data = []
        start = time.monotonic()
        for i in range(samples):
            if i:
                time.sleep(interval_ms / 1000)
            raw_result, payload = self._read(0x0205, 1)
            data.append(dict(elapsed_ms=round((time.monotonic() - start) * 1000, 3),
                             raw=payload[0], dbm=-payload[0] / 2.0,
                             status=raw_result["status"]))
        after = self.bridge.status()
        if after["decoded"]["chip_mode"] != 4:
            raise RadioError("Receiver left RX during RSSI sampling", after)
        errors = self._check_errors("errors_after_rssi")
        dbm = [sample["dbm"] for sample in data]
        return dict(samples=data, summary=dict(count=len(dbm), min_dbm=min(dbm),
                    max_dbm=max(dbm), median_dbm=statistics.median(dbm),
                    mean_dbm=round(statistics.mean(dbm), 3)),
                    status_before=before, status_after=after, errors=errors)

    def stop_rx(self):
        return self._write("standby_rc", 0x011C, b"\x00", 1)
