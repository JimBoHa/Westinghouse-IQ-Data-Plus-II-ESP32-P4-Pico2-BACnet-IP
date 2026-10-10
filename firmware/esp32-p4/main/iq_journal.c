#include "iq_journal.h"
#include <string.h>
#define RECORD_MAGIC 0x52455149u
#define COMMIT_MAGIC 0x43455149u
_Static_assert(sizeof(iq_journal_record_t)==IQ_JOURNAL_RECORD_BYTES,"Journal record size");
_Static_assert(offsetof(iq_journal_record_t,crc)==248,"Journal format offsets");
typedef struct { char magic[8];uint32_t version,bytes,record_bytes,reserved,crc,commit; } header_t;
_Static_assert(sizeof(header_t)==32,"Journal header size");
static uint32_t checksum(const void *data,size_t bytes)
{
    const uint8_t *p=data;uint32_t crc=UINT32_MAX;
    while(bytes--) { crc^=*p++;for(unsigned i=0;i<8;i++)crc=(crc>>1)^((crc&1)?0xedb88320u:0); }
    return ~crc;
}
static bool blank(const void *data,size_t bytes)
{ const uint8_t *p=data;while(bytes--)if(*p++!=0xff)return false;return true; }
static bool valid(const iq_journal_record_t *r)
{
    return r->magic==RECORD_MAGIC&&r->version==1&&r->bytes==sizeof(*r)&&r->sequence>0&&
        r->commit==COMMIT_MAGIC&&r->crc==checksum(r,offsetof(iq_journal_record_t,crc))&&
        r->component[15]==0&&r->event[23]==0&&r->detail[127]==0&&r->firmware[15]==0&&r->source[15]==0;
}
static size_t position(unsigned slot) { return IQ_JOURNAL_SECTOR+(size_t)slot*IQ_JOURNAL_RECORD_BYTES; }
static bool read_slot(iq_journal_t *j,unsigned slot,iq_journal_record_t *r)
{ return j->io.read(j->io.context,position(slot),r,sizeof(*r)); }
static bool failed(iq_journal_t *j) { j->ready=false;return false; }
bool iq_journal_open(iq_journal_t *j,const iq_journal_io_t *io)
{
    if(!j||!io||!io->read||!io->write||!io->erase||io->bytes<IQ_JOURNAL_SECTOR*3||
       io->bytes>IQ_JOURNAL_BYTES||io->bytes%IQ_JOURNAL_SECTOR)return false;
    *j=(iq_journal_t){.io=*io,.capacity=(io->bytes-IQ_JOURNAL_SECTOR)/IQ_JOURNAL_RECORD_BYTES};
    header_t expected={.magic={'I','Q','E','R','R','O','R','S'},.version=1,.bytes=io->bytes,
        .record_bytes=IQ_JOURNAL_RECORD_BYTES,.commit=COMMIT_MAGIC};
    expected.crc=checksum(&expected,offsetof(header_t,crc));
    header_t header;
    if(!io->read(io->context,0,&header,sizeof(header)))return false;
    if(memcmp(&header,&expected,sizeof(header))) {
        /* A interrupted first write may only have cleared bits present in our
         * exact header. All remaining bytes must still be erased. */
        const uint8_t *got=(const uint8_t*)&header,*want=(const uint8_t*)&expected;
        for(unsigned i=0;i<sizeof(header);i++)if((got[i]&want[i])!=want[i])return false;
        uint8_t block[256];
        for(size_t at=sizeof(header);at<io->bytes;) {
            size_t n=io->bytes-at;if(n>sizeof(block))n=sizeof(block);
            if(!io->read(io->context,at,block,n)||!blank(block,n))return false;
            at+=n;
        }
        if(!io->write(io->context,0,&expected,offsetof(header_t,commit))||
           !io->write(io->context,offsetof(header_t,commit),&expected.commit,sizeof(expected.commit)))return false;
        if(!io->read(io->context,0,&header,sizeof(header))||memcmp(&header,&expected,sizeof(header)))return false;
    }
    iq_journal_record_t r;
    for(unsigned slot=0;slot<j->capacity;slot++) {
        if(!read_slot(j,slot,&r))return false;
        if(valid(&r)) {
            j->count++;
            if(r.sequence>j->last_sequence) { j->last_sequence=r.sequence;j->last_slot=slot; }
        } else if(!blank(&r,sizeof(r)))j->invalid_records++;
    }
    j->next_slot=j->last_sequence?(j->last_slot+1)%j->capacity:0;
    /* Never program over a torn record. Skip to a clean slot, or to a sector
     * boundary where only the oldest sector can be recycled. */
    while(j->next_slot%(IQ_JOURNAL_SECTOR/IQ_JOURNAL_RECORD_BYTES)) {
        if(!read_slot(j,j->next_slot,&r))return false;
        if(blank(&r,sizeof(r)))break;
        j->next_slot=(j->next_slot+1)%j->capacity;
    }
    j->ready=true;return true;
}
bool iq_journal_append(iq_journal_t *j,const iq_journal_record_t *input)
{
    if(!j||!j->ready||!input||j->last_sequence==UINT64_MAX)return false;
    const unsigned per_sector=IQ_JOURNAL_SECTOR/IQ_JOURNAL_RECORD_BYTES;
    if(j->next_slot%per_sector==0) {
        unsigned removed=0,invalid_removed=0;iq_journal_record_t old;
        for(unsigned i=0;i<per_sector;i++) {
            if(!read_slot(j,j->next_slot+i,&old))return failed(j);
            if(valid(&old))removed++;
            else if(!blank(&old,sizeof(old)))invalid_removed++;
        }
        if(!j->io.erase(j->io.context,position(j->next_slot),IQ_JOURNAL_SECTOR))return failed(j);
        j->count=j->count>=removed?j->count-removed:0;
        j->invalid_records=j->invalid_records>=invalid_removed?j->invalid_records-invalid_removed:0;
    }
    iq_journal_record_t r=*input;
    r.magic=RECORD_MAGIC;r.version=1;r.bytes=sizeof(r);r.sequence=j->last_sequence+1;
    r.component[15]=r.event[23]=r.detail[127]=r.firmware[15]=r.source[15]=0;
    if(!r.occurrences)r.occurrences=1;
    r.crc=checksum(&r,offsetof(iq_journal_record_t,crc));r.commit=COMMIT_MAGIC;
    const size_t offset=position(j->next_slot);
    if(!j->io.write(j->io.context,offset,&r,offsetof(iq_journal_record_t,commit))||
       !j->io.write(j->io.context,offset+offsetof(iq_journal_record_t,commit),&r.commit,sizeof(r.commit)))return failed(j);
    iq_journal_record_t verify;
    if(!read_slot(j,j->next_slot,&verify)||memcmp(&verify,&r,sizeof(r)))return failed(j);
    j->last_sequence=r.sequence;j->last_slot=j->next_slot;
    j->next_slot=(j->next_slot+1)%j->capacity;j->count++;return true;
}
bool iq_journal_page(iq_journal_t *j,uint64_t before,iq_journal_record_t *records,
    unsigned limit,unsigned *count,bool *more)
{
    if(!j||!j->ready||!records||!limit||!count||!more)return false;
    *count=0;*more=false;
    for(unsigned i=0;i<j->capacity;i++) {
        unsigned slot=(j->last_slot+j->capacity-i)%j->capacity;iq_journal_record_t r;
        if(!read_slot(j,slot,&r))return failed(j);
        if(!valid(&r)||(before&&r.sequence>=before))continue;
        if(*count==limit) { *more=true;break; }
        records[(*count)++]=r;
    }
    return true;
}
