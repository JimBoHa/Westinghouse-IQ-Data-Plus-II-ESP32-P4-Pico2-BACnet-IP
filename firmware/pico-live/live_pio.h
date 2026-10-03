#pragma once
#include "engine.h"

enum {
    LIVE_PIO_ORIGIN_REQUEST = 1,
    LIVE_PIO_ORIGIN_COMPLETION = 2,
    LIVE_PIO_ORIGIN_METER = 3
};

// Call from core1 for MODE_TRANSACTION only. Owns PIO0 SM0/SM2/SM3, PIO1 SM0
// and the read-only clock pulse probe on PIO2 SM0.
// Results and events are already in microseconds; do not finalize them again.
// EV_READ describes completion of our own presented image, not received data.
// EV_FRAGMENT clocks=UINT32_MAX means the interrupted read length is unknown.
// clock_rises counts completed own reads plus captured writes, not idle polls.
// Event timestamps and GPIO-edge diagnostics are CPU observations; an external
// analyzer is required for independent waveform timing/provenance validation.
void live_pio_run(live_config_t config);

// Emergency interlock: force DATA/INT OE LOW before stopping PIO. Leaves the
// overrides LOW until normal cleanup or a new explicit transaction setup.
void live_pio_release(void);
