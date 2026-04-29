"""
Contrôle coordonné de l'enregistrement sur N capteurs Xsens DOT.

Fonctions :
- start_all()   : start_recording() sur tous en asyncio.gather
- stop_all()    : stop_recording() sur tous en asyncio.gather
- wait_duration(): attend N secondes avec affichage de la progression
- measure_jitter(): mesure le delta entre premier et dernier ACK start

Un RecordingResult documente le résultat par capteur et le jitter observé.
"""
from __future__ import annotations

import asyncio
import math
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .sensor import DotSensor, DotError, STATE_IDLE, STATE_RECORDING
from .protocol.gatt import STATE_NAMES
from .utc import verify_utc_before_recording, get_utc_status
logger = logging.getLogger(__name__)

# Sûreté : éviter les commandes concurrentes ou redondantes quand l'UI est très
# sollicitée / quand beaucoup de capteurs sont connectés.
_RECORDING_OP_LOCK = asyncio.Lock()
_STAGGER_SEC = 0.05
_GROUP_COOLDOWN_SEC = 0.15


def _normalize_sensors(sensors: list[DotSensor]) -> tuple[list[DotSensor], list[str]]:
    """Supprime les doublons par adresse et conserve l'ordre d'entrée."""
    unique: list[DotSensor] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    for sensor in sensors:
        addr = sensor.address.upper().strip()
        if addr in seen:
            duplicates.append(addr)
            continue
        seen.add(addr)
        unique.append(sensor)
    return unique, duplicates


async def _read_state(sensor: DotSensor, critical: bool = True) -> int:
    """Lit l'état réel du capteur avec un garde-fou de délai."""
    return await asyncio.wait_for(sensor.cmd_get_state(critical=critical), timeout=8.0)


async def _read_state_or_none(sensor: DotSensor, critical: bool = True) -> Optional[int]:
    """Lit l'état réel du capteur sans propager les timeouts/transitoires."""
    try:
        return await _read_state(sensor, critical=critical)
    except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError, DotError):
        return None


# ---------------------------------------------------------------------------
# Résultat
# ---------------------------------------------------------------------------

@dataclass
class RecordingResult:
    action: str                          # "start" ou "stop"
    success: bool
    per_sensor: dict[str, bool]          # addr → ok
    errors: dict[str, str]               # addr → message
    jitter_ms: Optional[float] = None    # latence ACK max mesurée (ms)
    total_duration_ms: float = 0.0

    @property
    def failed_sensors(self) -> list[str]:
        return [addr for addr, ok in self.per_sensor.items() if not ok]

    def __str__(self) -> str:
        ok = sum(self.per_sensor.values())
        total = len(self.per_sensor)
        jitter_str = f" — latence ACK max={self.jitter_ms:.1f} ms" if self.jitter_ms is not None else ""
        return (
            f"Recording {self.action.upper()} — "
            f"{ok}/{total} capteurs OK — "
            f"durée={self.total_duration_ms:.0f} ms"
            f"{jitter_str}"
        )


@dataclass
class RecordingHealthResult:
    """Résultat du watchdog de stabilité pendant capture."""
    success: bool
    checks: int
    problematic_sensors: list[str] = field(default_factory=list)
    first_issues: dict[str, str] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        state = "OK ✓" if self.success else "⚠ DÉGRADÉ"
        return (
            f"Watchdog capture {state} — "
            f"{self.checks} contrôle(s) — "
            f"{len(self.problematic_sensors)} capteur(s) en anomalie"
        )


# ---------------------------------------------------------------------------
# Tâche individuelle (start ou stop)
# ---------------------------------------------------------------------------

