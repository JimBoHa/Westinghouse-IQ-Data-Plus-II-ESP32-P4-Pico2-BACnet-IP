#!/usr/bin/env python3
"""Strict, hardware-free IQ Data Plus II diagnostic reply decoding.

IL17384 chapter108 pp.108-2--3 defines flags (3/C/8), setpoints (3/C/9),
and trip data (3/C/A). TD17271A pp.30--38 defines the DIP switch tables.
Sources:
https://pps2.com/communications/files/legacyPMP/files/il17384v30/partb/108_iqdataplusii.pdf
https://pps2.com/communications/files/legacyPMP/products/iqdpii/docs/td17271a_pp31_43.pdf

Input is the complete DATA-only reply, including its count header, as raw
25-bit local write words. The caller establishes transaction provenance and
adds the observation time. A trip-buffer read time is never an event time.
No commands, hardware access, reset operations, or database writes occur here.
"""

import argparse
import json

from decode_meter import decode_request


COUNTS = {"flags": 2, "settings": 3, "trip": 18}
ALIASES = {name: name for name in COUNTS}
ALIASES.update({name + "_repeat": name for name in COUNTS})

# Raw switch bit n-1 is 1 for OFF and 0 for ON. These are the Plus II
# tables, not the older chapter107 jumper table (which lacks 3200 A).
CT_PRIMARY_AMPS = (100, 150, 200, 250, 300, 400, 500, 600, 800, 1000,
                   1200, 1500, 1600, 2000, 2500, 3000, 3200, 4000, 5000)
PT_RATIOS = (1, 2, 4, 5, 20, 30, 35, 40, 55, 60, 70, 100, 120, 30, 60, 100)
NOMINAL_VOLTS = ((120, 69), (208, 120), (220, 127), (240, 138),
                 (380, 219), (416, 240), (460, 266), (575, 332))
PULSE_WH = (100, 500, 1000, 5000, 10000, 50000, 100000, 500000,
            1000000, 5000000, 10000000, 50000000, 100000000,
            500000000, 500000000, 500000000)

FLAG_FIELDS = (
    ("TRIP", "Trip", "Trip flag"),
    ("ALARM", "Alarm", "Alarm flag"),
    ("EXTERNAL", "External", "External cause"),
    ("OVERVOLTAGE", "Overvoltage", "Overvoltage cause"),
    ("UNDERVOLTAGE", "Undervoltage", "Undervoltage cause"),
    ("PHASE_UNBALANCE", "Phase-Unbalance", "Phase unbalance cause"),
    ("PHASE_LOSS", "Phase-Loss", "Phase loss cause"),
    ("PHASE_REVERSAL", "Phase-Reversal", "Phase reversal cause"),
    ("RAM_ROM_FAILURE", "RAM-ROM-Failure", "RAM or ROM failure cause"),
)

TRIP_FIELDS = (
    ("IA", "Current-A", "amperes"),
    ("IB", "Current-B", "amperes"),
    ("IC", "Current-C", "amperes"),
    ("VAB", "Voltage-AB", "volts"),
    ("VBC", "Voltage-BC", "volts"),
    ("VCA", "Voltage-CA", "volts"),
    ("VAN", "Voltage-AN", "volts"),
    ("VBN", "Voltage-BN", "volts"),
    ("VCN", "Voltage-CN", "volts"),
    ("P_W", "Real-Power", "watts"),
    ("DEMAND_W", "Demand", "watts"),
    ("ENERGY_Wh", "Energy", "wattHours"),
    ("FREQUENCY_Hz", "Frequency", "hertz"),
    ("Q_var", "Reactive-Power", "voltAmperesReactive"),
    ("PF", "Power-Factor", "powerFactor"),
)

