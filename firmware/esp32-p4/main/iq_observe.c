/* Input-only Pico diagnostics. These results never enter the meter model. */
#include "iq_observe.h"
#include <math.h>
#include <stdio.h>
#include <string.h>
static const cJSON *field(const cJSON *j,const char *name)
{ return cJSON_GetObjectItemCaseSensitive(j,name); }
static bool string_is(const cJSON *j,const char *name,const char *value)
{ const cJSON *v=field(j,name);return cJSON_IsString(v)&&!strcmp(v->valuestring,value); }
static bool number(const cJSON *j,const char *name,uint32_t *out)
{
    const cJSON *v=field(j,name);
    if(!cJSON_IsNumber(v)||!isfinite(v->valuedouble)||v->valuedouble<0||
       v->valuedouble>UINT32_MAX||floor(v->valuedouble)!=v->valuedouble)return false;
    *out=(uint32_t)v->valuedouble;return true;
}
static bool equals(const cJSON *j,const char *name,uint32_t expected)
{ uint32_t n;return number(j,name,&n)&&n==expected; }
void iq_observe_error(iq_observe_t *p,const char *error)
{
    if(!p->error[0])snprintf(p->error,sizeof(p->error),"%s",error);
    p->state=IQ_OBSERVE_FAILED;
}
static bool status_record(iq_pin_status_t *s,const cJSON *j)
{
    const char *names[]={"clk_ctrl","rw_ctrl","data_ctrl","int_ctrl"};
    if(!cJSON_IsFalse(field(j,"active"))||!number(j,"pins",&s->pins)||s->pins>15||
       !number(j,"sio_output_enables",&s->output_enables))return false;
    for(unsigned i=0;i<4;i++)if(!number(j,names[i],&s->control[i]))return false;
    s->valid=true;
    /* Qualified RP2350 0.4.7: SIO=5, OUTOVER=LOW(2), CLK/RW OEOVER=LOW(2).
     * SDK 2.3.1 RP2350 io_bank0 CTRL: OUTOVER bits 13:12, OEOVER bits 15:14. */
    return !s->output_enables&&s->control[0]==0xa005&&s->control[1]==0xa005&&
        s->control[2]==0x2005&&s->control[3]==0x2005;
}
static bool record(iq_observe_t *p,const cJSON *j)
{
    if(!cJSON_IsObject(j))return false;
    /* The protocol is flat; reject nested values and duplicate keys. */
    for(const cJSON *a=j->child;a;a=a->next) {
        if(cJSON_IsObject(a)||cJSON_IsArray(a))return false;
        for(const cJSON *b=a->next;b;b=b->next)if(!strcmp(a->string,b->string))return false;
    }
    if(string_is(j,"type","status")&&(p->phase==0||p->phase==4)) {
        if(!status_record(p->phase?&p->after:&p->before,j)) {
            iq_observe_error(p,"Pico must be idle with GP0..GP3 released and expected SIO controls");return false;
        }
        ++p->phase;return true;
    }
    if(p->phase==1&&string_is(j,"type","started")&&string_is(j,"command","observe")&&
       string_is(j,"request_kind","none")&&equals(j,"duration_ms",500)&&equals(j,"payload",0)&&
       string_is(j,"shift_edge","rising")) { p->phase=2;return true; }
    if(p->phase==2&&string_is(j,"type","event")) {
        uint32_t us,unused,origin;
        if(p->events>=4096||!number(j,"us",&us)||us<p->last_event_us||us>600000||
           !number(j,"clocks",&unused)||!number(j,"word",&unused)||!number(j,"origin_code",&origin))return false;
        bool known=(string_is(j,"event","read_poll")||string_is(j,"event","clock_only_fragment"))&&origin==0;
        known|=(string_is(j,"event","meter_write_candidate")||string_is(j,"event","empty_write"))&&origin==1;
        known|=string_is(j,"event","fault")&&origin>=1&&origin<=8;
        if(!known)return false;
        ++p->events;p->last_event_us=us;return true;
    }
    if(p->phase==2&&string_is(j,"type","timing")) {
        if(!string_is(j,"engine","cpu")||!cJSON_IsTrue(field(j,"bracketed_sampling"))||
           !number(j,"clock_hz",&p->clock_hz)||!p->clock_hz||
           !number(j,"maximum_sample_gap_cycles",&p->max_gap_cycles)||
           !number(j,"active_limit_cycles",&p->active_limit_cycles))return false;
        p->timing=true;p->phase=3;return true;
    }
    if(p->phase==3&&string_is(j,"type","result")) {
        for(unsigned i=0;i<IQ_RESULT_COUNT;i++)
            if(number(j,iq_result_names[i],&p->result[i]))p->result_present|=1u<<i;
        p->terminal=true;p->released=cJSON_IsTrue(field(j,"released"));
        if(p->result_present!=((1u<<IQ_RESULT_COUNT)-1)||!p->released||
           !cJSON_IsFalse(field(j,"telemetry_validated"))||p->result[IQ_RESULT_EVENTS]!=p->events||
           p->result[IQ_RESULT_STOP]>8||p->result[IQ_RESULT_ELAPSED]>600000||
           p->result[IQ_RESULT_INITIAL_PINS]>15||p->result[IQ_RESULT_FINAL_PINS]>15||
           p->result[IQ_RESULT_REQUESTS]||p->result[IQ_RESULT_COMPLETIONS]||
           p->result[IQ_RESULT_RETRIES_BEFORE]!=p->result[IQ_RESULT_RETRIES_AFTER])return false;
        p->phase=4;return true;
    }
    return false;
}
bool iq_observe_feed(iq_observe_t *p,const void *bytes,size_t length)
{
    const unsigned char *data=bytes;
    for(size_t i=0;i<length&&p->state!=IQ_OBSERVE_FAILED;i++) {
        unsigned char ch=data[i];
        if(ch=='\r')continue;
        if(ch=='\n') {
            p->line[p->used]=0;
            cJSON *j=cJSON_ParseWithOpts(p->line,NULL,true);
            bool ok=j&&record(p,j);cJSON_Delete(j);p->used=0;
            if(!ok)iq_observe_error(p,"Invalid or out-of-order passive observation response");
        } else if(ch<32||ch>126||p->used+1>=sizeof(p->line))
            iq_observe_error(p,"Invalid or oversized passive observation line");
        else p->line[p->used++]=(char)ch;
    }
    return p->state!=IQ_OBSERVE_FAILED;
}
bool iq_observe_finish(iq_observe_t *p)
{
    if(p->phase!=5||p->used||p->state==IQ_OBSERVE_FAILED) {
        iq_observe_error(p,"Incomplete passive observation response");return false;
    }
    p->state=IQ_OBSERVE_COMPLETE;return true;
}
static cJSON *pin_json(const iq_pin_status_t *s)
{
    if(!s->valid)return cJSON_CreateNull();
    cJSON *j=cJSON_CreateObject();
    cJSON_AddNumberToObject(j,"pins",s->pins);cJSON_AddNumberToObject(j,"sio_output_enables",s->output_enables);
    const char *names[]={"clk_ctrl","rw_ctrl","data_ctrl","int_ctrl"};
    for(unsigned i=0;i<4;i++)cJSON_AddNumberToObject(j,names[i],s->control[i]);
    return j;
}
cJSON *iq_observe_json(const iq_observe_t *p)
{
    const char *states[]={"idle","queued","running","complete","failed"};
    cJSON *j=cJSON_CreateObject();
    cJSON_AddStringToObject(j,"state",states[p->state]);cJSON_AddNumberToObject(j,"sequence",p->sequence);
    cJSON_AddNumberToObject(j,"queued_uptime_ms",p->queued_ms);cJSON_AddNumberToObject(j,"finished_uptime_ms",p->finished_ms);
    cJSON_AddNumberToObject(j,"duration_ms",500);cJSON_AddStringToObject(j,"error",p->error);
    cJSON_AddStringToObject(j,"clock_count_scope","CPU sampled rising edges; short pulses can be missed");
    cJSON_AddBoolToObject(j,"telemetry_validated",false);
    cJSON_AddBoolToObject(j,"full_duration",p->state==IQ_OBSERVE_COMPLETE&&p->result[IQ_RESULT_STOP]==0&&p->result[IQ_RESULT_ELAPSED]>=500000);
    cJSON_AddItemToObject(j,"before",pin_json(&p->before));cJSON_AddItemToObject(j,"after",pin_json(&p->after));
    cJSON *timing=cJSON_AddObjectToObject(j,"timing");
    if(p->timing) {
        cJSON_AddNumberToObject(timing,"clock_hz",p->clock_hz);
        cJSON_AddNumberToObject(timing,"maximum_sample_gap_cycles",p->max_gap_cycles);
        cJSON_AddNumberToObject(timing,"maximum_sample_gap_us",p->max_gap_cycles*1000000.0/p->clock_hz);
        cJSON_AddNumberToObject(timing,"active_limit_cycles",p->active_limit_cycles);
    }
    cJSON *result=cJSON_AddObjectToObject(j,"result");
    for(unsigned i=0;i<IQ_RESULT_COUNT;i++) {
        if(p->result_present&(1u<<i))cJSON_AddNumberToObject(result,iq_result_names[i],p->result[i]);
        else cJSON_AddNullToObject(result,iq_result_names[i]);
    }
    if(p->terminal)cJSON_AddBoolToObject(result,"released",p->released);
    else cJSON_AddNullToObject(result,"released");
    return j;
}
