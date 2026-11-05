/* SPDX-License-Identifier: MIT */
#ifndef BRIDGE_PROTOCOL_H
#define BRIDGE_PROTOCOL_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define BRIDGE_MAX_BYTES 4096u
#define BRIDGE_LINE_SIZE (2u * BRIDGE_MAX_BYTES + 128u)
#define BRIDGE_MAX_TIMEOUT_MS 10000u

typedef enum {
    CMD_INFO, CMD_PINS, CMD_RESET, CMD_SPEED, CMD_XFER,
    CMD_READ, CMD_WRITE, CMD_WAIT, CMD_BOOTSEL, CMD_BENCH, CMD_STREAM, CMD_STREAM2, CMD_PACKED,
    CMD_GUARD, CMD_GUARD_STATUS,
    /* ESP32-only (nomad/): parsed in parseNomadRequest, never by parse_request. */
    CMD_OOKTX, CMD_OOKRX
} command_t;

typedef struct {
    uint32_t id;
    command_t command;
    uint32_t timeout_ms;
    uint32_t speed_hz;
    uint32_t iterations;
    uint32_t guard_us;
    uint32_t pause_ms;
    uint32_t sample_rate;
    size_t tx_len;
    size_t read_len;
    /* OOK (CMD_OOKTX / CMD_OOKRX), set only by the ESP32 parser. TX pulse
     * durations (big-endian u16 microseconds, alternating on/off) are packed
     * in tx[] with ook_pulse_count entries. */
    uint32_t ook_freq_hz;
    uint32_t ook_repeat;
    uint32_t ook_gap_us;
    uint32_t ook_duration_ms;
    uint32_t ook_poll_us;
    uint32_t ook_bw;
    int32_t ook_threshold_dbm_x2;
    uint32_t ook_pulse_count;
    uint8_t ook_pa[4];
    uint8_t ook_power;
    uint8_t ook_ramp;
    uint8_t tx[BRIDGE_MAX_BYTES];
} request_t;

// Mutates line. No hardware is touched unless the entire request is valid.
bool parse_request(char *line, request_t *req, const char **error);
#endif
