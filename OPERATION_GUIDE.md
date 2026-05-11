# Guide opérationnel xdot-manager

Bonnes pratiques, troubleshooting, et optimisations pour enregistrements stabiliés multi-capteurs.

---

## 1. Synchronisation temporelle UTC (NTP)

### Pourquoi c'est important

La synchronisation UTC/NTP affecte **directement la précision du timestamp de démarrage** (`StartUTC`) envoyé aux capteurs. Sans NTP synchronisé, les capteurs démarrent avec un UTC décalé, ce qui fausse les données exportées.

**Impact observé :**
- UTC décalé de ±5s → timestamps toutes les mesures décalées de ±5s
- Problème détectable lors du merge/fusion multi-capteurs : les timeaxis ne s'alignent pas

### Vérifier l'état NTP

```bash
# Méthode 1 : timedatectl (systemd-timesyncd)
timedatectl status

# Attendu :
#   System clock synchronized: yes
#   RTC in local TZ: no
#   DST active: n/a

# Méthode 2 : ntpq (NTPD ou Chrony)
ntpq -p
# ou
chronyc sources
```

**Status "Not synchronized"** → relancer le service et attendre 2-3 min :

```bash
# Pour systemd-timesyncd
sudo systemctl restart systemd-timesyncd

# Pour Chrony
sudo systemctl restart chrony && sleep 30 && chronyc sources

# Forcer une synchronisation (mode burst)
sudo ntpdate -u ntp.ubuntu.com  # ou pool.ntp.org
```

### Réseau universitaire / WiFi d'entreprise

Les réseaux corporatifs **bloquent souvent le UDP port 123** (NTP).

**Symptômes :**
- Tous les serveurs NTP en timeout (185.125.190.56, pool.ntp.org)
- timedatectl montre "Synchronizing..." → "Not synchronized" après 30s

**Solutions :**
1. **Réseau mobile (4G/5G)** — le moyen le plus fiable
   ```bash
   timedatectl status  # attend 10-20s après connexion 4G
   ```

2. **Réseau avec accès NTP autorisé** — lancer timedatectl en background
   ```bash
   sudo systemctl restart systemd-timesyncd &
   # Puis dans une autre session, tester qd que l'acquisition
   ```

3. **Synchronisation manuelle une seule fois** (déprécié)
   ```bash
   sudo ntpdate -u ntp.ubuntu.com
   # Puis enregistrer rapidement, car le drift reprend après
   ```

**Avant un enregistrement long (>30 min)**, vérifier `timedatectl status` affiche `synchronized: yes`.

### GUI — Avertissement NTP

Quand vous lancez un enregistrement :

- ✅ **"UTC OK"** — horloge synchronisée, drift < 100 ms → normal
- ⚠️ **"Horloge système pas synchronisée NTP"** (jaune) → précision dégradée, enregistrement possible mais timestamps moins fiables
- 🔴 **"UTC non disponible"** (rouge) — NTP tools not accessible → enregistrement possible mais aucun contrôle

**L'enregistrement n'est pas bloqué par ces avertissements** pour ne pas empêcher les tests réseau congestionné.

---

## 2. Reconnexion automatique BLE

### Stratégie implémentée

La reconnexion automatique **détecte une chute inattendue** et tente de se reconnecter **avant l'enregistrement**.

```
Chute BLE détectée
    ↓
Tentative 1 (attente 1s) → Succès? Retour à CONNECTED
    ↓
Tentative 2 (attente 2s) → Succès? Retour à CONNECTED
    ↓
Tentative 3 (attente 4s) → Succès? Retour à CONNECTED
    ↓
Échec définitif → État "ERREUR", capteur retiré de la liste active
```

**Durée totale : ~7 secondes maximum**

### Limitations importantes

⚠️ **La reconnexion est DÉSACTIVÉE pendant l'enregistrement** car une reconnexion BLE interrompt le flux de données enregistrées sur le capteur → risque de corruption.