CONFIG_ANALOG_FIELDS = (
    ("CT_PRIMARY_A", "CT-Primary", "amperes", "Configured CT primary rating; not a current measurement"),
    ("CT_SECONDARY_A", "CT-Secondary", "amperes", "Configured CT secondary rating, 5 A"),
    ("FREQUENCY_Hz", "Line-Frequency", "hertz", "Configured nominal line frequency"),
    ("WIRING_WIRES", "Wiring-Wires", "noUnits", "Configured AC wiring: 3 or 4 wires"),
    ("PT_RATIO", "PT-Ratio", "noUnits", "Configured PT primary-to-secondary voltage ratio"),
    ("DEMAND_MINUTES", "Demand-Window", "minutes", "Configured internal demand window; bypassed when external sync is enabled"),
    ("NOMINAL_LL_V", "Nominal-Voltage-LL", "volts", "Configured nominal line-to-line voltage; international PT selection overrides SW4"),
    ("NOMINAL_LN_V", "Nominal-Voltage-LN", "volts", "Configured nominal line-to-neutral voltage; international PT selection overrides SW4"),
    ("PULSE_Wh", "Pulse-Energy", "wattHours", "Configured energy per output pulse; not an energy measurement"),
    ("OVERVOLTAGE_PERCENT", "Overvoltage-Threshold", "percent", "Configured percent of nominal voltage for overvoltage detection"),
    ("UNDERVOLTAGE_PERCENT", "Undervoltage-Threshold", "percent", "Configured percent of nominal voltage for undervoltage detection"),
    ("UNBALANCE_PERCENT", "Unbalance-Threshold", "percent", "Configured phase unbalance detection percent"),
    ("PROTECTION_DELAY_S", "Protection-Delay", "seconds", "Configured protective delay; used only when delayed action is enabled"),
)

CONFIG_BINARY_FIELDS = (
    ("DELAY_ENABLED", "Delay-Enabled", "Configured delayed protection action"),
    ("FOUR_WIRE", "Four-Wire", "Configured four-wire AC system; false means three-wire"),
    ("DEMAND_SYNC_ENABLED", "Demand-Sync-Enabled", "External demand synchronization enabled; overrides internal demand window"),
    ("PROTECTION_ENABLED", "Protection-Enabled", "Internal protection functions enabled; external trip remains possible independently"),
    ("TEST_MODE", "Test-Mode", "Factory test switch is ON; readout only, never activates the test"),
    ("ALTERNATE_PF", "Alternate-PF", "Alternate power-factor calculation enabled by SW5 switch4 OFF"),
    ("AUTO_RESET_ENABLED", "Auto-Reset-Enabled", "Automatic reset attempts enabled; readout only"),
    ("ENERGY_RESET_ENABLED", "Energy-Reset-Enabled", "Front-panel energy reset enable switch; reading does not reset energy"),
    ("OVERVOLTAGE_TRIP_ENABLED", "Overvoltage-Trip-Enabled", "Configured overvoltage trip response; not relay state"),
    ("OVERVOLTAGE_ALARM_ENABLED", "Overvoltage-Alarm-Enabled", "Configured overvoltage alarm response; not relay state"),
    ("UNDERVOLTAGE_TRIP_ENABLED", "Undervoltage-Trip-Enabled", "Configured undervoltage trip response; not relay state"),
    ("UNDERVOLTAGE_ALARM_ENABLED", "Undervoltage-Alarm-Enabled", "Configured undervoltage alarm response; not relay state"),
    ("PHASE_LOSS_REVERSAL_TRIP_ENABLED", "Phase-Loss-Reversal-Trip-Enabled", "Configured phase loss/reversal trip response; not relay state"),
    ("PHASE_LOSS_REVERSAL_ALARM_ENABLED", "Phase-Loss-Reversal-Alarm-Enabled", "Configured phase loss/reversal alarm response; not relay state"),
    ("PHASE_UNBALANCE_TRIP_ENABLED", "Phase-Unbalance-Trip-Enabled", "Configured phase unbalance trip response; not relay state"),
    ("PHASE_UNBALANCE_ALARM_ENABLED", "Phase-Unbalance-Alarm-Enabled", "Configured phase unbalance alarm response; not relay state"),
    ("PT_INTERNATIONAL", "PT-International", "International 110 V PT secondary selection; overrides nominal voltage selection"),
)


def _spec(key, name, kind, instance, units, description, group):
    return {"key": key, "name": "IQData-" + name, "type": kind,
            "instance": instance, "units": units, "description": description,
            "group": group}


FIELD_SPECS = []
for group, start in (("flags", 10), ("trip", 30)):
    for offset, (key, name, description) in enumerate(FLAG_FIELDS):
        context = "Current diagnostic buffer" if group == "flags" else "Trip buffer; event time and retention are unknown"
        FIELD_SPECS.append(_spec(group.upper() + "_" + key,
                                group.title() + "-" + name, "binary", start + offset,
                                "noUnits", context + ": " + description, group))
