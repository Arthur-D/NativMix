#!/bin/bash
set -e

echo "Installiere NativMix im User-Space (~/.local) ..."

# Ziel-Ordner
APP_DIR="$HOME/.local/share/nativmix"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"

# Ordner anlegen
mkdir -p "$APP_DIR"
mkdir -p "$BIN_DIR"
mkdir -p "$DESKTOP_DIR"

# Quellcode kopieren (mit Aufräumen falls schon existent)
rm -rf "$APP_DIR/lib" "$APP_DIR/assets"
cp -r lib "$APP_DIR/"
if [ -d "assets" ]; then
    cp -r assets "$APP_DIR/"
fi

# Echtes Logo in den App-Ordner kopieren
cp assets/icon.png "$APP_DIR/nativmix.png"

# Wrapper Skript in ~/.local/bin/ anlegen
WRAPPER="$BIN_DIR/nativmix"
cat << EOF > "$WRAPPER"
#!/bin/bash
export PYTHONPATH="$APP_DIR/lib:\$PYTHONPATH"
exec /usr/bin/python "$APP_DIR/lib/nativmix/main.py" "\$@"
EOF

# Ausführbar machen
chmod +x "$WRAPPER"

# .desktop Datei für KDE/Wayland Desktop-Integration
DESKTOP_FILE="$DESKTOP_DIR/nativmix.desktop"
cat << EOF > "$DESKTOP_FILE"
[Desktop Entry]
Name=NativMix
Comment=Hardware Volume Mixer
Exec=$WRAPPER
Icon=$APP_DIR/nativmix.png
Terminal=false
Type=Application
Categories=AudioVideo;Audio;Mixer;
EOF

# Desktop Database updaten, damit die App im Startmenü erscheint
update-desktop-database "$DESKTOP_DIR"

echo "Installation abgeschlossen!"
echo "Stelle sicher, dass ~/.local/bin in deinem PATH ist."
echo "Führe nun einfach 'nativmix' im Terminal aus."
