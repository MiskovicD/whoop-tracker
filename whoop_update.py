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
import argparse, asyncio, datetime as dt, json, os, shutil, signal, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.environ.get("WHOOP_RESEARCH") or os.path.expanduser("~/whoop-research")
# Niet op je Desktop: macOS blokkeert die map voor achtergrondtaken (launchd),
# waardoor een automatische sync stilzwijgend afbreekt op "Operation not permitted".
PLAYGROUND = os.path.join(RESEARCH, "research_playground.py")
DB = os.path.join(RESEARCH, "whoop.db")
import whoop_config

# Adres, leeftijd en slaapdoel staan in ~/.whoop-tracker/config.json, niet
# langer verspreid over alarm.json en auto.conf.
BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"


def adres():
    return whoop_config.adres()


def stap(nr, tekst):
    print("\n[%d] %s" % (nr, tekst))
    print("-" * 58)


# Een BLE-aanroep die een slaapstand of een weggevallen verbinding niet
# overleeft komt nooit terug. De sync-client negeert daarbij zijn eigen
# --timeout. Gemeten op 2026-09-26: de stap "band aantikken" (info) stond 66
# minuten te hangen en hield de hele uursync 9,5 uur vast. Vandaar hier een
# eigen klok om elke stap.
STAP_TIMEOUT = 300          # seconden; een normale stap duurt seconden
SYNC_TIMEOUT = 900          # een sync-ronde mag langer, maar niet eindeloos


def draai(cmd, cwd=None, timeout=STAP_TIMEOUT):
    """Start in een eigen sessie, zodat we ook de kinderen kunnen stoppen."""
    kind = subprocess.Popen(cmd, cwd=cwd, start_new_session=True)
    try:
        kind.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print("  afgebroken na %d min: deze stap kwam niet terug" % (timeout // 60))
        for sein in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(kind.pid), sein)
            except OSError:
                break
            try:
                kind.wait(timeout=10)
                break
            except subprocess.TimeoutExpired:
                continue
        return False
    return kind.returncode == 0


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
    p.add_argument("--sync", action="store_true", help="één sync-ronde")
    p.add_argument("--drain", action="store_true",
                   help="blijf syncen tot de band leeg is (dit wil je na een nacht)")
    p.add_argument("--rondes", type=int, default=20, help="maximum aantal drain-rondes")
    p.add_argument("--geen-backup", action="store_true")
    p.add_argument("--save-daily", action="store_true", help="tel mee voor de baseline")
    p.add_argument("--address", "-a", default=None)
    a = p.parse_args()

    # Niets meegegeven? Dan pakken we wat er in je instellingen staat, zodat
    # niemand elke keer zijn leeftijd hoeft te typen - en de app en de uursync
    # hem niet hoeven door te geven.
    if not (a.age or a.hrmax):
        cfg = whoop_config.laad()
        a.age, a.hrmax = cfg.get("age"), cfg.get("hrmax")
    if not (a.age or a.hrmax):
        sys.exit("Geen leeftijd of maximale hartslag bekend.\n"
                 "Zet die eenmalig in de app (Whoop.app), of geef --age mee.")
    if not os.path.exists(PLAYGROUND):
        sys.exit("research_playground.py niet gevonden in %s" % RESEARCH)

    adr = a.address or adres()
    basis = [sys.executable, PLAYGROUND]
    if adr:
        basis += ["--address", adr]

    n = 0

    if not a.geen_backup and os.path.exists(DB):
        n += 1
        stap(n, "Back-up van whoop.db")
        # De band wist wat hij verstuurd heeft ("Trim" in zijn eigen log), dus
        # dit bestand is de enige kopie van je geschiedenis.
        doel = os.path.join(os.path.dirname(DB),
                            "whoop-backup-%s.db" % dt.date.today().isoformat())
        try:
            shutil.copy2(DB, doel)
            print("  %s  (%.1f MB)" % (doel, os.path.getsize(doel) / 1048576))
        except OSError as e:
            print("  back-up mislukt: %s" % e)

    if a.drain:
        n += 1
        stap(n, "Historie leegtrekken tot de band bij is")
        draai([sys.executable, os.path.join(HERE, "whoop_drain.py"),
               "--max", str(a.rondes)] + (["--address", adr] if adr else []))
    elif a.sync:
        n += 1
        stap(n, "Historie leegtrekken")
        if not draai(basis + ["--timeout", "600", "sync"], cwd=RESEARCH,
                     timeout=SYNC_TIMEOUT):
            print("  sync gaf een fout - ga toch door met de rest")

    if not a.quick:
        n += 1
        stap(n, "Live meten (%ds) - band moet om je pols" % a.duration)
        if not draai(basis + ["--duration", str(a.duration), "live"], cwd=RESEARCH,
                     timeout=a.duration + 120):
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
    # Zonder --all stuurt whoop_push alleen de laatste dag. Een ronde die een
    # achterstand van dagen wegwerkt, liet de dagen ertussen dan ongemoeid:
    # 2 en 8 september ontbraken zo maandenlang in de app zonder foutmelding.
    # Een week terug is ruim genoeg voor elke inhaalslag, en kost bijna niets
    # extra omdat het inlezen van whoop.db toch al het zware werk is. Grotere
    # gaten vul je met de hand: whoop_push.py --all --since JJJJ-MM-DD
    vanaf = dt.date.today() - dt.timedelta(days=7)
    duw += ["--all", "--since", vanaf.isoformat()]

    n += 1
    stap(n, "Naar Supabase sturen")
    ok = draai(duw)

    print("\n" + "=" * 58)
    print(" Klaar." if ok else " Klaar, maar het versturen ging mis.")
    print(" Tik in de app op 'ververs'.")
    print("=" * 58 + "\n")


if __name__ == "__main__":
    main()
