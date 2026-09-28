#include "iq_usb.h"
#include "iq_diagnostics.h"
#include "iq_transport.h"
#include "iq_pico_update.h"
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include "freertos/task.h"
#include "freertos/stream_buffer.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "usb/usb_host.h"
#include "usb/cdc_acm_host.h"

static iq_model_t *model;
static SemaphoreHandle_t model_lock,status_lock;
static const iq_config_t *settings;
static StreamBufferHandle_t receive;
static iq_usb_status_t status;
static atomic_bool disconnected,overflow,stopping,maintenance_requested;
static atomic_int driver_error;
static uint64_t now_ms(void) { return esp_timer_get_time()/1000; }
static void new_device(usb_device_handle_t device)
{
    const usb_device_desc_t *desc=NULL;
    if(usb_host_get_device_descriptor(device,&desc)!=ESP_OK||!desc)return;
    xSemaphoreTake(status_lock,portMAX_DELAY);
    ++status.devices_seen;status.last_vid=desc->idVendor;status.last_pid=desc->idProduct;
    if(desc->idVendor==0x2e8a&&desc->idProduct==0x000f)
        snprintf(status.last_error,sizeof(status.last_error),"RP2350 BOOTSEL: install plain Pico 2 iqdata-pico-live 0.4.7 firmware");
    xSemaphoreGive(status_lock);
    ESP_LOGI("iq_usb","USB device %04x:%04x class %02x",desc->idVendor,desc->idProduct,desc->bDeviceClass);
}
static void failure(const char *text)
{
    xSemaphoreTake(status_lock,portMAX_DELAY);
    snprintf(status.last_error,sizeof(status.last_error),"%s",text);++status.failures;
    xSemaphoreGive(status_lock);
    iq_event("usb","failure",1,text);
    ESP_LOGW("iq_usb","%s",text);
}
void iq_usb_status(iq_usb_status_t *out)
{
    xSemaphoreTake(status_lock,portMAX_DELAY);*out=status;xSemaphoreGive(status_lock);
}
void iq_usb_stop(void) { atomic_store(&stopping,true); }
bool iq_usb_maintenance_begin(char *error,size_t size)
{
    iq_event("usb","maintenance",0,"Releasing meter transaction for firmware maintenance");
    atomic_store(&maintenance_requested,true);
    uint64_t until=now_ms()+12000;
    while(now_ms()<until) {
        iq_usb_status_t current;iq_usb_status(&current);
        if(current.maintenance)return true;
        vTaskDelay(pdMS_TO_TICKS(25));
    }
    atomic_store(&maintenance_requested,false);
    snprintf(error,size,"Meter worker did not enter maintenance before deadline");return false;
}
void iq_usb_maintenance_end(void) { atomic_store(&maintenance_requested,false); }
static bool rx(const uint8_t *data,size_t size,void *arg)
{
    (void)arg;
    if(xStreamBufferSend(receive,data,size,0)!=size) atomic_store(&overflow,true);
    return true;
}
static void event(const cdc_acm_host_dev_event_data_t *event,void *arg)
{
    (void)arg;
    if(event->type==CDC_ACM_HOST_DEVICE_DISCONNECTED) atomic_store(&disconnected,true);
    else if(event->type==CDC_ACM_HOST_ERROR) atomic_store(&driver_error,event->data.error?event->data.error:-1);
}
static void usb_library(void *arg)
{
    (void)arg;
    for(;;) {
        uint32_t flags=0;
        esp_err_t err=usb_host_lib_handle_events(pdMS_TO_TICKS(1000),&flags);
        if(err!=ESP_OK&&err!=ESP_ERR_TIMEOUT) ESP_LOGW("iq_usb","USB library: %s",esp_err_to_name(err));
        if(flags&USB_HOST_LIB_EVENT_FLAGS_NO_CLIENTS) (void)usb_host_device_free_all();
    }
}
static bool send(cdc_acm_dev_hdl_t device,const char *text,char *error,size_t size)
{
    esp_err_t err=cdc_acm_host_data_tx_blocking(device,(const uint8_t *)text,strlen(text),500);
    if(err!=ESP_OK) snprintf(error,size,"USB transmit: %s (0x%x)",esp_err_to_name(err),(unsigned)err);
    return err==ESP_OK;
}
static void drain(uint64_t milliseconds)
{
    uint8_t bytes[512];uint64_t until=now_ms()+milliseconds;
    while(now_ms()<until&&!atomic_load(&stopping)&&!atomic_load(&disconnected)) {
        xSemaphoreTake(status_lock,portMAX_DELAY);status.heartbeat_ms=now_ms();xSemaphoreGive(status_lock);
        (void)xStreamBufferReceive(receive,bytes,sizeof(bytes),pdMS_TO_TICKS(10));
    }
}
static bool identify(cdc_acm_dev_hdl_t device)
{
    char error[192]={0},line[1024];size_t used=0;
    drain(200);
    if(!send(device,"info\n",error,sizeof(error))) { failure(error);return false; }
    uint64_t until=now_ms()+5000;
    while(now_ms()<until&&!atomic_load(&disconnected)&&!atomic_load(&stopping)) {
        uint8_t ch;
        if(!xStreamBufferReceive(receive,&ch,1,pdMS_TO_TICKS(20))) continue;
        if(ch=='\r') continue;
        if(ch=='\n') {
            line[used]=0;
            char version[48];
            if(!iq_pico_identity(line,version,sizeof(version))) {
                failure("Pico identity mismatch: require iqdata-pico-live 0.4.7, build_board=pico2");return false;
            }
            xSemaphoreTake(status_lock,portMAX_DELAY);
            snprintf(status.version,sizeof(status.version),"%s",version);status.qualified=true;
            if(!status.failures)status.last_error[0]=0;
            xSemaphoreGive(status_lock);
            iq_event("usb","qualified",0,version);ESP_LOGI("iq_usb","Qualified %s",version);return true;
        }
        if(ch<32||ch>126||used+1>=sizeof(line)) { failure("Invalid Pico info response");return false; }
        line[used++]=(char)ch;
    }
    failure("No Pico info response before 5-second deadline");return false;
}

