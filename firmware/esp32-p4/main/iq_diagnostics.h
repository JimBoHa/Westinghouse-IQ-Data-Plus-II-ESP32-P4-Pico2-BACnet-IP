#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "cJSON.h"
#include "iq_transport.h"
#define IQ_EVENT_CAPACITY 32
typedef struct {
    uint64_t sequence, uptime_ms;
    int64_t utc_ms;
    int code;
    char component[16],event[24],detail[160];
} iq_event_t;
typedef struct { iq_event_t events[IQ_EVENT_CAPACITY]; uint64_t total; unsigned count,next; } iq_event_ring_t;
void iq_event_append(iq_event_ring_t *ring,uint64_t uptime,int64_t utc,const char *component,
    const char *event,int code,const char *detail);
cJSON *iq_event_json(const iq_event_ring_t *ring);
#ifdef ESP_PLATFORM
void iq_diagnostics_init(void);
const char *iq_boot_id(void);
void iq_clock_init(void);
void iq_clock_network_ready(void);
bool iq_clock_utc(int64_t *utc_ms);
cJSON *iq_clock_json(void);
cJSON *iq_diagnostics_json(void);
void iq_event(const char *component,const char *event,int code,const char *detail);
void iq_meter_transaction(const iq_stream_t *stream,uint64_t host_elapsed_ms,bool accepted);
#else
static inline void iq_event(const char *component,const char *event,int code,const char *detail)
{ (void)component;(void)event;(void)code;(void)detail; }
#endif
