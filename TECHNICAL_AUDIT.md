# Audit technique — Optimisations mesure et synchronisation

Analyse détaillée du code pour identifier améliorations de précision, latence et robustesse.

---

## 1. Synchronisation temporelle (recording.py)

### État actuel ✅

- **Stratégie adaptative** : ajustement du délai de démarrage en fonction de latence ACK mesurée
- **Sécurité de précédence** : `math.ceil()` évite un StartUTC dans le passé
- **Mesure ACK latency** : enregistrée per-capteur via `time.monotonic()`
- **Confirmation d'état** : vérification post-démarrage (STATE_RECORDING)

### Optimisations identifiées 🔧

#### 1.1 — Jitter post-démarrage non mesuré

**Problème** : on mesure l'ACK latency mais pas le **vrai jitter de synchronisation** (écart max entre démarrages réels).

**Impact** : l'ACK latency (357ms observé) ne dit pas si les capteurs ont réellement démarré simultanément.

**Solution proposée** :
```python
# Actuellement (recording.py line 370):
jitter_ms=measured_max_ack * 1000.0,  # ACK latency seulement

# Proposé:
# 1. Mesurer le timestamp de démarrage (StartUTC) envoyé vs confirmé
# 2. Calculer le vrai jitter = delta(premiers samples entre capteurs) [dans export]
# 3. Retourner les DEUX métriques dans RecordingResult
```

**Implémentation** :
```python
@dataclass
class RecordingResult:
    ack_latency_ms: Optional[float] = None  # Latence ACK (réseau)
    sync_jitter_ms: Optional[float] = None  # Jitter réel (post-export analysis)
```

**Bénéfice** : diagnostic plus précis des causes de mauvaise synchro (réseau vs capteur).

---

#### 1.2 — Safety margin fixe (0.5s) ignorant dérive UTC

**Problème** : `safety_margin = 0.5s` est constant, mais ne prend pas en compte le drift UTC disponible dans `utc_status`.

**Impact** : sur enregistrement long (>30 min) avec drift > 0.1s, le margin est insuffisant.

**Current code** (recording.py line 319):
```python
safety_margin = 0.5  # Fixe à 0.5s
```

**Proposé** :
```python
# Adapter la marge en fonction du drift mesuré
drift_s = utc_status.drift_seconds if utc_status else 0.0
safety_margin = 0.5 + min(drift_s * 2, 1.0)  # 0.5-1.5s selon drift
logger.info(
    "Safety margin adapté : %.2fs (drift=%.3fs)",
    safety_margin, drift_s
)
```

**Bénéfice** : résilience améliorée sur enregistrements longs.

---

#### 1.3 — États pré-lecture stales (pas revérifiés avant armement)

**Problème** : `preflight_states` lus une seule fois (line 276), puis utilisés pour ALL n armements 200ms+ tard.

**Impact** : sur changement d'état rapide (capteur en erreur), on essaie d'armer un capteur invalide.

**Current code** (recording.py line 276):
```python
preflight_states = await asyncio.gather(*[_read_state_or_none(s, critical=False) for s in sensors])
# ... 200ms+++ plus tard, utilisé pour vérifier validité
```

**Proposé** :
```python
# Valider l'état JUSTE AVANT d'armer, pas sur pré-données stales
async def _arm_and_measure(...):
    # Re-check state right before arming (not using stale preflight)
    current_state = await _read_state_or_none(sensor, critical=False, timeout=2.0)
    if current_state is None or current_state not in (STATE_IDLE, STATE_RECORDING):
        return False, 0.0, f"état invalide avant armement: {_state_name(current_state)}"
    # PUIS envoyer l'armement
    start = time.monotonic()
    await sensor.cmd_start_recording(...)
```

**Bénéfice** : moins de faux positifs d'armement sur capteurs en mauvais état.

---

#### 1.4 — Délai adaptation (retry) limité à 1 retry

