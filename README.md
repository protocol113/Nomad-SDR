# Nomad-SDR

**Two Semtech LR1121 radios on a RadioMaster Nomad — 150–2500 MHz receive, waterfall in SDRangel / SDR++, JSON control API.**

![Nomad-SDR live in SDRangel](docs/images/nomad-sdrangel.jpg)

Firmware on the Nomad's main ESP32 turns the stock dual-LR1121 Gemini board into an I/Q receiver. Tune from SDRangel or SDR++, and keep ExpressLRS a restore away.

| | |
| --- | --- |
| Radios | Dual LR1121 (ExpressLRS Nomad pin map), transceiver 1.3 |
| USB | **Main** CP210x UART at 921600 baud, one radio at a time |
| Wi-Fi | AP `Nomad-SDR` / `nomadsdr1121` at `192.168.4.1`, or STA to your router |
| Waterfall | LID8 → `nomad_rtl_tcp.py` → SDRangel Remote TCP Input / SDR++ RTL-TCP |
| Control API | `http://127.0.0.1:8094/api/v1` for sweep, packet, CAD, gain, diagnostics |
| Status LEDs | Blue ready · green receiving · yellow queue filling · red error |
| Fan | On at ESP32 die ≥ 50 °C, off at ≤ 42 °C |

---

## Contents

