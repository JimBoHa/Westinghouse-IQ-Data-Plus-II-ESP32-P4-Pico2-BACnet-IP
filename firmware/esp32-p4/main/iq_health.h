#pragma once
#include <stdbool.h>
#include <stdint.h>
typedef struct { uint64_t last_sample; unsigned consecutive; } iq_health_gate_t;
/* Five distinct one-second samples after a ten-second startup grace period. */
bool iq_health_sample(iq_health_gate_t *gate, uint64_t elapsed_ms, bool healthy);
#ifdef ESP_PLATFORM
#include "cJSON.h"
void iq_health_begin(void);
void iq_health_start(bool (*healthy)(void));
bool iq_health_accepted(void);
cJSON *iq_health_json(void);
#endif
