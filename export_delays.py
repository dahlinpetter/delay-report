"""Turn collected observations into delays.json, the file the
Öresund Delay Refunds page reads. Keeps the last 45 days.

Format: {"example": false, "updated": "YYYY-MM-DDTHH:MM", "stops": [...],
         "trains": [{"d": date, "n": train_no, "c": 0|1,
                     "s": [[stop_index, departure_min, arrival_delay_min, arrival_min], ...]}]}
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
TZ = ZoneInfo("Europe/Stockholm")


def mins(ts):
    return int(ts[11:13]) * 60 + int(ts[14:16]) if ts else None


def main():
    now = datetime.now(TZ)
    cutoff = (now - timedelta(days=45)).date().isoformat()
    rows = {}
    for f in sorted((DATA / "observations").glob("*.json")):
        rows.update(json.loads(f.read_text(encoding="utf-8")))
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    order = config.get("stations", [])
    stops = order + sorted({r["station"] for r in rows.values()} - set(order))
    trains = {}
    for r in rows.values():
        if not r.get("scheduled") or r["scheduled"][:10] < cutoff:
            continue
        key = (r["start_date"], r["trip_id"])
        t = trains.setdefault(key, {"d": r["start_date"], "n": r.get("train_no"), "c": 0, "_s": {}})
        t["c"] |= int(bool(r["canceled"]))
        s = t["_s"].setdefault(r["station"], {})
        s[r["kind"]] = r
    out = []
    for t in trains.values():
        s_list = []
        for name, kinds in t.pop("_s").items():
            dep, arr = kinds.get("dep"), kinds.get("arr")
            first = dep or arr
            dep_min = mins(first["scheduled"])
            arr_min = mins(arr["scheduled"]) if arr else dep_min
            delay = round(max((arr or dep)["delay_s"], 0) / 60)
            s_list.append([stops.index(name), dep_min, delay, arr_min])
        s_list.sort(key=lambda x: x[1])  # stops in travel order
        t["s"] = s_list
        out.append(t)
    out.sort(key=lambda t: (t["d"], t["s"][0][1] if t["s"] else 0))
    doc = {"example": False, "updated": now.strftime("%Y-%m-%dT%H:%M"), "stops": stops, "trains": out}
    (DATA / "delays.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"delays.json: {len(out)} trains, {len(stops)} stations")


if __name__ == "__main__":
    main()
