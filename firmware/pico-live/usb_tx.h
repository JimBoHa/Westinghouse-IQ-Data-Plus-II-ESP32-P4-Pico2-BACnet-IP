#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* One core owns transmission. write() returns the number actually queued;
 * service() keeps USB and the safety watchdog responsive during backpressure. */
typedef struct {
    void *context;
    bool (*connected)(void *);
    uint64_t (*now_us)(void *);
    size_t (*write)(void *, const char *, size_t);
    void (*service)(void *);
} usb_tx_port_t;

bool usb_tx_send(const usb_tx_port_t *port, const char *data, size_t length,
                 uint64_t deadline_us);
