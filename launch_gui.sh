#!/bin/bash
# Lance l'interface graphique Xsens DOT Manager
VENV="/home/nicolas/Documents/exportateur_DOT/.venv"
DIR="/home/nicolas/Documents/exportateur_DOT/xdot-manager"

# Vérifier libxcb-cursor0
if ! dpkg -l libxcb-cursor0 2>/dev/null | grep -q "^ii"; then
    echo "[INFO] Installation de libxcb-cursor0..."
    sudo apt-get install -y libxcb-cursor0
fi

cd "$DIR" || exit 1
"$VENV/bin/python" -m xdot_manager.gui "$@"
