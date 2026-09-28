#include "gateway_bacnet.h"
#include "bip_port.h"
#include "bacnet_restart.h"
#include "bacnet/basic/object/device.h"
#include <arpa/inet.h>
#include <assert.h>
#include <string.h>

static unsigned packets;
static bool fail;
static int send_packet(const BACNET_IP_ADDRESS *to,const uint8_t *data,uint16_t size,void *context)
{
    (void)to;(void)context;
    if(size>8&&data[6]==0x10&&data[7]==SERVICE_UNCONFIRMED_COV_NOTIFICATION) {
        /* Local BVLC broadcast with no routed/global NPDU destination. */
        assert(data[0]==0x81&&data[1]==0x0b&&data[4]==1&&data[5]==0);
        ++packets;return fail?-1:size;
    }
    return size;
}
static gateway_bacnet_config_t config;
static void start(bool fail_send)
{
    packets=0;fail=fail_send;bip_port_set_send_hook(send_packet,NULL);
    config=(gateway_bacnet_config_t){.device_instance=75201,.device_name="IQData-Test",
        .firmware_version="test",.local_ip=inet_addr("192.0.2.1"),
        .netmask=inet_addr("255.255.255.0"),.udp_port=47808,.restart_reason=RESTART_REASON_WARMSTART};
    assert(gateway_bacnet_init(&config,1000));
}
static void tick(uint64_t uptime,int64_t utc,bool ready)
{ gateway_bacnet_runtime(uptime,utc,ready);gateway_bacnet_tick(uptime); }
int main(void)
{
    start(false);tick(1000,0,false);tick(2000,0,true);tick(6999,0,true);assert(packets==0);
    tick(7000,0,true);assert(packets==1);
    BACNET_TIMESTAMP frozen,property;bacnet_restart_timestamp_get(&frozen);Device_Time_Of_Restart(&property);
    assert(frozen.value.dateTime.date.year==1990&&bacapp_timestamp_same(&frozen,&property));
    tick(8000,1790467208000LL,true);bacnet_restart_timestamp_get(&property);
    assert(bacapp_timestamp_same(&frozen,&property)&&packets==1);
    assert(gateway_bacnet_network_update(config.local_ip,config.netmask,0,false,9000));
    tick(9000,1790467209000LL,true);
    assert(gateway_bacnet_network_update(config.local_ip,config.netmask,0,true,10000));
    tick(10000,1790467210000LL,true);assert(packets==1);
    uint8_t buffer[256];BACNET_READ_PROPERTY_DATA read={.application_data=buffer,
        .application_data_len=sizeof(buffer),.object_type=OBJECT_DEVICE,.object_instance=75201,
        .object_property=PROP_RESTART_NOTIFICATION_RECIPIENTS,.array_index=BACNET_ARRAY_ALL};
    int length=Device_Read_Property(&read);assert(length>0);
    BACNET_WRITE_PROPERTY_DATA write={.object_type=OBJECT_DEVICE,.object_instance=75201,
        .object_property=PROP_RESTART_NOTIFICATION_RECIPIENTS,.array_index=BACNET_ARRAY_ALL,
        .application_data_len=length};
    memcpy(write.application_data,buffer,(size_t)length);
    assert(!Device_Write_Property(&write)&&write.error_code==ERROR_CODE_WRITE_ACCESS_DENIED);
    gateway_bacnet_shutdown();

    start(true);tick(1000,1790467201000LL,false);assert(!packets);
    tick(2000,1790467202000LL,true);assert(packets==1);
    tick(2500,1790467202500LL,true);assert(packets==1);
    for(unsigned i=3;i<=12;++i)tick(i*1000,1790467200000LL+i*1000,true);
    gateway_bacnet_stats_t stats;gateway_bacnet_stats(&stats);
    assert(packets==5&&stats.restart_failures==5&&stats.restart_exhausted==1);
    assert(stats.restart_sent==0&&stats.restart_clock_valid&&stats.restart_boot_utc_ms==1790467200000LL);
    gateway_bacnet_shutdown();
    start(false);tick(1000,1790467201000LL,true);assert(packets==1);
    gateway_bacnet_stats(&stats);assert(stats.restart_sent==1&&stats.restart_clock_valid);
    gateway_bacnet_shutdown();return 0;
}
