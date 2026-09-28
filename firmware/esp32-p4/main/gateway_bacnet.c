/* SPDX-License-Identifier: 0BSD
 * Adapted from JimBoHa ESP32-P4 Modbus gateway 0729b7b; see ATTRIBUTION.md. */
#include "gateway_bacnet.h"
#include "iq_diagnostics.h"
#include "bacnet_restart.h"
#include "bip_port.h"
#include <float.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include "bacnet/bacapp.h"
#include "bacnet/bacdcode.h"
#include "bacnet/bacstr.h"
#include "bacnet/cov.h"
#include "bacnet/datetime.h"
#include "bacnet/ihave.h"
#include "bacnet/iam.h"
#include "bacnet/npdu.h"
#include "bacnet/whohas.h"
#include "bacnet/basic/binding/address.h"
#include "bacnet/basic/object/ai.h"
#include "bacnet/basic/object/bi.h"
#include "bacnet/basic/object/device.h"
#include "bacnet/basic/object/netport.h"
#include "bacnet/basic/npdu/h_npdu.h"
#include "bacnet/basic/services.h"
#include "bacnet/basic/service/h_wpm.h"
#include "bacnet/basic/tsm/tsm.h"

#define NETWORK_INSTANCE 1u
#define RECOVERY_CAPACITY (GATEWAY_BACNET_MAX_POINTS + 8u)

static gateway_bacnet_config_t config;
static gateway_bacnet_stats_t stats;
static char device_name[96], firmware_version[MAX_DEV_VER_LEN + 1], location[MAX_DEV_LOC_LEN + 1];
static uint64_t now_ms, started_ms, last_timer_ms, last_second_ms, next_announce_ms;
static uint64_t next_instance_check;
static uint64_t runtime_uptime,clock_updated_ms,restart_wait_ms;
static int64_t runtime_utc;
static bool runtime_ready,restart_wait_started;
static int32_t device_optional[96];
static uint8_t receive_buffer[MAX_PDU], transmit_buffer[MAX_PDU];
typedef struct { bool used; BACNET_OBJECT_TYPE type; uint32_t instance; uint64_t due; } recovery_t;
static recovery_t recovery[RECOVERY_CAPACITY];
static uint8_t failed_pdu[MAX_PDU];

unsigned long mstimer_now(void) { return (unsigned long)now_ms; }

static bool read_only(BACNET_WRITE_PROPERTY_DATA *data)
{
    data->error_class = ERROR_CLASS_PROPERTY;
    data->error_code = ERROR_CODE_WRITE_ACCESS_DENIED;
    return false;
}

static void no_writable_properties(uint32_t instance, const int32_t **properties)
{
    (void)instance;
    static const int32_t empty[] = {-1};
    if (properties) { *properties = empty; }
}

static bool retry_pending(BACNET_OBJECT_TYPE type, uint32_t instance)
{
    for (unsigned i = 0; i < RECOVERY_CAPACITY; ++i) {
        if (recovery[i].used && recovery[i].type == type && recovery[i].instance == instance &&
            now_ms >= recovery[i].due) { return true; }
    }
    return false;
}

static void retry_clear(BACNET_OBJECT_TYPE type, uint32_t instance)
{
    for (unsigned i = 0; i < RECOVERY_CAPACITY; ++i) {
        if (recovery[i].used && recovery[i].type == type && recovery[i].instance == instance) {
            if (now_ms >= recovery[i].due) { stats.cov_refreshes++; }
            recovery[i].used = false;
            stats.cov_pending--;
        }
    }
}

#define COV_WRAPPERS(name, type, prefix) \
    static bool name##_changed(uint32_t instance) { \
        return prefix##_Change_Of_Value(instance) || retry_pending(type, instance); } \
    static void name##_clear(uint32_t instance) { \
        prefix##_Change_Of_Value_Clear(instance); retry_clear(type, instance); }