**Problème** : `attempts = 2` (line 318) = une seule retry possible.

**Impact** : sur BLE congestionné, une latence ACK élevée pour cause transitoire nécessite plusieurs retries.

**Current code** :
```python
attempts = 2  # Fixe à 2
```

**Proposé** :
```python
# Adapter le nombre de tentatives selon conditions observées
max_attempts = 3 if measured_max_ack > 1.0 else 2
# Ajouter logging pour chaque retry:
logger.warning(
    "Retry #%d: latence ACK=%.2fs dépasse délai=%.2fs (max_attempts=%d)",
    attempt_idx, measured_max_ack, current_delay, max_attempts
)
```

**Bénéfice** : meilleure résilience sur configurations BLE difficiles.

---

## 2. Confirmation d'état post-démarrage

### État actuel ✅

- Délai fixe avant re-check : `_SLEEP_AFTER_START = 0.15s`
- Timeout implicite : attendu tout de suite ou None

### Optimisations identifiées 🔧

#### 2.1 — Timeout variable selon nombre de capteurs

**Problème** : `_SLEEP_AFTER_START = 0.15s` global, indépendant de N capteurs.

**Impact** : pour N=16 capteurs, tous font un read_state_or_none en parallèle = contention BLE.

**Proposé** :
```python
# Adapter le délai selon charge BLE
load_factor = 1.0 + (len(sensors) - 1) / 10.0  # 1.0 à 2.5 pour 1-16 capteurs
sleep_after_start = _SLEEP_AFTER_START * load_factor
await asyncio.sleep(sleep_after_start)
```

**Bénéfice** : moins de race conditions sur lectures d'état simultanées.

---

#### 2.2 — Pas de timeout explicite sur state reads

**Problème** : `_read_state_or_none()` sans timeout = dépend de GATT_TIMEOUT (5s), mais pas explicite.

**Current code** (recording.py line 158):
```python
async def _read_state_or_none(sensor, critical=True):
    try:
        return await _read_state(sensor, critical=critical)
    except (...):
        return None  # Timeout silencieux

async def _read_state(sensor, critical=True):
    return await asyncio.wait_for(
        sensor.cmd_get_state(critical=critical), 
        timeout=8.0  # ← Confus : pourquoi 8.0 et pas GATT_TIMEOUT?
    )
```

**Proposé** :
```python
async def _read_state_or_none(sensor: DotSensor, critical: bool = True, timeout: float = 2.0) -> Optional[int]:
    """Read with explicit timeout parameter, log if timeout."""
    try:
        return await asyncio.wait_for(
            sensor.cmd_get_state(critical=critical),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.debug("[%s] state read timeout (%.1fs)", sensor.name, timeout)
        return None
    except (TimeoutError, asyncio.CancelledError, DotError):
        return None
```

**Bénéfice** : meilleure contrôle des timeouts, moins de blocages.

---

## 3. UTC synchronisation

### État actuel ✅

- Vérification NTP effectuée avant chaque enregistrement
- Drift estimé (~3-5 mesures time.time())
- Status séparé (is_synchronized, drift_seconds, ntp_available)

### Optimisations identifiées 🔧

#### 3.1 — UTC check répété à chaque enregistrement

**Problème** : `get_utc_status()` appelé chaque fois (recording.py line 265), 2-3 sec de latence per call.

**Impact** : sur multiples enregistrements rapides, overhead cumulé.

**Current code** (recording.py line 265):
```python
utc_status = await get_utc_status()  # ← 2-3s si tools lents
```

**Proposé** (cache avec TTL):
```python
class UTCCache:
    def __init__(self, ttl_seconds: float = 30.0):
        self._cached = None
        self._cached_at = None
        self._ttl = ttl_seconds
    
    async def get(self) -> UTCStatus:
        now = time.time()
        if self._cached and (now - self._cached_at) < self._ttl:
            logger.debug("Using cached UTC status")
            return self._cached
        self._cached = await get_utc_status()
        self._cached_at = now
        return self._cached

_utc_cache = UTCCache(ttl_seconds=30)

# In start_all_synchronized():
utc_status = await _utc_cache.get()
```

