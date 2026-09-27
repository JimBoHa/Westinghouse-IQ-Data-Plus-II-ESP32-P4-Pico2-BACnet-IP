#!/usr/bin/env python3
"""Add collection metadata and a convenient wide view; never change captured data."""
import argparse
import json
import sqlite3
from pathlib import Path


METADATA = {
    "meter": {"model": "IQ Data Plus II", "communication_version": 9, "display_verified": False},
    "acquisition": {"firmware": "0.4.6", "board": "Pico 2 W",
                    "request": "all_standard_repeat", "address": 0, "interval_seconds": 1.05,
                    "interval_definition": "minimum request-start spacing; interleaved diagnostics make occasional measurement intervals about 2.1 seconds",
                    "configuration_note": "Package defaults; individual attempt timestamps and USB journals are authoritative for imported or reconfigured recordings",
                    "diagnostics_trended": False,
                    "transaction_limit_ms": 500, "clock_owner": "meter", "timezone": "UTC",
                    "timestamp_definition": "host receipt/completion time, not calibrated waveform UTC",
                    "simultaneous_snapshot_guaranteed": False},
    "power_conventions": {"negative_PF": "lagging", "positive_PF": "leading",
                          "positive_W_negative_var": "inductive, manufacturer quadrant 4",
                          "positive_W_positive_var": "capacitive, manufacturer quadrant 1",
                          "meter_PF_calculation_mode": "Read CONFIG_ALTERNATE_PF from this meter's diagnostic settings; do not assume another meter's configuration",
                          "reference": "https://pps2.com/communications/files/legacyPMP/products/iqdpii/docs/td17271a_pp11_20.pdf"},
    "derived_values": {"S_PQ_estimate_VA": "sqrt(P_W**2+Q_var**2), P/Q power-triangle estimate",
                       "PF_PQ_magnitude": "abs(P_W)/sqrt(P_W**2+Q_var**2)",
                       "limitation": "These estimates exclude a separate distortion-power component; not harmonic measurements"},
    "unavailable_quantities": {"voltage_THD_percent": None, "current_THD_percent": None,
                               "reason": "No documented THD response or harmonic spectrum; RMS/P/Q/PF scalars cannot determine THD"},
    "tables": {"attempts": "Every acquisition attempt and failure; IDs are ingestion order, not necessarily chronological",
               "raw_words": "Original meter DATA words in wire order; current word 4 is reserved",
               "readings": "Meter-reported scalars and validity flags",
               "derived_readings": "Estimates with method, inputs, and assumptions",
               "scalar_readings": "Combined long view with explicit source",
               "samples": "Wide chronological-analysis view; failed or unavailable values are NULL"},
    "collection": {"snapshot_interval_seconds": 3600,
                   "download_path": "/database", "telemetry_path": "/telemetry",
                   "diagnostic_trends": False},
}
FIELDS = {
    "IA": "ia_A", "IB": "ib_A", "IC": "ic_A",
    "VAB": "vab_V", "VBC": "vbc_V", "VCA": "vca_V",
    "VAN": "van_V", "VBN": "vbn_V", "VCN": "vcn_V",
    "P_W": "active_power_W", "Q_var": "reactive_power_var", "PF": "power_factor",
    "FREQUENCY_Hz": "frequency_Hz", "DEMAND_W": "meter_demand_W",
    "ENERGY_Wh": "scaled_energy_Wh", "ENERGY_kWh": "energy_counter_kWh",
    "S_PQ_estimate_VA": "apparent_power_PQ_estimate_VA", "PF_PQ_magnitude": "power_factor_PQ_magnitude",
}


def configure(path):
    path = Path(path).expanduser().resolve()
    metadata = {**METADATA, "collection": {**METADATA["collection"],
                "live_database": str(path),
                "consistent_snapshot": str(path.with_name("iqdata-latest.sqlite"))}}
    connection = sqlite3.connect(path, timeout=10)
    try:
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value_json TEXT NOT NULL)")
            connection.executemany("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                                   [(key, json.dumps(value, allow_nan=False)) for key, value in metadata.items()])
            fields = ",".join(f"MAX(CASE WHEN r.name='{name}' AND r.valid=1 THEN r.value END) AS {column}"
                              for name, column in FIELDS.items())
            connection.execute("CREATE VIEW IF NOT EXISTS samples AS SELECT a.attempt_id,a.observed_utc,a.kind,a.ok,"
                               "a.protocol_valid,a.decoded_valid,a.display_verified," + fields +
                               ",CAST(NULL AS REAL) AS voltage_THD_percent,CAST(NULL AS REAL) AS current_THD_percent "
                               "FROM attempts a LEFT JOIN scalar_readings r USING(attempt_id) GROUP BY a.attempt_id")
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    configure(parser.parse_args().database)