COV_WRAPPERS(ai, OBJECT_ANALOG_INPUT, Analog_Input)
COV_WRAPPERS(bi, OBJECT_BINARY_INPUT, Binary_Input)
static int device_read_property(BACNET_READ_PROPERTY_DATA *data)
{
    if(data&&data->object_property==PROP_RESTART_NOTIFICATION_RECIPIENTS)
        return bacnet_restart_read_property(data);
    return Device_Read_Property_Local(data);
}
static void device_property_lists(const int32_t **required,const int32_t **optional,const int32_t **proprietary)
{
    Device_Property_Lists(required,NULL,proprietary);
    if(optional)*optional=device_optional;
}
static object_functions_t object_table[] = {
    {.Object_Type=OBJECT_DEVICE, .Object_Count=Device_Count,
     .Object_Index_To_Instance=Device_Index_To_Instance, .Object_Valid_Instance=Device_Valid_Object_Instance_Number,
     .Object_Name=Device_Object_Name, .Object_Read_Property=device_read_property,
     .Object_Write_Property=read_only, .Object_RPM_List=device_property_lists,
     .Object_Writable_Property_List=no_writable_properties},
    {.Object_Type=OBJECT_ANALOG_INPUT, .Object_Init=Analog_Input_Init, .Object_Count=Analog_Input_Count,
     .Object_Index_To_Instance=Analog_Input_Index_To_Instance, .Object_Valid_Instance=Analog_Input_Valid_Instance,
     .Object_Name=Analog_Input_Object_Name, .Object_Read_Property=Analog_Input_Read_Property,
     .Object_Write_Property=read_only, .Object_RPM_List=Analog_Input_Property_Lists,
     .Object_Value_List=Analog_Input_Encode_Value_List, .Object_COV=ai_changed, .Object_COV_Clear=ai_clear,
     .Object_Writable_Property_List=no_writable_properties},
    {.Object_Type=OBJECT_BINARY_INPUT, .Object_Init=Binary_Input_Init, .Object_Count=Binary_Input_Count,
     .Object_Index_To_Instance=Binary_Input_Index_To_Instance, .Object_Valid_Instance=Binary_Input_Valid_Instance,
     .Object_Name=Binary_Input_Object_Name, .Object_Read_Property=Binary_Input_Read_Property,
     .Object_Write_Property=read_only, .Object_RPM_List=Binary_Input_Property_Lists,
     .Object_Value_List=Binary_Input_Encode_Value_List, .Object_COV=bi_changed, .Object_COV_Clear=bi_clear,
     .Object_Writable_Property_List=no_writable_properties},
    {.Object_Type=OBJECT_NETWORK_PORT, .Object_Init=Network_Port_Init, .Object_Count=Network_Port_Count,
     .Object_Index_To_Instance=Network_Port_Index_To_Instance, .Object_Valid_Instance=Network_Port_Valid_Instance,
     .Object_Name=Network_Port_Object_Name, .Object_Read_Property=Network_Port_Read_Property,
     .Object_Write_Property=read_only, .Object_RPM_List=Network_Port_Property_Lists,
     .Object_Writable_Property_List=no_writable_properties},
    {.Object_Type=MAX_BACNET_OBJECT_TYPE}
};