FIELD_SPECS.append(_spec("TRIP_DATA_VALID", "Trip-Data-Valid", "binary", 39, "noUnits",
                        "Trip flag is set and all trip measurements have valid scale flags; event time unknown, never live data", "trip"))
for offset, (key, name, units) in enumerate(TRIP_FIELDS):
    FIELD_SPECS.append(_spec("TRIP_" + key, "Trip-" + name, "analog", 100 + offset,
                            units, "Trip-buffer " + name + "; event time and retention unknown; not a live measurement", "trip"))
for instance, key, name in ((200, "FIRMWARE_REVISION", "Firmware-Revision"),
                             (201, "FIRMWARE_VERSION", "Firmware-Version")):
    FIELD_SPECS.append(_spec(key, name, "analog", instance, "noUnits",
                            "Raw firmware byte from setpoints buffer; not communication protocol version", "settings"))
for bank in range(1, 7):
    FIELD_SPECS.append(_spec(f"SW{bank}_RAW", f"SW{bank}-Raw", "analog", 201 + bank,
                            "noUnits", f"Raw SW{bank} byte; each bit value 1 means OFF and 0 means ON", "settings"))
    for switch in range(1, 9):
        FIELD_SPECS.append(_spec(f"SW{bank}_{switch}_ON", f"SW{bank}-{switch}-ON", "binary",
                                100 + (bank - 1) * 8 + switch - 1, "noUnits",
                                f"SW{bank} switch{switch} is ON; reports configuration, does not change it", "settings"))
for offset, (key, name, units, description) in enumerate(CONFIG_ANALOG_FIELDS):
    FIELD_SPECS.append(_spec("CONFIG_" + key, "Config-" + name, "analog", 210 + offset,
                            units, description, "settings"))
for offset, (key, name, description) in enumerate(CONFIG_BINARY_FIELDS):
    FIELD_SPECS.append(_spec("CONFIG_" + key, "Config-" + name, "binary", 150 + offset,
                            "noUnits", description, "settings"))


def _reading(value, **metadata):
    return {"value": value, "valid": value is not None, **metadata}


def _parse_words(kind, words):
    supplied = list(words)
    result = {"kind": kind, "protocol_valid": False, "complete": False,
              "valid": False, "all_readings_valid": False, "readings": {},
              "errors": [], "warnings": [], "input_words": supplied,
              "raw_words": [], "words": [], "reserved": [],
              "expected_words": COUNTS.get(kind), "received_words": len(supplied)}
    if kind not in COUNTS:
        result["errors"].append(f"unsupported diagnostic kind: {kind}")
        return result
    if len(supplied) != COUNTS[kind]:
        result["errors"].append(f"expected exactly {COUNTS[kind]} DATA words, received {len(supplied)}")
    for index, value in enumerate(supplied):
        try:
            if isinstance(value, str):
                word = int(value, 0)
            elif type(value) is int:
                word = value
            else:
                raise ValueError("not an integer")
        except ValueError:
            result["raw_words"].append(None)
            result["words"].append({"index": index, "input": value, "valid_word": False})
            result["errors"].append(f"word {index}: not an integer or numeric string")
            continue
        result["raw_words"].append(word)
        if not 0 <= word <= 0x1FFFFFF:
            result["words"].append({"index": index, "raw_word": word, "valid_word": False})
            result["errors"].append(f"word {index}: outside 25-bit range")
            continue
        result["words"].append({"index": index, "raw_word": word,
                                "hex": f"0x{word:07x}", "control": word & 1,
                                "payload": word >> 1, "payload_hex": f"0x{word >> 1:06x}",
                                "valid_word": True})
        if word & 1:
            result["errors"].append(f"word {index}: control message, expected DATA")
    if result["errors"]:
        return result
    header = result["words"][0]["payload"]
    additional = header & 0xFF
    result["header"] = {"raw_payload": header, "additional_messages": additional,
                        "byte1": (header >> 8) & 0xFF, "byte2": header >> 16}
    if additional != COUNTS[kind] - 1:
        result["errors"].append(f"header requires {additional} additional messages; documented {kind} buffer requires {COUNTS[kind] - 1}")
        return result
    if kind != "settings":
        result["reserved"].append({"index": 0, "raw_payload": header,
                                    "reserved_bytes": header >> 8})
        if header >> 8:
            result["warnings"].append("Reserved header bytes are nonzero; retained without interpretation.")
    result["protocol_valid"] = result["complete"] = True
    return result


