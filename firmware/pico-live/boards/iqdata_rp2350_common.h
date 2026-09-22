// Common USB/flash platform definitions shared by SDK2.3.1 pico2 and pico2_w.
// No board-specific LED, radio, SMPS, UART or sensing pins are selected.
#ifndef _BOARDS_IQDATA_RP2350_COMMON_H
#define _BOARDS_IQDATA_RP2350_COMMON_H
pico_board_cmake_set(PICO_PLATFORM, rp2350)
#define PICO_RP2350A 1
#define PICO_BOOT_STAGE2_CHOOSE_W25Q080 1
#define PICO_FLASH_SPI_CLKDIV 2
pico_board_cmake_set_default(PICO_FLASH_SIZE_BYTES, (4 * 1024 * 1024))
#define PICO_FLASH_SIZE_BYTES (4 * 1024 * 1024)
pico_board_cmake_set_default(PICO_RP2350_A2_SUPPORTED, 1)
#define PICO_RP2350_A2_SUPPORTED 1
#endif
