#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define IQ_UF2_MAX_BYTES (1024u*1024u)
bool iq_uf2_validate(const uint8_t *data,size_t length,size_t *skip,char *error,size_t size);
bool iq_uf2_validate_version(const uint8_t *data,size_t length,size_t *skip,
    char version[16],char *error,size_t size);
