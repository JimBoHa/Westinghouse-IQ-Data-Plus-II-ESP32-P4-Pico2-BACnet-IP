"""Live-only diagnostic estimates; no serial access, persistence, or database writes.

One accumulator belongs to one gateway process. All histories reset on restart.
Use update(host.telemetry(state)[1]) and expose the returned reading dictionaries.
Repeated or out-of-order observations never add samples. Missing/stale samples and
intervals over max_gap_seconds break coverage, never interpolate across outages.
Rolling values are invalid until the entire requested window has valid coverage.
"""
from collections import deque
from datetime import datetime
import math


def _spec(instance, key, name, units, description, kind="analog"):
    return {"key": key, "name": name, "type": kind, "instance": instance,
            "units": units, "description": description, "group": "derived"}


FIELD_SPECS = [
    _spec(300, "VLL_IMBALANCE_pct", "Voltage-Imbalance", "percent", "RMS line-voltage maximum absolute deviation from phase mean / mean; not sequence-component unbalance"),
    _spec(301, "CURRENT_IMBALANCE_pct", "Current-Imbalance", "percent", "RMS phase-current maximum absolute deviation from phase mean / mean"),
    _spec(302, "HIGHEST_CURRENT_PHASE", "Highest-Current-Phase", "noUnits", "Largest RMS current: A=1, B=2, C=3; ties choose lowest index; invalid with zero current"),
    _spec(303, "HIGHEST_CURRENT_A", "Highest-Phase-Current", "amperes", "Largest of the three RMS phase currents"),
    _spec(304, "SESSION_VLL_MIN_V", "Session-Voltage-Minimum", "volts", "Minimum sampled RMS line voltage across phases since gateway restart"),
    _spec(305, "SESSION_VLL_MAX_V", "Session-Voltage-Maximum", "volts", "Maximum sampled RMS line voltage across phases since gateway restart"),
    _spec(306, "SESSION_CURRENT_MIN_A", "Session-Current-Minimum", "amperes", "Minimum sampled phase RMS current since gateway restart"),
    _spec(307, "SESSION_CURRENT_MAX_A", "Session-Current-Maximum", "amperes", "Maximum sampled phase RMS current since gateway restart"),
    _spec(308, "SESSION_FREQUENCY_MIN_Hz", "Session-Frequency-Minimum", "hertz", "Minimum sampled meter frequency since gateway restart"),
    _spec(309, "SESSION_FREQUENCY_MAX_Hz", "Session-Frequency-Maximum", "hertz", "Maximum sampled meter frequency since gateway restart"),
    _spec(310, "SESSION_PF_MIN", "Session-PF-Magnitude-Minimum", "powerFactor", "Minimum absolute meter PF since gateway restart; not a distortion measurement"),
    _spec(311, "SESSION_PF_MAX", "Session-PF-Magnitude-Maximum", "powerFactor", "Maximum absolute meter PF since gateway restart"),
    _spec(312, "SESSION_AVERAGE_POWER_kW", "Session-Average-Power", "kilowatts", "Time-weighted net power over valid observed intervals only; trapezoidal interpolation"),
    _spec(313, "SESSION_PEAK_POWER_kW", "Session-Peak-Power", "kilowatts", "Maximum sampled signed real power since gateway restart"),
    _spec(314, "LOAD_CHANGE_kW", "Load-Change", "kilowatts", "Change from previous fresh real-power sample; invalid across gaps beyond configured maximum"),
    _spec(315, "LOW_PF_CONTINUOUS_s", "Low-PF-Continuous-Duration", "seconds", "Consecutive observed seconds with both endpoint PF magnitudes below configured threshold"),
    _spec(316, "LOW_PF_TOTAL_s", "Session-Low-PF-Duration", "seconds", "Accumulated observed intervals with both endpoint PF magnitudes below configured threshold"),
    _spec(317, "FREQUENCY_EXCURSION_CONTINUOUS_s", "Frequency-Excursion-Duration", "seconds", "Consecutive observed seconds outside configured nominal frequency tolerance"),
    _spec(318, "FREQUENCY_EXCURSION_TOTAL_s", "Session-Frequency-Excursion-Duration", "seconds", "Accumulated observed intervals with both endpoints outside configured frequency tolerance"),
    _spec(319, "VOLTAGE_EXCURSION_CONTINUOUS_s", "Voltage-Excursion-Duration", "seconds", "Consecutive observed seconds with any line voltage outside configured nominal tolerance; invalid without nominal voltage"),
    _spec(320, "VOLTAGE_EXCURSION_TOTAL_s", "Session-Voltage-Excursion-Duration", "seconds", "Accumulated observed intervals outside configured nominal voltage tolerance; invalid without nominal voltage"),
    _spec(321, "ROLLING_15MIN_DEMAND_kW", "Rolling-15-Minute-Demand", "kilowatts", "Time-weighted net power across a complete continuous 15-minute window; invalid during warmup or gaps"),
    _spec(322, "ROLLING_15MIN_LOAD_FACTOR_pct", "Rolling-15-Minute-Load-Factor", "percent", "100 times 15-minute average / sampled peak; valid only for complete nonnegative-power window with positive peak"),
    _spec(323, "SESSION_POWER_COVERAGE_h", "Session-Power-Coverage", "hours", "Sum of valid observed power intervals since gateway restart; excludes gaps"),
    _spec(324, "ROLLING_POWER_COVERAGE_s", "Rolling-Power-Coverage", "seconds", "Valid observed power seconds in the trailing 15 minutes; full value requires 900 seconds"),
    _spec(325, "OBSERVED_ENERGY_kWh", "Observed-Session-Energy", "kilowattHours", "Sum of nondecreasing counter increments across valid adjacent observations only; 1 kWh resolution; excludes gaps and resets"),
    _spec(326, "ROLLING_HOUR_ENERGY_kWh", "Rolling-Hour-Energy", "kilowattHours", "Counter change over a fully observed trailing hour; boundary interpolated; 1 kWh counter resolution"),
    _spec(327, "ROLLING_DAY_ENERGY_kWh", "Rolling-Day-Energy", "kilowattHours", "Counter change over a fully observed trailing 24 hours; boundary interpolated; 1 kWh counter resolution"),
    _spec(328, "CONTINUOUS_ENERGY_COVERAGE_h", "Continuous-Energy-Coverage", "hours", "Duration of current uninterrupted valid nondecreasing energy-counter sequence, retained up to 24 hours"),
    _spec(329, "ACCEPTED_SAMPLES", "Diagnostics-Sample-Count", "noUnits", "Distinct fresh meter observations accepted since gateway restart"),
    _spec(330, "DIAGNOSTICS_AGE_s", "Diagnostics-Session-Age", "seconds", "Elapsed meter observation time since first accepted sample after gateway restart"),
    _spec(331, "SAMPLE_GAP_COUNT", "Diagnostics-Sample-Gaps", "noUnits", "Observed intervals exceeding configured maximum gap; cumulative since gateway restart"),
    _spec(332, "ENERGY_COUNTER_RESET_COUNT", "Energy-Counter-Decreases", "noUnits", "Observed energy counter decreases; reset and wrap are not distinguished"),
    _spec(333, "OUT_OF_ORDER_COUNT", "Diagnostics-Out-of-Order-Count", "noUnits", "Rejected distinct observations older than last accepted timestamp"),
    _spec(334, "EXCLUDED_GAP_TIME_s", "Diagnostics-Excluded-Gap-Time", "seconds", "Total duration of excluded intervals exceeding maximum gap; not interpolated"),
    _spec(335, "NOMINAL_VOLTAGE_V", "Diagnostics-Nominal-Voltage", "volts", "Configured nominal line voltage for excursion checks; invalid when not configured"),
    _spec(336, "NOMINAL_FREQUENCY_Hz", "Diagnostics-Nominal-Frequency", "hertz", "Configured nominal frequency for excursion checks"),
    _spec(337, "LOW_PF_THRESHOLD", "Diagnostics-Low-PF-Threshold", "powerFactor", "PF magnitude threshold for live diagnostic duration; not a meter alarm setting"),
    _spec(338, "VOLTAGE_TOLERANCE_pct", "Diagnostics-Voltage-Tolerance", "percent", "Allowed percentage deviation from configured nominal line voltage"),
    _spec(339, "FREQUENCY_TOLERANCE_Hz", "Diagnostics-Frequency-Tolerance", "hertz", "Allowed absolute deviation from configured nominal frequency"),
    _spec(340, "SESSION_ENERGY_COVERAGE_h", "Session-Energy-Coverage", "hours", "Sum of observed valid energy-counter intervals; excludes gaps and decreases"),
    _spec(341, "LATEST_SAMPLE_INTERVAL_s", "Diagnostics-Sample-Interval", "seconds", "Time between latest two accepted snapshots; exposes skipped or delayed samples"),
    _spec(200, "LOW_PF_ACTIVE", "Low-PF-Active", "noUnits", "Absolute meter PF is below configured threshold", "binary"),
    _spec(201, "FREQUENCY_EXCURSION_ACTIVE", "Frequency-Excursion-Active", "noUnits", "Meter frequency is outside configured nominal frequency tolerance", "binary"),
    _spec(202, "VOLTAGE_EXCURSION_ACTIVE", "Voltage-Excursion-Active", "noUnits", "At least one line voltage is outside configured nominal voltage tolerance", "binary"),
    _spec(203, "ROLLING_DEMAND_READY", "Rolling-Demand-Ready", "noUnits", "A complete continuous 15-minute real-power window is available", "binary"),
    _spec(204, "ROLLING_HOUR_ENERGY_READY", "Rolling-Hour-Energy-Ready", "noUnits", "A complete continuous one-hour energy-counter window is available", "binary"),
    _spec(205, "ROLLING_DAY_ENERGY_READY", "Rolling-Day-Energy-Ready", "noUnits", "A complete continuous 24-hour energy-counter window is available", "binary"),
]


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


