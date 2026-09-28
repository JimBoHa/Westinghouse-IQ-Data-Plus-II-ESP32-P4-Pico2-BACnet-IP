#pragma once
#include "iq_config.h"
#include "iq_model.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
typedef struct {
    bool connected,qualified,stopping,stopped,maintenance;
    uint32_t connections,failures,overflows;
    uint32_t devices_seen;
    uint16_t last_vid,last_pid;
    uint64_t heartbeat_ms,last_success_ms;
    char version[48],last_error[192];
} iq_usb_status_t;
void iq_usb_start(iq_model_t *model,SemaphoreHandle_t lock,const iq_config_t *config);
void iq_usb_status(iq_usb_status_t *out);
void iq_usb_stop(void);
bool iq_usb_maintenance_begin(char *error,size_t size);
void iq_usb_maintenance_end(void);
