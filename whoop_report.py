#!/usr/bin/env python3
"""
Leest whoop.db (van OpenStrap research_playground.py) en schrijft een
self-contained HTML-rapport. Alleen stdlib - geen pip install nodig.

    python3 whoop_report.py [pad/naar/whoop.db] [-o rapport.html]
"""
import argparse, json, math, os, sqlite3, struct, sys, html
from datetime import datetime, timezone

GAP = 120.0          # seconden stilte -> nieuwe sessie
HIST_TYPES = (0x05, 0x07)   # 1 Hz-historie; beide dezelfde indeling als R24.
                            # 0x07 en 0x05 komen door elkaar uit de flash, en wie
                            # er maar één van pakt mist zomaar de helft van zijn dag.
GSR_SENTINEL = 65000 # >= dit is 0xFFFF-achtig: geen huidcontact, geen meting
RHR_WINDOW = 60      # rolling venster voor rusthartslag


# ---------------------------------------------------------------- inlezen

def read_history(con):
    """
    Leest de 1 Hz-historie uit de ruwe frames.

    De research-client herkent recordtype 7 niet en bewaart die frames als
    ongedecodeerde 'data'. De indeling blijkt identiek aan R24, geverifieerd
    tegen live metingen: byte 17 gaf 624 van de 624 overlappende seconden
    exact dezelfde hartslag, en de zwaartekrachtvector op offset 36 heeft een
    lengte van 1,001 g.

        [0]=0x2F pakkettype   [2]=0x07 recordtype
        [7:11]  u32 unix-tijd    [17] hartslag    [18] aantal RR
        [19:27] RR-intervallen (u16, ms)          [36:48] accel x/y/z (float g)
        [51] contactkwaliteit 0-198               [68] huidtemp (relatief)
    """
    uit = {}
    try:
        rijen = con.execute("select hex from frames where packet_type=?", (0x2F,))
    except sqlite3.Error:
        return []
    for (hx,) in rijen:
        try:
            inner = bytes.fromhex(hx)[4:-4]          # kop van 4, CRC32 van 4
        except ValueError:
            continue
        if len(inner) < 72 or inner[0] != 0x2F or inner[2] not in HIST_TYPES:
            continue
        ts = struct.unpack_from("<I", inner, 7)[0]
        if not (1_500_000_000 < ts < 2_000_000_000):
            continue
        n = inner[18]
        rr = [struct.unpack_from("<H", inner, 19 + 2 * i)[0] for i in range(min(n, 4))]
        rr = [v for v in rr if 300 <= v <= 2000]
        ax, ay, az = struct.unpack_from("<fff", inner, 36)
        uit[ts] = {"kind": "hist", "rec_type": inner[2], "hr": inner[17], "rr_ms": rr,
                   "accel_g": [ax, ay, az], "skin_contact": inner[51],
                   "skin_temp_raw": struct.unpack_from("<H", inner, 68)[0],
                   "ts_epoch": ts}
    return [{"t": float(ts), "kind": "hist", "ts": ts, "d": d}
            for ts, d in sorted(uit.items())]


def load(db_path):
    """Open read-only, zodat een lopende sync ons niet blokkeert."""
    uri = "file:%s?mode=ro" % os.path.abspath(db_path).replace("?", "%3f")
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    out = {"records": [], "events": [], "battery": [], "hello": None, "sync": []}

    for r in con.execute("select t, kind, ts_device, decoded from records order by t"):
        try:
            d = json.loads(r["decoded"]) if r["decoded"] else {}
        except (ValueError, TypeError):
            d = {}
        out["records"].append({"t": r["t"], "kind": r["kind"],
                               "ts": r["ts_device"], "d": d})

    for r in con.execute("select t, name, ts_device, decoded from events order by t"):
        try:
            d = json.loads(r["decoded"]) if r["decoded"] else {}
        except (ValueError, TypeError):
            d = {}
        out["events"].append({"t": r["t"], "name": r["name"] or d.get("event") or "?",
                              "ts": r["ts_device"]})

    for r in con.execute("select t, pct, charging, source from battery order by t"):
        out["battery"].append({"t": r["t"], "pct": r["pct"],
                               "charging": r["charging"], "source": r["source"]})

    try:
        h = con.execute("select * from hello order by t desc limit 1").fetchone()
        if h:
            out["hello"] = dict(h)
    except sqlite3.Error:
        pass

    try:
        for r in con.execute("select t, batch_id, n_records, complete from sync_batches order by t"):
            out["sync"].append(dict(r))
    except sqlite3.Error:
        pass

    hist = read_history(con)
    if hist:
        out["records"] = sorted(out["records"] + hist, key=lambda r: r["t"])
        out["history_n"] = len(hist)

    con.close()
    return out