class DiagnosticAccumulator:
    """Bounded in-memory estimates; ``poll_health`` reserved for caller metadata.

    ``nominal_vll=None`` deliberately disables voltage excursion calculations.
    Default frequency diagnostics explicitly assume 60 Hz, +/- 0.5 Hz.
    Output reading dictionaries have value/valid/stale/source/observed_utc.
    Callers must honor both valid and stale. Rolling windows start at process
    startup, do not query recorded history, and never claim full-window zeroes.
    """
    def __init__(self, nominal_hz=60.0, nominal_vll=None, low_pf_threshold=0.9,
                 max_gap_seconds=5.0, voltage_tolerance_pct=10.0,
                 frequency_tolerance_hz=0.5, max_samples=85000):
        for key, value in (("nominal_hz", nominal_hz), ("max_gap_seconds", max_gap_seconds),
                           ("voltage_tolerance_pct", voltage_tolerance_pct),
                           ("frequency_tolerance_hz", frequency_tolerance_hz)):
            if not _number(value) or value <= 0:
                raise ValueError(key + " must be finite and positive")
        if nominal_vll is not None and (not _number(nominal_vll) or nominal_vll <= 0):
            raise ValueError("nominal_vll must be finite and positive or None")
        if not _number(low_pf_threshold) or not 0 < low_pf_threshold <= 1:
            raise ValueError("low_pf_threshold must be in (0, 1]")
        if not isinstance(max_samples, int) or max_samples < 3:
            raise ValueError("max_samples must be at least three")
        self.nominal_hz, self.nominal_vll = nominal_hz, nominal_vll
        self.low_pf_threshold, self.max_gap_seconds = low_pf_threshold, max_gap_seconds
        self.voltage_tolerance_pct = voltage_tolerance_pct
        self.frequency_tolerance_hz = frequency_tolerance_hz
        self.power = deque(maxlen=max_samples)
        self.energy = deque(maxlen=max_samples)
        self.previous = None
        self.first_time = None
        self.accepted_samples = self.gap_count = self.counter_resets = self.out_of_order = 0
        self.excluded_gap_time = 0.0
        self.power_integral = self.power_seconds = self.energy_total = self.energy_seconds = 0.0
        self.extrema = {}
        self.durations = {key: [0.0, 0.0] for key in ("LOW_PF", "FREQUENCY_EXCURSION", "VOLTAGE_EXCURSION")}
        self.last_output = {}
        self.last_rejected_stamp = None
        self.break_continuity = False

    def set_nominals(self, voltage, frequency):
        """Expire dependent quality immediately, including duplicate samples.

        Unknown or changed settings break only the relevant excursion duration;
        measurement, demand and energy histories remain independent of settings.
        """
        for attribute, value, prefix, nominal in (
                ("nominal_vll", voltage, "VOLTAGE_EXCURSION", "NOMINAL_VOLTAGE_V"),
                ("nominal_hz", frequency, "FREQUENCY_EXCURSION", "NOMINAL_FREQUENCY_Hz")):
            if value is not None and (not _number(value) or value <= 0):
                raise ValueError(attribute + " must be finite and positive or None")
            if value == getattr(self, attribute):
                continue
            setattr(self, attribute, value)
            self.durations[prefix][0] = 0.0
            if self.previous:
                self.previous["flags"][prefix] = None
            for key in (nominal, prefix+"_ACTIVE", prefix+"_CONTINUOUS_s", prefix+"_TOTAL_s"):
                if key in self.last_output:
                    self.last_output[key].update(valid=False, stale=True)

    def _value(self, document, key):
        item = (document.get("readings") or {}).get(key) or {}
        value = item.get("value")
        age = item.get("age_seconds", document.get("age_seconds"))
        age_valid = age is None or (_number(age) and 0 <= age <= self.max_gap_seconds)
        if item.get("valid") is True and item.get("stale") is False and _number(value) and age_valid:
            return float(value)
        return None

    def _extreme(self, key, values):
        values = [v for v in values if v is not None]
        if not values:
            return
        lo, hi = self.extrema.get(key, (math.inf, -math.inf))
        self.extrema[key] = min(lo, min(values)), max(hi, max(values))

    @staticmethod
    def _imbalance(values):
        if any(v is None for v in values):
            return None
        mean = sum(values) / len(values)
        return 100 * max(abs(v - mean) for v in values) / mean if mean > 0 else None

    def _power_window(self, now):
        start = now - 900.0
        while self.power and self.power[0][1] <= start:
            self.power.popleft()
        integral, coverage, peak, lowest = 0.0, 0.0, -math.inf, math.inf
        for a, b, p0, p1 in self.power:
            lo, hi = max(a, start), min(b, now)
            if hi <= lo:
                continue
            f0, f1 = (lo-a)/(b-a), (hi-a)/(b-a)
            q0, q1 = p0 + (p1-p0)*f0, p0 + (p1-p0)*f1
            integral += (q0+q1) * 0.5 * (hi-lo)
            coverage += hi-lo
            peak, lowest = max(peak, q0, q1), min(lowest, q0, q1)
        complete = abs(coverage-900.0) < 1e-5
        demand = integral/900.0/1000 if complete else None
        factor = 100 * integral/900.0/peak if complete and peak > 0 and lowest >= 0 else None
        return demand, factor, coverage

    def _energy_window(self, now, seconds):
        start = now-seconds
        if len(self.energy) < 2 or self.energy[0][0] > start or self.energy[-1][0] < now:
            return None
        previous = self.energy[0]
        for current in self.energy:
            if current[0] == start:
                return self.energy[-1][1]-current[1]
            if current[0] > start:
                proportion = (start-previous[0])/(current[0]-previous[0])
                boundary = previous[1] + proportion*(current[1]-previous[1])
                return self.energy[-1][1]-boundary
            previous = current
        return None

    def update(self, document, poll_health=None):
        del poll_health  # Serial health belongs to its owning poller, not this sampler.
        stamp = document.get("observed_utc")
        now = _stamp(stamp)
        age = document.get("age_seconds")
        age_valid = age is None or (_number(age) and 0 <= age <= self.max_gap_seconds)
        fresh = (document.get("available") is True and document.get("protocol_valid") is True
                 and document.get("stale") is False and now is not None and age_valid)
        if not fresh:
            self.break_continuity = True
            return {key: {**item, "stale": True} for key, item in self.last_output.items()}
        if self.previous is not None and now <= self.previous["time"]:
            if now < self.previous["time"]:
                if now != self.last_rejected_stamp:
                    self.out_of_order += 1
                    self.last_rejected_stamp = now
                self.break_continuity = True
                output = {key: {**item, "stale": True} for key, item in self.last_output.items()}
                if "OUT_OF_ORDER_COUNT" in output:
                    output["OUT_OF_ORDER_COUNT"]["value"] = self.out_of_order
                return output
            return {key: dict(item) for key, item in self.last_output.items()}
        get = lambda key: self._value(document, key)
        currents = [get(key) for key in ("IA", "IB", "IC")]
        voltages = [get(key) for key in ("VAB", "VBC", "VCA")]
        currents = [v if v is not None and v >= 0 else None for v in currents]
        voltages = [v if v is not None and v >= 0 else None for v in voltages]
        power, frequency, pf, energy = get("P_W"), get("FREQUENCY_Hz"), get("PF"), get("ENERGY_kWh")
        frequency = frequency if frequency is not None and frequency > 0 else None
        pf = abs(pf) if pf is not None and abs(pf) <= 1 else None
        energy = energy if energy is not None and 0 <= energy <= 16777215 else None
        flags = {"LOW_PF": pf < self.low_pf_threshold if pf is not None else None,
                 "FREQUENCY_EXCURSION": abs(frequency-self.nominal_hz) > self.frequency_tolerance_hz
                 if frequency is not None and self.nominal_hz is not None else None,
                 "VOLTAGE_EXCURSION": any(abs(v-self.nominal_vll) > self.nominal_vll*self.voltage_tolerance_pct/100 for v in voltages)
                 if self.nominal_vll is not None and all(v is not None for v in voltages) else None}
        dt = now-self.previous["time"] if self.previous else None
        consecutive = dt is not None and dt <= self.max_gap_seconds and not self.break_continuity
        if dt is not None and dt > self.max_gap_seconds:
            self.gap_count += 1
            self.excluded_gap_time += dt
        previous = self.previous or {}
        load_change = (power-previous["power"])/1000 if consecutive and power is not None and previous.get("power") is not None else None
        if consecutive and power is not None and previous.get("power") is not None:
            self.power.append((previous["time"], now, previous["power"], power))
            self.power_integral += (previous["power"]+power)*0.5*dt
            self.power_seconds += dt
        for key, flag in flags.items():
            uninterrupted = consecutive and flag is True and previous.get("flags", {}).get(key) is True
            if uninterrupted:
                self.durations[key][0] += dt
                self.durations[key][1] += dt
            else:
                self.durations[key][0] = 0.0
        decreasing = energy is not None and previous.get("energy") is not None and energy < previous["energy"]
        if decreasing:
            self.counter_resets += 1
        good_energy_interval = consecutive and energy is not None and previous.get("energy") is not None and not decreasing
        if good_energy_interval:
            self.energy_total += energy-previous["energy"]
            self.energy_seconds += dt
        else:
            self.energy.clear()
        if energy is not None:
            self.energy.append((now, energy))
            # Keep one observation at/before the oldest rolling-window boundary.
            while len(self.energy) > 2 and self.energy[1][0] <= now-86400:
                self.energy.popleft()
        self._extreme("VLL", voltages)
        self._extreme("CURRENT", currents)
        self._extreme("FREQUENCY", [frequency])
        self._extreme("PF", [pf])
        self._extreme("POWER", [power])
        self.accepted_samples += 1
        if self.first_time is None:
            self.first_time = now
        demand, load_factor, rolling_coverage = self._power_window(now)
        hour_energy, day_energy = self._energy_window(now, 3600), self._energy_window(now, 86400)
        all_currents = all(v is not None for v in currents)
        highest = max(currents) if all_currents else None
        values = {
            "VLL_IMBALANCE_pct": self._imbalance(voltages), "CURRENT_IMBALANCE_pct": self._imbalance(currents),
            "HIGHEST_CURRENT_PHASE": currents.index(highest)+1 if highest is not None and highest > 0 else None,
            "HIGHEST_CURRENT_A": highest,
            "SESSION_AVERAGE_POWER_kW": self.power_integral/self.power_seconds/1000 if self.power_seconds else None,
            "SESSION_PEAK_POWER_kW": self.extrema["POWER"][1]/1000 if "POWER" in self.extrema else None,
            "LOAD_CHANGE_kW": load_change,
            "ROLLING_15MIN_DEMAND_kW": demand, "ROLLING_15MIN_LOAD_FACTOR_pct": load_factor,
            "SESSION_POWER_COVERAGE_h": self.power_seconds/3600,
            "ROLLING_POWER_COVERAGE_s": rolling_coverage,
            "OBSERVED_ENERGY_kWh": self.energy_total if self.energy_seconds > 0 else None,
            "ROLLING_HOUR_ENERGY_kWh": hour_energy, "ROLLING_DAY_ENERGY_kWh": day_energy,
            "CONTINUOUS_ENERGY_COVERAGE_h": (now-self.energy[0][0])/3600 if self.energy else None,
            "SESSION_ENERGY_COVERAGE_h": self.energy_seconds/3600,
            "ACCEPTED_SAMPLES": self.accepted_samples, "DIAGNOSTICS_AGE_s": now-self.first_time,
            "SAMPLE_GAP_COUNT": self.gap_count, "ENERGY_COUNTER_RESET_COUNT": self.counter_resets,
            "OUT_OF_ORDER_COUNT": self.out_of_order, "EXCLUDED_GAP_TIME_s": self.excluded_gap_time,
            "NOMINAL_VOLTAGE_V": self.nominal_vll, "NOMINAL_FREQUENCY_Hz": self.nominal_hz,
            "LOW_PF_THRESHOLD": self.low_pf_threshold, "VOLTAGE_TOLERANCE_pct": self.voltage_tolerance_pct,
            "FREQUENCY_TOLERANCE_Hz": self.frequency_tolerance_hz, "LATEST_SAMPLE_INTERVAL_s": dt,
            "ROLLING_DEMAND_READY": demand is not None, "ROLLING_HOUR_ENERGY_READY": hour_energy is not None,
            "ROLLING_DAY_ENERGY_READY": day_energy is not None,
        }
        for key, suffix in (("VLL", "V"), ("CURRENT", "A"), ("FREQUENCY", "Hz"), ("PF", "")):
            extremes = self.extrema.get(key, (None, None))
            for index, side in enumerate(("MIN", "MAX")):
                values["SESSION_"+key+"_"+side+("_"+suffix if suffix else "")] = extremes[index]
        for key, flag in flags.items():
            values[key+"_ACTIVE"] = flag
            values[key+"_CONTINUOUS_s"] = self.durations[key][0] if flag is not None else None
            values[key+"_TOTAL_s"] = self.durations[key][1] if flag is not None else None
        self.last_output = {spec["key"]: {"value": values.get(spec["key"]), "valid": values.get(spec["key"]) is not None,
                            "stale": False, "source": "derived", "observed_utc": stamp,
                            "unit": spec["units"], "description": spec["description"]}
                            for spec in FIELD_SPECS}
        self.previous = {"time": now, "power": power, "energy": energy, "flags": flags}
        self.break_continuity = False
        return {key: dict(item) for key, item in self.last_output.items()}
