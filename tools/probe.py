"""Diagnostics: prints stop lookups and what the boards contain (as GitHub notices)."""
import os, sys, json, urllib.parse
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collect import http_get_json, API
key = os.environ["TRAFIKLAB_KEY"].strip()
for q in ["Köpenhamn", "København", "Kobenhavn", "Kastrup"]:
    try:
        r = http_get_json(f"{API}/stops/name/{urllib.parse.quote(q)}?key={key}")
        g = [f"{x['name']} ({x['id']}, {'/'.join(x.get('transport_modes', []))})" for x in r.get("stop_groups", [])[:6]]
        print(f"::notice::lookup {q}: " + " | ".join(g))
    except Exception as e:
        print(f"::notice::lookup {q} failed: {e}")
for area, label in (("740000003", "Malmö C"),):
    for t in ("2026-10-15T08:00", "2026-10-14T08:00"):
        for ep in ("departures", "arrivals"):
            try:
                r = http_get_json(f"{API}/{ep}/{area}/{t}?key={key}")
                calls = r.get(ep, [])
                c = Counter(f"{(x.get('agency') or {}).get('name')}|{(x.get('route') or {}).get('name')}|{(x.get('route') or {}).get('transport_mode')}|rt={x.get('is_realtime')}" for x in calls)
                ex = next((x for x in calls if (x.get('route') or {}).get('transport_mode') == "TRAIN"), None)
                print(f"::notice::{label} {ep} @{t}: {len(calls)} calls; " + "; ".join(f"{k} x{v}" for k, v in c.most_common(8)))
                if ex:
                    print("::notice::example train: " + json.dumps({k: ex.get(k) for k in ("scheduled","realtime","delay","canceled","is_realtime","route","agency","trip")}, ensure_ascii=False)[:900])
            except Exception as e:
                print(f"::notice::{label} {ep} @{t} failed: {e}")
