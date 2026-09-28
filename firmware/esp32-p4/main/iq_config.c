#include "iq_config.h"
#include "iq_points.h"
#include <math.h>
#include <stdio.h>
#include <string.h>
#ifdef ESP_PLATFORM
#include "lwip/inet.h"
#include "lwip/sockets.h"
#else
#include <arpa/inet.h>
#endif

void iq_config_defaults(iq_config_t *s)
{
    memset(s,0,sizeof(*s)); s->schema=1; s->dhcp=true; s->bacnet_port=47808;
    snprintf(s->name,sizeof(s->name),"IQData-Uncommissioned");
}
static bool bad(char *error,size_t size,const char *text)
{ if(error&&size) snprintf(error,size,"%s",text); return false; }
static bool ip_number(const char *text,uint32_t *value)
{
    struct in_addr a;
    if(inet_pton(AF_INET,text,&a)!=1) return false;
    *value=ntohl(a.s_addr); return true;
}
bool iq_config_valid(const iq_config_t *s,char *error,size_t size)
{
    if(s->schema!=1||!s->commissioned||s->device_instance>=4194303||s->device_instance==75151)
        return bad(error,size,"Choose unique Device instance 0..4194302; 75151 is protected");
    size_t n=strnlen(s->name,sizeof(s->name));
    if(!n||n==sizeof(s->name)||!strcmp(s->name,"IQData-Uncommissioned")||!strcmp(s->name,"IQData-Ethernet"))
        return bad(error,size,"Choose a nonempty unique name of at most 63 ASCII characters");
    for(size_t i=0;i<n;++i) if((unsigned char)s->name[i]<32||(unsigned char)s->name[i]>126)
        return bad(error,size,"Device name must be printable ASCII");
    for(unsigned i=0;i<IQ_POINT_COUNT;++i) if(!strcmp(s->name,iq_points[i].name))
        return bad(error,size,"Device name conflicts with a point name");
    if(!s->bacnet_port||s->meter_address>4095) return bad(error,size,"Invalid BACnet port or meter address");
    if(!s->dhcp) {
        uint32_t ip,mask,gateway;
        if(!ip_number(s->ip,&ip)||!ip_number(s->mask,&mask)||!ip_number(s->gateway,&gateway))
            return bad(error,size,"Static IP, mask and gateway must be IPv4 addresses");
        if(ip==0xc0a84b97u) return bad(error,size,"192.168.75.151 is protected");
        uint32_t host=~mask;
        if(!mask||host<3||(host&(host+1))||!ip||(ip>>24)==127||ip>=0xe0000000u||
           !(ip&host)||(ip&host)==host||(gateway&&((gateway&mask)!=(ip&mask)||!(gateway&host)||(gateway&host)==host)))
            return bad(error,size,"Invalid unicast address, subnet mask, host or gateway");
    }
    if(error&&size) error[0]=0;
    return true;
}
static bool get_uint(const cJSON *j,const char *key,uint32_t *out)
{
    const cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key);
    if(!cJSON_IsNumber(v)||!isfinite(v->valuedouble)||v->valuedouble<0||v->valuedouble>UINT32_MAX||floor(v->valuedouble)!=v->valuedouble) return false;
    *out=(uint32_t)v->valuedouble; return true;
}
bool iq_config_parse(const char *text,iq_config_t *out,char *error,size_t size)
{
    if(!text||strlen(text)>1023) return bad(error,size,"Configuration exceeds 1023 bytes");
    cJSON *j=cJSON_ParseWithLengthOpts(text,strlen(text)+1,NULL,true);
    if(!cJSON_IsObject(j)) { cJSON_Delete(j); return bad(error,size,"Expected one JSON object"); }
    const char *allowed[]={"device_instance","name","dhcp","ip","mask","gateway","bacnet_port","meter_address","poll_enabled"};
    bool valid=true;
    for(const cJSON *a=j->child;a;a=a->next) {
        bool known=false;
        for(unsigned i=0;i<sizeof(allowed)/sizeof(allowed[0]);++i) if(a->string&&!strcmp(a->string,allowed[i])) known=true;
        if(!known) valid=false;
        for(const cJSON *b=a->next;b;b=b->next) if(a->string&&b->string&&!strcmp(a->string,b->string)) valid=false;
    }
    iq_config_t s; iq_config_defaults(&s); s.commissioned=true;
    valid &= get_uint(j,"device_instance",&s.device_instance);
    const cJSON *name=cJSON_GetObjectItemCaseSensitive(j,"name");
    if(!cJSON_IsString(name)||strlen(name->valuestring)>=sizeof(s.name)) valid=false;
    else snprintf(s.name,sizeof(s.name),"%s",name->valuestring);
    const char *booleans[]={"dhcp","poll_enabled"};
    bool *dest[]={&s.dhcp,&s.poll_enabled};
    for(unsigned i=0;i<2;++i) {
        const cJSON *v=cJSON_GetObjectItemCaseSensitive(j,booleans[i]);
        if(v&&!cJSON_IsBool(v)) valid=false;
        else if(v) *dest[i]=cJSON_IsTrue(v);
    }
    const char *addresses[]={"ip","mask","gateway"};
    char *strings[]={s.ip,s.mask,s.gateway};
    for(unsigned i=0;i<3;++i) {
        const cJSON *v=cJSON_GetObjectItemCaseSensitive(j,addresses[i]);
        if(v&&(!cJSON_IsString(v)||strlen(v->valuestring)>=16)) valid=false;
        else if(v) snprintf(strings[i],16,"%s",v->valuestring);
    }
    const char *numbers[]={"bacnet_port","meter_address"};
    uint16_t *targets[]={&s.bacnet_port,&s.meter_address};
    for(unsigned i=0;i<2;++i) if(cJSON_GetObjectItemCaseSensitive(j,numbers[i])) {
        uint32_t n;
        if(!get_uint(j,numbers[i],&n)||n>65535) valid=false; else *targets[i]=(uint16_t)n;
    }
    cJSON_Delete(j);
    if(!valid) return bad(error,size,"Unknown/duplicate field, missing identity or invalid field type");
    if(!iq_config_valid(&s,error,size)) return false;
    *out=s; return true;
}
cJSON *iq_config_json(const iq_config_t *s)
{
    cJSON *j=cJSON_CreateObject();
    cJSON_AddNumberToObject(j,"device_instance",s->device_instance);
    cJSON_AddStringToObject(j,"name",s->name);
    cJSON_AddBoolToObject(j,"dhcp",s->dhcp);
    cJSON_AddStringToObject(j,"ip",s->ip); cJSON_AddStringToObject(j,"mask",s->mask);
    cJSON_AddStringToObject(j,"gateway",s->gateway);
    cJSON_AddNumberToObject(j,"bacnet_port",s->bacnet_port);
    cJSON_AddNumberToObject(j,"meter_address",s->meter_address);
    cJSON_AddBoolToObject(j,"poll_enabled",s->poll_enabled);
    return j;
}
