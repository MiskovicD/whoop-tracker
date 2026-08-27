#!/usr/bin/env python3
"""
Rekent per dag de metrics uit whoop.db en zet ze in Supabase, zodat de
PWA op je telefoon ze kan tonen.

Eerste keer logt hij in met je e-mail en wachtwoord. Daarna wordt alleen
de refresh-token bewaard in ~/.whoop-tracker/session.json (chmod 600);
je wachtwoord wordt nergens opgeslagen.

    python3 whoop_push.py --age 23              # laatste dag
    python3 whoop_push.py --age 23 --all        # alle dagen in de database
    python3 whoop_push.py --age 23 --dry-run    # laten zien, niets versturen
"""
import argparse, getpass, json, os, ssl, stat, sys, urllib.error, urllib.request
from datetime import datetime, timezone, date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from whoop_report import load, sessions, series, resting_hr
import whoop_metrics as M

SB_URL = "https://zxlythycfgpqwpquuswg.supabase.co"
SB_ANON = None          # wordt uit je bestaande app gelezen, zie anon_key()
STATE_DIR = os.path.expanduser("~/.whoop-tracker")
STATE = os.path.join(STATE_DIR, "session.json")
CURVE_POINTS = 120


# --------------------------------------------------------------- supabase

def anon_key():
    """
    De anon-sleutel is publiek (hij staat in je andere apps in de HTML en is
    bedoeld voor browsergebruik; RLS doet het echte werk). We lezen hem
    daaruit zodat je hem niet nog een keer hoeft over te typen.
    """
    global SB_ANON
    if SB_ANON:
        return SB_ANON
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("finance-tracker/index.html", "nyp-tracker/index.html",
                "uren-tracker/index.html"):
        path = os.path.join(here, rel)
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8", errors="ignore"):
            if "SB_ANON" in line and "eyJ" in line:
                SB_ANON = line.split('"')[1]
                return SB_ANON
    sys.exit("anon-sleutel niet gevonden; zet SB_ANON handmatig bovenin dit bestand")


def _req(url, data=None, headers=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    h = {"apikey": anon_key(), "Content-Type": "application/json"}
    h.update(headers or {})
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, context=ssl.create_default_context()) as resp:
            txt = resp.read().decode()
            return json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise RuntimeError("Supabase %s: %s" % (e.code, detail))


def save_state(s):
    if not os.path.isdir(STATE_DIR):
        os.makedirs(STATE_DIR, mode=0o700)
    with open(STATE, "w") as f:
        json.dump(s, f)
    os.chmod(STATE, stat.S_IRUSR | stat.S_IWUSR)      # alleen jij mag erbij


def login():
    """Vraagt e-mail en wachtwoord lokaal; bewaart alleen de refresh-token."""
    print("Eenmalig inloggen op Supabase (zelfde account als je andere apps).")
    email = input("  e-mail: ").strip()
    pw = getpass.getpass("  wachtwoord: ")
    d = _req(SB_URL + "/auth/v1/token?grant_type=password",
             {"email": email, "password": pw})
    s = {"refresh_token": d["refresh_token"], "access_token": d["access_token"],
         "user_id": d["user"]["id"]}
    save_state(s)
    print("  ingelogd, refresh-token bewaard in %s\n" % STATE)
    return s


def session():
    if not os.path.exists(STATE):
        return login()
    with open(STATE) as f:
        s = json.load(f)
    try:                                   # access-token verloopt na een uur
        d = _req(SB_URL + "/auth/v1/token?grant_type=refresh_token",
                 {"refresh_token": s["refresh_token"]})
        s = {"refresh_token": d["refresh_token"], "access_token": d["access_token"],
             "user_id": d["user"]["id"]}
        save_state(s)
        return s
    except RuntimeError:
        print("Refresh-token verlopen of ingetrokken.")
        return login()


def upsert(s, rows):
    url = (SB_URL + "/rest/v1/whoop_days?on_conflict=user_id,day")
    return _req(url, rows, {
        "Authorization": "Bearer " + s["access_token"],
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }, method="POST")


# ------------------------------------------------------------ per dag

def local_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().date()


def group_by_day(recs):
    """
    Groepeert op de dag waarop je wakker werd: records van voor 12:00 horen
    bij die dag, records van na 12:00 ook. Slaap over middernacht valt dus
    uiteen - daarom hangen we de nacht hieronder aan de ochtenddag.
    """
    out = {}
    for r in recs:
        out.setdefault(local_day(r["t"]), []).append(r)
    return out


def curve(hr, n=CURVE_POINTS):
    """Downsample de hartslagreeks tot n punten voor de grafiek op je telefoon."""
    if not hr:
        return None
    if len(hr) <= n:
        return [[int(t), round(v, 1)] for t, v in hr]
    step = len(hr) / float(n)
    out = []
    for i in range(n):
        chunk = hr[int(i * step):max(int(i * step) + 1, int((i + 1) * step))]
        if chunk:
            out.append([int(chunk[0][0]),
                        round(sum(v for _, v in chunk) / len(chunk), 1)])
    return out


