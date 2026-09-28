/* SPDX-License-Identifier: 0BSD */
/* Real pinned codecs; callbacks replace storage, address discovery and sending. */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "bacnet/bacaddr.h"
#include "bacnet/bacdcode.h"
#include "bacnet/cov.h"
#include "bacnet_restart.h"

static unsigned sends, discoveries, stores, failures_left;
static bool store_ok, device_resolved, device_remote, wrong_broadcast;
static uint8_t saved[256];
static size_t saved_length;
static BACNET_ADDRESS last_destination;
static BACNET_TIMESTAMP expected_timestamp;
static uint8_t before_store[256];
static size_t before_store_length;
static bool check_atomic_store;

static int read_list(uint8_t *bytes, size_t capacity)
{
    BACNET_READ_PROPERTY_DATA read = {.object_type = OBJECT_DEVICE,
        .object_property = PROP_RESTART_NOTIFICATION_RECIPIENTS,
        .array_index = BACNET_ARRAY_ALL, .application_data = bytes,
        .application_data_len = (int)capacity};
    return bacnet_restart_read_property(&read);
}

static bool persist(const uint8_t *bytes, size_t length, void *context)
{
    assert(context == &sends);
    stores++;
    if (check_atomic_store) {
        uint8_t current[256];
        assert(read_list(current, sizeof(current)) == (int)before_store_length);
        assert(memcmp(current, before_store, before_store_length) == 0);
    }
    if (!store_ok) return false;
    assert(length <= sizeof(saved));
    if (length) memcpy(saved, bytes, length);
    saved_length = length;
    return true;
}

static bool resolve(const BACNET_RECIPIENT *recipient, BACNET_ADDRESS *destination,
    void *context)
{
    assert(context == &sends);
    memset(destination, 0, sizeof(*destination));
    if (wrong_broadcast) return true;
    if (recipient->tag == BACNET_RECIPIENT_TAG_DEVICE) {
        if (!device_resolved) return false;
        destination->mac_len = 6;
        memcpy(destination->mac, (uint8_t[]){192, 0, 2, 10, 0xba, 0xc0}, 6);
        if (device_remote) {
            destination->mac[3] = 254;
            destination->net = 200;
            destination->len = 1;
            destination->adr[0] = 3;
        }
    } else {
        *destination = recipient->type.address;
    }
    return true;
}

static void discover(const BACNET_RECIPIENT *recipient, void *context)
{
    assert(context == &sends);
    assert(recipient);
    discoveries++;
}

static void verify_wire(const uint8_t *apdu, size_t length)
{
    assert(length > 2 && apdu[0] == PDU_TYPE_UNCONFIRMED_SERVICE_REQUEST);
    assert(apdu[1] == SERVICE_UNCONFIRMED_COV_NOTIFICATION);
    BACNET_COV_DATA cov = {0};
    BACNET_PROPERTY_VALUE values[3] = {0};
    bacapp_property_value_list_init(values, 3);
    cov.listOfValues = values;
    assert(cov_notify_decode_service_request(apdu + 2, (unsigned)length - 2,
        &cov) == (int)length - 2);
    assert(cov.subscriberProcessIdentifier == 0);
    assert(cov.timeRemaining == 0 && cov.initiatingDeviceIdentifier == 1234);
    assert(cov.monitoredObjectIdentifier.type == OBJECT_DEVICE &&
        cov.monitoredObjectIdentifier.instance == 1234);
    assert(values[0].propertyIdentifier == PROP_SYSTEM_STATUS);
    assert(values[0].value.tag == BACNET_APPLICATION_TAG_ENUMERATED);
    assert(values[0].value.type.Enumerated == STATUS_OPERATIONAL);
    assert(values[1].propertyIdentifier == PROP_TIME_OF_DEVICE_RESTART);
    assert(values[1].value.tag == BACNET_APPLICATION_TAG_TIMESTAMP);
    assert(values[1].value.type.Time_Stamp.tag == TIME_STAMP_DATETIME);
    assert(bacapp_timestamp_same(&values[1].value.type.Time_Stamp, &expected_timestamp));
    assert(values[2].propertyIdentifier == PROP_LAST_RESTART_REASON);
    assert(values[2].value.tag == BACNET_APPLICATION_TAG_ENUMERATED);
    assert(values[2].value.type.Enumerated == RESTART_REASON_WARMSTART);
    for (unsigned i = 0; i < 3; ++i) {
        assert(values[i].propertyArrayIndex == BACNET_ARRAY_ALL);
        assert(values[i].priority == BACNET_NO_PRIORITY);
    }
    assert(values[2].next == NULL);
}

