#pragma once
#include "cJSON.h"
#include <stdbool.h>
#include <stddef.h>
cJSON *iq_status_json(void);
cJSON *iq_points_json(void);
bool iq_save_config(const char *json,char *error,size_t size);
bool iq_check_token(const char *token);
void iq_request_restart(void);
void iq_web_start(void);