static bool transact(cdc_acm_dev_hdl_t device,iq_kind_t kind,uint32_t *flags)
{
    iq_stream_t stream; iq_stream_init(&stream,kind,settings->meter_address);
    char command[80],error[192]={0};uint8_t bytes[512];
    snprintf(command,sizeof(command),"transact %s_repeat %u rising 500\n",iq_kind_name(kind),settings->meter_address);
    uint64_t began=now_ms(),deadline=began+5500,drain_deadline=0;
    if(!send(device,command,error,sizeof(error))) iq_stream_error(&stream,error);
    while(!stream.failed&&now_ms()<deadline) {
        xSemaphoreTake(status_lock,portMAX_DELAY);status.heartbeat_ms=now_ms();xSemaphoreGive(status_lock);
        if(atomic_load(&stopping)) { iq_stream_error(&stream,"Shutdown requested; no retry");break; }
        if(atomic_load(&disconnected)) { iq_stream_error(&stream,"Pico USB disconnected");break; }
        if(atomic_load(&overflow)) { iq_stream_error(&stream,"USB receive buffer overflow");break; }
        int code=atomic_load(&driver_error);
        if(code) { snprintf(error,sizeof(error),"CDC driver error %d",code);iq_stream_error(&stream,error);break; }
        size_t n=xStreamBufferReceive(receive,bytes,sizeof(bytes),pdMS_TO_TICKS(10));
        if(n) iq_stream_feed(&stream,bytes,n);
        if(stream.terminal&&!drain_deadline) drain_deadline=now_ms()+100;
        if(drain_deadline&&now_ms()>=drain_deadline) break;
    }
    bool good=iq_stream_finish(&stream);
    uint64_t finished=now_ms();
    xSemaphoreTake(model_lock,portMAX_DELAY);
    if(good) good=iq_model_accept(model,kind,stream.words,stream.word_count,finished,(finished-began)/1000.0,stream.stop,stream.malformed);
    else iq_model_fail(model,kind,finished,(finished-began)/1000.0,stream.stop,stream.malformed);
    xSemaphoreGive(model_lock);
    if(good&&kind==IQ_FLAGS&&flags) *flags=(stream.words[1]>>1)&0x7fffff;
    if(!good) {
        failure(stream.failed?stream.error:"Invalid meter DATA buffer");
        if(!atomic_load(&disconnected)) (void)send(device,"abort\n",error,sizeof(error));
    } else {
        xSemaphoreTake(status_lock,portMAX_DELAY);status.last_success_ms=finished;status.last_error[0]=0;xSemaphoreGive(status_lock);
    }
    return good;
}

