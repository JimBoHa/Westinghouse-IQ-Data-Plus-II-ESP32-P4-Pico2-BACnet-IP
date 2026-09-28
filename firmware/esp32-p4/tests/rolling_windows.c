/* Accelerated synthetic time only; this is not a physical meter soak test. */
#include "iq_model.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>

static void sample(iq_model_t *model, uint64_t now, unsigned energy)
{
    uint32_t words[] = {0x7e9, 0x400064, 0x40006e, 0x40005a, 0,
        0x4001e0, 0x4001ea, 0x4001f4, 0x40010a, 0x40010a, 0x40010a,
        0x6203e8, 0x400000, 0x400000, 0x40003c, 0x400000, 0xfeffa1, energy};
    for (unsigned i=0; i<18; ++i) words[i] <<= 1;
    assert(iq_model_accept(model, IQ_STANDARD, words, 18, now, .5, 0, 0));
}

int main(void)
{
    iq_model_t *model=iq_model_create();
    assert(model);
    iq_value_t values[IQ_POINT_COUNT];
    uint64_t now=1000;
    /* 90,001 samples exceed both ring lengths. A nonintegral 1.05 s cadence
     * also exercises interpolation at the hour/day window boundaries. */
    for (unsigned i=0; i<=90000; ++i) {
        now=1000+(uint64_t)i*1050;
        sample(model, now, 1000+i);
        if (i%1000==0 || i==857 || i==858 || i==3428 || i==3429 ||
            i==82285 || i==82286) {
            iq_model_snapshot(model, now, values);
            bool demand=i*1050ULL>=900000, hour=i*1050ULL>=3600000,
                 day=i*1050ULL>=86400000;
            assert(values[IQ_ROLLING_15MIN_DEMAND_kW].valid==demand);
            assert(values[IQ_ROLLING_HOUR_ENERGY_kWh].valid==hour);
            assert(values[IQ_ROLLING_DAY_ENERGY_kWh].valid==day);
            assert(values[IQ_ROLLING_DEMAND_READY].value==demand);
            assert(values[IQ_ROLLING_HOUR_ENERGY_READY].value==hour);
            assert(values[IQ_ROLLING_DAY_ENERGY_READY].value==day);
            if (demand) assert(fabs(values[IQ_ROLLING_15MIN_DEMAND_kW].value-100)<1e-7);
            if (hour) assert(fabs(values[IQ_ROLLING_HOUR_ENERGY_kWh].value-3600/1.05)<1e-7);
            if (day) assert(fabs(values[IQ_ROLLING_DAY_ENERGY_kWh].value-86400/1.05)<1e-7);
        }
    }
    /* Counter decrease invalidates energy continuity, not valid power. */
    sample(model, now+=1050, 1);
    iq_model_snapshot(model, now, values);
    assert(values[IQ_ROLLING_15MIN_DEMAND_kW].valid);
    assert(!values[IQ_ROLLING_HOUR_ENERGY_kWh].valid);
    assert(!values[IQ_ROLLING_DAY_ENERGY_kWh].valid);
    /* A transport failure breaks power continuity even with a short gap. */
    iq_model_fail(model, IQ_STANDARD, now+100, .5, 8, 0);
    sample(model, now+=1050, 2);
    iq_model_snapshot(model, now, values);
    assert(!values[IQ_ROLLING_15MIN_DEMAND_kW].valid);
    for (unsigned i=0; i<858; ++i) sample(model, now+=1050, 3+i);
    iq_model_snapshot(model, now, values);
    assert(values[IQ_ROLLING_15MIN_DEMAND_kW].valid);
    assert(!values[IQ_ROLLING_HOUR_ENERGY_kWh].valid);
    iq_model_destroy(model);
    puts("PASS accelerated day, wrap, interpolation, counter reset, failure and recovery");
    return 0;
}
