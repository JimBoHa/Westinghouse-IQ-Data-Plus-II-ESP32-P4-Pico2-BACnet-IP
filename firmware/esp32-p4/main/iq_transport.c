/* Streaming validation follows live/poll_meter.py:extract_meter_words.
 * Request/completion images can never enter the measurement decoder. */
#include "iq_transport.h"
#include "cJSON.h"
#include <math.h>
#include <stdio.h>
#include <string.h>

const char *const iq_result_names[IQ_RESULT_COUNT]={
    "stop_code","elapsed_us","clock_rises","rw_falls","rw_rises","data_edges",
    "int_edges","max_loop_us","late_loops","events","valid_write_lengths",
    "malformed_writes","requests","completions","initial_pins","final_pins",
    "startup_retries_before","startup_retries_after"
};
const char *const iq_check_names[IQ_CHECK_COUNT]={
    "firmware_stop","malformed_writes","outputs_not_released","event_total_mismatch",
    "request_total_mismatch","completion_total_mismatch","write_total_mismatch",
    "request_not_clocked_completely","write_without_completion",
    "completion_not_clocked_completely","image_still_active","no_meter_data_words"
};
const char *const iq_trace_names[IQ_TRACE_COUNT]={
    "unknown","request_presented","completion_presented","read_poll",
    "clock_only_fragment","empty_write","meter_write_candidate","fault"
};
const char *const iq_pio_diagnostic_names[IQ_PIO_DIAGNOSTIC_COUNT]={
#define IQ_PIO_NAME(symbol,name) #name,
    IQ_PIO_DIAGNOSTICS(IQ_PIO_NAME)
#undef IQ_PIO_NAME
};

static const cJSON *field(const cJSON *j,const char *key)
{ return cJSON_GetObjectItemCaseSensitive(j,key); }
static bool string_is(const cJSON *j,const char *key,const char *value)
{ const cJSON *v=field(j,key); return cJSON_IsString(v)&&!strcmp(v->valuestring,value); }
static bool number(const cJSON *j,const char *key,uint32_t *out)
{
    const cJSON *v=field(j,key);
    if(!cJSON_IsNumber(v)||!isfinite(v->valuedouble)||v->valuedouble<0||
       v->valuedouble>UINT32_MAX||floor(v->valuedouble)!=v->valuedouble) return false;
    *out=(uint32_t)v->valuedouble; return true;
}
static bool is_number(const cJSON *j,const char *key,uint32_t expected)
{ uint32_t v; return number(j,key,&v)&&v==expected; }
static bool unique(const cJSON *j,unsigned depth)
{
    if(depth>12) return false;
    for(const cJSON *a=j->child;a;a=a->next) {
        if(cJSON_IsObject(j)) for(const cJSON *b=a->next;b;b=b->next)
            if(!a->string||!b->string||!strcmp(a->string,b->string)) return false;
        if((cJSON_IsArray(a)||cJSON_IsObject(a))&&!unique(a,depth+1)) return false;
    }
    return true;
}
void iq_stream_error(iq_stream_t *s,const char *error)
{
    if(!s->failed) snprintf(s->error,sizeof(s->error),"%s",error);
    s->failed=true;
}
void iq_stream_init(iq_stream_t *s,iq_kind_t kind,uint16_t address)
{
    memset(s,0,sizeof(*s)); s->kind=kind; s->address=address; s->stop=-1;s->released=-1;
    s->payload=iq_request_payload(kind,address); s->image=5|(s->payload<<3);
    if(s->payload==UINT32_MAX) iq_stream_error(s,"Invalid allowlisted read request");
}

static void capture_event(iq_stream_t *s,const cJSON *j)
{
    iq_trace_event_t e={.data_released=-1};
    if(!number(j,"us",&e.us)||!number(j,"origin_code",&e.origin)||
       !number(j,"clocks",&e.clocks)||!number(j,"word",&e.word))return;
    for(unsigned i=1;i<IQ_TRACE_COUNT;i++)if(string_is(j,"event",iq_trace_names[i]))e.kind=i;
    const cJSON *released=field(j,"data_released_during_write");
    if(cJSON_IsBool(released))e.data_released=cJSON_IsTrue(released);
    s->trace[s->trace_next]=e;s->trace_next=(s->trace_next+1)%IQ_TRACE_EVENT_CAPACITY;
    if(s->trace_count<IQ_TRACE_EVENT_CAPACITY)++s->trace_count;
    ++s->trace_total;
}