static BACNET_TIMESTAMP fallback_timestamp(void)
{
    BACNET_TIMESTAMP stamp={.tag=TIME_STAMP_DATETIME};
    datetime_set_date(&stamp.value.dateTime.date,1990,1,1);
    datetime_set_time(&stamp.value.dateTime.time,0,0,0,0);return stamp;
}
static bool utc_datetime(int64_t utc_ms,BACNET_DATE_TIME *out)
{
    if(utc_ms<1704067200000LL||utc_ms>=4102444800000LL)return false;
    time_t seconds=(time_t)(utc_ms/1000);struct tm date;
    if(!gmtime_r(&seconds,&date))return false;
    datetime_set_date(&out->date,date.tm_year+1900,date.tm_mon+1,date.tm_mday);
    datetime_set_time(&out->time,date.tm_hour,date.tm_min,date.tm_sec,(utc_ms%1000)/10);return true;
}
static bool restart_resolve(const BACNET_RECIPIENT *recipient,BACNET_ADDRESS *destination,void *context)
{
    (void)context;
    if(recipient->tag!=BACNET_RECIPIENT_TAG_ADDRESS||recipient->type.address.net!=0||recipient->type.address.mac_len!=0)return false;
    *destination=recipient->type.address;return true;
}
static int restart_send(const BACNET_ADDRESS *destination,const uint8_t *apdu,size_t length,void *context)
{
    (void)context;BACNET_ADDRESS target=*destination,source={0};BACNET_NPDU_DATA npdu;
    uint8_t pdu[MAX_PDU];bip_get_my_address(&source);npdu_encode_npdu_data(&npdu,false,MESSAGE_PRIORITY_NORMAL);
    int offset=npdu_encode_pdu(pdu,&target,&source,&npdu);
    if(offset<=0||(size_t)offset>sizeof(pdu)||length>sizeof(pdu)-(size_t)offset)return -1;
    memcpy(pdu+offset,apdu,length);return bip_send_pdu(&target,&npdu,pdu,(unsigned)offset+length);
}
static bool restart_initialize(void)
{
    const int32_t *optional=NULL;Device_Property_Lists(NULL,&optional,NULL);unsigned count=0;
    while(optional&&optional[count]!=-1) {
        if(count>=sizeof(device_optional)/sizeof(device_optional[0])-2)return false;
        device_optional[count]=optional[count];++count;
    }
    device_optional[count++]=PROP_RESTART_NOTIFICATION_RECIPIENTS;device_optional[count]=-1;
    BACNET_TIMESTAMP fallback=fallback_timestamp();
    datetime_utc_offset_minutes_set(0);datetime_dst_enabled_set(false);
    datetime_timesync(&fallback.value.dateTime.date,&fallback.value.dateTime.time,false);
    Device_Set_Time_Of_Restart(&fallback);Device_Last_Restart_Reason_Set(config.restart_reason);
    const bacnet_restart_callbacks_t callbacks={.resolve=restart_resolve,.send=restart_send};
    /* A read-only recipient property is permitted with the standard default. */
    bacnet_restart_init(BACNET_RESTART_LOAD_MISSING,NULL,0,NULL,config.restart_reason,&callbacks,NULL);
    runtime_ready=restart_wait_started=false;runtime_uptime=clock_updated_ms=restart_wait_ms=0;runtime_utc=0;
    return true;
}
void gateway_bacnet_runtime(uint64_t uptime_ms,int64_t utc_ms,bool startup_ready)
{ runtime_uptime=uptime_ms;runtime_utc=utc_ms;runtime_ready=startup_ready; }
static void restart_tick(void)
{
    BACNET_DATE_TIME current;
    if(utc_datetime(runtime_utc,&current)&&(!clock_updated_ms||now_ms-clock_updated_ms>=1000)) {
        datetime_timesync(&current.date,&current.time,false);clock_updated_ms=now_ms;
    }
    bool ready=runtime_ready&&stats.link_up;
    if(ready&&!stats.restart_timestamp_frozen) {
        if(!restart_wait_started) { restart_wait_started=true;restart_wait_ms=now_ms; }
        BACNET_TIMESTAMP stamp=fallback_timestamp();
        bool valid=runtime_utc>0&&runtime_uptime<=(uint64_t)runtime_utc&&
            utc_datetime(runtime_utc-(int64_t)runtime_uptime,&stamp.value.dateTime);
        if(valid||now_ms-restart_wait_ms>=5000) {
            if(bacnet_restart_set_timestamp_before_ready(&stamp)) {
                Device_Set_Time_Of_Restart(&stamp);stats.restart_timestamp_frozen=true;stats.restart_clock_valid=valid;
                stats.restart_boot_utc_ms=valid?runtime_utc-(int64_t)runtime_uptime:0;
                iq_event("bacnet","restart_timestamp",0,valid?"Frozen NTP boot timestamp":"Frozen unsynchronized 1990 fallback timestamp");
            }
        }
    }
    bacnet_restart_tick(now_ms,ready,config.device_instance,Device_System_Status());
    bacnet_restart_stats_t state;bacnet_restart_stats_get(&state);
    stats.restart_sent=state.notifications_sent;stats.restart_failures=state.send_failures;
    stats.restart_exhausted=state.exhausted_recipients;
}

