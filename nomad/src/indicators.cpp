/* SPDX-License-Identifier: MIT */
#include "indicators.h"
#include "board.h"
#include "driver/rmt.h"
#include <stdlib.h>
#include <string.h>
#include <algorithm>
extern "C" {
#include "stream_format.h"
}

namespace {
enum class FanMode { On, Off, Auto };
enum class LedMode { Auto, Manual };
enum class LedState { Ready, Working, Buffered, Error };

constexpr rmt_channel_t kLedChannel = RMT_CHANNEL_0;
constexpr uint32_t kTickNs = 25; // 80 MHz / clk_div 2
constexpr uint32_t kT0h = 400 / kTickNs, kT0l = 850 / kTickNs;
constexpr uint32_t kT1h = 800 / kTickNs, kT1l = 450 / kTickNs;
constexpr uint8_t kFull = 255;
constexpr float kFanOnC = 50.0f;
constexpr float kFanOffC = 42.0f;
constexpr uint32_t kTempPeriodUs = 250000;
constexpr uint32_t kErrorBits = IQ_DMA_STARVED | IQ_QUEUE_FULL | IQ_RADIO_ERROR | IQ_FORMAT_ERROR;

FanMode fanMode = FanMode::Auto;
LedMode ledMode = LedMode::Auto;
// One logical status per radio. Boards with a single pixel (XR4) show the
// more severe of the two; LedState is ordered by severity.
constexpr unsigned kLogicalLeds = 2;
static_assert(board::led_count >= 1 && board::led_count <= kLogicalLeds, "led_count");
LedState ledState[kLogicalLeds] = {LedState::Ready, LedState::Ready};
uint8_t ledRgb[kLogicalLeds][3] = {{0, 0, 0}, {0, 0, 0}};
bool fanOutput = false;
bool ledReady = false;
float tempC = 0;
uint64_t lastTempUs = 0;
bool radioActive[2] = {};
uint32_t radioFlags[2] = {};
unsigned radioQueued[2] = {};
unsigned radioCapacity[2] = {};

uint64_t nowUs() { return esp_timer_get_time(); }

void encodeBit(rmt_item32_t &item, bool one) {
    item.level0 = 1;
    item.duration0 = one ? kT1h : kT0h;
    item.level1 = 0;
    item.duration1 = one ? kT1l : kT0l;
}

void encodeByte(rmt_item32_t *items, uint8_t value) {
    for (int bit = 7; bit >= 0; --bit)
        encodeBit(items[7 - bit], (value >> bit) & 1);
}

void ledShow() {
    if (!ledReady) return;
    rmt_item32_t items[board::led_count * 24];
    for (unsigned i = 0; i < board::led_count; ++i) {
        rmt_item32_t *led = items + i * 24;
        // ExpressLRS Nomad: led_rgb_isgrb.
        encodeByte(led, ledRgb[i][1]);
        encodeByte(led + 8, ledRgb[i][0]);
        encodeByte(led + 16, ledRgb[i][2]);
    }
    rmt_write_items(kLedChannel, items, board::led_count * 24, true);
    delayMicroseconds(60);
}

void setPixel(unsigned index, uint8_t r, uint8_t g, uint8_t b) {
    if (index >= board::led_count) return;
    ledRgb[index][0] = r;
    ledRgb[index][1] = g;
    ledRgb[index][2] = b;
}

void colorFor(LedState state, uint8_t *rgb) {
    switch (state) {
    case LedState::Error: rgb[0] = kFull; rgb[1] = 0; rgb[2] = 0; break;
    case LedState::Working: rgb[0] = 0; rgb[1] = kFull; rgb[2] = 0; break;
    case LedState::Buffered: rgb[0] = kFull; rgb[1] = kFull; rgb[2] = 0; break;
    case LedState::Ready: default: rgb[0] = 0; rgb[1] = 0; rgb[2] = kFull; break;
    }
}

const char *stateName(LedState state) {
    switch (state) {
    case LedState::Error: return "error";
    case LedState::Working: return "working";
    case LedState::Buffered: return "buffered";
    case LedState::Ready: default: return "ready";
    }
}

LedState radioLed(bool active, uint32_t flags, unsigned queued, unsigned capacity) {
    if (flags & kErrorBits) return LedState::Error;
    if (active && capacity && queued * 4u >= capacity) return LedState::Buffered;
    if (active) return LedState::Working;
    return LedState::Ready;
}

void applyFan(bool on) {
    if (!board::has_fan) return;
    if (fanOutput == on) return;
    fanOutput = on;
    digitalWrite(board::fan, on ? HIGH : LOW);
}

void sampleTemperature() {
    uint64_t now = nowUs();
    if (lastTempUs && now - lastTempUs < kTempPeriodUs) return;
    lastTempUs = now;
    tempC = temperatureRead();
}

void applyAutoFan() {
    sampleTemperature();
    if (fanMode == FanMode::On) applyFan(true);
    else if (fanMode == FanMode::Off) applyFan(false);
    else if (!fanOutput && tempC >= kFanOnC) applyFan(true);
    else if (fanOutput && tempC <= kFanOffC) applyFan(false);
}

void applyAutoLeds() {
    if (ledMode != LedMode::Auto) return;
    uint8_t next[kLogicalLeds][3];
    bool changed = false;
    for (unsigned i = 0; i < kLogicalLeds; ++i)
        ledState[i] = radioLed(radioActive[i], radioFlags[i], radioQueued[i], radioCapacity[i]);
    for (unsigned i = 0; i < board::led_count; ++i) {
        LedState shown = ledState[i];
        if (board::led_count == 1) shown = std::max(ledState[0], ledState[1]);
        colorFor(shown, next[i]);
        if (ledRgb[i][0] != next[i][0] || ledRgb[i][1] != next[i][1] || ledRgb[i][2] != next[i][2])
            changed = true;
    }
    if (!changed) return;
    for (unsigned i = 0; i < board::led_count; ++i)
        setPixel(i, next[i][0], next[i][1], next[i][2]);
    ledShow();
}

void jsonStatus(Print &out, uint32_t id) {
    const char *fanName = !board::has_fan ? "none" : fanMode == FanMode::Off ? "off" : fanMode == FanMode::Auto ? "auto" : "on";
    int tempTenths = int(tempC * 10.0f);
    out.printf("{\"id\":%lu,\"ok\":true,\"fan\":\"%s\",\"fan_on\":%s,\"temp_c\":%d.%d,\"fan_on_c\":%d,\"fan_off_c\":%d,\"led\":\"%s\",\"led_count\":%u,\"led_pin\":%d,\"led_order\":\"grb\",\"brightness\":%u,\"states\":[\"%s\",\"%s\"],\"pixels\":[[%u,%u,%u],[%u,%u,%u]]}\n",
        (unsigned long)id, fanName, fanOutput ? "true" : "false",
        tempTenths / 10, abs(tempTenths % 10), int(kFanOnC), int(kFanOffC),
        ledMode == LedMode::Auto ? "auto" : "manual", board::led_count, board::led, kFull,
        stateName(ledState[0]), stateName(ledState[1]),
        ledRgb[0][0], ledRgb[0][1], ledRgb[0][2],
        ledRgb[1][0], ledRgb[1][1], ledRgb[1][2]);
}
}

