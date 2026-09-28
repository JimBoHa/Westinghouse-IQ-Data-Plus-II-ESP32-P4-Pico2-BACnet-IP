#include "iq_pico_update.h"
#include "iq_uf2.h"
#include "iq_usb.h"
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "usb/msc_host_vfs.h"

static atomic_uint disk_address;
static void disk_event(const msc_host_event_t *event,void *arg)
{
    (void)arg;
    if(event->event==MSC_DEVICE_CONNECTED)atomic_store(&disk_address,event->device.address);
    else if(event->event==MSC_DEVICE_DISCONNECTED)atomic_store(&disk_address,0);
}
void iq_pico_update_init(void)
{
    const msc_host_driver_config_t config={.create_backround_task=true,.task_priority=5,
        .stack_size=4096,.core_id=0,.callback=disk_event};
    ESP_ERROR_CHECK(msc_host_install(&config));
}
bool iq_pico_update(const uint8_t *uf2,size_t length,char *error,size_t size)
{
    size_t skip;
    if(!iq_uf2_validate(uf2,length,&skip,error,size))return false;
    iq_usb_status_t before;iq_usb_status(&before);
    /* MSC reports disconnect only while a disk handle is installed. A ROM
     * volume closed before its disconnect may leave an old address cached. */
    if(before.connected)atomic_store(&disk_address,0);
    if(!iq_usb_maintenance_begin(error,size))return false;
    bool success=false;msc_host_device_handle_t device=NULL;msc_host_vfs_handle_t vfs=NULL;
    uint64_t until=esp_timer_get_time()/1000+12000;
    esp_err_t err=ESP_ERR_NOT_FOUND;
    while(esp_timer_get_time()/1000<until) {
        unsigned address=atomic_load(&disk_address);
        if(address) {
            err=msc_host_install_device(address,&device);
            if(err==ESP_OK)break;
            if(err!=ESP_ERR_NOT_FOUND) { snprintf(error,size,"USB disk open: %s",esp_err_to_name(err));goto done; }
            /* Do not erase a newer arrival that raced the failed open. */
            atomic_compare_exchange_strong(&disk_address,&address,0);
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    if(!device) { snprintf(error,size,"Pico did not enter USB BOOTSEL within 12 seconds");goto done; }
    msc_host_device_info_t info;
    err=msc_host_get_device_info(device,&info);
    if(err!=ESP_OK||info.idVendor!=0x2e8a||info.idProduct!=0x000f||info.sector_size!=512) {
        snprintf(error,size,"Target must be Raspberry Pi RP2350 USB BOOTSEL (2e8a:000f)");goto done;
    }
    const esp_vfs_fat_mount_config_t config={.format_if_mount_failed=false,.max_files=2,.allocation_unit_size=512};
    err=msc_host_vfs_register(device,"/pico_boot",&config,&vfs);
    if(err!=ESP_OK) { snprintf(error,size,"BOOTSEL mount: %s",esp_err_to_name(err));goto done; }
    char identity[512]={0};FILE *file=fopen("/pico_boot/INFO_UF2.TXT","rb");
    if(!file) { snprintf(error,size,"BOOTSEL INFO_UF2.TXT missing");goto done; }
    size_t n=fread(identity,1,sizeof(identity)-1,file);fclose(file);identity[n]=0;
    if(!strstr(identity,"RP2350")) { snprintf(error,size,"BOOTSEL board identity mismatch");goto done; }
    ESP_LOGI("iq_pico_update","RP2350 BOOTSEL verified; writing %u UF2 blocks",(unsigned)((length-skip)/512));
    file=fopen("/pico_boot/IQDATA.UF2","wb");
    if(!file) { snprintf(error,size,"Cannot open BOOTSEL upload file");goto done; }
    setvbuf(file,NULL,_IONBF,0);
    size_t sent=skip;until=esp_timer_get_time()/1000+60000;
    while(sent<length&&esp_timer_get_time()/1000<until) {
        size_t count=length-sent;if(count>4096)count=4096;
        size_t written=fwrite(uf2+sent,1,count,file);sent+=written;
        if(written!=count)break;
        vTaskDelay(1);
    }
    int close_result=fclose(file);
    if(sent!=length) { snprintf(error,size,"Pico upload interrupted at %u/%u bytes; retry in BOOTSEL",(unsigned)(sent-skip),(unsigned)(length-skip));goto done; }
    /* ROM disconnects when it has the final UF2 block; closing the FAT file can
     * therefore fail even after all blocks reached ROM. CDC identity decides. */
    if(close_result)ESP_LOGW("iq_pico_update","BOOTSEL closed during final file flush; verifying application");
    success=true;
done:
    if(vfs)(void)msc_host_vfs_unregister(vfs);
    if(device)(void)msc_host_uninstall_device(device);
    if(success)atomic_store(&disk_address,0);
    iq_usb_maintenance_end();
    if(success) {
        until=esp_timer_get_time()/1000+15000;
        while(esp_timer_get_time()/1000<until) {
            iq_usb_status_t status;iq_usb_status(&status);
            if(status.qualified) { error[0]=0;return true; }
            vTaskDelay(pdMS_TO_TICKS(100));
        }
        snprintf(error,size,"UF2 transferred; expected Pico 0.4.7 / pico2 identity did not return");
    }
    return false;
}
