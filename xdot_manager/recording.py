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
from dataclasses import dataclass, field
from typing import Optional

from .sensor import DotSensor, DotState, DotError

logger = logging.getLogger(__name__)


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
        if action == "start":
            await sensor.cmd_start_recording()
        else:
            await sensor.cmd_stop_recording()
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
    if not sensors:
        return RecordingResult("start", True, {}, {})

    logger.info("START RECORDING sur %d capteur(s)...", len(sensors))
    t0 = time.monotonic()
    timestamps: list[float] = []

    tasks = [_record_one(s, "start", timestamps) for s in sensors]
    outcomes = await asyncio.gather(*tasks)

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
    if not sensors:
        return RecordingResult("stop", True, {}, {})

    logger.info("STOP RECORDING sur %d capteur(s)...", len(sensors))
    t0 = time.monotonic()
    timestamps: list[float] = []

    tasks = [_record_one(s, "stop", timestamps) for s in sensors]
    outcomes = await asyncio.gather(*tasks)

    per_sensor: dict[str, bool] = {}
    errors: dict[str, str] = {}
    retry_sensors: list[DotSensor] = []

    for sensor, (ok, err) in zip(sensors, outcomes):
        per_sensor[sensor.address] = ok
        if not ok:
            errors[sensor.address] = err
            retry_sensors.append(sensor)

    # Passe de récupération : cmd_stop_recording poll maintenant l'état en
    # interne. Si elle a levé une exception, le capteur n'était pas en IDLE
    # après 12s. On tente un dernier stop individuel après une pause.
    if retry_sensors:
        await asyncio.sleep(2.0)
        for sensor in retry_sensors:
            try:
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
