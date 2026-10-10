#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "iq_points.h"

typedef enum { IQ_STANDARD, IQ_FLAGS, IQ_SETTINGS, IQ_TRIP, IQ_KIND_COUNT } iq_kind_t;
typedef enum {
    IQ_QUALITY_GOOD, IQ_QUALITY_NO_DATA, IQ_QUALITY_COMMUNICATION,
    IQ_QUALITY_STALE, IQ_QUALITY_METER_VALUE, IQ_QUALITY_ZERO_LOAD,
    IQ_QUALITY_HISTORY, IQ_QUALITY_SETTINGS, IQ_QUALITY_NO_TRIP,
    IQ_QUALITY_UNSUPPORTED, IQ_QUALITY_UNDEFINED, IQ_QUALITY_COUNT
} iq_quality_t;
typedef struct { double value; uint64_t stamp; bool valid; iq_quality_t quality; } iq_value_t;
iq_quality_t iq_value_quality(const iq_value_t *value);
const char *iq_quality_name(iq_quality_t quality);
const char *iq_quality_description(iq_quality_t quality);
/* BACnet standard Reliability enumeration, independent of the stack headers. */
unsigned iq_quality_reliability(iq_quality_t quality);
const char *iq_quality_reliability_name(iq_quality_t quality);
typedef struct iq_model iq_model_t;

const char *iq_kind_name(iq_kind_t kind);
uint32_t iq_request_payload(iq_kind_t kind, uint16_t address);
bool iq_decode(iq_kind_t kind, const uint32_t *words, size_t count,
               iq_value_t out[IQ_POINT_COUNT]);
bool iq_impacc(uint32_t payload, double *value);
iq_model_t *iq_model_create(void);
void iq_model_destroy(iq_model_t *model);
/* Single owner or externally locked. Times are monotonic milliseconds. */
bool iq_model_accept(iq_model_t *model, iq_kind_t kind, const uint32_t *words,
                     size_t count, uint64_t now, double duration, int stop, uint32_t malformed);
void iq_model_fail(iq_model_t *model, iq_kind_t kind, uint64_t now,
                   double duration, int stop, uint32_t malformed);
void iq_model_snapshot(iq_model_t *model, uint64_t now, iq_value_t out[IQ_POINT_COUNT]);
size_t iq_model_bytes(void);
