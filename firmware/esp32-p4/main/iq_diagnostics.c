#include "iq_diagnostics.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
void iq_event_append(iq_event_ring_t *ring,uint64_t uptime,int64_t utc,const char *component,
    const char *event,int code,const char *detail)
{
    iq_event_t *entry=&ring->events[ring->next];
    *entry=(iq_event_t){.sequence=++ring->total,.uptime_ms=uptime,.utc_ms=utc,.code=code};
    snprintf(entry->component,sizeof(entry->component),"%s",component?component:"");
    snprintf(entry->event,sizeof(entry->event),"%s",event?event:"");
    snprintf(entry->detail,sizeof(entry->detail),"%s",detail?detail:"");
    ring->next=(ring->next+1)%IQ_EVENT_CAPACITY;
    if(ring->count<IQ_EVENT_CAPACITY)++ring->count;
}
cJSON *iq_event_json(const iq_event_ring_t *ring)
{
    cJSON *j=cJSON_CreateObject(),*events=cJSON_AddArrayToObject(j,"events");
    cJSON_AddNumberToObject(j,"capacity",IQ_EVENT_CAPACITY);cJSON_AddNumberToObject(j,"total",ring->total);
    cJSON_AddNumberToObject(j,"overwritten",ring->total-ring->count);cJSON_AddStringToObject(j,"retention","RAM, current boot only");
    for(unsigned i=0;i<ring->count;i++) {
        const iq_event_t *e=&ring->events[(ring->next+IQ_EVENT_CAPACITY-1-i)%IQ_EVENT_CAPACITY];
        cJSON *v=cJSON_CreateObject();cJSON_AddItemToArray(events,v);
        cJSON_AddNumberToObject(v,"sequence",e->sequence);cJSON_AddNumberToObject(v,"uptime_ms",e->uptime_ms);
        if(e->utc_ms)cJSON_AddNumberToObject(v,"utc_ms",e->utc_ms);else cJSON_AddNullToObject(v,"utc_ms");
        cJSON_AddStringToObject(v,"component",e->component);cJSON_AddStringToObject(v,"event",e->event);
        cJSON_AddNumberToObject(v,"code",e->code);cJSON_AddStringToObject(v,"detail",e->detail);
    }
    return j;
}
#ifdef ESP_PLATFORM
#include <sys/time.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_netif_sntp.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "sdkconfig.h"
static iq_event_ring_t ring;
static SemaphoreHandle_t mutex;
static bool synchronized,clock_initialized,clock_started;
static uint64_t last_sync_ms;
static char boot_id[17];
static uint64_t monotonic_ms(void) { return esp_timer_get_time()/1000; }
static int64_t utc_now(void)
{
    struct timeval time;gettimeofday(&time,NULL);
    /* A plausible civil time alone is not evidence of synchronization. */
    return synchronized&&time.tv_sec>=1704067200LL&&time.tv_sec<4102444800LL?
        (int64_t)time.tv_sec*1000+time.tv_usec/1000:0;
}
void iq_diagnostics_init(void)
{
    mutex=xSemaphoreCreateMutex();configASSERT(mutex);
    unsigned char random[8];esp_fill_random(random,sizeof(random));
    for(unsigned i=0;i<8;i++)snprintf(boot_id+2*i,3,"%02x",random[i]);
    iq_event("system","boot",esp_reset_reason(),"Gateway startup; prior RAM events are not retained");
}
void iq_event(const char *component,const char *event,int code,const char *detail)
{
    if(!mutex)return;
    xSemaphoreTake(mutex,portMAX_DELAY);
    iq_event_append(&ring,monotonic_ms(),utc_now(),component,event,code,detail);
    xSemaphoreGive(mutex);
}
static void time_synced(struct timeval *time)
{
    xSemaphoreTake(mutex,portMAX_DELAY);
    synchronized=time->tv_sec>=1704067200LL&&time->tv_sec<4102444800LL;
    bool valid=synchronized;if(valid)last_sync_ms=monotonic_ms();
    xSemaphoreGive(mutex);
    iq_event("clock",valid?"synchronized":"rejected",valid?0:1,valid?"NTP time accepted":"NTP time outside supported 2024..2099 range");
}
void iq_clock_init(void)
{
    if(!CONFIG_IQ_NTP_SERVER[0])return;
    esp_sntp_config_t config=ESP_NETIF_SNTP_DEFAULT_CONFIG(CONFIG_IQ_NTP_SERVER);
    config.start=false;config.wait_for_sync=false;config.sync_cb=time_synced;
    esp_err_t err=esp_netif_sntp_init(&config);clock_initialized=err==ESP_OK;
    if(err!=ESP_OK)iq_event("clock","initialization_failed",err,"NTP unavailable; events use uptime");
}
void iq_clock_network_ready(void)
{
    if(clock_initialized&&!clock_started) {
        esp_err_t err=esp_netif_sntp_start();clock_started=err==ESP_OK;
        if(err!=ESP_OK)iq_event("clock","start_failed",err,"NTP start failed; events use uptime");
    }
}
bool iq_clock_utc(int64_t *utc_ms)
{
    if(!mutex) { *utc_ms=0;return false; }
    xSemaphoreTake(mutex,portMAX_DELAY);*utc_ms=utc_now();xSemaphoreGive(mutex);return *utc_ms!=0;
}
cJSON *iq_clock_json(void)
{
    xSemaphoreTake(mutex,portMAX_DELAY);int64_t utc=utc_now();uint64_t last=last_sync_ms;
    xSemaphoreGive(mutex);cJSON *j=cJSON_CreateObject();cJSON_AddBoolToObject(j,"synchronized",utc!=0);
    cJSON_AddStringToObject(j,"server",CONFIG_IQ_NTP_SERVER);cJSON_AddStringToObject(j,"timezone","UTC");
    if(utc)cJSON_AddNumberToObject(j,"utc_ms",utc);else cJSON_AddNullToObject(j,"utc_ms");
    if(last)cJSON_AddNumberToObject(j,"last_sync_age_seconds",(monotonic_ms()-last)/1000.0);
    else cJSON_AddNullToObject(j,"last_sync_age_seconds");
    return j;
}
cJSON *iq_diagnostics_json(void)
{
    iq_event_ring_t *snapshot=malloc(sizeof(*snapshot));if(!snapshot)return NULL;
    xSemaphoreTake(mutex,portMAX_DELAY);*snapshot=ring;xSemaphoreGive(mutex);
    cJSON *j=iq_event_json(snapshot);free(snapshot);
    cJSON_AddStringToObject(j,"boot_id",boot_id);cJSON_AddItemToObject(j,"clock",iq_clock_json());return j;
}
#endif
