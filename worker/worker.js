// Bro server (Cloudflare Worker)
// - keeps the Trafiklab key secret and serves live departures
// - stores push subscriptions and reminder plans in D1
// - every minute: sends due train reminders and the evening "morning train was late" check
// Data from Trafiklab.se (CC-BY 4.0).

const API = "https://realtime-api.trafiklab.se/v1";
const DELAYS_URL = "https://raw.githubusercontent.com/dahlinpetter/delay-report/main/data/delays.json";
const APP_URL = "https://dahlinpetter.github.io/delay-report/";
const STATIONS = {
  "Lund C": "740000120", "Malmö C": "740000003", "Triangeln": "740001587", "Hyllie": "740001586",
  "Kastrup": "860050858", "Ørestad": "860000856", "København H": "860000626"
};
const DANISH = new Set(["Kastrup", "Ørestad", "København H"]);
const DK_DIRECTIONS = /østerport|helsingør|københavn|kastrup|nørreport|ørestad/i;
const TZ = "Europe/Stockholm";

// ---------- helpers ----------
const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type"
};
const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...cors } });
const b64u = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const b64uStr = s => b64u(new TextEncoder().encode(s));

function localParts(t) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", weekday: "short", hourCycle: "h23"
  }).formatToParts(new Date(t)).map(x => [x.type, x.value]));
  return { date: `${p.year}-${p.month}-${p.day}`, hm: `${p.hour}:${p.minute}`, weekday: p.weekday };
}
// "2026-11-06T23:42[:00]" in Stockholm time -> epoch ms
function localToEpoch(s) {
  const guess = Date.parse(s.slice(0, 16) + ":00Z");
  const p = localParts(guess);
  const asUtc = Date.parse(`${p.date}T${p.hm}:00Z`);
  return guess - (asUtc - guess);
}
const toMin = hm => { const [h, m] = hm.split(":").map(Number); return h * 60 + m; };
const fmt = m => `${String(Math.floor(m / 60) % 24).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

async function idFor(endpoint) {
  const h = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint));
  return [...new Uint8Array(h)].slice(0, 12).map(b => b.toString(16).padStart(2, "0")).join("");
}

// ---------- database ----------
let ready = false;
async function db(env) {
  if (!ready) {
    await env.DB.batch([
      env.DB.prepare("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)"),
      env.DB.prepare("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, sub TEXT, cfg TEXT, morning INTEGER DEFAULT 0, r1 TEXT, updated INTEGER)"),
      env.DB.prepare("CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, due INTEGER, mins INTEGER, train TEXT, sched TEXT, frm TEXT, too TEXT, sent INTEGER DEFAULT 0)"),
      env.DB.prepare("CREATE TABLE IF NOT EXISTS msgs (uid TEXT PRIMARY KEY, title TEXT, body TEXT, at INTEGER)"),
      env.DB.prepare("CREATE TABLE IF NOT EXISTS checks (uid TEXT, kind TEXT, at TEXT, date TEXT, dep TEXT, frm TEXT, too TEXT)")
    ]);
    ready = true;
  }
  return env.DB;
}

// VAPID keys are created on first use and kept in the database
async function vapid(env) {
  const d = await db(env);
  const row = await d.prepare("SELECT v FROM kv WHERE k='vapid'").first();
  if (row) return JSON.parse(row.v);
  const kp = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
  const v = { priv: await crypto.subtle.exportKey("jwk", kp.privateKey), pub: b64u(await crypto.subtle.exportKey("raw", kp.publicKey)) };
  await d.prepare("INSERT OR IGNORE INTO kv (k, v) VALUES ('vapid', ?)").bind(JSON.stringify(v)).run();
  const again = await d.prepare("SELECT v FROM kv WHERE k='vapid'").first();
  return JSON.parse(again.v);
}

// ---------- web push (no payload: the app fetches the message from /msg) ----------
async function push(env, uid, subJson, title, body) {
  const d = await db(env);
  await d.prepare("INSERT INTO msgs (uid, title, body, at) VALUES (?, ?, ?, ?) ON CONFLICT(uid) DO UPDATE SET title=excluded.title, body=excluded.body, at=excluded.at")
    .bind(uid, title, body, Date.now()).run();
  const sub = JSON.parse(subJson);
  const v = await vapid(env);
  const aud = new URL(sub.endpoint).origin;
  const header = b64uStr(JSON.stringify({ typ: "JWT", alg: "ES256" }));
  const claims = b64uStr(JSON.stringify({ aud, exp: Math.floor(Date.now() / 1000) + 12 * 3600, sub: APP_URL }));
  const key = await crypto.subtle.importKey("jwk", v.priv, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key, new TextEncoder().encode(`${header}.${claims}`));
  const res = await fetch(sub.endpoint, {
    method: "POST",
    headers: { TTL: "3600", Urgency: "high", Authorization: `vapid t=${header}.${claims}.${b64u(sig)}, k=${v.pub}`, "Content-Length": "0" }
  });
  if (res.status === 404 || res.status === 410) await d.prepare("DELETE FROM users WHERE id=?").bind(uid).run();
  return res.status;
}

// ---------- live departures ----------
async function departures(env, from, to, time) {
  const area = STATIONS[from];
  if (!area || !STATIONS[to]) return [];
  const url = `${API}/departures/${area}${time ? "/" + time : ""}?key=${env.TRAFIKLAB_KEY}`;
  const cache = caches.default, ck = new Request(url.replace(/key=[^&]+/, "k"));
  let res = await cache.match(ck);
  if (!res) {
    const r = await fetch(url);
    if (!r.ok) return [];
    res = new Response(await r.text(), { headers: { "Content-Type": "application/json", "Cache-Control": "max-age=60" } });
    await cache.put(ck, res.clone());
  }
  const data = await res.json();
  const toDanish = DANISH.has(to), fromDanish = DANISH.has(from);
  return (data.departures || []).filter(dp => {
    const r = dp.route || {}, ag = dp.agency || {};
    if (r.transport_mode !== "TRAIN") return false;
    const op = `${ag.id || ""} ${ag.name || ""}`;
    if (!/110|öresund|oresund|SJ|snälltåget/i.test(op)) return false;
    const dir = r.direction || "";
    if (toDanish && !fromDanish) return DK_DIRECTIONS.test(dir);
    if (fromDanish && !toDanish) return !DK_DIRECTIONS.test(dir);
    return true;
  }).map(dp => ({
    train: dp.trip?.technical_number ? String(dp.trip.technical_number) : "",
    sched: dp.scheduled, real: dp.realtime || dp.scheduled,
    delay: Math.round((dp.delay || 0) / 60), canceled: !!dp.canceled,
    platform: dp.realtime_platform?.designation || dp.scheduled_platform?.designation || "",
    direction: dp.route?.direction || "",
    alerts: (dp.alerts || []).map(a => a.title || a.text || a.header || "").filter(Boolean).slice(0, 3)
  }));
}

// ---------- reminders ----------
async function sendDueReminders(env) {
  const d = await db(env);
  const now = Date.now();
  const due = (await d.prepare("SELECT r.*, u.sub FROM reminders r JOIN users u ON u.id=r.uid WHERE r.sent=0 AND r.due<=? ORDER BY r.due LIMIT 50").bind(now).all()).results;
  for (const r of due) {
    await d.prepare("UPDATE reminders SET sent=1 WHERE id=?").bind(r.id).run();
    if (now - r.due > 10 * 60 * 1000) continue; // too late to be useful
    const list = await departures(env, r.frm, r.too, r.sched.slice(0, 16)).catch(() => []);
    const t = list.find(x => x.train === r.train) || null;
    const dep = r.sched.slice(11, 16);
    let title = `Train home ${t && t.delay > 0 ? `${dep} (+${t.delay} min)` : dep}`;
    let body;
    if (t && t.canceled) {
      const next = list.find(x => !x.canceled && x.sched > r.sched);
      title = `Your ${dep} train is cancelled`;
      body = next ? `Next train: ${next.real.slice(11, 16)} from ${r.frm}${next.platform ? `, track ${next.platform}` : ""}.` : `Check the next departures from ${r.frm}.`;
    } else {
      const leaves = t ? t.real.slice(11, 16) : dep;
      body = `Leaves ${r.frm} at ${leaves}${t && t.platform ? `, track ${t.platform}` : ""}, in about ${r.mins} min.` + (r.mins <= 15 ? " Time to head to the station." : "");
    }
    await push(env, r.uid, r.sub, title, body).catch(() => {});
  }
}

// Morning alert (40 min before the usual train) and ticket reminders
async function runChecks(env) {
  const now = Date.now(), lp = localParts(now), d = await db(env);
  const weekday = lp.weekday !== "Sat" && lp.weekday !== "Sun";
  const rows = (await d.prepare("SELECT c.*, u.sub FROM checks c JOIN users u ON u.id=c.uid WHERE c.at=? AND (c.date IS NULL OR c.date=?)").bind(lp.hm, lp.date).all()).results;
  for (const c of rows) {
    if (c.kind === "ticket") {
      const end = new Date(c.dep + "T12:00:00Z");
      const when = end.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "short", timeZone: "UTC" });
      await push(env, c.uid, c.sub, "Your 30-day ticket runs out soon", `It's valid until ${when}. Buy a new one in Skånetrafiken's app, then tap "I bought a new ticket" in Bro.`).catch(() => {});
      continue;
    }
    if (!weekday) continue;
    const list = await departures(env, c.frm, c.too, `${lp.date}T${fmt(Math.max(0, toMin(c.dep) - 15))}`).catch(() => []);
    const want = toMin(c.dep);
    const t = list.filter(x => Math.abs(toMin(x.sched.slice(11, 16)) - want) <= 3).sort((a, b) => Math.abs(toMin(a.sched.slice(11, 16)) - want) - Math.abs(toMin(b.sched.slice(11, 16)) - want))[0];
    if (!t) continue;
    if (t.canceled) {
      const next = list.find(x => !x.canceled && x.sched > t.sched);
      await push(env, c.uid, c.sub, `Your ${c.dep} is cancelled`, next ? `Take the ${next.real.slice(11, 16)} from ${c.frm}${next.platform ? `, track ${next.platform}` : ""} instead.` : `Check the next departures from ${c.frm}.`).catch(() => {});
    } else if (t.delay >= 5) {
      await push(env, c.uid, c.sub, `Your ${c.dep} is ${t.delay} min late`, `Now expected to leave ${c.frm} at ${t.real.slice(11, 16)}${t.platform ? `, track ${t.platform}` : ""}.${t.alerts[0] ? " " + t.alerts[0] : ""}`).catch(() => {});
    } else if (t.alerts.length) {
      await push(env, c.uid, c.sub, `Disruption on your ${c.dep} train`, t.alerts[0]).catch(() => {});
    }
  }
}

// Evening check: at the start of the user's home window, was a morning train late?
async function morningCheck(env) {
  const now = Date.now(), lp = localParts(now);
  if (lp.weekday === "Sat" || lp.weekday === "Sun") return;
  const d = await db(env);
  const users = (await d.prepare("SELECT * FROM users WHERE morning=1 AND r1=?").bind(lp.hm).all()).results;
  if (!users.length) return;
  const doc = await fetch(DELAYS_URL, { cf: { cacheTtl: 120 } }).then(r => r.json()).catch(() => null);
  if (!doc) return;
  for (const u of users) {
    const c = JSON.parse(u.cfg || "{}");
    const fi = doc.stops.indexOf(c.from), ti = doc.stops.indexOf(c.to);
    if (fi < 0 || ti < 0) continue;
    const lo = toMin(c.o1), hi = toMin(c.o2), late = [];
    for (const t of doc.trains) {
      if (t.d !== lp.date) continue;
      const a = t.s.find(x => x[0] === fi), b = t.s.find(x => x[0] === ti);
      if (!a || !b || b[1] <= a[1] || a[1] < lo || a[1] > hi) continue;
      if (b[2] >= 20 || t.c) late.push({ dep: a[1], delay: b[2], c: t.c });
    }
    if (!late.length) continue;
    late.sort((x, y) => y.delay - x.delay);
    const w = late[0], trip = c.type === "single" ? Number(c.price || 0) : Number(c.price || 0) / Math.max(1, Number(c.factor || 20));
    const pct = w.delay >= 60 ? 100 : w.delay >= 40 ? 75 : 50;
    const what = w.c ? `the ${fmt(w.dep)} was cancelled` : `the ${fmt(w.dep)} arrived ${w.delay} min late`;
    await push(env, u.id, u.sub, "Delay on your morning commute",
      `This morning ${what} at ${c.to}. Were you on it? Mark it in Bro. If so, you may be entitled to about ${Math.round(trip * pct / 100)} kr.`).catch(() => {});
  }
}

// ---------- automatic deletion (data minimisation) ----------
// - reminders: deleted 1 hour after they were due
// - messages: deleted after 24 hours
// - users without the evening check: deleted 6 hours after their last reminder / last app visit
// - all users: deleted after 30 days without opening the app
async function cleanup(env) {
  const d = await db(env), now = Date.now(), H = 3600000;
  await d.batch([
    d.prepare("DELETE FROM reminders WHERE due < ?").bind(now - H),
    d.prepare("DELETE FROM msgs WHERE at < ?").bind(now - 24 * H),
    d.prepare("DELETE FROM users WHERE morning=0 AND updated < ? AND id NOT IN (SELECT uid FROM reminders WHERE sent=0) AND id NOT IN (SELECT uid FROM checks)").bind(now - 6 * H),
    d.prepare("DELETE FROM users WHERE updated < ?").bind(now - 30 * 24 * H),
    d.prepare("DELETE FROM msgs WHERE uid NOT IN (SELECT id FROM users)"),
    d.prepare("DELETE FROM checks WHERE uid NOT IN (SELECT id FROM users)"),
    d.prepare("DELETE FROM checks WHERE kind='ticket' AND date < ?").bind(localParts(now - 7 * 24 * H).date)
  ]);
}

// ---------- HTTP ----------
export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, { headers: cors });
    const url = new URL(req.url), p = url.pathname;
    try {
      if (p === "/vapid") return json({ key: (await vapid(env)).pub });

      if (p === "/trains") {
        const from = url.searchParams.get("from"), to = url.searchParams.get("to"), time = url.searchParams.get("time");
        let list = await departures(env, from, to, time);
        if (time && list.length < 4) {
          const next = new Date(localToEpoch(time) + 60 * 60000), lp = localParts(next);
          list = list.concat(await departures(env, from, to, `${lp.date}T${lp.hm}`));
        }
        const seen = new Set();
        list = list.filter(x => { const k = x.train + x.sched; if (seen.has(k)) return false; seen.add(k); return true; });
        return json({ trains: list, source: "Trafiklab.se (CC-BY 4.0)" });
      }

      if (p === "/subscribe" && req.method === "POST") {
        const b = await req.json();
        if (!b.sub || !b.sub.endpoint) return json({ error: "missing subscription" }, 400);
        const id = await idFor(b.sub.endpoint), raw = b.cfg || {};
        const c = {}; for (const k of ["from", "to", "o1", "o2", "r1", "type", "price", "factor"]) if (raw[k] != null) c[k] = raw[k];
        await (await db(env)).prepare("INSERT INTO users (id, sub, cfg, morning, r1, updated) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET sub=excluded.sub, cfg=excluded.cfg, morning=excluded.morning, r1=excluded.r1, updated=excluded.updated")
          .bind(id, JSON.stringify({ endpoint: b.sub.endpoint, keys: b.sub.keys || {} }), JSON.stringify(c), b.morning ? 1 : 0, c.r1 || null, Date.now()).run();
        // morning alerts and ticket reminders: replace this user's checks
        const d = await db(env), checks = Array.isArray(b.checks) ? b.checks.slice(0, 4) : [];
        const ops = [d.prepare("DELETE FROM checks WHERE uid=?").bind(id)];
        for (const k of checks) {
          if (k.kind === "usual" && /^\d\d:\d\d$/.test(k.dep || "") && STATIONS[k.from] && STATIONS[k.to])
            ops.push(d.prepare("INSERT INTO checks (uid, kind, at, date, dep, frm, too) VALUES (?, 'usual', ?, NULL, ?, ?, ?)").bind(id, fmt((toMin(k.dep) - 40 + 1440) % 1440), k.dep, k.from, k.to));
          if (k.kind === "ticket" && /^\d{4}-\d\d-\d\d$/.test(k.date || "")) {
            const before = new Date(Date.parse(k.date + "T12:00:00Z") - 2 * 86400000).toISOString().slice(0, 10);
            ops.push(d.prepare("INSERT INTO checks (uid, kind, at, date, dep, frm, too) VALUES (?, 'ticket', '18:00', ?, ?, NULL, NULL)").bind(id, before, k.date));
          }
        }
        await d.batch(ops);
        return json({ id, checks: ops.length - 1 });
      }

      if (p === "/plan" && req.method === "POST") {
        const b = await req.json(), d = await db(env);
        const u = await d.prepare("SELECT id FROM users WHERE id=?").bind(b.id).first();
        if (!u) return json({ error: "turn on notifications first" }, 400);
        await d.prepare("DELETE FROM reminders WHERE uid=? AND sent=0").bind(b.id).run();
        const depAt = localToEpoch(b.sched), now = Date.now(), set = [];
        for (const mins of (b.mins || [60, 30, 15])) {
          const due = depAt - mins * 60000;
          if (due < now - 60000) continue;
          await d.prepare("INSERT INTO reminders (uid, due, mins, train, sched, frm, too) VALUES (?, ?, ?, ?, ?, ?, ?)")
            .bind(b.id, due, mins, String(b.train || ""), b.sched, b.from, b.to).run();
          set.push(mins);
        }
        return json({ set });
      }

      if (p === "/plan" && req.method === "DELETE") {
        await (await db(env)).prepare("DELETE FROM reminders WHERE uid=? AND sent=0").bind(url.searchParams.get("id")).run();
        return json({ ok: true });
      }

      if (p === "/me" && req.method === "DELETE") {
        const id = url.searchParams.get("id"), d = await db(env);
        await d.batch([
          d.prepare("DELETE FROM reminders WHERE uid=?").bind(id),
          d.prepare("DELETE FROM msgs WHERE uid=?").bind(id),
          d.prepare("DELETE FROM checks WHERE uid=?").bind(id),
          d.prepare("DELETE FROM users WHERE id=?").bind(id)
        ]);
        return json({ deleted: true });
      }

      if (p === "/msg") {
        const m = await (await db(env)).prepare("SELECT title, body FROM msgs WHERE uid=?").bind(url.searchParams.get("id")).first();
        return json(m || { title: "Bro", body: "Open Bro for the latest train info." });
      }

      if (p === "/test" && req.method === "POST") {
        const b = await req.json();
        const u = await (await db(env)).prepare("SELECT * FROM users WHERE id=?").bind(b.id).first();
        if (!u) return json({ error: "not subscribed" }, 400);
        const status = await push(env, u.id, u.sub, "Bro notifications are on", "You'll get train reminders here.").catch(e => "failed: " + e.message);
        return json({ status });
      }

      return json({ ok: true, service: "kvitt" });
    } catch (e) {
      return json({ error: String(e && e.message || e) }, 500);
    }
  },

  async scheduled(event, env, ctx) {
    const jobs = [sendDueReminders(env), morningCheck(env), runChecks(env)];
    if (new Date(event.scheduledTime).getUTCMinutes() % 15 === 0) jobs.push(cleanup(env));
    ctx.waitUntil(Promise.all(jobs));
  }
};
