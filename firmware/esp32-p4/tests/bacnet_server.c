/* Native-only loopback BACnet test server. No meter fixtures in firmware. */
#include "gateway_bacnet.h"
#include <arpa/inet.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
static volatile sig_atomic_t running=1;
static void stop(int sig) { (void)sig; running=0; }
static uint64_t millis(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec*1000ull+t.tv_nsec/1000000; }
int main(int argc,char **argv)
{
    signal(SIGINT,stop);signal(SIGTERM,stop);
    uint16_t port=argc>1?(uint16_t)atoi(argv[1]):47842;
    gateway_bacnet_config_t c={.device_instance=75201,.device_name="IQData-Native-Test",
        .firmware_version="0.1.0-test",.vendor_id=999,.local_ip=inet_addr("127.0.0.1"),
        .netmask=inet_addr("255.0.0.0"),.udp_port=port,.database_revision=2};
    uint64_t start=millis();
    if(!gateway_bacnet_init(&c,start)) return 2;
    iq_model_t *model=iq_model_create();
    iq_value_t v[IQ_POINT_COUNT];
    puts("ready");fflush(stdout);
    while(running) {
        uint64_t now=millis();
        iq_model_snapshot(model,now-start,v);
        /* Explicit test-only point drives COV and quality transitions. */
        v[IQ_age_seconds]=(iq_value_t){(now-start)/1000.0,now,true};
        v[IQ_VAB]=(iq_value_t){230.0+(now-start)/2000,now,((now-start)/4000)%2==0};
        gateway_bacnet_update(v);gateway_bacnet_tick(now);gateway_bacnet_poll(5);
        usleep(10000);
    }
    gateway_bacnet_shutdown();iq_model_destroy(model);return 0;
}
