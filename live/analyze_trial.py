#!/usr/bin/env python3
"""Read-only independent analysis of one 24 MHz, one-byte sigrok active trial.

Digital levels describe sampled waveforms only. They do not identify which side
drove DATA, validate meter-originated payloads, or establish physical units.
"""
import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import time
import zipfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))
from analyze_sr import sample_blocks, session_info


def require(condition, message):
    if not condition:
        raise ValueError(message)


def stats(values, rate):
    values = np.asarray(values, dtype=np.int64)
    if not len(values):
        return {"count": 0}
    widths, counts = np.unique(values, return_counts=True)
    return {"count": len(values), "min_samples": int(values.min()),
            "median_samples": float(np.median(values)), "max_samples": int(values.max()),
            "min_seconds": int(values.min()) / rate,
            "median_seconds": float(np.median(values)) / rate,
            "max_seconds": int(values.max()) / rate,
            "sample_width_counts": {str(int(width)): int(count) for width, count in zip(widths, counts)}}


def words(bits):
    text = "".join(str(int(bit)) for bit in bits)
    return {"bits_chronological": text, "bit_count": len(text),
            "lsb_first_hex": hex(int(text[::-1], 2)) if text else None,
            "msb_first_hex": hex(int(text, 2)) if text else None}


def runs(times, after_values, initial, sample_count, bit):
    starts = np.r_[0, times].astype(np.int64)
    ends = np.r_[times, sample_count].astype(np.int64)
    levels = np.r_[(initial >> bit) & 1, (after_values >> bit) & 1]
    return [{"start_sample": int(start), "end_sample_exclusive": int(end),
             "samples": int(end - start), "level": int(level),
             "left_censored": index == 0, "right_censored": index == len(starts) - 1}
            for index, (start, end, level) in enumerate(zip(starts, ends, levels))]


def timed_interval(interval, rate):
    return {**interval, "start_seconds": interval["start_sample"] / rate,
            "end_seconds_exclusive": interval["end_sample_exclusive"] / rate,
            "duration_seconds": interval["samples"] / rate}


def selection(times, start, end):
    return slice(int(np.searchsorted(times, start, side="left")),
                 int(np.searchsorted(times, end, side="left")))


def region(times, start, end, rate):
    chosen = selection(times, start, end)
    return {"start_sample": int(start), "end_sample_exclusive": int(end),
            "duration_seconds": (int(end) - int(start)) / rate,
            "clock_rises": chosen.stop - chosen.start}