void indicatorsBegin() {
    if (board::has_fan) pinMode(board::fan, OUTPUT);
    fanMode = FanMode::Auto;
    ledMode = LedMode::Auto;
    rmt_config_t config = {};
    config.rmt_mode = RMT_MODE_TX;
    config.channel = kLedChannel;
    config.gpio_num = static_cast<gpio_num_t>(board::led);
    config.mem_block_num = 1;
    config.clk_div = 2;
    config.tx_config.loop_en = false;
    config.tx_config.carrier_en = false;
    config.tx_config.idle_output_en = true;
    config.tx_config.idle_level = RMT_IDLE_LEVEL_LOW;
    if (rmt_config(&config) == ESP_OK && rmt_driver_install(kLedChannel, 0, 0) == ESP_OK)
        ledReady = true;
    sampleTemperature();
    applyAutoFan();
    applyAutoLeds();
}

void indicatorsTick(bool radio0Active, bool radio1Active,
                    uint32_t radio0Flags, uint32_t radio1Flags,
                    unsigned radio0Queued, unsigned radio0Capacity,
                    unsigned radio1Queued, unsigned radio1Capacity) {
    radioActive[0] = radio0Active; radioActive[1] = radio1Active;
    radioFlags[0] = radio0Flags; radioFlags[1] = radio1Flags;
    radioQueued[0] = radio0Queued; radioQueued[1] = radio1Queued;
    radioCapacity[0] = radio0Capacity; radioCapacity[1] = radio1Capacity;
    applyAutoFan();
    applyAutoLeds();
}

