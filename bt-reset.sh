#!/usr/bin/env bash
# bt-reset.sh — Nettoyage d'urgence Bluetooth
# Usage : ./bt-reset.sh
# Tue la GUI si active, déconnecte tous les DOT via D-Bus, redémarre bluetoothd.

set -euo pipefail

echo "=== BT-RESET ==="

# 1) Tuer la GUI
if pgrep -f "xdot_manager.gui" > /dev/null 2>&1; then
    echo "[1] Arrêt de la GUI..."
    pkill -9 -f "xdot_manager.gui" 2>/dev/null || true
    sleep 1
else
    echo "[1] GUI non active."
fi

# 2) Déconnecter tous les DOT via D-Bus
echo "[2] Déconnexion des capteurs DOT via D-Bus..."
PATHS=$(busctl tree org.bluez 2>/dev/null \
    | grep -o '/org/bluez/hci[0-9][0-9]*/dev_D4_22_CD_[A-Z0-9_]*' \
    | sort -u || true)

if [ -z "$PATHS" ]; then
    echo "    Aucun capteur DOT trouvé dans BlueZ."
else
    echo "$PATHS" | while read -r path; do
        result=$(busctl call org.bluez "$path" org.bluez.Device1 Disconnect 2>&1 || true)
        echo "    DC $path"
    done
fi

sleep 1

# 3) Vérification connexions restantes
echo "[3] Connexions BLE restantes :"
for h in hci0 hci1 hci2 hci3; do
    n=$(hcitool -i "$h" con 2>/dev/null | grep -c "LE " || true)
    [ "$n" -gt 0 ] && echo "    $h : $n connexion(s) encore active(s)"
done

# 4) Redémarrage bluetooth si des connexions zombies subsistent ou si demandé
ZOMBIES=$(for h in hci0 hci1 hci2 hci3; do
    hcitool -i "$h" con 2>/dev/null | grep -c "LE " || true
done | awk '{s+=$1} END {print s+0}')

if [ "$ZOMBIES" -gt 0 ] || [ "${1:-}" = "--force" ]; then
    echo "[4] Redémarrage du service bluetooth (sudo requis)..."
    sudo systemctl restart bluetooth
    sleep 2
    echo "    Service redémarré."
else
    echo "[4] Pas de redémarrage nécessaire."
fi

# 5) État final
echo "[5] État final :"
hciconfig 2>&1 | grep -E "^hci|BD Address|UP RUNNING|DOWN" | while IFS= read -r line; do
    echo "    $line"
done

echo "=== PRÊT ==="