1. [Pick a mode](#pick-a-mode)
2. [What you need](#what-you-need)
3. [First-time setup](#first-time-setup)
4. [USB waterfall](#usb-waterfall)
5. [Wi-Fi](#wi-fi)
6. [Control API (scan / capture packets)](#control-api-scan--capture-packets)
7. [Frequency and antennas](#frequency-and-antennas)
8. [Flash firmware and restore ExpressLRS](#flash-firmware-and-restore-expresslrs)
9. [Common problems](#common-problems)
10. [Limits](#limits)

More detail: [USB native](docs/usb-native.md) · [live SDR](docs/live-sdr.md) · [control API](docs/control-api.md) · [Nomad](docs/nomad.md) · [XR4 port](docs/xr4.md)

---

## Pick a mode

Run **one** of these at a time. COM3 can be opened by a single program.

| Goal | Start | Keep closed |
| --- | --- | --- |
| Waterfall / listen | `Start-Nomad-USB.cmd`, then SDRangel or SDR++ | API server, recorder |
| Most stable path, or the PC has no Wi-Fi | USB, as above | — |
| PC on another machine / Ethernet | Join Nomad Wi-Fi, then `Start-Nomad-SDR.cmd` | USB proxy on the same port |
| RSSI sweep, LoRa/GFSK capture, chip temperature | `Start-Nomad-API.cmd` | USB waterfall |
| Short I/Q file | `host/nomad_rx.py` | USB proxy and API |

Radio **0** is the usual 900 MHz path (start at 915 MHz).
Radio **1** is the usual 2.4 GHz path (start at 2440 MHz).
USB streams one radio. Wi-Fi uses TCP 1121 / 1122. Dual I/Q at once is unproven.

---

## What you need

- RadioMaster Nomad (Gemini, two LR1121 chips)
- Windows, Python 3, USB into the **main** port (Silicon Labs CP210x)
- Leave the **backpack** USB (ESP32-C3) alone
- SDRangel or SDR++ for the waterfall
- The module's 900 MHz and/or 2.4 GHz antennas

Find the COM port in Device Manager → Ports (COM & LPT) → Silicon Labs.
This guide uses **COM3** from the test PC. Change the commands or `.cmd` files if yours differs.

---

## First-time setup

From PowerShell in this repo:

```powershell
python -m pip install -r host/requirements-live.txt
```

If Nomad-SDR firmware is not on the board yet, [flash it](#flash-firmware-and-restore-expresslrs) first.
This path expects **nomad-0.11.0**.

Check that the board answers:

```powershell
python host/bridge.py --port COM3 --radio 0 info
```

You want `"firmware":"nomad-0.11.0"` and `"board":"radiomaster-nomad"`.
If the port fails to open, close SDRangel, the API, and any other Python window holding COM3.

---

## USB waterfall

This is the most stable live path on the hardware that was tested.

### 1. Start the PC proxy

Double-click `Start-Nomad-USB.cmd` or run:

```powershell
python host/nomad_rtl_tcp.py --serial COM3 --radio 0 --port 1234
```

Leave that window open. It serves `127.0.0.1:1234`.
The proxy talks to Nomad only after the SDR program presses Start.
Ctrl+C in this window stops it.

Radio 1 uses `--radio 1 --port 1235`.

### 2. SDRangel

1. Add an Rx device and pick **Remote TCP Input**.
2. Server `127.0.0.1`, port `1234`.
3. Protocol **SDRangel** (the receiver falls back to RTL0).
4. Device / channel sample rate **250000**, decimation 1, sample bits **8**.
5. Enable **Override remote settings**.
6. Center frequency **915000000** Hz, then Start.
7. Add an **NFM Demodulator** or another demodulator.

### 3. SDR++

1. Source: **RTL-TCP**.
2. Host `127.0.0.1`, port `1234`.
3. Sample rate **250 kHz** before Play (the 2.4 MHz default will not work).
4. Frequency **915.000 MHz**, then Play.
5. For NFM, start around 12.5 kHz bandwidth.

If RTL-TCP is missing, enable `rtl_tcp_source` in Module Manager.
No Nomad-specific plugin is required.

### Waterfall notes

- **250 kHz in the GUI is PC interpolation.** USB actually carries LID8 at **31.25 kS/s**.
- A retune leaves a gap of about 1–2 seconds. The proxy does not invent samples.
- RTL-SDR gain / bias-tee menus do not drive Nomad hardware.
  Change PC-side level with the proxy `--gain-db` (default 60; lower it if it clips).
- Green LED = I/Q flowing, yellow = queue filling, red = error.

Record a short file without a GUI:

```powershell
python host/nomad_rx.py --port COM3 --radio 0 --decimate --frequency 915000000 --seconds 8 --output capture.cf32
```

The file is little-endian complex float32. GNU Radio can open it. Read the sample rate from the matching `.json` file.

---

## Wi-Fi

Use this when USB is inconvenient or I/Q should ride the network.

### AP mode (Nomad is the hotspot)

1. Join SSID **Nomad-SDR**, password **nomadsdr1121**.
2. The board IP is `192.168.4.1`.
3. Double-click `Start-Nomad-SDR.cmd` or run:

```powershell
python host/nomad_rtl_tcp.py --host 192.168.4.1 --radio 0 --port 1234
```

4. Point SDRangel / SDR++ at `127.0.0.1:1234`, same as USB.

Radio 1: double-click `Start-Nomad-Radio1.cmd` (PC port 1235, start frequency 2440 MHz).
On the board, radio 0 listens on TCP **1121** and radio 1 on **1122**.

### STA mode (Nomad joins your router)

Set this over USB only. TCP cannot change STA, and you must not change it while I/Q is streaming:

```powershell
python host/bridge.py --port COM3 --radio 0 wifi sta "SSID" "PASSWORD"
python host/bridge.py --port COM3 --radio 0 wifi
```

Wait until `connected` is true, then use the reported `ip` in place of `192.168.4.1`.
The PC must be on the same network (Ethernet is fine).

```powershell
python host/nomad_rtl_tcp.py --host THE_IP --radio 0 --port 1234
```

Back to hotspot: `python host/bridge.py --port COM3 --radio 0 wifi ap`.
Passwords are not echoed. Do not commit live SSIDs.

---

## Control API (scan / capture packets)

Use this for per-channel RSSI, LoRa/GFSK monitoring, CAD, gain, and chip temperature.
**Close the USB proxy first**, then double-click `Start-Nomad-API.cmd` or run:

```powershell
python host/nomad_api.py --serial COM3
```

The API is `http://127.0.0.1:8094/api/v1`.
Ctrl+C stops it and releases COM3.
Normal replies look like `{ "ok": true, "result": ... }`.
Bad parameters = 400, unknown route/job = 404, radio busy = 409, board I/O failed = 502.

PowerShell helpers:

```powershell
$nomadApi = 'http://127.0.0.1:8094/api/v1'
Invoke-RestMethod "$nomadApi/capabilities"

function Send-Nomad($route, $payload) {
    Invoke-RestMethod "$nomadApi/$route" -Method Post -ContentType 'application/json' -Body ($payload | ConvertTo-Json -Depth 8)
}
```

`{r}` is `0` or `1`. POST bodies are JSON objects. Start/stop routes take `{}`.

| Method | Path under `/api/v1` | Role |
| --- | --- | --- |
| GET | `/capabilities` | Chip UID/version, modems, bandwidths |
| GET | `/radios/{r}/status` | Profile, gain, active job |
| GET | `/radios/{r}/stream/status` | I/Q queue, bytes, throughput, loss |
| POST | `/radios/{r}/rx/configure` | LoRa or GFSK setup, then standby |
| POST | `/radios/{r}/rx/start` | Continuous packet RX |
| POST | `/radios/{r}/rx/stop` | Stop and standby |
| POST | `/radios/{r}/sweep/start` | Background RSSI scan |
| GET | `/radios/{r}/sweep/status` | Progress and min/mean/max |
| POST | `/radios/{r}/sweep/stop` | Cancel the sweep |
| POST | `/radios/{r}/packet/configure` | Packet profile |
| POST | `/radios/{r}/packet/start` | Background packet monitor |
| GET | `/radios/{r}/packet/status` | Payload, CRC, RSSI/SNR |
| POST | `/radios/{r}/packet/stop` | Cancel the monitor |
| POST | `/radios/{r}/cad` | One-shot LoRa activity detect |
| GET/POST | `/radios/{r}/gain` | AGC / step, RxBoosted, LF0 input |
| GET | `/radios/{r}/diagnostics` | LR1121 die temperature and supply |
| POST | `/dual/configure` | Store a two-radio sweep/packet plan |
| POST | `/dual/start` | Start the plan |
| GET | `/dual/status` | Both radios |
| POST | `/dual/stop` | Stop both radios |
| GET | `/jobs/{id}` | Job results still in memory |
| GET | `/jobs/{id}.csv` | Same results as CSV |

### Example: scan 902–928 MHz and save CSV

```powershell
$job = Send-Nomad 'radios/0/sweep/start' @{ start_hz=902000000; stop_hz=928000000; step_hz=1000000; dwell_ms=10; samples=4; cycles=1; capacity=4096 }
Invoke-RestMethod "$nomadApi/radios/0/sweep/status"
Invoke-WebRequest "$nomadApi/jobs/$($job.result.id).csv" -OutFile 'sweep.csv'
Send-Nomad 'radios/0/sweep/stop' @{}
```

This walks channels one by one. It is not a simultaneous FFT, so short bursts can be missed.
`cycles=0` repeats until you stop it. Maximum 4096 channels per pass.

### Example: LoRa RX and diagnostics

```powershell
Send-Nomad 'radios/0/rx/configure' @{ modem='lora'; frequency_hz=915000000; sf=7; bandwidth_hz=125000; sync_word=18 }
Send-Nomad 'radios/0/rx/start' @{}
Send-Nomad 'radios/0/gain' @{ agc=$true; rx_boosted=$true; lna='differential' }
Invoke-RestMethod "$nomadApi/radios/0/diagnostics"
Send-Nomad 'radios/0/rx/stop' @{}
```

Below 2300 MHz, LoRa bandwidth is 125000 / 250000 / 500000.
At 2.4 GHz use 203125 / 406250 / 812500 on radio 1.

### Example: GFSK monitor and CAD

```powershell
Send-Nomad 'radios/0/packet/start' @{
    profile = @{ modem='gfsk'; frequency_hz=915000000; bit_rate=50000; deviation_hz=25000; bandwidth_hz=156200; sync_word='12345678' }
    duration_s = 10; poll_ms = 10; capacity = 256
}
Invoke-RestMethod "$nomadApi/radios/0/packet/status"
Send-Nomad 'radios/0/packet/stop' @{}

Send-Nomad 'radios/1/rx/configure' @{ modem='lora'; frequency_hz=2440000000; sf=7; bandwidth_hz=203125 }
Send-Nomad 'radios/1/cad' @{ symbols=2; detect_peak=22; detect_min=10; timeout_ms=1000 }
```

This API decodes LoRa/GFSK PHY. It does not decrypt LoRaWAN and it does not follow ELRS hopping.
CAD reports activity against the thresholds you set. That is not a decoded packet.

### Example: both radios (sweep + packets)

```powershell
Send-Nomad 'dual/configure' @{ radios=@(
    @{ mode='sweep'; settings=@{ start_hz=902000000; stop_hz=928000000; step_hz=1000000; cycles=0 } },
    @{ mode='packet'; settings=@{ profile=@{ modem='lora'; frequency_hz=2440000000 } } }
) }
Send-Nomad 'dual/start' @{}
Invoke-RestMethod "$nomadApi/dual/status"
Send-Nomad 'dual/stop' @{}
```

API dual mode shares one UART with a lock so commands do not switch chips mid-request.
It is **not** dual I/Q into SDRangel.

Full fields and test scope: [docs/control-api.md](docs/control-api.md).
UART telemetry without HTTP: `python host/bridge.py --port COM3 --radio 0 capabilities`.

---

## Frequency and antennas

Both chips tune **150–2500 MHz**. That is the synthesizer span, not even sensitivity across the range.

| Band | How to treat it |
| --- | --- |
| 150–960 MHz | Specified RX. Nomad matching/antennas are strongest near 900 MHz — start at **915 MHz** |
| 1900–2200 MHz | Specified RX |
| 2400–2500 MHz | Specified RX. 2.4 GHz antennas — start at **2440 MHz** |
| Gaps 960–1900 and 2200–2400 MHz | The synthesizer will go there (1090, 1550, 2300 MHz were tried). Treat as experiments |
| Below 150 MHz | Out of range. FM 88–108 MHz is unsupported |
| Above 2500 MHz | Out of range |

Tuning 1090 MHz does not give full-band ADS-B; native bandwidth is still narrow.

RGB LEDs (full brightness, LED0 = radio 0, LED1 = radio 1):

| Color | Meaning |
| --- | --- |
| Blue | Ready |
| Green | Receiving I/Q |
| Yellow | Queue at least 1/4 full |
| Red | DMA / queue / radio error |

```powershell
python host/bridge.py --port COM3 --radio 0 fan auto
python host/bridge.py --port COM3 --radio 0 led auto
```

`fan auto` uses the ESP32 die temperature. API diagnostics read the LR1121 sensors.

---

## Flash firmware and restore ExpressLRS

Flash only the **main** ESP32 through the CP210x. Leave the backpack cable disconnected.

```powershell
python -m pip install platformio -r host/requirements.txt -r host/requirements-live.txt
python tools/test_nomad.py
python tools/build_nomad.py
```

Write the merged image at offset `0x0`:

```powershell
python -m platformio pkg exec -p tool-esptoolpy -- esptool.py --chip esp32 --port COM3 write_flash 0x0 dist/nomad/nomad-sdr-merged.bin
```

Run that from `nomad/`, or pass the absolute path to `nomad-sdr-merged.bin`.

Restore ExpressLRS with `python lr1121/install.py restore-elrs` plus a full-flash backup from `build/backups/`.
Keep transceiver **0x0103** for this SDR path. Do not flash 0x0104 / 0x0105 / Modem-E if you still need the current I/Q layout.

---

## Common problems

| Symptom | Check |
| --- | --- |
| `Access is denied` / port busy | Close the API, USB proxy, SDRangel, and Serial Monitor so one program owns COM3 |
| `info` fails or baud is wrong | Main CP210x at 921600, not the backpack |
| SDR++ shows nothing | Select 250 kHz before Play. 2.4 MHz will not work |
| Waterfall glitches on retune | About 1–2 seconds of setup is expected |
| API kills the waterfall | COM3 is exclusive. Stop one before starting the other |
| No signal at 915 MHz | 900 MHz antenna, radio 0, green LED after Start |
| 2.4 GHz dropouts | Continuous streaming in this band is unproven. Try radio 1 for a short capture first |
| STA has no IP | Call `wifi` again. After 20 seconds the board also opens the AP fallback |

Protocol check without a GUI (no RF reference tone):

```powershell
python tools/smoke_nomad_live.py --serial COM3 --radio 0 --seconds 60
```

The report is `build/live-smoke.json`.

---

## Limits

- USB I/Q is one radio at 31.25 kS/s (LID8). 250 kHz in the GUI is PC interpolation.
- The control API is receive-only. Dual jobs share the UART; they are not dual I/Q.
- Packet / CAD RF correctness still needs a reference transmitter.
- Continuous Wi-Fi I/Q at 2.4 GHz and dual-radio I/Q still need validation.
- Sensitivity outside the 900 / 2.4 GHz antennas is unproven.
- [Packed ARC](docs/packed-stream.md) is experimental on radio 1 (`--packed-verification`). Normal acquisition stays the default.
- Pico LIP2/LIP3 250/500 kS/s, TX, and the RP2040 SDR++ module remain on the upstream tree.

---

## Layout

- `nomad/` — ESP32 firmware (PlatformIO, MIT)
- `host/` — UART/TCP bridge, recorder, rtl_tcp adapter, JSON API (`nomad_api.py`)
- `pico/` — original RP2040 firmware from [lr1121-sdr](https://github.com/naorunaoru/lr1121-sdr)
- `sdrpp/` — GPL-3.0 SDR++ source module for the Pico
- `docs/` — hardware notes, live setup, API, measurements

## Contributor

[hereismeaw](https://github.com/hereismeaw)

## Licensing

This tree started from [naorunaoru/lr1121-sdr](https://github.com/naorunaoru/lr1121-sdr). Pin map from [ExpressLRS Nomad](https://github.com/ExpressLRS/targets). LR1121 research lineage includes [radioegor146/lr-semtech-research](https://github.com/radioegor146/lr-semtech-research).

Independent firmware, host tools and docs: [MIT](LICENSE).
`sdrpp/` and Nomad receiver helpers derived from it: [GPLv3](sdrpp/LICENSE).
See [license scope](docs/licensing.md) and [third-party notices](THIRD_PARTY_NOTICES.md).
Semtech LR1121 images keep their vendor license. Do not commit `nomad/src/wifi.local.h`.
