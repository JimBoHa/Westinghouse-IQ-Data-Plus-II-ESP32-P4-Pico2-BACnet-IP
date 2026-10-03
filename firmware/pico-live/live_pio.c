#include "live_pio.h"
#include <string.h>
#include "pico/stdlib.h"
#include "hardware/clocks.h"
#include "hardware/pio.h"
#include "hardware/structs/sio.h"
#include "hardware/structs/timer.h"
#include "hardware/structs/io_bank0.h"
#include "live_transport.pio.h"
#include "writer_arm.h"

#define CLK (1u << PIN_CLK)
#define RW (1u << PIN_RW)
#define DATA (1u << PIN_DATA)
#define INT (1u << PIN_INT)
#define DRIVE (DATA | INT)
enum { READER=0, INT_CLEAR=2, GUARD=3, WRITER=0, CLOCK_PROBE=0 };
static bool claimed, programs_loaded;
static uint guard_offset, read_offset, int_clear_offset, write_offset, probe_offset;
static const pio_program_t *read_program;
static pio_sm_config read_config, write_config;

static inline uint32_t now_us(void) { return timer_hw->timerawl; }
static bool record(uint32_t elapsed, uint32_t kind, uint32_t clocks,
                   uint32_t word, uint32_t origin) {
    uint32_t n=live_result.events;
    if (n>=LIVE_EVENT_CAPACITY) { live_result.stop=STOP_LOG_FULL; return false; }
    live_events[n]=(live_event_t){elapsed,kind,clocks,word,origin};
    live_result.events=n+1;
    return true;
}

void live_pio_release(void) {
    gpio_set_oeover(PIN_DATA,GPIO_OVERRIDE_LOW);
    gpio_set_oeover(PIN_INT,GPIO_OVERRIDE_LOW);
    if (claimed) {
        pio_set_sm_mask_enabled(pio0,(1u<<READER)|(1u<<INT_CLEAR)|(1u<<GUARD),false);
        pio_sm_set_enabled(pio1,WRITER,false);
        pio_sm_set_enabled(pio2,CLOCK_PROBE,false);
    }
    sio_hw->gpio_oe_clr=DRIVE;
}

static void restore_sio(void) {
    live_pio_release();
    for (uint pin=PIN_DATA;pin<=PIN_INT;++pin) {
        gpio_set_dir(pin,GPIO_IN);
        gpio_set_function(pin,GPIO_FUNC_SIO);
        gpio_disable_pulls(pin);
        gpio_set_outover(pin,GPIO_OVERRIDE_LOW);
        gpio_set_oeover(pin,GPIO_OVERRIDE_NORMAL);
    }
}

