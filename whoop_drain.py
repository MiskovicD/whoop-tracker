#!/usr/bin/env python3
"""
Blijft syncen tot de band niets nieuws meer geeft.

Eén sync levert ongeveer een half uur historie: de band stuurt een burst,
meldt 'Historical Dump Complete' en stopt. Bij een achterstand van uren
moet je dus meerdere rondes draaien. Dit script doet dat en stopt vanzelf
zodra de nieuwste tijdstempel niet meer opschuift.

    uv run --no-project --with bleak python whoop_drain.py
    ... --max 12        hoogstens 12 rondes
    ... --tot 18:30     stoppen zodra de historie tot na 18:30 loopt
"""
import argparse, datetime as dt, os, sqlite3, struct, subprocess, sys, time

BIJ = 180        # binnen 3 minuten van nu = de band is bij
MINIMAAL = 60    # minder dan een minuut aan nieuwe data = magere ronde
MAGER_MAX = 4    # pas na zoveel magere rondes op rij stoppen
RUST = 6         # seconden tussen rondes; de band meldt zelf 'idle - settle'

RESEARCH = os.path.expanduser("~/Desktop/whoop-research")
PLAYGROUND = os.path.join(RESEARCH, "research_playground.py")
DB = os.path.join(RESEARCH, "whoop.db")
STATE = os.path.expanduser("~/.whoop-tracker/alarm.json")


def adres():
    try:
        import json
        return json.load(open(STATE)).get("address")
    except Exception:
        return None


def stand():
    """(aantal records, nieuwste tijdstempel) van de 1 Hz-historie."""
    if not os.path.exists(DB):
        return 0, None
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    tss = set()
    try:
        for (hx,) in con.execute("select hex from frames where packet_type=?", (0x2F,)):
            try:
                b = bytes.fromhex(hx)[4:-4]
            except ValueError:
                continue
            if len(b) < 72 or b[0] != 0x2F or b[2] not in (0x05, 0x07):
                continue
            ts = struct.unpack_from("<I", b, 7)[0]
            if 1_500_000_000 < ts < 2_000_000_000:
                tss.add(ts)
    except sqlite3.Error:
        pass
    con.close()
    return len(tss), (max(tss) if tss else None)


def klok(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%d-%m %H:%M:%S") if ts else "-"


def main():
    p = argparse.ArgumentParser(description="Blijf syncen tot de band leeg is")
    p.add_argument("--max", type=int, default=10, help="hoogstens zoveel rondes")
    p.add_argument("--tot", help="stoppen zodra de historie tot voorbij dit tijdstip loopt, bv. 18:30")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--address", "-a", default=None)
    a = p.parse_args()

    doel = None
    if a.tot:
        u, m = (int(x) for x in a.tot.split(":"))
        doel = int(dt.datetime.now().replace(hour=u, minute=m, second=0,
                                             microsecond=0).timestamp())

    adr = a.address or adres()
    cmd = [sys.executable, PLAYGROUND]
    if adr:
        cmd += ["--address", adr]
    cmd += ["--timeout", str(a.timeout), "sync"]

    n0, t0 = stand()
    mager = 0
    print("start: %d records, tot %s\n" % (n0, klok(t0)))

    for ronde in range(1, a.max + 1):
        print("--- ronde %d/%d ---" % (ronde, a.max))
        subprocess.run(cmd, cwd=RESEARCH,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        n1, t1 = stand()
        erbij = n1 - n0
        vooruit = (t1 - t0) if (t1 and t0) else 0
        print("   +%d records, nu tot %s (+%.0f min)\n" % (erbij, klok(t1), vooruit / 60))

        # Stoppen zodra we de werkelijke tijd hebben ingehaald. Wachten op nul
        # nieuwe records werkt niet: de band loopt door, dus elke ronde levert
        # nog de paar seconden op die er tussendoor bij kwamen.
        if erbij == 0:
            print("Niets nieuws meer - de band is bij.\n")
            break
        if t1 and (time.time() - t1) < BIJ:
            print("Band is bij: historie loopt tot %s, dat is nu.\n" % klok(t1))
            break
        # Eén magere ronde betekent niet dat de band leeg is: bursts verschillen
        # in grootte. Pas na twee op rij stoppen we, anders breekt de lus af
        # terwijl er nog uren op de band staan.
        if erbij < MINIMAAL:
            mager += 1
            if mager >= MAGER_MAX:
                print("%d magere rondes op rij - de band is bij.\n" % mager)
                break
            print("   (magere ronde %d/%d, de band moet even bijkomen)" % (mager, MAGER_MAX))
        else:
            mager = 0
        if doel and t1 and t1 >= doel:
            print("Doel %s bereikt.\n" % a.tot)
            break
        n0, t0 = n1, t1
        time.sleep(RUST)
    else:
        print("Maximum aantal rondes bereikt; draai nog eens als je verder wilt.\n")

    n, t = stand()
    print("=" * 52)
    print(" %d records, historie tot %s" % (n, klok(t)))
    print("=" * 52)


if __name__ == "__main__":
    main()