→ Si un capteur se déconnecte pendant l'acquisition :
1. Arrêter l'enregistrement (`Stop Recording`)
2. Vérifier l'état du capteur (clique `Refresh`)
3. Si déconnecté, il sera retiré automatiquement
4. Relancer l'enregistrement avec les capteurs restants

### Optimisation des timeouts

- **GATT_TIMEOUT (GATT operations)**: 5s (optimisé pour réactivité)
- **CONNECT_TIMEOUT**: 15s (4 tentatives échelonnées)
- **DATA_TIMEOUT (export)**: 15s par paquet

Ces valeurs sont adaptées au Bluetooth BLE LE (low energy). Une réduction supplémentaire (<3s) provoque des timeouts sur connexions instables.

---

## 3. Mesure et synchronisation

### StartUTC — précision seconds-only

Le protocole Xsens DOT **encodé StartUTC comme uint32 (secondes Unix epoch)**, pas en fractions.

```python
# Spec §5.2.2
StartUTC = int(ceil(time.time() + delay + safety_margin))  # =>secondes entières
```

**Résolution : 1 seconde** (pas 1/120s même si acquisition à 120 Hz)

### Timestamps fins dans les données exportées

La précision **milliseconde/microseconde** est disponible dans les données exportées :

**CSV exported:**
```
timestamp,euler_x,euler_y,euler_z,...
[uint32_seconds + uint32_microseconds]
```

Le champ `timestamp` dans l'export CSV contient `SampleTimeFine` (résolution microseconde).

→ **Pour analyses fine (~8ms / 120Hz)** : utiliser les timestamps exportés, pas le StartUTC de démarrage.

### Mesure du jitter

Le GUI affiche **latence ACK max** après chaque enregistrement :

```
⊡ latence ACK max = 357 ms
```

C'est la **latence réseau mesurée**, pas le jitter intra-capteur.

**Interprétation:**
- < 100 ms → très bon
- 100-300 ms → acceptable
- > 500 ms → dégradation BLE probable (éloignement/interférences)

**Amélioration :** rapprocher les dongles des capteurs, réduire les obstacles RF.

---

## 4. Bonnes pratiques d'enregistrement

### Avant de lancer

1. ✅ Vérifier `timedatectl status` → "System clock synchronized: yes"
2. ✅ Scanner (8-10s) → au moins N capteurs trouvés
3. ✅ Connecter → tous les capteurs en **CONNECTED** (vert)
4. ✅ Synchroniser (Syncing) → tous en **IDLE** (attendus après sync)
5. ✅ Configurer taux acquisition (défaut 120 Hz recommandé)
6. ✅ Vérifier latence ACK avec un test court (5s) avant grosse acquisition

### Pendant l'enregistrement

- **Ne pas arrêter** le programme ni la connexion réseau
- **Ne pas débrancher** les dongles — provoque déconnexions en cascade
- **Surveillance simple** : affichage temps restant + watchdog d'état capteurs

### Après l'enregistrement

1. Attendre le GUI → "Enregistrement terminé. [X/N] capteurs OK"
2. **Vérifier les états exportés** (nombre de fichiers = nombre de capteurs)
3. Une déconnexion **après stop** est normale (pas grave)
4. Exporter les données (format CSV/JSON)

### Cas problématiques

| Symptôme | Cause probable | Action |
|---|---|---|
| Latence ACK > 500ms | Interférences RF / éloignement | Rapprocher dongles ou capteurs |
| "Capteur non confirmé" (IDLE avant start) | État invalide au démarrage | Relancer l'acquisition |
| "ACK timeout" répétés | Congestion BLE (trop capteurs par dongle) | Répartir sur 2-3 dongles |
| Fichier export vide | Capteur en "Not recording" pendant acquisition | Vérifier l'état initial avec cmd_get_state |

---

## 5. Optimisations techniques pour meilleures mesures

### Multi-adaptateurs (spread load)

Éviter de saturer un seul dongle :

