/* Fault-injected NOR-flash emulator, never included in device firmware. */
#include "iq_journal.h"
#include "iq_error_log.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
typedef struct { uint8_t *data;size_t size;long budget;unsigned writes,erases;bool read_failure; } flash_t;
static bool read_flash(void *context,size_t at,void *data,size_t bytes)
{
    flash_t *f=context;assert(at<=f->size&&bytes<=f->size-at);
    if(f->read_failure)return false;
    memcpy(data,f->data+at,bytes);return true;
}
static bool write_flash(void *context,size_t at,const void *data,size_t bytes)
{
    flash_t *f=context;const uint8_t *source=data;assert(at<=f->size&&bytes<=f->size-at);f->writes++;
    for(size_t i=0;i<bytes;i++) {
        if(!f->budget)return false;
        if(f->budget>0)f->budget--;
        assert((f->data[at+i]&source[i])==source[i]);f->data[at+i]&=source[i];
    }
    return true;
}
static bool erase_flash(void *context,size_t at,size_t bytes)
{
    flash_t *f=context;assert(at>=IQ_JOURNAL_SECTOR&&at%IQ_JOURNAL_SECTOR==0&&bytes==IQ_JOURNAL_SECTOR);
    assert(at<=f->size&&bytes<=f->size-at);f->erases++;
    for(size_t i=0;i<bytes;i++) {
        if(!f->budget)return false;
        if(f->budget>0)f->budget--;
        f->data[at+i]=0xff;
    }
    return true;
}
static void clear(flash_t *f)
{ memset(f->data,0xff,f->size);f->budget=-1;f->writes=f->erases=0;f->read_failure=false; }
static iq_journal_io_t io_for(flash_t *f)
{ return (iq_journal_io_t){read_flash,write_flash,erase_flash,f,f->size}; }
static iq_journal_record_t record(void)
{
    return (iq_journal_record_t){.boot_id=0x12345678,.uptime_ms=1234,.utc_ms=1800000000000LL,
        .code=401,.occurrences=1,.component="management",.event="auth_rejected",.detail="Authentication rejected",
        .firmware="test",.source="123456789abc"};
}
static void verify_page(iq_journal_t *j,unsigned expected,uint64_t newest)
{
    iq_journal_record_t page[7];unsigned count,total=0;bool more;uint64_t before=0,previous=newest+1;
    do {
        assert(iq_journal_page(j,before,page,7,&count,&more));
        for(unsigned i=0;i<count;i++) { assert(page[i].sequence<previous);previous=page[i].sequence;total++; }
        if(count)before=page[count-1].sequence;
        assert(!more||count==7);
    } while(more);
    assert(total==expected);
}
static void capacity_and_restart(void)
{
    flash_t f={.size=IQ_JOURNAL_BYTES};f.data=malloc(f.size);assert(f.data);clear(&f);
    iq_journal_io_t io=io_for(&f);iq_journal_t j;assert(iq_journal_open(&j,&io));
    assert(j.capacity==4080);iq_journal_record_t r=record();
    for(unsigned i=0;i<j.capacity;i++) { r.uptime_ms=i;assert(iq_journal_append(&j,&r)); }
    assert(j.count==4080);verify_page(&j,4080,4080);
    /* Rotation erases exactly the oldest sector, keeping the recent record. */
    assert(iq_journal_append(&j,&r));assert(j.count==4065&&j.last_sequence==4081);
    iq_journal_t restarted;assert(iq_journal_open(&restarted,&io));
    assert(restarted.count==4065&&restarted.last_sequence==4081);verify_page(&restarted,4065,4081);
    assert(iq_journal_append(&restarted,&r));assert(restarted.last_sequence==4082);
    /* Corruption is skipped; intact records remain readable after restart. */
    f.data[IQ_JOURNAL_SECTOR+10]^=1;
    assert(iq_journal_open(&j,&io));assert(j.count==4065&&j.invalid_records==1);
    verify_page(&j,4065,4082);
    unsigned count;bool more;
    f.read_failure=true;assert(!iq_journal_page(&j,0,&r,1,&count,&more));assert(!j.ready);
    free(f.data);
}
static void interrupted_writes(void)
{
    flash_t f={.size=3*IQ_JOURNAL_SECTOR};f.data=malloc(f.size);assert(f.data);
    iq_journal_io_t io=io_for(&f);iq_journal_record_t r=record();iq_journal_t j;
    /* Interrupt each byte of initial ownership header; retry must recover. */
    for(unsigned cut=0;cut<32;cut++) {
        clear(&f);f.budget=cut;assert(!iq_journal_open(&j,&io));
        f.budget=-1;assert(iq_journal_open(&j,&io));assert(iq_journal_append(&j,&r));
    }
    /* Every body/commit byte can be interrupted without accepting a torn event. */
    for(unsigned cut=0;cut<256;cut++) {
        clear(&f);assert(iq_journal_open(&j,&io));assert(iq_journal_append(&j,&r));
        f.budget=cut;assert(!iq_journal_append(&j,&r));assert(!j.ready);
        f.budget=-1;assert(iq_journal_open(&j,&io));assert(j.count==1&&j.last_sequence==1);
        assert(iq_journal_append(&j,&r));verify_page(&j,2,2);
    }
    /* A partial erase during wrap may lose old records, never the newest sector. */
    for(unsigned cut=0;cut<4096;cut+=127) {
        clear(&f);assert(iq_journal_open(&j,&io));
        for(unsigned i=0;i<32;i++)assert(iq_journal_append(&j,&r));
        f.budget=cut;assert(!iq_journal_append(&j,&r));f.budget=-1;
        assert(iq_journal_open(&j,&io));assert(j.last_sequence==32&&j.count>=16);
        assert(iq_journal_append(&j,&r));assert(j.count==17);verify_page(&j,17,33);
    }
    clear(&f);f.data[1000]=0;assert(!iq_journal_open(&j,&io));
    assert(f.writes==0&&f.erases==0&&f.data[1000]==0); /* Never erase unknown contents. */
    free(f.data);
}
typedef struct { unsigned records;uint64_t occurrences; } sink_t;
static bool emit(void *context,const iq_journal_record_t *r)
{ sink_t *s=context;s->records++;s->occurrences+=r->occurrences;return true; }
static void aggregation(void)
{
    assert(!iq_error_should_persist("clock","synchronized",0));
    assert(!iq_error_should_persist("system","boot",3));
    assert(iq_error_should_persist("system","unexpected_reset",3));
    assert(iq_error_should_persist("meter","transaction_failed",1));
    iq_error_policy_t p={0};iq_journal_record_t r=record();sink_t sink={0};
    assert(iq_error_policy_submit(&p,&r,0,emit,&sink));assert(sink.records==1);
    for(unsigned i=1;i<60000;i++)assert(iq_error_policy_submit(&p,&r,i,emit,&sink));
    assert(sink.records==1&&p.coalesced==59999);
    assert(iq_error_policy_flush(&p,60000,false,emit,&sink));
    assert(sink.records==2&&sink.occurrences==60000);
    assert(iq_error_policy_submit(&p,&r,60001,emit,&sink));
    assert(iq_error_policy_flush(&p,60002,true,emit,&sink));
    assert(sink.records==3&&sink.occurrences==60001);
    for(unsigned i=0;i<40;i++) {
        r.code=1000+i;assert(iq_error_policy_submit(&p,&r,60003+i,emit,&sink));
        assert(iq_error_policy_submit(&p,&r,60003+i,emit,&sink));
    }
    assert(iq_error_policy_flush(&p,61000,true,emit,&sink));assert(sink.occurrences==60081);
}
int main(void)
{
    capacity_and_restart();interrupted_writes();aggregation();
    puts("PASS 1 MiB bound, rotation, restart, CRC corruption, all torn-write bytes, partial erases and repeat aggregation");
}