**Bénéfice** : économie 2s sur enregistrements rapides, surtout multi-run.

---

#### 3.2 — NTP drift estimation basée sur samples simples

**Problème** : `_estimate_drift()` mesure drift entre N mesures time.time(), mais pas prédictif.

**Impact** : sur enregistrement 1h, drift initial 0.1s peut devenir 0.5s en fin.

**Current logic** (utc.py):
```python
async def _estimate_drift():
    # Moyenne simple de deltas time.time()
```

**Proposé** (prédiction linéaire) :
```python
async def estimate_drift_with_trend():
    """Estimate drift and trend (ppm) for predictive safety margin."""
    samples = []
    for _ in range(5):
        samples.append(time.time())
        await asyncio.sleep(0.2)
    
    # Calculer drift linéaire (ppm)
    t0, t_last = samples[0], samples[-1]
    n = len(samples)
    mean_diff = (t_last - t0) / (n - 1)
    ppm = mean_diff * 1e6 if mean_diff > 0 else 0
    
    # Pour durée d'enregistrement D, dérive prédite = D * ppm / 1e6
    return {
        "drift_ms": abs(t_last - t0) * 1000,
        "drift_ppm": ppm,
        "sample_count": n
    }
```

**Bénéfice** : prédiction de dérive pour sessions longues.

---

## 4. Jitter analysis (analysis.py)

### État actuel ✅

- Post-export analysis compare timestamps CSV
- Seuil référence 25ms (1 sample @ 40Hz)
- Détection root sensor (timestamp min)

### Optimisations identifiées 🔧

#### 4.1 — CSV parsing séquentiel sur N fichiers

**Problem** : boucle for sur addresses, ouvre/parse CSV un par un.

**Impact** : linéaire O(N), sur N=16 ~200-500ms.

**Current code** (analysis.py line 165):
```python
for addr in addresses:
    ts, reason = _first_timestamp_for_address(output_dir, addr)
    # Séquentiel
```

**Proposé** (paralléliser):
```python
async def analyze_sync_jitter_async(output_dir: Path, addresses: list[str]) -> JitterResult:
    """Parallelize CSV reads using asyncio."""
    tasks = [_first_timestamp_async(output_dir, addr) for addr in addresses]
    results = await asyncio.gather(*tasks)
    # Dict comprehension des résultats
```

**Bénéfice** : 3-5x speedup sur N > 6 capteurs.

---

#### 4.2 — Pas de variance/stdev du jitter

**Problème** : retourne jitter_max_ms, mais pas la distribution (stdev).

**Impact** : 25ms max peut être due à un seul capteur outlier, pas indicatif.

**Proposé** :
```python
@dataclass
class JitterResult:
    jitter_max_ms: float
    jitter_stdev_ms: float  # Nouveau
    jitter_mean_ms: float   # Nouveau
    outlier_count: int      # Capteurs > mean + 2*stdev

# Calcul
deltas = [ts - min_ts for ts in timestamps.values()]
mean_delta = statistics.mean(deltas)
stdev = statistics.stdev(deltas) if len(deltas) > 1 else 0
outliers = sum(1 for d in deltas if d > mean_delta + 2*stdev)
```

**Bénéfice** : meilleur diagnostic d'anomalies.

---

## 5. GATT communication robustness

### État actuel ✅

- Timeouts par opération (GATT_TIMEOUT=5s, DATA_TIMEOUT=15s)
- Retries sur read_ack (6x par défaut)
- Détection déconnexion tacite

### Optimisations identifiées 🔧

#### 5.1 — Pas d'exponential backoff sur GATT retries

**Problem** : send_and_ack() (sensor.py) retry fixe 0.05s entre tentatives.

