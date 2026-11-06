/* SPDX-License-Identifier: MIT */
#include "ook.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static int eqhex(const uint8_t *b, size_t n, const char *hex) {
    char got[64];
    for (size_t i = 0; i < n; ++i) sprintf(got + i * 2, "%02x", b[i]);
    got[n * 2] = 0;
    return strcmp(got, hex) == 0;
}

int main(void) {
    uint8_t b[16];

    /* Opcode framing matches Semtech SWDR001. */
    assert(eqhex(b, ook_cmd_standby(b, 1), "011c01"));
    assert(eqhex(b, ook_cmd_set_frequency(b, 303875000u), "020b121cc3b8"));
    assert(eqhex(b, ook_cmd_pkt_type(b, 1), "020e01"));
    assert(eqhex(b, ook_cmd_tx_params(b, (uint8_t)(-9), 2), "0211f702"));
    assert(eqhex(b, ook_cmd_tx_cw(b), "0219"));
    assert(eqhex(b, ook_cmd_get_rssi(b), "0205"));
    assert(eqhex(b, ook_cmd_set_rx(b, 0xffffffu), "0209ffffff"));

    /* 303.875 MHz encodes as 0x121cc3b8. */
    assert(303875000u == 0x121cc3b8u);

    /* PA profiles match host/tx.py. */
    uint8_t pa[4];
    const ook_pa_profile_t *hp = ook_pa_profile((unsigned)ook_pa_index("hp"));
    assert(hp && hp->config[0] == 1 && hp->config[3] == 7 && hp->max_dbm == 22);
    memcpy(pa, hp->config, 4);
    assert(eqhex(b, ook_cmd_pa_config(b, pa), "021501010407"));
    assert(ook_pa_index("lp") == 0 && ook_pa_index("hf") == 2 && ook_pa_index("xx") == -1);

    /* Image calibration: on inside LF band, off at 303.875 MHz. */
    uint8_t f1, f2;
    assert(!ook_image_cal_band(303875000u, &f1, &f2));
    assert(ook_image_cal_band(915000000u, &f1, &f2));
    /* 915 MHz -> floor(914/4)=228, ceil(916/4)=229 */
    assert(f1 == 228 && f2 == 229);
    assert(ook_image_cal_band(868000000u, &f1, &f2));

    /* GFSK mod params: 10 payload bytes after the opcode. */
    assert(ook_cmd_gfsk_mod(b, 4800, 0, 0x0b, 19000) == 12);

    /* Big-endian u16 pulse accessor. */
    uint8_t pulses[6] = {0x01, 0x2c, 0x03, 0xe8, 0xff, 0xff};
    assert(ook_pulse_at(pulses, 0) == 300);
    assert(ook_pulse_at(pulses, 1) == 1000);
    assert(ook_pulse_at(pulses, 2) == 65535);

    printf("test_ook: all assertions passed\n");
    return 0;
}
