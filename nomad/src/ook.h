/* SPDX-License-Identifier: MIT */
/* OOK (on-off keying) for the LR1121: carrier-keyed TX and RSSI-envelope RX.
 *
 * The LR1121 has no native OOK modem. These helpers build the stock SPI
 * commands (opcodes verified against Semtech SWDR001) that let the ESP32 key
 * an unmodulated carrier (SetTxCw / SetStandby) on a precise timer, and poll
 * instantaneous RSSI (GetRssiInst) to recover an OOK envelope. Pure C with no
 * Arduino dependency so it is unit-tested natively (nomad/tests/test_ook.c).
 *
 * Frequency is the chip's full synthesizer range; the caller is responsible for
 * on-air legality (power, duty cycle, band) at whatever frequency it selects.
 */
#ifndef NOMAD_OOK_H
#define NOMAD_OOK_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Bounds shared by firmware and host, so both reject the same inputs. */
#define OOK_MAX_PULSES 2048u       /* edges per burst (tx[] is BE u16 each) */
#define OOK_MAX_PULSE_US 32768u    /* one level's duration ceiling */
#define OOK_MAX_TX_TOTAL_US 2000000u /* summed on+off+gap across repeats */
#define OOK_MAX_RX_MS 30000u       /* one receive window */
#define OOK_MIN_POLL_US 5u
#define OOK_MAX_EDGES 2048u        /* RX edges reported before truncation */

/* LR1121 PA supply/select profiles, matching host/tx.py PA_PROFILES.
 * {pa_sel, pa_reg_supply, pa_duty_cycle, pa_hp_sel}, power range in dBm. */
typedef struct { uint8_t config[4]; int8_t min_dbm; int8_t max_dbm; } ook_pa_profile_t;
/* profile index: 0=lp, 1=hp, 2=hf. Returns NULL if out of range. */
const ook_pa_profile_t *ook_pa_profile(unsigned index);
/* Map a name ("lp"/"hp"/"hf") to an index, or -1. */
int ook_pa_index(const char *name);

/* Command frame builders. Each writes opcode (big-endian) + params to out and
 * returns the byte count. out must hold at least 12 bytes. */
size_t ook_cmd_standby(uint8_t *out, uint8_t mode);               /* 011c mode (0 RC,1 XOSC) */
size_t ook_cmd_calibrate_image(uint8_t *out, uint8_t f1, uint8_t f2); /* 0111 f1 f2 */
size_t ook_cmd_set_frequency(uint8_t *out, uint32_t hz);          /* 020b u32be */
size_t ook_cmd_pkt_type(uint8_t *out, uint8_t type);              /* 020e type (1 GFSK) */
size_t ook_cmd_tx_params(uint8_t *out, uint8_t power, uint8_t ramp); /* 0211 power ramp */
size_t ook_cmd_pa_config(uint8_t *out, const uint8_t pa[4]);      /* 0215 .... */
size_t ook_cmd_tx_cw(uint8_t *out);                               /* 0219 */
size_t ook_cmd_set_rx(uint8_t *out, uint32_t timeout);            /* 0209 u24be */
size_t ook_cmd_get_rssi(uint8_t *out);                            /* 0205 */
size_t ook_cmd_gfsk_mod(uint8_t *out, uint32_t bitrate, uint8_t pulse_shape,
                        uint8_t bw, uint32_t fdev);               /* 020f (10 bytes) */

/* LF image calibration band bytes, same heuristic as host/tx.py (400..1000 MHz
 * gets a 1 MHz-margin band pair). Returns false when no calibration applies. */
bool ook_image_cal_band(uint32_t hz, uint8_t *f1, uint8_t *f2);

/* Big-endian u16 pulse accessor over the packed tx[] buffer. */
uint16_t ook_pulse_at(const uint8_t *tx, unsigned i);

#endif