# ---------------------------------------------------------------- afleiden

def sessions(records):
    """Knip de records in aaneengesloten sessies op basis van tijdgaten."""
    ses, cur, prev = [], [], None
    for r in records:
        if prev is not None and r["t"] - prev > GAP:
            ses.append(cur); cur = []
        cur.append(r); prev = r["t"]
    if cur:
        ses.append(cur)
    return ses


def series(recs):
    """Trek de bruikbare tijdreeksen uit een sessie."""
    hr_by_sec = {}                # seconde -> (prioriteit, bpm); ontdubbelt
    gsr, motion, orient, rr = [], [], [], []
    rr_per_sec = {}               # seconde -> RR-intervallen uit de historie
    g_by_sec = {}                 # seconde -> zwaartekrachtvector uit de historie
    bad_gsr = [0]
    for r in recs:
        d, t = r["d"], r["ts"] or r["t"]
        v = d.get("hr")
        if isinstance(v, (int, float)) and v > 0:
            # realtime_hr is het toegewijde hartslagrecord; R10 draagt hem ook,
            # dus zonder deze voorkeur telt elke seconde dubbel.
            prio = {"realtime_hr": 0, "hist": 1}.get(r["kind"], 2)
            sec = int(t)
            if sec not in hr_by_sec or prio < hr_by_sec[sec][0]:
                hr_by_sec[sec] = (prio, float(v))
        v = d.get("gsr")
        if isinstance(v, (int, float)) and v > 0:
            if v >= GSR_SENTINEL:
                bad_gsr[0] += 1          # 0xFFFF-achtig = geen huidcontact
            else:
                gsr.append((t, float(v)))

        a = d.get("accel")
        if isinstance(a, dict) and all(k in a for k in ("x", "y", "z")):
            try:
                rng = [float(a[k]["max"]) - float(a[k]["min"]) for k in "xyz"]
                motion.append((t, math.sqrt(sum(x * x for x in rng))))
                avg = [float(a[k]["avg"]) for k in "xyz"]
                orient.append((t, avg))
            except (KeyError, TypeError, ValueError):
                pass

        a = d.get("accel_g")
        if isinstance(a, list) and len(a) == 3:
            g_by_sec[int(t)] = a

        for key in ("rr_intervals_ms", "rr_ms", "rr_raw"):
            v = d.get(key)
            if isinstance(v, list) and v:
                schoon = [float(x) for x in v if isinstance(x, (int, float)) and x > 0]
                rr.extend(schoon)
                if schoon:
                    rr_per_sec.setdefault(int(t), []).extend(schoon)
                break

    hr = [(float(sec), v) for sec, (_p, v) in sorted(hr_by_sec.items())]

    # Beweging uit de historie: de band levert daar één zwaartekrachtvector per
    # seconde, geen min/max zoals R10. De verandering tussen twee opeenvolgende
    # seconden is dan de bruikbare activiteitsmaat.
    if g_by_sec and not motion:
        secs = sorted(g_by_sec)
        for vorige, nu in zip(secs, secs[1:]):
            if nu - vorige > 3:
                continue
            a, b = g_by_sec[vorige], g_by_sec[nu]
            d3 = math.sqrt(sum((b[i] - a[i]) ** 2 for i in range(3)))
            motion.append((float(nu), d3 * 1000.0))     # milli-g, leesbaarder

    # RR-intervallen alleen aaneenrijgen binnen een ononderbroken reeks: over
    # een gat heen is het verschil tussen twee intervallen betekenisloos, en
    # dat blaast een RMSSD volledig op.
    runs, huidig, vorige_sec = [], [], None
    for sec in sorted(rr_per_sec):
        if vorige_sec is not None and sec - vorige_sec > 2:
            if len(huidig) > 1:
                runs.append(huidig)
            huidig = []
        huidig.extend(rr_per_sec[sec])
        vorige_sec = sec
    if len(huidig) > 1:
        runs.append(huidig)

    return {"hr": hr, "gsr": gsr, "motion": motion, "orient": orient, "rr": rr,
            "rr_runs": runs, "gsr_dropped": bad_gsr[0]}


