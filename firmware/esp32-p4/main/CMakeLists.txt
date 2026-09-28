set(app_source "app_main.c")
if(IQ_RECOVERY_BUILD)
    set(app_source "app_recovery.c")
endif()
idf_component_register(SRCS ${app_source} "iq_points.c" "iq_decode.c" "iq_model.c"
    "iq_transport.c" "iq_usb.c" "iq_config.c" "iq_web.c" "iq_health.c" "iq_uf2.c" "iq_pico_update.c" "gateway_bacnet.c" "bip_port.c"
    INCLUDE_DIRS "." PRIV_REQUIRES bacnet_stack esp_eth esp_netif esp_event esp_timer
    esp_http_server nvs_flash json lwip esp_app_format esp_driver_uart esp_driver_usb_serial_jtag
    usb esp_hw_support esp_system app_update esp_partition mbedtls bootloader_support)
if(CONFIG_SECURE_BOOT OR CONFIG_SECURE_FLASH_ENC_ENABLED OR
   CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK OR CONFIG_SECURE_DISABLE_ROM_DL_MODE OR
   CONFIG_SECURE_ENABLE_SECURE_ROM_DL_MODE)
    message(FATAL_ERROR "This development port must not provision hardware security/eFuses")
endif()
execute_process(COMMAND git describe --always --dirty --abbrev=12
    WORKING_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}" OUTPUT_VARIABLE revision
    OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
target_compile_definitions(${COMPONENT_LIB} PRIVATE IQ_SOURCE_REVISION="${revision}")
if(IQ_RECOVERY_BUILD)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE IQ_RECOVERY_BUILD=1)
endif()
