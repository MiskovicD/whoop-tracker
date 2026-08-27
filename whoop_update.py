#!/usr/bin/env python3
"""
Eén commando: band uitlezen, doorrekenen, naar Supabase sturen.

    uv run --no-project --with bleak python whoop_update.py --age 23
    ... --quick          alleen even aantikken (accu + status), geen meting
    ... --duration 120   langer live meten
    ... --sync           trek eerst de historie leeg (doe dit 's ochtends)
    ... --save-daily     tel deze dag mee voor je HRV-baseline

Draait de stappen als losse processen, zodat een mislukte stap de rest niet
meesleurt - en zodat elke stap dezelfde code gebruikt die je los al draait.
"""
import argparse, asyncio, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.expanduser("~/Desktop/whoop-research")
PLAYGROUND = os.path.join(RESEARCH, "research_playground.py")
DB = os.path.join(RESEARCH, "whoop.db")
STATE = os.path.expanduser("~/.whoop-tracker/alarm.json")
BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"


def adres():
    try:
        return json.load(open(STATE)).get("address")
    except Exception:
        return None


def stap(nr, tekst):
    print("\n[%d] %s" % (nr, tekst))
    print("-" * 58)


def draai(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd)
    return r.returncode == 0


async def lees_accu(a):
    """
    De standaard Battery Service (0x2A19) geeft één byte, 0-100. Dat is de
    betrouwbare waarde - de accu-events uit het propriëtaire kanaal zijn door
    de decoder zelf als 'unreliable' gemarkeerd en springen alle kanten op.
    """
    from bleak import BleakClient
    async with BleakClient(a) as c:
        raw = await c.read_gatt_char(BATTERY_UUID)
        return int(raw[0])


def main():
    p = argparse.ArgumentParser(description="Band uitlezen en naar Supabase sturen")
    p.add_argument("--age", type=int)
    p.add_argument("--hrmax", type=float)
    p.add_argument("--duration", type=int, default=90, help="seconden live meten")
    p.add_argument("--quick", action="store_true", help="geen meting, alleen accu + status")
    p.add_argument("--sync", action="store_true", help="eerst de historie leegtrekken")
    p.add_argument("--save-daily", action="store_true", help="tel mee voor de baseline")
    p.add_argument("--address", "-a", default=None)
    a = p.parse_args()

    if not (a.age or a.hrmax):
        sys.exit("geef --age of --hrmax mee")
    if not os.path.exists(PLAYGROUND):
        sys.exit("research_playground.py niet gevonden in %s" % RESEARCH)

    adr = a.address or adres()
    basis = [sys.executable, PLAYGROUND]
    if adr:
        basis += ["--address", adr]

    n = 0

    if a.sync:
        n += 1
        stap(n, "Historie leegtrekken")
        if not draai(basis + ["--timeout", "600", "sync"], cwd=RESEARCH):
            print("  sync gaf een fout - ga toch door met de rest")

    if not a.quick:
        n += 1
        stap(n, "Live meten (%ds) - band moet om je pols" % a.duration)
        if not draai(basis + ["--duration", str(a.duration), "live"], cwd=RESEARCH):
            print("  meting gaf een fout - ga toch door met de rest")
    else:
        n += 1
        stap(n, "Band aantikken voor verse status")
        draai(basis + ["info"], cwd=RESEARCH)

    n += 1
    stap(n, "Accustand uitlezen (0x2A19)")
    accu = None
    try:
        accu = asyncio.run(lees_accu(adr)) if adr else None
        print("  accu: %d %%" % accu)
    except Exception as e:
        print("  accu niet gelezen: %s" % e)

    metingen = [sys.executable, os.path.join(HERE, "whoop_metrics.py"), DB]
    metingen += ["--hrmax", str(a.hrmax)] if a.hrmax else ["--age", str(a.age)]
    if a.save_daily:
        metingen.append("--save-daily")

    n += 1
    stap(n, "Doorrekenen" + (" + baseline bijwerken" if a.save_daily else ""))
    draai(metingen)

    duw = [sys.executable, os.path.join(HERE, "whoop_push.py"), DB]
    duw += ["--hrmax", str(a.hrmax)] if a.hrmax else ["--age", str(a.age)]
    if accu is not None:
        duw += ["--battery", str(accu)]

    n += 1
    stap(n, "Naar Supabase sturen")
    ok = draai(duw)

    print("\n" + "=" * 58)
    print(" Klaar." if ok else " Klaar, maar het versturen ging mis.")
    print(" Tik in de app op 'ververs'.")
    print("=" * 58 + "\n")


if __name__ == "__main__":
    main()
