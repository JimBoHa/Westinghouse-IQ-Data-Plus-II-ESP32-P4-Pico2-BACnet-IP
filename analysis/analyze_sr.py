#!/usr/bin/env python3
"""Read-only streaming analysis of sigrok v2 logic sessions. Requires numpy.

No protocol, word width, or signal function is inferred from the wiring labels.
Pulse statistics exclude runs touching either acquisition boundary. Burst groups
are a descriptive, user-adjustable gap heuristic, not discovered packet framing.
"""
import argparse
from collections import Counter
import configparser
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
import zipfile

import numpy as np


class Widths:
    """Exact distribution until 8192 distinct widths; bounded thereafter."""
    def __init__(self):
        self.count = 0
        self.minimum = None
        self.maximum = None
        self.histogram = Counter()
        self.log_histogram = Counter()
        self.omitted = 0

    def add(self, values):
        values = np.asarray(values, dtype=np.int64)
        if not values.size:
            return
        self.count += int(values.size)
        lo, hi = int(values.min()), int(values.max())
        self.minimum = lo if self.minimum is None else min(lo, self.minimum)
        self.maximum = hi if self.maximum is None else max(hi, self.maximum)
        widths, counts = np.unique(values, return_counts=True)
        for width, count in zip(widths.tolist(), counts.tolist()):
            self.log_histogram[int(width).bit_length()] += count
            if width in self.histogram or len(self.histogram) < 8192:
                self.histogram[width] += count
            else:
                self.omitted += count

    def result(self, rate):
        result = {"count": self.count, "min_samples": self.minimum,
                  "max_samples": self.maximum, "median_samples": None,
                  "histogram_exact": self.omitted == 0,
                  "histogram_omitted_observations": self.omitted,
                  "top_sample_widths": [
                      {"samples": int(k), "count": int(v), "seconds": k / rate}
                      for k, v in self.histogram.most_common(12)]}
        if not self.count:
            return result
        result["min_seconds"] = self.minimum / rate
        result["max_seconds"] = self.maximum / rate
        if not self.omitted:
            ranks = [(self.count - 1) // 2, self.count // 2]
            found, cumulative = [], 0
            for width, count in sorted(self.histogram.items()):
                for rank in ranks:
                    if cumulative <= rank < cumulative + count:
                        found.append(width)
                cumulative += count
            result["median_samples"] = sum(found) / 2
            result["median_seconds"] = result["median_samples"] / rate
        else:
            cumulative = 0
            for exponent, count in sorted(self.log_histogram.items()):
                cumulative += count
                if cumulative > self.count // 2:
                    result["median_upper_rank_bin_samples"] = (
                        [0, 0] if exponent == 0 else [2 ** (exponent - 1), 2 ** exponent - 1])
                    break
        return result


def parse_rate(value):
    match = re.fullmatch(r"\s*([\d.eE+-]+)\s*([kKmMgG]?)\s*(?:Hz)?\s*", value)
    if not match:
        raise ValueError(f"Unrecognized samplerate: {value!r}")
    return float(match[1]) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[match[2].lower()]


def session_info(archive):
    version = archive.read("version").decode().strip()
    if version != "2":
        raise ValueError(f"Only .sr session version 2 supported, got {version!r}")
    metadata = archive.read("metadata").decode("utf-8")
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(metadata)
    devices = [s for s in cfg.sections() if s.startswith("device ") and "capturefile" in cfg[s]]
    if len(devices) != 1:
        raise ValueError(f"Expected one logic device, found {devices}")
    device = dict(cfg[devices[0]])
    unit = int(device.get("unitsize", "1"))
    if not 1 <= unit <= 8:
        raise ValueError(f"Unsupported logic unit size: {unit}")
    probes = []
    for key, name in device.items():
        match = re.fullmatch(r"probe(\d+)", key)
        if match:
            bit = int(match[1]) - 1
            if bit < 0 or bit >= unit * 8:
                raise ValueError(f"Probe {key} exceeds stored unit size {unit}")
            probes.append({"bit": bit, "name": name, "metadata_key": key})
    probes.sort(key=lambda p: p["bit"])
    if not probes:
        raise ValueError("No enabled probeN fields in metadata; will not guess channel mapping")
    prefix = device["capturefile"]
    chunks = []
    for name in archive.namelist():
        match = re.fullmatch(re.escape(prefix) + r"-(\d+)", name)
        if match:
            chunks.append((int(match[1]), name))
    chunks.sort()
    if not chunks and prefix in archive.namelist():
        chunks = [(0, prefix)]
    if not chunks:
        raise ValueError(f"No logic chunks for {prefix!r}")
    indexes = [i for i, _ in chunks]
    if len(set(indexes)) != len(indexes) or indexes != list(range(indexes[0], indexes[-1] + 1)):
        raise ValueError("Duplicate or missing logic chunk index")
    rate = parse_rate(device["samplerate"])
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("Invalid samplerate")
    expected = 0
    for _, name in chunks:
        size = archive.getinfo(name).file_size
        if size % unit:
            raise ValueError(f"Logic chunk {name} not aligned to unitsize")
        expected += size // unit
    if not expected:
        raise ValueError("Capture contains no logic samples")
    return {"session_version": version, "device": device, "samplerate_hz": rate,
            "unit_size_bytes": unit, "channels": probes,
            "chunks": [name for _, name in chunks], "expected_samples": expected,
            "raw_metadata": metadata}


def sample_blocks(archive, info, block_samples=1048576):
    unit = info["unit_size_bytes"]
    for name in info["chunks"]:
        with archive.open(name) as stream:
            while True:
                data = stream.read(unit * block_samples)
                if not data:
                    break
                if len(data) % unit:
                    raise ValueError(f"Partial sample in {name}")
                if unit in (1, 2, 4, 8):
                    yield np.frombuffer(data, dtype=f"<u{unit}")
                else:
                    raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, unit)
                    words = np.zeros(len(raw), dtype=np.uint64)
                    for byte in range(unit):
                        words |= raw[:, byte].astype(np.uint64) << (8 * byte)
                    yield words