static int send_packet(const BACNET_ADDRESS *destination, const uint8_t *apdu,
    size_t length, void *context)
{
    assert(context == &sends);
    verify_wire(apdu, length);
    sends++;
    last_destination = *destination;
    if (failures_left) {
        failures_left--;
        return -1;
    }
    return (int)length;
}

static const bacnet_restart_callbacks_t callbacks = {
    .persist = persist, .resolve = resolve, .discover = discover, .send = send_packet,
};

static void init(bacnet_restart_load_state_t state, const uint8_t *bytes, size_t length)
{
    sends = discoveries = stores = failures_left = 0;
    store_ok = true;
    device_resolved = device_remote = wrong_broadcast = check_atomic_store = false;
    saved_length = 0;
    expected_timestamp = (BACNET_TIMESTAMP){.tag = TIME_STAMP_DATETIME,
        .value.dateTime = {.date = {.year = 2026, .month = 9, .day = 10, .wday = 4},
            .time = {.hour = 12, .min = 34, .sec = 56, .hundredths = 0}}};
    bacnet_restart_init(state, bytes, length, &expected_timestamp,
        RESTART_REASON_WARMSTART, &callbacks, &sends);
}

static bool write_list(const uint8_t *bytes, size_t length)
{
    BACNET_WRITE_PROPERTY_DATA write = {.object_type = OBJECT_DEVICE,
        .object_property = PROP_RESTART_NOTIFICATION_RECIPIENTS,
        .array_index = BACNET_ARRAY_ALL, .application_data_len = (int)length};
    assert(length <= sizeof(write.application_data));
    if (length) memcpy(write.application_data, bytes, length);
    return bacnet_restart_write_property(&write);
}

static size_t encode_device(uint8_t *bytes, uint32_t instance)
{
    BACNET_RECIPIENT recipient = {.tag = BACNET_RECIPIENT_TAG_DEVICE,
        .type.device = {.type = OBJECT_DEVICE, .instance = instance}};
    return (size_t)bacnet_recipient_encode(bytes, &recipient);
}

static size_t encode_address(uint8_t *bytes, uint16_t network, unsigned length)
{
    BACNET_RECIPIENT recipient = {.tag = BACNET_RECIPIENT_TAG_ADDRESS};
    BACNET_ADDRESS *address = &recipient.type.address;
    address->net = network;
    if (network) {
        address->len = length;
        memcpy(address->adr, (uint8_t[]){3, 4, 5, 6, 7, 8, 9}, length);
    } else {
        address->mac_len = length;
        memcpy(address->mac, (uint8_t[]){192, 0, 2, 20, 0xba, 0xc0, 0}, length);
    }
    return (size_t)bacnet_recipient_encode(bytes, &recipient);
}

static void tick(uint64_t now_ms, bool ready)
{
    bacnet_restart_tick(now_ms, ready, 1234, STATUS_OPERATIONAL);
}

static bacnet_restart_stats_t stats(void)
{
    bacnet_restart_stats_t result;
    bacnet_restart_stats_get(&result);
    return result;
}

