#include "iq_error_log.h"
#include <string.h>
bool iq_error_should_persist(const char *component,const char *event,int code)
{ return code!=0&&!(component&&event&&!strcmp(component,"system")&&!strcmp(event,"boot")); }
static bool emit_pending(iq_error_bucket_t *b,uint64_t now,iq_error_emit_t emit,void *context)
{
    if(!b->pending)return true;
    b->latest.occurrences=b->pending;
    if(!emit(context,&b->latest))return false;
    b->pending=0;b->emitted_ms=now;return true;
}
bool iq_error_policy_flush(iq_error_policy_t *p,uint64_t now,bool force,iq_error_emit_t emit,void *context)
{
    for(unsigned i=0;i<IQ_ERROR_BUCKETS;i++)
        if((force||now-p->buckets[i].emitted_ms>=IQ_ERROR_REPEAT_MS)&&
           !emit_pending(&p->buckets[i],now,emit,context))return false;
    return true;
}
bool iq_error_policy_submit(iq_error_policy_t *p,const iq_journal_record_t *r,uint64_t now,
    iq_error_emit_t emit,void *context)
{
    iq_error_bucket_t *chosen=NULL,*oldest=&p->buckets[0];
    for(unsigned i=0;i<IQ_ERROR_BUCKETS;i++) {
        iq_error_bucket_t *b=&p->buckets[i];
        if(b->used&&b->latest.code==r->code&&!strcmp(b->latest.component,r->component)&&
           !strcmp(b->latest.event,r->event)) {
            if(b->pending==UINT32_MAX&&!emit_pending(b,now,emit,context))return false;
            b->latest=*r;b->pending++;p->coalesced++;
            return now-b->emitted_ms<IQ_ERROR_REPEAT_MS||emit_pending(b,now,emit,context);
        }
        if(!b->used)chosen=b;
        if(b->emitted_ms<oldest->emitted_ms)oldest=b;
    }
    if(!chosen)chosen=oldest;
    if(!emit_pending(chosen,now,emit,context))return false;
    *chosen=(iq_error_bucket_t){.latest=*r,.pending=1,.used=true};
    return emit_pending(chosen,now,emit,context);
}