def resting_hr(hr):
    """Laagste voortschrijdend 60s-gemiddelde: een eerlijke RHR-proxy."""
    if len(hr) < 5:
        return None
    best = None
    for i in range(len(hr)):
        win = [v for t, v in hr if hr[i][0] <= t < hr[i][0] + RHR_WINDOW]
        if len(win) >= max(5, RHR_WINDOW // 3):
            m = sum(win) / len(win)
            if best is None or m < best:
                best = m
    return best if best is not None else min(v for _, v in hr)


def rmssd(rr):
    if len(rr) < 2:
        return None
    diffs = [rr[i + 1] - rr[i] for i in range(len(rr) - 1)]
    return math.sqrt(sum(d * d for d in diffs) / len(diffs))


def sdnn(rr):
    if len(rr) < 2:
        return None
    m = sum(rr) / len(rr)
    return math.sqrt(sum((x - m) ** 2 for x in rr) / len(rr))


def fmt_dur(s):
    s = int(s)
    if s < 60:
        return "%d s" % s
    if s < 3600:
        return "%d m %02d s" % (s // 60, s % 60)
    return "%d u %02d m" % (s // 3600, (s % 3600) // 60)


def fmt_t(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%d-%m %H:%M:%S")


# ---------------------------------------------------------------- tekenen

def chart(points, unit="", color="var(--accent)", height=200, fill=True):
    """Inline SVG lijngrafiek. points = [(t, value)]."""
    if len(points) < 2:
        return '<p class="empty">Te weinig datapunten om te tekenen.</p>'

    W, H = 1000, height
    pad_l, pad_r, pad_t, pad_b = 52, 12, 14, 26
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if y1 - y0 < 1e-9:
        y0, y1 = y0 - 1, y1 + 1
    pad = (y1 - y0) * 0.08
    y0, y1 = y0 - pad, y1 + pad
    span = (x1 - x0) or 1.0

    def px(t):
        return pad_l + (t - x0) / span * (W - pad_l - pad_r)

    def py(v):
        return pad_t + (1 - (v - y0) / (y1 - y0)) * (H - pad_t - pad_b)

    pts = " ".join("%.1f,%.1f" % (px(t), py(v)) for t, v in points)

    grid, base = [], H - pad_b
    for i in range(4):
        v = y0 + (y1 - y0) * i / 3.0
        y = py(v)
        grid.append('<line class="grid" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>' % (pad_l, y, W - pad_r, y))
        grid.append('<text class="ytick" x="%d" y="%.1f">%s</text>' % (pad_l - 8, y + 4, _num(v)))

    for frac in (0.0, 0.5, 1.0):
        t = x0 + span * frac
        anchor = "start" if frac == 0 else ("end" if frac == 1 else "middle")
        grid.append('<text class="xtick" x="%.1f" y="%d" text-anchor="%s">%s</text>'
                    % (px(t), H - 6, anchor,
                       datetime.fromtimestamp(t, timezone.utc).astimezone().strftime("%H:%M:%S")))

    area = ""
    if fill:
        area = ('<polygon class="area" style="fill:%s" points="%.1f,%.1f %s %.1f,%.1f"/>'
                % (color, px(x0), base, pts, px(x1), base))

    return (
        '<svg class="chart" viewBox="0 0 %d %d" preserveAspectRatio="none" role="img">'
        '%s%s<polyline class="line" style="stroke:%s" points="%s"/></svg>'
        '<div class="unit">%s</div>' % (W, H, "".join(grid), area, color, pts, html.escape(unit))
    )


def _num(v):
    a = abs(v)
    if a >= 1000:
        return "%.0f" % v
    if a >= 100:
        return "%.0f" % v
    if a >= 10:
        return "%.1f" % v
    return "%.2f" % v


def stat(label, value, sub=""):
    sub = '<div class="sub">%s</div>' % html.escape(sub) if sub else ""
    return ('<div class="stat"><div class="lab">%s</div><div class="val">%s</div>%s</div>'
            % (html.escape(label), value, sub))


def pending(title, why):
    return ('<section class="card pending"><h2>%s <span class="badge">nog geen data</span></h2>'
            '<p>%s</p></section>' % (html.escape(title), html.escape(why)))


# ---------------------------------------------------------------- rapport

CSS = """
:root{--bg:#fbfaf9;--panel:#fff;--ink:#1c1a17;--muted:#6b655d;--line:#e6e1da;
--accent:#c8553d;--accent2:#3d6b7d;--accent3:#7d6b3d;--ok:#3f7d5a;--shadow:0 1px 2px rgba(0,0,0,.05)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#161513;--panel:#1e1d1a;
--ink:#ece8e1;--muted:#9c958a;--line:#2f2d29;--accent:#e0705a;--accent2:#6fa3b8;--accent3:#b8a06f;
--ok:#63ad84;--shadow:none}}
*{box-sizing:border-box}
body{margin:0;padding:28px 20px 64px;background:var(--bg);color:var(--ink);
font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:1060px;margin:0 auto}
header{margin-bottom:26px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}
.meta{color:var(--muted);font-size:13.5px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:20px 22px;margin-bottom:18px;box-shadow:var(--shadow)}
h2{font-size:15px;margin:0 0 16px;letter-spacing:.01em;display:flex;align-items:center;gap:9px}
.badge{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;
color:var(--muted);border:1px solid var(--line);border-radius:99px;padding:2px 9px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:14px;margin-bottom:6px}
.stat{border-left:2px solid var(--line);padding-left:12px}
.lab{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.val{font-size:25px;font-variant-numeric:tabular-nums;letter-spacing:-.02em;margin-top:1px}
.val small{font-size:13px;color:var(--muted);font-weight:400;letter-spacing:0}
.sub{font-size:12px;color:var(--muted);margin-top:1px}
.chartwrap{overflow-x:auto}
.chart{width:100%;height:200px;display:block;min-width:420px}
.line{fill:none;stroke-width:1.6;vector-effect:non-scaling-stroke}
.area{opacity:.10}
.grid{stroke:var(--line);stroke-width:1;vector-effect:non-scaling-stroke}
.ytick,.xtick{fill:var(--muted);font-size:11px;font-family:inherit}
.ytick{text-anchor:end}
.unit{font-size:11.5px;color:var(--muted);margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:7px 10px 7px 0;border-bottom:1px solid var(--line)}
th{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600}
td.num{font-variant-numeric:tabular-nums}
.pending p{color:var(--muted);margin:0;font-size:13.5px}
.empty{color:var(--muted);font-size:13.5px;margin:0}
.note{color:var(--muted);font-size:12.5px;margin:12px 0 0;padding-top:12px;border-top:1px solid var(--line)}
.tl{display:flex;flex-wrap:wrap;gap:6px}
.ev{font-size:12px;border:1px solid var(--line);border-radius:6px;padding:3px 8px;color:var(--muted)}
.ev b{color:var(--ink);font-weight:600}
"""


def build(data, db_path):
    recs = data["records"]
    ses = sessions(recs)
    parts = []

    # -- kop
    h = data["hello"] or {}
    bits = []
    if h.get("serial"):
        bits.append("serial %s" % h["serial"])
    if h.get("battery") is not None:
        bits.append("accu %.1f%%" % h["battery"])
    if h.get("fw"):
        bits.append("fw %s" % h["fw"])
    bits.append("%d records, %d events" % (len(recs), len(data["events"])))
    bits.append(os.path.basename(db_path))

    parts.append('<header><h1>Whoop &mdash; lokaal rapport</h1>'
                 '<div class="meta">%s<br>gegenereerd %s</div></header>'
                 % (html.escape(" &middot; ".join(bits)).replace("&amp;middot;", "&middot;"),
                    datetime.now().strftime("%d-%m-%Y %H:%M")))

    if not ses:
        parts.append('<section class="card"><h2>Geen data</h2>'
                     '<p class="empty">De database bevat nog geen records. '
                     'Draai eerst <code>w --duration 600 live</code> met de band om je pols.</p></section>')
        return render(parts)

    # -- sessie-overzicht
    rows = []
    for i, s in enumerate(ses):
        sr = series(s)
        dur = s[-1]["t"] - s[0]["t"]
        hrv = [v for _, v in sr["hr"]]
        rows.append("<tr><td class='num'>%d</td><td>%s</td><td class='num'>%s</td>"
                    "<td class='num'>%d</td><td class='num'>%s</td></tr>"
                    % (i + 1, fmt_t(s[0]["t"]), fmt_dur(dur), len(s),
                       "%.0f&ndash;%.0f bpm" % (min(hrv), max(hrv)) if hrv else "&mdash;"))
    parts.append('<section class="card"><h2>Sessies</h2><table>'
                 '<tr><th>#</th><th>Start</th><th>Duur</th><th>Records</th><th>Hartslag</th></tr>'
                 '%s</table><p class="note">Een nieuwe sessie begint na %d seconden stilte.</p>'
                 '</section>' % ("".join(rows), int(GAP)))

    # -- laatste sessie in detail
    last = ses[-1]
    sr = series(last)
    dur = last[-1]["t"] - last[0]["t"]
    parts.append('<section class="card"><h2>Laatste sessie <span class="badge">%s &middot; %s</span></h2>'
                 % (html.escape(fmt_t(last[0]["t"])), html.escape(fmt_dur(dur))))

    hr = sr["hr"]
    if hr:
        vals = [v for _, v in hr]
        rhr = resting_hr(hr)
        secs = len(set(int(t) for t, _ in hr))
        worn = 100.0 * secs / max(1.0, dur)
        parts.append('<div class="stats">%s%s%s%s</div>' % (
            stat("Gemiddeld", "%.0f <small>bpm</small>" % (sum(vals) / len(vals))),
            stat("Rusthartslag", ("%.0f <small>bpm</small>" % rhr) if rhr else "&mdash;",
                 "laagste %ds-gemiddelde" % RHR_WINDOW),
            stat("Bereik", "%.0f&ndash;%.0f <small>bpm</small>" % (min(vals), max(vals))),
            stat("Gedragen", "%.0f<small>%%</small>" % min(worn, 100.0), "seconden met hartslag"),
        ))
        parts.append('<div class="chartwrap">%s</div>' % chart(hr, "hartslag (bpm)"))
    else:
        parts.append('<p class="empty">Geen hartslag in deze sessie &mdash; '
                     'de band moet om je pols zitten.</p>')
    parts.append("</section>")

    # -- beweging
    if sr["motion"]:
        mv = [v for _, v in sr["motion"]]
        still = 100.0 * sum(1 for v in mv if v < (sum(mv) / len(mv)) * 0.5) / len(mv)
        parts.append('<section class="card"><h2>Beweging</h2><div class="stats">%s%s</div>'
                     '<div class="chartwrap">%s</div>'
                     '<p class="note">Afgeleid uit de spreiding van de versnellingsmeter, '
                     'die intern op 100 Hz bemonstert en per seconde min/max/gemiddelde per as levert. '
                     'Ruwe eenheden &mdash; bruikbaar als relatieve maat, niet als absolute g.</p></section>'
                     % (stat("Gemiddeld", _num(sum(mv) / len(mv))),
                        stat("Stil", "%.0f<small>%%</small>" % still, "onder halve gemiddelde"),
                        chart(sr["motion"], "bewegingsintensiteit (ruw)", "var(--accent2)")))

    # -- GSR
    if sr["gsr"]:
        gv = [v for _, v in sr["gsr"]]
        parts.append('<section class="card"><h2>Huidgeleiding (GSR) <span class="badge">ongevalideerd</span></h2><div class="stats">%s%s</div>'
                     '<div class="chartwrap">%s</div>'
                     '<p class="note">Het veld heet <code>gsr</code> in de decoder, maar die naam '
                     'komt uit reverse engineering en is nooit tegen de hardware geverifieerd. '
                     'De waarde zwerft over het hele 16-bits bereik en verzadigt aan de bovenkant, '
                     'wat niet past bij een schone huidgeleidingsmeting. Behandel deze grafiek als '
                     'een signaal dat nog uitgezocht moet worden, niet als een stressmaat.%s</p></section>'
                     % (stat("Gemiddeld", _num(sum(gv) / len(gv))),
                        stat("Bereik", "%s&ndash;%s" % (_num(min(gv)), _num(max(gv)))),
                        chart(sr["gsr"], "geleiding (ruw)", "var(--accent3)"),
                        (" %d metingen weggelaten die tegen 0xFFFF aan zaten &mdash; "
                         "dat is de sentinelwaarde voor geen huidcontact."
                         % sr["gsr_dropped"]) if sr["gsr_dropped"] else ""))

    # -- HRV
    rr = sr["rr"]
    if rr:
        r_, s_ = rmssd(rr), sdnn(rr)
        parts.append('<section class="card"><h2>HRV</h2><div class="stats">%s%s%s</div>'
                     '<p class="note">Uit RR-intervallen. Controleer de eenheid: de decoder '
                     'markeert die als onbevestigd (ms versus 1/1024s-ticks).</p></section>'
                     % (stat("RMSSD", ("%.1f <small>ms</small>" % r_) if r_ else "&mdash;"),
                        stat("SDNN", ("%.1f <small>ms</small>" % s_) if s_ else "&mdash;"),
                        stat("Intervallen", "%d" % len(rr))))
    else:
        parts.append(pending("HRV", "RR-intervallen komen uit R25-records, en die zijn sync-only. "
                                    "Draag de band een nacht en draai daarna 'w --timeout 600 sync'."))

    # -- nog niet beschikbaar
    parts.append(pending("Huidtemperatuur en SpO₂",
                         "De band meet deze alleen tijdens je slaap. Ze zitten in de 0x5C-records "
                         "en komen mee met een sync na een nacht dragen."))
    parts.append(pending("Slaap, strain en recovery",
                         "Deze worden berekend uit HRV, rusthartslag en beweging over meerdere dagen. "
                         "Er is nog geen historie om ze op te baseren."))

    # -- accu
    real = [b for b in data["battery"] if b["source"] and "tentative" not in b["source"]]
    if real:
        parts.append('<section class="card"><h2>Accu</h2><div class="stats">%s</div>'
                     '<div class="chartwrap">%s</div></section>'
                     % (stat("Laatst", "%.1f<small>%%</small>" % real[-1]["pct"]),
                        chart([(b["t"], b["pct"]) for b in real], "accu (%)", "var(--ok)")))

    # -- events
    if data["events"]:
        seen, chips = set(), []
        for e in reversed(data["events"]):
            if e["name"] in seen:
                continue
            seen.add(e["name"])
            chips.append('<span class="ev"><b>%s</b> %s</span>'
                         % (html.escape(e["name"]), html.escape(fmt_t(e["t"]))))
            if len(chips) >= 24:
                break
        parts.append('<section class="card"><h2>Events <span class="badge">%d totaal</span></h2>'
                     '<div class="tl">%s</div>'
                     '<p class="note">Meest recente voorkomen per type.</p></section>'
                     % (len(data["events"]), "".join(chips)))

    return render(parts)


def render(parts):
    return ("<!doctype html><html lang=\"nl\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Whoop lokaal rapport</title><style>%s</style></head>"
            "<body><div class=\"wrap\">%s</div></body></html>" % (CSS, "".join(parts)))


def main():
    ap = argparse.ArgumentParser(description="Bouw een HTML-rapport uit whoop.db")
    ap.add_argument("db", nargs="?", default=os.path.expanduser("~/Desktop/whoop-research/whoop.db"))
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()

    if not os.path.exists(a.db):
        sys.exit("whoop.db niet gevonden: %s" % a.db)

    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.db)) or ".", "whoop_report.html")
    data = load(a.db)
    with open(out, "w") as f:
        f.write(build(data, a.db))

    print("%d records, %d events, %d sessies" %
          (len(data["records"]), len(data["events"]), len(sessions(data["records"]))))
    print("geschreven: %s" % out)


if __name__ == "__main__":
    main()
