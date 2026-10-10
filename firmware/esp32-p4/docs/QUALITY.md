# Point quality

The 106 AI and 92 BI identifiers, units, values, and read-only behavior remain
unchanged. `/api/points` adds `quality_reason` and `quality_detail`; the dashboard
shows the explanation for unavailable values. BACnet Reliability now distinguishes
transport problems from calculations that cannot produce a result:

| Reason | BACnet Reliability |
|---|---|
| No response yet, failed transaction, stale value | communication-failure (12) |
| Invalid meter operand, e.g. zero frequency in a frequency calculation | process-error (8) |
| Undefined zero-load calculation, incomplete history, missing nominal settings, unsupported value, absent trip snapshot | unreliable-other (7) |
| Valid, fresh value | no-fault-detected (0) |

Unavailable values retain the BACnet FAULT status flag and the last present value;
the dashboard hides the stale/undefined number. A communication failure or stale
source overrides a previous calculation reason. Recovery restores quality when
inputs and the required history are available.

This does not rescale meter values, substitute nominal frequency, or certify
electrical accuracy. A protocol-valid zero current or zero frequency remains
available as the raw meter reading; calculations that need a nonzero denominator
or a positive frequency explain why they are unavailable. Rolling histories
still start empty after reboot and are not persisted.