#ifdef ESP_PLATFORM
#include <stdio.h>
#include <stdlib.h>
#include <stdatomic.h>
#include "esp_app_desc.h"
#include "esp_partition.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
static iq_journal_t journal;
static iq_error_policy_t policy;
static SemaphoreHandle_t lock;
static QueueHandle_t queue;
static esp_err_t storage_error;
static bool runtime_partition;
static atomic_uint dropped,flush_requested,flush_completed;
static uint64_t clock_ms(void) { return esp_timer_get_time()/1000; }
static bool storage_read(void *context,size_t offset,void *data,size_t size)
{ esp_err_t e=esp_partition_read(context,offset,data,size);if(e!=ESP_OK)storage_error=e;return e==ESP_OK; }
static bool storage_write(void *context,size_t offset,const void *data,size_t size)
{ esp_err_t e=esp_partition_write(context,offset,data,size);if(e!=ESP_OK)storage_error=e;return e==ESP_OK; }
static bool storage_erase(void *context,size_t offset,size_t size)
{ esp_err_t e=esp_partition_erase_range(context,offset,size);if(e!=ESP_OK)storage_error=e;return e==ESP_OK; }
static bool append(void *unused,const iq_journal_record_t *r)
{
    (void)unused;
    if(iq_journal_append(&journal,r))return true;
    if(storage_error==ESP_OK)storage_error=ESP_FAIL;
    return false;
}
static void worker(void *unused)
{
    (void)unused;iq_journal_record_t record;
    for(;;) {
        bool received=xQueueReceive(queue,&record,pdMS_TO_TICKS(100))==pdTRUE;
        xSemaphoreTake(lock,portMAX_DELAY);
        if(received&&(!journal.ready||!iq_error_policy_submit(&policy,&record,clock_ms(),append,NULL)))
            atomic_fetch_add(&dropped,1);
        unsigned request=atomic_load(&flush_requested);
        bool force=request!=atomic_load(&flush_completed)&&uxQueueMessagesWaiting(queue)==0;
        if(journal.ready&&!iq_error_policy_flush(&policy,clock_ms(),force,append,NULL))journal.ready=false;
        if(force)atomic_store(&flush_completed,request);
        xSemaphoreGive(lock);
    }
}
void iq_error_log_init(void)
{
    lock=xSemaphoreCreateMutex();
    if(!lock) { storage_error=ESP_ERR_NO_MEM;return; }
    const esp_partition_t *partition=esp_partition_find_first(ESP_PARTITION_TYPE_DATA,0x40,"iqerrors");
    if(!partition) {
        /* OTA does not rewrite the partition table. On existing installations,
         * register exactly the same reserved range; IDF rejects overlap/overflow. */
        storage_error=esp_partition_register_external(NULL,IQ_JOURNAL_OFFSET,IQ_JOURNAL_BYTES,
            "iqerrors",ESP_PARTITION_TYPE_DATA,0x40,&partition);
        runtime_partition=storage_error==ESP_OK;
    }
    if(!partition)return;
    if(partition->address!=IQ_JOURNAL_OFFSET||partition->size!=IQ_JOURNAL_BYTES||partition->encrypted||partition->readonly) {
        storage_error=ESP_ERR_INVALID_SIZE;return;
    }
    iq_journal_io_t io={storage_read,storage_write,storage_erase,(void*)partition,partition->size};
    if(!iq_journal_open(&journal,&io)) {
        if(storage_error==ESP_OK)storage_error=ESP_ERR_INVALID_STATE;
        ESP_LOGE("iq_errors","Persistent error journal unavailable: %s",esp_err_to_name(storage_error));return;
    }
    queue=xQueueCreate(16,sizeof(iq_journal_record_t));
    if(!queue||xTaskCreate(worker,"iq_errors",4096,NULL,2,NULL)!=pdPASS) {
        journal.ready=false;storage_error=ESP_ERR_NO_MEM;
        if(queue)vQueueDelete(queue);
        queue=NULL;
    }
}
void iq_error_log_submit(const iq_event_t *e)
{
    /* Nonzero boot codes are reset reasons, not automatically errors. */
    if(!iq_error_should_persist(e->component,e->event,e->code))return;
    if(!queue) { atomic_fetch_add(&dropped,1);return; }
    iq_journal_record_t r={.boot_id=strtoull(iq_boot_id(),NULL,16),.uptime_ms=e->uptime_ms,
        .utc_ms=e->utc_ms,.code=e->code,.occurrences=1};
    snprintf(r.component,sizeof(r.component),"%s",e->component);
    snprintf(r.event,sizeof(r.event),"%s",e->event);
    snprintf(r.detail,sizeof(r.detail),"%.*s",(int)sizeof(r.detail)-1,e->detail);
    snprintf(r.firmware,sizeof(r.firmware),"%.*s",(int)sizeof(r.firmware)-1,esp_app_get_description()->version);
    snprintf(r.source,sizeof(r.source),"%.*s",(int)sizeof(r.source)-1,IQ_SOURCE_REVISION);
    if(xQueueSend(queue,&r,0)!=pdTRUE)atomic_fetch_add(&dropped,1);
}
bool iq_error_log_flush(unsigned timeout_ms)
{
    if(!queue||!lock)return false;
    unsigned requested=atomic_fetch_add(&flush_requested,1)+1;
    uint64_t deadline=clock_ms()+timeout_ms;
    while(atomic_load(&flush_completed)<requested&&clock_ms()<deadline)vTaskDelay(pdMS_TO_TICKS(10));
    xSemaphoreTake(lock,portMAX_DELAY);bool ready=journal.ready;xSemaphoreGive(lock);
    return ready&&atomic_load(&flush_completed)>=requested;
}
static cJSON *summary(void)
{
    cJSON *j=cJSON_CreateObject();if(!j)return NULL;
    cJSON_AddBoolToObject(j,"available",journal.ready);
    cJSON_AddStringToObject(j,"retention","Flash error journal; retained across restarts; oldest sector rotates when full");
    cJSON_AddNumberToObject(j,"capacity_bytes",IQ_JOURNAL_BYTES);
    cJSON_AddNumberToObject(j,"capacity_records",IQ_JOURNAL_CAPACITY);
    cJSON_AddNumberToObject(j,"stored_records",journal.count);
    cJSON_AddNumberToObject(j,"record_bytes",IQ_JOURNAL_RECORD_BYTES);
    cJSON_AddNumberToObject(j,"last_sequence",journal.last_sequence);
    cJSON_AddNumberToObject(j,"invalid_records_skipped",journal.invalid_records);
    cJSON_AddNumberToObject(j,"dropped_this_boot",atomic_load(&dropped));
    cJSON_AddNumberToObject(j,"coalesced_this_boot",policy.coalesced);
    unsigned pending=0;for(unsigned i=0;i<IQ_ERROR_BUCKETS;i++)pending+=policy.buckets[i].pending;
    cJSON_AddNumberToObject(j,"pending_repeat_occurrences",pending);
    cJSON_AddNumberToObject(j,"queued_records",queue?uxQueueMessagesWaiting(queue):0);
    cJSON_AddNumberToObject(j,"repeat_interval_seconds",IQ_ERROR_REPEAT_MS/1000);
    cJSON_AddBoolToObject(j,"runtime_partition",runtime_partition);
    cJSON_AddNumberToObject(j,"partition_offset",IQ_JOURNAL_OFFSET);
    cJSON_AddStringToObject(j,"storage_error",storage_error==ESP_OK?"":esp_err_to_name(storage_error));
    return j;
}
cJSON *iq_error_log_status(void)
{
    if(lock)xSemaphoreTake(lock,portMAX_DELAY);
    cJSON *j=summary();if(lock)xSemaphoreGive(lock);return j;
}
static cJSON *record_json(const iq_journal_record_t *r)
{
    cJSON *j=cJSON_CreateObject();char boot[17];snprintf(boot,sizeof(boot),"%016llx",(unsigned long long)r->boot_id);
    cJSON_AddNumberToObject(j,"sequence",r->sequence);cJSON_AddStringToObject(j,"boot_id",boot);
    cJSON_AddNumberToObject(j,"uptime_ms",r->uptime_ms);
    if(r->utc_ms)cJSON_AddNumberToObject(j,"utc_ms",r->utc_ms);else cJSON_AddNullToObject(j,"utc_ms");
    cJSON_AddNumberToObject(j,"code",r->code);cJSON_AddNumberToObject(j,"occurrences",r->occurrences);
    cJSON_AddStringToObject(j,"component",r->component);cJSON_AddStringToObject(j,"event",r->event);
    cJSON_AddStringToObject(j,"detail",r->detail);cJSON_AddStringToObject(j,"firmware_version",r->firmware);
    cJSON_AddStringToObject(j,"source_revision",r->source);return j;
}
cJSON *iq_error_log_json(uint64_t before,unsigned limit)
{
    if(!limit||limit>32)limit=32;
    iq_journal_record_t *records=calloc(limit,sizeof(*records));if(!records)return NULL;
    if(lock)xSemaphoreTake(lock,portMAX_DELAY);
    unsigned count=0;bool more=false;
    if(journal.ready&&!iq_journal_page(&journal,before,records,limit,&count,&more)) {
        if(storage_error==ESP_OK)storage_error=ESP_FAIL;
    }
    cJSON *j=summary();if(lock)xSemaphoreGive(lock);
    if(j) {
        cJSON *a=cJSON_AddArrayToObject(j,"records");
        for(unsigned i=0;i<count;i++)cJSON_AddItemToArray(a,record_json(&records[i]));
        cJSON_AddBoolToObject(j,"has_more",more);
        if(count&&more)cJSON_AddNumberToObject(j,"next_before",records[count-1].sequence);
        else cJSON_AddNullToObject(j,"next_before");
    }
    free(records);return j;
}
#endif