static void test_default_and_wire(void)
{
    init(BACNET_RESTART_LOAD_MISSING, NULL, 0);
    uint8_t encoded[256];
    int length = read_list(encoded, sizeof(encoded));
    BACNET_RECIPIENT recipient = {0};
    assert(length > 0 && bacnet_recipient_decode(encoded, length, &recipient) == length);
    assert(recipient.tag == BACNET_RECIPIENT_TAG_ADDRESS);
    assert(recipient.type.address.net == 0 && recipient.type.address.mac_len == 0);
    uint8_t packet[96];
    int packet_length = bacnet_restart_encode_notification(packet, sizeof(packet),
        1234, STATUS_OPERATIONAL);
    verify_wire(packet, packet_length);
    for (unsigned n = 0; n < (unsigned)packet_length; ++n) {
        memset(packet, 0xa5, sizeof(packet));
        assert(bacnet_restart_encode_notification(packet, n, 1234, STATUS_OPERATIONAL) == 0);
        for (unsigned i = 0; i < sizeof(packet); ++i) assert(packet[i] == 0xa5);
    }
    tick(100, false);
    assert(!stats().boot_ready && sends == 0);
    tick(500, true);
    assert(sends == 1 && last_destination.net == 0 && last_destination.mac_len == 0);
    tick(1000, false);
    tick(20000, true);
    assert(sends == 1 && stats().pending_recipients == 0);
    puts("PASS default local broadcast, exact three-property wire format, readiness/link-cycle");
}

static void test_storage_and_atomic_write(void)
{
    init(BACNET_RESTART_LOAD_INVALID, NULL, 0);
    assert(stats().configuration_errors == 1 && stats().recipients_count == 0);
    tick(0, true);
    assert(sends == 0);
    init(BACNET_RESTART_LOAD_VALID, NULL, 0);
    assert(stats().configuration_errors == 0);
    tick(0, true);
    assert(sends == 0);
    init(BACNET_RESTART_LOAD_MISSING, NULL, 0);
    before_store_length = read_list(before_store, sizeof(before_store));
    check_atomic_store = true;
    uint8_t encoded[256];
    size_t length = encode_device(encoded, 2000);
    store_ok = false;
    assert(!write_list(encoded, length));
    assert(stats().persistence_failures == 1);
    uint8_t current[256];
    assert(read_list(current, sizeof(current)) == (int)before_store_length);
    assert(memcmp(current, before_store, before_store_length) == 0);
    store_ok = true;
    assert(write_list(encoded, length));
    assert(saved_length == length && memcmp(saved, encoded, length) == 0);
    check_atomic_store = false;
    init(BACNET_RESTART_LOAD_VALID, encoded, length);
    assert(read_list(current, sizeof(current)) == (int)length);
    assert(memcmp(current, encoded, length) == 0);
    assert(write_list(NULL, 0));
    assert(saved_length == 0 && stats().recipients_count == 0);
    puts("PASS missing/corrupt/empty storage and persist-before-publish atomicity");
}