def build_row(day, recs, user_id, hrmax, sex, trimp_ref, baseline, data_hello=None):
    sr = series(recs)
    hr = sr["hr"]
    if not hr:
        return None
    vals = [v for _, v in hr]
    rhr = resting_hr(hr) or min(vals)

    zones, below = M.hr_zones(hr, hrmax)
    ed = M.edwards_trimp(zones)
    ba = M.banister_trimp(hr, rhr, hrmax, sex)
    h = M.hrv_metrics(sr["rr"])
    sl = M.detect_sleep(hr, sr["motion"], rhr) if sr["motion"] else None

    row = {
        "user_id": user_id, "day": day.isoformat(),
        "hr_avg": round(sum(vals) / len(vals), 1),
        "hr_min": min(vals), "hr_max": max(vals), "rhr": round(rhr, 1),
        "worn_min": int(len(hr) / 60),
        "battery": (data_hello or {}).get("battery"),
        "trimp_edwards": round(ed, 2),
        "trimp_banister": round(ba, 2) if ba is not None else None,
        "strain21": round(M.strain21(ed, trimp_ref), 2),
        "hr_curve": curve(hr),
        "zones": {("Z%d" % (i + 1)): int(s) for i, s in zones.items()},
    }
    if h:
        row.update({"hrv_rmssd": round(h["rmssd"], 1), "hrv_sdnn": round(h["sdnn"], 1),
                    "ln_rmssd": round(h["ln_rmssd"], 4), "hrv_n": h["n"]})
    if sl:
        row.update({
            "sleep_start": datetime.fromtimestamp(sl["start"], timezone.utc).isoformat(),
            "sleep_end": datetime.fromtimestamp(sl["end"], timezone.utc).isoformat(),
            "sleep_min": sl["asleep_min"], "sleep_waso_min": sl["waso_min"],
            "sleep_efficiency": round(sl["efficiency"], 1)})

    if h:
        rec = M.recovery(baseline, day.isoformat(), h["ln_rmssd"], rhr)
        if rec.get("ready"):
            row.update({"recovery": round(rec["score"], 1),
                        "recovery_z": round(rec["z"], 3),
                        "baseline_days": rec["baseline_days"]})
        else:
            row["baseline_days"] = rec.get("have", 0)
    return row


def main():
    p = argparse.ArgumentParser(description="Zet je Whoop-metrics in Supabase")
    p.add_argument("db", nargs="?", default=os.path.expanduser("~/Desktop/whoop-research/whoop.db"))
    p.add_argument("--age", type=int)
    p.add_argument("--hrmax", type=float)
    p.add_argument("--sex", choices=["m", "v"], default="m")
    p.add_argument("--trimp-ref", type=float, default=300.0)
    p.add_argument("--all", action="store_true", help="alle dagen, niet alleen de laatste")
    p.add_argument("--dry-run", action="store_true", help="toon wat er verstuurd zou worden")
    a = p.parse_args()

    if a.hrmax:
        hrmax = a.hrmax
    elif a.age:
        hrmax = 211.0 - 0.64 * a.age
    else:
        sys.exit("geef --age of --hrmax mee")

    if not os.path.exists(a.db):
        sys.exit("whoop.db niet gevonden: %s" % a.db)

    data = load(a.db)
    days = group_by_day(data["records"])
    if not days:
        sys.exit("geen records in de database")

    wanted = sorted(days) if a.all else [max(days)]
    baseline = M.load_baseline()

    s = None if a.dry_run else session()
    uid = "00000000-0000-0000-0000-000000000000" if a.dry_run else s["user_id"]

    rows = []
    for d in wanted:
        row = build_row(d, days[d], uid, hrmax, a.sex, a.trimp_ref, baseline,
                        data.get("hello"))
        if row is None:
            print("  %s  overgeslagen (geen hartslag)" % d)
            continue
        rows.append(row)
        print("  %s  HR %.0f (%.0f-%.0f)  RHR %.0f  slaap %s  strain %.1f  HRV %s"
              % (d, row["hr_avg"], row["hr_min"], row["hr_max"], row["rhr"],
                 ("%d min" % row["sleep_min"]) if row.get("sleep_min") else "-",
                 row["strain21"],
                 ("%.0f ms" % row["hrv_rmssd"]) if row.get("hrv_rmssd") else "-"))

    if not rows:
        sys.exit("\nniets te versturen")

    if a.dry_run:
        print("\n--dry-run: niets verstuurd. Voorbeeldrij:")
        sample = dict(rows[-1])
        if sample.get("hr_curve"):
            sample["hr_curve"] = "[%d punten]" % len(sample["hr_curve"])
        print(json.dumps(sample, indent=1, ensure_ascii=False))
        return

    upsert(s, rows)
    print("\n%d dag(en) naar Supabase gestuurd." % len(rows))


if __name__ == "__main__":
    main()
