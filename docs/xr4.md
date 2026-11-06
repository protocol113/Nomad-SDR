# RadioMaster XR4 SDR port

Status: **running on XR4 hardware (2026-11-05).** Each radio streams over UART
or Wi-Fi with zero lost samples. Both radios stream **at the same time** over
Wi-Fi in narrow mode (31.25 kS/s each, see below): two 60 s recordings (radio 0
at 915 MHz, radio 1 at 2440 MHz) and 15 s of two live feeds, all with zero lost
samples, at a weak -74 dBm Wi-Fi signal. Sensitivity and power draw are not yet
measured.

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

Flashing through Betaflight `serialpassthrough` (FC UART2) **did not work** on
the tested unit: passthrough opened and the old firmware answered through it, but
the ESP32 ROM bootloader never replied to esptool. A direct CP2102 adapter on
the pads worked first time. Disconnect the FC from the RX/TX pads while flashing.
The tested unit is an ESP32-PICO-D4 (rev 1.1) with 4 MB flash. esptool cannot
reset it over RTS, so power-cycle the adapter (without the button) after flashing.

## Convert the LR1121 chips to stock 1.3 (required once)

The XR4 ships with Semtech's ExpressLRS build **F30104** (`22f30104`) on both
LR1121 chips. The SDR needs stock **1.3** (`22030103`): it installs its sample
ring at RAM addresses and checks a layout guard taken from 1.3. On F30104 the RX
DMA descriptors match but the guard word at `0x800498` reads `0x0006` instead
of `0x0104`, so streaming refuses (nothing is written). Convert each chip with
the application-only installer. It checks bootloader `22df2100` before erasing
and never touches the bootloader:

```sh
python lr1121/install.py stock --port PORT --radio 0 --out build/xr4-radio0
python lr1121/install.py stock --port PORT --radio 1 --out build/xr4-radio1
```

Restore F30104 (before going back to ExpressLRS) from the checksum-pinned image
in the Semtech submodule:

```sh
mkdir -p build/backups
cp external/radio-firmware-images/lr1121/transceiver_drone/lr1121_transceiver_F30104.bin build/backups/lr1121-elrs-f30104.bin
python lr1121/install.py restore-elrs --port PORT --radio 0 --out build/xr4-radio0
python lr1121/install.py restore-elrs --port PORT --radio 1 --out build/xr4-radio1
```

## Use

Every Nomad workflow in the [README](../README.md) applies. Replace `COM3` with the
adapter's port and use the `XR4-SDR` access point for Wi-Fi. Wi-Fi is the natural
mode for a standalone receiver because the board needs only 5 V power.
`python host/bridge.py --port PORT --radio 0 wifi sta "SSID" "PASSWORD"` joins a
router, exactly as on the Nomad.

The `fan` command reports `"fan":"none"`. `led INDEX R G B` accepts only index 0.

## Monitor both radios at once (Wi-Fi)

Two live feeds, one per radio, for SDR++ (RTL-TCP source) or SDRangel:

```sh
python host/nomad_rtl_tcp.py --radio both --host XR4_IP
```

Radio 0 serves `127.0.0.1:1234` (start 915 MHz, `--frequency`), radio 1
`127.0.0.1:1235` (start 2440 MHz, `--frequency2`). Each feed tunes, starts and
stops on its own; they are not sample-synchronized. Select 250 kHz in the GUI.
Status files are `build/rtl-tcp-status.radio0.json` / `.radio1.json`.

Recording both radios:

```sh
python host/nomad_rx.py --host XR4_IP --radio both --rate 31250 \
  --frequency 915000000 --frequency2 2440000000 --seconds 60 --output both.cf32
```

Dual mode uses **narrow** acquisition: the LR1121 itself delivers 31.25 kS/s
(GFSK RX bandwidth code `0x0E`, rate field 6 at `0xF20124`) with its own
14.6 kHz (nominal) filter. Usable bandwidth is about **11 kHz per radio**,
against about 22 kHz for the single-radio default (62.5 kS/s acquired, FIR /2 on
the ESP32). `--radio both` selects narrow automatically; `--narrow` (live) or
`--rate 31250` (recorder) selects it for one radio.

