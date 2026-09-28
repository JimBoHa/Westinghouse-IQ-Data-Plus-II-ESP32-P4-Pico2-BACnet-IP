#include "iq_health.h"
bool iq_health_sample(iq_health_gate_t *gate, uint64_t elapsed_ms, bool healthy)
{
    if (!gate || elapsed_ms < 10000 || elapsed_ms < gate->last_sample ||
        (gate->last_sample && elapsed_ms - gate->last_sample < 1000)) return false;
    gate->last_sample = elapsed_ms;
    gate->consecutive = healthy ? gate->consecutive + (gate->consecutive < 5) : 0;
    return gate->consecutive >= 5;
}
#ifdef ESP_PLATFORM
#include <stdatomic.h>
#include "iq_diagnostics.h"
#include "esp_ota_ops.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
static atomic_bool accepted;
static atomic_uint samples;
static esp_timer_handle_t deadline;
static int64_t started;
static bool (*health_check)(void);
static void expired(void *unused)
{
    (void)unused;
    if (!atomic_load(&accepted)) { iq_event("ota","rollback_deadline",1,"Startup acceptance deadline expired");esp_restart(); }
}
void iq_health_begin(void)
{
    started = esp_timer_get_time();
    esp_ota_img_states_t state;
    const esp_partition_t *running = esp_ota_get_running_partition();
    bool pending = running && esp_ota_get_state_partition(running, &state) == ESP_OK &&
        state == ESP_OTA_IMG_PENDING_VERIFY;
    atomic_store(&accepted, !pending);
    if (pending) {
        esp_timer_create_args_t args = {.callback=expired,.name="iq_boot_deadline"};
        ESP_ERROR_CHECK(esp_timer_create(&args, &deadline));
        ESP_ERROR_CHECK(esp_timer_start_once(deadline, 60000000));
    }
}
static void validate(void *unused)
{
    (void)unused;
    iq_health_gate_t gate = {0};
    while (!atomic_load(&accepted)) {
        bool ready = iq_health_sample(&gate, (esp_timer_get_time()-started)/1000,
            health_check && health_check());
        atomic_store(&samples, gate.consecutive);
        if (ready) {
            ESP_ERROR_CHECK(esp_ota_mark_app_valid_cancel_rollback());
            atomic_store(&accepted, true);
            iq_event("ota","accepted",0,"Five consecutive startup health samples passed");
            (void)esp_timer_stop(deadline);
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    vTaskDelete(NULL);
}
void iq_health_start(bool (*healthy)(void))
{
    health_check = healthy;
    if (!atomic_load(&accepted))
        configASSERT(xTaskCreate(validate,"iq_boot_health",4096,NULL,5,NULL)==pdPASS);
}
bool iq_health_accepted(void) { return atomic_load(&accepted); }
cJSON *iq_health_json(void)
{
    cJSON *j=cJSON_CreateObject();
    cJSON_AddBoolToObject(j,"accepted",iq_health_accepted());
    cJSON_AddNumberToObject(j,"consecutive_samples",atomic_load(&samples));
    cJSON_AddNumberToObject(j,"required_samples",5);
    cJSON_AddNumberToObject(j,"deadline_seconds",60);
    return j;
}
#endif
