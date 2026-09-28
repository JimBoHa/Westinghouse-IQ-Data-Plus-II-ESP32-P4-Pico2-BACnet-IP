/* SPDX-License-Identifier: 0BSD */
#include "bacnet_restart.h"

#include <string.h>

#include "bacnet/bacaddr.h"
#include "bacnet/bacdcode.h"

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
    uint64_t next_attempt_ms;
} restart_destination_t;

static BACNET_RECIPIENT s_recipients[BACNET_RESTART_RECIPIENTS_MAX];
static uint8_t s_encoded[BACNET_RESTART_RECIPIENT_BYTES_MAX];
static size_t s_encoded_length;
static restart_destination_t s_destinations[BACNET_RESTART_RECIPIENTS_MAX];
static unsigned s_destination_count;
static BACNET_TIMESTAMP s_timestamp;
static bool s_timestamp_valid;
static BACNET_RESTART_REASON s_reason;
static bacnet_restart_callbacks_t s_callbacks;
static void *s_context;
static bacnet_restart_stats_t s_stats;

static bool recipient_valid(const BACNET_RECIPIENT *recipient)
{
    if (recipient->tag == BACNET_RECIPIENT_TAG_DEVICE) {
        return recipient->type.device.type == OBJECT_DEVICE &&
            recipient->type.device.instance < BACNET_MAX_INSTANCE;
    }
    if (recipient->tag != BACNET_RECIPIENT_TAG_ADDRESS) {
        return false;
    }
    const BACNET_ADDRESS *address = &recipient->type.address;
    if (address->mac_len > MAX_MAC_LEN || address->len > MAX_MAC_LEN) {
        return false;
    }
    /* Direct-address router discovery is not implemented. Reject destinations
       that cannot be delivered, rather than accept them and silently skip or
       broadcast. Device-ID recipients may still resolve through a router. */
    return address->net == 0 &&
        (address->mac_len == 0 || address->mac_len == 6);
}

static bool decode_list(const uint8_t *bytes, size_t length,
    BACNET_RECIPIENT *recipients, unsigned *count, uint8_t *canonical,
    size_t *canonical_length)
{
    if (length > BACNET_RESTART_RECIPIENT_BYTES_MAX || (length && !bytes)) {
        return false;
    }
    *count = 0;
    *canonical_length = 0;
    size_t offset = 0;
    while (offset < length) {
        if (*count == BACNET_RESTART_RECIPIENTS_MAX) {
            return false;
        }
        BACNET_RECIPIENT recipient = {0};
        int consumed = bacnet_recipient_decode(bytes + offset,
            (int)(length - offset), &recipient);
        if (consumed <= 0 || (size_t)consumed > length - offset ||
            !recipient_valid(&recipient)) {
            return false;
        }
        int needed = bacnet_recipient_encode(NULL, &recipient);
        if (needed <= 0 || (size_t)needed >
            BACNET_RESTART_RECIPIENT_BYTES_MAX - *canonical_length) {
            return false;
        }
        bacnet_recipient_encode(canonical + *canonical_length, &recipient);
        *canonical_length += (size_t)needed;
        recipients[(*count)++] = recipient;
        offset += (size_t)consumed;
    }
    return true;
}

static bool recipient_still_listed(const BACNET_RECIPIENT *recipient)
{
    for (unsigned i = 0; i < s_stats.recipients_count; ++i) {
        if (bacnet_recipient_same(recipient, &s_recipients[i])) {
            return true;
        }
    }
    return false;
}

bool bacnet_restart_set_timestamp_before_ready(const BACNET_TIMESTAMP *timestamp)
{
    if (s_stats.boot_ready || !timestamp || timestamp->tag != TIME_STAMP_DATETIME ||
        !datetime_is_valid(&timestamp->value.dateTime.date,
            &timestamp->value.dateTime.time) ||
        datetime_wildcard_present(&timestamp->value.dateTime)) {
        return false;
    }
    bacapp_timestamp_copy(&s_timestamp, timestamp);
    s_timestamp_valid = true;
    return true;
}

