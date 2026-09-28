#include "iq_management.h"
#include "iq_health.h"
#include "iq_diagnostics.h"
#include "iq_security.h"
#include "iq_config.h"
#include "iq_usb.h"
#include "gateway_bacnet.h"
#include <math.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/task.h"
#include "driver/uart.h"
#include "esp_app_desc.h"
#include "esp_chip_info.h"
#include "esp_eth.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_ota_ops.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "bootloader_random.h"
#include "nvs_flash.h"
#include "mdns.h"
#include "lwip/inet.h"

static iq_model_t *model;
static iq_config_t settings;
static SemaphoreHandle_t model_lock,state_lock,config_lock;
static nvs_handle_t storage;
static char update_token[65],hostname[32],mac_text[18];
static esp_netif_t *netif;
static esp_netif_ip_info_t network;
static bool link_up,ip_ready,protected_ip;
static gateway_bacnet_stats_t bacnet_stats;
static uint64_t bacnet_heartbeat;
static atomic_bool restarting;
static uint64_t now_ms(void) { return esp_timer_get_time()/1000; }

static void print_json(cJSON *value)
{
    char *text=cJSON_PrintUnformatted(value);cJSON_Delete(value);
    if(text) { printf("IQJSON %s\n",text);free(text); }
}
bool iq_check_token(const char *token)
{
    if(!token||strlen(token)!=64)return false;
    xSemaphoreTake(config_lock,portMAX_DELAY);
    unsigned mismatch=0;
    for(unsigned i=0;i<64;++i)mismatch|=(unsigned char)token[i]^(unsigned char)update_token[i];
    xSemaphoreGive(config_lock);
    return mismatch==0;
}
static bool save_token(const char *token)
{
    if(strlen(token)!=64)return false;
    for(unsigned i=0;i<64;++i)if(!((token[i]>='0'&&token[i]<='9')||(token[i]>='a'&&token[i]<='f')))return false;
    xSemaphoreTake(config_lock,portMAX_DELAY);
    esp_err_t err=nvs_set_str(storage,"update_token",token);
    if(err==ESP_OK)err=nvs_commit(storage);
    if(err==ESP_OK) { memcpy(update_token,token,65);iq_security_rotate(token); }
    xSemaphoreGive(config_lock);
    return err==ESP_OK;
}
bool iq_save_config(const char *json,char *error,size_t size)
{
    iq_config_t parsed;
    if(!iq_config_parse(json,&parsed,error,size))return false;
    cJSON *value=iq_config_json(&parsed);char *text=cJSON_PrintUnformatted(value);cJSON_Delete(value);
    if(!text) { snprintf(error,size,"Out of memory");return false; }
    xSemaphoreTake(config_lock,portMAX_DELAY);
    esp_err_t err=nvs_set_str(storage,"settings",text);
    if(err==ESP_OK)err=nvs_commit(storage);
    xSemaphoreGive(config_lock);free(text);
    if(err!=ESP_OK)snprintf(error,size,"Configuration storage: %s",esp_err_to_name(err));
    return err==ESP_OK;
}
static void restart_task(void *arg)
{
    (void)arg;vTaskDelay(pdMS_TO_TICKS(300));iq_usb_stop();
    uint64_t until=now_ms()+6500;
    do {
        iq_usb_status_t status;iq_usb_status(&status);
        if(status.stopped)break;
        vTaskDelay(pdMS_TO_TICKS(25));
    } while(now_ms()<until);
    esp_restart();
}
void iq_request_restart(void)
{
    bool expected=false;
    if(atomic_compare_exchange_strong(&restarting,&expected,true))
        configASSERT(xTaskCreate(restart_task,"iq_restart",3072,NULL,5,NULL)==pdPASS);
}
cJSON *iq_status_json(void)
{
    cJSON *j=cJSON_CreateObject();
    const esp_app_desc_t *app=esp_app_get_description();
    cJSON_AddStringToObject(j,"project",app->project_name);
    cJSON_AddStringToObject(j,"version",app->version);
    cJSON_AddStringToObject(j,"source_revision",IQ_SOURCE_REVISION);
    char hash[65];for(unsigned i=0;i<32;++i)snprintf(hash+2*i,3,"%02x",app->app_elf_sha256[i]);
    cJSON_AddStringToObject(j,"elf_sha256",hash);
    cJSON_AddStringToObject(j,"board","ESP32-P4-WIFI6-POE-ETH");
    cJSON_AddStringToObject(j,"ethernet_mac",mac_text);
    cJSON_AddStringToObject(j,"hostname",hostname);
    cJSON_AddNumberToObject(j,"uptime_seconds",now_ms()/1000.0);
    cJSON_AddNumberToObject(j,"reset_reason",esp_reset_reason());
    cJSON_AddNumberToObject(j,"free_heap",esp_get_free_heap_size());
    cJSON_AddNumberToObject(j,"minimum_free_heap",esp_get_minimum_free_heap_size());
    cJSON_AddNumberToObject(j,"internal_free_heap",heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    cJSON_AddNumberToObject(j,"model_bytes",iq_model_bytes());
    cJSON_AddBoolToObject(j,"commissioned",settings.commissioned);
    cJSON_AddItemToObject(j,"config",iq_config_json(&settings));
    esp_netif_ip_info_t ip;bool link,ready,blocked;gateway_bacnet_stats_t bs;uint64_t heartbeat;
    xSemaphoreTake(state_lock,portMAX_DELAY);
    ip=network;link=link_up;ready=ip_ready;blocked=protected_ip;bs=bacnet_stats;heartbeat=bacnet_heartbeat;
    xSemaphoreGive(state_lock);
    cJSON *eth=cJSON_AddObjectToObject(j,"ethernet");char address[16];
    esp_ip4addr_ntoa(&ip.ip,address,sizeof(address));cJSON_AddStringToObject(eth,"ip",address);
    cJSON_AddBoolToObject(eth,"link_up",link);cJSON_AddBoolToObject(eth,"ready",ready);
    cJSON_AddBoolToObject(eth,"protected_address_blocked",blocked);
    iq_usb_status_t us;iq_usb_status(&us);
    cJSON *usb=cJSON_AddObjectToObject(j,"pico");
    cJSON_AddBoolToObject(usb,"connected",us.connected);cJSON_AddBoolToObject(usb,"qualified",us.qualified);
    cJSON_AddStringToObject(usb,"version",us.version);cJSON_AddStringToObject(usb,"last_error",us.last_error);
    cJSON_AddNumberToObject(usb,"connections",us.connections);cJSON_AddNumberToObject(usb,"failures",us.failures);
    cJSON_AddNumberToObject(usb,"overflows",us.overflows);
    cJSON_AddNumberToObject(usb,"devices_seen_since_boot",us.devices_seen);
    cJSON_AddBoolToObject(usb,"maintenance",us.maintenance);
    cJSON_AddNumberToObject(usb,"last_usb_vid",us.last_vid);cJSON_AddNumberToObject(usb,"last_usb_pid",us.last_pid);
    cJSON_AddNumberToObject(usb,"heartbeat_age_seconds",(now_ms()-us.heartbeat_ms)/1000.0);
    cJSON *bac=cJSON_AddObjectToObject(j,"bacnet");
    cJSON_AddBoolToObject(bac,"initialized",bs.initialized);cJSON_AddNumberToObject(bac,"analog_inputs",106);
    cJSON_AddNumberToObject(bac,"binary_inputs",92);cJSON_AddNumberToObject(bac,"good_points",bs.good_points);
    cJSON_AddNumberToObject(bac,"fault_points",bs.fault_points);cJSON_AddNumberToObject(bac,"received_packets",bs.received_packets);
    cJSON_AddNumberToObject(bac,"cov_pending",bs.cov_pending);cJSON_AddNumberToObject(bac,"cov_timeouts",bs.cov_timeouts);
    cJSON_AddNumberToObject(bac,"heartbeat_age_seconds",(now_ms()-heartbeat)/1000.0);
    cJSON_AddStringToObject(bac,"instance_status",bs.instance_conflicts?"conflict":(bs.instance_check_complete?"checked":"checking"));
    cJSON_AddNumberToObject(bac,"instance_checks",bs.instance_checks);cJSON_AddNumberToObject(bac,"instance_conflicts",bs.instance_conflicts);
    if(bs.instance_conflicts) {
        cJSON *conflict=cJSON_AddObjectToObject(bac,"last_conflict");esp_ip4_addr_t peer={.addr=bs.conflict_ip};
        esp_ip4addr_ntoa(&peer,address,sizeof(address));cJSON_AddStringToObject(conflict,"ip",address);
        cJSON_AddNumberToObject(conflict,"port",bs.conflict_port);cJSON_AddNumberToObject(conflict,"network",bs.conflict_network);
        cJSON_AddNumberToObject(conflict,"uptime_ms",bs.last_conflict_ms);
    } else cJSON_AddNullToObject(bac,"last_conflict");
    cJSON *ota=cJSON_AddObjectToObject(j,"ota");
    const esp_partition_t *running=esp_ota_get_running_partition(),*next=esp_ota_get_next_update_partition(NULL);
    esp_ota_img_states_t state=ESP_OTA_IMG_UNDEFINED;
    if(running)(void)esp_ota_get_state_partition(running,&state);
    cJSON_AddStringToObject(ota,"running_slot",running?running->label:"");
    cJSON_AddStringToObject(ota,"next_slot",next?next->label:"");
    cJSON_AddNumberToObject(ota,"slot_bytes",next?next->size:0);
    cJSON_AddNumberToObject(ota,"image_state",state);
    cJSON_AddBoolToObject(ota,"rollback_enabled",true);
    cJSON_AddItemToObject(ota,"startup_health",iq_health_json());
    cJSON_AddItemToObject(j,"clock",iq_clock_json());
    cJSON_AddItemToObject(j,"security",iq_security_json());
    cJSON_AddBoolToObject(j,"restarting",atomic_load(&restarting));
    return j;
}
cJSON *iq_points_json(void)
{
    iq_value_t *values=malloc(sizeof(*values)*IQ_POINT_COUNT);
    if(!values)return NULL;
    xSemaphoreTake(model_lock,portMAX_DELAY);iq_model_snapshot(model,now_ms(),values);xSemaphoreGive(model_lock);
    cJSON *array=cJSON_CreateArray();
    for(unsigned i=0;i<IQ_POINT_COUNT;++i) {
        cJSON *p=cJSON_CreateObject();cJSON_AddItemToArray(array,p);
        cJSON_AddStringToObject(p,"type",iq_points[i].object_type==0?"analog-input":"binary-input");
        cJSON_AddNumberToObject(p,"instance",iq_points[i].instance);cJSON_AddStringToObject(p,"name",iq_points[i].name);
        cJSON_AddStringToObject(p,"key",iq_points[i].key);cJSON_AddNumberToObject(p,"units",iq_points[i].units);
        cJSON_AddNumberToObject(p,"value",values[i].value);cJSON_AddBoolToObject(p,"valid",values[i].valid);
        cJSON_AddStringToObject(p,"reliability",values[i].valid?"no-fault-detected":"communication-failure");
    }
    free(values);return array;
}
static void network_event(void *arg,esp_event_base_t base,int32_t id,void *data)
{
    (void)arg;
    xSemaphoreTake(state_lock,portMAX_DELAY);
    if(base==ETH_EVENT) {
        if(id==ETHERNET_EVENT_CONNECTED) { link_up=true;iq_event("ethernet","link_up",0,"Ethernet link established");ESP_LOGI("iq_eth","Link up"); }
        else if(id==ETHERNET_EVENT_DISCONNECTED||id==ETHERNET_EVENT_STOP) {
            iq_event("ethernet","link_down",1,"Ethernet link lost");link_up=false;ip_ready=false;memset(&network,0,sizeof(network));ESP_LOGW("iq_eth","Link down");
        }
    } else if(base==IP_EVENT&&id==IP_EVENT_ETH_GOT_IP) {
        network=((ip_event_got_ip_t*)data)->ip_info;
        protected_ip=ntohl(network.ip.addr)==0xc0a84b97u;
        ip_ready=network.ip.addr&&!protected_ip;
        if(ip_ready) { iq_clock_network_ready();iq_event("ethernet","ipv4_ready",0,"IPv4 address assigned"); }
        ESP_LOGI("iq_eth","Address " IPSTR " (%s.local)",IP2STR(&network.ip),hostname);
    } else if(base==IP_EVENT&&id==IP_EVENT_ETH_LOST_IP)ip_ready=false;
    bool stop=protected_ip;xSemaphoreGive(state_lock);
    if(stop) { ESP_LOGE("iq_eth","Protected production address assigned; stopping Ethernet");esp_netif_action_stop(netif,ETH_EVENT,ETHERNET_EVENT_STOP,NULL); }
}
static void ethernet_start(void)
{
    ESP_ERROR_CHECK(esp_netif_init());ESP_ERROR_CHECK(esp_event_loop_create_default());iq_clock_init();
    esp_netif_config_t cfg=ESP_NETIF_DEFAULT_ETH();netif=esp_netif_new(&cfg);configASSERT(netif);
    ESP_ERROR_CHECK(esp_netif_set_hostname(netif,hostname));
    eth_mac_config_t mc=ETH_MAC_DEFAULT_CONFIG();eth_phy_config_t pc=ETH_PHY_DEFAULT_CONFIG();
    eth_esp32_emac_config_t emac=ETH_ESP32_EMAC_DEFAULT_CONFIG();
    /* Waveshare schematic: RMII clock input GPIO50; fixed data pins from IDF P4 defaults. */
    emac.smi_gpio.mdc_num=31;emac.smi_gpio.mdio_num=52;pc.phy_addr=1;pc.reset_gpio_num=51;
    esp_eth_mac_t *mac=esp_eth_mac_new_esp32(&emac,&mc);esp_eth_phy_t *phy=esp_eth_phy_new_ip101(&pc);
    configASSERT(mac&&phy);esp_eth_config_t ec=ETH_DEFAULT_CONFIG(mac,phy);esp_eth_handle_t handle;
    ESP_ERROR_CHECK(esp_eth_driver_install(&ec,&handle));
    uint8_t address[6];ESP_ERROR_CHECK(esp_eth_ioctl(handle,ETH_CMD_G_MAC_ADDR,address));
    snprintf(mac_text,sizeof(mac_text),"%02x:%02x:%02x:%02x:%02x:%02x",address[0],address[1],address[2],address[3],address[4],address[5]);
    ESP_ERROR_CHECK(esp_netif_attach(netif,esp_eth_new_netif_glue(handle)));
    ESP_ERROR_CHECK(esp_event_handler_register(ETH_EVENT,ESP_EVENT_ANY_ID,network_event,NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT,IP_EVENT_ETH_GOT_IP,network_event,NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT,IP_EVENT_ETH_LOST_IP,network_event,NULL));
    if(settings.commissioned&&!settings.dhcp) {
        esp_netif_ip_info_t ip={0};
        ESP_ERROR_CHECK(esp_netif_dhcpc_stop(netif));
        configASSERT(esp_netif_str_to_ip4(settings.ip,&ip.ip)==ESP_OK);
        configASSERT(esp_netif_str_to_ip4(settings.mask,&ip.netmask)==ESP_OK);
        configASSERT(esp_netif_str_to_ip4(settings.gateway,&ip.gw)==ESP_OK);
        ESP_ERROR_CHECK(esp_netif_set_ip_info(netif,&ip));
    }
    ESP_ERROR_CHECK(esp_eth_start(handle));
    ESP_ERROR_CHECK(mdns_init());ESP_ERROR_CHECK(mdns_hostname_set(hostname));
    ESP_ERROR_CHECK(mdns_instance_name_set("IQ Data Plus II ESP32-P4 gateway"));
    mdns_txt_item_t txt[]={{"project","iqdata_p4_gateway"},{"mac",mac_text}};
    ESP_ERROR_CHECK(mdns_service_add(NULL,"_http","_tcp",80,txt,2));
    ESP_LOGI("iq_eth","IP101 initialized; MAC %s; DHCP %s",mac_text,settings.dhcp?"on":"off");
}
static void bacnet_task(void *arg)
{
    (void)arg;bool initialized=false;uint64_t next=0;esp_netif_ip_info_t previous={0};bool previous_up=false;
    iq_value_t *values=malloc(sizeof(*values)*IQ_POINT_COUNT);configASSERT(values);
    for(;;) {
        esp_netif_ip_info_t ip;bool ready;
        xSemaphoreTake(state_lock,portMAX_DELAY);ip=network;ready=link_up&&ip_ready&&!protected_ip;xSemaphoreGive(state_lock);
        if(!initialized&&ready&&settings.commissioned) {
            gateway_bacnet_config_t cfg={.device_instance=settings.device_instance,.device_name=settings.name,
                .firmware_version=esp_app_get_description()->version,.udp_port=settings.bacnet_port,
                .local_ip=ip.ip.addr,.netmask=ip.netmask.addr,.gateway=ip.gw.addr,.dhcp_enabled=settings.dhcp,
                .vendor_id=0,.database_revision=1};
            initialized=gateway_bacnet_init(&cfg,now_ms());
            if(!initialized) { ESP_LOGE("iq_bacnet","BACnet initialization failed");vTaskDelay(pdMS_TO_TICKS(1000)); }
            else ESP_LOGI("iq_bacnet","Device %lu: 106 AI + 92 BI",(unsigned long)settings.device_instance);
        }
        if(initialized) {
            if(ready!=previous_up||memcmp(&ip,&previous,sizeof(ip)))
                (void)gateway_bacnet_network_update(ip.ip.addr,ip.netmask.addr,ip.gw.addr,ready,now_ms());
            previous=ip;previous_up=ready;
            if(now_ms()>=next) {
                xSemaphoreTake(model_lock,portMAX_DELAY);iq_model_snapshot(model,now_ms(),values);xSemaphoreGive(model_lock);
                gateway_bacnet_update(values);next=now_ms()+100;
            }
            gateway_bacnet_tick(now_ms());(void)gateway_bacnet_poll(0);
        }
        gateway_bacnet_stats_t stats;gateway_bacnet_stats(&stats);
        xSemaphoreTake(state_lock,portMAX_DELAY);bacnet_stats=stats;bacnet_heartbeat=now_ms();xSemaphoreGive(state_lock);
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
static void console_task(void *arg)
{
    (void)arg;char line[1200];size_t used=0;bool overflow=false;
    for(;;) {
        uint8_t ch;
        if(uart_read_bytes(UART_NUM_0,&ch,1,pdMS_TO_TICKS(100))<=0)continue;
        if(ch=='\r')continue;
        if(ch=='\n') {
            line[used]=0;
            if(overflow)puts("IQERROR Command too long");
            else if(!strcmp(line,"status"))print_json(iq_status_json());
            else if(!strcmp(line,"token")) { xSemaphoreTake(config_lock,portMAX_DELAY);printf("IQTOKEN %s\n",update_token);xSemaphoreGive(config_lock); }
            else if(!strncmp(line,"key ",4))puts(save_token(line+4)?"IQOK Token saved":"IQERROR Invalid token or storage failure");
            else if(!strncmp(line,"config ",7)) {
                char error[192];if(iq_save_config(line+7,error,sizeof(error))) { puts("IQOK Config saved; restarting");iq_request_restart(); }
                else printf("IQERROR %s\n",error);
            } else if(!strcmp(line,"reboot"))iq_request_restart();
            else if(used)puts("IQERROR Commands: status, token, key <hex>, config <JSON>, reboot");
            used=0;overflow=false;continue;
        }
        if(ch<32||ch>126||used+1>=sizeof(line))overflow=true;
        else if(!overflow)line[used++]=(char)ch;
    }
}
static bool startup_healthy(void)
{
    iq_usb_status_t us;iq_usb_status(&us);
    xSemaphoreTake(state_lock,portMAX_DELAY);
    uint64_t heartbeat=bacnet_heartbeat;
    bool network_ok=!(link_up&&ip_ready&&settings.commissioned)||bacnet_stats.initialized;
    bool blocked=protected_ip;
    xSemaphoreGive(state_lock);
    return iq_web_ready()&&!blocked&&network_ok&&now_ms()-heartbeat<2000&&
        now_ms()-us.heartbeat_ms<7000&&heap_caps_get_free_size(MALLOC_CAP_INTERNAL)>32768;
}
void app_main(void)
{
    iq_health_begin();iq_diagnostics_init();
    esp_chip_info_t chip;esp_chip_info(&chip);
    ESP_LOGI("iq_main","IQData %s source %s; ESP32-P4 revision %u",esp_app_get_description()->version,IQ_SOURCE_REVISION,chip.revision);
    configASSERT(chip.model==CHIP_ESP32P4&&chip.revision>=100&&chip.revision<300);
    model_lock=xSemaphoreCreateMutex();state_lock=xSemaphoreCreateMutex();config_lock=xSemaphoreCreateMutex();
    configASSERT(model_lock&&state_lock&&config_lock);
    /* Never erase a partition automatically on NVS errors. */
    ESP_ERROR_CHECK(nvs_flash_init_partition("iqconfig"));
    ESP_ERROR_CHECK(nvs_open_from_partition("iqconfig","iqdata",NVS_READWRITE,&storage));
    iq_config_defaults(&settings);char text[1024],error[192];size_t size=sizeof(text);
    esp_err_t err=nvs_get_str(storage,"settings",text,&size);
    if(err==ESP_OK&&!iq_config_parse(text,&settings,error,sizeof(error)))ESP_LOGE("iq_main","Stored config rejected: %s",error);
    else if(err!=ESP_OK&&err!=ESP_ERR_NVS_NOT_FOUND)ESP_ERROR_CHECK(err);
    size=sizeof(update_token);err=nvs_get_str(storage,"update_token",update_token,&size);
    if(err==ESP_ERR_NVS_NOT_FOUND) {
        uint8_t random[32];char token[65];bootloader_random_enable();esp_fill_random(random,sizeof(random));bootloader_random_disable();
        for(unsigned i=0;i<32;++i)snprintf(token+2*i,3,"%02x",random[i]);
        configASSERT(save_token(token));memset(random,0,sizeof(random));memset(token,0,sizeof(token));
    } else ESP_ERROR_CHECK(err);
    configASSERT(strlen(update_token)==64);
    uint8_t mac[6];ESP_ERROR_CHECK(esp_read_mac(mac,ESP_MAC_BASE));
    snprintf(hostname,sizeof(hostname),"iqdata-%02x%02x%02x",mac[3],mac[4],mac[5]);
    model=iq_model_create();configASSERT(model);
    iq_usb_start(model,model_lock,&settings);
    ethernet_start();iq_security_init(update_token,mac_text);iq_web_start();
    configASSERT(xTaskCreate(bacnet_task,"iq_bacnet",16384,NULL,4,NULL)==pdPASS);
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_0,2048,0,0,NULL,0));
    configASSERT(xTaskCreate(console_task,"iq_console",8192,NULL,3,NULL)==pdPASS);
    ESP_LOGI("iq_main","Ready: https://%s.local; meter polling %s; commissioning %s",hostname,settings.poll_enabled?"on":"off",settings.commissioned?"complete":"required");
    iq_health_start(startup_healthy);
}