class Bursts:
    def __init__(self, gap_samples, channel_bits):
        self.gap = gap_samples
        self.channel_bits = channel_bits
        self.open = None
        self.count = 0
        self.preview = []
        self.durations = Widths()
        self.edge_counts = Widths()
        self.start_intervals = Widths()
        self.last_start = None
        self.signatures = Counter()
        self.omitted_signatures = 0
        self.signature_previews = {}
        self.channel_stats = {
            bit: {key: Widths() for key in ["rising_count", "falling_count",
                "first_rising_offset", "last_rising_offset",
                "first_falling_offset", "last_falling_offset"]}
            for bit in self.channel_bits}

    @staticmethod
    def counts_result(widths):
        raw = widths.result(1)
        return {"groups": raw["count"], "min": raw["min_samples"],
                "max": raw["max_samples"], "median": raw["median_samples"],
                "histogram_exact": raw["histogram_exact"],
                "histogram_omitted_observations": raw["histogram_omitted_observations"],
                "top_counts": [{"value": x["samples"], "groups": x["count"]}
                               for x in raw["top_sample_widths"]]}

    def close(self):
        if self.open is None:
            return
        group = self.open
        start, end, count = group["start"], group["end"], group["count"]
        if self.last_start is not None:
            self.start_intervals.add([start - self.last_start])
        self.last_start = start
        self.count += 1
        self.durations.add([end - start])
        self.edge_counts.add([count])
        signature = group["hasher"].hexdigest()
        if signature in self.signatures or len(self.signatures) < 8192:
            self.signatures[signature] += 1
        else:
            self.omitted_signatures += 1
        if signature not in self.signature_previews and len(self.signature_previews) < 4:
            self.signature_previews[signature] = group["event_preview"]
        offsets = {}
        for bit, stats in self.channel_stats.items():
            offsets[bit] = {}
            for polarity in ("rising", "falling"):
                stats[polarity + "_count"].add([group["counts"][bit][polarity]])
                offsets[bit][polarity + "_count"] = group["counts"][bit][polarity]
                for which in ("first", "last"):
                    value = group[which][bit][polarity]
                    offset = value - start if value is not None else None
                    offsets[bit][which + "_" + polarity + "_offset"] = offset
                    if offset is not None:
                        stats[which + "_" + polarity + "_offset"].add([offset])
        if len(self.preview) < 32:
            self.preview.append({"start_sample": start, "last_edge_sample": end,
                                 "distinct_edge_timestamps": count,
                                 "event_order_sha256": signature,
                                 "channel_counts_and_offsets_samples": offsets})
        self.open = None

    def add(self, edges, values, changes):
        if not len(edges):
            return
        cuts = np.flatnonzero(np.diff(edges) > self.gap) + 1
        starts, ends = np.r_[0, cuts], np.r_[cuts, len(edges)]
        for start_index, end_index in zip(starts.tolist(), ends.tolist()):
            start, end = int(edges[start_index]), int(edges[end_index - 1])
            count = end_index - start_index
            if self.open is None or start - self.open["end"] > self.gap:
                self.close()
                self.open = {"start": start, "end": start, "count": 0,
                             "hasher": hashlib.sha256(), "event_preview": [],
                             "counts": {bit: {"rising": 0, "falling": 0} for bit in self.channel_bits},
                             "first": {bit: {"rising": None, "falling": None} for bit in self.channel_bits},
                             "last": {bit: {"rising": None, "falling": None} for bit in self.channel_bits}}
            group = self.open
            group["end"] = end
            group["count"] += count
            event_values = values[start_index:end_index]
            event_changes = changes[start_index:end_index]
            event_samples = edges[start_index:end_index]
            pairs = np.column_stack((event_changes, event_values)).astype("<u8")
            group["hasher"].update(pairs.tobytes())
            room = max(0, 64 - len(group["event_preview"]))
            group["event_preview"].extend([
                {"changed_bits_hex": hex(int(mask)), "enabled_values_hex": hex(int(value))}
                for mask, value in pairs[:room]])
            for bit in self.channel_bits:
                changed = ((event_changes >> bit) & 1).astype(bool)
                high = ((event_values >> bit) & 1).astype(bool)
                for polarity, mask in [("rising", changed & high), ("falling", changed & ~high)]:
                    selected = np.flatnonzero(mask)
                    if not len(selected):
                        continue
                    group["counts"][bit][polarity] += len(selected)
                    if group["first"][bit][polarity] is None:
                        group["first"][bit][polarity] = int(event_samples[selected[0]])
                    group["last"][bit][polarity] = int(event_samples[selected[-1]])

    def result(self, rate):
        self.close()
        return {"heuristic": "Union of enabled-channel transitions; new group when gap exceeds threshold. Not packet framing.",
                "gap_threshold_samples": self.gap, "gap_threshold_seconds": self.gap / rate,
                "group_count": self.count, "preview": self.preview,
                "group_duration": self.durations.result(rate),
                "edge_timestamps_per_group": self.counts_result(self.edge_counts),
                "group_start_intervals": self.start_intervals.result(rate),
                "event_order_signatures": {
                    "definition": "SHA-256 of (changed enabled bits, resulting enabled values) uint64 little-endian pairs, chronological; excludes timing. Acquisition-cut groups can differ.",
                    "distinct_tracked": len(self.signatures),
                    "untracked_groups": self.omitted_signatures,
                    "top": [{"sha256": key, "groups": count,
                             "first_64_events": self.signature_previews.get(key)}
                            for key, count in self.signatures.most_common(12)]},
                "per_channel": {str(bit): {key: (self.counts_result(value) if key.endswith("_count") else value.result(rate))
                    for key, value in stats.items()} for bit, stats in self.channel_stats.items()}}