void bacnet_restart_init(bacnet_restart_load_state_t load_state,
    const uint8_t *bytes, size_t length, const BACNET_TIMESTAMP *restart_timestamp,
    BACNET_RESTART_REASON reason, const bacnet_restart_callbacks_t *callbacks,
    void *context)
{
    memset(s_recipients, 0, sizeof(s_recipients));
    memset(s_encoded, 0, sizeof(s_encoded));
    memset(s_destinations, 0, sizeof(s_destinations));
    memset(&s_stats, 0, sizeof(s_stats));
    memset(&s_callbacks, 0, sizeof(s_callbacks));
    s_encoded_length = 0;
    s_destination_count = 0;
    s_context = context;
    if (callbacks) {
        s_callbacks = *callbacks;
    }
    memset(&s_timestamp, 0, sizeof(s_timestamp));
    s_timestamp_valid = false;
    if (restart_timestamp &&
        !bacnet_restart_set_timestamp_before_ready(restart_timestamp)) {
        s_stats.configuration_errors++;
    }
    s_reason = (unsigned)reason < BACNET_RESTART_REASON_MAX ?
        reason : RESTART_REASON_UNKNOWN;
    if (load_state == BACNET_RESTART_LOAD_MISSING) {
        s_recipients[0].tag = BACNET_RECIPIENT_TAG_ADDRESS;
        s_stats.recipients_count = 1;
        s_encoded_length = (size_t)bacnet_recipient_encode(s_encoded,
            &s_recipients[0]);
    } else if (load_state != BACNET_RESTART_LOAD_VALID ||
        !decode_list(bytes, length, s_recipients, &s_stats.recipients_count,
            s_encoded, &s_encoded_length)) {
        /* Corruption is not permission to broaden notification recipients. */
        memset(s_recipients, 0, sizeof(s_recipients));
        s_stats.recipients_count = 0;
        s_encoded_length = 0;
        s_stats.configuration_errors++;
    }
}

void bacnet_restart_timestamp_get(BACNET_TIMESTAMP *timestamp)
{
    if (timestamp) {
        bacapp_timestamp_copy(timestamp, &s_timestamp);
    }
}

void bacnet_restart_stats_get(bacnet_restart_stats_t *stats)
{
    if (stats) {
        *stats = s_stats;
        stats->timestamp_valid = s_timestamp_valid;
        stats->destination_count = s_destination_count;
        for (unsigned i = 0; i < s_destination_count; ++i) {
            const restart_destination_t *target = &s_destinations[i];
            stats->destinations[i] = (bacnet_restart_destination_stats_t){
                .recipient = target->recipient, .destination = target->destination,
                .pending = target->pending, .canceled = target->canceled,
                .resolved = target->resolved, .attempts = target->attempts,
                .send_attempts = target->send_attempts, .last_result = target->last_result,
                .first_attempt_ms = target->first_attempt_ms,
                .last_attempt_ms = target->last_attempt_ms,
                .last_accepted_ms = target->last_accepted_ms,
            };
        }
    }
}

void bacnet_restart_writable_property_list(uint32_t instance,
    const int32_t **properties)
{
    static const int32_t writable[] = {PROP_RESTART_NOTIFICATION_RECIPIENTS, -1};
    (void)instance;
    if (properties) {
        *properties = writable;
    }
}

int bacnet_restart_read_property(BACNET_READ_PROPERTY_DATA *data)
{
    if (!data) {
        return BACNET_STATUS_ERROR;
    }
    data->error_class = ERROR_CLASS_PROPERTY;
    if (data->object_property != PROP_RESTART_NOTIFICATION_RECIPIENTS) {
        data->error_code = ERROR_CODE_UNKNOWN_PROPERTY;
        return BACNET_STATUS_ERROR;
    }
    if (data->array_index != BACNET_ARRAY_ALL) {
        data->error_code = ERROR_CODE_PROPERTY_IS_NOT_AN_ARRAY;
        return BACNET_STATUS_ERROR;
    }
    if (data->application_data_len < 0 ||
        (size_t)data->application_data_len < s_encoded_length ||
        (s_encoded_length && !data->application_data)) {
        data->error_code = ERROR_CODE_ABORT_SEGMENTATION_NOT_SUPPORTED;
        return BACNET_STATUS_ABORT;
    }
    if (s_encoded_length) {
        memcpy(data->application_data, s_encoded, s_encoded_length);
    }
    return (int)s_encoded_length;
}