static void poll_task(void *arg)
{
    (void)arg;
    const cdc_acm_host_device_config_t config={.connection_timeout_ms=1000,
        .out_buffer_size=512,.in_buffer_size=2048,.event_cb=event,.data_cb=rx};
    cdc_acm_dev_hdl_t device=NULL;
    uint64_t next=0,due[IQ_KIND_COUNT]={0};unsigned fails[IQ_KIND_COUNT]={0};
    const unsigned periods[IQ_KIND_COUNT]={0,10000,300000,60000};
    bool diagnostic_slot=false;
    bool have_flags=false;uint32_t last_flags=0;
    for(;;) {
        xSemaphoreTake(status_lock,portMAX_DELAY);
        status.heartbeat_ms=now_ms();status.stopping=atomic_load(&stopping);
        xSemaphoreGive(status_lock);
        if(atomic_load(&stopping)) {
            if(device) { char error[192];(void)send(device,"abort\n",error,sizeof(error));
                vTaskDelay(pdMS_TO_TICKS(600));(void)cdc_acm_host_set_control_line_state(device,false,false);
                (void)cdc_acm_host_close(device);device=NULL; }
            xSemaphoreTake(status_lock,portMAX_DELAY);status.stopped=true;status.qualified=false;xSemaphoreGive(status_lock);
            vTaskDelete(NULL);
        }
        if(atomic_load(&maintenance_requested)) {
            /* A failed meter transaction closes CDC during retry backoff.
             * Maintenance must still acquire and identify that running Pico;
             * a Pico already in BOOTSEL simply has no matching CDC device. */
            if(!device) {
                atomic_store(&disconnected,false);atomic_store(&overflow,false);atomic_store(&driver_error,0);
                xStreamBufferReset(receive);
                esp_err_t err=cdc_acm_host_open(0x2e8a,0x0009,0,&config,&device);
                if(err==ESP_OK) {
                    xSemaphoreTake(status_lock,portMAX_DELAY);
                    status.connected=true;++status.connections;
                    xSemaphoreGive(status_lock);
                    cdc_acm_line_coding_t line={.dwDTERate=115200,.bDataBits=8};
                    err=cdc_acm_host_line_coding_set(device,&line);
                    if(err==ESP_OK)err=cdc_acm_host_set_control_line_state(device,true,false);
                    if(err!=ESP_OK||!identify(device)) {
                        (void)cdc_acm_host_close(device);device=NULL;
                    }
                } else device=NULL;
            }
            if(device) {
                char error[192];(void)send(device,"abort\n",error,sizeof(error));
                /* Existing firmware's absolute output deadline is four seconds.
                 * Keep VBUS on and allow every meter output to release first. */
                drain(4500);
                /* Pico SDK 2.3.1 reset vendor interface 2, BOOTSEL request 1.
                 * The baseline intentionally disables baud-rate-triggered reset. */
                (void)cdc_acm_host_send_custom_request(device,0x21,1,0,2,0,NULL);
                vTaskDelay(pdMS_TO_TICKS(150));
                (void)cdc_acm_host_close(device);device=NULL;
            }
            xSemaphoreTake(status_lock,portMAX_DELAY);
            status.connected=false;status.qualified=false;status.maintenance=true;status.heartbeat_ms=now_ms();
            xSemaphoreGive(status_lock);vTaskDelay(pdMS_TO_TICKS(25));continue;
        }
        xSemaphoreTake(status_lock,portMAX_DELAY);status.maintenance=false;xSemaphoreGive(status_lock);
        if(!device) {
            atomic_store(&disconnected,false);atomic_store(&overflow,false);atomic_store(&driver_error,0);
            xStreamBufferReset(receive);
            /* Pico SDK's RP2350 CDC PID differs from RP2040 builds. Firmware
             * identity and board target, not VID/PID alone, authorize reads. */
            esp_err_t err=cdc_acm_host_open(0x2e8a,0x0009,0,&config,&device);
            if(err!=ESP_OK) { device=NULL;vTaskDelay(pdMS_TO_TICKS(250));continue; }
            xSemaphoreTake(status_lock,portMAX_DELAY);status.connected=true;++status.connections;xSemaphoreGive(status_lock);
            cdc_acm_line_coding_t line={.dwDTERate=115200,.bDataBits=8};
            err=cdc_acm_host_line_coding_set(device,&line);
            if(err==ESP_OK) err=cdc_acm_host_set_control_line_state(device,true,false);
            if(err!=ESP_OK) {
                char error[96];snprintf(error,sizeof(error),"CDC line configuration: %s",esp_err_to_name(err));failure(error);
            }
            bool qualified=err==ESP_OK&&identify(device);
            if(!qualified) {
                (void)cdc_acm_host_set_control_line_state(device,false,false);
                (void)cdc_acm_host_close(device);device=NULL;
                xSemaphoreTake(status_lock,portMAX_DELAY);status.connected=false;status.qualified=false;xSemaphoreGive(status_lock);
                vTaskDelay(pdMS_TO_TICKS(1000));continue;
            }
            memset(due,0,sizeof(due));next=now_ms();diagnostic_slot=false;
        }
        if(atomic_load(&disconnected)||atomic_load(&driver_error)||atomic_load(&overflow)) {
            xSemaphoreTake(model_lock,portMAX_DELAY);
            for(unsigned k=0;k<IQ_KIND_COUNT;++k) iq_model_fail(model,k,now_ms(),0,-1,0);
            xSemaphoreGive(model_lock);
            if(atomic_load(&overflow)) { xSemaphoreTake(status_lock,portMAX_DELAY);++status.overflows;xSemaphoreGive(status_lock); }
            failure(atomic_load(&disconnected)?"Pico USB disconnected":"USB driver/receive overflow fault");
            (void)cdc_acm_host_close(device);device=NULL;
            xSemaphoreTake(status_lock,portMAX_DELAY);status.connected=status.qualified=false;xSemaphoreGive(status_lock);
            continue;
        }
        if(!settings->poll_enabled||now_ms()<next) { vTaskDelay(pdMS_TO_TICKS(25));continue; }
        iq_kind_t kind=IQ_STANDARD;
        if(diagnostic_slot) for(unsigned k=IQ_FLAGS;k<IQ_KIND_COUNT;++k)
            if(due[k]<=now_ms()&&(kind==IQ_STANDARD||due[k]<due[kind])) kind=k;
        diagnostic_slot=kind==IQ_STANDARD;
        uint64_t began=now_ms();
        uint32_t current_flags=0;
        bool good=transact(device,kind,&current_flags);
        next=began+1050;
        fails[kind]=good?0:fails[kind]+1;
        if(kind!=IQ_STANDARD) {
            unsigned backoff=1000u<<(fails[kind]>9?9:fails[kind]);
            if(backoff>300000)backoff=300000;
            due[kind]=now_ms()+(periods[kind]>backoff?periods[kind]:backoff);
        }
        if(good&&kind==IQ_FLAGS) {
            /* Match the reference's early trip-buffer read on changed flags. */
            if(have_flags&&current_flags!=last_flags)due[IQ_TRIP]=0;
            last_flags=current_flags;have_flags=true;
        }
        if(!good) {
            /* Keep an attached CDC interface claimed while aborting/draining.
             * Releasing and reclaiming active USB bulk endpoints can desync
             * endpoint DATA toggles without a device bus reset. Actual USB
             * disconnects still close and reopen through the event path. */
            drain(4500);
            unsigned wait=kind==IQ_STANDARD?(1u<<(fails[kind]>6?6:fails[kind])):5;
            if(wait<5)wait=5;
            if(wait>60)wait=60;
            uint64_t until=now_ms()+wait*1000;
            while(now_ms()<until&&!atomic_load(&stopping)&&!atomic_load(&maintenance_requested)) {
                xSemaphoreTake(status_lock,portMAX_DELAY);status.heartbeat_ms=now_ms();xSemaphoreGive(status_lock);
                vTaskDelay(pdMS_TO_TICKS(25));
            }
            if(!atomic_load(&stopping)&&!atomic_load(&maintenance_requested)&&
               !atomic_load(&disconnected)&&!atomic_load(&driver_error)&&!atomic_load(&overflow)) {
                if(!identify(device)) {
                    (void)cdc_acm_host_set_control_line_state(device,false,false);
                    (void)cdc_acm_host_close(device);device=NULL;
                    xSemaphoreTake(status_lock,portMAX_DELAY);status.connected=status.qualified=false;xSemaphoreGive(status_lock);
                }
            }
        }
    }
}

void iq_usb_start(iq_model_t *m,SemaphoreHandle_t lock,const iq_config_t *config)
{
    model=m;model_lock=lock;settings=config;
    status_lock=xSemaphoreCreateMutex();receive=xStreamBufferCreate(65536,1);
    configASSERT(status_lock&&receive);
    const usb_host_config_t host={.skip_phy_setup=false,.intr_flags=ESP_INTR_FLAG_LEVEL1,.peripheral_map=0};
    ESP_ERROR_CHECK(usb_host_install(&host));
    configASSERT(xTaskCreate(usb_library,"iq_usb_library",4096,NULL,8,NULL)==pdPASS);
    const cdc_acm_host_driver_config_t driver={.driver_task_stack_size=4096,
        .driver_task_priority=10,.xCoreID=0,.new_dev_cb=new_device};
    ESP_ERROR_CHECK(cdc_acm_host_install(&driver));
    iq_pico_update_init();
    configASSERT(xTaskCreate(poll_task,"iq_meter",24576,NULL,4,NULL)==pdPASS);
}
