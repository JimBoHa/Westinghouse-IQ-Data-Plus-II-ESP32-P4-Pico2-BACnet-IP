#include "iq_meter_trace.h"
#include <string.h>

void iq_transaction_append(iq_transaction_ring_t *r,const iq_stream_t *s,
    uint64_t uptime_ms,int64_t utc_ms,uint64_t host_elapsed_ms,bool accepted)
{
    iq_transaction_trace_t *t=&r->entries[r->next];
    t->stream=*s;
    /* Preserve counts and bounded wire events, not the parser's scratch buffers. */
    memset(t->stream.line,0,sizeof(t->stream.line));
    memset(t->stream.words,0,sizeof(t->stream.words));
    t->sequence=++r->total;t->uptime_ms=uptime_ms;t->utc_ms=utc_ms;
    t->host_elapsed_ms=host_elapsed_ms;t->accepted=accepted;
    r->next=(r->next+1)%IQ_TRANSACTION_CAPACITY;
    if(r->count<IQ_TRANSACTION_CAPACITY)++r->count;
}

static const char *stop_name(int stop)
{
    const char *const names[]={"deadline","abort","log_full","int_could_not_pull_low",
        "data_could_not_pull_low","rw_clock","bad_write_length","loop_gap","ambiguous"};
    return stop>=0&&stop<(int)(sizeof(names)/sizeof(names[0]))?names[stop]:"unknown";
}
static const char *progress(const iq_stream_t *s)
{
    if(s->word_count)return "meter_data_received";
    if(s->handshake_completed)return "repeat_handshake_completed";
    if(s->handshake)return "repeat_control_received";
    if(s->completed_requests)return "request_clocked";
    if(s->requests)return "request_presented";
    return s->started?"command_started":"no_start_record";
}
static void nullable_bool(cJSON *j,const char *name,int value)
{
    if(value<0)cJSON_AddNullToObject(j,name);else cJSON_AddBoolToObject(j,name,value!=0);
}
static void pins(cJSON *j,const char *name,const iq_stream_t *s,unsigned index)
{
    cJSON *v=cJSON_AddObjectToObject(j,name);
    const char *const names[]={"CLK_GP0","RW_GP1","DATA_GP2","INT_GP3"};
    for(unsigned i=0;i<4;i++)
        nullable_bool(v,names[i],(s->result_present&(1u<<index))&&s->result[index]<=15?
            (int)((s->result[index]>>i)&1):-1);
}
cJSON *iq_transaction_json(const iq_transaction_trace_t *t)
{
    const iq_stream_t *s=&t->stream;
    cJSON *j=cJSON_CreateObject();if(!j)return NULL;
    cJSON_AddNumberToObject(j,"sequence",t->sequence);cJSON_AddNumberToObject(j,"uptime_ms",t->uptime_ms);
    if(t->utc_ms)cJSON_AddNumberToObject(j,"utc_ms",t->utc_ms);else cJSON_AddNullToObject(j,"utc_ms");
    cJSON_AddStringToObject(j,"request_kind",iq_kind_name(s->kind));
    cJSON_AddNumberToObject(j,"meter_address",s->address);cJSON_AddNumberToObject(j,"duration_ms",500);
    cJSON_AddNumberToObject(j,"host_elapsed_ms",t->host_elapsed_ms);
    cJSON_AddStringToObject(j,"outcome",t->accepted?"accepted":s->failed?"stream_rejected":"decode_rejected");
    cJSON_AddBoolToObject(j,"buffer_accepted",t->accepted);
    cJSON_AddStringToObject(j,"error",s->failed?s->error:t->accepted?"":"Invalid meter DATA buffer");
    cJSON_AddStringToObject(j,"progress",progress(s));
    cJSON_AddBoolToObject(j,"start_received",s->started);cJSON_AddBoolToObject(j,"terminal_received",s->terminal);
    cJSON_AddBoolToObject(j,"terminal_checks_evaluated",s->terminal);
    cJSON *checks=cJSON_AddArrayToObject(j,"failed_checks");
    for(unsigned i=0;i<IQ_CHECK_COUNT;i++)if(s->failed_checks&(1u<<i))
        cJSON_AddItemToArray(checks,cJSON_CreateString(iq_check_names[i]));
    cJSON *host=cJSON_AddObjectToObject(j,"host_observed");
    cJSON_AddNumberToObject(host,"bytes_parsed",s->bytes_received);
    cJSON_AddNumberToObject(host,"records_parsed",s->records_received);
    cJSON_AddNumberToObject(host,"partial_record_bytes",s->used);
    cJSON_AddNumberToObject(host,"events",s->events);cJSON_AddNumberToObject(host,"requests",s->requests);
    cJSON_AddNumberToObject(host,"completed_requests",s->completed_requests);
    cJSON_AddNumberToObject(host,"writes",s->writes);cJSON_AddNumberToObject(host,"completions",s->completions);
    cJSON_AddNumberToObject(host,"completed_completions",s->completed_completions);
    cJSON_AddNumberToObject(host,"active_origin",s->active_origin);cJSON_AddNumberToObject(host,"data_words",s->word_count);
    cJSON_AddBoolToObject(host,"repeat_control_received",s->handshake);
    cJSON_AddBoolToObject(host,"repeat_handshake_completed",s->handshake_completed);
    cJSON *result=cJSON_AddObjectToObject(j,"pico_reported");
    for(unsigned i=0;i<IQ_RESULT_COUNT;i++) {
        if(s->result_present&(1u<<i))cJSON_AddNumberToObject(result,iq_result_names[i],s->result[i]);
        else cJSON_AddNullToObject(result,iq_result_names[i]);
    }
    nullable_bool(result,"released",s->released);cJSON_AddStringToObject(result,"stop_name",stop_name(s->stop));
    cJSON *levels=cJSON_AddObjectToObject(j,"pin_levels");
    pins(levels,"initial",s,IQ_RESULT_INITIAL_PINS);pins(levels,"final",s,IQ_RESULT_FINAL_PINS);
    cJSON *timing=cJSON_AddObjectToObject(j,"timing");
    if(s->timing_pio)cJSON_AddStringToObject(timing,"engine","pio");else cJSON_AddNullToObject(timing,"engine");
    if(s->clock_hz)cJSON_AddNumberToObject(timing,"clock_hz",s->clock_hz);else cJSON_AddNullToObject(timing,"clock_hz");
    cJSON *pio=cJSON_AddObjectToObject(j,"pio_diagnostics");
    for(unsigned i=0;i<IQ_PIO_DIAGNOSTIC_COUNT;i++) {
        if(s->pio_diagnostics_present&(UINT64_C(1)<<i))cJSON_AddNumberToObject(pio,iq_pio_diagnostic_names[i],s->pio_diagnostics[i]);
        else cJSON_AddNullToObject(pio,iq_pio_diagnostic_names[i]);
    }
    cJSON *trace=cJSON_AddObjectToObject(j,"event_trace"),*events=cJSON_AddArrayToObject(trace,"events");
    cJSON_AddNumberToObject(trace,"capacity",IQ_TRACE_EVENT_CAPACITY);
    cJSON_AddNumberToObject(trace,"total",s->trace_total);
    cJSON_AddNumberToObject(trace,"omitted",s->trace_total-s->trace_count);
    for(unsigned i=0;i<s->trace_count;i++) {
        unsigned index=(s->trace_next+IQ_TRACE_EVENT_CAPACITY-s->trace_count+i)%IQ_TRACE_EVENT_CAPACITY;
        const iq_trace_event_t *e=&s->trace[index];
        cJSON *v=cJSON_CreateObject();cJSON_AddItemToArray(events,v);
        cJSON_AddNumberToObject(v,"sequence",s->trace_total-s->trace_count+i+1);
        cJSON_AddStringToObject(v,"event",iq_trace_names[e->kind]);cJSON_AddNumberToObject(v,"us",e->us);
        cJSON_AddNumberToObject(v,"origin_code",e->origin);cJSON_AddNumberToObject(v,"clocks",e->clocks);
        cJSON_AddNumberToObject(v,"word",e->word);nullable_bool(v,"data_released_during_write",e->data_released);
    }
    return j;
}
cJSON *iq_transaction_ring_json(const iq_transaction_ring_t *r)
{
    cJSON *j=cJSON_CreateObject();if(!j)return NULL;
    cJSON_AddNumberToObject(j,"schema",1);cJSON_AddNumberToObject(j,"capacity",IQ_TRANSACTION_CAPACITY);
    cJSON_AddNumberToObject(j,"total",r->total);cJSON_AddNumberToObject(j,"overwritten",r->total-r->count);
    cJSON_AddStringToObject(j,"retention","RAM, current boot only; newest transactions first");
    cJSON_AddStringToObject(j,"clock_count_scope","completed_program_shifts_and_captured_writes; not all physical clock edges");
    cJSON_AddStringToObject(j,"event_scope","CPU service timestamps; request/read/completion words are presented images, not sampled replies");
    cJSON_AddStringToObject(j,"pin_scope","Initial/final digital levels only; not voltage measurements or a signal trace");
    cJSON_AddStringToObject(j,"stop_scope","stop_code 0 means deadline reached, not successful communication");
    cJSON *entries=cJSON_AddArrayToObject(j,"entries");
    for(unsigned i=0;i<r->count;i++) {
        const iq_transaction_trace_t *t=&r->entries[(r->next+IQ_TRANSACTION_CAPACITY-1-i)%IQ_TRANSACTION_CAPACITY];
        cJSON *entry=iq_transaction_json(t);
        if(!entry){cJSON_Delete(j);return NULL;}cJSON_AddItemToArray(entries,entry);
    }
    return j;
}