static void init_transport(bool falling) {
    live_pio_release();
    if (!claimed) {
        pio_sm_claim(pio0,READER);
        pio_sm_claim(pio0,INT_CLEAR);
        pio_sm_claim(pio0,GUARD);
        pio_sm_claim(pio1,WRITER);
        pio_sm_claim(pio2,CLOCK_PROBE);
        claimed=true;
    }
    if (programs_loaded) {
        pio_remove_program(pio0,read_program,read_offset);
        pio_remove_program(pio0,&live_rw_guard_program,guard_offset);
        pio_remove_program(pio0,&live_int_clear_program,int_clear_offset);
        pio_remove_program(pio1,&live_write_capture_program,write_offset);
        pio_remove_program(pio2,&live_clock_probe_program,probe_offset);
    }
    read_program=falling?&live_read_falling_program:&live_read_rising_program;
    guard_offset=pio_add_program(pio0,&live_rw_guard_program);
    read_offset=pio_add_program(pio0,read_program);
    int_clear_offset=pio_add_program(pio0,&live_int_clear_program);
    write_offset=pio_add_program(pio1,&live_write_capture_program);
    probe_offset=pio_add_program(pio2,&live_clock_probe_program);
    programs_loaded=true;

    read_config=falling?live_read_falling_program_get_default_config(read_offset):
                        live_read_rising_program_get_default_config(read_offset);
    sm_config_set_out_pins(&read_config,PIN_DATA,1);
    sm_config_set_sideset_pins(&read_config,PIN_INT);
    sm_config_set_jmp_pin(&read_config,PIN_RW);
    sm_config_set_out_shift(&read_config,true,false,32);
    sm_config_set_clkdiv(&read_config,1.0f);
    pio_sm_init(pio0,READER,read_offset,&read_config);

    pio_sm_config guard_config=live_rw_guard_program_get_default_config(guard_offset);
    sm_config_set_sideset_pins(&guard_config,PIN_DATA);
    sm_config_set_jmp_pin(&guard_config,PIN_RW);
    sm_config_set_clkdiv(&guard_config,1.0f);
    pio_sm_init(pio0,GUARD,guard_offset+live_rw_guard_offset_held,&guard_config);

    pio_sm_config int_config=live_int_clear_program_get_default_config(int_clear_offset);
    sm_config_set_sideset_pins(&int_config,PIN_INT);
    sm_config_set_jmp_pin(&int_config,PIN_CLK);
    sm_config_set_clkdiv(&int_config,1.0f);
    pio_sm_init(pio0,INT_CLEAR,int_clear_offset,&int_config);

    write_config=live_write_capture_program_get_default_config(write_offset);
    sm_config_set_in_pins(&write_config,PIN_CLK);
    sm_config_set_jmp_pin(&write_config,PIN_RW);
    sm_config_set_in_shift(&write_config,true,false,32);
    sm_config_set_out_shift(&write_config,true,false,32);
    sm_config_set_clkdiv(&write_config,1.0f);
    pio_sm_init(pio1,WRITER,write_offset,&write_config);
    pio_sm_config probe_config=live_clock_probe_program_get_default_config(probe_offset);
    sm_config_set_jmp_pin(&probe_config,PIN_CLK);
    sm_config_set_fifo_join(&probe_config,PIO_FIFO_JOIN_RX);
    sm_config_set_clkdiv(&probe_config,1.0f);
    pio_sm_init(pio2,CLOCK_PROBE,probe_offset,&probe_config);
    pio2->fdebug=0xffffffffu;
    pio0->irq=0xffu;
    pio1->irq=0xffu;
    pio_sm_set_pins_with_mask(pio0,READER,0,DRIVE);
    pio_sm_set_pindirs_with_mask(pio0,READER,INT,DRIVE);
    for (uint pin=PIN_DATA;pin<=PIN_INT;++pin) {
        pio_gpio_init(pio0,pin);
        gpio_disable_pulls(pin);
        // pio_gpio_init resets overrides, so reapply LOW after mux selection.
        gpio_set_outover(pin,GPIO_OVERRIDE_LOW);
        gpio_set_oeover(pin,GPIO_OVERRIDE_NORMAL);
    }
    gpio_set_oeover(PIN_CLK,GPIO_OVERRIDE_LOW);
    gpio_set_oeover(PIN_RW,GPIO_OVERRIDE_LOW);
    pio_set_sm_mask_enabled(pio0,(1u<<GUARD)|(1u<<INT_CLEAR),true);
}

static void prepare_reader(uint32_t image,uint32_t shifts) {
    pio_sm_set_enabled(pio0,READER,false);
    pio_sm_init(pio0,READER,read_offset,&read_config);
    pio_interrupt_clear(pio0,0);
    pio_interrupt_clear(pio0,4);
    pio_sm_exec(pio0,READER,pio_encode_set(pio_y,shifts-1));
    // Remaining high bits shift in as low-driving directions after bit26.
    pio_sm_put(pio0,READER,~image);
}

static bool writer_rw_high(void) { return (sio_hw->gpio_in&RW)!=0; }
static void writer_reset(void) {
    pio_sm_init(pio1,WRITER,write_offset,&write_config);
    pio_interrupt_clear(pio1,5);
    pio_interrupt_clear(pio1,6);
    pio_interrupt_clear(pio1,7);
}
static void writer_enable(void) { pio_sm_set_enabled(pio1,WRITER,true); }
static const writer_arm_ops_t writer_ops={writer_rw_high,writer_reset,writer_enable};