static void observe_i_am(uint8_t *data,uint16_t length,BACNET_ADDRESS *source)
{
    uint32_t instance;unsigned max_apdu;int segmentation;uint16_t vendor;
    if(!source||source->mac_len!=6||
       bacnet_iam_request_decode(data,length,&instance,&max_apdu,&segmentation,&vendor)!=(int)length||
       instance!=config.device_instance)return;
    uint32_t peer;memcpy(&peer,source->mac,4);
    uint16_t port=((uint16_t)source->mac[4]<<8)|source->mac[5];
    if(source->net==0&&peer==config.local_ip&&port==config.udp_port)return;
    if(stats.instance_conflicts<UINT32_MAX)++stats.instance_conflicts;
    stats.conflict_ip=peer;stats.conflict_port=port;stats.conflict_network=source->net;
    stats.last_conflict_ms=now_ms;
    iq_event("bacnet","duplicate_instance",1,"Another BACnet address advertised this Device instance; identity remains fixed");
}

static void who_is(uint8_t *data, uint16_t length, BACNET_ADDRESS *source)
{
    if (!stats.link_up) { return; }
    if (bip_port_last_receive_was_broadcast()) { handler_who_is(data, length, source); }
    else { handler_who_is_unicast(data, length, source); }
}

static void who_has(uint8_t *request, uint16_t length, BACNET_ADDRESS *source)
{
    BACNET_WHO_HAS_DATA query;
    if (!stats.link_up || whohas_decode_service_request(request, length, &query) != length) { return; }
    uint32_t device = Device_Object_Instance_Number();
    if (query.low_limit >= 0 && query.high_limit >= 0 &&
        (device < (uint32_t)query.low_limit || device > (uint32_t)query.high_limit)) { return; }
    BACNET_I_HAVE_DATA response = {.device_id={OBJECT_DEVICE, device}};
    BACNET_OBJECT_TYPE type;
    uint32_t instance;
    if (query.is_object_name) {
        if (!Device_Valid_Object_Name(&query.object.name, &type, &instance)) { return; }
        response.object_id.type = type; response.object_id.instance = instance;
        characterstring_copy(&response.object_name, &query.object.name);
    } else {
        response.object_id = query.object.identifier;
        if (!Device_Object_Name_Copy(response.object_id.type, response.object_id.instance, &response.object_name)) { return; }
    }
    BACNET_ADDRESS destination = *source, local;
    BACNET_NPDU_DATA npdu;
    if (bip_port_last_receive_was_broadcast()) { bip_get_broadcast_address(&destination); }
    bip_get_my_address(&local);
    npdu_encode_npdu_data(&npdu, false, MESSAGE_PRIORITY_NORMAL);
    int encoded = npdu_encode_pdu(transmit_buffer, &destination, &local, &npdu);
    encoded += ihave_encode_apdu(transmit_buffer + encoded, &response);
    (void)bip_send_pdu(&destination, &npdu, transmit_buffer, (unsigned)encoded);
}

