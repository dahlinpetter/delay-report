"""Weekly delay report.

For every commuter in config.json: find the trains they would have taken in
the last N days, look up the delay at their destination, apply the
compensation tiers and email a summary.

Usage:  python report.py [--days 7] [--end YYYY-MM-DD] [--dry-run] [--user NAME]
SMTP env vars (for sending): SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM
Without them (or with --dry-run) the email is only written to reports/.
"""
import argparse
import html
import json
import os
import smtplib
import ssl
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OBS_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data")) / "observations"
REPORTS = Path(os.environ.get("REPORTS_DIR", ROOT / "reports"))
FMT = "%Y-%m-%dT%H:%M:%S"


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def parse(ts):
    return datetime.strptime(ts[:19], FMT) if ts else None


def hm(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def load_observations(start, end):
    rows = {}
    months = {(start + timedelta(days=i)).strftime("%Y-%m") for i in range((end - start).days + 1)}
    for m in sorted(months):
        rows.update(load_json(OBS_DIR / f"{m}.json", {}))
    return list(rows.values())


def trip_value(ticket):
    if ticket.get("trip_price_sek"):
        return float(ticket["trip_price_sek"])
    return float(ticket["price_sek"]) / float(ticket.get("trips_factor") or 40)


def percent_for(minutes, tiers):
    for t in sorted(tiers, key=lambda t: -t["min_minutes"]):
        if minutes >= t["min_minutes"]:
            return t["percent"]
    return 0


def build_trips(rows, day, origin, dest):
    """All trains departing `origin` on `day` that later arrive at `dest`."""
    deps = [r for r in rows if r["station"] == origin and r["kind"] == "dep"
            and r["scheduled"] and r["scheduled"].startswith(day.isoformat())]
    arr_idx = {(r["start_date"], r["trip_id"]): r for r in rows if r["station"] == dest and r["kind"] == "arr"}
    trips = []
    for d in deps:
        a = arr_idx.get((d["start_date"], d["trip_id"]))
        if not a or parse(a["scheduled"]) <= parse(d["scheduled"]):
            continue  # train does not continue to destination (or wrong direction)
        trips.append({"dep": d, "arr": a})
    trips.sort(key=lambda t: t["dep"]["scheduled"])
    return trips


def effective_delay_min(trip, all_trips):
    """Delay at destination in minutes. For a cancelled train: time until the
    next train actually arrives, compared with the cancelled train's schedule."""
    d, a = trip["dep"], trip["arr"]
    if not (d["canceled"] or a["canceled"]):
        return round(max(a["delay_s"], 0) / 60), False
    sched_arr = parse(a["scheduled"])
    later = [t for t in all_trips if t["dep"]["scheduled"] > d["scheduled"]
             and not (t["dep"]["canceled"] or t["arr"]["canceled"])]
    if not later:
        return None, True
    first = min(parse(t["arr"]["realtime"] or t["arr"]["scheduled"]) for t in later)
    return round((first - sched_arr).total_seconds() / 60), True


def analyse_user(user, rows, start, end, tiers):
    value = trip_value(user["ticket"])
    legs = [("To work", user["home_station"], user["work_station"], user["outbound"]),
            ("Home", user["work_station"], user["home_station"], user["return"])]
    claims, others, no_data_days, trips_counted = [], [], [], 0
    day = start
    while day <= end:
        if day.isoweekday() in user.get("workdays", [1, 2, 3, 4, 5]):
            day_has_data = False
            for label, origin, dest, leg in legs:
                all_trips = build_trips(rows, day, origin, dest)
                lo, hi = hm(leg["window"][0]), hm(leg["window"][1])
                in_win = [t for t in all_trips if lo <= hm(t["dep"]["scheduled"][11:16]) <= hi]
                if not in_win:
                    continue
                day_has_data = True
                usual = hm(leg.get("usual_departure") or leg["window"][0])
                mine = min(in_win, key=lambda t: abs(hm(t["dep"]["scheduled"][11:16]) - usual))
                trips_counted += 1
                for t in in_win:
                    mins, cancelled = effective_delay_min(t, all_trips)
                    pct = percent_for(mins or 0, tiers) if mins is not None else 0
                    if pct == 0 and not cancelled:
                        continue
                    item = {
                        "date": day, "leg": label, "origin": origin, "dest": dest,
                        "train": t["dep"].get("train_no"), "route": t["dep"].get("route"),
                        "dep": t["dep"]["scheduled"][11:16],
                        "arr_sched": t["arr"]["scheduled"][11:16],
                        "arr_real": (t["arr"]["realtime"] or "")[11:16],
                        "delay_min": mins, "cancelled": cancelled, "percent": pct,
                        "sek": round(value * pct / 100), "realtime": t["arr"]["is_realtime"],
                    }
                    (claims if t is mine else others).append(item)
            if not day_has_data:
                no_data_days.append(day)
        day += timedelta(days=1)
    return {"claims": claims, "others": others, "no_data_days": no_data_days,
            "trips_counted": trips_counted, "trip_value": value,
            "total": sum(c["sek"] for c in claims)}


# ---------- email ----------
def fmt_day(d):
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return f"{days[d.weekday()]} {d.day}/{d.month}"


def row_html(c):
    e = html.escape
    if c["cancelled"]:
        delay = "Cancelled" + (f" (+{c['delay_min']} min)" if c["delay_min"] is not None else "")
        badge = "#b42318"
    else:
        delay = f"+{c['delay_min']} min"
        badge = "#b54708" if c["delay_min"] < 40 else "#b42318"
    sek = f"{c['sek']} kr" if c["percent"] else "check"
    return f"""
    <tr>
      <td style="padding:10px 8px;border-bottom:1px solid #eaecf0;">
        <div style="font-weight:600;color:#101828;">{e(fmt_day(c['date']))} · {e(c['leg'])}</div>
        <div style="color:#667085;font-size:13px;">{e(c['origin'])} {e(c['dep'])} → {e(c['dest'])} {e(c['arr_sched'])}{' · train ' + e(str(c['train'])) if c['train'] else ''}</div>
      </td>
      <td style="padding:10px 8px;border-bottom:1px solid #eaecf0;text-align:right;white-space:nowrap;">
        <span style="color:{badge};font-weight:600;">{e(delay)}</span>
        <div style="color:#667085;font-size:13px;">{c['percent']}%</div>
      </td>
      <td style="padding:10px 8px;border-bottom:1px solid #eaecf0;text-align:right;font-weight:700;color:#101828;white-space:nowrap;">{e(sek)}</td>
    </tr>"""


def render_email(user, res, start, end):
    e = html.escape
    total = res["total"]
    n = len(res["claims"])
    period = f"{fmt_day(start)} – {fmt_day(end)}"
    headline = (f"You can claim about <b>{total} kr</b> for this week's delays"
                if total else "No claimable delays on your usual trains this week")
    rows = "".join(row_html(c) for c in res["claims"]) or (
        '<tr><td colspan="3" style="padding:16px 8px;color:#667085;">All your usual trains were less than 20 minutes late. 🎉</td></tr>')
    others = ""
    if res["others"]:
        others = f"""
      <h3 style="font-size:15px;margin:28px 0 6px;color:#101828;">Other delayed trains in your time windows</h3>
      <p style="margin:0 0 8px;color:#667085;font-size:13px;">Only claimable if you were actually on them.</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">{''.join(row_html(c) for c in res['others'])}</table>"""
    gaps = ""
    if res["no_data_days"]:
        gaps = (f'<p style="color:#667085;font-size:13px;">No train data for: '
                f'{e(", ".join(fmt_day(d) for d in res["no_data_days"]))}.</p>')
    return f"""<!doctype html><html><body style="margin:0;background:#f2f4f7;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
  <div style="max-width:600px;margin:0 auto;padding:24px 16px;">
    <div style="background:#ffffff;border-radius:14px;padding:24px;">
      <div style="color:#667085;font-size:13px;">Your weekly delay report · {e(period)}</div>
      <h1 style="font-size:22px;line-height:1.3;margin:8px 0 4px;color:#101828;">{headline}</h1>
      <p style="margin:0 0 18px;color:#475467;">{n} of your {res['trips_counted']} usual trips were 20+ minutes late. One trip is valued at {res['trip_value']:.0f} kr on your {e(user['ticket'].get('type', 'ticket'))}.</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">{rows}</table>
      {others}
      <div style="margin-top:24px;padding:16px;background:#f9fafb;border-radius:10px;">
        <div style="font-weight:600;color:#101828;margin-bottom:6px;">How to claim</div>
        <div style="color:#475467;font-size:14px;line-height:1.5;">Open Skånetrafiken's delay compensation form (skanetrafiken.se → Kundservice → Förseningsersättning), log in with BankID and add each trip above with date, departure time and station. Claim soon after the trip.</div>
      </div>
      {gaps}
      <p style="color:#98a2b3;font-size:12px;line-height:1.5;margin-top:20px;">Estimate only. Tiers: 50% at 20–39 min, 75% at 40–59 min, 100% at 60+ min delay at your destination. Skånetrafiken decides each claim. Data from Trafiklab.se (CC BY 4.0).</p>
    </div>
  </div></body></html>"""


def send(to, subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host:
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ.get("MAIL_FROM", os.environ["SMTP_USER"]), to
    msg.attach(MIMEText(body, "html", "utf-8"))
    port = int(os.environ.get("SMTP_PORT", "465"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as s:
            s.starttls(context=ctx)
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--end", help="last day to include, YYYY-MM-DD (default: today)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--user")
    a = ap.parse_args()
    end = date.fromisoformat(a.end) if a.end else date.today()
    start = end - timedelta(days=a.days - 1)
    config = load_json(ROOT / "config.json", {})
    rows = load_observations(start, end)
    tiers = config["compensation_rules"]["tiers"]
    REPORTS.mkdir(exist_ok=True)
    for user in config.get("users", []):
        if a.user and user["name"] != a.user:
            continue
        res = analyse_user(user, rows, start, end, tiers)
        body = render_email(user, res, start, end)
        out = REPORTS / f"{user['name'].lower()}-{end.isoformat()}.html"
        out.write_text(body, encoding="utf-8")
        subject = (f"🚆 You're owed ~{res['total']} kr for this week's delays" if res["total"]
                   else "🚆 Your weekly train delay report")
        sent = False if a.dry_run or "@" not in user.get("email", "") else send(user["email"], subject, body)
        print(f"{user['name']}: {len(res['claims'])} claimable trips, {res['total']} kr -> {out.name}"
              + (" (emailed)" if sent else " (not emailed)"))


if __name__ == "__main__":
    main()
