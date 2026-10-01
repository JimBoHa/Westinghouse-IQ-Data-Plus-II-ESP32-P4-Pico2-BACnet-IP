#include "iq_diagnostics.h"
#include "iq_meter_trace.h"
#include <assert.h>
#include <string.h>
int main(void)
{
    iq_event_ring_t r={0};
    iq_event_append(&r,10,0,"usb","failure",8,"no meter response");
    iq_event_append(&r,20,1800000000000LL,"clock","synchronized",0,"clock ready");
    cJSON *j=iq_event_json(&r),*a=cJSON_GetObjectItem(j,"events");
    assert(cJSON_IsNull(cJSON_GetObjectItem(cJSON_GetArrayItem(a,1),"utc_ms")));
    assert(cJSON_GetObjectItem(cJSON_GetArrayItem(a,0),"utc_ms")->valuedouble==1800000000000LL);
    cJSON_Delete(j);
    char large[512];memset(large,'x',sizeof(large)-1);large[511]=0;
    for(unsigned i=0;i<100;i++)iq_event_append(&r,30+i,0,"test","bounded",1,large);
    assert(r.count==32&&r.total==102);
    j=iq_event_json(&r);a=cJSON_GetObjectItem(j,"events");
    assert(cJSON_GetArraySize(a)==32);
    assert(cJSON_GetObjectItem(cJSON_GetArrayItem(a,0),"sequence")->valueint==102);
    assert(cJSON_GetObjectItem(cJSON_GetArrayItem(a,31),"sequence")->valueint==71);
    assert(strlen(cJSON_GetObjectItem(cJSON_GetArrayItem(a,0),"detail")->valuestring)==159);
    cJSON_Delete(j);
    iq_transaction_ring_t meter={0};iq_stream_t stream;
    iq_stream_init(&stream,IQ_STANDARD,0);
    iq_stream_error(&stream,"No terminal response before bounded command deadline");
    for(unsigned i=0;i<12;i++)iq_transaction_append(&meter,&stream,1000+i,0,5500,false);
    assert(meter.total==12&&meter.count==8);
    j=iq_transaction_ring_json(&meter);a=cJSON_GetObjectItem(j,"entries");
    assert(cJSON_GetArraySize(a)==8);
    assert(cJSON_GetObjectItem(j,"overwritten")->valueint==4);
    assert(cJSON_GetObjectItem(cJSON_GetArrayItem(a,0),"sequence")->valueint==12);
    assert(cJSON_GetObjectItem(cJSON_GetArrayItem(a,7),"sequence")->valueint==5);
    assert(cJSON_IsNull(cJSON_GetObjectItem(cJSON_GetArrayItem(a,0),"utc_ms")));
    cJSON_Delete(j);return 0;
}