```
Recommandé :
- 3-4 capteurs par dongle (adapter)
- 3+ dongles pour 9-16 capteurs
- Max 18 capteurs sur 3-4 dongles = théorique, pratique 12-14 stable
```

**Pourquoi ?** Chaque dongle a une limite BLE de **~40 Mbps partagée** entre connexions.

### Output rate (Hz)

```
- 120 Hz (défaut) : meilleure résolution, plus de trafic BLE
-- Recommandé pour la plupart des cas

- 60 Hz : équilibre → bonnes mesures, charge réduite

- 30 Hz ou moins : bon pour décimation tempo, moins de données
```

**Impact BLE :** réduction Hz → réduit la charge réseau, améliore la stabilité sur configurations instables.

### Format export

| Format | Avantage | Inconvénient |
|---|---|---|
| CSV | Lisible, standard | Plus volumineux |
| JSON | Structure complète, métadonnées | Plus lent à parser |

→ Pour > 5 capteurs × 1h : CSV préféré (moins d'I/O).

### Calibration/Offset capteurs

Xsens DOT supporte in-device Quaternion fusion. **Pas d'offset inter-capteur natif**.

→ Faire un **enregistrement de référence pré-vol/pré-test** (30s statique tous capteurs) pour post-traitement offset.

---

## 6. Enregistrements longs (>30 min)

### Préparation

1. **Réseau NTP stable** (WiFi entreprise: utiliser réseau mobile mieux)
2. **Batterie capteurs** > 50% (autonomie ~8h à 120Hz, ~12h à 60Hz)
3. **Flash libre** sur capteurs (~20 min à 120Hz, 9-DOF full)
4. **Test court (5 min)** avant acquisition longue

### Monitoring

```bash
# Terminal séparé : vérifier dérive NTP en live
watch -n 5 'timedatectl status'

# Capteur : battery level affiché en GUI si disponible
```

### Après capture longue

Exporter immédiatement (ne pas laisser données en flash > 2j).

---

## 7. Troubleshooting

### Les capteurs ne sont pas détectés au scan

```bash
# Vérifier BlueZ actif
sudo systemctl status bluetooth

# Vérifier interfaces
hciconfig
# Attendu : hci0, hci1, ... avec flag "UP"

# Reset complet (caution!)
./bt-reset.sh
```

### Connexion impossible (timeout)

1. Vérifier dongle est reconnu : `hciconfig`
2. Relancer BlueZ : `sudo systemctl restart bluetooth`
3. **Relancer la GUI** (session asyncio parfois bloquée)
4. Vérifier capteur n'est pas connecté ailleurs (autre app / autre session)

### ACK timeout pendant start/stop

```
Cause probable: BLE congestion
Action: 
- Arrêter la GUI
- Attendre 5s
- Réduire nombre capteurs
- Relancer avec 3-4 capteurs seulement
```

### Export très lent (> 5 min pour 5 capteurs)

```
Cause probable: Flash lente (capteur vieil firmware) ou USB2 dongle
Action:
- Activer debug pour voir vitesse paquet
  logger.setLevel(DEBUG)
- Réduire output rate avant acquisition (60 Hz au lieu 120Hz)
```

---

## 8. Fichiers de log

Par défaut, logs écrits en **stdout**. Pour persister :

```bash
# Redirection dans un fichier
python -m xdot_manager.gui 2>&1 | tee recording_$(date +%Y%m%d_%H%M%S).log
```

**Logs utiles pour debug :**

```
[INFO] GATT_TIMEOUT=5.0s
[INFO] Latence ACK max mesurée: 0.357s
[DEBUG] ACK stale mais state=RECORDING confirmé
[WARNING] Tentative 1/3 échouée...
```

---

## 9. Liens références

- [Xsens DOT BLE Services Spec](file:///path/to/spec)
- [BlueZ Documentation](http://www.bluez.org/)
- [NTP Configuration](https://wiki.debian.org/systemd-timesyncd)
- AUDIT_IMPLEMENTATION.md (conformité spec)