async def _record_one(
    sensor: DotSensor,
    action: str,          # "start" ou "stop"
    timestamps: list[float],
    stale_ack_accepted: Optional[dict[str, int]] = None,
) -> tuple[bool, str]:
    """
    Exécute start_recording ou stop_recording sur un capteur.
    Ajoute le timestamp de l'ACK dans `timestamps`.
    Retourne (success, error_message).
    """
    try:
        current_state = await _read_state_or_none(sensor)
        if current_state is None:
            return False, "état capteur indisponible avant commande"

        if action == "start":
            if current_state == STATE_RECORDING:
                timestamps.append(time.monotonic())
                return True, ""
            if current_state != STATE_IDLE:
                return False, (
                    f"état non autorisé avant démarrage : 0x{current_state:02x}"
                )
            try:
                await sensor.cmd_start_recording()
            except DotError as exc:
                # Si l'ACK est périmé mais que l'état réel a basculé en
                # enregistrement, on préfère considérer l'opération comme réussie.
                await asyncio.sleep(0.25)
                confirmed_state = await _read_state_or_none(sensor)
                if confirmed_state == STATE_RECORDING:
                    timestamps.append(time.monotonic())
                    if stale_ack_accepted is not None:
                        stale_ack_accepted[sensor.address] = stale_ack_accepted.get(sensor.address, 0) + 1
                    return True, ""
                raise exc

            await asyncio.sleep(0.15)
            confirmed_state = await _read_state_or_none(sensor)
            if confirmed_state is None:
                timestamps.append(time.monotonic())
                logger.warning(
                    "[%s] start_recording: confirmation d'état indisponible, commande acceptée.",
                    sensor.name,
                )
                return True, ""
            if confirmed_state != STATE_RECORDING:
                return False, f"démarrage non confirmé (état={confirmed_state:#04x})"
        else:
            if current_state == STATE_IDLE:
                timestamps.append(time.monotonic())
                return True, ""
            if current_state != STATE_RECORDING:
                return False, (
                    f"état non autorisé avant arrêt : 0x{current_state:02x}"
                )
            try:
                await sensor.cmd_stop_recording()
            except DotError:
                await asyncio.sleep(0.35)
                confirmed_state = await _read_state_or_none(sensor)
                if confirmed_state == STATE_IDLE:
                    timestamps.append(time.monotonic())
                    if stale_ack_accepted is not None:
                        stale_ack_accepted[sensor.address] = stale_ack_accepted.get(sensor.address, 0) + 1
                    return True, ""
                raise
        timestamps.append(time.monotonic())
        return True, ""
    except DotError as exc:
        msg = str(exc)
        logger.error("[%s] Erreur recording %s : %s", sensor.name, action, msg)
        return False, msg
    except Exception as exc:
        msg = f"Erreur inattendue : {exc}"
        logger.exception("[%s] %s", sensor.name, msg)
        return False, msg


async def _check_recording_health(sensors: list[DotSensor]) -> tuple[dict[str, int | None], list[str]]:
    """Lit l'état de tous les capteurs et retourne les états détectés + erreurs."""
    states = await asyncio.gather(*[_read_state_or_none(s) for s in sensors])
    mapping: dict[str, int | None] = {}
    issues: list[str] = []
    for sensor, state in zip(sensors, states):
        mapping[sensor.address] = state
        if state is None:
            issues.append(f"[{sensor.name}] état indisponible")
        elif state != STATE_RECORDING:
            state_name = STATE_NAMES.get(state, f"0x{state:02x}")
            issues.append(f"[{sensor.name}] état={state_name}")
    return mapping, issues


