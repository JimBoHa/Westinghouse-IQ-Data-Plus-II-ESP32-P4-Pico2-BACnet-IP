#include "iq_https_redirect.h"
#include <stdio.h>
#include <string.h>
#include <strings.h>

bool iq_https_location(const cJSON *status,const char *request_host,char *out,size_t size)
{
    if(!out||!size)return false;
    out[0]=0;
    const cJSON *hostname=cJSON_GetObjectItemCaseSensitive(status,"hostname");
    const cJSON *ethernet=cJSON_GetObjectItemCaseSensitive(status,"ethernet");
    const cJSON *ip=cJSON_GetObjectItemCaseSensitive(ethernet,"ip");
    if(!ethernet)ip=cJSON_GetObjectItemCaseSensitive(status,"ip"); /* Recovery image. */
    char dns[80],with_port[84];
    const char *destination=NULL;
    if(cJSON_IsString(hostname)&&hostname->valuestring[0]) {
        int n=snprintf(dns,sizeof(dns),"%s.local",hostname->valuestring);
        if(n>0&&(size_t)n<sizeof(dns)) {
            (void)snprintf(with_port,sizeof(with_port),"%s:80",dns);
            if(request_host&&(!strcasecmp(request_host,dns)||!strcasecmp(request_host,with_port)))
                destination=dns;
        }
    }
    /* Preserve explicitly used mDNS names. IP/missing/unrecognized Host values
     * use our own current address; never reflect an arbitrary Host header. */
    if(!destination&&cJSON_IsString(ip)&&ip->valuestring[0]&&strcmp(ip->valuestring,"0.0.0.0"))
        destination=ip->valuestring;
    if(!destination)return false;
    int n=snprintf(out,size,"https://%s/",destination);
    if(n<0||(size_t)n>=size) { out[0]=0;return false; }
    return true;
}