bool bacnet_restart_write_property(BACNET_WRITE_PROPERTY_DATA *data)
{
    if (!data) {
        return false;
    }
    data->error_class = ERROR_CLASS_PROPERTY;
    if (data->object_property != PROP_RESTART_NOTIFICATION_RECIPIENTS) {
        data->error_code = ERROR_CODE_WRITE_ACCESS_DENIED;
        return false;
    }
    if (data->array_index != BACNET_ARRAY_ALL) {
        data->error_code = ERROR_CODE_PROPERTY_IS_NOT_AN_ARRAY;
        return false;
    }
    BACNET_RECIPIENT recipients[BACNET_RESTART_RECIPIENTS_MAX] = {0};
    uint8_t canonical[BACNET_RESTART_RECIPIENT_BYTES_MAX];
    unsigned count = 0;
    size_t length = 0;
    if (data->application_data_len < 0 ||
        !decode_list(data->application_data, (size_t)data->application_data_len,
            recipients, &count, canonical, &length)) {
        data->error_code = ERROR_CODE_VALUE_OUT_OF_RANGE;
        return false;
    }
    if (!s_callbacks.persist ||
        !s_callbacks.persist(canonical, length, s_context)) {
        s_stats.persistence_failures++;
        data->error_class = ERROR_CLASS_RESOURCES;
        data->error_code = ERROR_CODE_OPERATIONAL_PROBLEM;
        return false;
    }
    /* Commit completed before any live state changes or SimpleACK eligibility. */
    memcpy(s_recipients, recipients, sizeof(s_recipients));
    if (length) {
        memcpy(s_encoded, canonical, length);
    }
    s_encoded_length = length;
    s_stats.recipients_count = count;
    for (unsigned i = 0; i < s_destination_count; ++i) {
        if (s_destinations[i].pending &&
            !recipient_still_listed(&s_destinations[i].recipient)) {
            s_destinations[i].pending = false;
            s_destinations[i].canceled = true;
            s_stats.pending_recipients--;
        }
    }
    return true;
}

int bacnet_restart_encode_notification(uint8_t *apdu, size_t capacity,
    uint32_t device_instance, BACNET_DEVICE_STATUS system_status)
{
    if (!apdu || !s_timestamp_valid || device_instance >= BACNET_MAX_INSTANCE) {
        return 0;
    }
    /* All fields are fixed scalars; a DateTime timestamp is at most 12 bytes.
       Encode locally before checking/copying into the caller's bounded buffer.
       Direct timestamp encoding avoids requiring BACAPP_TIMESTAMP in the
       firmware's minimal application-value union. */
    uint8_t encoded[96];
    int length = 0;
    encoded[length++] = PDU_TYPE_UNCONFIRMED_SERVICE_REQUEST;
    encoded[length++] = SERVICE_UNCONFIRMED_COV_NOTIFICATION;
    length += encode_context_unsigned(encoded + length, 0, 0);
    length += encode_context_object_id(encoded + length, 1, OBJECT_DEVICE, device_instance);
    length += encode_context_object_id(encoded + length, 2, OBJECT_DEVICE, device_instance);
    length += encode_context_unsigned(encoded + length, 3, 0);
    length += encode_opening_tag(encoded + length, 4);

    length += encode_context_enumerated(encoded + length, 0, PROP_SYSTEM_STATUS);
    length += encode_opening_tag(encoded + length, 2);
    length += encode_application_enumerated(encoded + length, system_status);
    length += encode_closing_tag(encoded + length, 2);

    length += encode_context_enumerated(encoded + length, 0, PROP_TIME_OF_DEVICE_RESTART);
    length += encode_opening_tag(encoded + length, 2);
    length += bacapp_encode_timestamp(encoded + length, &s_timestamp);
    length += encode_closing_tag(encoded + length, 2);

    length += encode_context_enumerated(encoded + length, 0, PROP_LAST_RESTART_REASON);
    length += encode_opening_tag(encoded + length, 2);
    length += encode_application_enumerated(encoded + length, s_reason);
    length += encode_closing_tag(encoded + length, 2);
    length += encode_closing_tag(encoded + length, 4);
    if ((size_t)length > capacity) {
        return 0;
    }
    memcpy(apdu, encoded, (size_t)length);
    return length;
}

