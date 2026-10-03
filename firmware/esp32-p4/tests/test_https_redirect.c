#include "iq_https_redirect.h"
#include <assert.h>
#include <string.h>

static void check(const cJSON *status,const char *host,const char *expected)
{
    char location[96];
    assert(iq_https_location(status,host,location,sizeof(location)));
    assert(!strcmp(location,expected));
}
int main(void)
{
    const char *states[]={
        "{\"hostname\":\"iqdata-aabbcc\",\"ethernet\":{\"ip\":\"192.168.10.42\"}}",
        "{\"hostname\":\"iqdata-aabbcc\",\"ip\":\"192.168.10.42\"}"
    };
    for(unsigned i=0;i<2;i++) {
        cJSON *s=cJSON_Parse(states[i]);assert(s);
        check(s,"192.168.10.42","https://192.168.10.42/");
        check(s,"192.168.10.42:80","https://192.168.10.42/");
        check(s,NULL,"https://192.168.10.42/");
        check(s,"","https://192.168.10.42/");
        check(s,"iqdata-aabbcc.local","https://iqdata-aabbcc.local/");
        check(s,"IQDATA-AABBCC.LOCAL:80","https://iqdata-aabbcc.local/");
        check(s,"attacker.invalid","https://192.168.10.42/");
        check(s,"iqdata-aabbcc.local.attacker.invalid","https://192.168.10.42/");
        check(s,"iqdata-aabbcc.local@attacker.invalid","https://192.168.10.42/");
        check(s,"iqdata-aabbcc.local\r\nX-Injected: yes","https://192.168.10.42/");
        char tiny[8]="filled";
        assert(!iq_https_location(s,NULL,tiny,sizeof(tiny))&&tiny[0]==0);
        assert(!iq_https_location(s,NULL,NULL,0));
        cJSON_Delete(s);
    }
    cJSON *s=cJSON_Parse("{\"hostname\":\"iqdata-aabbcc\",\"ethernet\":{\"ip\":\"0.0.0.0\"}}");
    char location[96];assert(!iq_https_location(s,"192.168.10.42",location,sizeof(location)));
    cJSON_Delete(s);
    assert(!iq_https_location(NULL,NULL,location,sizeof(location)));
    return 0;
}
