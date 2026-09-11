#!/usr/bin/env python3
"""
Leegtrekken met een knop, zonder terminal.

Start een klein servertje op localhost en opent je browser. De knop doet
`whoop_auto.sh inhalen` - daar zitten de vergrendeling, de bewaker en het
versturen naar Supabase al in, dus de logica staat op een plek en er zijn
alleen twee manieren om hem te starten: de uursync en deze knop.

Waarom een pagina en geen venster: de Tk die bij Apple's Python zit is te oud
voor macOS 15 en valt om met "macOS 15 (1507) or later required". Een pagina
werkt met alleen de standaardbibliotheek.

    python3 whoop_app.py            # opent je browser
    python3 whoop_app.py --geen-browser
"""
import argparse, json, os, re, signal, socket, sqlite3, stat, struct, subprocess
import sys, threading, time, urllib.error, urllib.request, webbrowser
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import whoop_config

HERE = os.path.dirname(os.path.abspath(__file__))
AUTO = os.path.join(HERE, "whoop_auto.sh")
RESEARCH = os.environ.get("WHOOP_RESEARCH") or os.path.expanduser("~/whoop-research")
DB = os.path.join(RESEARCH, "whoop.db")
LOCK = os.path.join(whoop_config.STATE_DIR, "auto.lock")
SESSIE = os.path.join(whoop_config.STATE_DIR, "session.json")
PLAYGROUND = os.path.join(RESEARCH, "research_playground.py")
PLIST = os.path.expanduser("~/Library/LaunchAgents/whoop-auto.plist")
STIL_AF = 90          # seconden zonder pagina die meekijkt = servertje sluit
SCAN_MAX = 60         # seconden; een scan duurt normaal een tiental


# ------------------------------------------------------- account (Supabase)

SB_URL = "https://zxlythycfgpqwpquuswg.supabase.co"

# Supabase antwoordt in het Engels; de rest van dit venster is Nederlands.
# Onbekende meldingen laten we staan - liever Engels dan verzwegen.
VERTAALD = {
    "Invalid login credentials":
        "E-mail of wachtwoord klopt niet.",
    "User already registered":
        "Dit e-mailadres heeft al een account. Log in in plaats van registreren.",
    "Unable to validate email address: invalid format":
        "Dat is geen geldig e-mailadres.",
    "Password should be at least 6 characters":
        "Kies een wachtwoord van minstens 6 tekens.",
    "Email signups are disabled":
        "Registreren staat uit in dit Supabase-project.",
    "Signups not allowed for this instance":
        "Registreren staat uit in dit Supabase-project.",
}


def anon_key():
    """De publieke sleutel staat in de telefoon-app; RLS doet het echte werk."""
    for naam in ("app/index.html",):
        pad = os.path.join(HERE, naam)
        try:
            for regel in open(pad, encoding="utf-8", errors="ignore"):
                if "SB_ANON" in regel and "eyJ" in regel:
                    return regel.split('"')[1]
        except OSError:
            pass
    return None