async def start_all_synchronized(
    sensors: list[DotSensor],
    *,
    start_delay_s: float = 2.5,
) -> RecordingResult:
    """
    Démarre l'enregistrement sur tous les capteurs de manière synchronisée,
    en utilisant un StartUTC commun envoyé à tous les capteurs.

    Stratégie adaptative :
    1. Calculer un UTC cible dans le futur (start_delay_s secondes)
    2. Envoyer exactement le même UTC à tous les capteurs avec wait_ack=True
    3. Mesurer la latence ACK par capteur
    4. Si max(ACK latencies) dépasse le délai configuré, augmenter le délai et recommencer (une fois)
    5. Attendre le démarrage planifié et confirmer les états
    6. Confirmation par lecture d'état après stabilisation

    Args:
        sensors         : liste des capteurs à synchroniser
        start_delay_s   : délai d'attente avant démarrage (en secondes)

    Returns:
        RecordingResult avec jitter mesuré lors de l'armement
    """
    import time as _time

    async with _RECORDING_OP_LOCK:
        if not sensors:
            return RecordingResult("start", True, {}, {})

        sensors, duplicates = _normalize_sensors(sensors)
        logger.info("START RECORDING SYNCHRONIZED sur %d capteur(s)...", len(sensors))

        if duplicates:
            logger.warning(
                "Capteurs en doublon ignorés avant start: %s",
                ", ".join(duplicates),
            )

        # ===== Vérification UTC (robustesse) =====
        logger.info("Vérification synchronisation UTC système...")
        utc_status = await get_utc_status()
        logger.info(f"UTC Status: {utc_status}")
        
        if not utc_status.is_synchronized:
            logger.warning(
                "⚠️ Horloge système pas synchronisée NTP. Les timestamps peuvent être incorrects."
            )
        elif utc_status.drift_seconds > 1.0:
            logger.warning(
                "⚠️ Dérive UTC %.2fs détectée. "
                "Impact: timestamps de démarrage peuvent être imprécis. "
                "Si enregistrement long (>30 min): synchronisez NTP d'abord.",
                utc_status.drift_seconds
            )
        else:
            logger.info(f"✓ UTC OK (drift: {utc_status.drift_seconds:.3f}s)")

        t0 = time.monotonic()

        # Pré-lecture de l'état avant démarrage
        preflight_states = await asyncio.gather(*[_read_state_or_none(s, critical=False) for s in sensors])

        def _state_name(state: Optional[int]) -> str:
            if state is None:
                return "indisponible"
            return STATE_NAMES.get(state, f"0x{state:02x}")

        async def _arm_and_measure(sensor: DotSensor, utc_ts: int, pre_state: Optional[int]) -> tuple[bool, float, str]:
            """
            Envoie la commande armée avec ACK et mesure le temps de réponse.
            Retourne (success_ack, ack_latency_s, error_message)
            """
            if pre_state is None:
                return False, 0.0, "état indisponible"
            if pre_state not in (STATE_IDLE, STATE_RECORDING):
                return False, 0.0, f"état non autorisé: {_state_name(pre_state)}"
            if pre_state == STATE_RECORDING:
                return True, 0.0, ""

            start = time.monotonic()
            try:
                await sensor.cmd_start_recording(
                    utc_timestamp=utc_ts,
                    wait_ack=True,
                    critical=False,
                )
                latency = time.monotonic() - start
                return True, latency, ""
            except Exception as exc:
                latency = time.monotonic() - start
                return False, latency, str(exc)

        safety_margin = 0.5  # Marge de sécurité au-delà de la latence max ACK
        attempts = 2
        current_delay = float(start_delay_s)
        per_sensor_final: dict[str, bool] = {}
        per_sensor_errors: dict[str, str] = {}
        measured_max_ack = 0.0

        for attempt_idx in range(1, attempts + 1):
            # StartUTC est transporté en secondes entières : on arrondit vers le haut
            # pour ne jamais programmer un départ plus tôt que demandé.
            # NOTE: Spec §5.2.2 limite StartUTC à la résolution de la SECONDE (int32 Unix timestamp).
            # Pour une précision plus fine au 1/120 s (acquisition 120 Hz), voir SampleTimeFine
            # dans les données CSV exportées (résolution microseconde).
            start_utc = int(math.ceil(_time.time() + current_delay + safety_margin))
            logger.info(
                "Tentative d'armement %d/%d : UTC cible = %d (délai = %.1fs)",
                attempt_idx, attempts, start_utc, current_delay,
            )

            # Armement parallèle avec mesure ACK-latency
            tasks = [
                _arm_and_measure(sensor, start_utc, pre_state)
                for sensor, pre_state in zip(sensors, preflight_states)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=False)

            # Collecte des métriques et résultats
            ack_latencies: list[float] = []
            for sensor, (ok, latency, err) in zip(sensors, results):
                per_sensor_final[sensor.address] = ok
                if not ok and err:
                    per_sensor_errors[sensor.address] = err
                ack_latencies.append(latency)

            measured_max_ack = max(ack_latencies) if ack_latencies else 0.0
            logger.info("Latence ACK max mesurée : %.3fs", measured_max_ack)

            # Vérifier si les ACKs arrivent dans le délai planifié
            if measured_max_ack + safety_margin > current_delay and attempt_idx < attempts:
                # Augmenter le délai et recommencer
                current_delay = measured_max_ack + safety_margin + 1.0
                logger.warning(
                    "Latence ACK (%.3fs) + marge (%.3fs) dépasse le délai (%.3fs). "
                    "Augmentation du délai à %.3fs et nouvel armement.",
                    measured_max_ack, safety_margin, start_delay_s, current_delay,
                )
                continue

            # Sinon, attendre le moment du démarrage planifié
            now = _time.time()
            wait_until = start_utc + 0.2
            to_wait = max(0.0, wait_until - now)
            logger.info("Attente du top synchronisé (%.2fs)...", to_wait)
            await asyncio.sleep(to_wait)

            # Après le démarrage planifié, confirmer les états
            confirm_tasks = [_read_state_or_none(sensor, critical=False) for sensor in sensors]
            confirmed_states = await asyncio.gather(*confirm_tasks, return_exceptions=False)

            for sensor, confirmed_state in zip(sensors, confirmed_states):
                if confirmed_state == STATE_RECORDING or confirmed_state is None:
                    per_sensor_final.setdefault(sensor.address, True)
                else:
                    per_sensor_final[sensor.address] = False
                    per_sensor_errors.setdefault(
                        sensor.address,
                        f"confirmation : état={_state_name(confirmed_state)}"
                    )

            break

        result = RecordingResult(
            action="start",
            success=all(per_sensor_final.values()) if per_sensor_final else False,
            per_sensor=per_sensor_final,
            errors=per_sensor_errors,
            jitter_ms=measured_max_ack * 1000.0,
            total_duration_ms=(time.monotonic() - t0) * 1000,
        )
        logger.info("%s", result)
        return result


