#pragma once
#include "cJSON.h"
#include <stdbool.h>
#include <stddef.h>
/* status is the local device's trusted status, never client-supplied JSON. */
bool iq_https_location(const cJSON *status,const char *request_host,char *out,size_t size);