def _decode_flags(result, payload, prefix):
    trip, alarm, causes = payload & 0xFF, (payload >> 8) & 0xFF, payload >> 16
    result["flags"] = {"raw_payload": payload, "trip_byte": trip, "alarm_byte": alarm,
                       "cause_byte": causes, "reserved_cause_bits": causes & 0x80}
    values = [trip, alarm] + [bool(causes & (1 << n)) for n in range(7)]
    for index, ((key, _, _), value) in enumerate(zip(FLAG_FIELDS, values)):
        valid = index >= 2 or value in (0, 1)
        result["readings"][prefix + key] = _reading(bool(value) if valid else None,
            raw_payload=payload, raw_value=value, word_index=1, context=result["kind"])
        if not valid:
            result["errors"].append(f"{prefix + key}: expected boolean byte 0 or 1, received {value}")
    if causes & 0x80:
        result["warnings"].append("Reserved FLAGS3 bit7 is set; known cause bits decoded, bit7 retained uninterpreted.")


def _decode_settings(result):
    payloads = [word["payload"] for word in result["words"]]
    sw = [(payloads[1] >> shift) & 0xFF for shift in (0, 8, 16)]
    sw += [(payloads[2] >> shift) & 0xFF for shift in (0, 8, 16)]
    readings = result["readings"]
    readings["FIRMWARE_REVISION"] = _reading(result["header"]["byte1"], word_index=0, byte_index=1)
    readings["FIRMWARE_VERSION"] = _reading(result["header"]["byte2"], word_index=0, byte_index=2)
    for bank, byte in enumerate(sw, 1):
        index = 1 + (bank - 1) // 3
        readings[f"SW{bank}_RAW"] = _reading(byte, word_index=index, byte_index=(bank - 1) % 3)
        for switch in range(1, 9):
            readings[f"SW{bank}_{switch}_ON"] = _reading(not bool(byte & (1 << (switch - 1))),
                raw_switch_byte=byte, switch_bank=bank, switch_number=switch)
    a, b, c, d, e, f = sw
    ct_code = a & 0x1F
    pt_code = c & 0xF
    international = pt_code >= 13
    ll, ln = (110, 64) if international else NOMINAL_VOLTS[d & 7]
    values = {
        "CT_PRIMARY_A": CT_PRIMARY_AMPS[ct_code] if ct_code < len(CT_PRIMARY_AMPS) else None,
        "CT_SECONDARY_A": 5,
        "FREQUENCY_Hz": 50 if a & 0x20 else 60,
        "WIRING_WIRES": 3 if a & 0x80 else 4,
        "PT_RATIO": PT_RATIOS[pt_code],
        "DEMAND_MINUTES": (5, 10, 15, 30)[(c >> 4) & 3],
        "NOMINAL_LL_V": ll, "NOMINAL_LN_V": ln,
        "PULSE_Wh": PULSE_WH[d >> 4],
        "OVERVOLTAGE_PERCENT": 105 + 5 * (e & 7),
        "UNDERVOLTAGE_PERCENT": 95 - 5 * (e >> 5),
        "UNBALANCE_PERCENT": 5 + 5 * (f & 7),
        "PROTECTION_DELAY_S": 1 + ((f >> 4) & 7),
        "DELAY_ENABLED": not bool(a & 0x40),
        "FOUR_WIRE": not bool(a & 0x80),
        "DEMAND_SYNC_ENABLED": not bool(c & 0x40),
        "PROTECTION_ENABLED": not bool(c & 0x80),
        "TEST_MODE": not bool(d & 8),
        "ALTERNATE_PF": bool(e & 8),
        "AUTO_RESET_ENABLED": not bool(e & 0x10),
        "ENERGY_RESET_ENABLED": not bool(f & 8),
        "PT_INTERNATIONAL": international,
    }
    for offset, condition in enumerate(("OVERVOLTAGE", "UNDERVOLTAGE",
                                         "PHASE_LOSS_REVERSAL", "PHASE_UNBALANCE")):
        values[condition + "_TRIP_ENABLED"] = bool(b & (1 << (offset * 2)))
        values[condition + "_ALARM_ENABLED"] = bool(b & (1 << (offset * 2 + 1)))
    for key, value in values.items():
        readings["CONFIG_" + key] = _reading(value, method="documented_switch_table",
                                             source="TD17271A pp.30-38", context="configuration")
    if values["CT_PRIMARY_A"] is None:
        result["errors"].append(f"CONFIG_CT_PRIMARY_A: undocumented CT switch code {ct_code}")
        readings["CONFIG_CT_PRIMARY_A"]["raw_code"] = ct_code
    readings["CONFIG_DEMAND_MINUTES"]["internal_window_enabled"] = not values["DEMAND_SYNC_ENABLED"]
    readings["CONFIG_PROTECTION_DELAY_S"]["delay_enabled"] = values["DELAY_ENABLED"]
    result["switch_encoding"] = "raw bit n-1: 1=OFF, 0=ON for switch n"
    result["warnings"].append("Configuration readout only; configured protection responses do not report relay contact state.")


