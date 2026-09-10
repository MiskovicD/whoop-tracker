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
import argparse, json, os, signal, socket, sqlite3, struct, subprocess
import sys, threading, time, webbrowser
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
AUTO = os.path.join(HERE, "whoop_auto.sh")
RESEARCH = os.environ.get("WHOOP_RESEARCH") or os.path.expanduser("~/whoop-research")
DB = os.path.join(RESEARCH, "whoop.db")
LOCK = os.path.expanduser("~/.whoop-tracker/auto.lock")
STIL_AF = 90          # seconden zonder pagina die meekijkt = servertje sluit


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


klus = Klus()
laatst_gezien = [time.time()]


# ---------------------------------------------------------------- pagina

PAGINA = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<title>Whoop</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0b0c0e;--kaart:#16181c;--lijn:#26292e;--ink:#f2f3f5;--grijs:#8b9199;
  --groen:#28c76f;--geel:#e5b83b;--rood:#ea4f4f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}
.wrap{max-width:660px;margin:0 auto;padding:28px 18px 40px}
h1{font-size:13px;letter-spacing:.14em;margin:0 0 3px}
.sub{color:var(--grijs);font-size:12.5px;margin:0 0 18px}
.kaart{background:var(--kaart);border:1px solid var(--lijn);border-radius:14px}
.tegels{display:grid;grid-template-columns:repeat(3,1fr);padding:16px}
.tegels>div+div{border-left:1px solid var(--lijn);padding-left:16px}
.kop{color:var(--grijs);font-size:9.5px;letter-spacing:.09em;font-weight:600}
.waarde{font-size:20px;font-weight:600;margin-top:3px}
button{width:100%;margin:14px 0 0;padding:15px;border:0;border-radius:12px;
  background:var(--ink);color:#000;font-size:15px;font-weight:600;cursor:pointer}
button:disabled{background:#2a2d33;color:var(--grijs);cursor:default}
button.stop{background:var(--rood);color:#fff}
.hint{color:var(--grijs);font-size:12.5px;text-align:center;margin:9px 0 16px;min-height:34px}
pre{background:var(--kaart);border:1px solid var(--lijn);border-radius:14px;
  margin:0;padding:13px 15px;height:300px;overflow:auto;white-space:pre-wrap;
  font:12px/1.65 "SF Mono",Menlo,monospace;color:var(--grijs)}
</style></head><body><div class="wrap">
<h1>WHOOP</h1><p class="sub" id="sub">&nbsp;</p>
<div class="kaart"><div class="tegels">
  <div><div class="kop">BIJGEWERKT TOT</div><div class="waarde" id="tot">&mdash;</div></div>
  <div><div class="kop">ACHTERSTAND</div><div class="waarde" id="achter">&mdash;</div></div>
  <div><div class="kop">ACCU BAND</div><div class="waarde" id="accu">&mdash;</div></div>
</div></div>
<button id="knop">LEEGTREKKEN</button>
<p class="hint" id="hint">&nbsp;</p>
<pre id="log"></pre>
</div><script>
const $ = s => document.querySelector(s);
let bezig = false, n = 0;

$("#knop").addEventListener("click", async () => {
  const pad = bezig ? "/stop" : "/start";
  $("#knop").disabled = true;
  const r = await fetch(pad, {method:"POST"});
  const d = await r.json();
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
    k.textContent = "STOPPEN"; k.className = "stop";
    $("#hint").textContent = "Bezig. Alles wat binnen is blijft staan, ook als je stopt.";
  } else if(d.bezet){
    k.textContent = "ER LOOPT AL EEN SYNC"; k.className = ""; k.disabled = true;
    $("#hint").textContent = "De uursync of een terminal is bezig. Zodra die klaar is, kun je hier klikken.";
  } else {
    k.textContent = "LEEGTREKKEN"; k.className = "";
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

ververs(); logje();
setInterval(ververs, 3000);
setInterval(logje, 800);
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

    def do_POST(self):
        laatst_gezien[0] = time.time()
        if self.path == "/start":
            ok, bericht = klus.start()
            return self._stuur(200, json.dumps({"ok": ok, "fout": None if ok else bericht}))
        if self.path == "/stop":
            return self._stuur(200, json.dumps({"ok": klus.stop()}))
        return self._stuur(404, json.dumps({"fout": "onbekend"}))

    def log_message(self, *a):
        pass                      # geen regel per verzoek in de terminal


def vrije_poort(voorkeur=8152):
    for p in (voorkeur, 0):
        try:
            s = socket.socket()
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