def analyze(path, block_samples=1048576, burst_gap_us=100, clock_bit=0,
            data_bit=1, edge_output=None, edge_limit=100000, word_width=None):
    with zipfile.ZipFile(path) as archive:
        info = session_info(archive)
        rate = info["samplerate_hz"]
        states = {}
        for probe in info["channels"]:
            states[probe["bit"]] = {**probe, "initial": None, "final": None,
                "transitions": 0, "rising": 0, "falling": 0, "high_samples": 0,
                "last_edge": None, "low_runs": Widths(), "high_runs": Widths()}
        clock_data_present = clock_bit in states and data_bit in states
        edge_streams = {level: {"edge_count": 0, "data_changed_at_clock_edge": 0,
                        "data_stable_previous_current_next": 0,
                        "data_unstable_previous_current_next": 0,
                        "no_following_sample": 0, "bits_at_edge": [],
                        "bits_before_edge": [], "stable_preview": []}
                        for level in (0, 1)}
        pending = None
        bursts = Bursts(max(1, round(burst_gap_us * 1e-6 * rate)), list(states))
        union_intervals = Widths()
        last_union_edge = None
        enabled_mask = sum(1 << bit for bit in states)
        total, previous, exported = 0, None, 0
        writer = csv.writer(edge_output) if edge_output else None
        if writer:
            writer.writerow(["sample", "seconds", "enabled_values_hex", "changed_bits_hex"])
        for words in sample_blocks(archive, info, block_samples):
            n = len(words)
            union_changes = np.zeros(n, dtype=bool)
            clock_indexes = None
            for bit, state in states.items():
                values = ((words >> bit) & 1).astype(np.uint8)
                if state["initial"] is None:
                    state["initial"] = int(values[0])
                changes = np.empty(n, dtype=bool)
                changes[0] = previous is not None and int(values[0]) != ((previous >> bit) & 1)
                changes[1:] = values[1:] != values[:-1]
                union_changes |= changes
                indexes = np.flatnonzero(changes)
                if bit == clock_bit:
                    clock_indexes = indexes
                edges = indexes + total
                after = values[indexes]
                rising = int(np.count_nonzero(after))
                state["transitions"] += len(edges)
                state["rising"] += rising
                state["falling"] += len(edges) - rising
                state["high_samples"] += int(np.count_nonzero(values))
                state["final"] = int(values[-1])
                if len(edges):
                    if state["last_edge"] is not None:
                        name = "low_runs" if after[0] else "high_runs"
                        state[name].add([int(edges[0]) - state["last_edge"]])
                    widths = np.diff(edges)
                    state["low_runs"].add(widths[after[:-1] == 0])
                    state["high_runs"].add(widths[after[:-1] == 1])
                    state["last_edge"] = int(edges[-1])
            if clock_data_present:
                data = ((words >> data_bit) & 1).astype(np.uint8)
                if pending is not None:
                    stream, before, after, preview_index = pending
                    stable = before == after == int(data[0])
                    key = "data_stable_previous_current_next" if stable else "data_unstable_previous_current_next"
                    stream[key] += 1
                    if preview_index is not None:
                        stream["stable_preview"][preview_index] = stable
                    pending = None
                for level in (0, 1):
                    indexes = clock_indexes[((words[clock_indexes] >> clock_bit) & 1) == level]
                    if not len(indexes):
                        continue
                    stream = edge_streams[level]
                    before = data[np.maximum(indexes - 1, 0)].copy()
                    if indexes[0] == 0:
                        before[0] = (previous >> data_bit) & 1
                    after = data[indexes]
                    has_next = indexes + 1 < n
                    following = data[np.minimum(indexes + 1, n - 1)]
                    stable = (before == after) & (after == following)
                    stream["edge_count"] += len(indexes)
                    stream["data_changed_at_clock_edge"] += int(np.count_nonzero(before != after))
                    stream["data_stable_previous_current_next"] += int(np.count_nonzero(stable & has_next))
                    stream["data_unstable_previous_current_next"] += int(np.count_nonzero(~stable & has_next))
                    room = max(0, 4096 - len(stream["bits_at_edge"]))
                    take = min(room, len(indexes))
                    old_length = len(stream["bits_at_edge"])
                    stream["bits_at_edge"].extend(after[:take].tolist())
                    stream["bits_before_edge"].extend(before[:take].tolist())
                    stream["stable_preview"].extend([bool(stable[i]) if has_next[i] else None for i in range(take)])
                    if not has_next[-1]:
                        preview_index = old_length + len(indexes) - 1 if len(indexes) <= room else None
                        pending = (stream, int(before[-1]), int(after[-1]), preview_index)
            indexes = np.flatnonzero(union_changes)
            union_edges = indexes + total
            if len(union_edges):
                if last_union_edge is not None:
                    union_intervals.add([int(union_edges[0]) - last_union_edge])
                union_intervals.add(np.diff(union_edges))
                last_union_edge = int(union_edges[-1])
                event_values = words[indexes].astype(np.uint64) & enabled_mask
                event_prior = words[np.maximum(indexes - 1, 0)].astype(np.uint64) & enabled_mask
                if indexes[0] == 0:
                    event_prior[0] = previous & enabled_mask
                bursts.add(union_edges, event_values, event_values ^ event_prior)
            if writer and exported < edge_limit:
                for i in indexes[:edge_limit - exported].tolist():
                    current = int(words[i]) & enabled_mask
                    prior = (int(words[i - 1]) if i else previous) & enabled_mask
                    writer.writerow([total + i, f"{(total + i) / rate:.12g}", hex(current), hex(current ^ prior)])
                    exported += 1
            previous = int(words[-1])
            total += n
        if total != info["expected_samples"]:
            raise ValueError("Read sample count differs from archive sizes")
        if pending is not None:
            pending[0]["no_following_sample"] += 1
        channels = []
        for state in states.values():
            state.pop("last_edge")
            state["high_fraction"] = state["high_samples"] / total if total else None
            state["constant_state"] = state["initial"] if state["transitions"] == 0 else None
            state["low_complete_runs"] = state.pop("low_runs").result(rate)
            state["high_complete_runs"] = state.pop("high_runs").result(rate)
            channels.append(state)
        clock_result = {"clock_stored_bit": clock_bit, "data_stored_bit": data_bit,
                        "present": clock_data_present,
                        "caveat": "Values are sampled at acquisition resolution. Same-sample changes cannot be ordered. A stable neighbor test does not prove device setup/hold compliance.",
                        "framing": "Chronological bit streams only; no word width or packet boundary inferred."}
        if clock_data_present:
            for level, name in [(1, "rising"), (0, "falling")]:
                stream = edge_streams[level]
                if word_width:
                    bits = stream["bits_at_edge"]
                    groups = [bits[i:i + word_width] for i in range(0, len(bits) - word_width + 1, word_width)][:64]
                    stream["candidate_words"] = {
                        "hypothesis_only": True, "width": word_width, "offset_edges": 0,
                        "msb_first": [sum(b << (word_width - i - 1) for i, b in enumerate(g)) for g in groups],
                        "lsb_first": [sum(b << i for i, b in enumerate(g)) for g in groups]}
                stream["preview_truncated"] = stream["edge_count"] > len(stream["bits_at_edge"])
                stream["bits_at_edge"] = "".join(map(str, stream["bits_at_edge"]))
                stream["bits_before_edge"] = "".join(map(str, stream["bits_before_edge"]))
                clock_result[name] = stream
        union_count = union_intervals.count + (last_union_edge is not None)
        return {"source": str(Path(path).resolve()), "metadata": info,
                "sample_count": total, "duration_seconds": total / rate,
                "first_to_last_sample_seconds": max(0, total - 1) / rate,
                "sample_period_seconds": 1 / rate,
                "pulse_statistics_note": "Complete runs only. First and last runs are censored and excluded. Quantized to sample period.",
                "channels": channels, "distinct_edge_timestamps": int(union_count),
                "edge_interval_statistics": union_intervals.result(rate),
                "burst_groups": bursts.result(rate), "clock_data_analysis": clock_result,
                "edge_export": {"enabled": bool(writer), "rows": exported,
                    "limit": edge_limit, "truncated": bool(writer) and exported < union_count},
                "finding": "All enabled channels constant throughout this capture; no protocol can be decoded." if not union_count else "Transitions observed. Signal function and protocol remain hypotheses."}


