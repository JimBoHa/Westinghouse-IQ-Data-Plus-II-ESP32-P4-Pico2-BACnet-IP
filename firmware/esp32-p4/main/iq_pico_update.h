#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
void iq_pico_update_init(void);
bool iq_pico_update(const uint8_t *uf2,size_t length,char *error,size_t size);
