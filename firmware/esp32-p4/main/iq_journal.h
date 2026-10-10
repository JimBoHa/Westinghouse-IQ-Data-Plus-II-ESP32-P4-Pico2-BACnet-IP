#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define IQ_JOURNAL_BYTES 0x100000u
#define IQ_JOURNAL_OFFSET 0x1100000u
#define IQ_JOURNAL_SECTOR 4096u
#define IQ_JOURNAL_RECORD_BYTES 256u
#define IQ_JOURNAL_CAPACITY ((IQ_JOURNAL_BYTES-IQ_JOURNAL_SECTOR)/IQ_JOURNAL_RECORD_BYTES)
/* Versioned little-endian on-flash layout. CRC and commit are written last.
 * Contains error context only, never meter measurement buffers or credentials. */
typedef struct {
    uint32_t magic;
    uint16_t version,bytes;
    uint64_t sequence,boot_id,uptime_ms;
    int64_t utc_ms;
    int32_t code;
    uint32_t occurrences;
    char component[16],event[24],detail[128],firmware[16],source[16];
    uint32_t crc,commit;
} iq_journal_record_t;
typedef struct {
    bool (*read)(void *context,size_t offset,void *data,size_t size);
    bool (*write)(void *context,size_t offset,const void *data,size_t size);
    bool (*erase)(void *context,size_t offset,size_t size);
    void *context;
    size_t bytes;
} iq_journal_io_t;
typedef struct {
    iq_journal_io_t io;
    uint64_t last_sequence;
    unsigned count,capacity,last_slot,next_slot,invalid_records;
    bool ready;
} iq_journal_t;
/* Claims only blank storage (or a compatible, interrupted initial header).
 * Refuses unrecognized data; never formats it automatically. */
bool iq_journal_open(iq_journal_t *journal,const iq_journal_io_t *io);
bool iq_journal_append(iq_journal_t *journal,const iq_journal_record_t *record);
/* Newest first; before==0 starts at newest. Bounded destination, exclusive cursor. */
bool iq_journal_page(iq_journal_t *journal,uint64_t before,iq_journal_record_t *records,
    unsigned limit,unsigned *count,bool *more);
