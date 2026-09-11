#!/bin/bash
# Zet alles klaar wat nodig is, en opent daarna het venster.
#
# Eén keer draaien na het klonen. Alles wat hierna nog moet - je account, je
# band, je leeftijd - vul je in het venster in, niet meer in de terminal.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
RESEARCH="${WHOOP_RESEARCH:-$HOME/whoop-research}"

zeg() { printf "\n\033[1m%s\033[0m\n" "$*"; }
fout() { printf "\n%s\n" "$*" >&2; exit 1; }

# macOS schermt deze mappen af voor programma's zonder ondertekening. Een app
# die daar vandaan start kan zijn eigen bestanden niet lezen en sluit meteen
# weer - van buiten niet te onderscheiden van "hij doet niets".
case "$HERE/" in
  "$HOME"/Desktop/*|"$HOME"/Documents/*|"$HOME"/Downloads/*)
    fout "Deze map is door macOS afgeschermd:
  $HERE

Programma's zonder ondertekening mogen hier hun eigen bestanden niet lezen.
De app zou starten en meteen weer sluiten, zonder uitleg.

Verplaats de map naar je thuismap en draai het daar opnieuw:
  mv \"$HERE\" ~/whoop-tracker
  cd ~/whoop-tracker && ./installeer.sh

Of haal hem rechtstreeks op de goede plek binnen:
  git clone https://github.com/MiskovicD/whoop-tracker.git ~/whoop-tracker
  cd ~/whoop-tracker && ./installeer.sh" ;;
esac

zeg "1/4  Gereedschap controleren"
if ! command -v python3 >/dev/null; then
  fout "python3 ontbreekt. Installeer de Apple-ontwikkelgereedschappen:
  xcode-select --install"
fi
echo "  python3: $(python3 -V 2>&1)"

if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
  # Bewust niet zelf een script van internet uitvoeren: dat mag jij beslissen.
  fout "uv ontbreekt. Dat heeft de band-client nodig. Installeer hem met:
  curl -LsSf https://astral.sh/uv/install.sh | sh

Open daarna een nieuw terminalvenster en draai dit script opnieuw."
fi
echo "  uv:      $(command -v uv || echo "$HOME/.local/bin/uv")"

zeg "2/4  Band-client van OpenStrap"
if [ -f "$RESEARCH/research_playground.py" ]; then
  echo "  staat al in $RESEARCH"
else
  if ! command -v git >/dev/null; then
    fout "git ontbreekt. Installeer de Apple-ontwikkelgereedschappen:
  xcode-select --install"
  fi
  echo "  klonen naar $RESEARCH"
  git clone --quiet https://github.com/OpenStrap/research.git "$RESEARCH" \
    || fout "klonen mislukte. Bestaat $RESEARCH al met andere inhoud?"
  echo "  klaar (MIT-licentie, niet van ons)"
fi

zeg "3/4  Programma bouwen"
"$HERE/maak-app.sh"

zeg "4/4  Openen"
open -a "$HOME/Applications/Whoop.app" 2>/dev/null \
  || { echo "  kon de app niet openen; start hem uit Launchpad"; }

cat <<'EIND'

Klaar. In het venster vul je nog drie dingen in:

  1. je account   - maak er een aan, of log in met een bestaand
  2. je band      - doe hem van je pols, tik twee keer, en zoek
  3. jij          - je leeftijd, of je gemeten maximale hartslag

Daarna is één klik op Leegtrekken genoeg. Zet in de instellingen de uursync
aan, dan hoef je er helemaal niet meer aan te denken.
EIND
