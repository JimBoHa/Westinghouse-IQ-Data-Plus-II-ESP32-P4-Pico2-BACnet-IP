#include "iq_management.h"
#include "iq_health.h"
#include "iq_security.h"
#include "iq_diagnostics.h"
#include "iq_pico_update.h"
#include "iq_uf2.h"
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_http_server.h"
#include "esp_https_server.h"
#include "esp_app_desc.h"
#include "esp_ota_ops.h"
#include "esp_image_format.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"

static atomic_bool updating;
static atomic_bool web_ready;
bool iq_web_ready(void) { return atomic_load(&web_ready); }
static esp_err_t json_reply(httpd_req_t *r,cJSON *j)
{
    if(!j) return httpd_resp_send_err(r,HTTPD_500_INTERNAL_SERVER_ERROR,"Out of memory");
    char *text=cJSON_PrintUnformatted(j);cJSON_Delete(j);
    if(!text) return httpd_resp_send_err(r,HTTPD_500_INTERNAL_SERVER_ERROR,"Out of memory");
    httpd_resp_set_type(r,"application/json");httpd_resp_set_hdr(r,"Cache-Control","no-store");
    esp_err_t err=httpd_resp_send(r,text,HTTPD_RESP_USE_STRLEN);free(text);return err;
}
static bool authorized(httpd_req_t *r)
{
    if(!iq_health_accepted()) {
        httpd_resp_set_status(r,"409 Conflict");
        httpd_resp_sendstr(r,"Startup health validation pending");return false;
    }
    bool ok=iq_security_authorize(r);
    if(!ok)iq_event("management","auth_rejected",401,"Request authentication rejected");
    return ok;
}
static esp_err_t challenge_handler(httpd_req_t *r) { return json_reply(r,iq_security_challenge()); }
static esp_err_t pair_handler(httpd_req_t *r)
{
    if(r->content_len!=64)return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Expected 32-byte hex challenge");
    char nonce[65];size_t received=0;
    while(received<64) { int n=httpd_req_recv(r,nonce+received,64-received);if(n<=0)return ESP_FAIL;received+=n; }
    nonce[64]=0;cJSON *reply=iq_security_pair(nonce);
    if(!reply)return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Invalid challenge");
    return json_reply(r,reply);
}
static esp_err_t auth_check_handler(httpd_req_t *r)
{
    if(!authorized(r))return ESP_OK;
    if(r->content_len||!iq_security_body(r,"",0))return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Expected empty authenticated body");
    cJSON *j=cJSON_CreateObject();cJSON_AddBoolToObject(j,"authenticated",true);return json_reply(r,j);
}
static esp_err_t diagnostics_handler(httpd_req_t *r)
{
    if(!authorized(r))return ESP_OK;
    if(r->content_len||!iq_security_body(r,"",0))return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Expected empty authenticated body");
    return json_reply(r,iq_diagnostics_json());
}
static esp_err_t reboot_handler(httpd_req_t *r)
{
    if(!authorized(r))return ESP_OK;
    if(r->content_len||!iq_security_body(r,"",0))return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Expected empty authenticated body");
    if(atomic_load(&updating))return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Firmware update in progress");
    cJSON *j=cJSON_CreateObject();cJSON_AddBoolToObject(j,"restarting",true);
    esp_err_t result=json_reply(r,j);iq_event("management","restart_requested",0,"Authenticated restart requested");
    iq_request_restart();return result;
}
static esp_err_t redirect_handler(httpd_req_t *r)
{
    cJSON *status=iq_status_json();const cJSON *host=cJSON_GetObjectItemCaseSensitive(status,"hostname");
    char location[96];snprintf(location,sizeof(location),"https://%s.local/",cJSON_IsString(host)?host->valuestring:"iqdata");
    cJSON_Delete(status);httpd_resp_set_status(r,"308 Permanent Redirect");
    httpd_resp_set_hdr(r,"Location",location);return httpd_resp_sendstr(r,"Use HTTPS management");
}
static esp_err_t status_handler(httpd_req_t *r) { return json_reply(r,iq_status_json()); }
static esp_err_t points_handler(httpd_req_t *r) { return json_reply(r,iq_points_json()); }
static esp_err_t index_handler(httpd_req_t *r)
{
    extern const char index_start[] asm("_binary_index_html_start");
    httpd_resp_set_type(r,"text/html; charset=utf-8");
    httpd_resp_set_hdr(r,"Cache-Control","no-store");
    httpd_resp_set_hdr(r,"X-Content-Type-Options","nosniff");
    httpd_resp_set_hdr(r,"Referrer-Policy","no-referrer");
    httpd_resp_set_hdr(r,"Content-Security-Policy","default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'");
    return httpd_resp_sendstr(r,index_start);
}
static esp_err_t script_handler(httpd_req_t *r)
{
    extern const char script_start[] asm("_binary_dashboard_js_start");
    httpd_resp_set_type(r,"text/javascript; charset=utf-8");
    httpd_resp_set_hdr(r,"Cache-Control","no-store");httpd_resp_set_hdr(r,"X-Content-Type-Options","nosniff");
    return httpd_resp_sendstr(r,script_start);
}
static esp_err_t style_handler(httpd_req_t *r)
{
    extern const char style_start[] asm("_binary_dashboard_css_start");
    httpd_resp_set_type(r,"text/css; charset=utf-8");httpd_resp_set_hdr(r,"Cache-Control","no-store");
    httpd_resp_set_hdr(r,"X-Content-Type-Options","nosniff");return httpd_resp_sendstr(r,style_start);
}
static esp_err_t config_handler(httpd_req_t *r)
{
    if(!authorized(r)) return ESP_OK;
    if(atomic_load(&updating)) return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Firmware update in progress");
    if(r->content_len<2||r->content_len>1023) return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Configuration length 2..1023 required");
    char text[1024],error[192];size_t used=0;int64_t deadline=esp_timer_get_time()+10000000;
    while(used<r->content_len&&esp_timer_get_time()<deadline) {
        int n=httpd_req_recv(r,text+used,r->content_len-used);
        if(n<=0) return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Incomplete configuration");
        used+=n;
    }
    if(used!=r->content_len) return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Configuration deadline exceeded");
    text[used]=0;
    if(!iq_security_body(r,text,used))return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Authenticated body hash mismatch");
    if(strlen(text)!=used||!iq_save_config(text,error,sizeof(error)))
        return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,strlen(text)!=used?"Embedded NUL":error);
    httpd_resp_set_type(r,"application/json");httpd_resp_sendstr(r,"{\"saved\":true,\"restarting\":true}");
    iq_event("management","configuration_saved",0,"Configuration saved; restart requested");
    iq_request_restart();return ESP_OK;
}
static bool hex_digest(const char *text,unsigned char out[32])
{
    if(strlen(text)!=64)return false;
    for(unsigned i=0;i<32;++i) {
        unsigned value=0;
        for(unsigned b=0;b<2;++b) {
            char c=text[i*2+b];unsigned nibble;
            if(c>='0'&&c<='9')nibble=c-'0';
            else if(c>='a'&&c<='f')nibble=c-'a'+10;
            else return false;
            value=value*16+nibble;
        }
        out[i]=value;
    }
    return true;
}
static esp_err_t ota_handler(httpd_req_t *r)
{
    if(!authorized(r))return ESP_OK;
    if(!iq_security_image(r,"esp32p4"))return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Valid ESP32-P4 release signature required");
    iq_event("ota","upload_started",0,r->uri);
    bool expected=false;
    if(!atomic_compare_exchange_strong(&updating,&expected,true))
        return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Update already in progress");
    char error[160]="Invalid update",digest_text[65];unsigned char expected_hash[32],actual_hash[32];
    const esp_partition_t *next=esp_ota_get_next_update_partition(NULL);
    esp_ota_handle_t handle=0;bool begun=false,success=false;
    mbedtls_sha256_context hash;mbedtls_sha256_init(&hash);
    uint8_t *buffer=heap_caps_malloc(4096,MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    if(!buffer) { snprintf(error,sizeof(error),"Out of memory");goto done; }
    if(!next||r->content_len<512||r->content_len>next->size) {
        snprintf(error,sizeof(error),"Image does not fit inactive OTA slot");goto done;
    }
    if(httpd_req_get_hdr_value_len(r,"X-SHA256")!=64||
       httpd_req_get_hdr_value_str(r,"X-SHA256",digest_text,sizeof(digest_text))!=ESP_OK||
       !hex_digest(digest_text,expected_hash)) {
        snprintf(error,sizeof(error),"X-SHA256 must contain image SHA256");goto done;
    }
    size_t received=0,first=0;int64_t deadline=esp_timer_get_time()+120000000;
    while(first<512&&esp_timer_get_time()<deadline) {
        int n=httpd_req_recv(r,(char*)buffer+first,512-first);
        if(n<=0) { snprintf(error,sizeof(error),"Truncated image header");goto done; }
        first+=n;
    }
    if(first!=512) { snprintf(error,sizeof(error),"Image header deadline exceeded");goto done; }
    esp_image_header_t header;esp_app_desc_t desc;
    memcpy(&header,buffer,sizeof(header));
    memcpy(&desc,buffer+sizeof(header)+sizeof(esp_image_segment_header_t),sizeof(desc));
    if(first!=512||header.magic!=ESP_IMAGE_HEADER_MAGIC||header.chip_id!=ESP_CHIP_ID_ESP32P4||
       desc.magic_word!=ESP_APP_DESC_MAGIC_WORD||
       memcmp(desc.project_name,esp_app_get_description()->project_name,sizeof(desc.project_name))) {
        snprintf(error,sizeof(error),"Image must be an IQData ESP32-P4 application");goto done;
    }
    esp_err_t err=esp_ota_begin(next,OTA_WITH_SEQUENTIAL_WRITES,&handle);
    if(err!=ESP_OK) { snprintf(error,sizeof(error),"esp_ota_begin: %s",esp_err_to_name(err));goto done; }
    begun=true;
    if(mbedtls_sha256_starts(&hash,0)!=0)goto done;
    for(;;) {
        size_t count=first;first=0;
        if(!count&&received<r->content_len) {
            if(esp_timer_get_time()>=deadline) { snprintf(error,sizeof(error),"120-second upload deadline exceeded");goto done; }
            size_t remaining=r->content_len-received;
            int n=httpd_req_recv(r,(char*)buffer,remaining>4096?4096:remaining);
            if(n<=0) { snprintf(error,sizeof(error),"Upload disconnected or timed out");goto done; }
            count=n;
        }
        if(!count)break;
        if(mbedtls_sha256_update(&hash,buffer,count)!=0)goto done;
        err=esp_ota_write(handle,buffer,count);
        if(err!=ESP_OK) { snprintf(error,sizeof(error),"esp_ota_write: %s",esp_err_to_name(err));goto done; }
        received+=count;
        vTaskDelay(1);
    }
    if(mbedtls_sha256_finish(&hash,actual_hash)!=0||memcmp(expected_hash,actual_hash,32)) {
        snprintf(error,sizeof(error),"Image SHA256 mismatch; boot slot unchanged");goto done;
    }
    err=esp_ota_end(handle);begun=false;
    if(err!=ESP_OK) { snprintf(error,sizeof(error),"Image verification: %s",esp_err_to_name(err));goto done; }
    err=esp_ota_set_boot_partition(next);
    if(err!=ESP_OK) { snprintf(error,sizeof(error),"Boot slot: %s",esp_err_to_name(err));goto done; }
    success=true;
done:
    if(begun)esp_ota_abort(handle);
    mbedtls_sha256_free(&hash);free(buffer);
    if(!success) { iq_event("ota","p4_rejected",1,error);atomic_store(&updating,false);return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,error); }
    httpd_resp_set_type(r,"application/json");httpd_resp_sendstr(r,"{\"verified\":true,\"restarting\":true}");
    iq_request_restart();return ESP_OK;
}
#ifndef IQ_RECOVERY_BUILD
static esp_err_t pico_handler(httpd_req_t *r)
{
    if(!authorized(r))return ESP_OK;
    if(!iq_security_image(r,"pico2"))return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Valid Pico 2 release signature required");
    iq_event("ota","upload_started",0,r->uri);
    bool expected=false;
    if(!atomic_compare_exchange_strong(&updating,&expected,true))
        return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"Update already in progress");
    char error[192]="Invalid Pico image",digest[65];unsigned char expected_hash[32],actual_hash[32];
    uint8_t *data=NULL;bool success=false;
    if(r->content_len<1024||r->content_len>IQ_UF2_MAX_BYTES||r->content_len%512) {
        snprintf(error,sizeof(error),"Pico UF2 size must be 1024..1048576 bytes, divisible by 512");goto done;
    }
    if(httpd_req_get_hdr_value_len(r,"X-SHA256")!=64||
       httpd_req_get_hdr_value_str(r,"X-SHA256",digest,sizeof(digest))!=ESP_OK||!hex_digest(digest,expected_hash)) {
        snprintf(error,sizeof(error),"X-SHA256 must contain UF2 SHA256");goto done;
    }
    data=malloc(r->content_len);
    if(!data) { snprintf(error,sizeof(error),"No memory for Pico upload");goto done; }
    size_t received=0;int64_t deadline=esp_timer_get_time()+30000000;
    while(received<r->content_len&&esp_timer_get_time()<deadline) {
        size_t count=r->content_len-received;if(count>4096)count=4096;
        int n=httpd_req_recv(r,(char*)data+received,count);
        if(n<=0) { snprintf(error,sizeof(error),"Incomplete Pico upload; Pico unchanged");goto done; }
        received+=n;
    }
    if(received!=r->content_len||mbedtls_sha256(data,received,actual_hash,0)!=0||memcmp(actual_hash,expected_hash,32)) {
        snprintf(error,sizeof(error),"Pico upload deadline or SHA256 mismatch; Pico unchanged");goto done;
    }
    success=iq_pico_update(data,received,error,sizeof(error));
