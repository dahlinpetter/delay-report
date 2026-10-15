"""Occupancy from Trafiklab KoDa VehiclePositions (Skånetrafiken).

    KODA_KEY=... python tools/koda_occupancy.py --probe --start 2026-10-13 --hours 6
    KODA_KEY=... python tools/koda_occupancy.py --start 2026-09-15 --end 2026-10-14 --hours 4-8,13-17

Writes data/occupancy.json:
  {"stops": [...], "levels": [...], "trains": [{"d": date, "n": train, "s": [[stop_idx, sched_dep_min, level, pct], ...]}]}
level = GTFS OccupancyStatus (0 empty .. 5 full), pct = occupancy_percentage or -1.
"""
import argparse, csv, json, os, shutil, sys, tempfile, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from koda_backfill import get, extract, BASE, OPERATOR, STATIONS, ORDER, local_min, parse_hours, note

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "occupancy.json"
LEVELS = ["Empty", "Many seats", "Few seats", "Standing room", "Crowded", "Full", "Not accepting", "No data", "Not boardable"]


def load_static(key, day, work):
    arch = work / f"static_{day}.bin"
    if not get(f"{BASE}/gtfs-static/{OPERATOR}?date={day}", key, dest=arch, tries=20, wait=30):
        raise RuntimeError(f"No static GTFS for {day}")
    d = work / f"static_{day}"; d.mkdir(exist_ok=True)
    extract(arch, d)
    stop_map = {}
    with open(next(d.rglob("stops.txt")), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            n = r["stop_name"].lower().strip()
            for label, pats in STATIONS.items():
                if any(n == p or n.startswith(p + " ") or n.startswith(p + ",") for p in pats):
                    stop_map[r["stop_id"]] = label
    trains = set()
    with open(next(d.rglob("routes.txt")), encoding="utf-8-sig") as f:
        rt = {r["route_id"] for r in csv.DictReader(f) if (int(r.get("route_type") or 0) == 2 or 100 <= int(r.get("route_type") or 0) <= 117)}
    trip_no = {}
    with open(next(d.rglob("trips.txt")), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["route_id"] in rt:
                trip_no[r["trip_id"]] = r.get("trip_short_name") or ""
    sched = defaultdict(dict)  # trip_id -> label -> dep minutes
    with open(next(d.rglob("stop_times.txt")), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            tid = r["trip_id"]
            if tid in trip_no and r["stop_id"] in stop_map:
                t = r.get("departure_time") or r.get("arrival_time")
                if t:
                    h, m, _ = t.split(":")
                    sched[tid][stop_map[r["stop_id"]]] = int(h) * 60 + int(m)
    shutil.rmtree(d, ignore_errors=True); arch.unlink(missing_ok=True)
    return stop_map, trip_no, sched


def parse_files(files, stop_map, trip_no, step, stats):
    from google.transit import gtfs_realtime_pb2
    obs = defaultdict(dict)  # (start_date, trip_id) -> label -> (level, pct)
    files = sorted(files)
    for path in files[::step]:
        msg = gtfs_realtime_pb2.FeedMessage()
        try:
            msg.ParseFromString(Path(path).read_bytes())
        except Exception:
            continue
        for ent in msg.entity:
            if not ent.HasField("vehicle"):
                continue
            v = ent.vehicle
            stats["vehicles"] += 1
            tid = v.trip.trip_id
            if tid not in trip_no:
                continue
            stats["train_vehicles"] += 1
            has_status = v.HasField("occupancy_status")
            has_pct = v.HasField("occupancy_percentage")
            if has_status: stats["with_status"] += 1; stats["status_" + LEVELS[v.occupancy_status]] += 1
            if has_pct: stats["with_pct"] += 1
            if len(v.multi_carriage_details): stats["with_carriages"] += 1
            label = stop_map.get(v.stop_id)
            if not label or not (has_status or has_pct):
                continue
            lvl = v.occupancy_status if has_status else -1
            pct = v.occupancy_percentage if has_pct else -1
            k = (v.trip.start_date, tid)
            prev = obs[k].get(label)
            if prev is None or (lvl, pct) > prev:
                obs[k][label] = (lvl, pct)
    return obs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True); ap.add_argument("--end")
    ap.add_argument("--hours", default="4-8,13-17"); ap.add_argument("--step", type=int, default=6)
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    key = os.environ["KODA_KEY"].strip()
    work = Path(tempfile.mkdtemp())
    start = date.fromisoformat(a.start); end = date.fromisoformat(a.end) if a.end else start
    hours = parse_hours(a.hours)
    out = json.loads(OUT.read_text()) if OUT.exists() and not a.probe else {"stops": ORDER, "levels": LEVELS, "trains": []}
    have = {(t["d"]) for t in out["trains"]}
    static = None; static_day = None
    stats = Counter()
    day = start
    while day <= end:
        if day.isoweekday() <= 5 and day.isoformat() not in have:
            if static is None or (day - static_day).days >= 7:
                try:
                    static = load_static(key, day.isoformat(), work); static_day = day
                except Exception as e:
                    if static is None:
                        print(f"::warning::static {day}: {e}"); day += timedelta(days=1); continue
            stop_map, trip_no, sched = static

            def one(h, day=day):
                arch = work / f"vp_{day}_{h}.bin"; d = work / f"vp_{day}_{h}"
                st = Counter()
                try:
                    if not get(f"{BASE}/gtfs-rt/{OPERATOR}/VehiclePositions?date={day}&hour={h:02d}", key, dest=arch, tries=24, wait=30):
                        return {}, st, f"{day} {h:02d}h: no archive"
                    st["archive_mb"] += arch.stat().st_size / 1e6
                    d.mkdir(exist_ok=True); extract(arch, d)
                    files = [p for p in d.rglob("*") if p.is_file()]
                    st["files"] += len(files)
                    return parse_files(files, stop_map, trip_no, a.step, st), st, None
                except Exception as e:
                    return {}, st, f"{day} {h:02d}h: {str(e)[:160]}"
                finally:
                    shutil.rmtree(d, ignore_errors=True); arch.unlink(missing_ok=True)

            day_obs = defaultdict(dict)
            with ThreadPoolExecutor(max_workers=len(hours)) as ex:
                for obs, st, err in ex.map(one, hours):
                    stats.update(st)
                    if err: print(f"::warning::{err}", flush=True)
                    for k, v in obs.items():
                        for lab, val in v.items():
                            if lab not in day_obs[k] or val > day_obs[k][lab]:
                                day_obs[k][lab] = val
            for (sd, tid), v in day_obs.items():
                s = [[ORDER.index(lab), sched.get(tid, {}).get(lab, -1), lvl, pct] for lab, (lvl, pct) in v.items()]
                s = [x for x in s if x[1] >= 0]
                if s:
                    s.sort(key=lambda x: x[1])
                    out["trains"].append({"d": f"{sd[:4]}-{sd[4:6]}-{sd[6:]}", "n": trip_no.get(tid, ""), "s": s})
            print(f"{day}: {len(day_obs)} trains with occupancy", flush=True)
        day += timedelta(days=1)
    if a.probe:
        note("Probe stats: " + ", ".join(f"{k}={round(v,1) if isinstance(v,float) else v}" for k, v in sorted(stats.items())))
        sample = out["trains"][:4]
        note(f"Probe: {len(out['trains'])} trains with occupancy at our stations. Sample: {json.dumps(sample, ensure_ascii=False)[:700]}")
        # variation check: distinct levels per station
        lv = Counter((ORDER[x[0]], x[2]) for t in out["trains"] for x in t["s"])
        note("Levels by station: " + "; ".join(f"{k[0]}:{LEVELS[k[1]] if k[1]>=0 else 'pct-only'}={c}" for k, c in sorted(lv.items())))
        pcts = [x[3] for t in out["trains"] for x in t["s"] if x[3] >= 0]
        if pcts: note(f"Percentages: n={len(pcts)} min={min(pcts)} max={max(pcts)} distinct={len(set(pcts))}")
    else:
        OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        note(f"occupancy.json: {len(out['trains'])} trains; stats " + ", ".join(f"{k}={round(v,1) if isinstance(v,float) else v}" for k, v in sorted(stats.items()) if not k.startswith('status_')))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
