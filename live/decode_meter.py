#!/usr/bin/env python3
"""Decode IQ Data Plus II DATA replies, without reading hardware.

Input words are 25-bit local writes: bit 0 is control, bits 1..24 are payload.
The caller must identify the request and establish transaction provenance. Do
not pass TX-complete images, the request image, or a repeat-request handshake.

Sources (linked originals; copyrighted manuals are not bundled):
* IL17384 Part A, August 2011, printed pp.14,18,24-25: scaling/status/buffers.
  https://pps2.com/communications/files/INCOM/incom_17384_partA_8-2011.pdf
* IL17384 chapter5, March1998, pp.5-8--5-9: original all-standard buffer map.
  https://pps2.com/communications/files/legacyPMP/files/il17384v30/parta/005_standardmasterslave.pdf
* IL17384 chapter108, November1998, p.108-1: IQ Data Plus II identity and the
  reserved fourth current word (overrides the general IX-current definition).
  https://pps2.com/communications/files/legacyPMP/files/il17384v30/partb/108_iqdataplusii.pdf
"""

import argparse
from decimal import Decimal, localcontext
import json


ALIASES = {
    "status": "fast_status", "fast_status": "fast_status",
    "fast_status_repeat": "fast_status",
    "voltage": "line_voltages", "line_voltages": "line_voltages",
    "line_voltages_repeat": "line_voltages",
    "current": "currents", "currents": "currents",
    "currents_repeat": "currents",
    "neutral_voltages": "neutral_voltages",
    "neutral_voltages_repeat": "neutral_voltages",
    "power1": "power1", "power1_repeat": "power1",
    "power2": "power2", "power2_repeat": "power2",
    "energy": "energy", "energy_repeat": "energy",
    "all_standard": "all_standard", "all_standard_repeat": "all_standard",
}
COUNTS = {"fast_status": 1, "line_voltages": 3, "currents": 4,
          "neutral_voltages": 3, "power1": 3, "power2": 3, "energy": 1}
STANDARD_BUFFERS = ((5, "currents"), (6, "line_voltages"),
                    (7, "neutral_voltages"), (8, "power1"),
                    (9, "power2"), (10, "energy"))
ANALOG_FIELDS = {
    "currents": (("IA", "A"), ("IB", "A"), ("IC", "A")),
    "line_voltages": (("VAB", "V"), ("VBC", "V"), ("VCA", "V")),
    "neutral_voltages": (("VAN", "V"), ("VBN", "V"), ("VCN", "V")),
    "power1": (("P_W", "W"), ("DEMAND_W", "W"), ("ENERGY_Wh", "Wh")),
    "power2": (("FREQUENCY_Hz", "Hz"), ("Q_var", "var"), ("PF", "1")),
}


def decode_impacc(payload):
    """Decode one 24-bit IMPACC payload, retaining invalid-value metadata.

    Invalid data returns no numeric value. The exact decimal companion avoids
    losing information if a downstream JSON consumer uses binary floating point.
    """
    if type(payload) is not int or not 0 <= payload <= 0xFFFFFF:
        raise ValueError("IMPACC payload must be a 24-bit integer")
    scale = payload >> 16
    unsigned_mantissa = payload & 0xFFFF
    signed = bool(scale & 0x80)
    mantissa = unsigned_mantissa
    if signed and mantissa & 0x8000:
        mantissa -= 0x10000
    exponent = scale & 0x1F
    if exponent & 0x10:
        exponent -= 0x20
    base = 10 if scale & 0x20 else 2
    valid = bool(scale & 0x40)
    value = None
    exact = None
    if valid:
        with localcontext() as context:
            context.prec = 64
            number = Decimal(mantissa) * Decimal(base) ** exponent
            exact = format(number, "f")
            value = float(number)
    return {
        "raw_payload": payload,
        "payload_hex": f"0x{payload:06x}",
        "scale_byte": scale,
        "unsigned_mantissa": unsigned_mantissa,
        "signed": signed,
        "mantissa": mantissa,
        "base": base,
        "exponent": exponent,
        "valid": valid,
        "value": value,
        "value_exact": exact,
    }


