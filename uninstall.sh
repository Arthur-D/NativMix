#!/bin/bash
set -e

echo "Deinstalliere NativMix aus dem User-Space (~/.local) ..."

# 1. Programmdateien und Ordner löschen
rm -f "$HOME/.local/bin/nativmix"
rm -rf "$HOME/.local/share/nativmix"

# 2. Startmenü- & Autostart-Einträge entfernen
rm -f "$HOME/.local/share/applications/nativmix.desktop"
rm -f "$HOME/.config/autostart/nativmix.desktop"

# 3. Desktop Database aktualisieren (KDE Menü bereinigen)
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

# 4. Optionale Konfigurationsbereinigung
echo ""
read -p "Möchtest du auch die Konfigurationsdateien (~/.config/nativmix) löschen? (y/N) " config_answer

if [[ "$config_answer" =~ ^[Yy]$ ]]; then
    rm -rf "$HOME/.config/nativmix"
    echo "> Konfigurationsdateien wurden gelöscht."
else
    echo "> Konfigurationsdateien bleiben als Backup erhalten."
fi

echo ""
echo "NativMix wurde erfolgreich deinstalliert."
