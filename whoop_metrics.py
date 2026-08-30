#!/usr/bin/env python3
"""
Berekent trainingsbelasting, HRV, slaap en herstel uit whoop.db.

Alle formules zijn gepubliceerd en na te slaan - geen black box:
  Edwards TRIMP   Edwards (1993), zone-gewogen tijd
  Banister TRIMP  Banister (1975), exponentieel gewogen HR-reserve
  Cole-Kripke     Cole et al. (1992), slaap/waak uit actigrafie
  RMSSD / SDNN    standaard HRV-tijdsdomein
  ln(RMSSD) z     Altini's methode: vergelijk met je eigen baseline

    python3 whoop_metrics.py --age 23
    python3 whoop_metrics.py --hrmax 191 --save-daily
"""
import argparse, json, math, os, statistics, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from whoop_report import (load, sessions, series, rmssd, sdnn,
                          fmt_dur, fmt_t, resting_hr, laatste_met_hr,
                          nacht_venster)

BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline.json")
MIN_BASELINE_DAYS = 7      # onder dit aantal is een z-score betekenisloos
MERGE_GAP = 30             # minuten: korte ontwaking breekt de nacht niet
MIN_SLEEP_MIN = 45         # minder dan dit is geen slaapperiode
MALIK = 0.20               # RR mag max 20% van zijn buur afwijken (artefactfilter)
HR_MARGE = 1.25            # terugvalgrens voor de hartslagpoort t.o.v. de rustwaarde
MIN_EFFICIENTIE = 0.60     # onder dit aandeel slaap is het geen slaapperiode
PIEK_EIS = 15.0            # ademhalingspiek moet 15x het gemiddelde vermogen zijn.
                           # Bij 4 kwam er 8,9/min uit, bij 15 werd het 13-14 met
                           # een spreiding van 0,2: die lage waarde was ruis, geen
                           # langzame ademhaling. Strenger kost vensters maar levert
                           # een uitkomst die je kunt vertrouwen.
MIN_VENSTERS = 20          # minder dan dit is geen bruikbare schatting

# Edwards-zones als fractie van HRmax, met hun gewicht
ZONES = [(0.50, 0.60, 1), (0.60, 0.70, 2), (0.70, 0.80, 3),
         (0.80, 0.90, 4), (0.90, 1.01, 5)]


# ------------------------------------------------------------ hartslagzones

def hr_zones(hr, hrmax):
    """Seconden per zone. hr = [(t, bpm)], aangenomen ~1 Hz."""
    out = {i: 0.0 for i in range(len(ZONES))}
    below = 0.0
    for _, v in hr:
        f = v / float(hrmax)
        for i, (lo, hi, _w) in enumerate(ZONES):
            if lo <= f < hi:
                out[i] += 1.0
                break
        else:
            if f < ZONES[0][0]:
                below += 1.0
            else:
                out[len(ZONES) - 1] += 1.0
    return out, below


def edwards_trimp(zones):
    """Som van (minuten in zone x gewicht). Sekse-onafhankelijk."""
    return sum((sec / 60.0) * ZONES[i][2] for i, sec in zones.items())


def banister_trimp(hr, rhr, hrmax, sex="m"):
    """
    D x dHR x k x e^(b x dHR), met dHR = HR-reserve-fractie.
    Constanten uit Banister: m -> k=0.64 b=1.92, v -> k=0.86 b=1.67.
    """
    if not hr or hrmax <= rhr:
        return None
    k, b = (0.64, 1.92) if sex == "m" else (0.86, 1.67)
    total = 0.0
    for _, v in hr:
        d = (v - rhr) / float(hrmax - rhr)
        if d <= 0:
            continue
        total += (1.0 / 60.0) * d * k * math.exp(b * d)   # 1 s = 1/60 min
    return total


def strain21(trimp, ref):
    """
    Eigen 0-21 schaal. Logaritmisch, net als Whoop, maar het ijkpunt is
    een keuze - niet hun kalibratie. ref = TRIMP van een zware dag.
    """
    if trimp <= 0:
        return 0.0
    return min(21.0, 21.0 * math.log1p(trimp) / math.log1p(max(ref, 1.0)))


# ------------------------------------------------------------------- HRV