def decode_request(kind, words):
    """Return a JSON-safe result for a complete DATA-only reply buffer.

    Accepted words: integer 0..0x1ffffff, or decimal/0x-prefixed strings.
    `complete` means the exact count and DATA/control framing passed, not that
    physical provenance is proved; `protocol_valid` reports the same structural
    result. `valid` additionally requires valid analog
    scale flags, or a recognized IQ Data Plus II status identity. Structural
    failure produces no readings/status. Invalid analog items remain present
    with value=None; valid siblings remain available with overall valid=False.
    Unknown kinds and malformed words are reported in errors, not silently
    truncated, masked, reordered, or treated as measurements.
    """
    canonical = ALIASES.get(kind)
    supplied = list(words)
    result = {
        "kind": canonical or kind,
        "valid": False,
        "complete": False,
        "protocol_valid": False,
        "all_readings_valid": False,
        "errors": [],
        "warnings": [],
        "input_words": supplied,
        "raw_words": [],
        "words": [],
        "status": None,
        "readings": {},
        "reserved": [],
    }
    if canonical is None:
        result["errors"].append(f"unsupported request kind: {kind}")
        return result
    expected = COUNTS.get(canonical)
    result["expected_words"] = expected
    result["received_words"] = len(supplied)
    if expected is not None and len(supplied) != expected:
        result["errors"].append(
            f"expected exactly {expected} DATA words, received {len(supplied)}")
    for index, supplied_word in enumerate(supplied):
        try:
            if isinstance(supplied_word, str):
                word = int(supplied_word, 0)
            elif type(supplied_word) is int:
                word = supplied_word
            else:
                raise ValueError("not an integer or numeric string")
        except ValueError:
            result["raw_words"].append(None)
            result["words"].append({"index": index, "input": supplied_word,
                                    "valid_word": False})
            result["errors"].append(f"word {index}: not an integer or numeric string")
            continue
        result["raw_words"].append(word)
        if not 0 <= word <= 0x1FFFFFF:
            result["words"].append({"index": index, "raw_word": word,
                                    "valid_word": False})
            result["errors"].append(f"word {index}: outside 25-bit range")
            continue
        control = word & 1
        payload = word >> 1
        result["words"].append({
            "index": index, "raw_word": word, "hex": f"0x{word:07x}",
            "control": control, "payload": payload,
            "payload_hex": f"0x{payload:06x}", "valid_word": True,
        })
        if control:
            result["errors"].append(f"word {index}: control message, expected DATA")
    if canonical == "all_standard" and not result["errors"]:
        if not supplied:
            result["errors"].append("all-standard reply requires a capability-map DATA word")
        else:
            header = result["words"][0]["payload"]
            # 1998 section5.2.2.2: C0..CA, six-bit expanded-buffer count,
            # and reserved zero bits. Never guess unknown buffer lengths.
            unsupported_bits = header & ~0x3F07FF
            sequence = [(subcommand, name) for subcommand, name in STANDARD_BUFFERS
                        if header & (1 << subcommand)]
            result["buffer_map"] = {
                "raw_payload": header,
                "supported_subcommands": [n for n in range(11) if header & (1 << n)],
                "expanded_buffer_count": (header >> 16) & 0x3F,
                "unsupported_bits": unsupported_bits,
                "included_buffers": [name for _, name in sequence],
            }
            if unsupported_bits:
                result["errors"].append(
                    f"unsupported/reserved all-standard map bits: 0x{unsupported_bits:06x}")
            else:
                expected = 1 + sum(COUNTS[name] for _, name in sequence)
                result["expected_words"] = expected
                if len(supplied) != expected:
                    result["errors"].append(
                        f"map requires exactly {expected} DATA words, received {len(supplied)}")
    if result["errors"]:
        return result
    result["complete"] = True
    result["protocol_valid"] = True
    payloads = [word["payload"] for word in result["words"]]
    if canonical == "fast_status":
        payload = payloads[0]
        division = payload & 0x3F
        version = (payload >> 6) & 0xF
        product = (payload >> 10) & 0x3F
        status = payload >> 16
        matches = division == 1 and (product == 22 or (product == 2 and version >= 5))
        names = {2: "IQ Data Plus II", 22: "IQ Data Plus II HV"}
        state_names = ["normal_inactive", "normal_active", "tripped", "alarmed"]
        result["status"] = {
            "raw_payload": payload, "division": division,
            "product_id": product, "communication_version": version,
            "product_name": names.get(product) if matches else None,
            "matches_iq_data_plus_ii": matches,
            "status_byte": status, "status_hex": f"0x{status:02x}",
            "state_code": status >> 6, "state": state_names[status >> 6],
            "remote_open_or_off": bool(status & 0x20),
            "product_specific_bits": status & 0x1F,
        }
        if not matches:
            result["errors"].append("status identity does not match documented IQ Data Plus II")
        result["warnings"].append(
            "Status bits S4..S0 are retained without model-specific interpretation; "
            "communication version is not the meter firmware version.")
    elif canonical == "all_standard":
        offset = 1
        result["buffers"] = []
        for subcommand, name in sequence:
            count = COUNTS[name]
            decoded = decode_request(name, result["raw_words"][offset:offset + count])
            result["buffers"].append({"kind": name, "subcommand": subcommand,
                                      "word_index": offset, "word_count": count,
                                      "valid": decoded["valid"]})
            for label, reading in decoded["readings"].items():
                result["readings"][label] = {
                    **reading, "word_index": reading["word_index"] + offset,
                    "source_kind": name,
                }
            for reserved in decoded["reserved"]:
                result["reserved"].append({**reserved, "index": reserved["index"] + offset})
            result["errors"].extend(f"{name}: {error}" for error in decoded["errors"])
            result["warnings"].extend(decoded["warnings"])
            offset += count
        result["warnings"].append(
            "All-standard buffers arrive sequentially; simultaneous measurement sampling is not established.")
    elif canonical == "energy":
        # SCOMM A has no sign, exponent, or validity flag: every payload bit is
        # part of one unsigned kWh counter. Do not apply IMPACC float decoding.
        payload = payloads[0]
        result["readings"]["ENERGY_kWh"] = {
            "raw_payload": payload, "payload_hex": f"0x{payload:06x}",
            "encoding": "uint24", "value": payload, "value_exact": str(payload),
            "unit": "kWh", "valid": True, "word_index": 0,
        }
    else:
        for index, (label, unit) in enumerate(ANALOG_FIELDS[canonical]):
            reading = decode_impacc(payloads[index])
            reading["unit"] = unit
            reading["word_index"] = index
            if label == "PF":
                reading["method"] = "meter_reported"
                reading["sign_convention"] = "negative=lagging; positive=leading"
                reading["phase_relation"] = (
                    "lagging" if reading["value"] < 0 else
                    "leading" if reading["value"] > 0 else "undetermined"
                ) if reading["valid"] else None
                reading["calculation_mode"] = "unknown_meter_setting"
                reading["interpretation"] = (
                    "TD17271A allows standard or alternate PF calculation via SW5#4; "
                    "active setting was not read. Neither true nor displacement PF is asserted.")
                reading["definition_source"] = "TD17271A pp.15,37"
            elif label == "Q_var":
                reading["method"] = "meter_reported"
                reading["sign_convention"] = (
                    "TD17271A Fig3.2: +P/-Q inductive lagging; +P/+Q capacitive leading; "
                    "-P/+Q lagging; -P/-Q leading")
                reading["definition_source"] = "TD17271A p.15 Fig3.2"
            result["readings"][label] = reading
            if not reading["valid"]:
                result["errors"].append(f"{label}: scale-byte valid bit is clear")
        if canonical == "currents":
            result["reserved"].append({
                **result["words"][3],
                "reason": "Chapter108 marks current response word4 reserved; not IX or a measurement",
            })
    result["valid"] = not result["errors"]
    result["all_readings_valid"] = all(reading["valid"] for reading in result["readings"].values())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=sorted(ALIASES))
    parser.add_argument("words", nargs="*", help="DATA-only 25-bit words, e.g. 0xB01482")
    args = parser.parse_args()
    result = decode_request(args.kind, args.words)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
