/* Independent wire requests through the real BVLC/NPDU/APDU stack. No LAN I/O. */
#include "gateway_bacnet.h"
#include "bip_port.h"
#include <arpa/inet.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>

static unsigned packets;
static bool fail_send;
static BACNET_IP_ADDRESS destination;
static uint8_t reply[256];
static unsigned reply_size;
static const uint8_t iam[]={0x10,0,0xc4,2,1,0x25,0xc2,0x22,5,0xc4,0x91,3,0x21,0};
static const uint8_t local[]={1,0};
static const uint8_t global[]={1,0x20,0xff,0xff,0,0xff};
static const uint8_t routed_local[]={1,8,0,123,1,0xa6};
static const uint8_t routed_global[]={1,0x28,0xff,0xff,0,0,123,1,0xa6,0xff};

static int send_packet(const BACNET_IP_ADDRESS *to,const uint8_t *data,uint16_t size,void *context)
{
    (void)context;assert(size<=sizeof(reply));
    destination=*to;memcpy(reply,data,size);reply_size=size;++packets;
    return fail_send?-1:size;
}
static void start(void)
{
    bip_port_set_send_hook(send_packet,NULL);
    gateway_bacnet_config_t config={.device_instance=75202,.device_name="Discovery-Test",
        .firmware_version="test",.local_ip=inet_addr("192.0.2.1"),
        .netmask=inet_addr("255.255.255.0"),.udp_port=47808};
    assert(gateway_bacnet_init(&config,1000));packets=0;
}
static void request(uint8_t function,const uint8_t *npdu,size_t npdu_len,
                    const uint8_t *limits,size_t limits_len)
{
    uint8_t frame[128]={0x81,function,0,0};size_t offset=4;
    if(function==4) {
        const uint8_t origin[]={192,0,2,6,0xba,0xca};
        memcpy(frame+offset,origin,sizeof(origin));offset+=sizeof(origin);
    }
    memcpy(frame+offset,npdu,npdu_len);offset+=npdu_len;
    frame[offset++]=0x10;frame[offset++]=8;
    if(limits_len) { memcpy(frame+offset,limits,limits_len);offset+=limits_len; }
    frame[2]=(uint8_t)(offset>>8);frame[3]=(uint8_t)offset;packets=0;
    assert(gateway_bacnet_process_datagram(frame,offset,inet_addr("192.0.2.20"),47819));
}
static void check_reply(bool forwarded,bool routed)
{
    uint32_t ip=inet_addr(forwarded?"192.0.2.6":"192.0.2.20");
    assert(packets==1&&memcmp(destination.address,&ip,4)==0);
    assert(destination.port==(forwarded?47818:47819));
    assert(reply[0]==0x81&&reply[1]==0x0a&&reply[2]==0&&reply[3]==reply_size);
    const uint8_t routed_dest[]={1,0x20,0,123,1,0xa6,0xff};
    const uint8_t *prefix=routed?routed_dest:local;
    size_t size=routed?sizeof(routed_dest):sizeof(local);
    assert(reply_size==4+size+sizeof(iam));
    assert(!memcmp(reply+4,prefix,size)&&!memcmp(reply+4+size,iam,sizeof(iam)));
}
int main(void)
{
    start();
    const uint8_t functions[]={10,11,4};
    const struct { const uint8_t *data;size_t size;bool routed; } npdus[]={
        {local,sizeof(local),false},{global,sizeof(global),false},
        {routed_local,sizeof(routed_local),true},{routed_global,sizeof(routed_global),true}};
    const uint8_t match[]={0x0b,1,0x25,0xc2,0x1b,1,0x25,0xc2};
    const uint8_t excluded[]={0x0b,1,0x25,0xc3,0x1b,1,0x25,0xc3};
    const uint8_t wide[]={0x09,0,0x1b,0x3f,0xff,0xfe};
    for(unsigned f=0;f<sizeof(functions);++f) {
        for(unsigned n=0;n<sizeof(npdus)/sizeof(npdus[0]);++n) {
            request(functions[f],npdus[n].data,npdus[n].size,match,sizeof(match));
            check_reply(functions[f]==4,npdus[n].routed);
            request(functions[f],npdus[n].data,npdus[n].size,NULL,0);
            check_reply(functions[f]==4,npdus[n].routed);
            request(functions[f],npdus[n].data,npdus[n].size,wide,sizeof(wide));
            check_reply(functions[f]==4,npdus[n].routed);
            request(functions[f],npdus[n].data,npdus[n].size,excluded,sizeof(excluded));
            assert(packets==0);
        }
    }
    /* A pair of ordered limits is required; no partial or trailing fields. */
    for(unsigned cut=1;cut<sizeof(match);++cut) {
        request(10,local,sizeof(local),match,cut);assert(packets==0);
    }
    uint8_t trailing[sizeof(match)+1];memcpy(trailing,match,sizeof(match));trailing[sizeof(match)]=0;
    request(10,local,sizeof(local),trailing,sizeof(trailing));assert(packets==0);
    const uint8_t reversed[]={0x0b,1,0x25,0xc3,0x1b,1,0x25,0xc1};
    request(10,local,sizeof(local),reversed,sizeof(reversed));assert(packets==0);
    gateway_bacnet_stats_t stats;gateway_bacnet_stats(&stats);
    assert(stats.who_is_received==57&&stats.who_is_invalid==9&&stats.who_is_excluded==12);
    assert(stats.i_am_sent==36&&stats.i_am_failed==0);
    fail_send=true;request(11,global,sizeof(global),match,sizeof(match));
    check_reply(false,false);fail_send=false;gateway_bacnet_stats(&stats);
    assert(stats.i_am_sent==36&&stats.i_am_failed==1);
    /* A subsequent standard ReadProperty proves that discovery progressed. */
    uint8_t read[]={0x81,0x0a,0,17,1,4,0,5,1,12,0x0c,2,1,0x25,0xc2,0x19,77};
    assert(gateway_bacnet_process_datagram(read,sizeof(read),inet_addr("192.0.2.20"),47819));
    gateway_bacnet_stats(&stats);cJSON *json=gateway_bacnet_discovery_json(&stats);assert(json);
    assert(cJSON_GetObjectItemCaseSensitive(json,"who_is_received")->valuedouble==58);
    cJSON *peers=cJSON_GetObjectItemCaseSensitive(json,"peers");assert(cJSON_GetArraySize(peers)==4);
    bool found=false;
    cJSON *peer; cJSON_ArrayForEach(peer,peers) {
        if(!strcmp(cJSON_GetObjectItemCaseSensitive(peer,"ip")->valuestring,"192.0.2.20")&&
           cJSON_GetObjectItemCaseSensitive(peer,"source_network")->valuedouble==0) {
            assert(cJSON_GetObjectItemCaseSensitive(peer,"read_requests")->valuedouble==1);
            assert(cJSON_GetObjectItemCaseSensitive(peer,"last_read_service")->valuedouble==12);
            assert(!strcmp(cJSON_GetObjectItemCaseSensitive(peer,"last_result")->valuestring,"send-failed"));
            found=true;
        }
    }
    assert(found);cJSON_Delete(json);
    /* Traffic from more than eight clients cannot grow the diagnostic table. */
    for(unsigned i=30;i<50;++i) {
        char ip[16];snprintf(ip,sizeof(ip),"192.0.2.%u",i);
        assert(gateway_bacnet_process_datagram(read,sizeof(read),inet_addr(ip),47808));
    }
    gateway_bacnet_stats(&stats);json=gateway_bacnet_discovery_json(&stats);assert(json);
    assert(cJSON_GetArraySize(cJSON_GetObjectItemCaseSensitive(json,"peers"))==GATEWAY_BACNET_DISCOVERY_PEERS);
    cJSON_Delete(json);assert(!gateway_bacnet_discovery_json(NULL));
    gateway_bacnet_shutdown();
    puts("PASS local/global/forwarded/routed discovery, requester ports, range and malformed filters");
    return 0;
}