def resp_rate(rr_punten, venster=300, stap=150):
    """
    Ademhaling uit respiratoire sinusaritmie: je hartslag versnelt bij inademen
    en vertraagt bij uitademen, dus de ademhaling zit als golf in de reeks
    R-R-intervallen.

    rr_punten = [(tijdstip, RR in ms)], ongelijkmatig bemonsterd.

    Werkwijze per venster van 5 minuten:
      1. hermonsteren op een gelijkmatig raster van 4 Hz (lineair)
      2. trend eruit halen met een voortschrijdend gemiddelde van 10 s
      3. vermogen bepalen tussen 0,1 en 0,5 Hz (6 tot 30 ademhalingen/min)
      4. de piekfrequentie is de ademhaling

    De mediaan over alle vensters is de uitkomst; de spreiding ertussen zegt
    hoeveel vertrouwen je erin mag hebben.

    LET OP de bemonsteringsgrens: de band levert ongeveer 0,8 R-R-intervallen
    per seconde, dus boven ~24 ademhalingen per minuut wordt het onbetrouwbaar.
    Voor slaap (12-20) volstaat het net.
    """
    ruw = sorted((t, v) for t, v in rr_punten if 300 <= v <= 2000)
    if len(ruw) < 200:
        return None

    # Artefacten er eerst uit (Malik): een gemiste of dubbel getelde slag geeft
    # een sprong van tientallen procenten, en dat is breedbandige ruis die de
    # ademhalingspiek overstemt.
    pts, vorig = [], ruw[0][1]
    for t, v in ruw:
        if abs(v - vorig) <= MALIK * vorig:
            pts.append((t, v))
        vorig = v
    if len(pts) < 200:
        return None

    schattingen, totaal = [], [0]
    begin, eind = pts[0][0], pts[-1][0]
    t0 = begin
    while t0 + venster <= eind:
        blok = [(t, v) for t, v in pts if t0 <= t < t0 + venster]
        t0 += stap
        totaal[0] += 1
        if len(blok) < venster * 0.4:            # te weinig slagen in dit venster
            continue

        FS = 4.0
        n = int(venster * FS)
        raster, j = [], 0
        for i in range(n):
            tt = blok[0][0] + i / FS
            while j + 1 < len(blok) and blok[j + 1][0] < tt:
                j += 1
            if j + 1 >= len(blok):
                raster.append(blok[-1][1]); continue
            t1, v1 = blok[j]; t2, v2 = blok[j + 1]
            f = 0.0 if t2 == t1 else (tt - t1) / (t2 - t1)
            raster.append(v1 + (v2 - v1) * max(0.0, min(1.0, f)))

        k = int(FS * 5)                          # 10 s venster, halve breedte
        x = [raster[i] - sum(raster[max(0, i - k):i + k + 1])
             / len(raster[max(0, i - k):i + k + 1]) for i in range(n)]

        spectrum = []
        f = 0.10
        while f <= 0.50:
            w = 2 * math.pi * f / FS
            re = sum(x[i] * math.cos(w * i) for i in range(n))
            im = sum(x[i] * math.sin(w * i) for i in range(n))
            spectrum.append((f, re * re + im * im))
            f += 0.004

        best_f, best_p = max(spectrum, key=lambda z: z[1])
        gemiddeld = sum(p for _, p in spectrum) / len(spectrum)
        # Alleen vensters met een duidelijk uitstekende piek. Zonder deze eis
        # telt ruis net zo hard mee als een echte ademhalingsgolf.
        if gemiddeld > 0 and best_p / gemiddeld >= PIEK_EIS:
            schattingen.append(best_f * 60.0)

    if len(schattingen) < MIN_VENSTERS:
        return None
    schattingen.sort()
    n = len(schattingen)
    med = schattingen[n // 2]
    # Spreiding als halve interkwartielafstand: robuuster dan min-max, want
    # een handvol uitschieters zegt niets over de betrouwbaarheid van de rest.
    spreiding = (schattingen[3 * n // 4] - schattingen[n // 4]) / 2.0
    return {"rpm": med, "vensters": n, "spreiding": spreiding,
            "bruikbaar": 100.0 * n / max(1, totaal[0])}


def nacht_temp(temp, sl):
    """
    Gemiddelde huidtemperatuur tijdens de slaap.

    De waarde is een ruwe ADC-uitlezing, niet in graden - Whoop rekent die
    in de cloud om. Wel weten we sinds de sauna-meting van 27-08 dat hij
    STIJGT bij warmte, dus de afwijking t.o.v. je eigen baseline is
    betekenisvol. Dat is ook wat Whoop zelf toont.
    """
    if not temp or not sl:
        return None
    v = [x for t, x in temp if sl["start"] <= t <= sl["end"]]
    if len(v) < 60:
        return None
    return sum(v) / len(v)


def temp_afwijking(bl, vandaag, waarde):
    """Afwijking t.o.v. het gemiddelde van je eerdere nachten, in procenten."""
    eerder = [d["skin_temp"] for dag, d in bl["days"].items()
              if dag != vandaag and d.get("skin_temp")]
    if len(eerder) < 2 or not waarde:
        return None, len(eerder)
    m = sum(eerder) / len(eerder)
    return (100.0 * (waarde - m) / m), len(eerder)


def hrv_metrics(rr, runs=None):
    """
    rr    = alle RR-intervallen in ms (voor SDNN en het gemiddelde)
    runs  = lijst van aaneengesloten reeksen (voor RMSSD en pNN50)

    RMSSD kijkt naar het verschil tussen opeenvolgende hartslagen. Reken je dat
    over een gat heen - twee intervallen die minuten uit elkaar liggen - dan
    krijg je een enorm verschil dat niets met variabiliteit te maken heeft.
    Vandaar dat opeenvolgende maten alleen binnen een reeks worden bepaald.
    """
    clean = [x for x in rr if 300 <= x <= 2000]
    dropped = len(rr) - len(clean)
    if len(clean) < 2:
        return None

    # Artefactfilter (Malik): een optische meting mist soms een slag of telt er
    # een dubbel. Zo'n interval wijkt dan tientallen procenten af van zijn buur.
    # Ongefilterd geeft dat een RMSSD van honderden ms, wat fysiologisch niet
    # kan - een rustwaarde ligt tussen 20 en 100 ms.
    def filter_run(run):
        r2 = [x for x in run if 300 <= x <= 2000]
        if len(r2) < 2:
            return []
        uit, vorig = [r2[0]], r2[0]
        for v in r2[1:]:
            if abs(v - vorig) <= MALIK * vorig:
                uit.append(v)
                vorig = v
            else:
                uit.append(None)          # onderbreekt het paar, houdt de reeks
                vorig = v
        return uit

    paren, verworpen, behouden = [], 0, []
    bronnen = runs if runs else [clean]
    for run in bronnen:
        f = filter_run(run)
        verworpen += sum(1 for x in f if x is None)
        behouden += [x for x in f if x is not None]
        for i in range(len(f) - 1):
            if f[i] is not None and f[i + 1] is not None:
                paren.append((f[i], f[i + 1]))

    if len(paren) < 2:
        return None
    diffs = [b - a for a, b in paren]
    r = math.sqrt(sum(d * d for d in diffs) / len(diffs))
    nn50 = sum(1 for d in diffs if abs(d) > 50)

    return {"n": len(clean), "dropped": dropped + verworpen, "pairs": len(paren),
            "runs": len(runs) if runs else 1,
            "rmssd": r, "sdnn": sdnn(behouden or clean),
            "mean_rr": sum(behouden or clean) / len(behouden or clean),
            "hr_from_rr": 60000.0 / (sum(behouden or clean) / len(behouden or clean)),
            "ln_rmssd": math.log(r) if r > 0 else None,
            "pnn50": 100.0 * nn50 / len(diffs)}


# ------------------------------------------------------------------ slaap

def per_minute(motion):
    """Bewegingsreeks (1 Hz) samenvatten tot activiteitstellingen per minuut."""
    buckets = {}
    for t, v in motion:
        buckets.setdefault(int(t // 60), []).append(v)
    return [(m, sum(vs)) for m, vs in sorted(buckets.items())]


def cole_kripke_scores(counts):
    """
    Cole-Kripke's gewogen som over een venster van -4 tot +2 minuten:
        D = 106*A-4 + 54*A-3 + 58*A-2 + 76*A-1 + 230*A0 + 74*A+1 + 67*A+2

    De weging is het waardevolle deel: één rustige minuut betekent niets,
    een rustige minuut omringd door rustige minuten wel. De originele
    drempel D<1 gaat uit van actigraaf-eenheden waarin stilliggen nul telt.
    Onze versnellingsmeter heeft een ruisvloer, dus die drempel bepalen we
    hieronder uit de verdeling zelf.
    """
    if len(counts) < 8:
        return []
    a = [c for _, c in counts]
    w = [(-4, 106), (-3, 54), (-2, 58), (-1, 76), (0, 230), (1, 74), (2, 67)]
    out = []
    for i in range(len(a)):
        d = 0.0
        for off, coef in w:
            j = i + off
            if 0 <= j < len(a):
                d += coef * a[j]
        out.append((counts[i][0], d))
    return out


def split_threshold(vals, iters=40):
    """
    1D k-means (k=2) in logruimte: rust en activiteit vormen twee wolken.
    Geeft de drempel ertussen terug, plus hoe ver de wolken uit elkaar
    liggen. Ligt dat te dicht op elkaar, dan is er geen echte tweedeling
    en mogen we niet splitsen.
    """
    xs = sorted(math.log1p(max(v, 0.0)) for v in vals)
    if len(xs) < 8:
        return None, 0.0
    lo, hi = xs[len(xs) // 10], xs[-max(1, len(xs) // 10)]
    if hi - lo < 1e-9:
        return None, 0.0
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        a = [x for x in xs if x <= mid] or [lo]
        b = [x for x in xs if x > mid] or [hi]
        nlo, nhi = sum(a) / len(a), sum(b) / len(b)
        if abs(nlo - lo) < 1e-9 and abs(nhi - hi) < 1e-9:
            break
        lo, hi = nlo, nhi
    return math.expm1((lo + hi) / 2.0), (hi - lo)


def detect_sleep(hr, motion, rhr):
    """
    Combineert Cole-Kripke met een hartslagpoort: echte slaap gaat gepaard
    met een hartslag rond of onder je rustwaarde. Dat vangt stilzitten af,
    wat actigrafie alleen niet kan.
    """
    scores = cole_kripke_scores(per_minute(motion))
    if not scores:
        return None
    thr, sep = split_threshold([d for _, d in scores])
    if thr is None or sep < 0.35:      # geen duidelijke rust/activiteit-tweedeling
        return None
    epochs = [(m, d < thr) for m, d in scores]

    hr_min = {}
    for t, v in hr:
        hr_min.setdefault(int(t // 60), []).append(v)

    # De hartslagpoort adaptief bepalen, net als de bewegingsdrempel. Een vaste
    # marge boven de rusthartslag werkt niet: die rustwaarde is het minimum van
    # de hele periode - je diepste slaapmoment - en de rest van de nacht ligt
    # daar gewoon boven. Met rhr*1.10 viel 87% van een echte nacht af.
    minuut_hr = {m: sum(v) / len(v) for m, v in hr_min.items()}
    hr_thr, hr_sep = split_threshold(list(minuut_hr.values()))
    if hr_thr is None or hr_sep < 0.05:
        hr_thr = rhr * HR_MARGE          # terugval als er geen tweedeling is
    else:
        hr_thr = max(hr_thr, rhr * 1.05)

    asleep = []
    for m, quiet in epochs:
        v = minuut_hr.get(m)
        low = (v < hr_thr) if v is not None else True
        asleep.append((m, quiet and low))

    # 1. aaneengesloten slaapruns.
    #    Let op de tweede voorwaarde: alleen minuten die er ook echt zijn tellen
    #    als aaneengesloten. Ontbrekende minuten staan niet in de lijst, dus op
    #    lijstvolgorde alleen liep een reeks dwars door een gat van uren heen -
    #    dat leverde een "nacht" van achttien uur met 17% efficientie op.
    runs, cur, vorige = [], None, None
    for m, is_sleep in asleep:
        onderbroken = vorige is not None and m - vorige > 2
        if is_sleep and (cur is None or onderbroken):
            if cur:
                runs.append(cur)
            cur = [m, m]
        elif is_sleep:
            cur[1] = m
        elif cur is not None:
            runs.append(cur); cur = None
        vorige = m
    if cur:
        runs.append(cur)
    if not runs:
        return None

    # 2. runs met minder dan MERGE_GAP minuten ertussen horen bij dezelfde
    #    nacht: een korte ontwaking maakt geen nieuwe slaapperiode.
    merged = [runs[0][:]]
    for r in runs[1:]:
        if r[0] - merged[-1][1] <= MERGE_GAP:
            merged[-1][1] = r[1]
        else:
            merged.append(r[:])

    # 3. alleen periodes die als nacht of dut tellen
    periods = [b for b in merged
               if sum(1 for m, sl in asleep if sl and b[0] <= m <= b[1]) >= MIN_SLEEP_MIN]
    if not periods:
        return None

    # Alleen periodes met een geloofwaardige efficientie: een blok waarin je
    # maar een fractie van de tijd slaapt is geen nacht maar een stilzit-venster.
    def eff(b):
        span = b[1] - b[0] + 1
        sl = sum(1 for m, x in asleep if x and b[0] <= m <= b[1])
        return sl / span if span else 0.0

    geloofwaardig = [b for b in periods if eff(b) >= MIN_EFFICIENTIE]
    if not geloofwaardig:
        return None

    main = max(geloofwaardig, key=lambda b: b[1] - b[0])
    span = main[1] - main[0] + 1
    slept = sum(1 for m, sl in asleep if sl and main[0] <= m <= main[1])
    return {"threshold": thr, "separation": sep, "hr_threshold": hr_thr,
            "periods": len(periods),
            "start": main[0] * 60, "end": (main[1] + 1) * 60,
            "in_bed_min": span, "asleep_min": slept,
            "waso_min": span - slept,
            "efficiency": 100.0 * slept / span if span else 0.0,
            "blocks": len(runs)}


# --------------------------------------------------------------- baseline

def load_baseline():
    if os.path.exists(BASELINE):
        try:
            with open(BASELINE) as f:
                return json.load(f)
        except ValueError:
            pass
    return {"days": {}}


def save_daily(bl, day, ln_rmssd=None, rhr=None, sleep_min=None, trimp=None,
               skin_temp=None, resp_rate=None):
    e = bl["days"].setdefault(day, {})
    for k, v in (("ln_rmssd", ln_rmssd), ("rhr", rhr), ("sleep_min", sleep_min),
                 ("skin_temp", skin_temp), ("resp_rate", resp_rate), ("trimp", trimp)):
        if v is not None:
            e[k] = v
    with open(BASELINE, "w") as f:
        json.dump(bl, f, indent=1, sort_keys=True)
    return len(bl["days"])


def recovery(bl, today, ln_rmssd, rhr):
    """
    z-score van vandaag tegen je eigen voortschrijdende baseline, daarna
    naar 0-100 afgebeeld. HRV weegt zwaar, rusthartslag corrigeert.
    Onder MIN_BASELINE_DAYS geven we bewust geen getal terug.
    """
    hist = [v for d, v in sorted(bl["days"].items()) if d != today]
    ln = [d["ln_rmssd"] for d in hist if d.get("ln_rmssd") is not None]
    rh = [d["rhr"] for d in hist if d.get("rhr") is not None]
    if len(ln) < MIN_BASELINE_DAYS:
        return {"ready": False, "have": len(ln), "need": MIN_BASELINE_DAYS}

    ln, rh = ln[-30:], rh[-30:]
    m, s = statistics.mean(ln), (statistics.pstdev(ln) or 0.01)
    z_hrv = (ln_rmssd - m) / s

    z_rhr = 0.0
    if rhr is not None and len(rh) >= MIN_BASELINE_DAYS:
        mr, sr = statistics.mean(rh), (statistics.pstdev(rh) or 0.01)
        z_rhr = -(rhr - mr) / sr                       # lagere RHR is beter

    z = 0.75 * z_hrv + 0.25 * z_rhr
    # tanh in plaats van harde afkap: een hele slechte dag blijft
    # onderscheidbaar van een slechte dag, en 0 of 100 wordt nooit bereikt.
    score = 50.0 + 50.0 * math.tanh(z / 1.8)
    return {"ready": True, "score": score, "z": z, "z_hrv": z_hrv,
            "z_rhr": z_rhr, "baseline_days": len(ln),
            "baseline_ln_rmssd": m}


# ------------------------------------------------------------------- CLI

def herbouw(data, hrmax, sex, trimp_ref):
    """
    Bouwt baseline.json opnieuw op uit elke nacht in de database.

    Nodig omdat de toewijzing onderweg veranderde: eerst op sessiedatum, nu op
    de dag van ontwaken. Zo staan er dagen door elkaar en ontbreken er nachten.
    """
    from datetime import date as _date
    dagen = sorted({datetime.fromtimestamp(r["t"]).astimezone().date()
                    for r in data["records"]})
    bl = {"days": {}}
    print("Baseline opnieuw opbouwen uit %d dagen...\n" % len(dagen))
    for d in dagen:
        nacht = nacht_venster(data["records"], d)
        if not nacht:
            continue
        ns = series(nacht)
        if not (ns["hr"] and ns["motion"]):
            continue
        rhr_n = resting_hr(ns["hr"])
        sl = detect_sleep(ns["hr"], ns["motion"], rhr_n or 60)
        if not sl:
            continue
        if not (datetime.fromtimestamp(sl["start"]).hour >= 20
                or datetime.fromtimestamp(sl["start"]).hour < 6):
            continue
        h = hrv_metrics(ns["rr"], ns.get("rr_runs"))
        if not h:
            continue
        t = nacht_temp(ns["temp"], sl)
        rr_n = [(tt, v) for r in nacht for tt, v in
                [(r["t"], x) for x in (r["d"].get("rr_ms") or [])]
                if sl["start"] <= tt <= sl["end"]]
        ad = resp_rate(rr_n)
        bl["days"][d.strftime("%Y-%m-%d")] = {
            "ln_rmssd": round(h["ln_rmssd"], 4), "rhr": round(rhr_n or 0, 2),
            "sleep_min": sl["asleep_min"],
            **({"skin_temp": round(t, 1)} if t else {}),
            **({"resp_rate": round(ad["rpm"], 1)} if ad else {})}
        print("  %s  slaap %3d min  HRV %5.1f ms  RHR %4.1f  temp %s  adem %s"
              % (d, sl["asleep_min"], h["rmssd"], rhr_n or 0,
                 ("%.0f" % t) if t else "-",
                 ("%.1f" % ad["rpm"]) if ad else "-"))
    with open(BASELINE, "w") as f:
        json.dump(bl, f, indent=1, sort_keys=True)
    print("\n%d nachten opgeslagen in %s" % (len(bl["days"]), BASELINE))


def bar(frac, width=28):
    n = int(round(max(0.0, min(1.0, frac)) * width))
    return "#" * n + "." * (width - n)


def main():
    p = argparse.ArgumentParser(description="Trainingsbelasting, HRV, slaap en herstel uit whoop.db")
    p.add_argument("db", nargs="?", default=os.path.expanduser("~/Desktop/whoop-research/whoop.db"))
    p.add_argument("--age", type=int, help="voor geschatte HRmax (Gellish: 211 - 0.64 x leeftijd)")
    p.add_argument("--hrmax", type=float, help="gemeten HRmax - altijd beter dan een formule")
    p.add_argument("--rhr", type=float, help="rusthartslag; standaard uit de data")
    p.add_argument("--sex", choices=["m", "v"], default="m",
                   help="alleen voor Banister-constanten (Edwards gebruikt dit niet)")
    p.add_argument("--trimp-ref", type=float, default=300.0,
                   help="TRIMP van een zware dag; ijkpunt voor de 0-21 schaal")
    p.add_argument("--all-sessions", action="store_true", help="alle sessies samen i.p.v. de laatste")
    p.add_argument("--save-daily", action="store_true", help="voeg vandaag toe aan de baseline")
    p.add_argument("--herbouw-baseline", action="store_true",
                   help="bouw baseline.json opnieuw op uit alle nachten in de database")
    p.add_argument("--json", help="schrijf de uitkomsten ook als JSON")
    a = p.parse_args()

    if not os.path.exists(a.db):
        sys.exit("whoop.db niet gevonden: %s" % a.db)

    if a.hrmax:
        hrmax, how = a.hrmax, "opgegeven"
    elif a.age:
        hrmax, how = 211.0 - 0.64 * a.age, "Gellish, %d jaar" % a.age
    else:
        sys.exit("geef --age of --hrmax mee")

    data = load(a.db)

    if a.herbouw_baseline:
        herbouw(data, hrmax, a.sex, a.trimp_ref)
        return

    ses = sessions(data["records"])
    if not ses:
        sys.exit("geen records in de database")

    recs = [r for s in ses for r in s] if a.all_sessions else laatste_met_hr(ses)
    sr = series(recs)
    hr, motion = sr["hr"], sr["motion"]
    if not hr:
        sys.exit("geen hartslag in deze sessie - zat de band om je pols?")

    vals = [v for _, v in hr]
    rhr = a.rhr or resting_hr(hr) or min(vals)   # zelfde definitie als de viewer
    dur = recs[-1]["t"] - recs[0]["t"]

    print("=" * 62)
    print(" WHOOP  %s  %s" % (fmt_t(recs[0]["t"]), fmt_dur(dur)))
    print("=" * 62)
    print(" HRmax %.0f bpm (%s)   rusthartslag %.0f bpm   %d metingen"
          % (hrmax, how, rhr, len(hr)))

    # -- zones
    zones, below = hr_zones(hr, hrmax)
    print("\n HARTSLAGZONES")
    tot = sum(zones.values()) + below
    if below:
        print("   onder Z1  %-28s %s" % (bar(below / tot), fmt_dur(below)))
    for i, (lo, hi, w) in enumerate(ZONES):
        sec = zones[i]
        if sec or i < 3:
            print("   Z%d  %3d-%3d  %-28s %s  (x%d)"
                  % (i + 1, lo * hrmax, hi * hrmax, bar(sec / tot), fmt_dur(sec), w))

    # -- belasting
    ed = edwards_trimp(zones)
    ba = banister_trimp(hr, rhr, hrmax, a.sex)
    print("\n TRAININGSBELASTING")
    print("   Edwards TRIMP    %8.1f   (zone-gewogen minuten)" % ed)
    if ba is not None:
        print("   Banister TRIMP   %8.1f   (sekse-constanten: %s)" % (ba, a.sex))
    print("   eigen schaal     %8.1f   / 21   ijkpunt TRIMP %.0f"
          % (strain21(ed, a.trimp_ref), a.trimp_ref))

    out = {"start": recs[0]["t"], "duration_s": dur, "hrmax": hrmax, "rhr": rhr,
           "hr_n": len(hr), "hr_avg": sum(vals) / len(vals),
           "hr_min": min(vals), "hr_max": max(vals),
           "edwards_trimp": ed, "banister_trimp": ba,
           "strain21": strain21(ed, a.trimp_ref),
           "zones_sec": {("Z%d" % (i + 1)): s for i, s in zones.items()}}

    # -- HRV
    print("\n HRV")
    h = hrv_metrics(sr["rr"], sr.get("rr_runs"))
    if h:
        print("   RMSSD  %6.1f ms      SDNN  %6.1f ms      pNN50  %4.1f %%"
              % (h["rmssd"], h["sdnn"], h["pnn50"]))
        print("   ln(RMSSD) %5.2f       %d intervallen in %d reeksen"
              % (h["ln_rmssd"], h["n"], h["runs"]))
        print("   %d bruikbare opeenvolgende paren, %d verworpen als artefact"
              % (h["pairs"], h["dropped"]))
        out["hrv"] = h
    else:
        print("   geen RR-intervallen - die zitten in R25-records en komen")
        print("   alleen uit een sync, niet uit de live-stream.")

    # -- slaap
    print("\n SLAAP")
    sl = detect_sleep(hr, motion, rhr) if motion else None
    if sl:
        print("   %s  ->  %s" % (fmt_t(sl["start"]), fmt_t(sl["end"])))
        print("   in bed %s   geslapen %s   wakker %s"
              % (fmt_dur(sl["in_bed_min"] * 60), fmt_dur(sl["asleep_min"] * 60),
                 fmt_dur(sl["waso_min"] * 60)))
        print("   efficientie %.0f %%   %d aaneengesloten blokken"
              % (sl["efficiency"], sl["blocks"]))
        out["sleep"] = sl
    else:
        print("   geen slaapperiode van %d+ minuten gevonden." % MIN_SLEEP_MIN)

    # -- herstel
    print("\n HERSTEL")
    bl = load_baseline()

    # De baseline gaat over NACHTEN, niet over sessies. Een sessie loopt
    # inmiddels over meerdere dagen, en dan vindt detect_sleep telkens dezelfde
    # eerste nacht terug en overschrijft die onder dezelfde datum - waardoor de
    # baseline nooit groeit. Dus: pak het nachtvenster van de laatste dag.
    laatste_dag = datetime.fromtimestamp(data["records"][-1]["t"]).astimezone().date()
    nacht = nacht_venster(data["records"], laatste_dag)
    ns = series(nacht) if nacht else None
    nacht_h = nacht_sl = None
    if ns and ns["hr"] and ns["motion"]:
        nacht_rhr = resting_hr(ns["hr"]) or rhr
        kandidaat = detect_sleep(ns["hr"], ns["motion"], nacht_rhr)
        if kandidaat:
            begin = datetime.fromtimestamp(kandidaat["start"]).hour
            if begin >= 20 or begin < 6:          # een nacht begint 's nachts
                nacht_sl = kandidaat
                nacht_h = hrv_metrics(ns["rr"], ns.get("rr_runs"))
                print("   nacht van %s: %s, %d min geslapen, HRV %s"
                      % (laatste_dag.strftime("%d-%m"),
                         datetime.fromtimestamp(kandidaat["start"]).strftime("%H:%M"),
                         kandidaat["asleep_min"],
                         ("%.0f ms" % nacht_h["rmssd"]) if nacht_h else "-"))
    day = laatste_dag.strftime("%Y-%m-%d")
    nacht_t = nacht_temp(ns["temp"], nacht_sl) if (ns and nacht_sl) else None
    nacht_ad = None
    if ns and nacht_sl:
        rr_nacht = [(t, v) for r in nacht for t, v in
                    [(r["t"], x) for x in (r["d"].get("rr_ms") or [])]
                    if nacht_sl["start"] <= t <= nacht_sl["end"]]
        nacht_ad = resp_rate(rr_nacht)
        if nacht_ad:
            print("   ademhaling deze nacht: %.1f per minuut  (+/- %.1f, %d vensters)"
                  % (nacht_ad["rpm"], nacht_ad["spreiding"], nacht_ad["vensters"]))
    if nacht_t:
        afw, n_eerder = temp_afwijking(bl, day, nacht_t)
        print("   huidtemp deze nacht: %.0f (ruw)%s" % (nacht_t,
              ("   %+.1f%% t.o.v. %d eerdere nachten" % (afw, n_eerder))
              if afw is not None else "   (nog geen vergelijking)"))

    if a.save_daily and not (nacht_h and nacht_sl):
        print("   niet opgeslagen in de baseline: daarvoor is een nacht nodig")
        print("   (HRV %s, slaap %s). Een baseline van losse dagmetingen"
              % ("ja" if nacht_h else "nee", "ja" if nacht_sl else "nee"))
        print("   is niet vergelijkbaar en maakt elke z-score scheef.")
    elif a.save_daily:
        n = save_daily(bl, day,
                       ln_rmssd=nacht_h["ln_rmssd"],
                       rhr=resting_hr(ns["hr"]) or rhr,
                       sleep_min=nacht_sl["asleep_min"],
                       skin_temp=nacht_t,
                       resp_rate=nacht_ad["rpm"] if nacht_ad else None,
                       trimp=ed)
        print("   %s opgeslagen in de baseline (%d dagen totaal)" % (day, n))
        bl = load_baseline()

    if h:
        rec = recovery(bl, day, h["ln_rmssd"], rhr)
        if rec["ready"]:
            print("   %.0f %%   z=%+.2f  (HRV %+.2f, RHR %+.2f)"
                  % (rec["score"], rec["z"], rec["z_hrv"], rec["z_rhr"]))
            print("   baseline over %d dagen" % rec["baseline_days"])
            out["recovery"] = rec
        else:
            print("   nog %d van %d dagen baseline - een herstelscore zonder"
                  % (rec["need"] - rec["have"], rec["need"]))
            print("   eigen normaal is betekenisloos, dus die geef ik nog niet.")
    else:
        print("   wacht op HRV.")

    print()
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f, indent=1)
        print(" JSON geschreven: %s\n" % a.json)


if __name__ == "__main__":
    main()