static void release_guard(void) {
    pio_interrupt_clear(pio0,3);
    pio_sm_put(pio0,GUARD,1);
}

static void collect_clock_probe(uint32_t *diagnostics) {
    while(!pio_sm_is_rx_fifo_empty(pio2,CLOCK_PROBE)) {
        uint32_t loops=~pio_sm_get(pio2,CLOCK_PROBE);
        if(!diagnostics[IQ_PIO_PROBE_PULSES]) {
            diagnostics[IQ_PIO_PROBE_FIRST_HIGH]=loops;
            diagnostics[IQ_PIO_PROBE_MIN_HIGH]=loops;
        }
        if(loops<diagnostics[IQ_PIO_PROBE_MIN_HIGH])diagnostics[IQ_PIO_PROBE_MIN_HIGH]=loops;
        if(loops>diagnostics[IQ_PIO_PROBE_MAX_HIGH])diagnostics[IQ_PIO_PROBE_MAX_HIGH]=loops;
        ++diagnostics[IQ_PIO_PROBE_PULSES];
    }
}

void live_pio_run(live_config_t c) {
    memset(&live_result,0,sizeof live_result);
    live_result.sys_hz=clock_get_hz(clk_sys);
    live_result.initial_pins=sio_hw->gpio_in&15u;
    if (c.mode!=MODE_TRANSACTION) {
        live_result.stop=STOP_AMBIGUOUS;
        restore_sio();
        live_result.final_pins=sio_hw->gpio_in&15u;
        return;
    }
    const uint32_t began=now_us(), budget=c.duration_ms*1000u;
    uint32_t previous=began, last_activity=began, before=live_result.initial_pins;
    uint32_t image=0, origin=0, pending_origin=0, reader_shifts=27;
    uint32_t request_due=began+20000u;
    uint32_t int_bad_since=0, data_bad_since=0;
    uint32_t *diagnostics=live_result.pio_diagnostics;
    bool int_bad=false, data_bad=false, started=false, guard_armed=false;
    bool guard_risen=false, guard_rearming=false, reader_active=false;
    bool request_sent=false, completion_pending=false;
    bool repeat_pending=false, repeat_consumed=false;
    init_transport(c.falling);
    // Setup can span an external clock edge. Establish a new idle observation
    // instead of using the pre-setup pin sample to decide capture ownership.
    last_activity=now_us();
    before=sio_hw->gpio_in&15u;
    while (true) {
        uint32_t now=now_us(), elapsed=now-began, pins=sio_hw->gpio_in&15u;
        uint32_t gap=now-previous;
        previous=now;
        if (gap>live_result.max_loop_us) live_result.max_loop_us=gap;
        if (live_abort) { live_result.stop=STOP_ABORT; break; }
        if (elapsed>=budget) {
            if (!started) live_result.stop=STOP_AMBIGUOUS;
            break;
        }
        collect_clock_probe(diagnostics);
        uint32_t changed=pins^before;
        if((changed&CLK)&&(pins&CLK))++diagnostics[IQ_PIO_CPU_CLOCK_RISES];
        if(changed&RW)++diagnostics[(pins&RW)?IQ_PIO_CPU_RW_RISES:IQ_PIO_CPU_RW_FALLS];
        if(changed&INT) {
            unsigned edge=(pins&INT)?IQ_PIO_FIRST_INT_RISE_US:IQ_PIO_FIRST_INT_FALL_US;
            if(!diagnostics[edge])diagnostics[edge]=elapsed;
        }
        if((changed&DATA)&&!(pins&DATA)&&!diagnostics[IQ_PIO_FIRST_DATA_FALL_US])
            diagnostics[IQ_PIO_FIRST_DATA_FALL_US]=elapsed;
        if (changed&(CLK|RW)) last_activity=now;
        if (pins&CLK) last_activity=now;
        if (changed&DATA) ++live_result.data_edges;
        if (changed&INT) ++live_result.int_edges;
        before=pins;

        uint32_t irq0=pio0->irq, irq1=pio1->irq;
        if (irq1&((1u<<5)|(1u<<7))) {
            ++live_result.malformed_writes;
            live_result.stop=(irq1&(1u<<7))?STOP_AMBIGUOUS:STOP_BAD_WRITE;
            record(elapsed,EV_FAULT,(irq1&(1u<<5))?26u:UINT32_MAX,0,LIVE_PIO_ORIGIN_METER);
            break;
        }
        // Completion precedes the following RW fall in the PIO program. Both
        // flags can already be latched by the time the CPU services them.
        if (reader_active && (irq0&((1u<<0)|(1u<<4)))) {
            pio_sm_set_enabled(pio0,READER,false);
            reader_active=false;
            if (irq0&(1u<<4)) {
                if (!record(elapsed,EV_FRAGMENT,UINT32_MAX,image,origin)) break;
                if (origin==LIVE_PIO_ORIGIN_REQUEST) { live_result.stop=STOP_AMBIGUOUS; break; }
                repeat_pending=false;
            } else {
                live_result.clock_rises+=reader_shifts;
                if (!record(elapsed,EV_READ,reader_shifts,image,origin)) break;
                if (origin==LIVE_PIO_ORIGIN_COMPLETION && repeat_pending &&
                    !repeat_consumed && live_result.requests==1) {
                    // One resend only, after the actual completion clock and
                    // a 20 ms backoff, with the same PIO ownership session.
                    repeat_pending=false;
                    repeat_consumed=true;
                    request_sent=false;
                    request_due=now+20000u;
                }
            }
            pio_interrupt_clear(pio0,0);
            pio_interrupt_clear(pio0,4);
        }
        if (irq0&(1u<<1)) {
            pio_interrupt_clear(pio0,1);
            ++live_result.rw_falls;
            last_activity=now;
            guard_armed=false;
            // An image prepared but not yet presented has no bus provenance.
            // Recreate it after this frame, with completion taking precedence.
            pending_origin=0;
            if (reader_active) {
                pio_sm_set_enabled(pio0,READER,false);
                reader_active=false;
                if (!record(elapsed,EV_FRAGMENT,UINT32_MAX,image,origin)) break;
                // A completion may be acknowledged with one clock or followed
                // directly by a write. A request requires its full read image.
                if (origin==LIVE_PIO_ORIGIN_REQUEST) { live_result.stop=STOP_AMBIGUOUS; break; }
                repeat_pending=false;
            }
            pio_interrupt_clear(pio0,0);
            pio_interrupt_clear(pio0,4);
        }
        if (irq0&(1u<<2)) {
            pio_interrupt_clear(pio0,2);
            ++live_result.rw_rises;
            guard_risen=true;
            last_activity=now;
        }
        if (guard_risen && (irq1&(1u<<6))) {
            if (pio_sm_get_rx_fifo_level(pio1,WRITER)!=2) { live_result.stop=STOP_BAD_WRITE; break; }
            uint32_t raw=pio_sm_get(pio1,WRITER), remaining=pio_sm_get(pio1,WRITER);
            if (remaining>25) { live_result.stop=STOP_BAD_WRITE; break; }
            uint32_t clocks=25-remaining, word=clocks?raw>>(32-clocks):0;
            live_result.clock_rises+=clocks;
            if (!record(elapsed,clocks?EV_WRITE:EV_EMPTY_WRITE,clocks,word,LIVE_PIO_ORIGIN_METER)) break;
            if (clocks==25) {
                ++live_result.valid_writes; completion_pending=true;
                repeat_pending=c.repeat_once && !repeat_consumed && live_result.requests==1 && word==0x400027u;
            }
            else if (clocks) { ++live_result.malformed_writes; live_result.stop=STOP_BAD_WRITE; break; }
            guard_risen=false;
            // Once capture has started, ownership loss remains a hard fault.
            if (writer_arm(&writer_ops)!=WRITER_ARMED) { live_result.stop=STOP_AMBIGUOUS; break; }
            release_guard();
            guard_rearming=true;
        }
        if (guard_rearming && (pio0->irq&(1u<<3))) {
            pio_interrupt_clear(pio0,3);
            guard_rearming=false;
            guard_armed=true;
        }
        if (!started && (pins&(CLK|RW))==RW && now-last_activity>=20u) {
            writer_arm_result_t armed=writer_arm(&writer_ops);
            if (armed!=WRITER_ARMED) {
                if (armed==WRITER_BUSY_BEFORE_RESET) ++live_result.startup_retries_before;
                else ++live_result.startup_retries_after;
                // The reader has never run; DATA is still released by the held
                // guard. Retry only initial synchronization, within budget.
                last_activity=now;
                continue;
            }
            release_guard();
            started=true;
            guard_rearming=true;
            last_activity=now;
        }

        if (started && guard_armed && !reader_active && !pending_origin &&
            pio_sm_get_pc(pio0,GUARD)==guard_offset+live_rw_guard_offset_armed &&
            (pins&(CLK|RW))==RW && now-last_activity>=20u &&
            (completion_pending || (!request_sent && (int32_t)(now-request_due)>=0))) {
            pending_origin=completion_pending?LIVE_PIO_ORIGIN_COMPLETION:LIVE_PIO_ORIGIN_REQUEST;
            image=completion_pending?0:5u|(c.payload<<3);
            reader_shifts=(c.repeat_once && completion_pending)?1u:27u;
            prepare_reader(image,reader_shifts);
        }
        // Recheck ownership after preparation. A latched guard fall cancels
        // startup for this window; the image can wait for the next idle gap.
        if (pending_origin && guard_armed && !(pio0->irq&(1u<<1)) &&
            pio_sm_get_pc(pio0,GUARD)==guard_offset+live_rw_guard_offset_armed &&
            (sio_hw->gpio_in&(CLK|RW))==RW && now-last_activity>=20u) {
            origin=pending_origin;
            pending_origin=0;
            if (origin==LIVE_PIO_ORIGIN_REQUEST && c.repeat_once && live_result.requests>=2) {
                live_result.stop=STOP_AMBIGUOUS;
                break;
            }
            reader_active=true;
            if(origin==LIVE_PIO_ORIGIN_REQUEST) {
                diagnostics[IQ_PIO_REQUEST_CLOCK_RISES]=diagnostics[IQ_PIO_CPU_CLOCK_RISES];
                diagnostics[IQ_PIO_REQUEST_RW_FALLS]=diagnostics[IQ_PIO_CPU_RW_FALLS];
                diagnostics[IQ_PIO_REQUEST_RW_RISES]=diagnostics[IQ_PIO_CPU_RW_RISES];
                diagnostics[IQ_PIO_REQUEST_US]=elapsed;
                diagnostics[IQ_PIO_REQUEST_PINS]=sio_hw->gpio_in&15u;
                diagnostics[IQ_PIO_REQUEST_PADOE]=pio0->dbg_padoe&15u;
                pio_sm_set_enabled(pio2,CLOCK_PROBE,true);
            }
            pio_sm_set_enabled(pio0,READER,true);
            if (!record(elapsed,origin==LIVE_PIO_ORIGIN_REQUEST?EV_REQUEST:EV_COMPLETE,reader_shifts,image,origin)) break;
            if (origin==LIVE_PIO_ORIGIN_REQUEST) { request_sent=true; ++live_result.requests; }
            else { completion_pending=false; ++live_result.completions; }
        }

        uint32_t low=pio0->dbg_padoe&DRIVE;
        if ((low&pins)&INT) {
            if (!int_bad) { int_bad=true; int_bad_since=now; }
            else if (now-int_bad_since>=10u) { live_result.stop=STOP_INT_HIGH; break; }
        } else int_bad=false;
        if ((low&pins)&DATA) {
            if (!data_bad) { data_bad=true; data_bad_since=now; }
            else if (now-data_bad_since>=2u) { live_result.stop=STOP_DATA_HIGH; break; }
        } else data_bad=false;
    }
    /* Read registers before cleanup changes the mux or halts machines. Never
     * execute PIO instructions to inspect scratch registers on the live bus. */
    diagnostics[IQ_PIO_LAST_ACTIVITY_US]=last_activity-began;
    diagnostics[IQ_PIO_END_PINS]=sio_hw->gpio_in&15u;
    diagnostics[IQ_PIO_PADOE]=pio0->dbg_padoe&15u;
    diagnostics[IQ_PIO_READER_PC]=pio_sm_get_pc(pio0,READER);
    diagnostics[IQ_PIO_READER_INSTR]=pio0->sm[READER].instr;
    diagnostics[IQ_PIO_READER_TX]=pio_sm_get_tx_fifo_level(pio0,READER);
    diagnostics[IQ_PIO_GUARD_PC]=pio_sm_get_pc(pio0,GUARD);
    diagnostics[IQ_PIO_INT_PC]=pio_sm_get_pc(pio0,INT_CLEAR);
    diagnostics[IQ_PIO_WRITER_PC]=pio_sm_get_pc(pio1,WRITER);
    diagnostics[IQ_PIO_PIO0_CTRL]=pio0->ctrl;diagnostics[IQ_PIO_PIO1_CTRL]=pio1->ctrl;
    diagnostics[IQ_PIO_PIO0_IRQ]=pio0->irq;diagnostics[IQ_PIO_PIO1_IRQ]=pio1->irq;
    diagnostics[IQ_PIO_PIO0_FDEBUG]=pio0->fdebug;diagnostics[IQ_PIO_PIO1_FDEBUG]=pio1->fdebug;
    diagnostics[IQ_PIO_READER_OFFSET]=read_offset;diagnostics[IQ_PIO_GUARD_OFFSET]=guard_offset;
    diagnostics[IQ_PIO_INT_OFFSET]=int_clear_offset;diagnostics[IQ_PIO_WRITER_OFFSET]=write_offset;
    diagnostics[IQ_PIO_READER_EXECCTRL]=pio0->sm[READER].execctrl;
    diagnostics[IQ_PIO_READER_PINCTRL]=pio0->sm[READER].pinctrl;
    diagnostics[IQ_PIO_PIO0_GPIOBASE]=pio_get_gpio_base(pio0);
    diagnostics[IQ_PIO_PIO1_GPIOBASE]=pio_get_gpio_base(pio1);
    diagnostics[IQ_PIO_DATA_CTRL]=io_bank0_hw->io[PIN_DATA].ctrl;
    diagnostics[IQ_PIO_INT_CTRL]=io_bank0_hw->io[PIN_INT].ctrl;
    /* Scratch registers can only be inspected after outputs are forced
     * released and every transport SM has stopped. No injected instruction
     * is allowed to act on the live interface. Reader never uses its RX FIFO. */
    live_pio_release();
    collect_clock_probe(diagnostics);
    diagnostics[IQ_PIO_PROBE_OVERFLOW]=(pio2->fdebug&(1u<<CLOCK_PROBE))?1u:0u;
    pio_sm_exec(pio0,READER,pio_encode_mov(pio_isr,pio_x));
    pio_sm_exec(pio0,READER,pio_encode_push(false,false));
    diagnostics[IQ_PIO_READER_X]=pio_sm_get(pio0,READER);
    pio_sm_exec(pio0,READER,pio_encode_mov(pio_isr,pio_osr));
    pio_sm_exec(pio0,READER,pio_encode_push(false,false));
    diagnostics[IQ_PIO_READER_OSR]=pio_sm_get(pio0,READER);
    restore_sio();
    live_result.elapsed_us=now_us()-began;
    live_result.final_pins=sio_hw->gpio_in&15u;
}
