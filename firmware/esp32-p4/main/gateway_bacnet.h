/* SPDX-License-Identifier: 0BSD */
#ifndef GATEWAY_BACNET_H
#define GATEWAY_BACNET_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "iq_model.h"
#include "cJSON.h"

#define GATEWAY_BACNET_MAX_PEERS 4u
#define GATEWAY_BACNET_DISCOVERY_PEERS 8u
#define GATEWAY_BACNET_MAX_POINTS 256u
#define GATEWAY_BACNET_POINT_NAME_MAX 95u
#define GATEWAY_BACNET_POINT_DESCRIPTION_MAX 511u
#define GATEWAY_BACNET_STATE_NAME_MAX 63u
#define GATEWAY_BACNET_MAX_STATES 64u
typedef struct {
    uint32_t ip; /* IPv4 in network byte order. */
    uint16_t port; /* host byte order; zero uses BACnet listening port. */
} gateway_bacnet_peer_t;
typedef struct {
    uint32_t device_instance;
    const char *device_name;
    const char *firmware_version;
    const char *location;
    uint16_t vendor_id; /* Site/application vendor ID; no manufacturer identity claimed. */
    uint32_t local_ip, netmask, gateway; /* network byte order */
    uint16_t udp_port; /* host byte order, normally 47808 */
    bool dhcp_enabled;
    gateway_bacnet_peer_t peers[GATEWAY_BACNET_MAX_PEERS];
    size_t peer_count;
    /* Immutable generated IQData catalog; initialized internally. */
    const iq_point_def_t *points;
    size_t point_count;
    const char *model_name; /* Optional, at most 32 bytes. */
    const char *description; /* Optional, at most 64 bytes. */
    uint32_t database_revision; /* 0 uses 1; increment for each changed map. */
    uint8_t restart_reason;
} gateway_bacnet_config_t;
/* Bounded RAM-only discovery evidence. No measurement payloads or credentials. */
typedef struct {
    bool used,range_valid;
    uint32_t ip,requests,replies,failures,reads;
    uint16_t port,network;
    uint8_t address[6],address_len,bvlc_function,result,last_read_service;
    int32_t low_limit,high_limit;
    uint64_t last_seen_ms,last_who_is_ms,last_read_ms;
} gateway_bacnet_discovery_peer_t;
typedef struct {
    bool initialized, link_up;
    uint32_t received_packets, good_points, fault_points;
    uint32_t cov_timeouts, cov_refreshes, cov_pending;
    uint32_t instance_checks, instance_conflicts, conflict_ip;
    uint16_t conflict_port, conflict_network;
    uint64_t last_conflict_ms;
    bool instance_check_complete;
    bool restart_timestamp_frozen,restart_clock_valid;
    uint32_t restart_sent,restart_failures,restart_exhausted;
    int64_t restart_boot_utc_ms;
    uint32_t who_is_received,who_is_invalid,who_is_excluded,i_am_sent,i_am_failed;
    gateway_bacnet_discovery_peer_t discovery_peers[GATEWAY_BACNET_DISCOVERY_PEERS];
} gateway_bacnet_stats_t;

/* All functions belong to one BACnet task. Caller snapshots meter state
 * under its own lock, then calls update without holding that lock. */
bool gateway_bacnet_init(const gateway_bacnet_config_t *config, uint64_t now_ms);
/* values contains one entry per active catalog point, in catalog order. */
void gateway_bacnet_update(const iq_value_t *values);
void gateway_bacnet_tick(uint64_t now_ms);
/* Call from the BACnet owner before tick. UTC zero means unsynchronized. */
void gateway_bacnet_runtime(uint64_t uptime_ms,int64_t utc_ms,bool startup_ready);
unsigned gateway_bacnet_poll(unsigned timeout_ms);
bool gateway_bacnet_network_update(uint32_t ip, uint32_t mask, uint32_t gateway,
                                   bool link_up, uint64_t now_ms);
void gateway_bacnet_stats(gateway_bacnet_stats_t *out);
cJSON *gateway_bacnet_discovery_json(const gateway_bacnet_stats_t *state);
void gateway_bacnet_shutdown(void);
/* Production parser entry, also usable by an in-memory native test transport. */
bool gateway_bacnet_process_datagram(const uint8_t *data, size_t size,
                                    uint32_t source_ip, uint16_t source_port);
#endif