static bool record(iq_stream_t *s,const cJSON *j)
{
    if(!cJSON_IsObject(j)||!unique(j,0)) { iq_stream_error(s,"Malformed or duplicate JSON fields"); return false; }
    if(s->terminal) { iq_stream_error(s,"Record after terminal response"); return false; }
    if(string_is(j,"type","error")) {
        const cJSON *v=field(j,"error");
        iq_stream_error(s,cJSON_IsString(v)?v->valuestring:"Pico returned an error record"); return false;
    }
    if(string_is(j,"type","started")) {
        char request[40]; snprintf(request,sizeof(request),"%s_repeat",iq_kind_name(s->kind));
        if(s->started||!string_is(j,"command","transact")||!string_is(j,"request_kind",request)||
           !is_number(j,"payload",s->payload)||!is_number(j,"duration_ms",500)||
           !string_is(j,"shift_edge","rising")) {
            iq_stream_error(s,"Firmware start does not match bounded read"); return false;
        }
        s->started=true; return true;
    }
    if(!s->started) { iq_stream_error(s,"Response outside start/result boundary"); return false; }
    if(string_is(j,"type","pio_diagnostics")&&is_number(j,"schema",1)) {
        for(unsigned i=0;i<IQ_PIO_DIAGNOSTIC_COUNT;i++) {
            if(!field(j,iq_pio_diagnostic_names[i]))continue;
            if((s->pio_diagnostics_present&(UINT64_C(1)<<i))||!number(j,iq_pio_diagnostic_names[i],&s->pio_diagnostics[i])) {
                iq_stream_error(s,"Invalid or duplicate PIO diagnostic field");return false;
            }
            s->pio_diagnostics_present|=UINT64_C(1)<<i;
        }
        return true;
    }
    if(string_is(j,"type","timing")) {
        s->timing_pio=string_is(j,"engine","pio");(void)number(j,"clock_hz",&s->clock_hz);
        return true;
    }
    if(string_is(j,"type","result")) {
        for(unsigned i=0;i<IQ_RESULT_COUNT;i++)
            if(number(j,iq_result_names[i],&s->result[i]))s->result_present|=1u<<i;
        const cJSON *released=field(j,"released");
        if(cJSON_IsBool(released))s->released=cJSON_IsTrue(released);
        uint32_t stop;
        if(number(j,"stop_code",&stop)&&stop<=INT32_MAX) s->stop=(int)stop;
        (void)number(j,"malformed_writes",&s->malformed);
        s->terminal=true;
        const bool checks[IQ_CHECK_COUNT]={
            s->stop==0,is_number(j,"malformed_writes",0),cJSON_IsTrue(released),
            is_number(j,"events",s->events),is_number(j,"requests",s->requests),
            is_number(j,"completions",s->completions),is_number(j,"valid_write_lengths",s->writes),
            s->completed_requests==s->requests,s->writes==s->completions,
            s->completed_completions==s->completions,s->active_origin==0,s->word_count!=0
        };
        for(unsigned i=0;i<IQ_CHECK_COUNT;i++)if(!checks[i])s->failed_checks|=1u<<i;
        if(s->stop!=0||!is_number(j,"malformed_writes",0)||!cJSON_IsTrue(field(j,"released"))) {
            const cJSON *released=field(j,"released");
            const char *state=cJSON_IsTrue(released)?"true":cJSON_IsFalse(released)?"false":"unknown";
            char error[128]; snprintf(error,sizeof(error),"Firmware stop_code=%d malformed_writes=%lu released=%s",s->stop,(unsigned long)s->malformed,state);
            iq_stream_error(s,error); return false;
        }
        if(s->failed_checks) {
            unsigned first=0;while(!(s->failed_checks&(1u<<first)))++first;
            char error[192];snprintf(error,sizeof(error),"Meter response rejected: %s (see meter_transactions)",iq_check_names[first]);
            iq_stream_error(s,error);return false;
        }
        return true;
    }
    if(!string_is(j,"type","event")) { iq_stream_error(s,"Unexpected USB record type"); return false; }
    capture_event(s,j);
    uint32_t at,origin,clocks,word;
    if(++s->events>4096||!number(j,"us",&at)||at>600000||
       (s->have_time&&at<s->last_us)||!number(j,"origin_code",&origin)||
       !number(j,"clocks",&clocks)||!number(j,"word",&word)) {
        iq_stream_error(s,"Invalid event fields, limit or nonmonotonic time"); return false;
    }
    s->have_time=true; s->last_us=at;
    if(string_is(j,"event","request_presented")) {
        if(++s->requests>2||s->active_origin||origin!=1||clocks!=27||word!=s->image||
           (s->requests==2&&(!s->handshake_completed||s->word_count||at-s->completion_us<20000))) {
            iq_stream_error(s,"Invalid request image or repeat handshake/backoff"); return false;
        }
        s->active_origin=1; s->active_word=word; s->active_clocks=27;
    } else if(string_is(j,"event","completion_presented")) {
        if(origin!=2||clocks!=1||word||s->active_origin||s->completions>=s->writes) {
            iq_stream_error(s,"Unexpected completion image"); return false;
        }
        ++s->completions; s->active_origin=2; s->active_word=0; s->active_clocks=1;
    } else if(string_is(j,"event","read_poll")) {
        if(!s->active_origin||origin!=s->active_origin||word!=s->active_word||clocks!=s->active_clocks) {
            iq_stream_error(s,"Completed image has no matching presentation"); return false;
        }
        s->active_origin=0;
        if(origin==1) ++s->completed_requests;
        else if(origin==2) {
            ++s->completed_completions;
            if(s->handshake&&!s->handshake_completed) { s->handshake_completed=true; s->completion_us=at; }
        }
    } else if(string_is(j,"event","clock_only_fragment")) {
        if(origin!=2||s->active_origin!=2) { iq_stream_error(s,"Request or unowned read fragment"); return false; }
        s->active_origin=0;
    } else if(string_is(j,"event","empty_write")) {
        if(origin!=3||clocks||word) { iq_stream_error(s,"Malformed empty write"); return false; }
    } else if(string_is(j,"event","meter_write_candidate")) {
        if(origin!=3||clocks!=25||word>0x1ffffff||!cJSON_IsTrue(field(j,"data_released_during_write"))||
           s->completed_requests!=s->requests||!s->requests) {
            iq_stream_error(s,"Meter write lacks request/ownership provenance"); return false;
        }
        ++s->writes;
        if(word&1) {
            if(word!=0x400027||s->handshake||s->word_count||s->requests!=1) {
                iq_stream_error(s,"Unexpected control reply"); return false;
            }
            s->handshake=true;
        } else {
            if((s->handshake&&s->requests!=2)||s->word_count>=18) {
                iq_stream_error(s,"DATA before repeat or too many measurement words"); return false;
            }
            s->words[s->word_count++]=word;
        }
    } else { iq_stream_error(s,"Unexpected or fault event"); return false; }
    return true;
}

