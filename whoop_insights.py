#!/usr/bin/env python3
"""
Twee lagen boven je meetgegevens.

  Laag 1  statistiek in gewoon Python: verbanden tussen slaap, belasting,
          HRV, rusthartslag, temperatuur en ademhaling. Rekent zelf uit wat
          er te zien is, en zegt eerlijk "te weinig data" als het niet kan.

  Laag 2  Claude leest de UITKOMSTEN van laag 1 - niet je ruwe metingen - en
          vertaalt die naar iets leesbaars.

Die volgorde is het hele punt. Taalmodellen zijn slecht in patronen zoeken in
getallenreeksen en vinden altijd wel iets. Rekenen doe je met statistiek;
interpreteren en prioriteren doe je met taal.

    python3 whoop_insights.py              # alleen de statistiek
    python3 whoop_insights.py --uitleg     # met Claude erbij
"""
import argparse, json, math, os, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BASELINE = os.path.join(HERE, "baseline.json")

MIN_DAGEN = 8          # onder dit aantal is een correlatie zinloos
STERK = 0.5            # |r| hierboven noemen we een duidelijk verband

VELDEN = {
    "sleep_min":    "slaapduur",
    "ln_rmssd":     "HRV (log)",
    "rhr":          "rusthartslag",
    "skin_temp":    "huidtemperatuur",
    "resp_rate":    "ademhaling",
    "stress_rmssd": "HRV in rust overdag",
    "trimp":        "belasting",
    "gevoel":       "hoe je je voelde",
}


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    tel = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    noem = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return tel / noem if noem else None


def ci_grens(r, n):
    """
    Ruwe betrouwbaarheidsmarge via Fisher-transformatie. Met acht nachten is
    die marge breed, en dat hoort zichtbaar te zijn: een r van 0,6 op n=8 is
    iets heel anders dan dezelfde 0,6 op n=60.
    """
    if n < 4 or abs(r) >= 1:
        return None
    z = 0.5 * math.log((1 + r) / (1 - r))
    se = 1.0 / math.sqrt(n - 3)
    lo, hi = z - 1.96 * se, z + 1.96 * se
    t = lambda v: (math.exp(2 * v) - 1) / (math.exp(2 * v) + 1)
    return t(lo), t(hi)


def laad():
    if not os.path.exists(BASELINE):
        sys.exit("baseline.json niet gevonden - draai eerst whoop_metrics.py --save-daily")
    with open(BASELINE) as f:
        return json.load(f).get("days", {})


def analyse(dagen):
    sleutels = sorted(dagen)
    uit = {"dagen": len(sleutels), "van": sleutels[0] if sleutels else None,
           "tot": sleutels[-1] if sleutels else None,
           "verbanden": [], "vertraagd": [], "trends": [], "samenvatting": {}}

    # gemiddelden en spreiding per veld
    for veld, naam in VELDEN.items():
        v = [dagen[d][veld] for d in sleutels if dagen[d].get(veld) is not None]
        if len(v) >= 2:
            uit["samenvatting"][naam] = {
                "n": len(v), "gemiddeld": round(statistics.mean(v), 2),
                "spreiding": round(statistics.pstdev(v), 2),
                "min": round(min(v), 2), "max": round(max(v), 2)}

    if len(sleutels) < MIN_DAGEN:
        uit["te_weinig"] = MIN_DAGEN - len(sleutels)
        return uit

    # verbanden binnen dezelfde dag
    namen = list(VELDEN)
    for i, a in enumerate(namen):
        for b in namen[i + 1:]:
            paren = [(dagen[d][a], dagen[d][b]) for d in sleutels
                     if dagen[d].get(a) is not None and dagen[d].get(b) is not None]
            if len(paren) < MIN_DAGEN:
                continue
            r = pearson([p[0] for p in paren], [p[1] for p in paren])
            if r is None or abs(r) < STERK:
                continue
            ci = ci_grens(r, len(paren))
            uit["verbanden"].append({
                "a": VELDEN[a], "b": VELDEN[b], "r": round(r, 3), "n": len(paren),
                "ci": [round(ci[0], 2), round(ci[1], 2)] if ci else None,
                "overtuigend": bool(ci and (ci[0] > 0 or ci[1] < 0))})

    # verbanden met een dag vertraging: gisteren -> vandaag
    for a in ("trimp", "sleep_min", "stress_rmssd"):
        for b in ("ln_rmssd", "rhr", "gevoel", "skin_temp"):
            paren = []
            for i in range(1, len(sleutels)):
                gis, vd = dagen[sleutels[i - 1]], dagen[sleutels[i]]
                if gis.get(a) is not None and vd.get(b) is not None:
                    paren.append((gis[a], vd[b]))
            if len(paren) < MIN_DAGEN:
                continue
            r = pearson([p[0] for p in paren], [p[1] for p in paren])
            if r is None or abs(r) < STERK:
                continue
            ci = ci_grens(r, len(paren))
            uit["vertraagd"].append({
                "gisteren": VELDEN[a], "vandaag": VELDEN[b], "r": round(r, 3),
                "n": len(paren), "ci": [round(ci[0], 2), round(ci[1], 2)] if ci else None,
                "overtuigend": bool(ci and (ci[0] > 0 or ci[1] < 0))})

    # trend over de laatste week tegenover de week ervoor
    if len(sleutels) >= 14:
        for veld, naam in VELDEN.items():
            recent = [dagen[d][veld] for d in sleutels[-7:] if dagen[d].get(veld) is not None]
            ouder = [dagen[d][veld] for d in sleutels[-14:-7] if dagen[d].get(veld) is not None]
            if len(recent) >= 4 and len(ouder) >= 4:
                m1, m0 = statistics.mean(recent), statistics.mean(ouder)
                if m0 and abs(m1 - m0) / abs(m0) > 0.05:
                    uit["trends"].append({"wat": naam, "verandering_pct": round(100 * (m1 - m0) / m0, 1),
                                          "was": round(m0, 2), "nu": round(m1, 2)})
    return uit


