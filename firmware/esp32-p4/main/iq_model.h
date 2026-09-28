#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "iq_points.h"

typedef enum { IQ_STANDARD, IQ_FLAGS, IQ_SETTINGS, IQ_TRIP, IQ_KIND_COUNT } iq_kind_t;
typedef struct { double value; uint64_t stamp; bool valid; } iq_value_t;
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
