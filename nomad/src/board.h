/* SPDX-License-Identifier: MIT */
#pragma once
#include <stdint.h>

// Board profile is chosen at build time (PlatformIO env sets the define).
// Both profiles are classic ESP32 + two LR1121 on one SPI bus. A pin that a
// board does not have is -1 and is never driven.
namespace board {
#if defined(BOARD_RADIOMASTER_XR4)
// ExpressLRS/targets: RX/Generic LR1121 True Diversity.json + "xr4" overlay
// (radiomaster.rx_dual.xr4). Radio 0 RESET is GPIO26, which is the PA APC pin
// on the Nomad: never drive Nomad firmware on an XR4, or radio 0 stays in reset.
constexpr const char *name = "radiomaster-xr4";
constexpr const char *hostname = "xr4-sdr";
constexpr int sck = 25, miso = 33, mosi = 32;
constexpr int cs[2] = {27, 13}, busy[2] = {36, 39};
constexpr int reset[2] = {26, 21}, dio9[2] = {37, 34};
constexpr int pa_apc = -1, fan = -1, backpack_en = -1, led = 22;
constexpr unsigned led_count = 1;
// LR1121 SetDioAsRfSwitch (0x0112) payload from the xr4 radio_rfsw_ctrl
// overlay [15,0,12,8,8,6,0,5]: enable, standby, RX, TX, TX_HP, TX_HF, GNSS, WiFi.
constexpr const char *rf_switch = "0f000c0808060005";
#else
// ExpressLRS/targets, TX/Radiomaster Nomad.json (see docs/nomad.md).
#define BOARD_RADIOMASTER_NOMAD 1
constexpr const char *name = "radiomaster-nomad";
constexpr const char *hostname = "nomad-sdr";
constexpr int sck = 25, miso = 33, mosi = 32;
constexpr int cs[2] = {27, 13}, busy[2] = {36, 39};
constexpr int reset[2] = {15, 21}, dio9[2] = {37, 34};
constexpr int pa_apc = 26, fan = 2, backpack_en = 19, led = 22;
constexpr unsigned led_count = 2;
// ExpressLRS LR1121 driver default (Nomad target has no override).
constexpr const char *rf_switch = "0f00040808020001";
#endif
constexpr bool has_fan = fan >= 0;
constexpr uint16_t port[2] = {1121, 1122};
constexpr uint32_t stream_spi = 16000000;
constexpr uint32_t uart_baud = 921600;
constexpr unsigned queue_frames = 48;
}