def markdown(report):
    lines = [f"# {Path(report['source']).name}", "", report["finding"], "",
             f"Samples: {report['sample_count']:,}; sample rate: {report['metadata']['samplerate_hz']:g} Hz; actual acquisition duration: {report['duration_seconds']:.9g} s.", "",
             "| Stored bit | Channel label | Initial → final | Transitions | Rising | Falling | High fraction |",
             "|---:|---|---|---:|---:|---:|---:|"]
    for c in report["channels"]:
        fraction = "n/a" if c["high_fraction"] is None else f"{c['high_fraction']:.9g}"
        lines.append(f"| {c['bit']} | {c['name']} | {c['initial']} → {c['final']} | {c['transitions']:,} | {c['rising']:,} | {c['falling']:,} | {fraction} |")
    lines += ["", "Pulse widths exclude acquisition-boundary runs. No transitions means no complete pulse widths to measure.", ""]
    for c in report["channels"]:
        if not c["transitions"]:
            continue
        for level in ("low", "high"):
            s = c[f"{level}_complete_runs"]
            if s["count"]:
                median = s.get("median_seconds")
                medtext = f"{median:g} s" if median is not None else "not exact (see JSON histogram)"
                lines.append(f"- {c['name']} {level}: {s['count']:,} complete runs; min {s['min_seconds']:g} s, median {medtext}, max {s['max_seconds']:g} s.")
    b = report["burst_groups"]
    lines += ["", f"Burst heuristic: {b['group_count']:,} groups with gaps ≤ {b['gap_threshold_seconds']:g} s inside each group. These are not established packets."]
    if b["group_count"]:
        signatures = b["event_order_signatures"]
        lines += [f"Event-order signatures: {signatures['distinct_tracked']} distinct tracked; timing is excluded from each signature."]
        for bit, stats in b["per_channel"].items():
            if stats["rising_count"]["max"] or stats["falling_count"]["max"]:
                lines.append(f"- Bit {bit}: rising edges/group {stats['rising_count']['min']}–{stats['rising_count']['max']}; falling edges/group {stats['falling_count']['min']}–{stats['falling_count']['max']}.")
                for key in ("first_rising_offset", "last_rising_offset", "first_falling_offset", "last_falling_offset"):
                    stat = stats[key]
                    if stat["count"]:
                        lines.append(f"  {key.replace('_', ' ')}: {stat['min_seconds']:g}–{stat['max_seconds']:g} s from first group edge.")
    clock = report["clock_data_analysis"]
    if clock["present"]:
        lines += ["", f"Candidate clock bit {clock['clock_stored_bit']}, data bit {clock['data_stored_bit']}: sampled on both clock edges."]
        for name in ("rising", "falling"):
            s = clock[name]
            lines.append(f"- {name}: {s['edge_count']:,} edges; data changed in same sample at {s['data_changed_at_clock_edge']:,}; stable before/at/after at {s['data_stable_previous_current_next']:,}.")
            if s["edge_count"]:
                lines.append(f"  First chronological bits: `{s['bits_at_edge'][:128]}`")
        lines += ["", clock["framing"], clock["caveat"]]
    lines += ["", "Full distributions, bounded edge previews, metadata, and burst timings are in the companion JSON.", ""]
    return "\n".join(lines)


