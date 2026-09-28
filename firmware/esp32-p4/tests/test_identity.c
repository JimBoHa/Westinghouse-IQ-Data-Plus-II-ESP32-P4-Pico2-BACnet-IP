#include "gateway_bacnet.h"
#include "bip_port.h"
#include "bacnet/iam.h"
#include "bacnet/basic/object/device.h"
#include <arpa/inet.h>
#include <assert.h>
static int send_packet(const BACNET_IP_ADDRESS *to,const uint8_t *data,uint16_t size,void *context)
{ (void)to;(void)data;(void)context;return size; }
static void claim(uint32_t instance,uint32_t source,uint16_t port,unsigned truncate)
{
    uint8_t frame[64]={0x81,0x0a,0,0,1,0};
    int size=6+iam_encode_apdu(frame+6,instance,1476,SEGMENTATION_NONE,260);
    size-=(int)truncate;frame[2]=(unsigned)size>>8;frame[3]=(unsigned)size;
    (void)gateway_bacnet_process_datagram(frame,size,source,port);
}
int main(void)
{
    bip_port_set_send_hook(send_packet,NULL);
    gateway_bacnet_config_t config={.device_instance=75201,.device_name="IQData-Test",.firmware_version="test",
        .local_ip=inet_addr("192.0.2.1"),.netmask=inet_addr("255.255.255.0"),.udp_port=47808};
    assert(gateway_bacnet_init(&config,1000));gateway_bacnet_tick(1000);
    gateway_bacnet_stats_t state;gateway_bacnet_stats(&state);assert(state.instance_checks==1);
    claim(75201,config.local_ip,47808,0);claim(75202,inet_addr("192.0.2.2"),47808,0);
    for(unsigned cut=1;cut<=8;cut++)claim(75201,inet_addr("192.0.2.2"),47808,cut);
    gateway_bacnet_stats(&state);assert(state.instance_conflicts==0);
    claim(75201,inet_addr("192.0.2.2"),47809,0);
    gateway_bacnet_stats(&state);assert(state.instance_conflicts==1&&state.conflict_port==47809);
    assert(state.conflict_ip==inet_addr("192.0.2.2"));
    gateway_bacnet_tick(61000);gateway_bacnet_stats(&state);
    assert(state.instance_check_complete&&state.instance_checks==2&&state.instance_conflicts==1);
    assert(Device_Object_Instance_Number()==75201);
    gateway_bacnet_shutdown();return 0;
}
