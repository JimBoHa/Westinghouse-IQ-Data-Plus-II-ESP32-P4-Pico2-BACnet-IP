#include "iq_observe.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static const char *status="{\"type\":\"status\",\"active\":false,\"pins\":14,\"sio_output_enables\":0,\"clk_ctrl\":40965,\"rw_ctrl\":40965,\"data_ctrl\":8197,\"int_ctrl\":8197}\n";
static const char *started="{\"type\":\"started\",\"command\":\"observe\",\"request_kind\":\"none\",\"duration_ms\":500,\"payload\":0,\"shift_edge\":\"rising\"}\n";
static const char *timing="{\"type\":\"timing\",\"engine\":\"cpu\",\"clock_hz\":150000000,\"maximum_sample_gap_cycles\":42,\"active_limit_cycles\":100,\"bracketed_sampling\":true}\n";
static bool feed(iq_observe_t *p,const char *line)
{
    /* USB may split a record at any byte. */
    for(size_t i=0;i<strlen(line);i++)if(!iq_observe_feed(p,line+i,1))return false;
    return true;
}
static void begin(iq_observe_t *p)
{ *p=(iq_observe_t){.state=IQ_OBSERVE_RUNNING,.sequence=1};assert(feed(p,status));assert(feed(p,started)); }
static bool result(iq_observe_t *p,unsigned stop,unsigned events)
{
    cJSON *j=cJSON_CreateObject();cJSON_AddStringToObject(j,"type","result");
    for(unsigned i=0;i<IQ_RESULT_COUNT;i++)cJSON_AddNumberToObject(j,iq_result_names[i],
        i==IQ_RESULT_STOP?stop:i==IQ_RESULT_ELAPSED?(stop?123:500000):
        i==IQ_RESULT_EVENTS?events:i==IQ_RESULT_CLOCKS?11:
        i==IQ_RESULT_INITIAL_PINS||i==IQ_RESULT_FINAL_PINS?14:0);
    cJSON_AddBoolToObject(j,"released",true);cJSON_AddBoolToObject(j,"telemetry_validated",false);
    char *text=cJSON_PrintUnformatted(j);cJSON_Delete(j);
    bool ok=feed(p,text)&&feed(p,"\n");free(text);return ok;
}
int main(void)
{
    iq_observe_t p;begin(&p);
    assert(feed(&p,"{\"type\":\"event\",\"event\":\"clock_only_fragment\",\"us\":100,\"clocks\":11,\"word\":2047,\"origin_code\":0,\"data_released_during_write\":null}\n"));
    assert(feed(&p,timing));assert(result(&p,0,1));assert(feed(&p,status));assert(iq_observe_finish(&p));
    cJSON *j=iq_observe_json(&p);assert(cJSON_IsTrue(cJSON_GetObjectItem(j,"full_duration")));
    assert(cJSON_IsFalse(cJSON_GetObjectItem(j,"telemetry_validated")));cJSON_Delete(j);
    assert(!feed(&p,status)); /* Anything after final status invalidates the report. */
    begin(&p);assert(feed(&p,timing));assert(result(&p,8,0));assert(feed(&p,status));assert(iq_observe_finish(&p));
    j=iq_observe_json(&p);assert(cJSON_IsFalse(cJSON_GetObjectItem(j,"full_duration")));cJSON_Delete(j);
    begin(&p);assert(feed(&p,"{\"type\":\"event\",\"event\":\"fault\",\"us\":123,\"clocks\":0,\"word\":0,\"origin_code\":8}\n"));
    assert(feed(&p,timing));assert(result(&p,8,1));assert(feed(&p,status));assert(iq_observe_finish(&p));
    begin(&p);assert(feed(&p,timing));assert(!result(&p,0,1)); /* Missing event. */
    begin(&p);assert(!feed(&p,timing+1)); /* Broken JSON. */
    begin(&p);assert(!feed(&p,"{\"type\":\"timing\",\"type\":\"result\"}\n"));
    begin(&p);assert(!result(&p,0,0)); /* Missing CPU timing record. */
    begin(&p);assert(feed(&p,timing));assert(result(&p,0,0));assert(!iq_observe_finish(&p)); /* No post-status. */
    p=(iq_observe_t){.state=IQ_OBSERVE_RUNNING};assert(!feed(&p,started)); /* No released pre-status. */
    char bad[512];snprintf(bad,sizeof(bad),"%s",status);char *oe=strstr(bad,"enables\":0");oe[9]='1';
    p=(iq_observe_t){.state=IQ_OBSERVE_RUNNING};assert(!feed(&p,bad));assert(p.before.valid);
    p=(iq_observe_t){.state=IQ_OBSERVE_RUNNING};snprintf(bad,sizeof(bad),"%s",status);
    strstr(bad,"40965")[0]='9';assert(!feed(&p,bad)); /* Unexpected GPIO mux/overrides. */
    begin(&p);p.events=4096;assert(!feed(&p,"{\"type\":\"event\",\"event\":\"read_poll\",\"us\":1,\"clocks\":27,\"word\":0,\"origin_code\":0}\n"));
    begin(&p);char huge[IQ_LINE_CAPACITY+1];memset(huge,'x',sizeof(huge));assert(!iq_observe_feed(&p,huge,sizeof(huge)));
    p=(iq_observe_t){0};j=iq_observe_json(&p);
    assert(cJSON_IsNull(cJSON_GetObjectItem(cJSON_GetObjectItem(j,"result"),"clock_rises")));cJSON_Delete(j);
    puts("Passive parser: fragmented input, partial captures, GPIO safety, bounds and malformed streams passed");
    return 0;
}
