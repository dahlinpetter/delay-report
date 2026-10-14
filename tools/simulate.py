"""Offline test: fakes the Trafiklab API for a week of Öresundståg traffic,
runs the collector at every 10-minute poll, then you can run report.py.

    TRAFIKLAB_KEY=x python tools/simulate.py --data /tmp/sim
"""
import argparse
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect  # noqa: E402

STOPS = {"Malmö C": "740000003", "København H": "740000999"}


def make_trains(day, rng):
    trains = []
    n = 0
    for direction, start_h, end_h, frm, to in (("DK", 6, 10, "Malmö C", "København H"),
                                               ("SE", 15, 19, "København H", "Malmö C")):
        t = day.replace(hour=start_h, minute=2)
        while t.hour < end_h:
            n += 1
            r = rng.random()
            delay = 0 if r < .75 else rng.choice([4, 9, 23, 31, 47, 66]) * 60
            cancelled = r > .985
            trains.append({"id": f"{day:%Y%m%d}{n:03d}", "no": 1000 + n, "from": frm, "to": to,
                           "dep": t, "arr": t + timedelta(minutes=35), "delay": delay, "canceled": cancelled})
            t += timedelta(minutes=20)
    return trains


def board(trains, area, kind, now):
    name = [k for k, v in STOPS.items() if v == area][0]
    out = []
    for tr in trains:
        station = tr["from"] if kind == "departures" else tr["to"]
        sched = tr["dep"] if kind == "departures" else tr["arr"]
        other = tr["to"] if kind == "departures" else tr["from"]
        if station != name:
            continue
        # a train shows up from 60 minutes ahead until it has actually left/arrived
        actual = sched + timedelta(seconds=tr["delay"])
        if not (now <= actual and sched <= now + timedelta(minutes=60)):
            continue
        # delay becomes known gradually
        known = tr["delay"] if (actual - now) < timedelta(minutes=40) else tr["delay"] // 2
        out.append({
            "scheduled": sched.strftime(collect.FMT if hasattr(collect, "FMT") else "%Y-%m-%dT%H:%M:%S"),
            "realtime": (sched + timedelta(seconds=known)).strftime("%Y-%m-%dT%H:%M:%S"),
            "delay": known, "canceled": tr["canceled"], "is_realtime": True,
            "route": {"name": "Öresundståg", "transport_mode": "TRAIN", "direction": other},
            "trip": {"trip_id": tr["id"], "start_date": tr["dep"].strftime("%Y-%m-%d"), "technical_number": tr["no"]},
            "agency": {"name": "Öresundståg"},
            "alerts": [],
        })
    return {kind: out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--end", default="2026-10-16")
    a = ap.parse_args()
    collect.DATA = Path(a.data)
    collect.OBS_DIR = collect.DATA / "observations"
    collect.STATIONS_CACHE = collect.DATA / "stations.json"
    rng = random.Random(7)
    end = datetime.fromisoformat(a.end)
    days = [end - timedelta(days=i) for i in range(6, -1, -1)]
    all_trains = [t for d in days if d.isoweekday() <= 5 for t in make_trains(d, rng)]

    def fake_fetch(url, now_holder={}):
        now = now_holder["now"]
        if "/stops/name/" in url:
            name = "København H" if "K%C3%B8benhavn" in url else "Malmö C"
            return {"stop_groups": [{"id": STOPS[name], "name": name, "transport_modes": ["TRAIN"]}]}
        area = url.split("?")[0].rstrip("/").split("/")[-1]
        kind = "departures" if "/departures/" in url else "arrivals"
        return board(all_trains, area, kind, now)

    holder = fake_fetch.__defaults__[0]
    for d in days:
        if d.isoweekday() > 5:
            continue
        t = d.replace(hour=5, minute=0)
        while t.hour < 21:
            holder["now"] = t
            collect.run(fetch=fake_fetch, now=t.replace(tzinfo=collect.TZ))
            t += timedelta(minutes=10)
    # ground truth for checking
    truth = [(tr["dep"].strftime("%a %H:%M"), tr["from"], tr["delay"] // 60, tr["canceled"])
             for tr in all_trains if tr["delay"] >= 1200 or tr["canceled"]]
    print("\nGround truth (20+ min or cancelled):")
    for x in truth:
        print("  ", x)


if __name__ == "__main__":
    main()
