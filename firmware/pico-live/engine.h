#pragma once
#include <stdint.h>
#include <stdbool.h>

enum { PIN_CLK=0, PIN_RW=1, PIN_DATA=2, PIN_INT=3 };
enum { MODE_OBSERVE=1, MODE_INT=2, MODE_TRANSACTION=3 };
enum { EV_READ=1, EV_WRITE=2, EV_EMPTY_WRITE=3, EV_REQUEST=4, EV_COMPLETE=5, EV_FAULT=6, EV_FRAGMENT=7 };
enum { STOP_DEADLINE=0, STOP_ABORT=1, STOP_LOG_FULL=2, STOP_INT_HIGH=3, STOP_DATA_HIGH=4, STOP_RW_CLOCK=5, STOP_BAD_WRITE=6, STOP_LOOP_GAP=7, STOP_AMBIGUOUS=8 };
typedef struct { uint32_t mode, duration_ms, payload; bool falling, repeat_once; } live_config_t;
typedef struct { uint32_t us, kind, clocks, word, origin; } live_event_t;
typedef struct {
    uint32_t elapsed_us, clock_rises, rw_falls, rw_rises, data_edges, int_edges;
    // max_loop_us rounds the conservative bracketed cycle gap up to us;
    // late_loops counts bracketed cycle gaps strictly greater than one us.
    uint32_t max_loop_us, late_loops, events, valid_writes, malformed_writes;
    uint32_t max_sample_gap_cycles, sample_gap_limit_cycles, sys_hz;
    uint32_t stop, initial_pins, final_pins, requests, completions;
    uint32_t startup_retries_before, startup_retries_after;
} live_result_t;
#define LIVE_EVENT_CAPACITY 4096
extern live_event_t live_events[LIVE_EVENT_CAPACITY];
extern live_result_t live_result;
extern volatile bool live_active, live_abort;
void live_init(void);
bool live_start(live_config_t config);
void live_release(void);
