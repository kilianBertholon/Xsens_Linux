# xdot-manager

**Gestionnaire BLE multi-adaptateur pour capteurs Xsens DOT / Movella DOT**

Interface graphique PyQt6 permettant de connecter, synchroniser, enregistrer et exporter les données de **jusqu'à 18 capteurs IMU Xsens DOT simultanément**, répartis sur plusieurs dongles Bluetooth.

---

## Fonctionnalités

- **Scan BLE multi-adaptateur** — détecte automatiquement tous les dongles `hciX` présents
- **Connexion simultanée** de 16–18 capteurs (répartition automatique par dongle)
- **Synchronisation temporelle** réseau DOT (protocole Xsens, capteur root/esclaves)
- **Configuration du taux d'acquisition** : 1, 4, 10, 12, 15, 20, 30, 60, 120 Hz
- **Enregistrement flash** déclenché simultanément sur tous les capteurs
- **Export CSV** avec choix du payload (Euler, Quaternion, IMU, Full) et sélection par fichier
- **Analyse de synchronisation** : jitter max, offset par capteur, graphique scatter
- **Info flash** : visualisation des fichiers stockés par capteur avant export
- **Effacement flash** avec confirmation
- **Reconnexion robuste** avec retries, purge automatique des capteurs déconnectés

---

## Prérequis

### Matériel

