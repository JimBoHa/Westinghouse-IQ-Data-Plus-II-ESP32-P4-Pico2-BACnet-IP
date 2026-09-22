#!/usr/bin/env python3
"""Read-only audit: extra diagnostics never enter the measurement database."""
import argparse
import datetime as dt
import json
import sqlite3
import statistics
from pathlib import Path
from decode_diagnostics import FIELD_SPECS as METER_SPECS
from live_diagnostics import FIELD_SPECS as DERIVED_SPECS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        report = {"checked_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "since_utc": args.since,
                  "new_attempts": conn.execute("SELECT kind,ok,count(*) FROM attempts WHERE started_utc>=? GROUP BY kind,ok", [args.since]).fetchall()}
        for table in ("readings", "derived_readings"):
            report[table + "_names"] = [x[0] for x in conn.execute("SELECT DISTINCT name FROM " + table + " ORDER BY name")]
        names = set(report["readings_names"] + report["derived_readings_names"])
        report["new_fields_found_in_database"] = sorted(names & {x["key"] for x in METER_SPECS + DERIVED_SPECS})
        stamps = [dt.datetime.fromisoformat(x[0]).timestamp() for x in conn.execute(
            "SELECT started_utc FROM attempts WHERE started_utc>=? AND kind='all_standard' ORDER BY attempt_id", [args.since])]
    intervals = [b-a for a,b in zip(stamps, stamps[1:])]
    if intervals:
        report["measurement_intervals"] = {"count": len(intervals), "minimum": min(intervals),
                                             "median": statistics.median(intervals), "maximum": max(intervals)}
    report["poll_health"] = json.loads((args.state / "poll_health.json").read_text())
    report["passed"] = bool(report["new_attempts"]) and not report["new_fields_found_in_database"] and all(x[0] == "all_standard" for x in report["new_attempts"])
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
