"""Delay collector.

Runs every ~10 minutes (GitHub Actions). For every station used by a commuter
in config.json it fetches the departure and arrival boards from the
Trafiklab realtime API and stores the latest known delay for each train.
Because each board only shows the next 60 minutes, the last observation we
store for a train is (very close to) its actual delay.

Data: data/observations/YYYY-MM.json   (data from Trafiklab.se, CC-BY 4.0)
Needs env var TRAFIKLAB_KEY.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
OBS_DIR = DATA / "observations"
STATIONS_CACHE = DATA / "stations.json"
API = "https://realtime-api.trafiklab.se/v1"
TZ = ZoneInfo("Europe/Stockholm")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def http_get_json(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "delay-report/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # network hiccups: retry
            last = e
            time.sleep(2 * (attempt + 1))
    detail = ""
    if isinstance(last, urllib.error.HTTPError):
        try:
            detail = " " + last.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
    raise RuntimeError(f"GET failed: {redact(url)}: {last}{detail}")


def redact(url):
    return url.split("key=")[0] + "key=***" if "key=" in url else url


def resolve_station(name, key, config, cache, fetch=http_get_json):
    """Return the Trafiklab area id for a station name (cached)."""
    if name in config.get("station_ids", {}):
        return str(config["station_ids"][name])
    if name in cache:
        return cache[name]["id"]
    q = urllib.parse.quote(name)
    res = fetch(f"{API}/stops/name/{q}?key={key}")
    groups = res.get("stop_groups", [])
    norm = lambda s: s.lower().replace("ö", "o").replace("ø", "o").replace("å", "a").replace("ä", "a")
    exact = [g for g in groups if norm(g["name"]) == norm(name) and "TRAIN" in g.get("transport_modes", [])]
    train = [g for g in groups if "TRAIN" in g.get("transport_modes", [])]
    pick = (exact or train or groups or [None])[0]
    if pick is None:
        raise RuntimeError(f"No stop found for '{name}'. Set it manually in config.json station_ids.")
    cache[name] = {"id": pick["id"], "matched_name": pick["name"], "area_type": pick.get("area_type")}
    print(f"Resolved '{name}' -> {pick['name']} ({pick['id']})")
    return pick["id"]


def is_wanted(call, flt):
    route = call.get("route") or {}
    if flt.get("transport_mode") and route.get("transport_mode") != flt["transport_mode"]:
        return False
    agency = ((call.get("agency") or {}).get("name") or "") + " " + (route.get("name") or "")
    words = flt.get("agency_contains") or []
    return not words or any(w.lower() in agency.lower() for w in words)


def record(store, station_name, kind, call, now_iso):
    trip = call.get("trip") or {}
    route = call.get("route") or {}
    k = f"{trip.get('start_date')}|{trip.get('trip_id')}|{station_name}|{kind}"
    prev = store.get(k, {})
    rec = {
        "station": station_name,
        "kind": kind,  # dep or arr
        "start_date": trip.get("start_date"),
        "trip_id": trip.get("trip_id"),
        "train_no": trip.get("technical_number"),
        "scheduled": call.get("scheduled"),
        "realtime": call.get("realtime"),
        "delay_s": call.get("delay") or 0,
        "canceled": bool(call.get("canceled")),
        "is_realtime": bool(call.get("is_realtime")),
        "route": route.get("name") or route.get("designation"),
        "direction": route.get("direction"),
        "agency": (call.get("agency") or {}).get("name"),
        "first_seen": prev.get("first_seen", now_iso),
        "last_seen": now_iso,
        "max_delay_s": max(prev.get("max_delay_s", 0), call.get("delay") or 0),
        "alerts": [a.get("title") for a in (call.get("alerts") or []) if a.get("title")][:3],
    }
    # Keep a realtime observation over a later non-realtime one
    if prev.get("is_realtime") and not rec["is_realtime"]:
        for f in ("realtime", "delay_s"):
            rec[f] = prev[f]
        rec["is_realtime"] = True
    store[k] = rec


def run(fetch=http_get_json, now=None):
    key = (os.environ.get("TRAFIKLAB_KEY") or "").strip()
    if not key:
        print("::error::The TRAFIKLAB_KEY secret is missing or empty. Add it under Settings > Secrets and variables > Actions.")
        sys.exit(1)
    config = load_json(ROOT / "config.json", {})
    cache = load_json(STATIONS_CACHE, {})
    now = now or datetime.now(TZ)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S")

    stations = list(dict.fromkeys(config.get("stations", []) + [u[s] for u in config.get("users", []) for s in ("home_station", "work_station")]))
    obs_path = OBS_DIR / f"{now:%Y-%m}.json"
    store = load_json(obs_path, {})
    flt = config.get("train_filter", {})
    n_new = 0

    errors = []
    for name in stations:
        try:
            area = resolve_station(name, key, config, cache, fetch)
            for kind, endpoint, field in (("dep", "departures", "departures"), ("arr", "arrivals", "arrivals")):
                res = fetch(f"{API}/{endpoint}/{area}?key={key}")
                for call in res.get(field, []):
                    if is_wanted(call, flt):
                        record(store, name, kind, call, now_iso)
                        n_new += 1
        except Exception as e:
            msg = f"{name}: {e}"
            errors.append(msg)
            print(f"::error::{msg}")

    save_json(obs_path, store)
    save_json(STATIONS_CACHE, cache)
    realtime = sum(1 for r in store.values() if r["is_realtime"])
    summary = f"{now_iso}: stored {n_new} observations; month file has {len(store)} rows ({realtime} with realtime data)"
    print(summary)
    print(f"::notice::{summary}. Stations: " + "; ".join(f"{k} -> {v['matched_name']} ({v['id']})" for k, v in cache.items()))
    if errors and len(errors) == len(stations):
        sys.exit(1)


if __name__ == "__main__":
    run()