# ---------------------------------------------------------------------------
# API principale
# ---------------------------------------------------------------------------

async def start_all(sensors: list[DotSensor]) -> RecordingResult:
    """
    Démarre l'enregistrement sur tous les capteurs simultanément.

    Returns:
        RecordingResult avec jitter entre 1er et dernier ACK start.
    """
    async with _RECORDING_OP_LOCK:
        if not sensors:
            return RecordingResult("start", True, {}, {})

        sensors, duplicates = _normalize_sensors(sensors)

        logger.info("START RECORDING sur %d capteur(s)...", len(sensors))
        if duplicates:
            logger.warning(
                "Capteurs en doublon ignorés avant start: %s",
                ", ".join(duplicates),
            )

        t0 = time.monotonic()
        timestamps: list[float] = []
        dispatch_timestamps: dict[str, float] = {}
        per_sensor: dict[str, bool] = {}
        errors: dict[str, str] = {}

        # Pré-lecture de l'état avant démarrage, en parallèle.
        preflight_states = await asyncio.gather(*[_read_state_or_none(s, critical=False) for s in sensors])

        def _state_name(state: Optional[int]) -> str:
            if state is None:
                return "indisponible"
            return STATE_NAMES.get(state, f"0x{state:02x}")

        async def _start_one(sensor: DotSensor, pre_state: Optional[int]) -> tuple[bool, str]:
            if pre_state is None:
                return False, "état capteur indisponible avant commande"
            if pre_state not in (STATE_IDLE, STATE_RECORDING):
                return False, f"état non autorisé avant démarrage : {_state_name(pre_state)}"
            if pre_state == STATE_RECORDING:
                dispatch_timestamps[sensor.address] = time.monotonic()
                return True, ""

            try:
                await sensor.cmd_start_recording(wait_ack=False, critical=False)
                dispatch_timestamps[sensor.address] = time.monotonic()
            except DotError as exc:
                logger.warning("[%s] start rapide échoué, fallback ACK: %s", sensor.name, exc)
                try:
                    await sensor.cmd_start_recording(wait_ack=True, critical=False)
                    dispatch_timestamps[sensor.address] = time.monotonic()
                except DotError as exc2:
                    return False, str(exc2)

            await asyncio.sleep(0.20)
            confirmed_state = await _read_state_or_none(sensor, critical=False)
            if confirmed_state == STATE_RECORDING:
                return True, ""
            if confirmed_state is None:
                logger.warning(
                    "[%s] start_recording: confirmation indisponible, commande acceptée.",
                    sensor.name,
                )
                return True, ""

            # Fallback de robustesse : si le mode rapide n'a pas basculé,
            # retenter en mode ACK.
            try:
                await sensor.cmd_start_recording(wait_ack=True, critical=False)
                dispatch_timestamps[sensor.address] = time.monotonic()
                await asyncio.sleep(0.20)
                confirmed_state = await _read_state_or_none(sensor, critical=False)
                if confirmed_state == STATE_RECORDING:
                    return True, ""
            except DotError as exc2:
                return False, str(exc2)

            return False, f"démarrage non confirmé (état={confirmed_state:#04x})"

        logger.info("Lancement quasi simultané du START sur %d capteur(s)...", len(sensors))
        outcomes = await asyncio.gather(*[_start_one(sensor, pre_state) for sensor, pre_state in zip(sensors, preflight_states)])

        for sensor, (ok, err) in zip(sensors, outcomes):
            per_sensor[sensor.address] = ok
            if not ok:
                errors[sensor.address] = err

        if dispatch_timestamps:
            timestamps = list(dispatch_timestamps.values())

        jitter_ms: Optional[float] = None
        if len(timestamps) >= 2:
            jitter_ms = (max(timestamps) - min(timestamps)) * 1000

        result = RecordingResult(
            action="start",
            success=all(per_sensor.values()),
            per_sensor=per_sensor,
            errors=errors,
            jitter_ms=jitter_ms,
            total_duration_ms=(time.monotonic() - t0) * 1000,
        )
        logger.info("%s", result)
        return result