static void test_validation_and_bounds(void)
{
    init(BACNET_RESTART_LOAD_MISSING, NULL, 0);
    uint8_t encoded[512];
    size_t length = 0;
    for (unsigned i = 0; i < 8; ++i) length += encode_device(encoded + length, i);
    assert(write_list(encoded, length));
    assert(stats().recipients_count == 8);
    length += encode_device(encoded + length, 8);
    assert(!write_list(encoded, length));
    assert(stats().recipients_count == 8);
    length = encode_device(encoded, BACNET_MAX_INSTANCE);
    assert(!write_list(encoded, length));
    length = encode_address(encoded, 0, 1);
    assert(!write_list(encoded, length));
    length = encode_address(encoded, BACNET_BROADCAST_NETWORK, 1);
    assert(!write_list(encoded, length));
    length = encode_context_object_id(encoded, 0, OBJECT_BINARY_OUTPUT, 1);
    assert(!write_list(encoded, length));
    memset(encoded, 0, sizeof(encoded));
    assert(!write_list(encoded, 257));
    BACNET_WRITE_PROPERTY_DATA write = {.object_property = PROP_RESTART_NOTIFICATION_RECIPIENTS,
        .array_index = 0};
    assert(!bacnet_restart_write_property(&write));
    assert(write.error_code == ERROR_CODE_PROPERTY_IS_NOT_AN_ARRAY);
    write.array_index = BACNET_ARRAY_ALL;
    write.application_data_len = -1;
    assert(!bacnet_restart_write_property(&write));
    write.object_property = PROP_LAST_RESTART_REASON;
    assert(!bacnet_restart_write_property(&write));
    assert(write.error_code == ERROR_CODE_WRITE_ACCESS_DENIED);
    BACNET_READ_PROPERTY_DATA read = {.object_property = PROP_RESTART_NOTIFICATION_RECIPIENTS,
        .array_index = 0};
    assert(bacnet_restart_read_property(&read) < 0);
    read.array_index = BACNET_ARRAY_ALL;
    read.application_data = encoded;
    read.application_data_len = 1;
    assert(bacnet_restart_read_property(&read) == BACNET_STATUS_ABORT);
    const int32_t *properties = NULL;
    bacnet_restart_writable_property_list(1234, &properties);
    assert(properties[0] == PROP_RESTART_NOTIFICATION_RECIPIENTS && properties[1] == -1);
    puts("PASS list capacity, object/type/address validation and RP/WP bounds");
}

static void test_scheduler_retry_and_routing(void)
{
    init(BACNET_RESTART_LOAD_MISSING, NULL, 0);
    failures_left = 4;
    for (unsigned i = 0; i < 5; ++i) {
        tick(i * 1000, true);
        assert(sends == i + 1);
        tick(i * 1000 + 999, true);
        assert(sends == i + 1);
    }
    assert(stats().notifications_sent == 1 && stats().send_failures == 4);
    bacnet_restart_stats_t diagnostic = stats();
    assert(diagnostic.timestamp_valid && diagnostic.destination_count == 1);
    assert(diagnostic.destinations[0].attempts == 5 &&
        diagnostic.destinations[0].send_attempts == 5);
    assert(diagnostic.destinations[0].resolved && !diagnostic.destinations[0].pending);
    assert(diagnostic.destinations[0].destination.net == 0 &&
        diagnostic.destinations[0].destination.mac_len == 0);
    assert(diagnostic.destinations[0].first_attempt_ms == 0 &&
        diagnostic.destinations[0].last_attempt_ms == 4000 &&
        diagnostic.destinations[0].last_accepted_ms == 4000 &&
        diagnostic.destinations[0].last_result > 0);
    tick(100000, true);
    assert(sends == 5);
    init(BACNET_RESTART_LOAD_MISSING, NULL, 0);
    failures_left = 9;
    for (unsigned i = 0; i < 10; ++i) tick(i * 1000, true);
    assert(sends == 5 && stats().exhausted_recipients == 1);
    uint8_t encoded[256];
    size_t length = encode_device(encoded, 2000);
    init(BACNET_RESTART_LOAD_VALID, encoded, length);
    tick(0, true);
    tick(999, true);
    assert(discoveries == 1 && sends == 0);
    diagnostic = stats();
    assert(!diagnostic.destinations[0].resolved &&
        diagnostic.destinations[0].send_attempts == 0);
    device_resolved = true;
    tick(1000, true);
    assert(sends == 1 && last_destination.mac[3] == 10);
    diagnostic = stats();
    assert(diagnostic.destinations[0].recipient.type.device.instance == 2000 &&
        diagnostic.destinations[0].destination.mac[3] == 10 &&
        diagnostic.destinations[0].attempts == 2 &&
        diagnostic.destinations[0].send_attempts == 1 &&
        diagnostic.destinations[0].last_accepted_ms == 1000);
    init(BACNET_RESTART_LOAD_VALID, encoded, length);
    wrong_broadcast = true;
    for (unsigned i = 0; i < 8; ++i) tick(i * 1000, true);
    assert(sends == 0 && discoveries == 5 && stats().exhausted_recipients == 1);
    length = encode_device(encoded, 2000);
    init(BACNET_RESTART_LOAD_VALID, encoded, length);
    tick(0, true);
    assert(sends == 0);
    device_resolved = device_remote = true;
    tick(1000, true);
    assert(sends == 1 && last_destination.net == 200 && last_destination.adr[0] == 3);
    assert(last_destination.mac_len == 6 && last_destination.mac[3] == 254);
    length = encode_address(encoded, 0, 6);
    init(BACNET_RESTART_LOAD_VALID, encoded, length);
    tick(0, true);
    assert(sends == 1 && last_destination.net == 0 && last_destination.mac[3] == 20);
    init(BACNET_RESTART_LOAD_VALID, encoded, length);
    wrong_broadcast = true;
    tick(0, true);
    assert(sends == 0);
    length = encode_address(encoded, BACNET_BROADCAST_NETWORK, 0);
    init(BACNET_RESTART_LOAD_VALID, encoded, length);
    tick(0, true);
    assert(sends == 0 && stats().configuration_errors == 1);
    assert(!write_list(encoded, length));
    length = encode_address(encoded, 200, 1);
    assert(!write_list(encoded, length));
    BACNET_WRITE_PROPERTY_DATA write = {.object_type = OBJECT_DEVICE,
        .object_property = PROP_RESTART_NOTIFICATION_RECIPIENTS,
        .array_index = BACNET_ARRAY_ALL, .application_data_len = (int)length};
    memcpy(write.application_data, encoded, length);
    assert(!bacnet_restart_write_property(&write));
    assert(write.error_code == ERROR_CODE_VALUE_OUT_OF_RANGE);
    puts("PASS bounded retry/exhaustion, routed Device-ID binding, direct routed-address rejection, no broadcast fallback");
}

