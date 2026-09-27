#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdarg.h>
#include "pico/stdlib.h"
#include "pico/stdio_usb.h"
#include "pico/unique_id.h"
#include "pico/bootrom.h"
#include "hardware/watchdog.h"
#include "hardware/structs/sio.h"
#include "hardware/structs/io_bank0.h"
#include "hardware/sync.h"
#include "tusb.h"
#include "engine.h"
#include "usb_tx.h"

static bool report_pending, boot_watchdog;
static uint32_t deadline_ms, report_mode;
static uint64_t output_deadline;

static void safe_reboot(void) {
    live_abort=true;
    gpio_set_oeover(PIN_DATA,GPIO_OVERRIDE_LOW);
    gpio_set_oeover(PIN_INT,GPIO_OVERRIDE_LOW);
    live_release();
    watchdog_reboot(0,0,1);
    while (true) tight_loop_contents();
}
static void safety_tick(void) {
    watchdog_update();
    if (live_active && (int32_t)(to_ms_since_boot(get_absolute_time())-deadline_ms)>0)
        safe_reboot();
}
static bool tx_connected(void *unused) { (void)unused; return stdio_usb_connected(); }
static uint64_t tx_now(void *unused) { (void)unused; return time_us_64(); }
static void tx_service(void *unused) {
    (void)unused;
    safety_tick();
    sleep_us(100); /* SDK's USB worker IRQ services transfers between packets. */
}
static size_t tx_write(void *unused, const char *data, size_t length) {
    (void)unused;
    /* USB runs only on core0. Serialize these short FIFO operations with the
     * SDK's USB worker IRQ; do not mask interrupts while waiting for space. */
    uint32_t interrupts=save_and_disable_interrupts();
    uint32_t accepted=tud_cdc_write(data,(uint32_t)length);
    tud_cdc_write_flush();
    restore_interrupts(interrupts);
    return accepted;
}
static const usb_tx_port_t tx_port={NULL,tx_connected,tx_now,tx_write,tx_service};
static void output_begin(void) { output_deadline=time_us_64()+4000000u; }
static void usb_printf(const char *format, ...) __attribute__((format(printf,1,2)));
static void usb_printf(const char *format, ...) {
    char record[1024];
    va_list args;
    va_start(args,format);
    int length=vsnprintf(record,sizeof record,format,args);
    va_end(args);
    if (length<0 || (size_t)length>=sizeof record ||
        !usb_tx_send(&tx_port,record,(size_t)length,output_deadline)) {
        /* A partial response must never be followed by a success record.
         * Re-enumeration clears the broken stream; all meter outputs release. */
        safe_reboot();
    }
}
#define printf usb_printf
#define puts(text) usb_printf("%s\n",text)
static void info(void) {
    char id[2*PICO_UNIQUE_BOARD_ID_SIZE_BYTES+1];
    pico_get_unique_board_id_string(id,sizeof id);
    printf("{\"type\":\"info\",\"firmware\":\"iqdata-pico-live\",\"version\":\"0.4.6\",\"build_board\":\"%s\",\"id\":\"%s\",\"live_enabled\":true,\"initial_status_hold_clocks\":0,\"framing\":\"classic_27_positions\",\"synthetic_host\":false,\"timing\":\"pio_transaction_core1_observe_int\",\"watchdog_reboot\":%s,\"pins\":{\"CLK\":0,\"RW\":1,\"DATA\":2,\"INT\":3},\"drive\":\"low_or_release\"}\n",LIVE_BOARD,id,boot_watchdog?"true":"false");
}
static void status(void) {
    printf("{\"type\":\"status\",\"active\":%s,\"pins\":%lu,\"sio_output_enables\":%lu,\"clk_ctrl\":%lu,\"rw_ctrl\":%lu,\"data_ctrl\":%lu,\"int_ctrl\":%lu}\n",live_active?"true":"false",(unsigned long)(sio_hw->gpio_in&15u),(unsigned long)(sio_hw->gpio_oe&15u),(unsigned long)io_bank0_hw->io[0].ctrl,(unsigned long)io_bank0_hw->io[1].ctrl,(unsigned long)io_bank0_hw->io[2].ctrl,(unsigned long)io_bank0_hw->io[3].ctrl);
}
static const char *event_name(uint32_t kind) {
    switch(kind) {
        case EV_READ:return "read_poll";
        case EV_WRITE:return "meter_write_candidate";
        case EV_EMPTY_WRITE:return "empty_write";
        case EV_REQUEST:return "request_presented";
        case EV_COMPLETE:return "completion_presented";
        case EV_FRAGMENT:return "clock_only_fragment";
        default:return "fault";
    }
}
static void report(void) {
    output_begin();
    for (uint32_t i=0;i<live_result.events;++i) {
        live_event_t *e=&live_events[i];
        printf("{\"type\":\"event\",\"event\":\"%s\",\"us\":%lu,\"clocks\":%lu,\"word\":%lu,\"origin_code\":%lu,\"data_released_during_write\":%s}\n",event_name(e->kind),(unsigned long)e->us,(unsigned long)e->clocks,(unsigned long)e->word,(unsigned long)e->origin,e->kind==EV_WRITE||e->kind==EV_EMPTY_WRITE?"true":"null");
        watchdog_update();
    }
    live_result_t *r=&live_result;
    if (report_mode==MODE_TRANSACTION) {
        printf("{\"type\":\"timing\",\"engine\":\"pio\",\"clock_hz\":%lu,\"bracketed_sampling\":false,\"event_timestamps\":\"cpu_service_microseconds\",\"read_words\":\"presented_images_not_wire_samples\",\"clock_count_scope\":\"completed_program_shifts_and_captured_writes\"}\n",(unsigned long)r->sys_hz);
    } else {
        printf("{\"type\":\"timing\",\"engine\":\"cpu\",\"clock_hz\":%lu,\"maximum_sample_gap_cycles\":%lu,\"active_limit_cycles\":%lu,\"bracketed_sampling\":true}\n",(unsigned long)r->sys_hz,(unsigned long)r->max_sample_gap_cycles,(unsigned long)r->sample_gap_limit_cycles);
    }
    printf("{\"type\":\"result\",\"stop_code\":%lu,\"elapsed_us\":%lu,\"clock_rises\":%lu,\"rw_falls\":%lu,\"rw_rises\":%lu,\"data_edges\":%lu,\"int_edges\":%lu,\"max_loop_us\":%lu,\"late_loops\":%lu,\"events\":%lu,\"valid_write_lengths\":%lu,\"malformed_writes\":%lu,\"requests\":%lu,\"completions\":%lu,\"initial_pins\":%lu,\"final_pins\":%lu,\"released\":true,\"telemetry_validated\":false}\n",(unsigned long)r->stop,(unsigned long)r->elapsed_us,(unsigned long)r->clock_rises,(unsigned long)r->rw_falls,(unsigned long)r->rw_rises,(unsigned long)r->data_edges,(unsigned long)r->int_edges,(unsigned long)r->max_loop_us,(unsigned long)r->late_loops,(unsigned long)r->events,(unsigned long)r->valid_writes,(unsigned long)r->malformed_writes,(unsigned long)r->requests,(unsigned long)r->completions,(unsigned long)r->initial_pins,(unsigned long)r->final_pins);
}
static bool decimal(const char *s,uint32_t *out) {
    if (!s || !*s) return false;
    uint32_t n=0;
    for (;*s;++s) { if (*s<'0'||*s>'9'||n>100000u) return false; n=n*10u+(uint32_t)(*s-'0'); }
    *out=n; return true;
}
static bool request_payload(const char *kind,uint32_t address,uint32_t *payload) {
    // Experimental encodings of address-zero Fast Status only. Header and
    // electrical timing stay unchanged; arbitrary payloads are not accepted.
    if (!strcmp(kind,"fast_status_msb24")) { *payload=0xc00000u; return address==0; }
    if (!strcmp(kind,"fast_status_msb8")) { *payload=0x0000c0u; return address==0; }
    if (!strcmp(kind,"fast_status_byte_swap")) { *payload=0x030000u; return address==0; }
    uint32_t sub=0,comm=0;
    if (!strcmp(kind,"line_voltages") || !strcmp(kind,"line_voltages_repeat")) sub=6;
    else if (!strcmp(kind,"currents") || !strcmp(kind,"currents_repeat")) sub=5;
    else if (!strcmp(kind,"all_standard_repeat")) sub=3;
    else if (!strcmp(kind,"power1_repeat")) sub=8;
    else if (!strcmp(kind,"power2_repeat")) sub=9;
    else if (!strcmp(kind,"energy_repeat")) sub=10;
    // Chapter108 product-specific read buffers. No 3/D action/reset payloads.
    else if (!strcmp(kind,"flags_repeat")) { comm=12; sub=8; }
    else if (!strcmp(kind,"settings_repeat")) { comm=12; sub=9; }
    else if (!strcmp(kind,"trip_repeat")) { comm=12; sub=10; }
    else if (!strcmp(kind,"legacy_status")) sub=1;
    else if (!strcmp(kind,"short_buffer")) comm=1;
    else if (strcmp(kind,"fast_status") && strcmp(kind,"fast_status_repeat")) return false;
    *payload=3u|(comm<<4)|(address<<8)|(sub<<20);
    return true;
}
static void command(char *line) {
    output_begin();
    char *part[6]; unsigned count=0; char *save;
    for (char *p=strtok_r(line," ",&save);p;p=strtok_r(NULL," ",&save)) {
        if (count==6) { puts("{\"type\":\"error\",\"error\":\"too_many_arguments\"}"); return; }
        part[count++]=p;
    }
    if (!count) return;
    if (count==1 && !strcmp(part[0],"info")) { info(); return; }
    if (count==1 && !strcmp(part[0],"status")) { status(); return; }
    if (count==1 && !strcmp(part[0],"abort")) {
        live_abort=true;
        if (!live_active) { live_release(); puts("{\"type\":\"abort\",\"released\":true}"); }
        return;
    }
    if (live_active || report_pending) { puts("{\"type\":\"error\",\"error\":\"busy\"}"); return; }
    if (count==1 && !strcmp(part[0],"bootloader")) { live_release(); reset_usb_boot(0,0); }
    if (count==1 && !strcmp(part[0],"reboot")) { live_release(); watchdog_reboot(0,0,1); return; }
    live_config_t c={0}; uint32_t address=0;
    if (count==2 && !strcmp(part[0],"observe") && decimal(part[1],&c.duration_ms) && c.duration_ms>=1 && c.duration_ms<=10000) c.mode=MODE_OBSERVE;
    else if (count==2 && !strcmp(part[0],"trial_int") && decimal(part[1],&c.duration_ms) && c.duration_ms>=1 && c.duration_ms<=1000) c.mode=MODE_INT;
    else if (count==5 && !strcmp(part[0],"transact") && decimal(part[2],&address) && address<=4095 && decimal(part[4],&c.duration_ms) && c.duration_ms>=1 && c.duration_ms<=5000 && (!strcmp(part[3],"rising")||!strcmp(part[3],"falling"))) {
        if (request_payload(part[1],address,&c.payload)) {
            c.mode=MODE_TRANSACTION; c.falling=!strcmp(part[3],"falling");
            c.repeat_once=!strcmp(part[1],"fast_status_repeat") || !strcmp(part[1],"line_voltages_repeat") || !strcmp(part[1],"currents_repeat") ||
                          !strcmp(part[1],"all_standard_repeat") || !strcmp(part[1],"power1_repeat") || !strcmp(part[1],"power2_repeat") || !strcmp(part[1],"energy_repeat") ||
                          !strcmp(part[1],"flags_repeat") || !strcmp(part[1],"settings_repeat") || !strcmp(part[1],"trip_repeat");
        }
    }
    if (!c.mode) { puts("{\"type\":\"error\",\"error\":\"invalid_command\"}"); return; }
    // Print before starting the trial: USB formatting never delays its edges.
    printf("{\"type\":\"started\",\"command\":\"%s\",\"request_kind\":\"%s\",\"duration_ms\":%lu,\"payload\":%lu,\"shift_edge\":\"%s\"}\n",part[0],c.mode==MODE_TRANSACTION?part[1]:"none",(unsigned long)c.duration_ms,(unsigned long)c.payload,c.falling?"falling":"rising");
    deadline_ms=to_ms_since_boot(get_absolute_time())+c.duration_ms+100;
    report_mode=c.mode;
    report_pending=live_start(c);
}
int main(void) {
    boot_watchdog=watchdog_caused_reboot();
    live_init(); stdio_init_all(); watchdog_enable(2000,true);
    char line[160]; size_t used=0; bool invalid=false;
    while (true) {
        safety_tick();
        if (!stdio_usb_connected()) { live_abort=true; used=0; invalid=false; }
        if (report_pending && !live_active) { report_pending=false; report(); }
        int ch=getchar_timeout_us(1000);
        if (ch<0 || ch=='\r') continue;
        if (ch=='\n') {
            if (invalid) { output_begin(); puts("{\"type\":\"error\",\"error\":\"invalid_line\"}"); }
            else if (used) { line[used]=0; command(line); }
            used=0; invalid=false;
        } else if (ch<32||ch>126||used+1>=sizeof line) invalid=true;
        else if (!invalid) line[used++]=(char)ch;
    }
}
