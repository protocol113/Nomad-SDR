# RadioMaster XR4 SDR port

Status: **builds and passes offline tests; not yet run on XR4 hardware.**

The XR4 (Gemini Xrossband true-diversity receiver) carries the same ESP32 and
two LR1121 chips as the Nomad, on the same SPI bus. This port reuses the Nomad
firmware and host tools unchanged except for the board profile. The result is a
thumb-sized, fanless, Wi-Fi SDR: 150–2500 MHz synthesizer range, one radio
streamed at a time over UART or both over TCP 1121 / 1122.

## What differs from the Nomad

Source: ExpressLRS/targets `radiomaster.rx_dual.xr4` = `RX/Generic LR1121 True
Diversity.json` + the `xr4` overlay (checked 2026-11-05).

| Item | Nomad | XR4 |
| --- | --- | --- |
| Radio 0 RESET | GPIO15 | **GPIO26** |
| GPIO26 | PA APC (held low) | radio 0 RESET (Nomad firmware would hold radio 0 in reset) |
| RF switch (0x0112) | `0f 00 04 08 08 02 00 01` (ELRS default) | `0f 00 0c 08 08 06 00 05` |
| LEDs | 2 × WS2812 on GPIO22 | 1 × WS2812 on GPIO22 |
| Fan / backpack / PA | GPIO2 / GPIO19 / GPIO26 | none (never driven) |
| USB | CP210x on UART0 | **none**: UART0 pads (RX = GPIO3, TX = GPIO1) |
| Boot button | — | GPIO0 |
| Wi-Fi AP | `Nomad-SDR` | `XR4-SDR`, same password `nomadsdr1121`, `192.168.4.1` |

All other pins (SCK 25, MOSI 32, MISO 33, NSS 27/13, BUSY 36/39, DIO9 37/34,
RESET 2 = 21) match. The RX entry of the XR4 switch table adds DIO bit 3 (0x08),
which on this board most likely enables the receive LNA; the LED shows the more
severe state of the two radios (red > yellow > green > blue).

Do **not** flash the Nomad image onto an XR4. Use `dist/xr4/`.

## Build

```sh
python -m pip install platformio -r host/requirements.txt
python tools/build_nomad.py      # builds both radiomaster_nomad and radiomaster_xr4
python tools/test_nomad.py
```

Output: `dist/xr4/nomad-sdr-merged.bin` (offset `0x0`) and `firmware.bin`
(`0x10000`) with `manifest.json`. Single board: `pio run -d nomad -e radiomaster_xr4 -t package`.

## Wire and flash

You need a **3.3 V-logic** USB-serial adapter (CP2102, CH340, FTDI) that does
921600 baud.

| Adapter | XR4 pad |
| --- | --- |
| 5V | 5V |
| GND | GND |
| TX | RX |
| RX | TX |

1. Hold the XR4 **boot** button while plugging in the adapter, then release.
   The LED stays dark in the bootloader.
2. **Back up ExpressLRS first** (this is your restore image):
   `python -m esptool --chip esp32 --port PORT read_flash 0 0x400000 xr4-elrs-backup.bin`
3. Flash: `python -m esptool --chip esp32 --port PORT write_flash 0x0 dist/xr4/nomad-sdr-merged.bin`
4. Power-cycle without the button. The LED turns blue when ready.
5. Check: `python host/bridge.py --port PORT --radio 0 info` and expect
   `"board":"radiomaster-xr4"` and `"rf_switch":"0f000c0808060005"`.

Restore ExpressLRS: repeat step 1, then
`python -m esptool --chip esp32 --port PORT write_flash 0x0 xr4-elrs-backup.bin`,
or flash the XR4 target with the ExpressLRS Configurator over UART.

Flashing through the ExpressLRS Wi-Fi updater or Betaflight passthrough is
untested; use the pads.

## Use

Every Nomad workflow in the [README](../README.md) applies. Replace `COM3` with the
adapter's port and use the `XR4-SDR` access point for Wi-Fi. Wi-Fi is the natural
mode for a standalone receiver because the board needs only 5 V power.
`python host/bridge.py --port PORT --radio 0 wifi sta "SSID" "PASSWORD"` joins a
router, exactly as on the Nomad.

The `fan` command reports `"fan":"none"`. `led INDEX R G B` accepts only index 0.

## Not yet verified on hardware

- Which antenna connector and band each LR1121 serves on the XR4, and the exact
  function of switch bit 0x08. The table comes from ELRS, which receives on the
  board with it, but SDR sensitivity per band is unmeasured.
- TCXO/LF clock: the XR4 target, like the Nomad, has no override, so none is sent.
- Power draw and thermals without a fan. The ESP32 runs at 240 MHz with Wi-Fi
  power-save disabled, as on the Nomad.
