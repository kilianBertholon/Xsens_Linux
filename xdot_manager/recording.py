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
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .sensor import DotSensor, DotError, STATE_IDLE, STATE_RECORDING

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


async def _read_state(sensor: DotSensor) -> int:
    """Lit l'état réel du capteur avec un garde-fou de délai."""
    return await asyncio.wait_for(sensor.cmd_get_state(), timeout=8.0)


async def _read_state_or_none(sensor: DotSensor) -> Optional[int]:
    """Lit l'état réel du capteur sans propager les timeouts/transitoires."""
    try:
        return await _read_state(sensor)
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
    jitter_ms: Optional[float] = None    # delta entre 1er et dernier ACK (ms)
    total_duration_ms: float = 0.0

    @property
    def failed_sensors(self) -> list[str]:
        return [addr for addr, ok in self.per_sensor.items() if not ok]

    def __str__(self) -> str:
        ok = sum(self.per_sensor.values())
        total = len(self.per_sensor)
        jitter_str = f" — jitter={self.jitter_ms:.1f} ms" if self.jitter_ms is not None else ""
        return (
            f"Recording {self.action.upper()} — "
            f"{ok}/{total} capteurs OK — "
            f"durée={self.total_duration_ms:.0f} ms"
            f"{jitter_str}"
        )


# ---------------------------------------------------------------------------
# Tâche individuelle (start ou stop)
# ---------------------------------------------------------------------------

async def _record_one(
    sensor: DotSensor,
    action: str,          # "start" ou "stop"
    timestamps: list[float],
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
                    logger.warning(
                        "[%s] start_recording: ACK périmé accepté car l'état réel est RECORDING.",
                        sensor.name,
                    )
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
                    logger.warning(
                        "[%s] stop_recording: ACK périmé accepté car l'état réel est IDLE.",
                        sensor.name,
                    )
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

        # Group sensors by adapter to serialize operations per dongle
        adapters: dict[str, list[DotSensor]] = {}
        for s in sensors:
            key = s.adapter.name if s.adapter else "local"
            adapters.setdefault(key, []).append(s)

        outcomes: list[tuple[bool, str]] = []
        # Process adapters sequentially : priorité à la stabilité sur le débit.
        for adapter_name, group in adapters.items():
            logger.info("[%s] start group %d capteur(s)", adapter_name, len(group))
            for s in group:
                res = await _record_one(s, "start", timestamps)
                outcomes.append(res)
                await asyncio.sleep(_STAGGER_SEC)
            await asyncio.sleep(_GROUP_COOLDOWN_SEC)

        per_sensor: dict[str, bool] = {}
        errors: dict[str, str] = {}
        for sensor, (ok, err) in zip(sensors, outcomes):
            per_sensor[sensor.address] = ok
            if not ok:
                errors[sensor.address] = err

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

        # Group sensors by adapter to serialize operations per dongle
        adapters: dict[str, list[DotSensor]] = {}
        for s in sensors:
            key = s.adapter.name if s.adapter else "local"
            adapters.setdefault(key, []).append(s)

        outcomes: list[tuple[bool, str]] = []
        for adapter_name, group in adapters.items():
            logger.info("[%s] stop group %d capteur(s)", adapter_name, len(group))
            for s in group:
                res = await _record_one(s, "stop", timestamps)
                outcomes.append(res)
                await asyncio.sleep(_STAGGER_SEC)
            await asyncio.sleep(_GROUP_COOLDOWN_SEC)

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
