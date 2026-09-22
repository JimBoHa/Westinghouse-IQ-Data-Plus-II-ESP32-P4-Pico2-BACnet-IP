#!/usr/bin/env python3
"""Import preserved USB poll reports without acquiring or retransmitting anything."""
import argparse
import json
from pathlib import Path

import host
from decode_meter import decode_request
from derived_power import add_derived
from poll_meter import extract_meter_words, journal_messages
from store_readings import ReadingStore


def import_reports(raw_dir, database):
    counts = {"inserted": 0, "already_present": 0, "failed_attempts": 0, "unfinished_skipped": 0}
    with ReadingStore(database) as store:
        for path in sorted(Path(raw_dir).glob("poll_*.json")):
            report = host.json_object(path.read_text())
            if not report.get("finished_utc"):
                counts["unfinished_skipped"] += 1
                continue
            parts = report.get("command", "").split()
            if len(parts) != 5 or parts[0] != "transact" or not parts[1].endswith("_repeat"):
                raise ValueError(f"Unexpected preserved poll command: {path}")
            kind = parts[1][:-7]
            journal = path.with_name(path.name + ".jsonl")
            refs = [str(path.resolve()), str(journal.resolve())]
            attempt = {"kind": kind, "started_utc": report["started_utc"],
                       "finished_utc": report["finished_utc"], "ok": False,
                       "errors": [], "evidence_refs": refs, "raw_report": report}
            buffer = None
            try:
                words, evidence = extract_meter_words(report, journal_messages(journal), kind,
                                                       int(parts[2]), parts[3], int(parts[4]))
                decoded = add_derived(decode_request(kind, words))
                accepted = decoded.get("protocol_valid") if kind == "all_standard" else decoded.get("valid")
                if accepted is not True or decoded.get("complete") is not True:
                    raise ValueError(f"Invalid decoded response: {decoded.get('errors')}")
                attempt["ok"] = True
                buffer = {"last_good": {"observed_utc": report["finished_utc"],
                           "decoded": decoded, "evidence_refs": refs + evidence}}
            except Exception as error:
                attempt["errors"] = [f"{type(error).__name__}: {error}"]
                counts["failed_attempts"] += 1
            saved = store.record_attempt(attempt, buffer)
            counts["inserted" if saved["inserted"] else "already_present"] += 1
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_dir", type=Path)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    print(json.dumps(import_reports(args.raw_dir, args.database)))