Why not two full-width radios: both LR1121s share one 16 MHz SPI bus, and a
62.5 kS/s bank (125 samples every 2 ms) costs about 1.3 ms of ESP32 time per
radio. Narrow banks arrive every 4 ms and cost about 1.07 ms, so two radios use
about 53% of the budget.

Serial (UART) carries one radio: 921600 baud fits one 31.25 kS/s stream.

**Wi-Fi.** Two feeds need about 150 kB/s; the XR4 measured about 250 kB/s total
over TCP at -73 dBm. The Arduino lwIP build fixes the TCP send buffer at 5760 B
(4 segments): on a weak link a lost segment then waits for the retransmission
timer, and 60 s dual runs ended at 33-46 s with `flags: 2` (send queue full).
The firmware now raises each streaming connection's lwIP `snd_buf` to 3x
(17280 B, about 12 segments; the stream acknowledgement reports
`tcp_send_buffer`). With that, two 60 s dual runs at -74 dBm peaked at 3-27 of
83 queued frames per radio. Better placement still adds margin
(`bridge.py ... wifi` reports `rssi_dbm`); the ESP32 joins 2.4 GHz only.

## Update firmware without the boot button

Builds with `#define NOMAD_OTA_PASSWORD "..."` in the git-ignored
`nomad/src/wifi.local.h` accept Wi-Fi updates (espota, UDP 3232), refused while
a radio streams:

```sh
python ~/.platformio/packages/framework-arduinoespressif32/tools/espota.py \
  -i XR4_IP -a PASSWORD -f nomad/.pio/build/radiomaster_xr4/firmware.bin
```

Every build accepts updates over the serial adapter while the firmware runs,
also when Wi-Fi is down:

```sh
python tools/serial_update.py --port PORT nomad/.pio/build/radiomaster_xr4/firmware.bin
```

The board checks the image MD5 before switching, then reboots (about 18 s for
880 KB). If start-up fails (for example, out of memory), the firmware enters a
**safe mode** instead of reboot-looping: it prints `SAFE MODE: <reason>` on the
UART every second and stays reachable for both update paths. The boot button is
needed only if the running image cannot start at all.

## Timing and memory (`perf`)

`<id> perf` (UART or TCP) reports acquisition timing since `<id> perf reset`:
per-bank total, SPI, BUSY wait, FIR, empty polls, plus free heap. Measured on
the XR4 (firmware with the IRAM SPI path):

| Mode | Bank period | Per bank (avg / max) | SPI |
| --- | --- | --- | --- |
| 62.5 kS/s, FIR /2 to 31.25 (default) | 2 ms | 1297 / 1420 us | 686 us |
| Native 31.25 kS/s (narrow) | 4 ms | 1068 / 1223 us | 717 us |

Keep idle free heap above about 60 KB. Below that the ESP32 dropped full-size
Wi-Fi packets (100% loss on 1400-byte pings) and TCP stalled. The send queues
(32 frames per radio) leave about 92 KB idle; during the 60 s dual runs the
minimum was about 52 KB with Wi-Fi unaffected.

## Not yet verified on hardware

- Which antenna connector and band each LR1121 serves on the XR4, and the exact
  function of switch bit 0x08. The table comes from ELRS, which receives on the
  board with it, but SDR sensitivity per band is unmeasured.
- Whether F30104 could stream if the `0x800498` guard were relaxed (its RAM
  map is unverified; not pursued, because 1.3 works and nothing on the SDR uses
  F30104 features).
- TCXO/LF clock: the XR4 target, like the Nomad, has no override, so none is sent.
- Power draw and thermals without a fan. The ESP32 runs at 240 MHz with Wi-Fi
  power-save disabled, as on the Nomad.
