#!/usr/bin/env python3
"""Compare sampled 27-clock DATA sequences with an explicit expected request.

Uses raw sigrok samples through analyze_trial's complete transition scan. A match
is not a meter response, decoded telemetry, or proof that the meter accepted it.
"""
import argparse
from collections import Counter
import datetime as dt
import json
from pathlib import Path
import time

import numpy as np

from analyze_trial import analyze, require


def decode(analysis, expected, shift_edge):
    require(analysis.get("complete_source_scan") is True, "Incomplete source scan")
    sample_count = analysis["sample_count"]
    rate = analysis["metadata"]["samplerate_hz"]
    transition_times = np.array([edge["sample"] for edge in analysis["data_edges"]], dtype=np.int64)
    levels = np.array([analysis["signals"]["data"]["initial"],
                       *[edge["after_levels"]["data"] for edge in analysis["data_edges"]]], dtype=np.uint8)

    def data_at(points):
        points = np.asarray(points, dtype=np.int64)
        require(bool(np.all((points >= 0) & (points < sample_count))), "DATA sample point outside capture")
        return levels[np.searchsorted(transition_times, points, side="right")]

    def alignment(points):
        points = np.asarray(points, dtype=np.int64)
        bits = data_at(points)
        text = "".join(str(int(bit)) for bit in bits)
        lsb = sum(int(bit) << position for position, bit in enumerate(bits))
        msb = int(text, 2)
        neighbor_valid = (points > 0) & (points + 1 < sample_count)
        stable = ((data_at(np.maximum(points - 1, 0)) == bits)
                  & (data_at(np.minimum(points + 1, sample_count - 1)) == bits) & neighbor_valid)
        return {"chronological_bits": text, "bit_count": len(bits), "lsb_first_word_hex": f"0x{lsb:08x}",
                "msb_first_word_hex": f"0x{msb:08x}", "matches_expected_lsb_first": lsb == expected,
                "unstable_or_boundary_sampling_points": int(np.count_nonzero(~stable))}

    primary_name = f"initial_plus26_{shift_edge}_midlevel"
    rows, excluded = [], []
    match_counts, primary_words = Counter(), Counter()
    primary_matches, qualified_matches = [], []
    for index, group in enumerate(analysis["rw_high_clock_groups"], 1):
        if (group["clock_rises"] < 27 or group["clock_rises"] != group["clock_falls"]
                or group["rw_fall_sample"] is None):
            excluded.append({"source_group": index, "classification": group["classification"],
                             "start_sample": group["start_sample"], "clock_rises": group["clock_rises"],
                             "clock_falls": group["clock_falls"], "flags": group["flags"]})
            continue
        all_rises = np.array(group["rise_samples"], dtype=np.int64)
        all_falls = np.array(group["fall_samples"], dtype=np.int64)
        rises, falls = all_rises[:27], all_falls[:27]
        low_ends = np.r_[all_rises[1:28], group["rw_fall_sample"]][:27]
        high_mid = (rises + falls) // 2
        low_mid = (falls + low_ends) // 2
        require(bool(np.all((high_mid > rises) & (high_mid < falls))), "No interior HIGH sample")
        require(bool(np.all((low_mid > falls) & (low_mid < low_ends))), "No interior LOW sample")
        sampling = {
            "rising_pre": rises - 1, "rising_post": rises,
            "falling_pre": falls - 1, "falling_post": falls,
            "rising_midlevel": high_mid, "falling_midlevel": low_mid,
            "initial_plus26_rising_midlevel": np.r_[rises[0] - 1, high_mid[:26]],
            "initial_plus26_falling_midlevel": np.r_[rises[0] - 1, low_mid[:26]],
        }
        alignments = {name: alignment(points) for name, points in sampling.items()}
        for offset_us in (0.5, 1, 2, 3, 4, 5):
            offsets = rises[:26] + round(offset_us * rate / 1e6)
            name = f"initial_plus26_rising_offset_{offset_us:g}us"
            sampling[name] = np.r_[rises[0] - 1, offsets]
            alignments[name] = {**alignment(sampling[name]),
                                "points_at_or_after_next_clock_rise": int(np.count_nonzero(offsets >= rises[1:27]))}
        for name, candidate in alignments.items():
            if candidate["matches_expected_lsb_first"]:
                match_counts[name] += 1
        primary = alignments[primary_name]
        primary_words[primary["lsb_first_word_hex"]] += 1
        last_data = int(data_at(high_mid[-1:] if shift_edge == "rising" else low_mid[-1:])[0])
        is_match = primary["matches_expected_lsb_first"]
        if is_match:
            primary_matches.append(index)
        qualified = is_match and last_data == 0 and primary["unstable_or_boundary_sampling_points"] == 0
        if qualified:
            qualified_matches.append(index)
        edge_offsets = []
        data_first = int(np.searchsorted(transition_times, rises[0], side="left"))
        data_end = int(np.searchsorted(transition_times, group["rw_fall_sample"], side="left"))
        for edge in analysis["data_edges"][data_first:data_end]:
            sample = edge["sample"]
            if not rises[0] <= sample < group["rw_fall_sample"]:
                continue
            preceding = int(np.searchsorted(all_rises, sample, side="right")) - 1
            edge_offsets.append({"sample": sample, "after_data": edge["after_levels"]["data"],
                                 "preceding_rise_number": preceding + 1,
                                 "after_preceding_rise_us": (sample - int(all_rises[preceding])) * 1e6 / rate,
                                 "relative_to_following_fall_us": (sample - int(all_falls[preceding])) * 1e6 / rate})
        rows.append({"source_group": index, "source_classification": group["classification"],
                     "source_clock_rises": len(all_rises), "decoded_first_clock_count": 27,
                     "extra_clock_rise_samples": all_rises[27:].tolist(),
                     "source_is_exact_27_clock_group": len(all_rises) == 27,
                     "start_sample": group["start_sample"],
                     "start_seconds": group["start_seconds"], "rw_fall_sample": group["rw_fall_sample"],
                     "rise_samples": all_rises.tolist(), "fall_samples": all_falls.tolist(),
                     "alignments": alignments, "primary_alignment": primary_name,
                     "primary_sample_indices": sampling[primary_name].tolist(),
                     "data_transition_offsets": edge_offsets,
                     "post_shift27_midlevel_data": last_data,
                     "expected_word_stable_with_zero_backfill": qualified,
                     "boundary_flags": group["flags"], "driver_origin": "undetermined"})
    clocked_writes = []
    for interval in analysis["rw_low_intervals"]:
        if not interval["clock_rises"]:
            continue
        rises = np.array(interval["first25_clock_rise_samples"], dtype=np.int64)
        points = {"rising_pre": rises - 1, "rising_post": rises}
        points.update({f"rising_offset_{us:g}us": rises + round(us * rate / 1e6) for us in (0.5, 1, 2, 3)})
        clocked_writes.append({**interval, "independent_data_alignments": {
            name: alignment(samples) for name, samples in points.items()
            if bool(np.all((samples >= interval["start_sample"]) & (samples < interval["end_sample_exclusive"])))},
            "driver_origin": "undetermined", "telemetry_validated": False})
    return {"source": analysis["source"], "sha256": analysis["sha256"],
            "sample_count": sample_count, "sampled_seconds": analysis["sampled_seconds"],
            "samplerate_hz": analysis["metadata"]["samplerate_hz"], "channel_bits": analysis["channel_bits"],
            "complete_source_scan": True, "expected_request_word_hex": f"0x{expected:08x}",
            "expected_word_width": 27, "expected_bits_lsb_first": "".join(str((expected >> index) & 1) for index in range(27)),
            "configured_shift_edge": shift_edge, "primary_alignment": primary_name,
            "decoded_request_windows": len(rows),
            "decoded_27_clock_groups": sum(row["source_is_exact_27_clock_group"] for row in rows),
            "irregular_request_windows": sum(not row["source_is_exact_27_clock_group"] for row in rows),
            "alignment_match_counts": dict(match_counts),
            "primary_word_counts": dict(primary_words), "primary_match_groups": primary_matches,
            "stable_expected_word_with_zero_backfill_groups": qualified_matches,
            "groups": rows, "excluded_groups": excluded, "data_transition_count": analysis["signals"]["data"]["transitions"],
            "rw_low_summary": analysis["rw_low_summary"], "rw_low_clocked_intervals": clocked_writes,
            "telemetry_validated": False,
            "meter_origin_established": False,
            "sampling_definitions": {
                "pre": "Sample immediately preceding the indicated clock transition.",
                "post": "Sample containing the indicated clock transition; simultaneous DATA changes have unresolved order.",
                "rising_midlevel": "Middle sample of each HIGH clock pulse.",
                "falling_midlevel": "Middle sample between clock fall and next rise; final interval ends at trailing RW fall.",
                "initial_plus26": "DATA immediately before first clock rise followed by first 26 indicated midlevel samples; the 27th shift is checked separately for zero backfill."},
            "qualifications": [
                "All alignments are preserved. The primary alignment follows the explicitly selected shift-edge hypothesis, not whichever alignment happens to match.",
                "A stable-neighbor test is only digital sampling evidence; it does not prove analog setup/hold margins.",
                "An expected request match must not be reported as received measurement telemetry. No physical units or register payload decoding is performed.",
                "The analyzer does not identify the DATA driver. Meter origin requires separate RW-low/output-release evidence and independent validation.",
                "A group with more than 27 paired clocks is decoded only as an explicitly identified first-27-clock window; extra clocks and original irregular classification remain visible. Partial groups are never padded.",
                "Fixed-offset samples at or after the next rise do not measure the same clock cycle and are explicitly counted."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected", type=lambda value: int(value, 0), default=0x0300001D)
    parser.add_argument("--shift-edge", choices=["rising", "falling"], default="rising")
    parser.add_argument("--data-bit", type=int, default=0)
    parser.add_argument("--clock-bit", type=int, default=1)
    parser.add_argument("--rw-bit", type=int, default=2)
    parser.add_argument("--interrupt-bit", type=int, default=3)
    args = parser.parse_args()
    bits = {"data": args.data_bit, "clock": args.clock_bit, "rw": args.rw_bit, "interrupt": args.interrupt_bit}
    if len(set(bits.values())) != 4 or not all(0 <= bit <= 7 for bit in bits.values()):
        parser.error("Four distinct signal bits from 0 through 7 are required")
    if not 0 <= args.expected < (1 << 27):
        parser.error("Expected request must fit in 27 bits")
    if args.output.resolve() == args.capture.resolve() or args.output.suffix.lower() != ".json":
        parser.error("--output must name a separate JSON file")
    timer = time.monotonic()
    report = {"source": str(args.capture.resolve()), "complete_source_scan": False, "telemetry_validated": False}
    code = 0
    try:
        report = decode(analyze(args.capture, bits), args.expected, args.shift_edge)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        code = 2
    report["analysis_finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    report["analysis_wall_seconds"] = time.monotonic() - timer
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    temporary.replace(args.output)
    print(json.dumps({"output": str(args.output.resolve()), "complete_source_scan": report["complete_source_scan"],
                      "decoded_27_clock_groups": report.get("decoded_27_clock_groups"),
                      "primary_word_counts": report.get("primary_word_counts"),
                      "primary_match_groups": report.get("primary_match_groups"), "error": report.get("error")}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
