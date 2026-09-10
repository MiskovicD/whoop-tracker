#!/bin/bash
# Bouwt "Whoop.app" in ~/Applications, zodat leegtrekken een dubbelklik is.
#
# Eén keer draaien. Daarna staat hij in Launchpad en kun je hem naar je Dock
# slepen. De app is een dun laagje: hij start whoop_app.py, dat op zijn beurt
# whoop_auto.sh gebruikt. Verander je die scripts, dan hoef je hier niets
# opnieuw te doen - het pad zit erin, niet de code.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/Whoop.app"
PY="$(command -v python3 || echo /usr/bin/python3)"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>            <string>Whoop</string>
  <key>CFBundleDisplayName</key>     <string>Whoop</string>
  <key>CFBundleIdentifier</key>      <string>nl.misha.whoop</string>
  <key>CFBundleVersion</key>         <string>1.0</string>
  <key>CFBundleShortVersionString</key> <string>1.0</string>
  <key>CFBundlePackageType</key>     <string>APPL</string>
  <key>CFBundleExecutable</key>      <string>whoop</string>
  <key>CFBundleIconFile</key>        <string>whoop</string>
  <key>NSHighResolutionCapable</key> <true/>
  <!-- Bluetooth loopt via de scripts, maar macOS kan het aan deze app
       toerekenen; zonder tekst weigert het systeem de vraag te stellen. -->
  <key>NSBluetoothAlwaysUsageDescription</key>
  <string>Om je Whoop-band uit te lezen.</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/whoop" <<LAUNCHER
#!/bin/bash
# Gegenereerd door maak-app.sh - pas dat script aan, niet dit bestand.
exec "$PY" "$HERE/whoop_app.py"
LAUNCHER
chmod +x "$APP/Contents/MacOS/whoop"

# Icoon uit de telefoon-app, zodat het één familie is.
BRON="$HERE/app/icon-512.png"
if [ -f "$BRON" ]; then
  SET="$(mktemp -d)/whoop.iconset"; mkdir -p "$SET"
  for n in 16 32 128 256 512; do
    sips -z $n $n "$BRON" --out "$SET/icon_${n}x${n}.png" >/dev/null 2>&1
    d=$((n * 2))
    sips -z $d $d "$BRON" --out "$SET/icon_${n}x${n}@2x.png" >/dev/null 2>&1
  done
  iconutil -c icns "$SET" -o "$APP/Contents/Resources/whoop.icns" 2>/dev/null \
    || echo "  (icoon overgeslagen, verder werkt alles)"
  rm -rf "$(dirname "$SET")"
fi

# Zonder dit blijft Finder het oude icoon en de oude naam tonen.
touch "$APP"
echo "Klaar: $APP"
echo "Te vinden in Launchpad als 'Whoop'. Sleep hem naar je Dock."
