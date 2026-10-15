"""Rebuild past delays from Trafiklab's KoDa history archive.

Downloads Skånetrafiken GTFS-Realtime TripUpdates for the given dates/hours,
keeps the last known delay per train at the commuter stations, and writes
data/history.json in the same format as data/delays.json.

    KODA_KEY=... python tools/koda_backfill.py --start 2026-04-15 --end 2026-10-14 --hours 6-9,15-19
    KODA_KEY=... python tools/koda_backfill.py --probe --start 2026-10-13
"""
from concurrent.futures import ThreadPoolExecutor
import argparse, csv, io, json, os, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.request, zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "history.json"
STATE = ROOT / "data" / "history_progress.json"
BASE = "https://api.koda.trafiklab.se/KoDa/api/v2"
TZ = ZoneInfo("Europe/Stockholm")
OPERATOR = "skane"
# station label -> name patterns found in GTFS stops.txt
STATIONS = {
    "Lund C": ["lund central", "lund c"],
    "Malmö C": ["malmö central", "malmö c"],
    "Triangeln": ["malmö triangeln", "triangeln"],
    "Hyllie": ["malmö hyllie", "hyllie"],
    "Kastrup": ["kastrup", "københavns lufthavn", "köpenhamns lufthavn", "cph airport"],
    "Ørestad": ["örestad", "ørestad"],
    "København H": ["köpenhamn h", "københavn h", "kobenhavn h", "köpenhamn central", "københavn central"],
}
ORDER = list(STATIONS)


def note(msg):
    print(f"::notice::{msg}", flush=True)


def get(url, key, dest=None, tries=40, wait=60):
    """GET with polling while KoDa prepares an archive (it answers 202 until ready)."""
    full = f"{url}{'&' if '?' in url else '?'}key={key}"
    for i in range(tries):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "delay-report/1.0"})
            with urllib.request.urlopen(req, timeout=300) as r:
                if r.status == 202:
                    time.sleep(wait); continue
                if dest:
                    with open(dest, "wb") as f:
                        shutil.copyfileobj(r, f)
                    return dest
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf-8", "replace")
            if e.code in (202, 429, 500, 502, 503, 504):
                time.sleep(wait); continue
            if e.code == 404:
                return None
            raise RuntimeError(f"HTTP {e.code} for {url}: {body}")
        except (urllib.error.URLError, TimeoutError):
            time.sleep(wait)
    raise RuntimeError(f"Gave up waiting for {url}")


def extract(archive, into):
    with open(archive, "rb") as f:
        magic = f.read(6)
    if magic.startswith(b"PK"):
        zipfile.ZipFile(archive).extractall(into)
    else:  # 7z
        subprocess.run(["7z", "x", "-y", f"-o{into}", str(archive)], check=True, stdout=subprocess.DEVNULL)


