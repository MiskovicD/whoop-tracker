#!/usr/bin/env python3
"""
Wekker op de Whoop 4.0 uitlezen, zetten, testen en uitschakelen.

De research-client kan alleen zetten, en dan nog met een unix-epoch. Dit
gereedschap legt alle vier de commando's bloot met leesbare tijden:

    python3 whoop_alarm.py status          # staat er een wekker?
    python3 whoop_alarm.py set 07:30       # eerstvolgende 07:30
    python3 whoop_alarm.py set 2026-08-28T06:45
    python3 whoop_alarm.py test            # laat hem nu trillen
    python3 whoop_alarm.py off             # uitschakelen

Adres wordt onthouden in ~/.whoop-tracker/alarm.json na de eerste keer.
"""
import argparse, asyncio, json, os, struct, sys
from datetime import datetime, timedelta

RESEARCH = os.path.expanduser("~/Desktop/whoop-research")
sys.path.insert(0, RESEARCH)
try:
    from research_playground import WhoopClient, Cmd, REVISION_1, PacketType
except ImportError:
    sys.exit("research_playground.py niet gevonden in %s" % RESEARCH)


class LezendeClient(WhoopClient):
    """
    De decoder geeft voor GET_ALARM_TIME alleen de opcode terug, geen bytes.
    Daarom lezen we op frameniveau mee: alleen zo komen we bij de payload
    waar de wekkertijd in zou moeten staan.
    """
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.alarm_bodies = []
        self.alarm_events = []

    def _on_frame(self, role, frame):
        try:
            if (frame.packet_type == PacketType.COMMAND_RESPONSE
                    and frame.opcode == int(Cmd.GET_ALARM_TIME)):
                self.alarm_bodies.append(bytes(frame.body))
        except Exception:
            pass
        return super()._on_frame(role, frame)

STATE = os.path.expanduser("~/.whoop-tracker/alarm.json")
PLAUSIBEL = (1_600_000_000, 1_900_000_000)     # 2020-2030, om een epoch te herkennen


def bewaar_adres(addr):
    d = os.path.dirname(STATE)
    if not os.path.isdir(d):
        os.makedirs(d, mode=0o700)
    json.dump({"address": addr}, open(STATE, "w"))


def lees_adres():
    try:
        return json.load(open(STATE)).get("address")
    except Exception:
        return None


def zoek_epoch(payload):
    """
    Zoekt een plausibele tijdstempel in de payload. De decoder van de
    research-client laat GET_ALARM_TIME ongemoeid, en de exacte byte-positie
    is niet gedocumenteerd - dus scannen we alle vensters in plaats van een
    offset te gokken. Vinden we niets, dan staat er geen wekker.
    """
    b = bytes(payload)
    for i in range(max(0, len(b) - 3)):
        for fmt in ("<I", ">I"):
            v = struct.unpack_from(fmt, b, i)[0]
            if PLAUSIBEL[0] < v < PLAUSIBEL[1]:
                return v
    return None


def volgende(tijd):
    """'07:30' -> eerstvolgende 07:30. Volledige ISO-tijd mag ook."""
    if "T" in tijd or "-" in tijd:
        return int(datetime.fromisoformat(tijd).timestamp())
    try:
        u, m = (int(x) for x in tijd.split(":"))
    except ValueError:
        sys.exit("tijd niet begrepen: gebruik 07:30 of 2026-08-28T06:45")
    nu = datetime.now()
    doel = nu.replace(hour=u, minute=m, second=0, microsecond=0)
    if doel <= nu:
        doel += timedelta(days=1)
    return int(doel.timestamp())


async def verbind(adres, on_decode=None):
    c = LezendeClient(address=adres, on_decode=on_decode, verbose=False)
    if not await c.connect():
        sys.exit("geen verbinding met de band - ligt hij binnen bereik en is hij opgeladen?")
    if c.address:
        bewaar_adres(c.address)
    return c


async def doe(actie, adres, epoch=None):
    antwoorden = []
    c = await verbind(adres, on_decode=lambda d: antwoorden.append(d))
    c.alarm_events = []
    try:
        if actie == "status":
            await c.send(Cmd.GET_ALARM_TIME, bytes([REVISION_1]))
        elif actie == "set":
            await c.send(Cmd.SET_ALARM_TIME,
                         b"\x01" + struct.pack("<I", epoch) + b"\x00\x00")
            await asyncio.sleep(0.6)
            await c.send(Cmd.GET_ALARM_TIME, bytes([REVISION_1]))   # meteen terugcontroleren
        elif actie == "off":
            await c.send(Cmd.DISABLE_ALARM, b"\x00")
            await asyncio.sleep(0.6)
            await c.send(Cmd.GET_ALARM_TIME, bytes([REVISION_1]))
        elif actie == "test":
            await c.send(Cmd.RUN_ALARM, b"\x00")
        await asyncio.sleep(2.5)
    finally:
        bodies = list(c.alarm_bodies)
        await c.disconnect()
    return antwoorden, bodies


def toon_status(antwoorden, bodies):
    events = [d.get("event") for d in antwoorden if d.get("kind") == "event"
              and "ALARM" in str(d.get("event", ""))]

    if not bodies:
        print("  geen antwoord op GET_ALARM_TIME ontvangen")
    else:
        ep = zoek_epoch(bodies[-1])
        if ep:
            print("  wekker staat op %s"
                  % datetime.fromtimestamp(ep).strftime("%d-%m-%Y %H:%M"))
        else:
            print("  geen wekker ingesteld")
        print("  payload: %s" % bodies[-1].hex())
    for e in events:
        print("  event: %s" % e)


def main():
    p = argparse.ArgumentParser(description="Wekker op de Whoop 4.0")
    p.add_argument("actie", choices=["status", "set", "off", "test"])
    p.add_argument("tijd", nargs="?", help="bij 'set': 07:30 of 2026-08-28T06:45")
    p.add_argument("--address", "-a", default=None)
    a = p.parse_args()

    adres = a.address or lees_adres()
    if a.actie == "set" and not a.tijd:
        sys.exit("geef een tijd mee, bijvoorbeeld: whoop_alarm.py set 07:30")

    epoch = volgende(a.tijd) if a.actie == "set" else None
    if epoch:
        print("Zetten op %s" % datetime.fromtimestamp(epoch).strftime("%d-%m-%Y %H:%M"))

    antwoorden, bodies = asyncio.run(doe(a.actie, adres, epoch))

    if a.actie == "test":
        print("  RUN_ALARM verstuurd - voelde je hem trillen?")
    else:
        toon_status(antwoorden, bodies)


if __name__ == "__main__":
    main()
