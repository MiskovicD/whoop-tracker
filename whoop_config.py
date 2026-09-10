#!/usr/bin/env python3
"""
Eén plek voor je persoonlijke instellingen: ~/.whoop-tracker/config.json.

Hiervoor stonden ze verspreid en in drie formaten: het adres van je band in
alarm.json, je leeftijd als shell-regel in auto.conf, en je slaapdoel
hardgecodeerd in de telefoon-app. Twee daarvan kon je alleen via de terminal
zetten, wat het onmogelijk maakte dit uit te delen aan iemand anders.

Alles buiten deze map is code en mag weggegooid worden; alles erin is jouw
staat en moet blijven. Daarom staat dit bestand naast session.json,
baseline.json en alarm.json, en niet naast de scripts.
"""
import json, os, tempfile

# WHOOP_STATE bestaat om te kunnen testen met een schone staat zonder je
# echte instellingen aan te raken. Laat hem leeg voor normaal gebruik.
STATE_DIR = os.environ.get("WHOOP_STATE") or os.path.expanduser("~/.whoop-tracker")
CONFIG = os.path.join(STATE_DIR, "config.json")

# Oude plekken, alleen nog om eenmalig uit over te nemen.
OUD_ALARM = os.path.join(STATE_DIR, "alarm.json")
OUD_CONF = os.path.join(STATE_DIR, "auto.conf")

STANDAARD = {
    "address": None,        # BLE-adres van je band (op macOS een UUID)
    "band_naam": None,      # zoals hij zich adverteert, bv. "WHOOP MISHA"
    "age": None,            # voor de geschatte maximale hartslag
    "hrmax": None,          # gemeten maximum; gaat voor op age
    "sleep_target": 480,    # minuten; 8 uur
}


def _lees(pad):
    try:
        with open(pad) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _schrijf(cfg):
    """Atomair: de app en een lopende sync kunnen tegelijk lezen."""
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile("w", dir=STATE_DIR, delete=False)
    try:
        json.dump(cfg, tmp, indent=1, sort_keys=True)
        tmp.close()
        os.replace(tmp.name, CONFIG)
    except BaseException:
        os.unlink(tmp.name)
        raise


def _neem_oude_over(cfg):
    """Eenmalig: adres uit alarm.json en leeftijd uit auto.conf."""
    veranderd = False
    if not cfg.get("address"):
        adres = _lees(OUD_ALARM).get("address")
        if adres:
            cfg["address"] = adres
            veranderd = True
    if not cfg.get("age") and not cfg.get("hrmax") and os.path.exists(OUD_CONF):
        try:
            for regel in open(OUD_CONF):
                if regel.startswith("LEEFTIJD="):
                    n = regel.split("=", 1)[1].strip()
                    if n.isdigit():
                        cfg["age"] = int(n)
                        veranderd = True
        except OSError:
            pass
    return veranderd


def laad():
    cfg = dict(STANDAARD)
    # Eenmalig overnemen, en alleen zolang dit bestand nog niet bestaat. Doe je
    # dat bij elke lezing, dan zet het oude auto.conf een leeggemaakte leeftijd
    # steeds weer terug en kun je een instelling nooit wissen.
    if not os.path.exists(CONFIG):
        _neem_oude_over(cfg)
        _schrijf(cfg)
        return cfg
    cfg.update(_lees(CONFIG))
    return cfg


def bewaar(**velden):
    """Alleen de meegegeven velden wijzigen; de rest blijft staan."""
    cfg = laad()
    onbekend = set(velden) - set(STANDAARD)
    if onbekend:
        raise KeyError("onbekende instelling: %s" % ", ".join(sorted(onbekend)))
    cfg.update(velden)
    _schrijf(cfg)
    return cfg


def adres(cfg=None):
    return (cfg or laad()).get("address")


def hrmax(cfg=None):
    """Gemeten maximum gaat voor; anders de schatting van Gellish (2007).

    Geen van beide bekend? Dan geven we None terug in plaats van een getal te
    verzinnen - de belastingscore is zonder dit niets waard.
    """
    cfg = cfg or laad()
    if cfg.get("hrmax"):
        return float(cfg["hrmax"])
    if cfg.get("age"):
        return 211.0 - 0.64 * float(cfg["age"])
    return None


def volledig(cfg=None):
    """Genoeg ingevuld om te kunnen syncen?"""
    cfg = cfg or laad()
    return bool(cfg.get("address")) and hrmax(cfg) is not None


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "hrmax":
        h = hrmax()
        print("" if h is None else "%.0f" % h)
    elif len(sys.argv) > 1 and sys.argv[1] == "adres":
        print(adres() or "")
    else:
        print(json.dumps(laad(), indent=1, sort_keys=True))
