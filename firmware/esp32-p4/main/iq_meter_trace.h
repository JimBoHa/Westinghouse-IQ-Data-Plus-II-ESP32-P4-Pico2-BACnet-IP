#pragma once
#include "iq_transport.h"
#include "cJSON.h"
#define IQ_TRANSACTION_CAPACITY 8
typedef struct {
    iq_stream_t stream;
    uint64_t sequence,uptime_ms,host_elapsed_ms;
    int64_t utc_ms;
    bool accepted;
} iq_transaction_trace_t;
typedef struct {
    iq_transaction_trace_t entries[IQ_TRANSACTION_CAPACITY];
    uint64_t total;
    unsigned count,next;
} iq_transaction_ring_t;
void iq_transaction_append(iq_transaction_ring_t *ring,const iq_stream_t *stream,
    uint64_t uptime_ms,int64_t utc_ms,uint64_t host_elapsed_ms,bool accepted);
cJSON *iq_transaction_json(const iq_transaction_trace_t *trace);
cJSON *iq_transaction_ring_json(const iq_transaction_ring_t *ring);
