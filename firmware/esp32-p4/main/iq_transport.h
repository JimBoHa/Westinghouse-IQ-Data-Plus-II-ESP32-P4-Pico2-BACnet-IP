#pragma once
#include "iq_model.h"
#define IQ_LINE_CAPACITY 1024
typedef struct {
    iq_kind_t kind;
    uint16_t address;
    uint32_t payload, image, words[18];
    unsigned word_count, events, requests, completed_requests, writes;
    unsigned completions, completed_completions, active_origin;
    uint32_t active_word, active_clocks, completion_us, last_us;
    bool started, terminal, handshake, handshake_completed, failed, have_time;
    int stop;
    uint32_t malformed;
    size_t used;
    char line[IQ_LINE_CAPACITY], error[192];
} iq_stream_t;

void iq_stream_init(iq_stream_t *stream, iq_kind_t kind, uint16_t address);
bool iq_stream_feed(iq_stream_t *stream, const void *bytes, size_t length);
bool iq_stream_finish(iq_stream_t *stream);
/* Terminal status must also survive a later transport/parsing failure. */
void iq_stream_error(iq_stream_t *stream, const char *error);
bool iq_pico_identity(const char *line, char *version, size_t size);