def load_static(key, day, work, quick=False):
    arch = work / "static.bin"
    if not get(f"{BASE}/gtfs-static/{OPERATOR}?date={day}", key, dest=arch, tries=4 if quick else 20, wait=30):
        raise RuntimeError(f"No static GTFS for {day}")
    d = work / "static"; d.mkdir(exist_ok=True)
    extract(arch, d)
    stops_txt = next(d.rglob("stops.txt"))
    routes_txt = next(d.rglob("routes.txt"))
    trips_txt = next(d.rglob("trips.txt"))
    stop_map = {}
    with open(stops_txt, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            n = r["stop_name"].lower().strip()
            for label, pats in STATIONS.items():
                if any(n == p or n.startswith(p + " ") or n.startswith(p + ",") for p in pats):
                    stop_map[r["stop_id"]] = label
    train_routes = set()
    with open(routes_txt, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            t = int(r.get("route_type") or 0)
            if t == 2 or 100 <= t <= 117:
                train_routes.add(r["route_id"])
    train_trips = {}
    with open(trips_txt, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["route_id"] in train_routes:
                train_trips[r["trip_id"]] = r.get("trip_short_name") or r.get("trip_headsign") or ""
    shutil.rmtree(d, ignore_errors=True)
    return stop_map, train_trips


def parse_hour(files, stop_map, train_trips, trains, step):
    from google.transit import gtfs_realtime_pb2
    files = sorted(files)
    for path in files[::step] + files[-1:]:
        msg = gtfs_realtime_pb2.FeedMessage()
        try:
            msg.ParseFromString(Path(path).read_bytes())
        except Exception:
            continue
        for ent in msg.entity:
            if not ent.HasField("trip_update"):
                continue
            tu = ent.trip_update
            tid = tu.trip.trip_id
            if tid not in train_trips:
                continue
            sd = tu.trip.start_date
            k = (sd, tid)
            t = trains.setdefault(k, {"n": train_trips[tid], "c": 0, "stops": {}})
            if tu.trip.schedule_relationship == 3:  # CANCELED
                t["c"] = 1
            for su in tu.stop_time_update:
                label = stop_map.get(su.stop_id)
                if not label:
                    continue
                s = t["stops"].setdefault(label, {})
                if su.schedule_relationship == 1:  # SKIPPED
                    t["c"] = 1
                if su.HasField("arrival") and su.arrival.time:
                    s["arr_t"], s["arr_d"] = su.arrival.time, su.arrival.delay
                if su.HasField("departure") and su.departure.time:
                    s["dep_t"], s["dep_d"] = su.departure.time, su.departure.delay


def local_min(ts):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ)
    return dt.strftime("%Y-%m-%d"), dt.hour * 60 + dt.minute


def to_doc(trains):
    out = []
    for (sd, tid), t in trains.items():
        s_list, day = [], None
        for label, s in t["stops"].items():
            dep_t = s.get("dep_t") or s.get("arr_t")
            arr_t = s.get("arr_t") or s.get("dep_t")
            if not dep_t:
                continue
            dep_sched = dep_t - (s.get("dep_d") if s.get("dep_t") else s.get("arr_d", 0))
            arr_sched = arr_t - (s.get("arr_d") if s.get("arr_t") else s.get("dep_d", 0))
            day, dmin = local_min(dep_sched)
            _, amin = local_min(arr_sched)
            delay = round(max(s.get("arr_d", s.get("dep_d", 0)), 0) / 60)
            s_list.append([ORDER.index(label), dmin, delay, amin])
        if len(s_list) >= 2 and day:
            s_list.sort(key=lambda x: x[1])
            out.append({"d": day, "n": t["n"], "c": t["c"], "s": s_list})
    out.sort(key=lambda t: (t["d"], t["s"][0][1]))
    return out


def parse_hours(spec):
    hrs = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        hrs += list(range(int(a), int(b or a) + 1))
    return hrs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end")
    ap.add_argument("--hours", default="6-9,15-19", help="UTC hours to download")
    ap.add_argument("--step", type=int, default=4, help="use every Nth snapshot")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--extra", action="store_true", help="add more hours to days already done")
    ap.add_argument("--max-minutes", type=int, default=300)
    a = ap.parse_args()
    key = os.environ["KODA_KEY"].strip()
    t_start = time.time()
    work = Path(tempfile.mkdtemp())
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else start
    hours = parse_hours(a.hours)

    state = json.loads(STATE.read_text()) if STATE.exists() and not a.probe else {"done": []}
    done_key = f"done_extra_{a.hours}" if a.extra else "done"
    state.setdefault(done_key, [])
    trains = {}
    if OUT.exists() and not a.probe:
        prev = json.loads(OUT.read_text())
        for t in prev.get("trains", []):
            trains[(t["d"].replace("-", ""), f"old-{t['d']}-{t['n']}-{t['s'][0][1]}")] = {"_doc": t}

    stop_map, train_trips, static_day = None, None, None
    errors = []
    day = start
    while day <= end:
        if day.isoweekday() <= 5 and day.isoformat() not in state[done_key]:
            if time.time() - t_start > a.max_minutes * 60:
                note(f"Time budget used; stopping before {day}. Run again to continue.")
                break
            # refresh static data weekly (trip ids change with timetables)
            if static_day is None or (day - static_day).days >= 7:
                try:
                    stop_map, tt = load_static(key, day.isoformat(), work, quick=static_day is not None)
                    train_trips = tt
                    static_day = day
                except Exception as e:
                    if stop_map is None:
                        for back in range(1, 15):
                            try:
                                stop_map, train_trips = load_static(key, (day - timedelta(days=back)).isoformat(), work, quick=True)
                                print(f"::warning::using timetable from {day - timedelta(days=back)} for {day}", flush=True)
                                break
                            except Exception:
                                continue
                    if stop_map is None:
                        print(f"::warning::static {day}: {str(e)[:160]}; skipping day", flush=True)
                        day += timedelta(days=1); continue
                    print(f"::warning::static {day} not ready; reusing previous timetable", flush=True)
                    static_day = day
            def one_hour(h, day=day, stop_map=stop_map, train_trips=train_trips):
                arch = work / f"rt_{day}_{h}.bin"
                d = work / f"rt_{day}_{h}"
                part = {}
                try:
                    if not get(f"{BASE}/gtfs-rt/{OPERATOR}/TripUpdates?date={day}&hour={h:02d}", key, dest=arch, tries=24, wait=30):
                        return part, f"{day} {h:02d}h: no archive"
                    d.mkdir(exist_ok=True)
                    extract(arch, d)
                    files = [p for p in d.rglob("*") if p.is_file()]
                    parse_hour(files, stop_map, train_trips, part, a.step)
                    return part, None
                except Exception as e:
                    return part, f"{day} {h:02d}h: {str(e)[:160]}"
                finally:
                    shutil.rmtree(d, ignore_errors=True); arch.unlink(missing_ok=True)
            day_trains = {}
            with ThreadPoolExecutor(max_workers=len(hours)) as ex:
                for part, err in ex.map(one_hour, hours):
                    if err:
                        errors.append(err); print(f"::warning::{err}", flush=True)
                    for k, t in part.items():
                        cur = day_trains.setdefault(k, {"n": t["n"], "c": 0, "stops": {}})
                        cur["c"] |= t["c"]
                        for lab, st in t["stops"].items():
                            cur["stops"].setdefault(lab, {}).update(st)
            trains.update(day_trains)
            state[done_key].append(day.isoformat())
            if not a.probe:
                save(trains, state)
                if len(state[done_key]) % 3 == 0 and os.environ.get("GITHUB_ACTIONS"):
                    subprocess.run("git config user.name delay-bot && git config user.email delay-bot@users.noreply.github.com && "
                                   "git add data/history.json data/history_progress.json && git commit -qm 'history: progress' && "
                                   "git pull -q --rebase && git push -q", shell=True)
            print(f"{day}: {len(day_trains)} trains ({int((time.time()-t_start)/60)} min elapsed)", flush=True)
        day += timedelta(days=1)
    if a.probe:
        doc = to_doc({k: v for k, v in trains.items() if "_doc" not in v})
        sample = [t for t in doc if any(s[2] >= 10 for s in t["s"])][:3] or doc[:3]
        note(f"Probe parsed {len(doc)} trains. Sample: {json.dumps(sample, ensure_ascii=False)[:800]}")
    else:
        save(trains, state)
        note(f"Done {len(state['done'])} days, {sum(1 for v in trains.values())} trains, {len(errors)} skipped hours, {int((time.time()-t_start)/60)} min")
    shutil.rmtree(work, ignore_errors=True)


def save(trains, state):
    old = [v["_doc"] for v in trains.values() if "_doc" in v]
    new = to_doc({k: v for k, v in trains.items() if "_doc" not in v})
    merged = {(t["d"], t["n"], t["s"][0][1]): t for t in old + new}
    doc = {"example": False, "history": True,
           "updated": datetime.now(TZ).strftime("%Y-%m-%dT%H:%M"),
           "stops": ORDER, "trains": sorted(merged.values(), key=lambda t: (t["d"], t["s"][0][1]))}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    STATE.write_text(json.dumps(state))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print("::error::" + (traceback.format_exc().strip().splitlines()[-1])[:400], flush=True)
        print(traceback.format_exc(), flush=True)
        sys.exit(1)