def _sb(pad, lijf):
    key = anon_key()
    if not key:
        raise RuntimeError("anon-sleutel niet gevonden in app/index.html")
    r = urllib.request.Request(SB_URL + pad, json.dumps(lijf).encode(),
                               {"apikey": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            d = json.load(e)
        except Exception:
            d = {}
        reden = (d.get("error_description") or d.get("msg") or d.get("error")
                 or d.get("message") or "")
        raise RuntimeError(VERTAALD.get(reden.strip(), reden)
                           or "aanmelden mislukt (HTTP %s)" % e.code)
    except urllib.error.URLError as e:
        raise RuntimeError("geen verbinding: %s" % e.reason)


def bewaar_sessie(d):
    s = {"refresh_token": d["refresh_token"], "access_token": d["access_token"],
         "user_id": (d.get("user") or d)["id"]}
    e = (d.get("user") or {}).get("email")
    if e:
        s["email"] = e
    os.makedirs(os.path.dirname(SESSIE), mode=0o700, exist_ok=True)
    with open(SESSIE, "w") as f:
        json.dump(s, f)
    os.chmod(SESSIE, stat.S_IRUSR | stat.S_IWUSR)   # alleen jij mag erbij
    return s


def sessie():
    try:
        with open(SESSIE) as f:
            s = json.load(f)
    except (OSError, ValueError):
        return None
    # Sessies van voor deze versie hebben geen e-mail opgeslagen. Zonder dat
    # kan het instellingenpaneel niet zeggen onder welk account je kijkt, en
    # dat is juist de enige verklaring die klopt als je Mac wel pusht maar de
    # telefoon-app leeg blijft.
    if s.get("email") or not s.get("refresh_token"):
        return s
    # Via de refresh-token, niet via /auth/v1/user met het access-token: dat
    # verloopt na een uur, dus bij een sessie van gisteren mislukt dat altijd.
    # Een verversing geeft het e-mailadres meteen mee.
    try:
        d = _sb("/auth/v1/token?grant_type=refresh_token",
                {"refresh_token": s["refresh_token"]})
        if d.get("access_token"):
            s = bewaar_sessie(d)
    except Exception:
        pass                    # niet kunnen ophalen mag niets blokkeren
    return s


def aanmelden(email, wachtwoord, nieuw=False):
    pad = "/auth/v1/signup" if nieuw else "/auth/v1/token?grant_type=password"
    d = _sb(pad, {"email": email, "password": wachtwoord})
    if not d.get("access_token"):
        # Staat e-mailbevestiging aan, dan komt er geen token terug.
        raise RuntimeError("Account aangemaakt. Bevestig eerst de mail, "
                           "dan kun je hier inloggen.")
    return bewaar_sessie(d)


# ------------------------------------------------------- uursync (launchd)

def uursync_aan():
    return os.path.exists(PLIST)


def zet_uursync(aan):
    opdracht = "install" if aan else "uninstall"
    r = subprocess.run(["/bin/bash", AUTO, opdracht], cwd=HERE,
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


# ---------------------------------------------------------------- database

def _db(fn, standaard=None):
    """Altijd read-only, zodat een lopende sync hier niet op stuk kan lopen."""
    if not os.path.exists(DB):
        return standaard
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        try:
            return fn(con)
        finally:
            con.close()
    except sqlite3.Error:
        return standaard


def laatste_meting():
    """Nieuwste tijdstempel uit de historie.

    Alleen de laatste frames, niet de hele tabel: de drain schrijft
    chronologisch, dus het nieuwste staat achteraan. 0,01 s in plaats van
    1,1 s, met dezelfde uitkomst - en dat merk je als de pagina elke paar
    seconden bijwerkt.
    """
    def q(con):
        top = 0
        for (hx,) in con.execute("select hex from frames where packet_type=47"
                                 " order by id desc limit 4000"):
            b = bytes.fromhex(hx)[4:-4]
            if len(b) < 72 or b[0] != 0x2F or b[2] not in (0x05, 0x07):
                continue
            ts = struct.unpack_from("<I", b, 7)[0]
            if 1_600_000_000 < ts < 2_000_000_000 and ts > top:
                top = ts
        return top or None
    return _db(q)


def accu():
    return _db(lambda c: (c.execute("select battery from hello where battery is not null"
                                    " order by t desc limit 1").fetchone() or [None])[0])


def laatste_verbinding():
    return _db(lambda c: (c.execute("select t from frames order by id desc limit 1")
                          .fetchone() or [None])[0])


# ---------------------------------------------------------------- de klus

class Klus:
    def __init__(self):
        self.proc = None
        self.regels = []
        self.slot = threading.Lock()
        self.code = None

    @property
    def bezig(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.bezig:
            return False, "er loopt hier al een sync"
        if os.path.isdir(LOCK):
            return False, "er loopt al een sync buiten dit venster"
        if not os.path.exists(AUTO):
            return False, "whoop_auto.sh niet gevonden in %s" % HERE
        with self.slot:
            self.regels = []
            self.code = None
        omgeving = dict(os.environ, PYTHONUNBUFFERED="1")
        # caffeinate erbij: beide vastlopers tot nu toe waren een BLE-aanroep
        # die een slaapstand niet overleefde.
        self.proc = subprocess.Popen(
            ["/usr/bin/caffeinate", "-is", "/bin/bash", AUTO, "inhalen"],
            cwd=HERE, env=omgeving, start_new_session=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
        threading.Thread(target=self._lezen, args=(self.proc,), daemon=True).start()
        return True, "gestart"

    def _lezen(self, p):
        try:
            for regel in p.stdout:
                with self.slot:
                    self.regels.append(regel.rstrip("\n"))
        finally:
            self.code = p.wait()
            with self.slot:
                self.regels.append("")
                self.regels.append("Klaar." if self.code == 0 else
                                   "Gestopt (code %s). Alles wat binnen is, is bewaard."
                                   % self.code)

    def stop(self):
        if not self.bezig:
            return False
        try:
            # Hetzelfde sein als Ctrl-C: de drain vangt dat op en ruimt zijn
            # eigen sync-client op. SIGTERM zou die laten zweven.
            os.killpg(os.getpgid(self.proc.pid), signal.SIGINT)
            return True
        except OSError:
            return False

    def sinds(self, n):
        with self.slot:
            return self.regels[n:], len(self.regels)


class Scan:
    """Zoekt banden in de buurt. Aparte klus, want dit is geen sync."""

    def __init__(self):
        self.bezig = False
        self.gevonden = []
        self.fout = None

    def start(self):
        if self.bezig:
            return False, "de scan loopt al"
        if klus.bezig or os.path.isdir(LOCK):
            return False, "er loopt een sync; wacht tot die klaar is"
        if not os.path.exists(PLAYGROUND):
            return False, ("research_playground.py niet gevonden in %s - "
                           "draai ./installeer.sh" % RESEARCH)
        self.bezig, self.gevonden, self.fout = True, [], None
        threading.Thread(target=self._draai, daemon=True).start()
        return True, "zoeken"

    def _draai(self):
        try:
            r = subprocess.run(
                ["uv", "run", "--no-project", "--with", "bleak", "python",
                 PLAYGROUND, "scan"],
                cwd=RESEARCH, capture_output=True, text=True, timeout=SCAN_MAX,
                env=dict(os.environ, PYTHONUNBUFFERED="1"))
            uit = ((r.stdout or "") + (r.stderr or "")).strip()
            # De client print regels als:  found WHOOP MISHA @ <uuid>
            for naam, adr in re.findall(r"found (.+?) @ (\S+)", uit):
                naam = naam.strip()
                self.gevonden.append({"naam": naam, "address": adr,
                                      "whoop": "whoop" in naam.lower()})
            if self.gevonden:
                pass
            elif "No WHOOP found" in uit or "Scanning" in uit:
                # Hij heeft echt gezocht en niets gezien. Een band die al met
                # iets verbonden is adverteert niet, dus dit is meestal geen
                # storing maar een band die niet in koppelstand staat.
                self.fout = ("Gezocht, maar geen band gezien. Doe hem van je pols en "
                             "tik twee keer op het scherm; dan maakt hij zich even "
                             "vindbaar. Een band die al verbonden is, met je Mac of je "
                             "telefoon, laat zich niet vinden.")
            elif not uit:
                # Geen enkele regel, ook geen foutregel: de client is afgebroken
                # voordat hij kon zoeken. Dat is vrijwel altijd de Bluetooth-
                # toestemming van macOS, die bij een ongesigneerd programma
                # geweigerd wordt zonder dat er iets gevraagd wordt.
                self.fout = ("De scan brak meteen af, zonder ook maar te zoeken. "
                             "Dat is vrijwel altijd de Bluetooth-toestemming: kijk bij "
                             "Systeeminstellingen \u2192 Privacy en beveiliging \u2192 "
                             "Bluetooth of Whoop daar aan staat. Lukt dat niet, dan werkt "
                             "leegtrekken wel via de uursync of de terminal.")
            else:
                # Wél uitvoer, maar niets bruikbaars: laat zien wat hij zei in
                # plaats van er een verklaring bij te verzinnen.
                self.fout = "De scan gaf geen band terug. Dit zei hij:\n" + uit[-400:]
        except subprocess.TimeoutExpired:
            self.fout = "De scan liep vast na %d seconden." % SCAN_MAX
        except OSError as e:
            self.fout = "uv niet gevonden (%s). Draai eerst ./installeer.sh" % e
        finally:
            self.bezig = False


klus = Klus()
scan = Scan()
laatst_gezien = [time.time()]


def setup_stand():
    """Wat is er al ingesteld, en wat nog niet."""
    cfg = whoop_config.laad()
    s = sessie()
    return {
        "account": {"klaar": bool(s), "email": (s or {}).get("email")},
        "band": {"klaar": bool(cfg.get("address")), "naam": cfg.get("band_naam"),
                 "address": cfg.get("address")},
        "jij": {"klaar": whoop_config.hrmax(cfg) is not None,
                "age": cfg.get("age"), "hrmax": cfg.get("hrmax"),
                "sleep_target": cfg.get("sleep_target"),
                "hrmax_berekend": whoop_config.hrmax(cfg)},
        "uursync": {"aan": uursync_aan()},
        "compleet": bool(s) and whoop_config.volledig(cfg),
    }


# ---------------------------------------------------------------- pagina

PAGINA = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<title>Whoop</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0b0c0e;--kaart:#16181c;--kaart2:#1d2025;--lijn:#26292e;--ink:#f2f3f5;
  --grijs:#8b9199;--groen:#28c76f;--geel:#e5b83b;--rood:#ea4f4f;--blauw:#4a8cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}
.wrap{max-width:660px;margin:0 auto;padding:28px 18px 44px}
h1{font-size:13px;letter-spacing:.14em;margin:0 0 3px}
.sub{color:var(--grijs);font-size:12.5px;margin:0 0 18px}
.kaart{background:var(--kaart);border:1px solid var(--lijn);border-radius:14px;
  padding:16px;margin:0 0 12px}
.tegels{display:grid;grid-template-columns:repeat(3,1fr);padding:16px}
.tegels>div+div{border-left:1px solid var(--lijn);padding-left:16px}
.kop{color:var(--grijs);font-size:9.5px;letter-spacing:.09em;font-weight:600}
.waarde{font-size:20px;font-weight:600;margin-top:3px}
h2{font-size:13px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
h2 .nr{width:19px;height:19px;border-radius:50%;background:var(--kaart2);
  color:var(--grijs);font-size:11px;display:grid;place-items:center;flex:none}
h2.klaar .nr{background:var(--groen);color:#000}
.uitleg{color:var(--grijs);font-size:12.5px;margin:0 0 12px}
input,select{width:100%;padding:11px 12px;margin:0 0 8px;border-radius:10px;
  border:1px solid var(--lijn);background:var(--kaart2);color:var(--ink);font-size:14px}
input:focus,select:focus{outline:none;border-color:var(--blauw)}
.rij{display:flex;gap:8px}
.rij>*{flex:1;margin-bottom:8px}
button{width:100%;padding:12px;border:0;border-radius:11px;background:var(--ink);
  color:#000;font-size:14px;font-weight:600;cursor:pointer}
button.stil{background:var(--kaart2);color:var(--ink);border:1px solid var(--lijn)}
button.stop{background:var(--rood);color:#fff}
button:disabled{background:#2a2d33;color:var(--grijs);cursor:default}
button.groot{padding:15px;font-size:15px}
.fout{color:var(--rood);font-size:12.5px;min-height:17px;margin:4px 0 0;white-space:pre-wrap}
.fout.ok{color:var(--groen)}
.hint{color:var(--grijs);font-size:12.5px;text-align:center;margin:9px 0 16px;min-height:34px}
pre{background:var(--kaart);border:1px solid var(--lijn);border-radius:14px;margin:0;
  padding:13px 15px;height:280px;overflow:auto;white-space:pre-wrap;
  font:12px/1.65 "SF Mono",Menlo,monospace;color:var(--grijs)}
.band{display:flex;justify-content:space-between;align-items:center;gap:10px;
  padding:10px 12px;border:1px solid var(--lijn);border-radius:10px;margin:0 0 7px;
  background:var(--kaart2);font-size:13.5px}
.band button{width:auto;padding:7px 13px;font-size:12.5px}
.band .adr{color:var(--grijs);font-size:11px;font-family:"SF Mono",Menlo,monospace}
.klaarregel{display:flex;justify-content:space-between;align-items:center;gap:10px;
  font-size:13.5px}
.klaarregel button{width:auto;padding:7px 13px;font-size:12.5px}
.schakel{display:flex;justify-content:space-between;align-items:center;gap:12px;
  padding:11px 0;border-top:1px solid var(--lijn);font-size:13.5px}
.schakel:first-of-type{border-top:0}
.schakel button{width:auto;padding:7px 14px;font-size:12.5px}
.voet{text-align:center;margin-top:16px}
.voet button{width:auto;background:none;color:var(--grijs);font-weight:400;
  font-size:12.5px;text-decoration:underline;padding:6px}
[hidden]{display:none!important}
</style></head><body><div class="wrap">
<h1>WHOOP</h1><p class="sub" id="sub">&nbsp;</p>

<div id="setup" hidden>
  <div class="kaart">
    <h2 id="kAccount"><span class="nr">1</span> Je account</h2>
    <p class="uitleg" id="uAccount">Hiermee komen je metingen in de telefoon-app.
      Alleen jij ziet je eigen gegevens.</p>
    <div id="accountForm">
      <input id="email" type="email" placeholder="e-mail" autocomplete="username"
             autocapitalize="none" spellcheck="false">
      <input id="pw" type="password" placeholder="wachtwoord" autocomplete="current-password">
      <div class="rij">
        <button id="bInloggen">Inloggen</button>
        <button id="bRegistreren" class="stil">Account maken</button>
      </div>
      <p class="fout" id="fAccount"></p>
    </div>
    <div id="accountKlaar" hidden>
      <div class="klaarregel"><span id="accountEmail"></span>
        <button class="stil" id="bAfmelden">Afmelden</button></div>
    </div>
  </div>

  <div class="kaart">
    <h2 id="kBand"><span class="nr">2</span> Je band</h2>
    <p class="uitleg">Doe je band van je pols en tik twee keer op het scherm, dan
      is hij vindbaar. Ligt hij aan de lader, dan werkt dat ook.</p>
    <div id="bandKlaar" hidden>
      <div class="klaarregel"><span><b id="bandNaam"></b>
        <span class="adr" id="bandAdres"></span></span>
        <button class="stil" id="bAnders">Andere band</button></div>
    </div>
    <div id="bandZoek">
      <button id="bScan">Zoek mijn band</button>
      <div id="bandLijst" style="margin-top:9px"></div>
      <p class="fout" id="fBand"></p>
    </div>
  </div>

  <div class="kaart">
    <h2 id="kJij"><span class="nr">3</span> Jij</h2>
    <p class="uitleg">Je maximale hartslag bepaalt je belastingscore. Ken je hem
      gemeten, vul die dan in &mdash; dat is altijd nauwkeuriger dan een formule.</p>
    <div class="rij">
      <div><input id="age" type="number" min="10" max="100" placeholder="leeftijd"></div>
      <div><input id="hrmax" type="number" min="120" max="230" placeholder="of gemeten HRmax"></div>
    </div>
    <input id="slaap" type="number" min="240" max="720" step="15" placeholder="slaapdoel in minuten (480 = 8 uur)">
    <button id="bJij">Opslaan</button>
    <p class="fout" id="fJij"></p>
  </div>
</div>

<div id="normaal" hidden>
  <div class="kaart" style="padding:0"><div class="tegels">
    <div><div class="kop">BIJGEWERKT TOT</div><div class="waarde" id="tot">&mdash;</div></div>
    <div><div class="kop">ACHTERSTAND</div><div class="waarde" id="achter">&mdash;</div></div>
    <div><div class="kop">ACCU BAND</div><div class="waarde" id="accu">&mdash;</div></div>
  </div></div>
  <button id="knop" class="groot">LEEGTREKKEN</button>
  <p class="hint" id="hint">&nbsp;</p>
  <pre id="log"></pre>
  <div class="voet"><button id="bInst">Instellingen</button></div>
  <div class="kaart" id="instellingen" hidden style="margin-top:12px">
    <div class="schakel"><span>Elk uur automatisch leegtrekken</span>
      <button class="stil" id="bUursync">&mdash;</button></div>
    <div class="schakel"><span id="instAccount"></span>
      <button class="stil" id="bAfmelden2">Afmelden</button></div>
    <div class="schakel"><span id="instBand"></span>
      <button class="stil" id="bBandOpnieuw">Wijzigen</button></div>
    <div class="schakel"><span id="instJij"></span>
      <button class="stil" id="bJijOpnieuw">Wijzigen</button></div>
    <p class="fout" id="fInst"></p>
  </div>
</div>
</div><script>
const $ = s => document.querySelector(s);
let bezig = false, n = 0, opzet = null, scanTimer = null;

const post = async (pad, lijf) => (await fetch(pad, {method:"POST",
  headers:{"Content-Type":"application/json"}, body: JSON.stringify(lijf || {})})).json();
const zeg = (el, tekst, ok) => { const e = $(el); e.textContent = tekst || "";
  e.classList.toggle("ok", !!ok); };

/* ---------------- instelwizard ---------------- */

async function haalOpzet(){
  opzet = await (await fetch("/setup")).json();
  const a = opzet.account, b = opzet.band, j = opzet.jij;

  $("#kAccount").classList.toggle("klaar", a.klaar);
  $("#accountForm").hidden = a.klaar;
  $("#accountKlaar").hidden = !a.klaar;
  $("#accountEmail").textContent = a.email || "ingelogd";

  $("#kBand").classList.toggle("klaar", b.klaar);
  $("#bandKlaar").hidden = !b.klaar;
  $("#bandZoek").hidden = b.klaar;
  $("#bandNaam").textContent = b.naam || "je band";
  $("#bandAdres").textContent = b.address || "";

  $("#kJij").classList.toggle("klaar", j.klaar);
  if(document.activeElement !== $("#age")) $("#age").value = j.age || "";
  if(document.activeElement !== $("#hrmax")) $("#hrmax").value = j.hrmax || "";
  if(document.activeElement !== $("#slaap")) $("#slaap").value = j.sleep_target || "";

  $("#setup").hidden = opzet.compleet;
  $("#normaal").hidden = !opzet.compleet;

  $("#bUursync").textContent = opzet.uursync.aan ? "AAN" : "UIT";
  $("#bUursync").style.color = opzet.uursync.aan ? "var(--groen)" : "";
  $("#instAccount").textContent = a.email || "ingelogd";
  $("#instBand").textContent = (b.naam || "band") + " " + (b.address || "");
  $("#instJij").textContent = j.hrmax ? ("HRmax " + j.hrmax + " (gemeten)")
    : (j.age ? (j.age + " jaar \\u2192 HRmax " + Math.round(j.hrmax_berekend)) : "onbekend");
}

async function meld(nieuw){
  zeg("#fAccount", "Bezig\\u2026");
  const d = await post("/aanmelden", {email:$("#email").value, wachtwoord:$("#pw").value, nieuw});
  if(!d.ok) return zeg("#fAccount", d.fout);
  zeg("#fAccount", ""); $("#pw").value = "";
  haalOpzet();
}
$("#bInloggen").onclick = () => meld(false);
$("#bRegistreren").onclick = () => meld(true);
$("#bAfmelden").onclick = $("#bAfmelden2").onclick =
  async () => { await post("/afmelden"); haalOpzet(); };

$("#bAnders").onclick = $("#bBandOpnieuw").onclick = async () => {
  await post("/instellingen", {address:null, band_naam:null});
  $("#instellingen").hidden = true; haalOpzet();
};
$("#bJijOpnieuw").onclick = async () => {
  await post("/instellingen", {age:null, hrmax:null});
  $("#instellingen").hidden = true; haalOpzet();
};

$("#bScan").onclick = async () => {
  $("#bScan").disabled = true; $("#bScan").textContent = "Zoeken\\u2026";
  $("#bandLijst").innerHTML = ""; zeg("#fBand", "");
  const d = await post("/scan");
  if(!d.ok){ zeg("#fBand", d.fout); klaarMetScannen(); return; }
  scanTimer = setInterval(async () => {
    const r = await (await fetch("/scan")).json();
    if(r.bezig) return;
    clearInterval(scanTimer); scanTimer = null;
    if(r.fout) zeg("#fBand", r.fout);
    $("#bandLijst").innerHTML = r.gevonden.map((b, i) =>
      `<div class="band"><span><b>${b.naam}</b>${b.whoop ? "" : " (geen Whoop?)"}
        <br><span class="adr">${b.address}</span></span>
        <button data-i="${i}">Dit is mijn band</button></div>`).join("");
    $("#bandLijst").querySelectorAll("button").forEach(kn => kn.onclick = async () => {
      const b = r.gevonden[+kn.dataset.i];
      await post("/instellingen", {address:b.address, band_naam:b.naam});
      haalOpzet();
    });
    klaarMetScannen();
  }, 1200);
};
function klaarMetScannen(){ $("#bScan").disabled = false; $("#bScan").textContent = "Zoek mijn band"; }

$("#bJij").onclick = async () => {
  const lijf = {sleep_target: $("#slaap").value || 480};
  if($("#hrmax").value) lijf.hrmax = $("#hrmax").value;
  else if($("#age").value) lijf.age = $("#age").value;
  else return zeg("#fJij", "Vul je leeftijd in, of je gemeten maximum");
  const d = await post("/instellingen", lijf);
  zeg("#fJij", d.ok ? "Opgeslagen" : d.fout, d.ok);
  haalOpzet();
};

$("#bInst").onclick = () => { $("#instellingen").hidden = !$("#instellingen").hidden; };
$("#bUursync").onclick = async () => {
  $("#bUursync").disabled = true;
  const d = await post("/uursync", {aan: !opzet.uursync.aan});
  $("#bUursync").disabled = false;
  if(!d.ok) zeg("#fInst", d.fout); else zeg("#fInst", "");
  haalOpzet();
};

/* ---------------- leegtrekken ---------------- */

$("#knop").addEventListener("click", async () => {
  $("#knop").disabled = true;
  const d = await post(bezig ? "/stop" : "/start");
  if(d.fout) $("#hint").textContent = d.fout;
  ververs();
});

function kleur(sec){ return sec < 900 ? "var(--groen)" : sec < 21600 ? "var(--geel)" : "var(--rood)"; }
function duur(sec){
  const u = Math.floor(sec/3600), m = Math.floor(sec%3600/60);
  return u ? u + " u " + String(m).padStart(2,"0") + " min" : m + " min";
}

async function ververs(){
  let d;
  try { d = await (await fetch("/status")).json(); }
  catch(e){ $("#hint").textContent = "Het programma is afgesloten."; return; }
  bezig = d.bezig;

  $("#tot").textContent = d.tot || "\\u2014";
  if(d.achter == null){ $("#achter").textContent = "\\u2014"; }
  else if(d.achter < 900){ $("#achter").textContent = "bij"; $("#achter").style.color = "var(--groen)"; }
  else { $("#achter").textContent = duur(d.achter); $("#achter").style.color = kleur(d.achter); }
  $("#accu").textContent = d.accu == null ? "\\u2014" : Math.round(d.accu) + "%";
  if(d.accu != null) $("#accu").style.color = d.accu < 15 ? "var(--rood)"
    : d.accu < 30 ? "var(--geel)" : "var(--groen)";
  $("#sub").textContent = "laatste contact met de band: " + (d.contact || "nog nooit");

  const k = $("#knop");
  k.disabled = false;
  if(bezig){
    k.textContent = "STOPPEN"; k.className = "groot stop";
    $("#hint").textContent = "Bezig. Alles wat binnen is blijft staan, ook als je stopt.";
  } else if(d.bezet){
    k.textContent = "ER LOOPT AL EEN SYNC"; k.className = "groot"; k.disabled = true;
    $("#hint").textContent = "De uursync of een terminal is bezig. Zodra die klaar is, kun je hier klikken.";
  } else {
    k.textContent = "LEEGTREKKEN"; k.className = "groot";
    $("#hint").textContent = "Draag je band en houd hem in de buurt. Je Mac blijft wakker zolang dit loopt.";
  }
}

async function logje(){
  try{
    const d = await (await fetch("/log?sinds=" + n)).json();
    if(d.reset){ $("#log").textContent = ""; n = 0; }
    if(d.regels.length){
      const p = $("#log");
      const onder = p.scrollTop + p.clientHeight >= p.scrollHeight - 30;
      p.textContent += d.regels.join("\\n") + "\\n";
      n = d.n;
      if(onder) p.scrollTop = p.scrollHeight;
    }
  }catch(e){}
}

haalOpzet(); ververs(); logje();
setInterval(ververs, 3000);
setInterval(logje, 800);
setInterval(haalOpzet, 8000);
</script></body></html>"""


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def _stuur(self, code, lijf, soort="application/json"):
        rauw = lijf.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", soort + "; charset=utf-8")
        self.send_header("Content-Length", str(len(rauw)))
        self.end_headers()
        self.wfile.write(rauw)

    def do_GET(self):
        laatst_gezien[0] = time.time()
        pad = self.path.split("?")[0]
        if pad == "/":
            return self._stuur(200, PAGINA, "text/html")
        if pad == "/status":
            t = laatste_meting()
            v = laatste_verbinding()
            return self._stuur(200, json.dumps({
                "bezig": klus.bezig,
                "bezet": (not klus.bezig) and os.path.isdir(LOCK),
                "tot": dt.datetime.fromtimestamp(t).strftime("%d %b %H:%M") if t else None,
                "achter": (dt.datetime.now().timestamp() - t) if t else None,
                "accu": accu(),
                "contact": dt.datetime.fromtimestamp(v).strftime("%d %b %H:%M") if v else None,
            }))
        if pad == "/setup":
            return self._stuur(200, json.dumps(setup_stand()))
        if pad == "/scan":
            return self._stuur(200, json.dumps({"bezig": scan.bezig,
                                                "gevonden": scan.gevonden,
                                                "fout": scan.fout}))
        if pad == "/log":
            vraag = self.path.split("sinds=")[-1] if "sinds=" in self.path else "0"
            try:
                n = int(vraag)
            except ValueError:
                n = 0
            regels, totaal = klus.sinds(n)
            # Is de log korter dan wat de pagina al had, dan is er een nieuwe
            # ronde begonnen: laat hem opnieuw beginnen in plaats van aanplakken.
            reset = n > totaal
            if reset:
                regels, totaal = klus.sinds(0)
            return self._stuur(200, json.dumps({"regels": regels, "n": totaal,
                                                "reset": reset}))
        return self._stuur(404, json.dumps({"fout": "onbekend"}))

    def _lijf(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def do_POST(self):
        laatst_gezien[0] = time.time()
        if self.path == "/aanmelden":
            d = self._lijf()
            email = (d.get("email") or "").strip()
            pw = d.get("wachtwoord") or ""
            if not email or not pw:
                return self._stuur(200, json.dumps({"ok": False,
                                                    "fout": "vul beide velden in"}))
            if d.get("nieuw") and len(pw) < 6:
                return self._stuur(200, json.dumps({"ok": False,
                                                    "fout": "kies minstens 6 tekens"}))
            try:
                aanmelden(email, pw, nieuw=bool(d.get("nieuw")))
            except RuntimeError as e:
                return self._stuur(200, json.dumps({"ok": False, "fout": str(e)}))
            return self._stuur(200, json.dumps({"ok": True}))
        if self.path == "/afmelden":
            try:
                os.unlink(SESSIE)
            except OSError:
                pass
            return self._stuur(200, json.dumps({"ok": True}))
        if self.path == "/scan":
            ok, bericht = scan.start()
            return self._stuur(200, json.dumps({"ok": ok,
                                                "fout": None if ok else bericht}))
        if self.path == "/instellingen":
            d = self._lijf()
            velden = {}
            if "address" in d:
                velden["address"] = d["address"] or None
                velden["band_naam"] = d.get("band_naam") or None
            for naam, omzet in (("age", int), ("hrmax", float),
                                ("sleep_target", int)):
                if naam in d:
                    try:
                        velden[naam] = omzet(d[naam]) if d[naam] not in (None, "") else None
                    except (TypeError, ValueError):
                        return self._stuur(200, json.dumps(
                            {"ok": False, "fout": "%s moet een getal zijn" % naam}))
            # Leeftijd en gemeten maximum sluiten elkaar uit: wie een gemeten
            # maximum invult, wil niet dat een formule dat overschrijft.
            if velden.get("hrmax"):
                velden["age"] = None
            elif velden.get("age"):
                velden["hrmax"] = None
            try:
                whoop_config.bewaar(**velden)
            except KeyError as e:
                return self._stuur(200, json.dumps({"ok": False, "fout": str(e)}))
            return self._stuur(200, json.dumps({"ok": True, "setup": setup_stand()}))
        if self.path == "/uursync":
            aan = bool(self._lijf().get("aan"))
            ok, bericht = zet_uursync(aan)
            return self._stuur(200, json.dumps({"ok": ok, "aan": uursync_aan(),
                                                "fout": None if ok else bericht}))
        if self.path == "/start":
            ok, bericht = klus.start()
            return self._stuur(200, json.dumps({"ok": ok, "fout": None if ok else bericht}))
        if self.path == "/stop":
            return self._stuur(200, json.dumps({"ok": klus.stop()}))
        return self._stuur(404, json.dumps({"fout": "onbekend"}))

    def log_message(self, *a):
        pass                      # geen regel per verzoek in de terminal


def vrije_poort(voorkeur=8152):
    """Vaste poort als het kan, anders een willekeurige.

    SO_REUSEADDR is nodig omdat een net afgesloten server de poort nog even in
    TIME_WAIT houdt. Zonder deze vlag denkt de test dat de poort bezet is -
    terwijl de server hem (die de vlag zelf wel zet) prima kan gebruiken. Je
    kreeg dan bij elke herstart een ander poortnummer.
    """
    for p in (voorkeur, 0):
        try:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", p))
            poort = s.getsockname()[1]
            s.close()
            return poort
        except OSError:
            continue
    return voorkeur


def main():
    p = argparse.ArgumentParser(description="Whoop leegtrekken met een knop")
    p.add_argument("--geen-browser", action="store_true")
    p.add_argument("--poort", type=int, default=8152)
    a = p.parse_args()

    poort = vrije_poort(a.poort)
    server = ThreadingHTTPServer(("127.0.0.1", poort), Handler)
    url = "http://127.0.0.1:%d/" % poort
    print("Whoop-venster op %s" % url)
    print("Sluit het tabblad om af te sluiten (of Ctrl-C).")
    if not a.geen_browser:
        threading.Thread(target=lambda: (time.sleep(0.4), webbrowser.open(url)),
                         daemon=True).start()

    # Sluit zichzelf zodra niemand meer meekijkt, anders blijft er een servertje
    # achter dat je niet ziet. Tijdens een sync blijft hij natuurlijk staan.
    def wachter():
        while True:
            time.sleep(5)
            if not klus.bezig and time.time() - laatst_gezien[0] > STIL_AF:
                server.shutdown()
                return
    threading.Thread(target=wachter, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if klus.bezig:
            klus.stop()
        print("afgesloten")


if __name__ == "__main__":
    main()
