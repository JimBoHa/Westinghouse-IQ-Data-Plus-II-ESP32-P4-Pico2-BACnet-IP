#pragma once
#include "iq_model.h"
#include "../../common/pio_diagnostics.h"
#define IQ_LINE_CAPACITY 1024
#define IQ_TRACE_EVENT_CAPACITY 64
typedef enum {
    IQ_RESULT_STOP, IQ_RESULT_ELAPSED, IQ_RESULT_CLOCKS, IQ_RESULT_RW_FALLS,
    IQ_RESULT_RW_RISES, IQ_RESULT_DATA_EDGES, IQ_RESULT_INT_EDGES,
    IQ_RESULT_MAX_LOOP, IQ_RESULT_LATE_LOOPS, IQ_RESULT_EVENTS,
    IQ_RESULT_WRITES, IQ_RESULT_MALFORMED, IQ_RESULT_REQUESTS,
    IQ_RESULT_COMPLETIONS, IQ_RESULT_INITIAL_PINS, IQ_RESULT_FINAL_PINS,
    IQ_RESULT_RETRIES_BEFORE, IQ_RESULT_RETRIES_AFTER, IQ_RESULT_COUNT
} iq_result_field_t;
typedef enum {
    IQ_CHECK_STOP, IQ_CHECK_MALFORMED, IQ_CHECK_RELEASED, IQ_CHECK_EVENTS,
    IQ_CHECK_REQUESTS, IQ_CHECK_COMPLETIONS, IQ_CHECK_WRITES,
    IQ_CHECK_REQUEST_UNFINISHED, IQ_CHECK_COMPLETION_MISSING,
    IQ_CHECK_COMPLETION_UNFINISHED, IQ_CHECK_ACTIVE, IQ_CHECK_NO_DATA,
    IQ_CHECK_COUNT
} iq_response_check_t;
typedef enum {
    IQ_TRACE_UNKNOWN, IQ_TRACE_REQUEST, IQ_TRACE_COMPLETION, IQ_TRACE_READ,
    IQ_TRACE_FRAGMENT, IQ_TRACE_EMPTY, IQ_TRACE_WRITE, IQ_TRACE_FAULT, IQ_TRACE_COUNT
} iq_trace_event_kind_t;
typedef struct {
    uint32_t us, origin, clocks, word;
    uint8_t kind;
    int8_t data_released; /* -1 means missing/invalid, never assume false or zero. */
} iq_trace_event_t;
extern const char *const iq_result_names[IQ_RESULT_COUNT];
extern const char *const iq_check_names[IQ_CHECK_COUNT];
extern const char *const iq_trace_names[IQ_TRACE_COUNT];
extern const char *const iq_pio_diagnostic_names[IQ_PIO_DIAGNOSTIC_COUNT];
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
    /* Bounded diagnostics only; none of these fields authorize measurements. */
    uint32_t result[IQ_RESULT_COUNT], result_present, failed_checks;
    uint32_t bytes_received, records_received, trace_total;
    unsigned trace_count, trace_next;
    int8_t released;
    bool timing_pio;
    uint32_t clock_hz;
    uint32_t pio_diagnostics[IQ_PIO_DIAGNOSTIC_COUNT],pio_diagnostics_present;
    iq_trace_event_t trace[IQ_TRACE_EVENT_CAPACITY];
} iq_stream_t;

void iq_stream_init(iq_stream_t *stream, iq_kind_t kind, uint16_t address);
bool iq_stream_feed(iq_stream_t *stream, const void *bytes, size_t length);
bool iq_stream_finish(iq_stream_t *stream);
/* Terminal status must also survive a later transport/parsing failure. */
void iq_stream_error(iq_stream_t *stream, const char *error);
bool iq_pico_identity(const char *line, char *version, size_t size);