bool iq_stream_feed(iq_stream_t *s,const void *bytes,size_t length)
{
    const unsigned char *data=bytes;
    for(size_t i=0;i<length&&!s->failed;++i) {
        ++s->bytes_received;
        unsigned ch=data[i];
        if(ch=='\r') continue;
        if(ch=='\n') {
            ++s->records_received;
            s->line[s->used]=0;
            cJSON *j=cJSON_ParseWithLengthOpts(s->line,s->used+1,NULL,true);
            if(!j) iq_stream_error(s,"Malformed serial JSON");
            else { (void)record(s,j); cJSON_Delete(j); }
            s->used=0;
        } else if(ch<32||ch>126||s->used+1>=sizeof(s->line))
            iq_stream_error(s,"Non-ASCII or oversized serial record");
        else s->line[s->used++]=(char)ch;
    }
    return !s->failed;
}

bool iq_stream_finish(iq_stream_t *s)
{
    if(!s->failed&&(!s->terminal||s->used))
        iq_stream_error(s,s->used?"Partial USB response":"No terminal response before bounded command deadline");
    return !s->failed&&s->terminal;
}

bool iq_pico_identity(const char *line,char *version,size_t size)
{
    cJSON *j=cJSON_ParseWithLengthOpts(line,strlen(line)+1,NULL,true);
    bool ok=j&&unique(j,0)&&string_is(j,"type","info")&&
        string_is(j,"firmware","iqdata-pico-live")&&(string_is(j,"version","0.4.7")||string_is(j,"version","0.4.8"))&&
        string_is(j,"build_board","pico2")&&cJSON_IsTrue(field(j,"live_enabled"))&&
        cJSON_IsFalse(field(j,"synthetic_host"))&&string_is(j,"drive","low_or_release");
    const cJSON *pins=field(j,"pins");
    ok=ok&&is_number(pins,"CLK",0)&&is_number(pins,"RW",1)&&is_number(pins,"DATA",2)&&is_number(pins,"INT",3);
    if(ok&&version&&size) snprintf(version,size,"Pico %s / pico2",field(j,"version")->valuestring);
    cJSON_Delete(j); return ok;
}