- 1 à 3 dongles Bluetooth USB (testés avec [ASUS USB-BT500](https://www.asus.com/fr/networking-iot-servers/adapters/usb-bt500/))
- Capteurs [Xsens DOT](https://www.movella.com/products/wearables/xsens-dot) / Movella DOT

### Système

- **Linux** avec BlueZ ≥ 5.55 (Ubuntu 22.04+ recommandé)
- Python **3.11** ou 3.12
- `libxcb-cursor0` (requis par PyQt6 sur Ubuntu)

```bash
sudo apt-get install -y python3.11 python3.11-venv libxcb-cursor0 bluez
```

---

## Installation

### 1. Cloner le dépôt

```bash
git clone https://github.com/<votre-compte>/xdot-manager.git
cd xdot-manager
```

### 2. Créer l'environnement virtuel

```bash
python3.11 -m venv ../.venv
source ../.venv/bin/activate
```

> L'environnement est créé **en dehors** du dossier `xdot-manager` pour ne pas versionner les packages.

### 3. Installer les dépendances

```bash
pip install -e .
```

Cela installe :

| Package | Version minimale | Rôle |
|---|---|---|
| `bleak` | ≥ 0.22.0 | Communication BLE (BlueZ backend) |
| `PyQt6` | ≥ 6.6.0 | Interface graphique |
| `qasync` | ≥ 0.27.0 | Boucle asyncio intégrée à Qt |
| `rich` | ≥ 13.0.0 | Logs colorés en terminal |

### 4. Permissions Bluetooth

Pour utiliser le BLE sans `sudo` :

```bash
sudo usermod -aG bluetooth $USER
# Puis se déconnecter / reconnecter pour appliquer
```

Si les capteurs ne sont pas détectés, vérifier que BlueZ est actif :

```bash
sudo systemctl enable --now bluetooth
sudo rfkill unblock bluetooth
```

---

## Lancement

### Interface graphique (recommandé)

```bash
../.venv/bin/xdot-gui
# ou via le script fourni :
bash launch_gui.sh
```

### Interface en ligne de commande

```bash
../.venv/bin/xdot --help
```

---

## Utilisation

### Workflow standard

```
🔍 Scanner  →  🔗 Connecter  →  ⚙ Réglages  →  ⟳ Synchroniser
→  ⏺ Enregistrer  →  ⏹ Arrêter  →  💽 Flash info  →  💾 Exporter
```

#### 1. 🔍 Scanner
Détecte tous les capteurs DOT (`D4:22:CD:…`) à portée, sur tous les adaptateurs disponibles.

#### 2. 🔗 Connecter
Connexion simultanée avec répartition automatique par dongle.  
Chaque capteur est affiché dans le tableau avec son état BLE.

#### 3. ⚙ Réglages *(optionnel)*
Sélectionner la fréquence d'acquisition avant synchronisation.  
> **Important** : doit être fait **avant ⟳ Synchroniser** (contrainte firmware DOT).

Fréquences disponibles : `1 / 4 / 10 / 12 / 15 / 20 / 30 / 60 / 120 Hz`  
Défaut : **60 Hz**

#### 4. ⟳ Synchroniser
Lance le protocole de synchronisation temporelle DOT.  
Tous les capteurs s'alignent sur le capteur root (premier de la liste).

#### 5. ⏺ Enregistrer
Démarre l'enregistrement simultané sur la flash interne de chaque capteur.  
Un timer indique la durée en cours.

#### 6. ⏹ Arrêter
Arrête l'enregistrement. Le fichier est finalisé et vérifié via un poll de l'état capteur.

#### 7. 💽 Flash info *(optionnel)*
Affiche la liste des fichiers présents en flash pour chaque capteur (horodatage, durée, nb d'échantillons).

#### 8. 💾 Exporter
- Choisir le **payload** (type de données à exporter)
- Sélectionner éventuellement quels fichiers par capteur
- Les CSV sont exportés dans `xdot_export/`

Après l'export :
- Analyse de synchronisation automatique (jitter max, offset par capteur)
- Bouton 📊 Analyse sync pour visualiser le scatter plot

---

## Structure du projet

```
xdot-manager/
├── pyproject.toml               # Configuration du package
├── launch_gui.sh                # Script de lancement rapide
├── xdot_manager/
│   ├── gui.py                   # Interface graphique principale (PyQt6)
│   ├── sensor.py                # DotSensor : toutes les opérations GATT d'un capteur
│   ├── scanner.py               # Scan BLE multi-adaptateur
│   ├── adapters.py              # Détection et gestion des dongles hciX
│   ├── sync.py                  # Protocole de synchronisation temporelle
│   ├── recording.py             # Démarrage / arrêt coordonné de l'enregistrement
│   ├── export.py                # Export flash → CSV, métadonnées
│   ├── main.py                  # Point d'entrée CLI
│   └── protocol/
│       ├── gatt.py              # UUIDs, constantes, codes d'état BLE
│       └── commands.py          # Constructeurs de trames GATT binaires
└── tests/
    ├── test_16.py               # Test 16 capteurs simultanés
    ├── test_adapters.py
    ├── test_connections.py
    └── test_sync.py
```

---

## Formats de sortie CSV

Chaque fichier exporté est nommé `<ADRESSE-MAC>_file<N>.csv`.

| Payload | Colonnes |
|---|---|
| `euler` | `timestamp, euler_x, euler_y, euler_z` |
| `quaternion` | `timestamp, quat_w, quat_x, quat_y, quat_z` |
| `imu` | `timestamp, acc_x, acc_y, acc_z, gyr_x, gyr_y, gyr_z` |
| `full` | `timestamp, quat_w, quat_x, quat_y, quat_z, acc_x, acc_y, acc_z, gyr_x, gyr_y, gyr_z` |

Le `timestamp` est en **microsecondes** (entier 64 bits, base interne DOT).

---

## Dépannage

### Capteur non détecté au scan

```bash
# Vérifier que les dongles sont vus par BlueZ
hciconfig -a
# Réinitialiser un dongle
sudo hciconfig hci1 reset
```

### `libxcb-cursor0` manquant (PyQt6 crash au démarrage)

```bash
sudo apt-get install -y libxcb-cursor0
```

### Capteurs qui se déconnectent pendant la sync

- Réduire le nombre de capteurs par dongle (max ~8 stable)
- Vérifier que les dongles sont sur des ports USB 3.0 séparés
- Éviter les interférences Wi-Fi 2.4 GHz (BLE partage la bande)

### Export lent (>10 min pour 18 capteurs)

Normal pour de longues sessions. Le transfert BLE de la flash est limité à ~3 Ko/s par capteur. Pour une session de 5 min à 60 Hz : ~90 000 échantillons × 18 capteurs ≈ 35–50 min d'export.

### Permission refusée sur `/dev/rfkill`

```bash
sudo chmod 664 /dev/rfkill
sudo chown root:bluetooth /dev/rfkill
```

---

## Configuration multi-dongles

Le scanner détecte automatiquement tous les adaptateurs `hciX` disponibles. Pour forcer la répartition :

```python
# Dans scanner.py, ajuster MAX_PER_ADAPTER (défaut : 8)
MAX_PER_ADAPTER = 6  # pour 3 dongles × 6 = 18 capteurs
```

---

## Dépendances matérielles testées

| Configuration | Résultat |
|---|---|
| 16 capteurs, 2 dongles hci0+hci1 | ✅ Stable |
| 18 capteurs, 3 dongles hci0+hci1+hci2 | ✅ Stable |
| 18 capteurs, 1 seul dongle | ❌ Trop de congestion BLE |

---

## Licence

Ce projet est distribué sous licence **MIT**.  
Xsens DOT et Movella DOT sont des marques déposées de Movella Inc.
