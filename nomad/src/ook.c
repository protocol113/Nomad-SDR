/* SPDX-License-Identifier: MIT */
#include "ook.h"

static const ook_pa_profile_t PROFILES[3] = {
    {{0, 0, 4, 0}, -17, 14}, /* lp */
    {{1, 1, 4, 7},  -9, 22}, /* hp */
    {{2, 0, 4, 0}, -18, 13}, /* hf */
};

const ook_pa_profile_t *ook_pa_profile(unsigned index) {
    return index < 3 ? &PROFILES[index] : 0;
}

static bool streq(const char *a, const char *b) {
    while (*a && *a == *b) { ++a; ++b; }
    return *a == *b;
}

int ook_pa_index(const char *name) {
    if (!name) return -1;
    if (streq(name, "lp")) return 0;
    if (streq(name, "hp")) return 1;
    if (streq(name, "hf")) return 2;
    return -1;
}

static size_t op(uint8_t *out, uint16_t opcode) {
    out[0] = (uint8_t)(opcode >> 8);
    out[1] = (uint8_t)opcode;
    return 2;
}

size_t ook_cmd_standby(uint8_t *out, uint8_t mode) {
    size_t n = op(out, 0x011c);
    out[n++] = mode;
    return n;
}

size_t ook_cmd_calibrate_image(uint8_t *out, uint8_t f1, uint8_t f2) {
    size_t n = op(out, 0x0111);
    out[n++] = f1;
    out[n++] = f2;
    return n;
}

size_t ook_cmd_set_frequency(uint8_t *out, uint32_t hz) {
    size_t n = op(out, 0x020b);
    out[n++] = (uint8_t)(hz >> 24);
    out[n++] = (uint8_t)(hz >> 16);
    out[n++] = (uint8_t)(hz >> 8);
    out[n++] = (uint8_t)hz;
    return n;
}

size_t ook_cmd_pkt_type(uint8_t *out, uint8_t type) {
    size_t n = op(out, 0x020e);
    out[n++] = type;
    return n;
}

size_t ook_cmd_tx_params(uint8_t *out, uint8_t power, uint8_t ramp) {
    size_t n = op(out, 0x0211);
    out[n++] = power;
    out[n++] = ramp;
    return n;
}

size_t ook_cmd_pa_config(uint8_t *out, const uint8_t pa[4]) {
    size_t n = op(out, 0x0215);
    for (unsigned i = 0; i < 4; ++i) out[n++] = pa[i];
    return n;
}

size_t ook_cmd_tx_cw(uint8_t *out) {
    return op(out, 0x0219);
}

size_t ook_cmd_set_rx(uint8_t *out, uint32_t timeout) {
    size_t n = op(out, 0x0209);
    out[n++] = (uint8_t)(timeout >> 16);
    out[n++] = (uint8_t)(timeout >> 8);
    out[n++] = (uint8_t)timeout;
    return n;
}

size_t ook_cmd_get_rssi(uint8_t *out) {
    return op(out, 0x0205);
}

size_t ook_cmd_gfsk_mod(uint8_t *out, uint32_t bitrate, uint8_t pulse_shape,
                        uint8_t bw, uint32_t fdev) {
    size_t n = op(out, 0x020f);
    out[n++] = (uint8_t)(bitrate >> 24);
    out[n++] = (uint8_t)(bitrate >> 16);
    out[n++] = (uint8_t)(bitrate >> 8);
    out[n++] = (uint8_t)bitrate;
    out[n++] = pulse_shape;
    out[n++] = bw;
    out[n++] = (uint8_t)(fdev >> 24);
    out[n++] = (uint8_t)(fdev >> 16);
    out[n++] = (uint8_t)(fdev >> 8);
    out[n++] = (uint8_t)fdev;
    return n;
}

bool ook_image_cal_band(uint32_t hz, uint8_t *f1, uint8_t *f2) {
    /* Same window as host/tx.py: documented for the LF path, 400..1000 MHz. */
    if (hz < 400000000u || hz > 1000000000u) return false;
    uint32_t mhz = hz / 1000000u;
    *f1 = (uint8_t)((mhz - 1u) / 4u);          /* floor */
    *f2 = (uint8_t)((mhz + 1u + 3u) / 4u);     /* ceil */
    return true;
}

uint16_t ook_pulse_at(const uint8_t *tx, unsigned i) {
    return (uint16_t)((tx[i * 2] << 8) | tx[i * 2 + 1]);
}
