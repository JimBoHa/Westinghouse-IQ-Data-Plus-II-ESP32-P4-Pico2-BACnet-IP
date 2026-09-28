#include "iq_health.h"
#include <assert.h>
int main(void)
{
    iq_health_gate_t g={0};
    assert(!iq_health_sample(&g,9999,true));
    assert(!iq_health_sample(&g,10000,true));
    for(unsigned t=10001;t<11000;t++) assert(!iq_health_sample(&g,t,true));
    assert(g.consecutive==1);
    assert(!iq_health_sample(&g,11000,true));
    assert(!iq_health_sample(&g,12000,false));
    assert(g.consecutive==0);
    for(unsigned t=13000;t<17000;t+=1000) assert(!iq_health_sample(&g,t,true));
    assert(iq_health_sample(&g,17000,true));
    assert(g.consecutive==5);
    assert(!iq_health_sample(&g,16000,true));
    assert(!iq_health_sample(&g,18000,false));
    return 0;
}
