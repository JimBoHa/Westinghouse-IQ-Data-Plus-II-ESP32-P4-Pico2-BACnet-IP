#!/usr/bin/env python3
"""Compare live derived state against its exact stored electrical sample."""
import argparse
import json
import math
import sqlite3
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--database", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    state = json.loads((args.state / "live_diagnostics.json").read_text())
    derived = state["readings"]
    stamp = derived["VLL_IMBALANCE_pct"]["observed_utc"]
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT r.name,r.value,r.valid FROM readings r JOIN attempts a USING(attempt_id) WHERE a.observed_utc=? AND a.kind='all_standard' AND a.ok=1", [stamp]).fetchall()
    assert rows, "The exact source observation is absent from the database"
    values = {key: value for key,value,valid in rows if valid}
    volts = [values[k] for k in ("VAB", "VBC", "VCA")]
    amps = [values[k] for k in ("IA", "IB", "IC")]
    nominal_v = derived["CONFIG_NOMINAL_LL_V"]["value"]
    nominal_f = derived["CONFIG_FREQUENCY_Hz"]["value"]
    expected = {
        "VLL_IMBALANCE_pct": 100*max(abs(x-sum(volts)/3) for x in volts)/(sum(volts)/3),
        "CURRENT_IMBALANCE_pct": 100*max(abs(x-sum(amps)/3) for x in amps)/(sum(amps)/3),
        "HIGHEST_CURRENT_A": max(amps), "HIGHEST_CURRENT_PHASE": amps.index(max(amps))+1,
        "LOW_PF_ACTIVE": abs(values["PF"]) < derived["LOW_PF_THRESHOLD"]["value"],
        "FREQUENCY_EXCURSION_ACTIVE": abs(values["FREQUENCY_Hz"]-nominal_f) > derived["FREQUENCY_TOLERANCE_Hz"]["value"],
        "VOLTAGE_EXCURSION_ACTIVE": any(abs(x-nominal_v) > nominal_v*derived["VOLTAGE_TOLERANCE_pct"]["value"]/100 for x in volts),
        "NOMINAL_VOLTAGE_V": nominal_v, "NOMINAL_FREQUENCY_Hz": nominal_f,
    }
    checks = {}
    for key,value in expected.items():
        item = derived[key]
        assert item["valid"] and not item["stale"] and state["quality"][key], key
        assert item["observed_utc"] == stamp, key
        actual = item["value"]
        good = actual == value if isinstance(value, bool) else math.isclose(actual, value, abs_tol=1e-9)
        checks[key] = {"expected": value, "actual": actual, "passed": good}
    report = {"passed": all(x["passed"] for x in checks.values()), "source_observed_utc": stamp,
              "source_meter_readings": values, "checks": checks, "database_read_only": True}
    args.output.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
