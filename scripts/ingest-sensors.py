#!/usr/bin/env python3
"""
Ingest sensor CSVs into source-adjacent .nt files.

For each source CSV, writes a companion <stem>.nt file (N-Triples, no graph
name) in the same directory. The graph name is synthesized at index-build time
by the qlever-life service (qlever-dir), which derives it from the .nt
file's path relative to the data directory (e.g. `<file:observations/.../foo.nt>`).

Run from repo root:
    python3 scripts/ingest-sensors.py

Ontology used:
    SOSA  http://www.w3.org/ns/sosa/
    RDF   http://www.w3.org/1999/02/22-rdf-syntax-ns#
    XSD   http://www.w3.org/2001/XMLSchema#
"""
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(os.environ.get("CHAMBER_DIR", Path(__file__).resolve().parent.parent))

# --- Namespace shortcuts --------------------------------------------------

SOSA     = "http://www.w3.org/ns/sosa/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
XSD      = "http://www.w3.org/2001/XMLSchema#"

PROP_GLUCOSE    = "urn:health:property:blood-glucose"
PROP_KETONE     = "urn:health:property:blood-ketone-bhb"
PROP_RHR        = "urn:health:property:resting-heart-rate"
PROP_HRV        = "urn:health:property:heart-rate-variability"
PROP_STEPS      = "urn:health:property:step-count"
PROP_SLEEP_SC   = "urn:health:property:sleep-score"
PROP_DEEP_SLEEP = "urn:health:property:deep-sleep-duration"
PROP_REM_SLEEP  = "urn:health:property:rem-sleep-duration"
PROP_RECOVERY   = "urn:health:property:recovery-score"
PROP_MOVEMENT   = "urn:health:property:movement-score"
PROP_SLEEP_EFF  = "urn:health:property:sleep-efficiency"
PROP_SLEEP_DUR  = "urn:health:property:sleep-duration"
PROP_STRESS     = "urn:health:property:stress-level"
PROP_SPO2       = "urn:health:property:spo2"
PROP_BODY_BATT  = "urn:health:property:body-battery"
PROP_SKIN_TEMP  = "urn:health:property:skin-temperature"

# --- NT helpers -----------------------------------------------------------

def u(uri):
    return f"<{uri}>"

def decimal(v):
    return f'"{v}"^^<{XSD}decimal>'

def dt_lit(v):
    return f'"{v}"^^<{XSD}dateTime>'

def nt(s, p, o):
    return f"{u(s)} {u(p)} {o} .\n"

# --- Extraction -----------------------------------------------------------

def extract_ckm(csv_path: Path) -> list[str]:
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row_no = row["No."].strip()
            ts_raw = row["Time"].strip()
            val    = row["Sensor reading(mmol/L)"].strip()
            if not val:
                continue
            ts     = ts_raw.replace(" ", "T")
            obs_id = f"urn:obs:ckm:{csv_path.stem}:{row_no}"
            rows += [
                nt(obs_id, RDF_TYPE,                  u(f"{SOSA}Observation")),
                nt(obs_id, f"{SOSA}observedProperty", u(PROP_KETONE)),
                nt(obs_id, f"{SOSA}hasSimpleResult",  decimal(val)),
                nt(obs_id, f"{SOSA}resultTime",       dt_lit(ts)),
                nt(obs_id, f"{SOSA}madeBySensor",     u(f"urn:health:sensor:ckm:{csv_path.stem}")),
            ]
    return rows


def extract_cgm(csv_path: Path) -> list[str]:
    rows = []
    seen: set[tuple] = set()
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader)   # metadata row
        next(reader)   # headers
        for row in reader:
            if len(row) < 5:
                continue
            record_type = row[3].strip()
            if record_type not in ("0", "1"):
                continue
            val = row[4].strip() if record_type == "0" else (row[5].strip() if len(row) > 5 else "")
            if not val:
                continue
            ts_raw = row[2].strip()
            try:
                ts = datetime.strptime(ts_raw, "%d-%m-%Y %H:%M").strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                continue
            key = (ts, record_type)
            if key in seen:
                continue
            seen.add(key)
            serial = row[1].strip()
            obs_id = f"urn:obs:cgm:{serial}:{ts}:t{record_type}"
            rows += [
                nt(obs_id, RDF_TYPE,                  u(f"{SOSA}Observation")),
                nt(obs_id, f"{SOSA}observedProperty", u(PROP_GLUCOSE)),
                nt(obs_id, f"{SOSA}hasSimpleResult",  decimal(val)),
                nt(obs_id, f"{SOSA}resultTime",       dt_lit(ts)),
                nt(obs_id, f"{SOSA}madeBySensor",     u(f"urn:health:sensor:cgm:{serial}")),
            ]
    return rows


