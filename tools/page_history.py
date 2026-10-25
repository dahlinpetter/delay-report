"""Write data/page_history.json: the last ~62 days of history.json, for the Kvitt page."""
import json
from datetime import date, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
h = json.loads((ROOT / "data" / "history.json").read_text(encoding="utf-8"))
cut = (date.today() - timedelta(days=62)).isoformat()
h["trains"] = [t for t in h["trains"] if t["d"] >= cut]
(ROOT / "data" / "page_history.json").write_text(json.dumps(h, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(len(h["trains"]), "trains from", cut)
