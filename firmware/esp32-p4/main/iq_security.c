#include "iq_security.h"
#include "iq_signing_key.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "bootloader_random.h"
#include "nvs.h"
#include "mbedtls/ecp.h"
#include "mbedtls/md.h"
#include "mbedtls/pk.h"
#include "mbedtls/sha256.h"
#include "mbedtls/x509_crt.h"

typedef struct { uint32_t version; char cert[1536], key[512]; } tls_identity_t;
typedef struct { char nonce[33]; int64_t issued; bool used; } challenge_t;
static tls_identity_t identity;
static challenge_t challenges[4];
static unsigned next_challenge;
static unsigned char admin_key[32];
static char device_mac[18], certificate_hash[65];
static SemaphoreHandle_t lock;
static mbedtls_pk_context signer;
static void require(bool ok) { if(!ok)abort(); }
static int rng(void *ctx, unsigned char *output, size_t size)
{ (void)ctx; esp_fill_random(output,size); return 0; }
static void hex(const unsigned char *bytes, size_t size, char *out)
{ for(size_t i=0;i<size;i++) snprintf(out+2*i,3,"%02x",bytes[i]); }
static bool unhex(const char *text, size_t size, unsigned char *out)
{
    if(!text||strlen(text)!=size*2)return false;
    for(size_t i=0;i<size;i++) {
        unsigned value=0;
        for(unsigned j=0;j<2;j++) {
            unsigned char c=text[2*i+j];
            if(c>='0'&&c<='9')value=value*16+c-'0';
            else if(c>='a'&&c<='f')value=value*16+c-'a'+10;
            else return false;
        }
        out[i]=(unsigned char)value;
    }
    return true;
}
static bool equal(const unsigned char *a,const unsigned char *b,size_t size)
{ unsigned mismatch=0;for(size_t i=0;i<size;i++)mismatch|=a[i]^b[i];return mismatch==0; }
static void hmac(const char *text,char out[65])
{
    unsigned char digest[32];
    xSemaphoreTake(lock,portMAX_DELAY);
    int result=mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256),admin_key,32,
        (const unsigned char*)text,strlen(text),digest);
    xSemaphoreGive(lock);
    require(result==0);hex(digest,32,out);
}
static void make_identity(const char *mac)
{
    mbedtls_pk_context key;mbedtls_pk_init(&key);
    mbedtls_x509write_cert cert;mbedtls_x509write_crt_init(&cert);
    require(mbedtls_pk_setup(&key,mbedtls_pk_info_from_type(MBEDTLS_PK_ECKEY))==0);
    bootloader_random_enable();
    require(mbedtls_ecp_gen_key(MBEDTLS_ECP_DP_SECP256R1,mbedtls_pk_ec(key),rng,NULL)==0);
    mbedtls_x509write_crt_set_version(&cert,MBEDTLS_X509_CRT_VERSION_3);
    mbedtls_x509write_crt_set_md_alg(&cert,MBEDTLS_MD_SHA256);
    mbedtls_x509write_crt_set_subject_key(&cert,&key);mbedtls_x509write_crt_set_issuer_key(&cert,&key);
    char subject[96];snprintf(subject,sizeof(subject),"CN=IQData %s,O=IQData Gateway",mac);
    require(mbedtls_x509write_crt_set_subject_name(&cert,subject)==0);
    require(mbedtls_x509write_crt_set_issuer_name(&cert,subject)==0);
    char hostname[48];snprintf(hostname,sizeof(hostname),"iqdata-%.2s%.2s%.2s.local",mac+9,mac+12,mac+15);
    mbedtls_x509_san_list san={.node={.type=MBEDTLS_X509_SAN_DNS_NAME,
        .san.unstructured_name={.p=(unsigned char*)hostname,.len=strlen(hostname)}}};
    require(mbedtls_x509write_crt_set_subject_alternative_name(&cert,&san)==0);
    unsigned char serial[16];rng(NULL,serial,sizeof(serial));serial[0]&=0x7f;serial[0]|=1;
    require(mbedtls_x509write_crt_set_serial_raw(&cert,serial,sizeof(serial))==0);
    require(mbedtls_x509write_crt_set_validity(&cert,"20260101000000","20400101000000")==0);
    require(mbedtls_x509write_crt_set_basic_constraints(&cert,0,-1)==0);
    require(mbedtls_x509write_crt_set_key_usage(&cert,MBEDTLS_X509_KU_DIGITAL_SIGNATURE)==0);
    require(mbedtls_x509write_crt_pem(&cert,(unsigned char*)identity.cert,sizeof(identity.cert),rng,NULL)==0);
    require(mbedtls_pk_write_key_pem(&key,(unsigned char*)identity.key,sizeof(identity.key))==0);
    bootloader_random_disable();
    identity.version=1;mbedtls_x509write_crt_free(&cert);mbedtls_pk_free(&key);
}
void iq_security_rotate(const char *token)
{
    if(!lock)return;
    unsigned char key[32];require(unhex(token,32,key));
    xSemaphoreTake(lock,portMAX_DELAY);memcpy(admin_key,key,32);memset(challenges,0,sizeof(challenges));
    xSemaphoreGive(lock);memset(key,0,sizeof(key));
}
void iq_security_init(const char *token,const char *mac)
{
    lock=xSemaphoreCreateMutex();require(lock);iq_security_rotate(token);
    snprintf(device_mac,sizeof(device_mac),"%s",mac);
    nvs_handle_t store;ESP_ERROR_CHECK(nvs_open_from_partition("iqconfig","iqdata",NVS_READWRITE,&store));
    size_t size=sizeof(identity);esp_err_t result=nvs_get_blob(store,"tls_identity",&identity,&size);
    if(result==ESP_ERR_NVS_NOT_FOUND) {
        make_identity(mac);ESP_ERROR_CHECK(nvs_set_blob(store,"tls_identity",&identity,sizeof(identity)));
        ESP_ERROR_CHECK(nvs_commit(store));
    } else { ESP_ERROR_CHECK(result);require(size==sizeof(identity)&&identity.version==1); }
    nvs_close(store);
    require(memchr(identity.cert,0,sizeof(identity.cert))&&memchr(identity.key,0,sizeof(identity.key)));
    mbedtls_x509_crt cert;mbedtls_x509_crt_init(&cert);mbedtls_pk_context key;mbedtls_pk_init(&key);
    require(mbedtls_x509_crt_parse(&cert,(unsigned char*)identity.cert,strlen(identity.cert)+1)==0);
    require(mbedtls_pk_parse_key(&key,(unsigned char*)identity.key,strlen(identity.key)+1,NULL,0,rng,NULL)==0);
    require(mbedtls_pk_check_pair(&cert.pk,&key,rng,NULL)==0);
    unsigned char digest[32];require(mbedtls_sha256(cert.raw.p,cert.raw.len,digest,0)==0);
    hex(digest,32,certificate_hash);mbedtls_pk_free(&key);mbedtls_x509_crt_free(&cert);
    mbedtls_pk_init(&signer);
    require(mbedtls_pk_parse_public_key(&signer,(const unsigned char*)IQ_SIGNING_PUBLIC_KEY,sizeof(IQ_SIGNING_PUBLIC_KEY))==0);
}
const char *iq_security_certificate(void) { return identity.cert; }
const char *iq_security_private_key(void) { return identity.key; }
cJSON *iq_security_json(void)
{
    cJSON *j=cJSON_CreateObject();cJSON_AddBoolToObject(j,"https",true);
    cJSON_AddStringToObject(j,"certificate_sha256",certificate_hash);
    cJSON_AddStringToObject(j,"authentication","hmac-sha256-single-use-nonce-v1");
    cJSON_AddStringToObject(j,"firmware_signature","ecdsa-p256-sha256-v1");
    cJSON_AddStringToObject(j,"signing_key_sha256",IQ_SIGNING_KEY_SHA256);
    return j;
}
cJSON *iq_security_challenge(void)
{
    unsigned char random[16];esp_fill_random(random,sizeof(random));char nonce[33];hex(random,16,nonce);
    xSemaphoreTake(lock,portMAX_DELAY);challenge_t *c=&challenges[next_challenge++%4];
    snprintf(c->nonce,sizeof(c->nonce),"%s",nonce);c->issued=esp_timer_get_time();c->used=false;xSemaphoreGive(lock);
    cJSON *j=cJSON_CreateObject();cJSON_AddStringToObject(j,"nonce",nonce);cJSON_AddNumberToObject(j,"expires_seconds",60);return j;
}
cJSON *iq_security_pair(const char *nonce)
{
    unsigned char decoded[32];if(!unhex(nonce,32,decoded))return NULL;
    char context[256],proof[65];snprintf(context,sizeof(context),"IQDATA-TLS-PAIR-V1\n%s\n%s\n%s\n",nonce,device_mac,certificate_hash);
    hmac(context,proof);cJSON *j=cJSON_CreateObject();cJSON_AddStringToObject(j,"ethernet_mac",device_mac);
    cJSON_AddStringToObject(j,"certificate_sha256",certificate_hash);cJSON_AddStringToObject(j,"proof",proof);return j;
}
static bool header(httpd_req_t *r,const char *name,char *out,size_t size,size_t exact)
{ return httpd_req_get_hdr_value_len(r,name)==exact&&httpd_req_get_hdr_value_str(r,name,out,size)==ESP_OK; }
bool iq_security_authorize(httpd_req_t *r)
{
    char nonce[33],digest[65],signature[65];unsigned char supplied[32],wanted[32],ignored[32];
    bool ok=header(r,"X-IQ-Nonce",nonce,sizeof(nonce),32)&&header(r,"X-SHA256",digest,sizeof(digest),64)&&
        header(r,"X-IQ-Auth",signature,sizeof(signature),64)&&unhex(digest,32,ignored)&&unhex(signature,32,supplied);
    if(ok) {
        char context[768],expected[65];snprintf(context,sizeof(context),"IQDATA-AUTH-V1\nPOST\n%s\n%s\n%u\n%s\n",
            r->uri,nonce,(unsigned)r->content_len,digest);hmac(context,expected);unhex(expected,32,wanted);
        ok=false;xSemaphoreTake(lock,portMAX_DELAY);
        for(unsigned i=0;i<4;i++) {
            challenge_t *c=&challenges[i];int64_t age=esp_timer_get_time()-c->issued;
            if(!c->used&&!strcmp(c->nonce,nonce)&&age>=0&&age<=60000000&&equal(supplied,wanted,32)) {
                c->used=true;ok=true;break;
            }
        }
        xSemaphoreGive(lock);
    }
    if(!ok) { httpd_resp_set_status(r,"401 Unauthorized");httpd_resp_sendstr(r,"Valid one-use request authentication required"); }
    return ok;
}
bool iq_security_body(httpd_req_t *r,const void *body,size_t size)
{
    char supplied[65];unsigned char digest[32],expected[32];
    return header(r,"X-SHA256",supplied,sizeof(supplied),64)&&unhex(supplied,32,expected)&&
        mbedtls_sha256(body,size,digest,0)==0&&equal(digest,expected,32);
}
bool iq_security_image(httpd_req_t *r,const char *target)
{
    size_t length=httpd_req_get_hdr_value_len(r,"X-Image-Signature");
    char signature[145],digest[65];unsigned char bytes[72],hash[32];
    if(length<128||length>144||length%2||httpd_req_get_hdr_value_str(r,"X-Image-Signature",signature,sizeof(signature))!=ESP_OK||
       !unhex(signature,length/2,bytes)||!header(r,"X-SHA256",digest,sizeof(digest),64))return false;
    char context[160];int n=snprintf(context,sizeof(context),"IQDATA-IMAGE-V1\n%s\n%u\n%s\n",target,(unsigned)r->content_len,digest);
    if(n<0||(size_t)n>=sizeof(context)||mbedtls_sha256((unsigned char*)context,n,hash,0))return false;
    return mbedtls_pk_verify(&signer,MBEDTLS_MD_SHA256,hash,32,bytes,length/2)==0;
}