ULTRAHUMAN_COLUMNS = {
    "Average RHR":    PROP_RHR,
    "Average HRV":    PROP_HRV,
    "Total Steps":    PROP_STEPS,
    "Sleep Score":    PROP_SLEEP_SC,
    "Deep Sleep":     PROP_DEEP_SLEEP,
    "REM Sleep":      PROP_REM_SLEEP,
    "Recovery Score": PROP_RECOVERY,
    "Movement Score": PROP_MOVEMENT,
    "Sleep Efficiency": PROP_SLEEP_EFF,
    "Total Sleep":    PROP_SLEEP_DUR,
}

GARMIN_COLUMNS = {
    "Steps":         PROP_STEPS,
    "RestingHR":     PROP_RHR,
    "AvgHRV":        PROP_HRV,
    "TotalSleepMin": PROP_SLEEP_DUR,
    "DeepSleepMin":  PROP_DEEP_SLEEP,
    "REMSleepMin":   PROP_REM_SLEEP,
    "LightSleepMin": "urn:health:property:light-sleep-duration",
    "AvgStress":     PROP_STRESS,
    "SpO2":          PROP_SPO2,
    "BodyBattery":   PROP_BODY_BATT,
    "SkinTemp":      PROP_SKIN_TEMP,
}


def extract_ultrahuman(csv_path: Path) -> list[str]:
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            date = row.get("Date", "").strip()
            if not date:
                continue
            ts = f"{date}T00:00:00"
            for col, prop in ULTRAHUMAN_COLUMNS.items():
                val = row.get(col, "").strip()
                if not val:
                    continue
                metric = col.lower().replace(" ", "-")
                obs_id = f"urn:obs:ultrahuman:{date}:{metric}"
                rows += [
                    nt(obs_id, RDF_TYPE,                  u(f"{SOSA}Observation")),
                    nt(obs_id, f"{SOSA}observedProperty", u(prop)),
                    nt(obs_id, f"{SOSA}hasSimpleResult",  decimal(val)),
                    nt(obs_id, f"{SOSA}resultTime",       dt_lit(ts)),
                    nt(obs_id, f"{SOSA}madeBySensor",     u("urn:health:sensor:ultrahuman:ring")),
                ]
    return rows


def extract_garmin(csv_path: Path) -> list[str]:
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            date = row.get("Date", "").strip()
            if not date:
                continue
            ts = f"{date}T00:00:00"
            for col, prop in GARMIN_COLUMNS.items():
                val = row.get(col, "").strip()
                if not val:
                    continue
                metric = col.lower().replace(" ", "-")
                obs_id = f"urn:obs:garmin:{date}:{metric}"
                rows += [
                    nt(obs_id, RDF_TYPE,                  u(f"{SOSA}Observation")),
                    nt(obs_id, f"{SOSA}observedProperty", u(prop)),
                    nt(obs_id, f"{SOSA}hasSimpleResult",  decimal(val)),
                    nt(obs_id, f"{SOSA}resultTime",       dt_lit(ts)),
                    nt(obs_id, f"{SOSA}madeBySensor",     u("urn:health:sensor:garmin:watch")),
                ]
    return rows


def write_nt(csv_path: Path, triples: list[str]):
    """Write triples to a .nt file alongside the source CSV."""
    nt_path = csv_path.with_suffix(".nt")
    with open(nt_path, "w", encoding="utf-8") as f:
        f.writelines(triples)
    return nt_path


# --- Main -----------------------------------------------------------------

def main():
    total_obs = 0

    ckm_dir = REPO_ROOT / "observations/clinical/sensors/ckm"
    for p in sorted(ckm_dir.glob("*.csv")):
        print(f"  CKM  {p.name} ...", end=" ", flush=True)
        triples = extract_ckm(p)
        write_nt(p, triples)
        n = len(triples) // 5
        total_obs += n
        print(f"{n} observations")

    cgm_dir = REPO_ROOT / "observations/clinical/sensors/cgm"
    for p in sorted(cgm_dir.glob("glucose_*.csv")):
        print(f"  CGM  {p.name} ...", end=" ", flush=True)
        triples = extract_cgm(p)
        write_nt(p, triples)
        n = len(triples) // 5
        total_obs += n
        print(f"{n} observations")

    wearable_dir = REPO_ROOT / "observations/clinical/sensors/wearable"
    for p in sorted(wearable_dir.glob("ultrahuman*.csv")):
        print(f"  UH   {p.name} ...", end=" ", flush=True)
        triples = extract_ultrahuman(p)
        write_nt(p, triples)
        n = len(triples) // 10
        total_obs += n
        print(f"{n} observations")

    garmin_dir = REPO_ROOT / "observations/clinical/sensors/garmin"
    if garmin_dir.exists():
        for p in sorted(garmin_dir.glob("garmin-daily-*.csv")):
            print(f"  GAR  {p.name} ...", end=" ", flush=True)
            triples = extract_garmin(p)
            write_nt(p, triples)
            n = len(triples) // 5
            total_obs += n
            print(f"{n} observations")

    print(f"\n{total_obs} observations written to source-adjacent .nt files")


if __name__ == "__main__":
    print("Ingesting sensor data...")
    main()
