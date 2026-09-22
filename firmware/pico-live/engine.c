#include "engine.h"
#include "live_pio.h"
#include <string.h>
#include "pico/stdlib.h"
#include "pico/multicore.h"
#include "hardware/sync.h"
#include "hardware/structs/sio.h"
#include "hardware/structs/timer.h"
#include "hardware/clocks.h"

#define CLK (1u << PIN_CLK)
#define RW (1u << PIN_RW)
#define DATA (1u << PIN_DATA)
#define INT (1u << PIN_INT)
#define DRIVE (DATA | INT)
live_event_t live_events[LIVE_EVENT_CAPACITY];
live_result_t live_result;
volatile bool live_active, live_abort;
static volatile bool pending;
static live_config_t config;
static uint32_t system_hz;

// Event timestamps remain raw elapsed cycles until core1 releases the outputs
// and converts the completed result for the existing microsecond USB contract.
static inline __attribute__((always_inline)) bool record(live_result_t *result, uint32_t cycles, uint32_t kind, uint32_t clocks, uint32_t word, uint32_t origin) {
    uint32_t n = result->events;
    if (n == LIVE_EVENT_CAPACITY) return false;
    live_events[n] = (live_event_t){cycles,kind,clocks,word,origin};
    result->events = n + 1;
    return true;
}
void live_release(void) { sio_hw->gpio_oe_clr = DRIVE; }

