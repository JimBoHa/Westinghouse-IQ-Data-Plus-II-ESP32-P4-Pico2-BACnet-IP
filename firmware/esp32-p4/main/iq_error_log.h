#pragma once
#include "iq_journal.h"
#include "cJSON.h"
#define IQ_ERROR_BUCKETS 16u
#define IQ_ERROR_REPEAT_MS 60000u
typedef struct {
    iq_journal_record_t latest;
    uint64_t emitted_ms;
    uint32_t pending;
    bool used;
} iq_error_bucket_t;
typedef struct { iq_error_bucket_t buckets[IQ_ERROR_BUCKETS];uint64_t coalesced; } iq_error_policy_t;
typedef bool (*iq_error_emit_t)(void *,const iq_journal_record_t *);
bool iq_error_should_persist(const char *component,const char *event,int code);
bool iq_error_policy_submit(iq_error_policy_t *p,const iq_journal_record_t *r,uint64_t now,
    iq_error_emit_t emit,void *context);
bool iq_error_policy_flush(iq_error_policy_t *p,uint64_t now,bool force,iq_error_emit_t emit,void *context);
#ifdef ESP_PLATFORM
#include "iq_diagnostics.h"
void iq_error_log_init(void);
void iq_error_log_submit(const iq_event_t *event);
bool iq_error_log_flush(unsigned timeout_ms);
cJSON *iq_error_log_status(void);
cJSON *iq_error_log_json(uint64_t before,unsigned limit);
#endif
