#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "cJSON.h"
typedef struct {
    uint32_t schema,device_instance;
    char name[64],ip[16],mask[16],gateway[16];
    uint16_t bacnet_port,meter_address;
    bool commissioned,dhcp,poll_enabled;
} iq_config_t;
void iq_config_defaults(iq_config_t *settings);
bool iq_config_parse(const char *text,iq_config_t *out,char *error,size_t size);
bool iq_config_valid(const iq_config_t *settings,char *error,size_t size);
cJSON *iq_config_json(const iq_config_t *settings);
