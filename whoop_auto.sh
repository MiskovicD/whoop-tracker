#!/bin/bash
# Elk uur proberen de band leeg te trekken en door te sturen.
#
# Ligt de band buiten bereik, dan mislukt dit stil en probeert launchd het een
# uur later opnieuw. Dat is de bedoeling: je hoeft nergens aan te denken, en de
# achterstand loopt nooit op. Trek je dagelijks leeg, dan is het één ronde van
# tien seconden in plaats van twintig rondes na een week.
#
#   ./whoop_auto.sh install 23    eenmalig aanzetten, met je leeftijd
#   ./whoop_auto.sh uninstall     weer uitzetten
#   ./whoop_auto.sh inhalen       lange inhaalslag, jij kijkt mee (Ctrl-C stopt)
#   ./whoop_auto.sh log           laatste regels bekijken
#   ./whoop_auto.sh               één ronde nu (dit doet launchd elk uur)
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/.whoop-tracker"
LOG="$DIR/auto.log"
LOCK="$DIR/auto.lock"
CONF="$DIR/auto.conf"
LABEL="whoop-auto"
MAXDUUR=1500     # seconden; een volle ronde van 12 duurt ~10 min, dus 25 is ruim
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
mkdir -p "$DIR"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*" >> "$LOG"; }