done:
    free(data);atomic_store(&updating,false);
    if(!success) { iq_event("ota","pico_failed",1,error);return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,error); }
    iq_event("ota","pico_verified",0,"Expected Pico 0.4.7 / pico2 returned");
    cJSON *j=cJSON_CreateObject();cJSON_AddBoolToObject(j,"pico_boot_verified",true);
    cJSON_AddStringToObject(j,"firmware","iqdata-pico-live");cJSON_AddStringToObject(j,"version","0.4.7");
    cJSON_AddStringToObject(j,"board","pico2");cJSON_AddStringToObject(j,"upload_sha256",digest);
    return json_reply(r,j);
}
#endif
void iq_web_start(void)
{
    httpd_ssl_config_t config=HTTPD_SSL_CONFIG_DEFAULT();
    config.httpd.stack_size=16384;config.httpd.recv_wait_timeout=5;config.httpd.send_wait_timeout=5;
    config.httpd.max_uri_handlers=16;config.httpd.max_open_sockets=3;config.httpd.lru_purge_enable=true;
    config.servercert=(const uint8_t*)iq_security_certificate();config.servercert_len=strlen(iq_security_certificate())+1;
    config.prvtkey_pem=(const uint8_t*)iq_security_private_key();config.prvtkey_len=strlen(iq_security_private_key())+1;
    config.tls_handshake_timeout_ms=5000;
    httpd_handle_t server;ESP_ERROR_CHECK(httpd_ssl_start(&server,&config));
    const httpd_uri_t handlers[]={
        {.uri="/",.method=HTTP_GET,.handler=index_handler},
        {.uri="/dashboard.js",.method=HTTP_GET,.handler=script_handler},
        {.uri="/dashboard.css",.method=HTTP_GET,.handler=style_handler},
        {.uri="/api/status",.method=HTTP_GET,.handler=status_handler},
        {.uri="/api/points",.method=HTTP_GET,.handler=points_handler},
        {.uri="/api/auth/challenge",.method=HTTP_GET,.handler=challenge_handler},
        {.uri="/api/auth/check",.method=HTTP_POST,.handler=auth_check_handler},
        {.uri="/api/diagnostics",.method=HTTP_POST,.handler=diagnostics_handler},
        {.uri="/api/reboot",.method=HTTP_POST,.handler=reboot_handler},
        {.uri="/api/pair",.method=HTTP_POST,.handler=pair_handler},
        {.uri="/api/config",.method=HTTP_POST,.handler=config_handler},
        {.uri="/api/firmware",.method=HTTP_POST,.handler=ota_handler},
#ifndef IQ_RECOVERY_BUILD
        {.uri="/api/pico/firmware",.method=HTTP_POST,.handler=pico_handler},
#endif
    };
    for(unsigned i=0;i<sizeof(handlers)/sizeof(handlers[0]);++i)
        ESP_ERROR_CHECK(httpd_register_uri_handler(server,&handlers[i]));
    httpd_config_t plain=HTTPD_DEFAULT_CONFIG();plain.ctrl_port=32768;plain.max_open_sockets=1;
    plain.uri_match_fn=httpd_uri_match_wildcard;
    httpd_handle_t redirect;ESP_ERROR_CHECK(httpd_start(&redirect,&plain));
    const httpd_uri_t root={.uri="/*",.method=HTTP_GET,.handler=redirect_handler};
    ESP_ERROR_CHECK(httpd_register_uri_handler(redirect,&root));
    atomic_store(&web_ready,true);
}