static void cov_timeout(uint8_t invoke)
{
    BACNET_ADDRESS dest = {0}, npdu_dest = {0}, source = {0};
    BACNET_NPDU_DATA npdu = {0};
    BACNET_PROPERTY_VALUE values[2] = {0};
    BACNET_COV_DATA cov = {0};
    uint16_t length = 0;
    if (!tsm_get_transaction_pdu(invoke, &dest, &npdu, failed_pdu, &length)) { return; }
    int offset = bacnet_npdu_decode(failed_pdu, length, &npdu_dest, &source, &npdu);
    if (offset <= 0 || offset + 4 > length || npdu.network_layer_message ||
        failed_pdu[offset] != PDU_TYPE_CONFIRMED_SERVICE_REQUEST ||
        failed_pdu[offset + 2] != invoke || failed_pdu[offset + 3] != SERVICE_CONFIRMED_COV_NOTIFICATION) { return; }
    bacapp_property_value_list_init(values, 2);
    cov.listOfValues = values;
    if (cov_notify_decode_service_request(failed_pdu + offset + 4, length - offset - 4, &cov) != length - offset - 4) { return; }
    stats.cov_timeouts++;iq_event("bacnet","cov_timeout",1,"Confirmed COV acknowledgement deadline exceeded; refresh scheduled");
    for (unsigned i = 0; i < RECOVERY_CAPACITY; ++i) {
        if (recovery[i].used && recovery[i].type == cov.monitoredObjectIdentifier.type &&
            recovery[i].instance == cov.monitoredObjectIdentifier.instance) { return; }
    }
    for (unsigned i = 0; i < RECOVERY_CAPACITY; ++i) {
        if (!recovery[i].used) {
            recovery[i] = (recovery_t){true, cov.monitoredObjectIdentifier.type,
                cov.monitoredObjectIdentifier.instance, now_ms + 1000};
            stats.cov_pending++;
            return;
        }
    }
}

static bool create_point(const iq_point_def_t *point)
{
    uint32_t id = point->instance;
    switch (point->object_type) {
        case OBJECT_ANALOG_INPUT:
            if (Analog_Input_Create(id) != id) { return false; }
            Analog_Input_Name_Set(id, point->name);
            Analog_Input_Description_Set(id, point->description ? point->description : "");
            Analog_Input_Units_Set(id, point->units);
            Analog_Input_COV_Increment_Set(id, (float)point->cov_increment);
            Analog_Input_Reliability_Set(id, RELIABILITY_COMMUNICATION_FAILURE);
            break;
        case OBJECT_BINARY_INPUT:
            if (Binary_Input_Create(id) != id) { return false; }
            Binary_Input_Name_Set(id, point->name);
            Binary_Input_Description_Set(id, point->description ? point->description : "");
            Binary_Input_Active_Text_Set(id, id == 1 ? "Fresh" : "Active");
            Binary_Input_Inactive_Text_Set(id, id == 1 ? "Stale or failed" : "Inactive");
            Binary_Input_Reliability_Set(id, RELIABILITY_COMMUNICATION_FAILURE);
            break;
        default: return false;
    }
    return true;
}

static void network_properties(void)
{
    uint8_t ip[4], gateway[4], mac[6];
    memcpy(ip, &config.local_ip, 4); memcpy(gateway, &config.gateway, 4);
    memcpy(mac, ip, 4); encode_unsigned16(mac + 4, config.udp_port);
    Network_Port_IP_Address_Set(NETWORK_INSTANCE, ip[0], ip[1], ip[2], ip[3]);
    Network_Port_IP_Subnet_Prefix_Set(NETWORK_INSTANCE, bip_get_subnet_prefix());
    Network_Port_IP_Gateway_Set(NETWORK_INSTANCE, gateway[0], gateway[1], gateway[2], gateway[3]);
    Network_Port_MAC_Address_Set(NETWORK_INSTANCE, mac, 6);
    Network_Port_Reliability_Set(NETWORK_INSTANCE, stats.link_up ? RELIABILITY_NO_FAULT_DETECTED : RELIABILITY_COMMUNICATION_FAILURE);
    Network_Port_Changes_Pending_Set(NETWORK_INSTANCE, false);
}