def analyze(path, bits, gap_us=100, max_events=2_000_000):
    event_times, event_before, event_after = [], [], []
    mask = sum(1 << bit for bit in bits.values())
    offset = count = 0
    previous = initial = None
    high_samples = {name: 0 for name in bits}
    with zipfile.ZipFile(path) as archive:
        require(len(archive.namelist()) == len(set(archive.namelist())), "Duplicate ZIP members")
        require(archive.testzip() is None, "ZIP CRC failure")
        info = session_info(archive)
        require(info["unit_size_bytes"] == 1, "This analyzer requires one byte per stored sample")
        require(info["samplerate_hz"] == 24_000_000, "This analyzer requires an actual 24 MHz sample rate")
        require(set(bits.values()) <= {probe["bit"] for probe in info["channels"]}, "Requested signal bit is not enabled")
        for block in sample_blocks(archive, info):
            if initial is None:
                initial = int(block[0])
            changed = np.empty(len(block), dtype=np.uint8)
            changed[0] = 0 if previous is None else int(block[0]) ^ previous
            changed[1:] = block[1:] ^ block[:-1]
            indexes = np.flatnonzero(changed & mask)
            count += len(indexes)
            require(count <= max_events, f"More than {max_events} selected-signal transitions; analysis incomplete, no absence claim allowed")
            if len(indexes):
                before = block[np.maximum(indexes - 1, 0)].copy()
                if indexes[0] == 0:
                    before[0] = previous
                event_times.append(indexes.astype(np.int64) + offset)
                event_before.append(before)
                event_after.append(block[indexes].copy())
            for name, bit in bits.items():
                high_samples[name] += int(np.count_nonzero(block & (1 << bit)))
            previous = int(block[-1])
            offset += len(block)
    require(offset == info["expected_samples"], "Streamed and stored sample counts differ")
    times = np.concatenate(event_times) if event_times else np.array([], dtype=np.int64)
    before = np.concatenate(event_before) if event_before else np.array([], dtype=np.uint8)
    after = np.concatenate(event_after) if event_after else np.array([], dtype=np.uint8)
    changes = before ^ after
    rate = info["samplerate_hz"]
    signals, signal_runs = {}, {}
    for name, bit in bits.items():
        selected = (changes & (1 << bit)) != 0
        signal_times = times[selected]
        signal_after = after[selected]
        signal_runs[name] = runs(signal_times, signal_after, initial, offset, bit)
        complete = [run for run in signal_runs[name] if not run["left_censored"] and not run["right_censored"]]
        rising_count = int(np.count_nonzero(signal_after & (1 << bit)))
        signals[name] = {"stored_bit": bit, "initial": (initial >> bit) & 1,
                         "final": (previous >> bit) & 1, "transitions": len(signal_times),
                         "rising": rising_count, "falling": len(signal_times) - rising_count,
                         "high_samples": high_samples[name], "high_fraction": high_samples[name] / offset,
                         "complete_low_widths": stats([run["samples"] for run in complete if not run["level"]], rate),
                         "complete_high_widths": stats([run["samples"] for run in complete if run["level"]], rate)}
    clock_bit, data_bit, rw_bit = bits["clock"], bits["data"], bits["rw"]
    clock_selected = (changes & (1 << clock_bit)) != 0
    clock_times = times[clock_selected]
    clock_before, clock_after = before[clock_selected], after[clock_selected]
    rising_selected = (clock_after & (1 << clock_bit)) != 0
    rise_times = clock_times[rising_selected]
    rise_before, rise_after = clock_before[rising_selected], clock_after[rising_selected]
    data_selected = (changes & (1 << data_bit)) != 0
    data_edges = []
    for sample, prior, current, change in zip(times[data_selected], before[data_selected], after[data_selected], changes[data_selected]):
        data_edges.append({"sample": int(sample), "seconds": int(sample) / rate,
                           "before_word_hex": hex(int(prior)), "after_word_hex": hex(int(current)),
                           "before_levels": {name: (int(prior) >> bit) & 1 for name, bit in bits.items()},
                           "after_levels": {name: (int(current) >> bit) & 1 for name, bit in bits.items()},
                           "changed_signals_same_sample": [name for name, bit in bits.items() if int(change) & (1 << bit)]})
    rw_low = []
    for raw in signal_runs["rw"]:
        if raw["level"]:
            continue
        interval = timed_interval(raw, rate)
        start, end = raw["start_sample"], raw["end_sample_exclusive"]
        chosen = selection(rise_times, start, end)
        selected_times = rise_times[chosen]
        prior, current = rise_before[chosen], rise_after[chosen]
        length = len(selected_times)
        flags = []
        if raw["left_censored"] or raw["right_censored"]:
            flags.append("acquisition_boundary_censors_rw_window")
        if length > 25:
            flags.append("more_than_25_clock_rises_malformed_candidate_write")
        elif 0 < length < 25:
            flags.append("fewer_than_25_clock_rises")
        same_sample = int(np.count_nonzero((prior ^ current) & (1 << data_bit)))
        if same_sample:
            flags.append("data_and_clock_change_in_same_sample_order_unresolved")
        at_start = bool(np.any(rise_times == start))
        at_return = bool(end < offset and np.any(rise_times == end))
        if at_start or at_return:
            flags.append("clock_and_rw_change_in_same_sample_order_unresolved")
        interval.update({"clock_rises": length, "first25_clock_rise_samples": selected_times[:25].tolist(),
                         "first25_data_pre_rise": words((prior[:25] >> data_bit) & 1),
                         "first25_data_post_rise": words((current[:25] >> data_bit) & 1),
                         "clock_data_coincident_changes": same_sample,
                         "clock_rise_at_rw_fall": at_start, "clock_rise_at_rw_return": at_return,
                         "malformed_more_than25": length > 25,
                         "classification": "no_clock_rw_low_interval" if length == 0 else
                         "candidate_25_clock_write" if length == 25 and not flags else "unqualified_write_window",
                         "flags": flags, "origin": "undetermined",
                         "external_data_release_verified": False})
        rw_low.append(interval)
    gap = round(gap_us * rate / 1e6)
    groups, group_counts = [], Counter()
    for window_index, window in enumerate(signal_runs["rw"]):
        if not window["level"]:
            continue
        start, end = window["start_sample"], window["end_sample_exclusive"]
        chosen = selection(clock_times, start, end)
        indexes = np.arange(chosen.start, chosen.stop)
        if not len(indexes):
            continue
        split = np.flatnonzero(np.diff(clock_times[indexes]) > gap) + 1
        for group_indexes in np.split(indexes, split):
            event_samples = clock_times[group_indexes]
            values = clock_after[group_indexes]
            directions = ((values >> clock_bit) & 1).astype(bool)
            rising_indexes = group_indexes[directions]
            falls = event_samples[~directions]
            rises = clock_times[rising_indexes]
            first, last = int(event_samples[0]), int(event_samples[-1])
            flags = []
            if start == 0 and first < gap:
                flags.append("left_acquisition_boundary_may_cut_group")
            if end == offset and offset - 1 - last < gap:
                flags.append("right_acquisition_boundary_may_cut_group")
            trailing_gap = end - last if end < offset else None
            paired = trailing_gap is not None and trailing_gap <= gap
            complete27 = len(rises) == len(falls) == 27 and bool(directions[0]) and not bool(directions[-1])
            if complete27 and paired:
                classification = "complete_27_clock_read_poll_candidate"
            elif flags:
                classification = "boundary_clock_fragment"
            elif paired:
                classification = "irregular_poll_group"
            else:
                classification = "clock_only_fragment"
            group_counts[classification] += 1
            prior, current = clock_before[rising_indexes], clock_after[rising_indexes]
            row = {"rw_high_run_index": window_index, "classification": classification,
                   "start_sample": first, "last_clock_edge_sample": last,
                   "start_seconds": first / rate, "clock_span_seconds": (last - first) / rate,
                   "rw_high_start_sample": start, "rw_fall_sample": end if end < offset else None,
                   "rw_return_observed": not window["left_censored"],
                   "clock_rises": len(rises), "clock_falls": len(falls),
                   "rise_samples": rises.tolist(), "fall_samples": falls.tolist(),
                   "data_pre_rise": words((prior >> data_bit) & 1),
                   "data_post_rise": words((current >> data_bit) & 1),
                   "trailing_rw_fall_gap_samples": trailing_gap,
                   "trailing_rw_fall_gap_seconds": trailing_gap / rate if trailing_gap is not None else None,
                   "flags": flags, "origin": "undetermined"}
            if len(rises) >= 26:
                initial_data = (int(clock_before[group_indexes[0]]) >> data_bit) & 1
                row["initial_plus26_rising_post_data"] = words([initial_data, *(((current[:26] >> data_bit) & 1).tolist())])
            groups.append(row)
    int_low = [timed_interval(run, rate) for run in signal_runs["interrupt"] if not run["level"]]
    for interval in int_low:
        start, end = interval["start_sample"], interval["end_sample_exclusive"]
        interval["clock_regions_relative_to_this_interval"] = {
            "before": region(rise_times, 0, start, rate),
            "during": region(rise_times, start, end, rate),
            "after": region(rise_times, end, offset, rate)}
    if int_low:
        first, last = int_low[0]["start_sample"], int_low[-1]["end_sample_exclusive"]
        aggregate = {"before_first_low": region(rise_times, 0, first, rate),
                     "during_low_intervals": {"duration_seconds": sum(item["duration_seconds"] for item in int_low),
                                              "clock_rises": sum(item["clock_regions_relative_to_this_interval"]["during"]["clock_rises"] for item in int_low)},
                     "between_low_intervals": {"duration_seconds": 0, "clock_rises": 0},
                     "after_last_low": region(rise_times, last, offset, rate)}
        for left, right in zip(int_low, int_low[1:]):
            middle = region(rise_times, left["end_sample_exclusive"], right["start_sample"], rate)
            aggregate["between_low_intervals"]["duration_seconds"] += middle["duration_seconds"]
            aggregate["between_low_intervals"]["clock_rises"] += middle["clock_rises"]
    else:
        aggregate = {"no_int_low_observed": region(rise_times, 0, offset, rate)}
    require(sum(part["clock_rises"] for part in aggregate.values()) == len(rise_times), "INT region counts do not cover all clock rises")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return {"source": str(path.resolve()), "sha256": digest.hexdigest(), "zip_crc_ok": True,
            "metadata": info, "sample_count": offset, "sampled_seconds": offset / rate,
            "channel_bits": bits, "complete_source_scan": True, "selected_signal_transition_timestamps": len(times),
            "signals": signals, "data_edges": data_edges,
            "interrupt_low_intervals": int_low,
            "interrupt_high_intervals": [timed_interval(run, rate) for run in signal_runs["interrupt"] if run["level"]],
            "clock_rise_regions_around_interrupt": aggregate, "rw_low_intervals": rw_low,
            "rw_low_summary": {"intervals": len(rw_low),
                               "clock_rise_counts": dict(Counter(str(interval["clock_rises"]) for interval in rw_low)),
                               "candidate_25_clock_writes": sum(row["classification"] == "candidate_25_clock_write" for row in rw_low),
                               "malformed_more_than25": sum(row["malformed_more_than25"] for row in rw_low)},
            "rw_high_clock_group_counts": dict(group_counts), "rw_high_clock_groups": groups,
            "gap_threshold_us": gap_us, "gap_threshold_samples": gap,
            "telemetry_validated": False, "meter_origin_established": False,
            "qualifications": [
                "Intervals are sample-index half-open ranges [start,end). Clock rises at an RW/INT transition are classified by the resulting sample state; within-sample order is unresolved.",
                "LOW and HIGH widths measure digital runs on a 1/24 MHz grid; analog rise/fall time cannot be measured by this capture.",
                "First/last runs are acquisition-censored. Complete-run width statistics omit those boundary runs.",
                "Each INT interval's before/after counts overlap those for other intervals; the separate aggregate regions are disjoint and cover all sampled clocks.",
                "RW-high clock groups split at clock-edge gaps greater than the configured threshold. A complete 27-clock read-poll candidate also has an observed trailing RW fall within that threshold.",
                "Raw bit strings and both bit-order numeric candidates do not establish register meaning, physical units, or valid telemetry.",
                "RW-low DATA cannot be attributed to the meter without separate evidence that the host released DATA during that interval. No such external evidence is assessed here."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-bit", type=int, default=0)
    parser.add_argument("--clock-bit", type=int, default=1)
    parser.add_argument("--rw-bit", type=int, default=2)
    parser.add_argument("--interrupt-bit", type=int, default=3)
    parser.add_argument("--gap-us", type=float, default=100)
    parser.add_argument("--max-events", type=int, default=2_000_000,
                        help="Fail explicitly above this selected-transition count; maximum 5,000,000")
    args = parser.parse_args()
    bits = {"data": args.data_bit, "clock": args.clock_bit, "rw": args.rw_bit, "interrupt": args.interrupt_bit}
    if len(set(bits.values())) != 4 or not all(0 <= bit <= 7 for bit in bits.values()):
        parser.error("Four distinct signal bits from 0 through 7 are required")
    if not 0 < args.gap_us <= 10000 or not 1 <= args.max_events <= 5_000_000:
        parser.error("gap-us must be positive and at most 10000; max-events must be 1 through 5000000")
    if args.output.resolve() == args.capture.resolve() or args.output.suffix.lower() != ".json":
        parser.error("--output must be a separate JSON file")
    start, timer = dt.datetime.now(dt.timezone.utc).isoformat(), time.monotonic()
    report = {"source": str(args.capture.resolve()), "complete_source_scan": False, "telemetry_validated": False}
    code = 0
    try:
        report = analyze(args.capture, bits, args.gap_us, args.max_events)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        code = 2
    report.update({"analysis_started_utc": start, "analysis_finished_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "analysis_wall_seconds": time.monotonic() - timer})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    temporary.replace(args.output)
    print(json.dumps({"output": str(args.output.resolve()), "complete_source_scan": report["complete_source_scan"],
                      "sample_count": report.get("sample_count"), "rw_low_summary": report.get("rw_low_summary"),
                      "rw_high_clock_group_counts": report.get("rw_high_clock_group_counts"), "error": report.get("error")}))
    return code


if __name__ == "__main__":
    sys.exit(main())
