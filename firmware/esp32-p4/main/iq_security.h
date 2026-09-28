#pragma once
#include <stdbool.h>
#include <stddef.h>
#include "cJSON.h"
#include "esp_http_server.h"
void iq_security_init(const char *token, const char *mac);
void iq_security_rotate(const char *token);
const char *iq_security_certificate(void);
const char *iq_security_private_key(void);
cJSON *iq_security_json(void);
cJSON *iq_security_challenge(void);
cJSON *iq_security_pair(const char *nonce);
bool iq_security_authorize(httpd_req_t *request);
bool iq_security_body(httpd_req_t *request, const void *body, size_t size);
bool iq_security_image(httpd_req_t *request, const char *target);
