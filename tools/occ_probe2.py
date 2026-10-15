"""Where does Skånetrafiken occupancy live? Counts occupancy fields across all vehicles and in TripUpdates."""
import os, sys, json, tempfile, shutil
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from koda_backfill import get, extract, BASE, OPERATOR, note
from koda_occupancy import load_static
from google.transit import gtfs_realtime_pb2
key = os.environ["KODA_KEY"].strip(); work = Path(tempfile.mkdtemp()); day = "2026-10-13"
stop_map, trip_no, sched = load_static(key, day, work)
for feed in ("TripUpdates",):
    arch = work / f"{feed}.bin"; d = work / feed; d.mkdir()
    get(f"{BASE}/gtfs-rt/{OPERATOR}/{feed}?date={day}&hour=06", key, dest=arch, tries=24, wait=30)
    try:
        extract(arch, d)
    except Exception as er:
        note(f"{feed}: extract failed {er}"); continue
    files = sorted(p for p in d.rglob("*") if p.is_file())[::15]
    c = Counter(); ex = []
    for p in files:
        m = gtfs_realtime_pb2.FeedMessage(); m.ParseFromString(p.read_bytes())
        for e in m.entity:
            if e.HasField("vehicle"):
                v = e.vehicle; train = v.trip.trip_id in trip_no
                c["veh_train" if train else "veh_other"] += 1
                if v.HasField("occupancy_status"):
                    c["occ_status_" + ("train" if train else "other")] += 1
                    if len(ex) < 3: ex.append(("veh", train, v.occupancy_status, v.occupancy_percentage if v.HasField("occupancy_percentage") else None, v.trip.route_id))
                if v.HasField("occupancy_percentage"): c["occ_pct_" + ("train" if train else "other")] += 1
                if len(v.multi_carriage_details): c["carriages_" + ("train" if train else "other")] += 1
            if e.HasField("trip_update"):
                tu = e.trip_update; train = tu.trip.trip_id in trip_no
                for su in tu.stop_time_update:
                    has = False
                    try:
                        from google.protobuf.unknown_fields import UnknownFieldSet
                        for uf in UnknownFieldSet(su):
                            c[f"unknown_field_{uf.field_number}_" + ("train" if train else "other")] += 1
                            if uf.field_number == 7 and len(ex) < 8: ex.append(("stu7", train, uf.data))
                    except Exception as er:
                        c["unknown_err"] += 1
                    if has:
                        c["stu_dep_occ_" + ("train" if train else "other")] += 1
                        if len(ex) < 6: ex.append(("tu", train, su.departure_occupancy_status))
                if tu.HasField("vehicle"):
                    c["tu_vehicle"] += 1
    note(f"{feed}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())) + f" | examples {ex}")
