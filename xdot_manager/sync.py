"""
Synchronisation temporelle entre capteurs Xsens DOT.

Le protocole Xsens utilise un capteur "root" dont l'adresse MAC est
broadcast à tous les autres capteurs via la commande start_syncing().
Les capteurs esclaves s'alignent ensuite en radio sur le root.

Séquence :
  1. Sélectionner le capteur root (le premier par défaut, ou choix manuel).
  2. Envoyer start_syncing(root_mac) à TOUS les capteurs simultanément.
  3. Attendre SYNC_SETTLE_TIME secondes (délai radio).
  4. Vérifier l'état de chaque capteur (STATE_SYNCING ou STATE_IDLE).
  5. Retourner un SyncResult avec le détail par capteur.

Note : après la sync, les capteurs doivent rester connectés.
L'enregistrement doit démarrer IMMÉDIATEMENT après la sync pour
minimiser le jitter.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .sensor import DotSensor, DotState, DotError, DotTimeoutError
from .protocol.gatt import SYNC_SETTLE_TIME, STATE_IDLE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Résultat de la synchronisation
# ---------------------------------------------------------------------------

@dataclass
class SyncResult:
    root_address: str
    success: bool
    duration_ms: float
    per_sensor: dict[str, bool] = field(default_factory=dict)   # addr → ok
    errors: dict[str, str]      = field(default_factory=dict)   # addr → message

    @property
    def failed_sensors(self) -> list[str]:
        return [addr for addr, ok in self.per_sensor.items() if not ok]

    def __str__(self) -> str:
        ok = sum(self.per_sensor.values())
        total = len(self.per_sensor)
        return (
            f"Sync {'OK' if self.success else 'ÉCHOUÉE'} — "
            f"{ok}/{total} capteurs synchronisés — "
            f"root={self.root_address} — "
            f"durée={self.duration_ms:.0f} ms"
        )


# ---------------------------------------------------------------------------
# Envoi de la commande de sync à un capteur (tâche individuelle)
# ---------------------------------------------------------------------------

async def _sync_one(sensor: DotSensor, root_mac: str, read_ack: bool = True) -> tuple[bool, str]:
    """
    Envoie start_syncing(root_mac) à un capteur.
    Retourne (success, error_message).
    """
    try:
        await sensor.cmd_send_syncing(root_mac, read_ack=read_ack)
        return True, ""
    except DotError as exc:
        msg = str(exc)
        logger.error("[%s] Erreur sync : %s", sensor.name, msg)
        return False, msg
    except Exception as exc:
        msg = f"Erreur inattendue : {exc}"
        logger.exception("[%s] %s", sensor.name, msg)
        return False, msg


# ---------------------------------------------------------------------------
# API principale
# ---------------------------------------------------------------------------

async def synchronize_sensors(
    sensors: list[DotSensor],
    root_index: int = 0,
    settle_time: float = SYNC_SETTLE_TIME,
    verify_state: bool = True,
    wait_for_idle: bool = True,
    idle_poll_interval: float = 0.5,
    idle_timeout: float = 30.0,
    progress_callback: Optional[Callable[[str, str], None]] = None,
    await_sync_ack: bool = False,
) -> SyncResult:
    """
    Synchronise un groupe de capteurs Xsens DOT.

    Args:
        sensors           : liste des DotSensor connectés.
        root_index        : index du capteur root (défaut : 0).
        settle_time       : attente après envoi des commandes (secondes).
        verify_state      : si True, lit l'état après settle.
        wait_for_idle     : si True, attend que chaque capteur revienne en
                            STATE_IDLE avant de rendre la main (nécessaire
                            avant de lancer start_recording).
        idle_poll_interval: intervalle de polling pour l'état Idle (s).
        idle_timeout      : timeout maximum pour attendre Idle (s).
        progress_callback : appelable optionnel (address, status) appelé
                            à chaque évènement par capteur.
                            status ∈ {"syncing", "synced", "idle", "error"}.
        await_sync_ack    : si False (défaut), envoie start_syncing sans attendre
                    l'ACK applicatif pour maximiser la simultanéité LED.

    Returns:
        SyncResult avec le détail de la réussite par capteur.
    """
    if not sensors:
        raise ValueError("La liste de capteurs est vide.")

    root = sensors[root_index]
    root_mac = root.address
    logger.info(
        "Synchronisation de %d capteur(s) — root=%s",
        len(sensors),
        root_mac,
    )

    t_start = time.monotonic()

    # ---------------------------------------------------------------
    # Étape 0 : forcer la désynchronisation si déjà en cours,
    #           puis attendre que tous les capteurs soient en Idle.
    # ---------------------------------------------------------------
    logger.info("Désynchronisation forcée avant re-sync...")
    await stop_sync_all(sensors)

    # Attendre le retour en Idle — indispensable si les capteurs étaient déjà
    # synchronisés : start_syncing sera rejeté (result=0x06) tant qu'ils n'ont
    # pas quitté leur état interne de sync.
    logger.info("Attente retour en Idle après stop_syncing (timeout=15s)...")
    pre_idle = [_wait_for_idle(s, 0.5, 15.0) for s in sensors]
    pre_idle_results = await asyncio.gather(*pre_idle, return_exceptions=True)
    n_idle = sum(
        1 for r in pre_idle_results
        if not isinstance(r, Exception) and r is True
    )
    logger.info("%d/%d capteur(s) en Idle avant re-sync.", n_idle, len(sensors))

    # Étape 1 : envoyer start_syncing à tous les capteurs en parallèle
    # Wrapper pour appeler le callback avant/après chaque commande individuelle
    async def _sync_with_cb(sensor: DotSensor) -> tuple[bool, str]:
        if progress_callback:
            progress_callback(sensor.address, "syncing")
        ok, err = await _sync_one(sensor, root_mac, read_ack=await_sync_ack)
        if progress_callback:
            progress_callback(sensor.address, "synced" if ok else "error")
        return ok, err

    tasks = [_sync_with_cb(sensor) for sensor in sensors]
    outcomes = await asyncio.gather(*tasks, return_exceptions=False)

    per_sensor: dict[str, bool] = {}
    errors: dict[str, str] = {}
    for sensor, (ok, err) in zip(sensors, outcomes):
        per_sensor[sensor.address] = ok
        if not ok:
            errors[sensor.address] = err

    # Étape 2 : attendre la stabilisation radio
    if settle_time > 0:
        logger.info("Attente stabilisation sync (%.1fs)...", settle_time)
        await asyncio.sleep(settle_time)

    # Étape 3 : attendre que tous les capteurs reviennent en Idle
    if wait_for_idle:
        logger.info("Attente retour en Idle (timeout=%.0fs)...", idle_timeout)
        idle_tasks = [
            _wait_for_idle(s, idle_poll_interval, idle_timeout)
            for s in sensors
        ]
        idle_results = await asyncio.gather(*idle_tasks, return_exceptions=True)
        for sensor, result in zip(sensors, idle_results):
            if isinstance(result, Exception):
                per_sensor[sensor.address] = False
                errors[sensor.address] = str(result)
                logger.warning("[%s] Retour Idle échoué : %s", sensor.name, result)
                if progress_callback:
                    progress_callback(sensor.address, "error")
            elif not result:
                logger.warning("[%s] Capteur non revenu en Idle après %.0fs", sensor.name, idle_timeout)
                if progress_callback:
                    progress_callback(sensor.address, "error")
            else:
                if progress_callback:
                    progress_callback(sensor.address, "idle")

    # Étape 4 : vérification optionnelle de l'état
    elif verify_state:
        verify_tasks = [_verify_sync_state(s) for s in sensors]
        verify_results = await asyncio.gather(*verify_tasks, return_exceptions=True)
        for sensor, result in zip(sensors, verify_results):
            if isinstance(result, Exception):
                per_sensor[sensor.address] = False
                errors[sensor.address] = str(result)
                logger.warning("[%s] Vérification état échouée : %s", sensor.name, result)
            else:
                if not result:
                    logger.warning("[%s] État après sync : non synchronisé", sensor.name)
                    per_sensor[sensor.address] = False

    duration_ms = (time.monotonic() - t_start) * 1000
    all_ok = all(per_sensor.values())
    result = SyncResult(
        root_address=root_mac,
        success=all_ok,
        duration_ms=duration_ms,
        per_sensor=per_sensor,
        errors=errors,
    )
    logger.info("%s", result)
    return result


async def synchronize_sensors_with_retry(
    sensors: list[DotSensor],
    root_index: int = 0,
    settle_time: float = SYNC_SETTLE_TIME,
    verify_state: bool = True,
    wait_for_idle: bool = True,
    idle_poll_interval: float = 0.5,
    idle_timeout: float = 30.0,
    progress_callback: Optional[Callable[[str, str], None]] = None,
    await_sync_ack: bool = False,
    retries: int = 2,
    retry_delay: float = 2.0,
) -> SyncResult:
    """
    Synchronise les capteurs avec tentatives supplémentaires si la première
    passe laisse des capteurs en échec.

    La stratégie est volontairement simple : on relance la sync complète
    après un court délai, car un échec partiel est souvent transitoire sur BLE.
    """
    last_result: SyncResult | None = None

    for attempt in range(1, max(1, retries) + 1):
        if attempt > 1:
            logger.info(
                "Nouvelle tentative de synchronisation %d/%d dans %.1fs...",
                attempt,
                retries,
                retry_delay,
            )
            await asyncio.sleep(retry_delay)

        last_result = await synchronize_sensors(
            sensors,
            root_index=root_index,
            settle_time=settle_time,
            verify_state=verify_state,
            wait_for_idle=wait_for_idle,
            idle_poll_interval=idle_poll_interval,
            idle_timeout=idle_timeout,
            progress_callback=progress_callback,
            await_sync_ack=await_sync_ack,
        )

        if last_result.success:
            if attempt > 1:
                logger.info("Synchronisation validée après %d tentative(s).", attempt)
            return last_result

        logger.warning(
            "Synchronisation incomplète après tentative %d/%d : %s",
            attempt,
            retries,
            last_result.failed_sensors,
        )

    assert last_result is not None
    return last_result


async def _verify_sync_state(sensor: DotSensor) -> bool:
    """
    Vérifie que le capteur est revenu en état Idle après la sync.
    La sync est transparente au sous-système flash — l'état doit être STATE_IDLE (0x06).
    """
    try:
        state_byte = await sensor.cmd_get_state()
        logger.debug("[%s] État = %#04x", sensor.name, state_byte)
        return state_byte == STATE_IDLE
    except DotError as exc:
        raise DotTimeoutError(f"Impossible de lire l'état : {exc}") from exc


async def _wait_for_idle(
    sensor: DotSensor,
    poll_interval: float,
    timeout: float,
) -> bool:
    """
    Attend que le capteur revienne en STATE_IDLE.
    Retourne True si Idle atteint avant le timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = await sensor.cmd_get_state()
            logger.debug("[%s] Poll état = %#04x", sensor.name, state)
            if state == STATE_IDLE:
                logger.info("[%s] Revenu en Idle.", sensor.name)
                return True
        except DotError as exc:
            logger.debug("[%s] Erreur lecture état pendant attente Idle : %s", sensor.name, exc)
        await asyncio.sleep(poll_interval)
    logger.warning("[%s] Timeout attente Idle (%.0fs).", sensor.name, timeout)
    return False


async def stop_sync_all(sensors: list[DotSensor]) -> None:
    """
    Envoie stop_syncing à tous les capteurs en parallèle.
    Les erreurs individuelles sont loggées mais ne font pas planter l'ensemble.
    """
    async def _stop_one(s: DotSensor) -> None:
        try:
            await s.cmd_stop_syncing()
        except DotError as exc:
            logger.warning("[%s] Erreur stop_syncing : %s", s.name, exc)

    await asyncio.gather(*[_stop_one(s) for s in sensors])
    logger.info("stop_syncing envoyé à %d capteur(s).", len(sensors))