async def stop_all(sensors: list[DotSensor]) -> RecordingResult:
    """
    Arrête l'enregistrement sur tous les capteurs simultanément.

    Particularité : si l'ACK applicatif du stop n'est pas reçu (congestion BLE
    sur beaucoup de capteurs simultanés), on vérifie l'état réel du capteur.
    Si le capteur est déjà en IDLE, on considère le stop comme réussi.
    """
    async with _RECORDING_OP_LOCK:
        if not sensors:
            return RecordingResult("stop", True, {}, {})

        sensors, duplicates = _normalize_sensors(sensors)

        logger.info("STOP RECORDING sur %d capteur(s)...", len(sensors))
        if duplicates:
            logger.warning(
                "Capteurs en doublon ignorés avant stop: %s",
                ", ".join(duplicates),
            )

        t0 = time.monotonic()
        timestamps: list[float] = []
        stale_ack_accepted: dict[str, int] = {}

        # Group sensors by adapter to serialize operations per dongle
        adapters: dict[str, list[DotSensor]] = {}
        for s in sensors:
            key = s.adapter.name if s.adapter else "local"
            adapters.setdefault(key, []).append(s)

        outcomes: list[tuple[bool, str]] = []
        for adapter_name, group in adapters.items():
            logger.info("[%s] stop group %d capteur(s)", adapter_name, len(group))
            for s in group:
                res = await _record_one(s, "stop", timestamps, stale_ack_accepted)
                outcomes.append(res)
                await asyncio.sleep(_STAGGER_SEC)
            await asyncio.sleep(_GROUP_COOLDOWN_SEC)

        if stale_ack_accepted:
            total = sum(stale_ack_accepted.values())
            details = ", ".join(
                f"{addr}×{count}" for addr, count in sorted(stale_ack_accepted.items())
            )
            logger.warning(
                "stop_recording: %d ACK périmé(s) accepté(s) via état réel IDLE [%s]",
                total,
                details,
            )

        per_sensor: dict[str, bool] = {}
        errors: dict[str, str] = {}
        retry_sensors: list[DotSensor] = []

        for sensor, (ok, err) in zip(sensors, outcomes):
            per_sensor[sensor.address] = ok
            if not ok:
                errors[sensor.address] = err
                retry_sensors.append(sensor)

        # Passe de récupération : si l'ACK n'est pas fiable, on revalide l'état
        # réel. En production, on ne considère pas un doute comme un succès.
        if retry_sensors:
            await asyncio.sleep(2.0)
            for sensor in retry_sensors:
                try:
                    state = await _read_state(sensor)
                    if state == STATE_IDLE:
                        per_sensor[sensor.address] = True
                        errors.pop(sensor.address, None)
                        timestamps.append(time.monotonic())
                        logger.info("[%s] Retry stop validé par état IDLE", sensor.name)
                        continue

                    await sensor.cmd_stop_recording()
                    per_sensor[sensor.address] = True
                    errors.pop(sensor.address, None)
                    timestamps.append(time.monotonic())
                    logger.info("[%s] Retry stop OK", sensor.name)
                except Exception as exc2:
                    logger.error("[%s] Retry stop échoué : %s", sensor.name, exc2)

        result = RecordingResult(
            action="stop",
            success=all(per_sensor.values()),
            per_sensor=per_sensor,
            errors=errors,
            total_duration_ms=(time.monotonic() - t0) * 1000,
        )
        logger.info("%s", result)
        return result


