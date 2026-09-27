#include "writer_arm.h"
#include <assert.h>
#include <stdio.h>

static bool rw, fall_during_reset;
static unsigned resets, enables;
static bool rw_high(void) { return rw; }
static void reset(void) { ++resets; if (fall_during_reset) rw=false; }
static void enable(void) { ++enables; }
int main(void) {
    const writer_arm_ops_t ops={rw_high,reset,enable};
    /* Meter is already writing: do not initialize or enable capture mid-word. */
    assert(writer_arm(&ops)==WRITER_BUSY_BEFORE_RESET);
    assert(resets==0 && enables==0);
    rw=true; fall_during_reset=true;
    assert(writer_arm(&ops)==WRITER_BUSY_AFTER_RESET);
    assert(resets==1 && enables==0);
    /* A later clean boundary can arm the same stopped capture engine. */
    rw=true; fall_during_reset=false;
    assert(writer_arm(&ops)==WRITER_ARMED);
    assert(resets==2 && enables==1);
    /* Ownership loss is still reported after an earlier successful arm. */
    rw=false;
    assert(writer_arm(&ops)==WRITER_BUSY_BEFORE_RESET);
    assert(enables==1);
    puts("Writer arm tests passed: busy start, setup race, recovery, ownership loss");
    return 0;
}
