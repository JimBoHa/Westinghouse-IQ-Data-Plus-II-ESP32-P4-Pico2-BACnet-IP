"""Explicit P/Q estimates; never invent harmonic measurements."""
import math


def add_derived(decoded):
    readings = decoded.get("readings", {})
    p, q = readings.get("P_W", {}), readings.get("Q_var", {})
    if p.get("valid") is not True or q.get("valid") is not True:
        return decoded
    watts, vars_ = p.get("value"), q.get("value")
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (watts, vars_)):
        return decoded
    apparent = math.hypot(watts, vars_)
    metadata = {"source": "derived", "inputs": ["P_W", "Q_var"],
                "assumption": "P/Q power triangle estimate; excludes a separate distortion-power component",
                "valid": True}
    readings["S_PQ_estimate_VA"] = {**metadata, "value": apparent, "unit": "VA",
                                       "method": "sqrt(P_W**2 + Q_var**2)"}
    readings["PF_PQ_magnitude"] = {**metadata, "value": abs(watts) / apparent if apparent else None,
                                      "unit": "1", "valid": apparent > 0,
                                      "method": "abs(P_W) / sqrt(P_W**2 + Q_var**2)"}
    decoded["unavailable_quantities"] = {"THD": "No harmonic spectrum or documented THD response; cannot derive from RMS/P/Q/PF alone"}
    return decoded
