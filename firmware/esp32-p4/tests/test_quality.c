#include "iq_model.h"
#include "gateway_bacnet.h"
#include "bip_port.h"
#include "bacnet/basic/object/ai.h"
#include "bacnet/basic/object/bi.h"
#include <arpa/inet.h>
#include <assert.h>
#include <math.h>
#include <stdio.h>

static int discard(const BACNET_IP_ADDRESS *to,const uint8_t *data,uint16_t size,void *context)
{ (void)to;(void)data;(void)context;return size; }
static void sample(iq_model_t *m,uint64_t now,bool zero)
{
    uint32_t words[]={0x7e9,0x400000,0x400000,0x400000,0,
        0x400350,0x400350,0x400350,0x400324,0x400324,0x400324,
        0x400000,0x400000,0x400000,0x400000,0x400000,0x400001,1000};
    if(!zero) { words[1]=words[2]=words[3]=0x400064;words[11]=0x400064;words[14]=0x40003c; }
    for(unsigned i=0;i<18;i++)words[i]<<=1;
    assert(iq_model_accept(m,IQ_STANDARD,words,18,now,.5,0,0));
}
static void check(const iq_value_t *v,iq_key_t key,iq_quality_t quality,unsigned reliability)
{
    assert(iq_value_quality(&v[key])==quality);
    const iq_point_def_t *p=&iq_points[key];
    assert((p->object_type==0?Analog_Input_Reliability(p->instance):
        Binary_Input_Reliability(p->instance))==reliability);
}
int main(void)
{
    bip_port_set_send_hook(discard,NULL);
    gateway_bacnet_config_t config={.device_instance=4000001,.device_name="Quality-Test",.firmware_version="test",
        .local_ip=inet_addr("127.0.0.2"),.netmask=inet_addr("255.0.0.0"),.udp_port=47865};
    assert(gateway_bacnet_init(&config,1000));
    iq_model_t *m=iq_model_create();assert(m);iq_value_t v[IQ_POINT_COUNT];
    iq_model_snapshot(m,1000,v);gateway_bacnet_update(v);
    check(v,IQ_VAB,IQ_QUALITY_NO_DATA,RELIABILITY_COMMUNICATION_FAILURE);
    uint32_t settings[]={4,0,12};
    assert(iq_model_accept(m,IQ_SETTINGS,settings,3,1000,.5,0,0));
    sample(m,1000,true);iq_model_snapshot(m,1000,v);gateway_bacnet_update(v);
    check(v,IQ_VAB,IQ_QUALITY_GOOD,RELIABILITY_NO_FAULT_DETECTED);
    assert(v[IQ_VAB].value==848&&v[IQ_IA].valid&&v[IQ_IA].value==0);
    assert(v[IQ_FREQUENCY_Hz].valid&&v[IQ_FREQUENCY_Hz].value==0);
    check(v,IQ_PF_PQ_magnitude,IQ_QUALITY_ZERO_LOAD,RELIABILITY_UNRELIABLE_OTHER);
    check(v,IQ_CURRENT_IMBALANCE_pct,IQ_QUALITY_ZERO_LOAD,RELIABILITY_UNRELIABLE_OTHER);
    check(v,IQ_HIGHEST_CURRENT_PHASE,IQ_QUALITY_ZERO_LOAD,RELIABILITY_UNRELIABLE_OTHER);
    check(v,IQ_FREQUENCY_EXCURSION_ACTIVE,IQ_QUALITY_METER_VALUE,RELIABILITY_PROCESS_ERROR);
    check(v,IQ_ROLLING_15MIN_DEMAND_kW,IQ_QUALITY_HISTORY,RELIABILITY_UNRELIABLE_OTHER);
    for(unsigned i=1;i<=900;i++)sample(m,1000+i*1000,true);
    iq_model_snapshot(m,901000,v);gateway_bacnet_update(v);
    check(v,IQ_ROLLING_15MIN_LOAD_FACTOR_pct,IQ_QUALITY_ZERO_LOAD,RELIABILITY_UNRELIABLE_OTHER);
    assert(v[IQ_ROLLING_15MIN_DEMAND_kW].valid);
    iq_model_snapshot(m,906001,v);gateway_bacnet_update(v);
    check(v,IQ_CURRENT_IMBALANCE_pct,IQ_QUALITY_STALE,RELIABILITY_COMMUNICATION_FAILURE);
    iq_model_fail(m,IQ_STANDARD,907000,.5,8,0);
    iq_model_snapshot(m,907000,v);gateway_bacnet_update(v);
    check(v,IQ_FREQUENCY_EXCURSION_ACTIVE,IQ_QUALITY_COMMUNICATION,RELIABILITY_COMMUNICATION_FAILURE);
    sample(m,908000,false);iq_model_snapshot(m,908000,v);gateway_bacnet_update(v);
    check(v,IQ_CURRENT_IMBALANCE_pct,IQ_QUALITY_GOOD,RELIABILITY_NO_FAULT_DETECTED);
    check(v,IQ_FREQUENCY_EXCURSION_ACTIVE,IQ_QUALITY_GOOD,RELIABILITY_NO_FAULT_DETECTED);
    check(v,IQ_ROLLING_15MIN_DEMAND_kW,IQ_QUALITY_HISTORY,RELIABILITY_UNRELIABLE_OTHER);
    iq_model_fail(m,IQ_SETTINGS,908100,.5,8,0);sample(m,909000,false);
    iq_model_snapshot(m,909000,v);gateway_bacnet_update(v);
    check(v,IQ_NOMINAL_VOLTAGE_V,IQ_QUALITY_SETTINGS,RELIABILITY_UNRELIABLE_OTHER);
    check(v,IQ_CONFIG_NOMINAL_LL_V,IQ_QUALITY_COMMUNICATION,RELIABILITY_COMMUNICATION_FAILURE);
    /* Unqualified trip snapshots are absent, not a broken connection. */
    uint32_t trip[18]={34,0};for(unsigned i=2;i<18;i++)trip[i]=0x800000;
    assert(iq_model_accept(m,IQ_TRIP,trip,18,909000,.5,0,0));
    iq_model_snapshot(m,909000,v);gateway_bacnet_update(v);
    check(v,IQ_TRIP_IA,IQ_QUALITY_NO_TRIP,RELIABILITY_UNRELIABLE_OTHER);
    iq_model_destroy(m);gateway_bacnet_shutdown();
    puts("PASS zero-load, raw-value preservation, warmup, stale/failure precedence, recovery and BACnet reliability");
}