static void test_recipient_edits_and_timestamp(void)
{
    uint8_t encoded[256];
    size_t length = encode_device(encoded, 2000);
    init(BACNET_RESTART_LOAD_MISSING, NULL, 0);
    assert(write_list(encoded, length));
    tick(0, true);
    assert(discoveries == 1 && stats().pending_recipients == 1);
    assert(write_list(NULL, 0));
    assert(stats().pending_recipients == 0);
    assert(stats().destinations[0].canceled);
    assert(write_list(encoded, length));
    device_resolved = true;
    tick(1000, true);
    assert(sends == 0);
    tick(2000, false);
    tick(3000, true);
    assert(sends == 0);
    BACNET_TIMESTAMP timestamp;
    bacnet_restart_timestamp_get(&timestamp);
    assert(bacapp_timestamp_same(&timestamp, &expected_timestamp));
    expected_timestamp.value.dateTime.time.hour++;
    bacnet_restart_init(BACNET_RESTART_LOAD_MISSING, NULL, 0, &expected_timestamp,
        RESTART_REASON_WARMSTART, &callbacks, &sends);
    tick(4000, true);
    assert(sends == 1);
    expected_timestamp.value.dateTime.time.hour++;
    BACNET_TIMESTAMP supplied = expected_timestamp;
    bacnet_restart_init(BACNET_RESTART_LOAD_MISSING, NULL, 0, &supplied,
        RESTART_REASON_WARMSTART, &callbacks, &sends);
    supplied.value.dateTime.time.hour++;
    tick(5000, true);
    assert(sends == 2);
    supplied.tag = TIME_STAMP_SEQUENCE;
    bacnet_restart_init(BACNET_RESTART_LOAD_MISSING, NULL, 0, &supplied,
        RESTART_REASON_WARMSTART, &callbacks, &sends);
    tick(6000, true);
    assert(sends == 2 && stats().configuration_errors == 1);
    puts("PASS pre-ready edits, cancellation, no fabricated restart, frozen DateTime, invalid time suppression");
}