case "${1:-run}" in
install)
  LEEFTIJD="${2:-}"
  # De leeftijd bepaalt je geschatte maximale hartslag, en daarmee je belasting.
  # Zonder dat getal is de belastingscore verzonnen, dus vragen we hem hier.
  case "$LEEFTIJD" in
    ''|*[!0-9]*) echo "Gebruik: $0 install <leeftijd>   (bijvoorbeeld: $0 install 23)"; exit 1 ;;
  esac
  echo "LEEFTIJD=$LEEFTIJD" > "$CONF"

  # macOS beschermt ~/Desktop, ~/Documents en ~/Downloads. launchd heeft die
  # toestemming niet, dus daar afgebroken met "Operation not permitted" voordat
  # het script uberhaupt startte - zonder dat je er iets van zag. Staat de
  # checkout in zo'n map, dan draaien we vanaf een kopie die er buiten ligt.
  BRON="$HERE"
  DRAAI="$HERE/whoop_auto.sh"
  case "$HERE/" in
    "$HOME"/Desktop/*|"$HOME"/Documents/*|"$HOME"/Downloads/*)
      DOEL="$DIR/bin"
      if [ "$HERE" != "$DOEL" ]; then
        mkdir -p "$DOEL/app"
        cp "$BRON"/whoop_auto.sh "$BRON"/whoop_*.py "$DOEL/" || exit 1
        # whoop_push.py leest hier de anon-sleutel uit.
        [ -f "$BRON/app/index.html" ] && cp "$BRON/app/index.html" "$DOEL/app/"
        chmod +x "$DOEL/whoop_auto.sh"
      fi
      DRAAI="$DOEL/whoop_auto.sh"
      KOPIE=1 ;;
  esac

  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$DRAAI</string>
  </array>

  <!-- elk uur -->
  <key>StartInterval</key>
  <integer>3600</integer>

  <key>RunAtLoad</key>
  <true/>

  <key>StandardOutPath</key>
  <string>$DIR/auto.out</string>
  <key>StandardErrorPath</key>
  <string>$DIR/auto.err</string>

  <!-- niet draaien terwijl de Mac slaapt; hij haalt het de volgende ronde in -->
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
PLISTEOF
  launchctl unload "$PLIST" 2>/dev/null
  launchctl load "$PLIST" || { echo "launchctl load mislukte"; exit 1; }
  echo "Aan. Elk uur een poging, met --age $LEEFTIJD."
  if [ "${KOPIE:-0}" = 1 ]; then
    echo
    echo "Let op: je checkout staat in een map die macOS afschermt voor"
    echo "achtergrondtaken, dus de uursync draait vanaf een kopie in"
    echo "  $DOEL"
    echo "Na een 'git pull' dus opnieuw:  $0 install $LEEFTIJD"
  fi
  echo "Kijken hoe het gaat:  $0 log"
  exit 0 ;;
uninstall)
  launchctl unload "$PLIST" 2>/dev/null
  rm -f "$PLIST"
  echo "Uit. Handmatig syncen blijft gewoon werken."
  exit 0 ;;
log)
  tail -n "${2:-30}" "$LOG" 2>/dev/null || echo "nog geen log op $LOG"
  exit 0 ;;
inhalen)
  # De band bindt aan een apparaat tegelijk. Draait de uursync er dwars
  # doorheen, dan pakken ze elkaar de verbinding af en levert geen van beide
  # nog iets op (gezien op 2026-09-09: "Bluetooth geweigerd (SIGABRT)").
  # Daarom neemt deze dezelfde vergrendeling; de uursync slaat dan over.
  RONDES="${2:-400}"
  VOORGROND=1 ;;
run) ;;
*)
  echo "Gebruik: $0 [install <leeftijd>|uninstall|inhalen [rondes]|log|run]"; exit 1 ;;
esac

RONDES="${RONDES:-12}"
VOORGROND="${VOORGROND:-0}"

# shellcheck source=/dev/null
[ -f "$CONF" ] && . "$CONF"
LEEFTIJD="${LEEFTIJD:-}"
if [ -z "$LEEFTIJD" ]; then
  log "geen leeftijd ingesteld - draai eerst: $0 install <leeftijd>"
  exit 1
fi

# Niet twee keer tegelijk: een trage drain mag de volgende ronde niet overlappen.
# mkdir is atomair, in tegenstelling tot "bestaat het bestand al".
if ! mkdir "$LOCK" 2>/dev/null; then
  # Ligt de eigenaar er allang niet meer, dan is dit een restant van een ronde
  # die hardhandig is afgebroken. Meteen opruimen, niet 90 minuten wachten.
  EIG=$(cat "$LOCK/pid" 2>/dev/null)
  if [ -n "$EIG" ] && ! kill -0 "$EIG" 2>/dev/null; then
    log "vergrendeling van dood proces $EIG opgeruimd"
    rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || exit 0
  elif [ -n "$(find "$LOCK" -maxdepth 0 -mmin +90 2>/dev/null)" ]; then
    log "oude vergrendeling opgeruimd (ouder dan 90 min)"
    rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || exit 0
  else
    # Op de achtergrond stil overslaan; kijk je mee, dan wil je weten waarom
    # er niets gebeurt.
    [ "${VOORGROND:-0}" = 1 ] && echo "De uursync is nu zelf bezig. Even wachten" \
      "tot die klaar is, of eerst: $0 uninstall"
    exit 0                      # vorige ronde loopt nog
  fi
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK" 2>/dev/null' EXIT

log "start"

# Harde tijdslimiet op de hele ronde. Zonder dit blijft een BLE-aanroep die na
# een slaapstand niet terugkeert eeuwig hangen: research_playground negeert dan
# zijn eigen --timeout, de vergrendeling blijft staan, en launchd start geen
# tweede exemplaar zolang de eerste "draait". Op 7 september kostte dat drie
# dagen sync zonder een enkele foutmelding.
UITBESTAND="$DIR/auto.run.$$"

if [ "$VOORGROND" = 1 ]; then
  # Jij kijkt mee, dus geen bewaker: Ctrl-C stopt de lopende ronde zelf en
  # ruimt de sync-client op. De vergrendeling gaat weg via de EXIT-trap.
  echo "Inhaalslag, hoogstens $RONDES rondes. Ctrl-C om te stoppen;"
  echo "alles wat binnen is blijft staan."
  uv run --no-project --with bleak python "$HERE/whoop_update.py" \
          --age "$LEEFTIJD" --drain --quick --save-daily --rondes "$RONDES" 2>&1 \
    | tee "$UITBESTAND"
  CODE=${PIPESTATUS[0]}
  UIT=$(cat "$UITBESTAND" 2>/dev/null); rm -f "$UITBESTAND"
else
set -m                                   # eigen procesgroep, zodat we alle
uv run --no-project --with bleak python "$HERE/whoop_update.py" \
        --age "$LEEFTIJD" --drain --quick --save-daily --rondes "$RONDES" \
        > "$UITBESTAND" 2>&1 &
KIND=$!
set +m                                   # kinderen in een keer kunnen stoppen
(
  # shellcheck disable=SC2034
  for _ in $(seq 1 "$MAXDUUR"); do
    kill -0 "$KIND" 2>/dev/null || exit 0
    sleep 1
  done
  kill -TERM -"$KIND" 2>/dev/null || kill -TERM "$KIND" 2>/dev/null
  sleep 10
  kill -KILL -"$KIND" 2>/dev/null || kill -KILL "$KIND" 2>/dev/null
) &
BEWAKER=$!

wait "$KIND"; CODE=$?
kill "$BEWAKER" 2>/dev/null
UIT=$(cat "$UITBESTAND" 2>/dev/null); rm -f "$UITBESTAND"
fi

# Alleen de regels die iets zeggen; de rest is ruis in een logbestand.
echo "$UIT" | grep -E "records, nu tot|dag\(en\) naar Supabase|accu:|Niets nieuws|is bij|kolom .* bestaat niet" \
  | sed "s/^/$(date '+%Y-%m-%d %H:%M:%S')  /" >> "$LOG"

if [ $CODE -ne 0 ]; then
  case $CODE in
    126) log "mislukt (126) - macOS blokkeert de toegang tot deze map; draai: $0 install <leeftijd>" ;;
    134) log "mislukt (134) - Bluetooth geweigerd (SIGABRT), geen bereikprobleem" ;;
    143|137) log "afgebroken na $((MAXDUUR / 60)) min - de sync liep vast, waarschijnlijk een slaapstand middenin" ;;
      *) log "mislukt (code $CODE) - waarschijnlijk band buiten bereik; volgende ronde opnieuw" ;;
  esac
fi
log "klaar"

# Logbestand kort houden
if [ "$(wc -l < "$LOG")" -gt 2000 ]; then
  tail -800 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