def _decode_trip(result):
    _decode_flags(result, result["words"][1]["payload"], "TRIP_")
    readings = result["readings"]
    trip_flag = readings["TRIP_TRIP"]
    alarm_flag = readings["TRIP_ALARM"]
    flag_bytes_valid = trip_flag["valid"] and alarm_flag["valid"]
    event_evidence = bool(flag_bytes_valid and trip_flag["value"] is True)
    result.update(event_evidence=event_evidence, event_time=None,
                  measurement_context="trip_buffer_not_live", retention_verified=False)
    all_scales_valid = True
    # One reserved current word makes 16 slots but only 15 measurements.
    for kind, start, count in (("currents", 2, 4), ("line_voltages", 6, 3),
                                ("neutral_voltages", 9, 3), ("power1", 12, 3),
                                ("power2", 15, 3)):
        decoded = decode_request(kind, result["raw_words"][start:start + count])
        for key, item in decoded["readings"].items():
            reading = {**item, "word_index": item["word_index"] + start,
                       "scale_valid": item["valid"], "context": "trip_buffer_not_live",
                       "event_evidence": event_evidence, "event_time": None}
            all_scales_valid &= item["valid"]
            if not event_evidence:
                reading.update(value=None, value_exact=None, valid=False,
                               unavailable_reason="No valid asserted trip flag; buffer contents are not qualified as a retained event")
            readings["TRIP_" + key] = reading
        for reserved in decoded["reserved"]:
            result["reserved"].append({**reserved, "index": reserved["index"] + start})
        result["errors"].extend("trip " + error for error in decoded["errors"])
    snapshot_valid = bool(event_evidence and all_scales_valid)
    readings["TRIP_DATA_VALID"] = _reading(snapshot_valid if flag_bytes_valid else None,
        context="trip_buffer_quality", event_time=None,
        interpretation="Asserted trip flag and all scale flags valid; neither event time nor retention behavior is established")
    result["trip_data_valid"] = snapshot_valid
    result["warnings"].append("Trip-buffer read time is not an event timestamp. These values must never replace live measurements; retention behavior is unverified.")
    if not event_evidence:
        result["warnings"].append("No qualified trip evidence: trip measurements are unavailable even if their scale bits are valid.")


def decode_diagnostics(kind, words):
    """Decode flags/settings/trip or their *_repeat aliases.

    protocol_valid/complete checks exact framing and documented count header.
    valid additionally requires every returned field to be available and valid.
    A well-formed no-trip reply therefore has protocol_valid=True, valid=False,
    and an available False TRIP_DATA_VALID flag. This is not a transport failure.
    Malformed framing yields no readings. Invalid fields keep raw evidence and
    do not invalidate usable sibling fields. FIELD_SPECS units match values
    directly; no scaling or timestamp is applied by this module.
    """
    canonical = ALIASES.get(kind, kind)
    result = _parse_words(canonical, words)
    if not result["protocol_valid"]:
        return result
    if canonical == "flags":
        _decode_flags(result, result["words"][1]["payload"], "FLAGS_")
    elif canonical == "settings":
        _decode_settings(result)
    else:
        _decode_trip(result)
    result["all_readings_valid"] = all(item["valid"] for item in result["readings"].values())
    result["valid"] = result["all_readings_valid"] and not result["errors"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=sorted(ALIASES))
    parser.add_argument("words", nargs="*", help="DATA-only raw25-bit words, including header")
    args = parser.parse_args()
    result = decode_diagnostics(args.kind, args.words)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["protocol_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
