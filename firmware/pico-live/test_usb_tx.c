/* Native test of the actual transmit loop: no USB hardware or Pico SDK. */
#include "usb_tx.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    uint64_t now, blocked_until, disconnect_at;
    size_t calls, used;
    char received[4096];
    bool permanently_blocked;
} fake_t;
static bool connected(void *p) {
    fake_t *f = p;
    return !f->disconnect_at || f->now < f->disconnect_at;
}
static uint64_t now_us(void *p) { return ((fake_t *)p)->now; }
static void service(void *p) { ((fake_t *)p)->now += 1000; }
static size_t write_bytes(void *p, const char *data, size_t n) {
    fake_t *f = p;
    ++f->calls;
    if (f->permanently_blocked || f->now < f->blocked_until) return 0;
    /* Short writes and several pauses longer than the old 100 ms timeout. */
    if (n > 7) n = 7;
    assert(f->used + n <= sizeof f->received);
    memcpy(f->received + f->used, data, n);
    f->used += n;
    if (f->used % 49 == 0) f->blocked_until = f->now + 250000;
    return n;
}
int main(void) {
    const char message[] = "{\"type\":\"event\",\"word\":12345}\n{\"type\":\"result\",\"released\":true}\n";
    fake_t f = {.blocked_until = 300000};
    usb_tx_port_t port = {&f, connected, now_us, write_bytes, service};
    assert(usb_tx_send(&port, message, sizeof(message)-1, 4000000));
    assert(f.now > 500000 && f.used == sizeof(message)-1);
    assert(memcmp(message, f.received, f.used) == 0);
    /* A single absolute deadline applies to an entire multi-record report. */
    f = (fake_t){.now = 3999000, .permanently_blocked = true};
    assert(!usb_tx_send(&port, message, sizeof(message)-1, 4000000));
    assert(f.now == 4000000 && f.calls == 1);
    f = (fake_t){.blocked_until = 300000, .disconnect_at = 50000};
    assert(!usb_tx_send(&port, message, sizeof(message)-1, 4000000));
    assert(f.now == 50000 && f.used == 0);
    puts("USB transmit tests passed: short writes, backpressure, deadline, disconnect");
    return 0;
}
