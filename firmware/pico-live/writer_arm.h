#pragma once
#include <stdbool.h>

typedef enum { WRITER_ARMED, WRITER_BUSY_BEFORE_RESET, WRITER_BUSY_AFTER_RESET } writer_arm_result_t;
typedef struct {
    bool (*rw_high)(void);
    void (*reset)(void);
    void (*enable)(void);
} writer_arm_ops_t;

/* Capture must not start halfway through a meter write. Recheck ownership
 * after setup; callers may retry only while establishing initial capture. */
static inline writer_arm_result_t writer_arm(const writer_arm_ops_t *ops) {
    if (!ops->rw_high()) return WRITER_BUSY_BEFORE_RESET;
    ops->reset();
    if (!ops->rw_high()) return WRITER_BUSY_AFTER_RESET;
    ops->enable();
    return WRITER_ARMED;
}
