/* Native-only test driver. Never linked into production firmware. */
#include "iq_model.h"
#include "iq_transport.h"
#include "iq_meter_trace.h"
#include "iq_config.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static double num(const cJSON *j,const char *key,double fallback)
{ const cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key); return cJSON_IsNumber(v)?v->valuedouble:fallback; }
static const char *str(const cJSON *j,const char *key)
{ const cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key); return cJSON_IsString(v)?v->valuestring:""; }
static iq_kind_t kind(const cJSON *j)
{
    for(unsigned k=0;k<IQ_KIND_COUNT;++k) if(!strcmp(str(j,"kind"),iq_kind_name(k))) return k;
    return IQ_KIND_COUNT;
}
static void values(cJSON *j,const iq_value_t *v)
{
    cJSON *r=cJSON_AddObjectToObject(j,"readings");
    for(unsigned i=0;i<IQ_POINT_COUNT;++i) {
        cJSON *p=cJSON_AddObjectToObject(r,iq_points[i].key);
        cJSON_AddBoolToObject(p,"valid",v[i].valid);
        if(v[i].valid) cJSON_AddNumberToObject(p,"value",v[i].value);
        else cJSON_AddNullToObject(p,"value");
    }
}
int main(void)
{
    iq_model_t *m=iq_model_create();
    if(!m) return 2;
    char *line=NULL; size_t capacity=0;
    while(getline(&line,&capacity,stdin)>0) {
        cJSON *j=cJSON_Parse(line),*out=cJSON_CreateObject();
        const char *op=str(j,"op");
        uint64_t now=(uint64_t)num(j,"now",0);
        iq_kind_t k=kind(j);
        uint32_t words[19]={0}; unsigned count=0;
        const cJSON *w=cJSON_GetObjectItemCaseSensitive(j,"words");
        for(const cJSON *v=w?w->child:NULL;v&&count<19;v=v->next) words[count++]=(uint32_t)v->valuedouble;
        iq_value_t v[IQ_POINT_COUNT]={0};
        if(!strcmp(op,"decode")) {
            cJSON_AddBoolToObject(out,"ok",iq_decode(k,words,count,v)); values(out,v);
        } else if(!strcmp(op,"impacc")) {
            double x=0; bool ok=iq_impacc((uint32_t)num(j,"payload",0),&x);
            cJSON_AddBoolToObject(out,"ok",ok);
            if(ok)cJSON_AddNumberToObject(out,"value",x);
        } else if(!strcmp(op,"reset")) {
            iq_model_destroy(m); m=iq_model_create(); if(!m) return 2;
        } else if(!strcmp(op,"accept")) {
            cJSON_AddBoolToObject(out,"ok",iq_model_accept(m,k,words,count,now,.5,0,0));
            iq_model_snapshot(m,now,v); values(out,v);
        } else if(!strcmp(op,"fail")) {
            iq_model_fail(m,k,now,.5,(int)num(j,"stop",-1),0);
            iq_model_snapshot(m,now,v); values(out,v);
        } else if(!strcmp(op,"snapshot")) {
            iq_model_snapshot(m,now,v); values(out,v);
        } else if(!strcmp(op,"stream")) {
            iq_stream_t s; iq_stream_init(&s,k,(uint16_t)num(j,"address",0));
            const char *text=str(j,"text"); size_t n=strlen(text),chunk=(size_t)num(j,"chunk",1);
            if(!chunk) chunk=1;
            for(size_t i=0;i<n;i+=chunk) iq_stream_feed(&s,text+i,n-i<chunk?n-i:chunk);
            if(*str(j,"transport_error")) iq_stream_error(&s,str(j,"transport_error"));
            cJSON_AddBoolToObject(out,"ok",iq_stream_finish(&s));
            cJSON_AddStringToObject(out,"error",s.error);
            cJSON_AddNumberToObject(out,"stop",s.stop);
            iq_transaction_trace_t trace={.stream=s,.accepted=!s.failed&&s.terminal};
            cJSON_AddItemToObject(out,"diagnostics",iq_transaction_json(&trace));
            cJSON *a=cJSON_AddArrayToObject(out,"words");
            for(unsigned i=0;i<s.word_count;++i)cJSON_AddItemToArray(a,cJSON_CreateNumber(s.words[i]));
        } else if(!strcmp(op,"config")) {
            iq_config_t s; char error[192]={0};
            bool ok=iq_config_parse(str(j,"text"),&s,error,sizeof(error));
            cJSON_AddBoolToObject(out,"ok",ok); cJSON_AddStringToObject(out,"error",error);
            if(ok)cJSON_AddItemToObject(out,"settings",iq_config_json(&s));
        } else cJSON_AddStringToObject(out,"error","Unknown operation");
        char *text=cJSON_PrintUnformatted(out); puts(text); fflush(stdout);
        free(text);cJSON_Delete(out);cJSON_Delete(j);
    }
    free(line);iq_model_destroy(m); return 0;
}