static bool resolved_destination_valid(const BACNET_RECIPIENT *recipient,
    const BACNET_ADDRESS *destination)
{
    if (destination->mac_len > MAX_MAC_LEN || destination->len > MAX_MAC_LEN) {
        return false;
    }
    if (recipient->tag == BACNET_RECIPIENT_TAG_DEVICE) {
        /* An unresolved device must never be silently redirected to broadcast. */
        return destination->mac_len == 6 &&
            destination->net != BACNET_BROADCAST_NETWORK &&
            (destination->net == 0 || destination->len != 0);
    }
    const BACNET_ADDRESS *address = &recipient->type.address;
    if (!bacnet_address_net_same(address, destination)) {
        return false;
    }
    return address->net == 0;
}

void bacnet_restart_tick(uint64_t now_ms, bool startup_and_network_ready,
    uint32_t device_instance, BACNET_DEVICE_STATUS system_status)
{
    if (!startup_and_network_ready || !s_timestamp_valid ||
        device_instance >= BACNET_MAX_INSTANCE) {
        return;
    }
    if (!s_stats.boot_ready) {
        s_stats.boot_ready = true;
        s_destination_count = s_stats.recipients_count;
        s_stats.pending_recipients = s_destination_count;
        for (unsigned i = 0; i < s_destination_count; ++i) {
            s_destinations[i].recipient = s_recipients[i];
            s_destinations[i].pending = true;
            s_destinations[i].next_attempt_ms = now_ms;
        }
    }
    uint8_t apdu[96];
    int length = bacnet_restart_encode_notification(apdu, sizeof(apdu),
        device_instance, system_status);
    if (!length) {
        return;
    }
    for (unsigned i = 0; i < s_destination_count; ++i) {
        restart_destination_t *target = &s_destinations[i];
        if (!target->pending || now_ms < target->next_attempt_ms) {
            continue;
        }
        if (!target->attempts) {
            target->first_attempt_ms = now_ms;
        }
        target->attempts++;
        target->last_attempt_ms = now_ms;
        BACNET_ADDRESS destination = {0};
        bool resolved = s_callbacks.resolve &&
            s_callbacks.resolve(&target->recipient, &destination, s_context) &&
            resolved_destination_valid(&target->recipient, &destination);
        target->resolved = resolved;
        target->destination = resolved ? destination : (BACNET_ADDRESS){0};
        target->last_result = 0;
        if (!resolved) {
            s_stats.resolution_failures++;
            if (s_callbacks.discover) {
                s_callbacks.discover(&target->recipient, s_context);
            }
        } else {
            target->send_attempts++;
            target->last_result = s_callbacks.send ?
                s_callbacks.send(&destination, apdu, (size_t)length, s_context) : -1;
            if (target->last_result > 0) {
                target->pending = false;
                target->last_accepted_ms = now_ms;
                s_stats.pending_recipients--;
                s_stats.notifications_sent++;
            } else {
                s_stats.send_failures++;
            }
        }
        if (target->pending) {
            if (target->attempts == BACNET_RESTART_ATTEMPTS_MAX) {
                target->pending = false;
                s_stats.pending_recipients--;
                s_stats.exhausted_recipients++;
            } else {
                target->next_attempt_ms = now_ms + BACNET_RESTART_RETRY_MS;
            }
        }
    }
}
