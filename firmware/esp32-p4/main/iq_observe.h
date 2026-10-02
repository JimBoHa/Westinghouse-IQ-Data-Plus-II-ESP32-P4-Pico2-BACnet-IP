#pragma once
#include "iq_transport.h"
#include "cJSON.h"
typedef struct {
    bool valid;
    uint32_t pins,output_enables,control[4];
} iq_pin_status_t;
typedef enum { IQ_OBSERVE_IDLE,IQ_OBSERVE_QUEUED,IQ_OBSERVE_RUNNING,
    IQ_OBSERVE_COMPLETE,IQ_OBSERVE_FAILED } iq_observe_state_t;
typedef struct {
    iq_observe_state_t state;
    uint32_t sequence;
    uint64_t queued_ms,finished_ms;
    iq_pin_status_t before,after;
    unsigned phase,events;
    uint32_t clock_hz,max_gap_cycles,active_limit_cycles,last_event_us;
    uint32_t result[IQ_RESULT_COUNT],result_present;
    bool timing,terminal,released;
    size_t used;
    char line[IQ_LINE_CAPACITY],error[192];
} iq_observe_t;
void iq_observe_error(iq_observe_t *probe,const char *error);
bool iq_observe_feed(iq_observe_t *probe,const void *bytes,size_t length);
bool iq_observe_finish(iq_observe_t *probe);
cJSON *iq_observe_json(const iq_observe_t *probe);