bool indicatorsCommand(const char *line, Print &out, uint32_t id) {
    char command[16];
    if (sscanf(line, "%*u %15s", command) != 1) return false;
    if (!strcmp(command, "fan")) {
        char mode[8] = {}, extra = 0;
        int n = sscanf(line, "%*u %*s %7s %c", mode, &extra);
        if (n == 0 || n == EOF) { jsonStatus(out, id); return true; }
        if (n != 1) {
            out.printf("{\"id\":%lu,\"ok\":false,\"error\":\"fan_on_off_auto\",\"phase\":\"parse\"}\n",
                (unsigned long)id);
            return true;
        }
        if (!strcmp(mode, "on")) fanMode = FanMode::On;
        else if (!strcmp(mode, "off")) fanMode = FanMode::Off;
        else if (!strcmp(mode, "auto")) fanMode = FanMode::Auto;
        else {
            out.printf("{\"id\":%lu,\"ok\":false,\"error\":\"fan_on_off_auto\",\"phase\":\"parse\"}\n",
                (unsigned long)id);
            return true;
        }
        applyAutoFan();
        jsonStatus(out, id);
        return true;
    }
    if (!strcmp(command, "led")) {
        char token[16] = {}, extra = 0;
        unsigned a = 0, b = 0, c = 0, d = 0;
        int n = sscanf(line, "%*u %*s %15s", token);
        if (n == 0 || n == EOF) { jsonStatus(out, id); return true; }
        if (!strcmp(token, "auto")) {
            if (sscanf(line, "%*u %*s %*s %c", &extra) == 1) {
                out.printf("{\"id\":%lu,\"ok\":false,\"error\":\"led_auto\",\"phase\":\"parse\"}\n",
                    (unsigned long)id);
                return true;
            }
            ledMode = LedMode::Auto;
            applyAutoLeds();
            jsonStatus(out, id);
            return true;
        }
        n = sscanf(line, "%*u %*s %u %u %u %u %c", &a, &b, &c, &d, &extra);
        if (n == 3 && a <= 255 && b <= 255 && c <= 255) {
            ledMode = LedMode::Manual;
            setPixel(0, uint8_t(a), uint8_t(b), uint8_t(c));
            setPixel(1, uint8_t(a), uint8_t(b), uint8_t(c));
            ledShow();
            jsonStatus(out, id);
            return true;
        }
        if (n == 4 && a < board::led_count && b <= 255 && c <= 255 && d <= 255) {
            ledMode = LedMode::Manual;
            setPixel(a, uint8_t(b), uint8_t(c), uint8_t(d));
            ledShow();
            jsonStatus(out, id);
            return true;
        }
        out.printf("{\"id\":%lu,\"ok\":false,\"error\":\"led_auto_or_rgb\",\"phase\":\"parse\"}\n",
            (unsigned long)id);
        return true;
    }
    return false;
}
