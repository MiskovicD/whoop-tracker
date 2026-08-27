#!/usr/bin/env python3
"""
Standaard-hartslagprofiel (BLE 0x180D) op de Whoop 4.0.

Werkt dat, dan ziet elke gewone hartslag-app op je telefoon je band als een
doodgewone hartslagband - Zwift, Peloton, een losse HR-monitor, wat je wilt.
Geen eigen app nodig.

    python3 whoop_hr.py services      # welke GATT-services biedt de band aan?
    python3 whoop_hr.py on            # zet broadcast aan
    python3 whoop_hr.py off
    python3 whoop_hr.py listen        # lees zelf even mee op 0x180D
    python3 whoop_hr.py battery       # betrouwbare accustand via 0x2A19

Draaien via:  uv run --no-project --with bleak python whoop_hr.py services
"""
import argparse, asyncio, json, os, sys

RESEARCH = os.path.expanduser("~/Desktop/whoop-research")
sys.path.insert(0, RESEARCH)
from research_playground import WhoopClient, Cmd, REVISION_1, PacketType, \
                                HR_SERVICE_UUID, HR_MEASUREMENT_UUID
from bleak import BleakClient, BleakScanner

STATE = os.path.expanduser("~/.whoop-tracker/alarm.json")
BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"   # standaard Battery Level
DEVINFO_UUID = "00002a29-0000-1000-8000-00805f9b34fb"   # fabrikantnaam


def lees_adres():
    try:
        return json.load(open(STATE)).get("address")
    except Exception:
        return None


async def zoek():
    print("Scannen...")
    devs = await BleakScanner.discover(timeout=12.0)
    for d in devs:
        if d.name and "whoop" in d.name.lower():
            print("  gevonden: %s (%s)" % (d.name, d.address))
            return d.address
    sys.exit("geen WHOOP gevonden - opgeladen en binnen bereik?")


async def toon_services(adres):
    async with BleakClient(adres) as c:
        print("Verbonden. GATT-services:\n")
        heeft_hr = False
        for s in c.services:
            kort = s.uuid.split("-")[0]
            std = s.uuid.lower() == HR_SERVICE_UUID.lower()
            print("  %s  %s%s" % (kort, s.description or "", "   <-- STANDAARD HARTSLAG" if std else ""))
            for ch in s.characteristics:
                props = ",".join(ch.properties)
                mark = ""
                if ch.uuid.lower() == HR_MEASUREMENT_UUID.lower():
                    heeft_hr = True
                    mark = "   <-- 0x2A37 hartslagmeting"
                print("      %s  [%s]%s" % (ch.uuid.split("-")[0], props, mark))
            print()
        print("=" * 58)
        if heeft_hr:
            print("De band biedt het STANDAARD hartslagprofiel aan (0x180D/0x2A37).")
            print("Elke gewone hartslag-app op je telefoon kan hem dus gebruiken,")
            print("zolang je Mac niet verbonden is - de band bindt aan een apparaat tegelijk.")
        else:
            print("Geen standaard hartslagprofiel gevonden. Probeer eerst:  whoop_hr.py on")


async def toggle(adres, aan, twee_byte):
    antwoorden = []
    c = WhoopClient(address=adres, on_decode=lambda d: antwoorden.append(d), verbose=False)
    if not await c.connect():
        sys.exit("geen verbinding")
    try:
        payload = (bytes([REVISION_1, 0x01 if aan else 0x00]) if twee_byte
                   else bytes([0x01 if aan else 0x00]))
        print("Versturen: opcode 0x%02X payload %s" % (int(Cmd.TOGGLE_GENERIC_HR_PROFILE),
                                                      payload.hex()))
        await c.send(Cmd.TOGGLE_GENERIC_HR_PROFILE, payload)
        await asyncio.sleep(2.0)
    finally:
        await c.disconnect()

    bevestigd = [d for d in antwoorden
                 if d.get("opcode") == "TOGGLE_GENERIC_HR_PROFILE"]
    if bevestigd:
        print("  band bevestigde het commando")
    else:
        print("  geen bevestiging gezien. Probeer de andere payloadvorm met --two-byte")
        print("  (of andersom als je die al gebruikte)")
    print("\nControleer nu met:  whoop_hr.py services")


async def accu(adres):
    """
    De standaard Battery Service geeft één byte, 0-100. Dat is iets heel anders
    dan de propriëtaire battery-events, die de decoder zelf 'unreliable' noemt
    en die binnen tien minuten van 23 naar 31 procent sprongen.
    """
    async with BleakClient(adres) as c:
        raw = await c.read_gatt_char(BATTERY_UUID)
        print("  accu: %d %%   (0x2A19, standaardprofiel)" % raw[0])
        try:
            naam = await c.read_gatt_char(DEVINFO_UUID)
            print("  fabrikant: %s" % naam.decode(errors="replace").strip("\x00"))
        except Exception:
            pass


async def luister(adres, seconden):
    """Leest zelf mee op het standaardprofiel - zo weet je of het echt stroomt."""
    n = [0]

    def cb(_h, data):
        # 0x2A37: [flags][hr] - bit0 van flags zegt of hr 8 of 16 bits is
        flags = data[0]
        hr = int.from_bytes(data[1:3], "little") if flags & 1 else data[1]
        n[0] += 1
        print("  %3d  %d bpm" % (n[0], hr))

    async with BleakClient(adres) as c:
        await c.start_notify(HR_MEASUREMENT_UUID, cb)
        print("Luisteren op 0x2A37 (%ds). Band om je pols houden.\n" % seconden)
        await asyncio.sleep(seconden)
        await c.stop_notify(HR_MEASUREMENT_UUID)
    print("\n%d metingen ontvangen." % n[0] if n[0] else
          "\nNiets ontvangen - profiel staat waarschijnlijk uit.")


def main():
    p = argparse.ArgumentParser(description="Standaard hartslagprofiel op de Whoop 4.0")
    p.add_argument("actie", choices=["services", "on", "off", "listen", "battery"])
    p.add_argument("--address", "-a", default=None)
    p.add_argument("--two-byte", action="store_true",
                   help="payload als [01,01] i.p.v. [01] - de twee conventies in dit protocol")
    p.add_argument("--seconds", type=int, default=20)
    a = p.parse_args()

    adres = a.address or lees_adres()
    if not adres:
        adres = asyncio.run(zoek())

    if a.actie == "services":
        asyncio.run(toon_services(adres))
    elif a.actie == "battery":
        asyncio.run(accu(adres))
    elif a.actie == "listen":
        asyncio.run(luister(adres, a.seconds))
    else:
        asyncio.run(toggle(adres, a.actie == "on", a.two_byte))


if __name__ == "__main__":
    main()