async def wait_duration(seconds: float, label: str = "Enregistrement") -> None:
    """
    Attend `seconds` secondes avec affichage d'une barre de progression simple.
    """
    interval = 1.0
    elapsed = 0.0
    while elapsed < seconds:
        remaining = seconds - elapsed
        pct = int(elapsed / seconds * 40)
        bar = "█" * pct + "░" * (40 - pct)
        print(
            f"\r{label} [{bar}] {elapsed:.0f}s / {seconds:.0f}s  ",
            end="",
            flush=True,
        )
        await asyncio.sleep(min(interval, remaining))
        elapsed += interval
    print(f"\r{label} [{'█' * 40}] {seconds:.0f}s — terminé.           ")


async def wait_duration_with_health_check(
    sensors: list[DotSensor],
    seconds: float,
    label: str = "Enregistrement",
    check_interval: float = 2.0,
) -> RecordingHealthResult:
    """
    Attend pendant la capture tout en vérifiant régulièrement que tous les
    capteurs restent en état RECORDING.

    Cette surveillance ne prouve pas la cohérence temporelle parfaite, mais
    elle détecte les pertes de capture ou les capteurs qui quittent l'état
    d'enregistrement pendant la fenêtre de mesure.
    """
    if not sensors:
        await wait_duration(seconds, label=label)
        return RecordingHealthResult(True, 0)

    interval = 1.0
    elapsed = 0.0
    next_check = 0.0
    checks = 0
    problematic: set[str] = set()
    first_issues: dict[str, str] = {}
    diagnostics: list[str] = []

    while elapsed < seconds:
        remaining = seconds - elapsed
        pct = int(elapsed / seconds * 40) if seconds > 0 else 40
        bar = "█" * pct + "░" * (40 - pct)
        print(
            f"\r{label} [{bar}] {elapsed:.0f}s / {seconds:.0f}s  ",
            end="",
            flush=True,
        )

        step = min(interval, remaining)
        await asyncio.sleep(step)
        elapsed += step

        if elapsed + 1e-9 >= next_check or elapsed >= seconds:
            next_check += check_interval
            checks += 1
            states, issues = await _check_recording_health(sensors)
            for sensor in sensors:
                state = states.get(sensor.address)
                if state != STATE_RECORDING:
                    problematic.add(sensor.address)
                    if sensor.address not in first_issues:
                        if state is None:
                            first_issues[sensor.address] = "état indisponible"
                        else:
                            first_issues[sensor.address] = STATE_NAMES.get(state, f"0x{state:02x}")
            if issues:
                diagnostics.append(
                    f"Contrôle #{checks}: " + ", ".join(issues)
                )

    print(f"\r{label} [{'█' * 40}] {seconds:.0f}s — terminé.           ")

    success = not problematic
    result = RecordingHealthResult(
        success=success,
        checks=checks,
        problematic_sensors=sorted(problematic),
        first_issues=first_issues,
        diagnostics=diagnostics,
    )
    logger.info("Watchdog capture : %s", result)
    return result
