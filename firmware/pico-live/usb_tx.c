#include "usb_tx.h"

bool usb_tx_send(const usb_tx_port_t *port, const char *data, size_t length,
                 uint64_t deadline_us) {
    size_t sent = 0;
    while (sent < length) {
        if (!port->connected(port->context) || port->now_us(port->context) >= deadline_us)
            return false;
        size_t remaining = length - sent;
        /* Bound each USB critical section to one full-speed packet. */
        size_t offered = remaining > 64 ? 64 : remaining;
        size_t accepted = port->write(port->context, data + sent, offered);
        if (accepted > offered) return false;
        sent += accepted;
        port->service(port->context);
    }
    return true;
}
