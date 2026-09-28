/* SPDX-License-Identifier: 0BSD */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "bacnet/bacdest.h"
#include "bacnet/rp.h"
#include "bacnet/timestamp.h"
#include "bacnet/wp.h"

#define BACNET_RESTART_RECIPIENTS_MAX 8U
#define BACNET_RESTART_RECIPIENT_BYTES_MAX 256U
#define BACNET_RESTART_ATTEMPTS_MAX 5U
#define BACNET_RESTART_RETRY_MS 1000U

typedef enum {
    BACNET_RESTART_LOAD_MISSING,
    BACNET_RESTART_LOAD_VALID,
    BACNET_RESTART_LOAD_INVALID,
} bacnet_restart_load_state_t;

typedef struct {
    bool (*persist)(const uint8_t *bytes, size_t length, void *context);
    bool (*resolve)(const BACNET_RECIPIENT *recipient, BACNET_ADDRESS *destination,
        void *context);
    void (*discover)(const BACNET_RECIPIENT *recipient, void *context);
    /* Send this APDU ONLY to destination; return positive for transport
       acceptance, not remote receipt. Caller supplies NPDU/datalink framing. */
    int (*send)(const BACNET_ADDRESS *destination, const uint8_t *apdu,
        size_t length, void *context);
} bacnet_restart_callbacks_t;

typedef struct {
    BACNET_RECIPIENT recipient;
    BACNET_ADDRESS destination;
    bool pending;
    bool canceled;
    bool resolved;
    unsigned attempts;
    unsigned send_attempts;
    int last_result;
    uint64_t first_attempt_ms;
    uint64_t last_attempt_ms;
    uint64_t last_accepted_ms;
} bacnet_restart_destination_stats_t;

typedef struct {
    bool boot_ready;
    bool timestamp_valid;
    unsigned recipients_count;
    unsigned pending_recipients;
    uint32_t notifications_sent;
    uint32_t send_failures;
    uint32_t resolution_failures;
    uint32_t exhausted_recipients;
    uint32_t configuration_errors;
    uint32_t persistence_failures;
    unsigned destination_count;
    bacnet_restart_destination_stats_t destinations[BACNET_RESTART_RECIPIENTS_MAX];
} bacnet_restart_stats_t;

/* Serialized by the BACnet object mutex. Initialize once per actual boot;
   missing storage selects local broadcast, invalid storage suppresses sending.
   This Device exposes Local_Date/Local_Time, so provide its frozen boot
   DateTime timestamp. NULL defers timestamp selection without discarding
   recipients; invalid/non-DateTime timestamps suppress notification. */
void bacnet_restart_init(bacnet_restart_load_state_t load_state,
    const uint8_t *bytes, size_t length, const BACNET_TIMESTAMP *restart_timestamp,
    BACNET_RESTART_REASON reason, const bacnet_restart_callbacks_t *callbacks,
    void *context);
/* Can only change the timestamp before the first ready tick snapshots this
   boot's destinations. Never changes recipient storage or makes another boot. */
bool bacnet_restart_set_timestamp_before_ready(const BACNET_TIMESTAMP *timestamp);
void bacnet_restart_timestamp_get(BACNET_TIMESTAMP *timestamp);
void bacnet_restart_stats_get(bacnet_restart_stats_t *stats);

/* Only Restart_Notification_Recipients is supported here. The application
   read wrapper delegates every other property to its existing Device reader.
   Device-ID recipients may be initially unbound. Direct addresses support
   only network 0 with a six-byte IPv4 MAC or empty local-broadcast MAC;
   other direct network addresses are rejected, not silently redirected. */
int bacnet_restart_read_property(BACNET_READ_PROPERTY_DATA *data);
bool bacnet_restart_write_property(BACNET_WRITE_PROPERTY_DATA *data);
void bacnet_restart_writable_property_list(uint32_t instance,
    const int32_t **properties);

/* No automatic resend on a link cycle or recipient-list edit. Pending
   recipients removed by an edit are canceled; added recipients only receive
   the next actual boot notification. Each original recipient has <=5 tries. */
void bacnet_restart_tick(uint64_t now_ms, bool startup_and_network_ready,
    uint32_t device_instance, BACNET_DEVICE_STATUS system_status);

/* Encodes an APDU without changing caller destinations or touching hardware.
   Exported for independent wire-format tests and explicit NPDU integration. */
int bacnet_restart_encode_notification(uint8_t *apdu, size_t capacity,
    uint32_t device_instance, BACNET_DEVICE_STATUS system_status);