static void announce(void)
{
    if (!stats.link_up || !bip_valid()) { return; }
    Send_I_Am_Broadcast(transmit_buffer);
    for (size_t i = 0; i < config.peer_count; ++i) {
        BACNET_ADDRESS peer = {0};
        peer.mac_len = 6;
        memcpy(peer.mac, &config.peers[i].ip, 4);
        encode_unsigned16(peer.mac + 4, config.peers[i].port ? config.peers[i].port : config.udp_port);
        Send_I_Am_Unicast(transmit_buffer, &peer);
    }
    next_announce_ms = now_ms + 60000;
}

bool gateway_bacnet_init(const gateway_bacnet_config_t *input, uint64_t timestamp)
{
    if (!input || stats.initialized || input->device_instance >= BACNET_MAX_INSTANCE ||
        input->device_instance == 75151 || !input->device_name || !input->device_name[0] ||
        strlen(input->device_name) > 63 || !input->local_ip || !input->udp_port ||
        input->peer_count > GATEWAY_BACNET_MAX_PEERS) return false;
    for (unsigned i=0; i<IQ_POINT_COUNT; ++i)
        if (!strcmp(input->device_name,iq_points[i].name)) return false;
    if (!strcmp(input->device_name,"IQData-Ethernet")) return false;
    config = *input;
    config.points = iq_points; config.point_count = IQ_POINT_COUNT;
    snprintf(device_name, sizeof(device_name), "%s", input->device_name);
    snprintf(firmware_version, sizeof(firmware_version), "%s", input->firmware_version ? input->firmware_version : "development");
    snprintf(location, sizeof(location), "%s", input->location ? input->location : "");
    config.device_name = device_name; config.firmware_version = firmware_version; config.location = location;
    now_ms = started_ms = last_timer_ms = last_second_ms = timestamp;
    stats = (gateway_bacnet_stats_t){.link_up = true, .fault_points = (uint32_t)config.point_count};
    memset(recovery, 0, sizeof(recovery));
    bip_port_configure(config.local_ip, config.netmask, config.gateway, config.udp_port);
    Device_Init(object_table);
    Device_Set_Object_Instance_Number(config.device_instance);
    Device_Object_Name_ANSI_Init(device_name);
    Device_Set_Vendor_Name("IQData site integration", strlen("IQData site integration"));
    Device_Set_Vendor_Identifier(config.vendor_id);
    const char *model_name = input->model_name ? input->model_name : "IQ Data Plus II P4 Gateway";
    const char *description = input->description ? input->description : "IQ Data Plus II via Pico USB; read-only meter gateway";
    Device_Set_Model_Name(model_name, strlen(model_name));
    Device_Set_Description(description, strlen(description));
    Device_Set_Location(location, strlen(location));
    Device_Set_Firmware_Revision(firmware_version, strlen(firmware_version));
    Device_Set_Application_Software_Version(firmware_version, strlen(firmware_version));
    Device_Set_System_Status(STATUS_OPERATIONAL, true);
    if(!restart_initialize()) { gateway_bacnet_shutdown();return false; }
    for (size_t i = 0; i < config.point_count; ++i) {
        if (!create_point(&config.points[i])) { gateway_bacnet_shutdown(); return false; }
    }
    Network_Port_Object_Instance_Number_Set(0, NETWORK_INSTANCE);
    Network_Port_Name_Set(NETWORK_INSTANCE, "IQData-Ethernet");
    Network_Port_Description_Set(NETWORK_INSTANCE, "BACnet/IP Ethernet port; configuration is read-only over BACnet");
    Network_Port_Type_Set(NETWORK_INSTANCE, PORT_TYPE_BIP);
    Network_Port_Network_Number_Set(NETWORK_INSTANCE, 0);
    Network_Port_BIP_Port_Set(NETWORK_INSTANCE, config.udp_port);
    Network_Port_BIP_Mode_Set(NETWORK_INSTANCE, BACNET_IP_MODE_NORMAL);
    Network_Port_APDU_Length_Set(NETWORK_INSTANCE, MAX_APDU);
    Network_Port_Link_Speed_Set(NETWORK_INSTANCE, 100000000.0f);
    Network_Port_IP_DHCP_Enable_Set(NETWORK_INSTANCE, config.dhcp_enabled);
    Network_Port_Quality_Set(NETWORK_INSTANCE, PORT_QUALITY_UNKNOWN);
    network_properties();
    address_init();
    apdu_set_unrecognized_service_handler_handler(handler_unrecognized_service);
    apdu_set_unconfirmed_handler(SERVICE_UNCONFIRMED_WHO_IS, who_is);
    apdu_set_unconfirmed_handler(SERVICE_UNCONFIRMED_WHO_HAS, who_has);
    apdu_set_unconfirmed_handler(SERVICE_UNCONFIRMED_I_AM, observe_i_am);
    apdu_set_confirmed_handler(SERVICE_CONFIRMED_READ_PROPERTY, handler_read_property);
    apdu_set_confirmed_handler(SERVICE_CONFIRMED_READ_PROP_MULTIPLE, handler_read_property_multiple);
    apdu_set_confirmed_handler(SERVICE_CONFIRMED_WRITE_PROPERTY, handler_write_property);
    apdu_set_confirmed_handler(SERVICE_CONFIRMED_WRITE_PROP_MULTIPLE, handler_write_property_multiple);
    apdu_set_confirmed_handler(SERVICE_CONFIRMED_SUBSCRIBE_COV, handler_cov_subscribe);
    apdu_timeout_set(3000); apdu_retries_set(3);
    handler_cov_init(); tsm_set_timeout_handler(cov_timeout);
    if (!bip_init(NULL)) { gateway_bacnet_shutdown(); return false; }
    Device_Set_Database_Revision(input->database_revision ? input->database_revision : 1);
    stats.initialized = true;
    next_instance_check=now_ms;
    announce();
    return true;
}

