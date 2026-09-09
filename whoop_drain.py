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
import argparse, datetime as dt, os, signal, sqlite3, struct, subprocess, sys, time

BIJ = 180        # binnen 3 minuten van nu = de band is bij
MINIMAAL = 60    # minder dan een minuut aan nieuwe data = magere ronde
MAGER_MAX = 4    # pas na zoveel magere rondes op rij stoppen
CONSOLE_MIN = 50   # zoveel logframes in een ronde = de band werkt zijn logboek weg
LOGRONDES_MAX = 40 # veiligheidsrem: nooit eindeloos op logboek blijven wachten
LEEG_MAX = 5     # zoveel lege rondes op rij voordat we geloven dat hij leeg is
RUST = 6         # seconden tussen rondes; de band meldt zelf 'idle - settle'
RONDE_MAX = 240  # seconden per ronde; een normale ronde duurt 40-150 s
HANG_MAX = 3     # zoveel vastgelopen rondes op rij = de band is echt weg

RESEARCH = os.environ.get("WHOOP_RESEARCH") or os.path.expanduser("~/whoop-research")
# Niet op je Desktop: macOS blokkeert die map voor achtergrondtaken (launchd),
# waardoor een automatische sync stilzwijgend afbreekt op "Operation not permitted".
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
    """(records, nieuwste tijdstempel, console-frames) van de 1 Hz-historie.

    De console-frames tellen mee omdat de band zijn eigen debuglog in dezelfde
    flash heeft staan. Loopt die achter, dan levert een ronde wel logregels maar
    geen records - en dat is geen reden om te stoppen, want de sensordata zit
    erachter. Zonder dit gaf de drain het na een ronde op en kwam je nooit meer
    bij je data (vastgesteld 2026-09-04: 25 uur achterstand die niet wegliep).
    """
    if not os.path.exists(DB):
        return 0, None, 0
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
    try:
        # Alleen console-frames tellen. Bij elke verbinding komen er sowieso een
        # paar realtime-frames binnen; die zijn geen bewijs van vooruitgang.
        console = con.execute("select count(*) from frames where packet_type=?",
                              (0x32,)).fetchone()[0]
    except sqlite3.Error:
        console = 0
    con.close()
    return len(tss), (max(tss) if tss else None), console


def stop_ronde(kind):
    """Stopt de sync-client en al zijn kinderen; eerst netjes, dan hard."""
    for sein in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(kind.pid), sein)
        except OSError:
            return
        try:
            kind.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


def klok(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%d-%m %H:%M:%S") if ts else "-"


def main():
    p = argparse.ArgumentParser(description="Blijf syncen tot de band leeg is")
    p.add_argument("--max", type=int, default=10, help="hoogstens zoveel rondes")
    p.add_argument("--tot", help="stoppen zodra de historie tot voorbij dit tijdstip loopt, bv. 18:30")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--ronde-timeout", type=int, default=RONDE_MAX,
                   help="seconden voordat een vastgelopen ronde wordt afgebroken")
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

    n0, t0, f0 = stand()
    mager = 0
    logrondes = 0
    vastgelopen = 0
    leeg = 0
    print("start: %d records, tot %s\n" % (n0, klok(t0)))

    for ronde in range(1, a.max + 1):
        print("--- ronde %d/%d ---" % (ronde, a.max))
        # De sync-client negeert zijn eigen --timeout als de verbinding wegvalt:
        # gemeten op 2026-09-07 een ronde die 1 dag 19 uur bleef staan met een
        # limiet van 10 minuten. Daarom hier een eigen klok eromheen. Eigen
        # sessie, zodat we ook de kinderen kunnen stoppen en niet alleen de
        # bovenste schil.
        kind = subprocess.Popen(cmd, cwd=RESEARCH, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            kind.wait(timeout=a.ronde_timeout)
            vastgelopen = 0
        except KeyboardInterrupt:
            # Het kind draait in een eigen sessie, dus Ctrl-C uit de terminal
            # bereikt hem niet. Zonder dit blijft de sync-client achter, houdt
            # hij de band bezet en is de volgende ronde kansloos.
            print("\n\nAfgebroken - de lopende ronde wordt gestopt...")
            stop_ronde(kind)
            break
        except subprocess.TimeoutExpired:
            stop_ronde(kind)
            vastgelopen += 1
            print("   (ronde afgebroken na %d min: de verbinding hing, %d op rij)"
                  % (a.ronde_timeout / 60, vastgelopen))
            if vastgelopen >= HANG_MAX:
                print("%d vastgelopen rondes op rij - de band is buiten bereik.\n"
                      % vastgelopen)
                break
        n1, t1, f1 = stand()
        erbij = n1 - n0
        erbij_log = f1 - f0
        vooruit = (t1 - t0) if (t1 and t0) else 0
        print("   +%d records, nu tot %s (+%.0f min)\n" % (erbij, klok(t1), vooruit / 60))

        # Stoppen zodra we de werkelijke tijd hebben ingehaald. Wachten op nul
        # nieuwe records werkt niet: de band loopt door, dus elke ronde levert
        # nog de paar seconden op die er tussendoor bij kwamen.
        if erbij == 0:
            # Nul records maar wel frames: de band werkt zijn eigen debuglog weg.
            # Die staat in dezelfde flash en moet er eerst uit voordat de
            # sensordata weer aan de beurt is. Stoppen zou hier betekenen dat je
            # nooit meer bij de data komt.
            if erbij_log >= CONSOLE_MIN and logrondes < LOGRONDES_MAX:
                logrondes += 1
                print("   (+%d logregels, geen records: de band werkt zijn logboek"
                      " weg, ronde %d/%d)\n" % (erbij_log, logrondes, LOGRONDES_MAX))
                n0, t0, f0 = n1, t1, f1
                time.sleep(RUST)
                continue
            # Een lege ronde is geen bewijs dat de band leeg is: de verbinding
            # kan een tel wegvallen, of hij moet even bijkomen. Weten we
            # bovendien dat er nog uren ontbreken, dan is "de band is bij"
            # aantoonbaar onwaar. Gezien op 2026-09-09: gestopt in ronde 88 met
            # nog 53 uur te gaan, op grond van een enkele lege ronde.
            achter = (time.time() - t1) if t1 else 0
            leeg += 1
            if achter > BIJ and leeg < LEEG_MAX:
                print("   (lege ronde %d/%d, maar er ontbreekt nog %.1f uur"
                      " - opnieuw)\n" % (leeg, LEEG_MAX, achter / 3600))
                n0, t0, f0 = n1, t1, f1
                time.sleep(RUST * 3)
                continue
            if achter > BIJ:
                print("%d lege rondes op rij terwijl er nog %.1f uur ontbreekt."
                      " De band geeft niets meer - probeer het later opnieuw.\n"
                      % (leeg, achter / 3600))
            else:
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
        logrondes = 0
        leeg = 0
        n0, t0, f0 = n1, t1, f1
        time.sleep(RUST)
    else:
        print("Maximum aantal rondes bereikt; draai nog eens als je verder wilt.\n")

    n, t, _ = stand()
    print("=" * 52)
    print(" %d records, historie tot %s" % (n, klok(t)))
    print("=" * 52)


if __name__ == "__main__":
    main()