// Dedicated core, interrupts disabled, all timing code/data in SRAM. Neither
// CLK nor RW ever has an output enable. USB and formatting stay on core 0.
static void __attribute__((noinline)) __not_in_flash_func(run_trial)(live_config_t c) {
    // Only core1 owns counters during capture; publish the finished aggregate
    // after releasing outputs, before the existing completion barrier.
    live_result_t result={0};
    const uint32_t budget = c.duration_ms * (system_hz / 1000u);
    const uint32_t cycles_1us = system_hz / 1000000u;
    const uint32_t cycles_2us = system_hz / 500000u;
    const uint32_t cycles_10us = system_hz / 100000u;
    const uint32_t cycles_20us = system_hz / 50000u;
    const uint32_t cycles_100us = system_hz / 10000u;
    const uint32_t cycles_20ms = system_hz / 50u;
    const uint32_t began = timer1_hw->timerawl;
    uint32_t before = sio_hw->gpio_in & 15u;
    uint32_t last_clock = began, read_count=0, read_word=0, write_count=0, write_word=0;
    uint32_t image=0, image_kind=0, shifts=0, data_low_since=0, int_low_since=began;
    uint32_t read_origin=0;
    bool data_low=false, int_low=c.mode!=MODE_OBSERVE, sent=false, complete_pending=false;
    bool collecting_write=false;
    result.initial_pins=before;
    result.sys_hz=system_hz;
    result.sample_gap_limit_cycles=system_hz/1500000u;
    sio_hw->gpio_oe_clr=DRIVE;
    if (int_low) sio_hw->gpio_oe_set=INT;
    uint32_t previous_pre=timer1_hw->timerawl;
    while (true) {
        uint32_t sample_pre=timer1_hw->timerawl;
        uint32_t pins=sio_hw->gpio_in & 15u;
        uint32_t sample_post=timer1_hw->timerawl;
        // Bracket GPIO sampling. Previous pre -> current post is a conservative
        // bound, including APB read latency; TIMER1 counts clk_sys directly.
        uint32_t sample_gap=sample_post-previous_pre;
        previous_pre=sample_pre;
        if (sample_gap>result.max_sample_gap_cycles) result.max_sample_gap_cycles=sample_gap;
        uint32_t now=sample_post, elapsed=now-began;
        if (sample_gap>cycles_1us) ++result.late_loops;
        if (live_abort) { result.stop=STOP_ABORT; break; }
        if (elapsed>=budget) break;
        // This detects a missed-timing risk after it occurs; it is not a
        // hardware ownership interlock. The analyzer must qualify real trials.
        // Constant INT trials have no edge-driven output and never drive DATA.
        // Their finite deadline/readback checks remain in force; sampling-gap
        // faults apply to the edge-critical transaction path only.
        if (sample_gap>result.sample_gap_limit_cycles && c.mode==MODE_TRANSACTION) { result.stop=STOP_LOOP_GAP; break; }
        uint32_t changed=pins^before;
        if ((changed&(RW|CLK))==(RW|CLK) && (pins&CLK)) {
            // Ordering is unknowable if RW and a sampling edge appeared
            // together. Do not turn a possible 26-bit write into a valid25.
            sio_hw->gpio_oe_clr=DRIVE;
            record(&result,elapsed,EV_FAULT,write_count,write_word,STOP_AMBIGUOUS);
            result.stop=STOP_AMBIGUOUS;
            break;
        }
        if (changed&DATA) ++result.data_edges;
        if (changed&INT) ++result.int_edges;
        if ((changed&RW) && !(pins&RW)) {
            // Ownership transfer takes precedence over every other event.
            sio_hw->gpio_oe_clr=DATA;
            data_low=false; image_kind=0;
            ++result.rw_falls;
            collecting_write=true; write_count=0; write_word=0;
            if (!record(&result,elapsed,EV_READ,read_count,read_word,read_origin)) { result.stop=STOP_LOG_FULL; break; }
            read_count=0; read_word=0; read_origin=0;
            if ((pins&CLK) && c.mode==MODE_TRANSACTION) { result.stop=STOP_RW_CLOCK; break; }
        }
        if (changed&CLK) {
            last_clock=now;
            if (pins&CLK) {
                ++result.clock_rises;
                if (c.mode==MODE_TRANSACTION) {
                    if (!int_low) { sio_hw->gpio_oe_set=INT; int_low=true; int_low_since=now; }
                }
                if (pins&RW) {
                    if (!read_count) read_origin=image_kind;
                    if (read_count<27 && (pins&DATA)) read_word|=1u<<read_count;
                    if (read_count<0xffff) ++read_count;
                } else if (collecting_write) {
                    if (write_count<25 && (pins&DATA)) write_word|=1u<<write_count;
                    if (write_count<26) ++write_count;
                }
            }
            if (c.mode==MODE_TRANSACTION && (pins&RW) && image_kind &&
                (c.falling ? !(pins&CLK) : !!(pins&CLK))) {
                if (shifts<27) ++shifts;
                bool low=shifts>=27 || !(image&(1u<<shifts));
                if (low!=data_low) {
                    if (low) { sio_hw->gpio_oe_set=DATA; data_low_since=now; }
                    else sio_hw->gpio_oe_clr=DATA;
                    data_low=low;
                }
            }
        }
        // Clock-only fragments are separate from complete RW-terminated polls.
        if ((pins&RW) && !(pins&CLK) && read_count && now-last_clock>=cycles_100us) {
            if (!record(&result,elapsed,EV_FRAGMENT,read_count,read_word,read_origin)) { result.stop=STOP_LOG_FULL; break; }
            read_count=0; read_word=0; read_origin=0;
        }
        if ((changed&RW) && (pins&RW)) {
            ++result.rw_rises;
            if (collecting_write) {
                if (!record(&result,elapsed,write_count ? EV_WRITE : EV_EMPTY_WRITE,write_count,write_word,1)) { result.stop=STOP_LOG_FULL; break; }
                if (write_count==25) {
                    ++result.valid_writes;
                    if (c.mode==MODE_TRANSACTION) complete_pending=true;
                } else if (write_count) {
                    ++result.malformed_writes;
                    if (c.mode==MODE_TRANSACTION) { result.stop=STOP_BAD_WRITE; break; }
                }
            }
            collecting_write=false; read_count=0; read_word=0;
        }
        if (c.mode==MODE_TRANSACTION && (pins&(CLK|RW))==RW && now-last_clock>=cycles_20us) {
            // One request only, after 20ms inactive INT, between exchanges.
            // A completion is separate and only follows an actual 25-bit write.
            if ((!sent && elapsed>=cycles_20ms) || complete_pending) {
                bool completion=complete_pending;
                image=completion ? 0 : 5u | (c.payload<<3);
                image_kind=completion ? 2 : 1; shifts=0;
                if (completion) { sio_hw->gpio_oe_set=DATA; data_low_since=now; data_low=true; }
                else { sio_hw->gpio_oe_clr=DATA; data_low=false; }
                // Image is established before INT assertion. Gap to the next
                // observed clock is independently checked by the analyzer.
                sio_hw->gpio_oe_clr=INT; int_low=false;
                if (!record(&result,elapsed,completion?EV_COMPLETE:EV_REQUEST,27,image,image_kind)) { result.stop=STOP_LOG_FULL; break; }
                if (completion) ++result.completions;
                else { sent=true; ++result.requests; }
                complete_pending=false;
            }
        }
        // Detect a pad that remains high while commanded LOW. Drive strength
        // is not a current limiter. These are bounded first physical trials.
        if (int_low && (pins&INT) && now-int_low_since>=cycles_10us) { result.stop=STOP_INT_HIGH; break; }
        if (data_low && (pins&DATA) && now-data_low_since>=cycles_2us) { result.stop=STOP_DATA_HIGH; break; }
        before=pins;
    }
    sio_hw->gpio_oe_clr=DRIVE;
    result.elapsed_us=timer1_hw->timerawl-began;
    result.final_pins=sio_hw->gpio_in & 15u;
    live_result=result;
}
static void finalize_timestamps(void) {
    const uint32_t hz=live_result.sys_hz;
    for (uint32_t i=0;i<live_result.events;++i)
        live_events[i].us=(uint32_t)((uint64_t)live_events[i].us*1000000u/hz);
    live_result.elapsed_us=(uint32_t)((uint64_t)live_result.elapsed_us*1000000u/hz);
    live_result.max_loop_us=(uint32_t)(((uint64_t)live_result.max_sample_gap_cycles*1000000u+hz-1u)/hz);
}
static void core1(void) {
    while (true) {
        while (!pending) tight_loop_contents();
        __dmb();
        live_config_t c=config;
        uint32_t irq=save_and_disable_interrupts();
        if (c.mode==MODE_TRANSACTION) live_pio_run(c);
        else run_trial(c);
        restore_interrupts(irq);
        if (c.mode!=MODE_TRANSACTION) finalize_timestamps();
        __dmb(); pending=false; live_active=false;
    }
}
void live_init(void) {
    system_hz=clock_get_hz(clk_sys);
    timer1_hw->source=TIMER_SOURCE_CLK_SYS_BITS;
    for (uint p=0;p<4;++p) {
        gpio_init(p); gpio_disable_pulls(p); gpio_set_dir(p,GPIO_IN);
        gpio_set_outover(p,GPIO_OVERRIDE_LOW);
        gpio_set_drive_strength(p,GPIO_DRIVE_STRENGTH_2MA);
        if (p==PIN_CLK || p==PIN_RW) gpio_set_oeover(p,GPIO_OVERRIDE_LOW);
    }
    live_release();
    multicore_launch_core1(core1);
}
bool live_start(live_config_t c) {
    if (live_active || pending) return false;
    memset(&live_result,0,sizeof live_result);
    config=c; live_abort=false; live_active=true;
    __dmb(); pending=true;
    return true;
}