void gateway_bacnet_update(const iq_value_t *values)
{
    if (!stats.initialized || !values) return;
    stats.good_points=0;
    for (unsigned i=0;i<IQ_POINT_COUNT;++i) {
        const iq_point_def_t *p=&iq_points[i];
        bool good=values[i].valid&&isfinite(values[i].value)&&fabs(values[i].value)<=FLT_MAX;
        if (p->object_type==OBJECT_BINARY_INPUT)
            good=good&&(values[i].value==0||values[i].value==1);
        BACNET_RELIABILITY reliability=good?RELIABILITY_NO_FAULT_DETECTED:RELIABILITY_COMMUNICATION_FAILURE;
        if (p->object_type==OBJECT_ANALOG_INPUT) {
            if(good) Analog_Input_Present_Value_Set(p->instance,(float)values[i].value);
            Analog_Input_Reliability_Set(p->instance,reliability);
        } else {
            if(good) Binary_Input_Present_Value_Set(p->instance,values[i].value?BINARY_ACTIVE:BINARY_INACTIVE);
            Binary_Input_Reliability_Set(p->instance,reliability);
        }
        stats.good_points+=good;
    }
    stats.fault_points=IQ_POINT_COUNT-stats.good_points;
}

void gateway_bacnet_tick(uint64_t timestamp)
{
    if (!stats.initialized) { return; }
    if (timestamp < now_ms) { return; } /* Caller supplies monotonic milliseconds. */
    now_ms = timestamp;
    restart_tick();
    uint64_t delta = now_ms - last_timer_ms;
    if (delta) {
        /* TSM accepts 16-bit elapsed time. A delayed task needs only enough
         * elapsed time to expire outstanding 3-second transactions; do not
         * wrap the timer or run an unbounded catch-up loop after a long pause. */
        tsm_timer_milliseconds(delta > UINT16_MAX ? UINT16_MAX : (uint16_t)delta);
        last_timer_ms = now_ms;
    }
    delta = (now_ms - last_second_ms) / 1000;
    if (delta) { handler_cov_timer_seconds(delta > UINT32_MAX ? UINT32_MAX : (uint32_t)delta); last_second_ms += delta * 1000; }
    if (stats.link_up) {
        if(now_ms>=next_instance_check) {
            Send_WhoIs_Local(config.device_instance,config.device_instance);
            if(stats.instance_checks<UINT32_MAX)++stats.instance_checks;
            next_instance_check=now_ms+60000;
        }
        if(stats.instance_checks&&now_ms-started_ms>=3000)stats.instance_check_complete=true;
        /* One full bounded pass through at most 256 subscriptions; no wait for ACK. */
        for (unsigned step = 0; step < 4u * MAX_COV_SUBSCRIPTIONS + 8u; ++step) {
            if (handler_cov_fsm()) { break; }
        }
        if (now_ms >= next_announce_ms) { announce(); }
    }
}

