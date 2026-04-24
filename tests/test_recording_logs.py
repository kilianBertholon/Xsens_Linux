#!/usr/bin/env python3
"""
test_recording_logs.py — Vérifie l'agrégation des logs ACK périmés.

Ce test ne nécessite aucun capteur physique. Il simule des capteurs dont
start/stop lèvent une DotError, mais avec état réel correctement basculé,
ce qui déclenche le fallback d'acceptation côté recording.py.

Exécution :
    python tests/test_recording_logs.py
"""

import asyncio
import logging
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

if "bleak" not in sys.modules:
    bleak_module = types.ModuleType("bleak")

    class _BleakClient:
        pass

    bleak_module.BleakClient = _BleakClient
    sys.modules["bleak"] = bleak_module

if "bleak.exc" not in sys.modules:
    bleak_exc_module = types.ModuleType("bleak.exc")

    class _BleakError(Exception):
        pass

    bleak_exc_module.BleakError = _BleakError
    sys.modules["bleak.exc"] = bleak_exc_module

if "bleak.backends" not in sys.modules:
    sys.modules["bleak.backends"] = types.ModuleType("bleak.backends")

if "bleak.backends.characteristic" not in sys.modules:
    bleak_char_module = types.ModuleType("bleak.backends.characteristic")

    class _BleakGATTCharacteristic:
        pass

    bleak_char_module.BleakGATTCharacteristic = _BleakGATTCharacteristic
    sys.modules["bleak.backends.characteristic"] = bleak_char_module

from xdot_manager.recording import start_all, stop_all
from xdot_manager.sensor import DotError, STATE_IDLE, STATE_RECORDING


class _FakeSensor:
    def __init__(self, address: str) -> None:
        self.address = address.upper()
        self.name = self.address[-5:]
        self.adapter = None
        self._state = STATE_IDLE

    async def cmd_get_state(self) -> int:
        return self._state

    async def cmd_start_recording(self) -> None:
        self._state = STATE_RECORDING
        raise DotError(f"[{self.name}] ACK stale simulé sur start")

    async def cmd_stop_recording(self) -> None:
        self._state = STATE_IDLE
        raise DotError(f"[{self.name}] ACK stale simulé sur stop")


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


async def _run() -> None:
    logger = logging.getLogger("xdot_manager.recording")
    old_level = logger.level
    old_handlers = list(logger.handlers)
    old_propagate = logger.propagate

    capture = _ListHandler()
    capture.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))

    logger.handlers = [capture]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    try:
        sensors = [_FakeSensor("D4:22:CD:00:AA:01"), _FakeSensor("D4:22:CD:00:AA:02")]

        start_res = await start_all(sensors)
        if not start_res.success:
            raise AssertionError(f"start_all devrait réussir via fallback: {start_res}")

        stop_res = await stop_all(sensors)
        if not stop_res.success:
            raise AssertionError(f"stop_all devrait réussir via fallback: {stop_res}")

        msgs = capture.messages

        per_sensor_spam = [m for m in msgs if "ACK périmé accepté car l'état réel" in m]
        if per_sensor_spam:
            raise AssertionError(
                "Logs unitaires par capteur encore présents (anti-spam régressé):\n"
                + "\n".join(per_sensor_spam)
            )

        start_agg = [m for m in msgs if "start_recording:" in m and "ACK périmé(s) accepté(s)" in m]
        stop_agg = [m for m in msgs if "stop_recording:" in m and "ACK périmé(s) accepté(s)" in m]

        if len(start_agg) != 1:
            raise AssertionError(f"Attendu 1 log agrégé start, obtenu {len(start_agg)}: {start_agg}")
        if len(stop_agg) != 1:
            raise AssertionError(f"Attendu 1 log agrégé stop, obtenu {len(stop_agg)}: {stop_agg}")

        print("✓ OK — agrégation des logs ACK périmés validée (start/stop)")

    finally:
        logger.handlers = old_handlers
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