**Current** (sensor.py ~line 370):
```python
for retry in range(retries):
    try:
        ...
    except:
        await asyncio.sleep(retry_delay) # Always 0.05s
```

**Proposé** (exponential backoff per attempt):
```python
retry_delay = 0.05 * (1.5 ** attempt)  # 0.05, 0.075, 0.1, 0.15s...
```

**Benefit** : meilleure résilience sur BLE soumis, pas de réessais immédiats.

---

#### 5.2 — Reconnexion BLE gap (7s min entre retries)

**Problem** : reconnexion exponential 1s, 2s, 4s = 7s min avant purge (gui.py line 657).

**Sur acquisition continue** : gap de 7s sans signal = données perdues.

**Proposé** :
```python
# Ou désactiver reconnect during recording (actuel = OK)
# Ou réduire à 1s, 1s, 2s = 4s total
delays = [1.0, 1.0, 2.0]  # 4s au lieu 7s
```

**Benefit** : moins de data loss durant sessions.

---

## 6. État système et logging

### Optimisations identifiées 🔧

#### 6.1 — Manque de logging détaillé sur mesures critiques

**Problem** : jitter/latency mesurés mais peu de logging fin grain.

**Proposé** :
```python
# Dans start_all_synchronized(), ajouter:
logger.info("Mesures latence ACK par capteur :")
for sensor, latency in zip(sensors, ack_latencies):
    logger.info("  [%s] = %.3fs", sensor.address, latency)

# Median latency
latency_sorted = sorted(ack_latencies)
median_latency = latency_sorted[len(latency_sorted)//2]
logger.info("Latence ACK médiane: %.3fs", median_latency)
```

**Benefit** : meilleur diagnostique post-facto.

---

## 7. Tableau récapitulatif des améliorations

| Section | Optimisation | Estimé gain | Priorité |
|---|---|---|---|
| 1.1 | Mesurer vrai jitter post-démarrage | +diagnostic | **HAUTE** |
| 1.2 | Adapter safety_margin sur drift UTC | +robustesse longue durée | MOYENNE |
| 1.3 | Re-vérifier états avant armement | +fiabilité | HAUTE |
| 1.4 | Retry adaptatif (max_attempts) | +résilience BLE congestionné | MOYENNE |
| 2.1 | Délai confirmation adaptatif au load | -race conditions | BASSE |
| 2.2 | Timeout explicite state reads | +contrôle | MOYENNE |
| 3.1 | Cache UTC status (TTL 30s) | -2s per record| BASSE |
| 3.2 | Prédiction drift linéaire | +robustesse 1h+ | BASSE |
| 4.1 | Paralléliser CSV parsing | -50% export analysis | BASSE |
| 4.2 | Calculer stdev/variance jitter | +diagnostic | BASSE |
| 5.1 | Exponential backoff retries | +résilience | MOYENNE |
| 5.2 | Vérifier reconnect timing (7s) | +données | BASSE |

---

## 8. Priorité implémentation recommandée

**Phase 1 (HAUTE — impact immédiat):**
- ✅ 1.1 — Mesurer vrai jitter post-démarrage
- ✅ 1.3 — Re-vérifier états avant armement

**Phase 2 (MOYENNE — robustesse):**
- ⏳ 1.2 — Adapter safety_margin sur drift
- ⏳ 1.4 — Retry adaptatif
- ⏳ 2.2 — Timeout explicite

**Phase 3 (BASSE — optimisations long-terme):**
- Others (cache UTC, parallel parsing, etc.)

---

## 9. Testing recommendations

Avant d'implémenter Phase 1+2 :

```bash
# Test jitter measurement improvement
python tests/test_sync.py --count 9 --duration 30

# Test avec drift UTC émulé
timedatectl set-ntp false
timedatectl set-time "2024-01-01 12:00:05"  # Décalé de 5s
python -m xdot_manager.gui  # Observer safety_margin adapté
```