bool gateway_bacnet_process_datagram(const uint8_t *data, size_t size,
                                    uint32_t source_ip, uint16_t source_port)
{
    if (!stats.initialized || !stats.link_up) { return false; }
    BACNET_ADDRESS source;
    uint16_t length = bip_port_decode_datagram(data, size, source_ip, source_port,
        &source, receive_buffer, sizeof(receive_buffer));
    if (!length) { return false; }
    stats.received_packets++;
    npdu_handler(&source, receive_buffer, length);
    return true;
}

unsigned gateway_bacnet_poll(unsigned timeout_ms)
{
    if (!stats.initialized || !stats.link_up) { return 0; }
    unsigned count = 0;
    for (; count < 16; ++count) {
        BACNET_ADDRESS source;
        uint16_t length = bip_receive(&source, receive_buffer, sizeof(receive_buffer), count == 0 ? timeout_ms : 0);
        if (!length) { break; }
        stats.received_packets++;
        npdu_handler(&source, receive_buffer, length);
    }
    return count;
}

bool gateway_bacnet_network_update(uint32_t ip, uint32_t mask, uint32_t gateway,
                                   bool link_up, uint64_t timestamp)
{
    if (!stats.initialized) { return false; }
    now_ms = timestamp >= now_ms ? timestamp : now_ms;
    bool changed = config.local_ip != ip || config.netmask != mask || config.gateway != gateway;
    bool was_up = stats.link_up;
    stats.link_up = link_up && ip != 0;
    if (stats.link_up) {
        config.local_ip = ip; config.netmask = mask; config.gateway = gateway;
        bip_port_configure(ip, mask, gateway, config.udp_port);
        if ((!was_up || changed || !bip_valid()) && !bip_init(NULL)) { stats.link_up = false; }
    } else { bip_cleanup(); }
    network_properties();
    if (stats.link_up && (!was_up || changed)) { announce(); }
    return stats.link_up == link_up;
}

void gateway_bacnet_stats(gateway_bacnet_stats_t *out) { if (out) { *out = stats; } }
void gateway_bacnet_shutdown(void)
{
    bip_cleanup();
    handler_cov_init();
    /* The stack keeps its COV FSM position across handler_cov_init(). Empty
     * the state machine before loading another catalog in the same process. */
    for (unsigned step = 0; step < 8; ++step) { if (handler_cov_fsm()) { break; } }
    for (unsigned invoke = 1; invoke <= UINT8_MAX; ++invoke) { tsm_free_invoke_id((uint8_t)invoke); }
    Analog_Input_Cleanup(); Binary_Input_Cleanup(); Network_Port_Cleanup();
    memset(recovery, 0, sizeof(recovery));
    config = (gateway_bacnet_config_t){0};
    stats = (gateway_bacnet_stats_t){0};
    now_ms = started_ms = last_timer_ms = last_second_ms = next_announce_ms = 0;
}
