#pragma once
/* Bounded snapshots only: these counters never authorize meter data. */
#define IQ_PIO_DIAGNOSTICS(X) \
    X(CPU_CLOCK_RISES,cpu_clock_rises) \
    X(CPU_RW_FALLS,cpu_rw_falls) \
    X(CPU_RW_RISES,cpu_rw_rises) \
    X(REQUEST_CLOCK_RISES,last_request_cpu_clock_rises) \
    X(REQUEST_RW_FALLS,last_request_cpu_rw_falls) \
    X(REQUEST_RW_RISES,last_request_cpu_rw_rises) \
    X(REQUEST_US,last_request_us) \
    X(LAST_ACTIVITY_US,last_activity_us) \
    X(END_PINS,end_pins_before_release) \
    X(PADOE,pio_output_enables_before_release) \
    X(READER_PC,reader_pc) \
    X(READER_INSTR,reader_instruction) \
    X(READER_TX,reader_tx_fifo_level) \
    X(GUARD_PC,guard_pc) \
    X(INT_PC,int_clear_pc) \
    X(WRITER_PC,writer_pc) \
    X(PIO0_CTRL,pio0_ctrl) \
    X(PIO1_CTRL,pio1_ctrl) \
    X(PIO0_IRQ,pio0_irq) \
    X(PIO1_IRQ,pio1_irq) \
    X(PIO0_FDEBUG,pio0_fdebug) \
    X(PIO1_FDEBUG,pio1_fdebug) \
    X(READER_OFFSET,reader_program_offset) \
    X(GUARD_OFFSET,guard_program_offset) \
    X(INT_OFFSET,int_clear_program_offset) \
    X(WRITER_OFFSET,writer_program_offset) \
    X(READER_EXECCTRL,reader_execctrl) \
    X(READER_PINCTRL,reader_pinctrl) \
    X(PIO0_GPIOBASE,pio0_gpio_base) \
    X(PIO1_GPIOBASE,pio1_gpio_base) \
    X(DATA_CTRL,data_ctrl_before_release) \
    X(INT_CTRL,int_ctrl_before_release) \
    X(REQUEST_PINS,last_request_pins) \
    X(REQUEST_PADOE,last_request_output_enables) \
    X(READER_X,reader_remaining_x) \
    X(READER_OSR,reader_osr) \
    X(FIRST_INT_RISE_US,first_int_rise_us) \
    X(FIRST_INT_FALL_US,first_int_fall_us) \
    X(FIRST_DATA_FALL_US,first_data_fall_us)
typedef enum {
#define IQ_PIO_ENUM(symbol,name) IQ_PIO_##symbol,
    IQ_PIO_DIAGNOSTICS(IQ_PIO_ENUM)
#undef IQ_PIO_ENUM
    IQ_PIO_DIAGNOSTIC_COUNT
} iq_pio_diagnostic_index_t;
_Static_assert(IQ_PIO_DIAGNOSTIC_COUNT<=64,"Diagnostic presence mask capacity");