def self_test():
    def write_fixture(path, values, chunk_sizes, unit=1):
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("version", "2")
            z.writestr("metadata", f"[global]\nsigrok version=synthetic\n[device 1]\ncapturefile=logic-1\nunitsize={unit}\nsamplerate=1 MHz\ntotal probes=4\nprobe1=0\nprobe2=1\nprobe3=2\nprobe4=3\n")
            offset = 0
            for i, size in enumerate(chunk_sizes, 1):
                z.writestr(f"logic-1-{i}", np.array(values[offset:offset + size], dtype=f"<u{unit}").tobytes())
                offset += size
            assert offset == len(values)
    with tempfile.TemporaryDirectory() as tmp:
        flat = Path(tmp) / "flat.sr"
        write_fixture(flat, [14] * 12, [1] * 12)
        r = analyze(flat, block_samples=3)
        assert r["sample_count"] == 12 and r["duration_seconds"] == 12e-6
        assert [c["constant_state"] for c in r["channels"]] == [0, 1, 1, 1]
        assert r["distinct_edge_timestamps"] == 0
        path = Path(tmp) / "edges.sr"
        values = [0, 0, 1, 3, 2, 2, 0, 1, 1, 0]
        write_fixture(path, values, [2, 3, 1, 4])
        a = analyze(path, block_samples=2)
        b = analyze(path, block_samples=100)
        assert a["channels"] == b["channels"]
        assert a["clock_data_analysis"] == b["clock_data_analysis"]
        assert a["burst_groups"] == b["burst_groups"]
        c = a["channels"][0]
        assert (c["transitions"], c["rising"], c["falling"], c["high_samples"]) == (4, 2, 2, 4)
        assert c["high_complete_runs"]["median_samples"] == 2
        assert c["high_complete_runs"]["count"] == 2
        assert c["low_complete_runs"]["count"] == 1
        assert c["low_complete_runs"]["median_samples"] == 3
        assert a["distinct_edge_timestamps"] == 6
        assert a["clock_data_analysis"]["falling"]["no_following_sample"] == 1
        # Independent oracle checks randomized cross-chunk and cross-block runs.
        rng = np.random.default_rng(2026)
        values = rng.integers(0, 16, size=103).tolist()
        write_fixture(path, values, [1, 2, 7, 35, 58], unit=2)
        a = analyze(path, block_samples=7)
        for bit, channel in enumerate(a["channels"]):
            bits = np.array([(v >> bit) & 1 for v in values])
            edges = np.flatnonzero(bits[1:] != bits[:-1]) + 1
            assert channel["transitions"] == len(edges)
            for level, key in [(0, "low_complete_runs"), (1, "high_complete_runs")]:
                widths = np.diff(edges)[bits[edges[:-1]] == level]
                assert channel[key]["count"] == len(widths)
                if len(widths):
                    assert channel[key]["median_samples"] == float(np.median(widths))
        b = analyze(path, block_samples=1000)
        assert a["channels"] == b["channels"]
        assert a["clock_data_analysis"] == b["clock_data_analysis"]
        assert a["burst_groups"] == b["burst_groups"]
    print("Self-test passed: flat traces, numeric chunk ordering, cross-chunk edges, pulse boundaries, clock neighbors, multi-byte units, randomized oracle.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", nargs="?", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("analysis"))
    parser.add_argument("--burst-gap-us", type=float, default=100)
    parser.add_argument("--clock-bit", type=int, default=0, help="Stored sample bit, not arbitrary display label")
    parser.add_argument("--data-bit", type=int, default=1)
    parser.add_argument("--word-width", type=int, help="Optional explicit hypothesis; outputs MSB/LSB first values, offset zero")
    parser.add_argument("--export-edges", action="store_true", help="Write capped gzip CSV of union transition timestamps")
    parser.add_argument("--edge-limit", type=int, default=100000)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.capture is None:
        parser.error("capture is required unless --self-test")
    if args.burst_gap_us <= 0 or args.edge_limit < 0 or (args.word_width is not None and not 1 <= args.word_width <= 64):
        parser.error("burst gap must be positive, edge limit nonnegative, word width 1–64")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.output_dir / args.capture.stem
    edge_file = gzip.open(str(prefix) + ".edges.csv.gz", "wt", newline="") if args.export_edges else None
    try:
        report = analyze(args.capture, burst_gap_us=args.burst_gap_us,
                         clock_bit=args.clock_bit, data_bit=args.data_bit,
                         edge_output=edge_file, edge_limit=args.edge_limit,
                         word_width=args.word_width)
    finally:
        if edge_file:
            edge_file.close()
    for extension, content in [(".json", json.dumps(report, indent=2) + "\n"), (".md", markdown(report))]:
        output = Path(str(prefix) + extension)
        temporary = Path(str(output) + ".tmp")
        temporary.write_text(content)
        temporary.replace(output)
    print(report["finding"])
    print(f"{report['sample_count']:,} samples at {report['metadata']['samplerate_hz']:g} Hz; {report['duration_seconds']:.9g} s")
    print(f"Wrote {prefix}.json and {prefix}.md")


if __name__ == "__main__":
    main()