def toon(a):
    print("=" * 62)
    print(" %d nachten, %s tot %s" % (a["dagen"], a["van"], a["tot"]))
    print("=" * 62)

    print("\n GEMIDDELDEN")
    for naam, s in a["samenvatting"].items():
        print("   %-22s %8.2f  +/- %.2f   (%d nachten)"
              % (naam, s["gemiddeld"], s["spreiding"], s["n"]))

    if a.get("te_weinig"):
        print("\n VERBANDEN")
        print("   Nog %d nachten nodig. Met minder dan %d metingen is elke"
              % (a["te_weinig"], MIN_DAGEN))
        print("   correlatie vooral toeval, en zou ik je patronen laten zien")
        print("   die er niet zijn.")
        return

    print("\n VERBANDEN BINNEN DEZELFDE DAG")
    if not a["verbanden"]:
        print("   Geen verband sterker dan %.1f gevonden." % STERK)
    for v in a["verbanden"]:
        merk = "" if v["overtuigend"] else "   (marge omvat nul: nog onzeker)"
        print("   %s <-> %s : r = %+.2f  n=%d%s" % (v["a"], v["b"], v["r"], v["n"], merk))

    print("\n GISTEREN -> VANDAAG")
    if not a["vertraagd"]:
        print("   Geen vertraagd verband gevonden.")
    for v in a["vertraagd"]:
        merk = "" if v["overtuigend"] else "   (marge omvat nul: nog onzeker)"
        print("   %s (gisteren) -> %s : r = %+.2f  n=%d%s"
              % (v["gisteren"], v["vandaag"], v["r"], v["n"], merk))

    if a["trends"]:
        print("\n TREND, afgelopen week tegenover de week ervoor")
        for t in a["trends"]:
            print("   %-22s %+6.1f%%   (%.2f -> %.2f)" % (t["wat"], t["verandering_pct"], t["was"], t["nu"]))


def uitleg_van_claude(a):
    try:
        import anthropic
    except ImportError:
        sys.exit("pip install anthropic  (of: uv run --with anthropic ...)")

    try:
        client = anthropic.Anthropic()
    except Exception as e:
        sys.exit("Geen Claude-inloggegevens gevonden: %s\n"
                 "Zet ANTHROPIC_API_KEY, of log in met 'ant auth login'." % e)

    system = (
        "Je bent een nuchtere sportfysioloog die naar de meetgegevens van één "
        "persoon kijkt. Je krijgt UITGEREKENDE statistiek, geen ruwe metingen.\n\n"
        "Regels:\n"
        "- Verzin geen verbanden die niet in de cijfers staan.\n"
        "- Een correlatie waarvan de marge nul omvat is geen bevinding; benoem "
        "die hoogstens als iets om in de gaten te houden.\n"
        "- Correlatie is geen oorzaak. Bij acht tot dertig nachten is elk "
        "verband voorlopig.\n"
        "- Geen medische diagnoses. Bij iets zorgwekkends: verwijs naar een arts.\n"
        "- Schrijf Nederlands, hooguit 200 woorden, geen opsomming van alle "
        "getallen - de gebruiker ziet die tabel er zelf al bij staan.\n"
        "- Noem het belangrijkste patroon eerst, en zeg wat de gebruiker er "
        "morgen concreet mee kan."
    )

    resp = client.messages.create(
        model="claude-opus-5",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content":
                   "Hier is de statistiek over mijn metingen:\n\n"
                   + json.dumps(a, indent=1, ensure_ascii=False)}],
    )
    if resp.stop_reason == "refusal":
        print("\n Claude heeft dit verzoek geweigerd.")
        return
    print("\n" + "=" * 62)
    print(" WAT CLAUDE ERVAN MAAKT")
    print("=" * 62)
    for blok in resp.content:
        if blok.type == "text":
            print("\n" + blok.text.strip())
    print()


def main():
    p = argparse.ArgumentParser(description="Patronen in je Whoop-gegevens")
    p.add_argument("--uitleg", action="store_true", help="laat Claude de uitkomsten duiden")
    p.add_argument("--json", help="schrijf de statistiek ook naar dit bestand")
    a = p.parse_args()

    resultaat = analyse(laad())
    toon(resultaat)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(resultaat, f, indent=1, ensure_ascii=False)
    if a.uitleg:
        if resultaat.get("te_weinig"):
            print("\n Geen uitleg gevraagd aan Claude: er valt nog niets te duiden.")
        else:
            uitleg_van_claude(resultaat)


if __name__ == "__main__":
    main()
