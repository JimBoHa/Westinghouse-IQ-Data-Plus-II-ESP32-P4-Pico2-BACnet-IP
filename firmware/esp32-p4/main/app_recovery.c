/* Optional small Ethernet recovery image. No meter requests or USB resets. */
#include "iq_management.h"
#include "iq_health.h"
#include "iq_security.h"
#include "iq_config.h"
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_app_desc.h"
#include "esp_eth.h"
#include "esp_event.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "mdns.h"
#include "lwip/inet.h"

static char token[65],mac_text[18],hostname[32];
static esp_netif_t *netif;
static iq_config_t settings;
static void restart(void *unused)
{ (void)unused;vTaskDelay(pdMS_TO_TICKS(500));esp_restart(); }
void iq_request_restart(void)
{ configASSERT(xTaskCreate(restart,"restart",2048,NULL,5,NULL)==pdPASS); }
bool iq_check_token(const char *supplied)
{
    if(!supplied||strlen(supplied)!=64)return false;
    unsigned difference=0;
    for(unsigned i=0;i<64;++i)difference|=(unsigned char)token[i]^(unsigned char)supplied[i];
    return difference==0;
}
bool iq_save_config(const char *json,char *error,size_t size)
{ (void)json;snprintf(error,size,"Recovery image: install full application before changing configuration");return false; }
cJSON *iq_points_json(void) { return cJSON_CreateArray(); }
cJSON *iq_status_json(void)
{
    cJSON *j=cJSON_CreateObject();const esp_app_desc_t *app=esp_app_get_description();
    cJSON_AddStringToObject(j,"project",app->project_name);cJSON_AddStringToObject(j,"version",app->version);
    cJSON_AddBoolToObject(j,"recovery",true);cJSON_AddStringToObject(j,"ethernet_mac",mac_text);
    cJSON_AddStringToObject(j,"hostname",hostname);cJSON_AddNumberToObject(j,"reset_reason",esp_reset_reason());
    cJSON_AddNumberToObject(j,"uptime_seconds",esp_timer_get_time()/1000000.0);
    cJSON_AddNumberToObject(j,"free_heap",esp_get_free_heap_size());
    cJSON_AddItemToObject(j,"config",iq_config_json(&settings));
    cJSON_AddItemToObject(j,"security",iq_security_json());
    char hash[65];for(unsigned i=0;i<32;++i)snprintf(hash+2*i,3,"%02x",app->app_elf_sha256[i]);
    cJSON_AddStringToObject(j,"elf_sha256",hash);
    esp_netif_ip_info_t ip={0};(void)esp_netif_get_ip_info(netif,&ip);char address[16];
    esp_ip4addr_ntoa(&ip.ip,address,sizeof(address));cJSON_AddStringToObject(j,"ip",address);
    cJSON *ota=cJSON_AddObjectToObject(j,"ota");
    const esp_partition_t *running=esp_ota_get_running_partition(),*next=esp_ota_get_next_update_partition(NULL);
    esp_ota_img_states_t state=ESP_OTA_IMG_UNDEFINED;(void)esp_ota_get_state_partition(running,&state);
    cJSON_AddStringToObject(ota,"running_slot",running->label);cJSON_AddStringToObject(ota,"next_slot",next->label);
    cJSON_AddNumberToObject(ota,"slot_bytes",next->size);cJSON_AddNumberToObject(ota,"image_state",state);
    return j;
}
static void got_ip(void *arg,esp_event_base_t base,int32_t id,void *data)
{
    (void)arg;(void)base;(void)id;
    const ip_event_got_ip_t *event=data;
    if(ntohl(event->ip_info.ip.addr)==0xc0a84b97u)
        esp_netif_action_stop(netif,ETH_EVENT,ETHERNET_EVENT_STOP,NULL);
}
void app_main(void)
{
    iq_health_begin();
    ESP_ERROR_CHECK(nvs_flash_init_partition("iqconfig"));nvs_handle_t store;
    ESP_ERROR_CHECK(nvs_open_from_partition("iqconfig","iqdata",NVS_READONLY,&store));
    size_t n=sizeof(token);ESP_ERROR_CHECK(nvs_get_str(store,"update_token",token,&n));
    configASSERT(strlen(token)==64);iq_config_defaults(&settings);
    char json[1024],error[192];n=sizeof(json);
    if(nvs_get_str(store,"settings",json,&n)==ESP_OK)configASSERT(iq_config_parse(json,&settings,error,sizeof(error)));
    nvs_close(store);
    uint8_t mac_bytes[6];ESP_ERROR_CHECK(esp_read_mac(mac_bytes,ESP_MAC_BASE));
    snprintf(hostname,sizeof(hostname),"iqdata-%02x%02x%02x",mac_bytes[3],mac_bytes[4],mac_bytes[5]);
    ESP_ERROR_CHECK(esp_netif_init());ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_config_t cfg=ESP_NETIF_DEFAULT_ETH();netif=esp_netif_new(&cfg);configASSERT(netif);
    ESP_ERROR_CHECK(esp_netif_set_hostname(netif,hostname));
    eth_mac_config_t mc=ETH_MAC_DEFAULT_CONFIG();eth_phy_config_t pc=ETH_PHY_DEFAULT_CONFIG();
    eth_esp32_emac_config_t emac=ETH_ESP32_EMAC_DEFAULT_CONFIG();
    emac.smi_gpio.mdc_num=31;emac.smi_gpio.mdio_num=52;pc.phy_addr=1;pc.reset_gpio_num=51;
    esp_eth_mac_t *mac=esp_eth_mac_new_esp32(&emac,&mc);esp_eth_phy_t *phy=esp_eth_phy_new_ip101(&pc);
    configASSERT(mac&&phy);esp_eth_config_t ec=ETH_DEFAULT_CONFIG(mac,phy);esp_eth_handle_t handle;
    ESP_ERROR_CHECK(esp_eth_driver_install(&ec,&handle));
    ESP_ERROR_CHECK(esp_eth_ioctl(handle,ETH_CMD_G_MAC_ADDR,mac_bytes));
    snprintf(mac_text,sizeof(mac_text),"%02x:%02x:%02x:%02x:%02x:%02x",mac_bytes[0],mac_bytes[1],mac_bytes[2],mac_bytes[3],mac_bytes[4],mac_bytes[5]);
    ESP_ERROR_CHECK(esp_netif_attach(netif,esp_eth_new_netif_glue(handle)));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT,IP_EVENT_ETH_GOT_IP,got_ip,NULL));
    if(settings.commissioned&&!settings.dhcp) {
        esp_netif_ip_info_t ip={0};ESP_ERROR_CHECK(esp_netif_dhcpc_stop(netif));
        ESP_ERROR_CHECK(esp_netif_str_to_ip4(settings.ip,&ip.ip));ESP_ERROR_CHECK(esp_netif_str_to_ip4(settings.mask,&ip.netmask));
        ESP_ERROR_CHECK(esp_netif_str_to_ip4(settings.gateway,&ip.gw));ESP_ERROR_CHECK(esp_netif_set_ip_info(netif,&ip));
    }
    ESP_ERROR_CHECK(esp_eth_start(handle));ESP_ERROR_CHECK(mdns_init());ESP_ERROR_CHECK(mdns_hostname_set(hostname));
    ESP_ERROR_CHECK(mdns_service_add(NULL,"_https","_tcp",443,NULL,0));iq_security_init(token,mac_text);iq_web_start();
    iq_health_start(iq_web_ready);
}