static void test_deferred_timestamp(void)
{
    uint8_t encoded[256], readback[256];
    init(BACNET_RESTART_LOAD_MISSING, NULL, 0);
    bacnet_restart_init(BACNET_RESTART_LOAD_MISSING, NULL, 0, NULL,
        RESTART_REASON_WARMSTART, &callbacks, &sends);
    assert(!stats().timestamp_valid && stats().configuration_errors == 0);
    tick(0, true);
    assert(!stats().boot_ready && sends == 0);
    size_t length = encode_address(encoded, 0, 6);
    assert(write_list(encoded, length));
    BACNET_TIMESTAMP invalid = {.tag = TIME_STAMP_SEQUENCE};
    assert(!bacnet_restart_set_timestamp_before_ready(&invalid));
    assert(bacnet_restart_set_timestamp_before_ready(&expected_timestamp));
    assert(read_list(readback, sizeof(readback)) == (int)length &&
        memcmp(encoded, readback, length) == 0);
    failures_left = 1;
    tick(5000, true);
    assert(sends == 1 && stats().pending_recipients == 1);
    BACNET_TIMESTAMP changed = expected_timestamp;
    changed.value.dateTime.time.hour++;
    assert(!bacnet_restart_set_timestamp_before_ready(&changed));
    tick(6000, true); /* Every retry still encodes the original chosen time. */
    assert(sends == 2 && stats().notifications_sent == 1);
    tick(7000, false);
    tick(8000, true);
    assert(sends == 2);
    BACNET_TIMESTAMP received;
    bacnet_restart_timestamp_get(&received);
    assert(bacapp_timestamp_same(&received, &expected_timestamp));

    /* A subsequent actual initialization/boot may choose a different time. */
    expected_timestamp = changed;
    bacnet_restart_init(BACNET_RESTART_LOAD_VALID, encoded, length, NULL,
        RESTART_REASON_WARMSTART, &callbacks, &sends);
    assert(bacnet_restart_set_timestamp_before_ready(&expected_timestamp));
    tick(9000, true);
    assert(sends == 3);
    puts("PASS deferred clock selection preserves recipients; first ready tick freezes all sends; new boot can differ");
}

static void test_malformed_prefixes_and_fuzz(void)
{
    uint8_t encoded[256];
    size_t length = encode_device(encoded, 2000);
    length += encode_address(encoded + length, 0, 6);
    length += encode_address(encoded + length, 0, 0);
    for (size_t prefix = 0; prefix <= length; ++prefix) {
        init(BACNET_RESTART_LOAD_MISSING, NULL, 0);
        (void)write_list(encoded, prefix);
        bacnet_restart_init(BACNET_RESTART_LOAD_VALID, encoded, prefix, &expected_timestamp,
            RESTART_REASON_WARMSTART, &callbacks, &sends);
    }
    for (unsigned byte = 0; byte < 256; ++byte) {
        encoded[0] = (uint8_t)byte;
        assert(!write_list(encoded, 1));
    }
    uint32_t random = 0x521ad;
    for (unsigned trial = 0; trial < 2000; ++trial) {
        size_t size = trial % 257;
        for (size_t i = 0; i < size; ++i) {
            random = random * 1664525U + 1013904223U;
            encoded[i] = random >> 24;
        }
        (void)write_list(encoded, size);
    }
    puts("PASS every valid-list prefix, all one-byte tags, 2000 bounded malformed payloads");
}

int main(void)
{
    test_default_and_wire();
    test_storage_and_atomic_write();
    test_validation_and_bounds();
    test_scheduler_retry_and_routing();
    test_recipient_edits_and_timestamp();
    test_deferred_timestamp();
    test_malformed_prefixes_and_fuzz();
    puts("Restart module: all seven scenario groups passed; no network or relay operations.");
    return 0;
}
