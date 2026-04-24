"""
Scanner BLE multi-adaptateur pour les capteurs Xsens DOT / Movella DOT.

Stratégie :
- Lance un BleakScanner par adaptateur disponible en parallèle.
- Chaque scanner tourne pendant `timeout` secondes.
- Résultats dédupliqués par adresse MAC (on garde le plus fort RSSI).
- Retourne une liste de DotDevice triée par RSSI décroissant.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .adapters import (
    BtAdapter,
    list_adapters,
    assign_sensors_round_robin,
    SAFE_DEFAULT_MAX_PER_ADAPTER,
)
from .protocol.gatt import DOT_NAMES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass résultat
# ---------------------------------------------------------------------------

@dataclass
class DotDevice:
    address: str            # MAC ex. "D4:22:CD:00:49:C7"
    name: str               # "Xsens DOT" ou "Movella DOT"
    rssi: int               # dBm (négatif, plus proche de 0 = meilleur signal)
    adapter: Optional[BtAdapter] = field(default=None, repr=False)
    ble_device: Optional[BLEDevice] = field(default=None, repr=False)

    def __str__(self) -> str:
        adapter_name = self.adapter.name if self.adapter else "?"
        return f"{self.address}  {self.name:<14}  RSSI={self.rssi:>4} dBm  [{adapter_name}]"


# ---------------------------------------------------------------------------
# Scan sur un adaptateur unique
# ---------------------------------------------------------------------------

async def _scan_one_adapter(
    adapter: BtAdapter,
    timeout: float,
) -> dict[str, DotDevice]:
    """
    Lance un scan BLE sur un seul adaptateur.
    Retourne un dict {address → DotDevice}.
    """
    found: dict[str, DotDevice] = {}

    def detection_callback(device: BLEDevice, adv: AdvertisementData) -> None:
        name = device.name or ""
        if not any(dot_name in name for dot_name in DOT_NAMES):
            return
        addr = device.address.upper()
        rssi = adv.rssi if adv.rssi is not None else -127
        existing = found.get(addr)
        if existing is None or rssi > existing.rssi:
            found[addr] = DotDevice(
                address=addr,
                name=name,
                rssi=rssi,
                adapter=adapter,
                ble_device=device,
            )
            logger.debug("[%s] DOT trouvé : %s RSSI=%d", adapter.name, addr, rssi)

    scanner = BleakScanner(
        detection_callback=detection_callback,
        adapter=adapter.bleak_id,
    )
    try:
        await scanner.start()
        await asyncio.sleep(timeout)
        await scanner.stop()
    except Exception as exc:
        logger.warning("[%s] Erreur scan : %s", adapter.name, exc)

    logger.info("[%s] %d DOT détecté(s)", adapter.name, len(found))
    return found


# ---------------------------------------------------------------------------
# Scan multi-adaptateur
# ---------------------------------------------------------------------------

async def scan_for_dots(
    timeout: float = 5.0,
    adapters: Optional[list[BtAdapter]] = None,
    max_per_adapter: int = SAFE_DEFAULT_MAX_PER_ADAPTER,
) -> list[DotDevice]:
    """
    Scan BLE en parallèle sur tous les adaptateurs disponibles.

    Args:
        timeout         : durée du scan par adaptateur (secondes).
        adapters        : liste d'adaptateurs à utiliser.
                          Si None, utilise list_adapters() automatiquement.
        max_per_adapter : nombre max de capteurs à assigner par adaptateur.

    Returns:
        Liste de DotDevice dédupliqués, triés par RSSI décroissant.
        Chaque DotDevice.adapter pointe vers l'adaptateur qui l'a détecté
        (et sera utilisé pour la connexion).
    """
    if adapters is None:
        adapters = list_adapters()

    if not adapters:
        logger.warning("Aucun adaptateur Bluetooth trouvé.")
        return []

    logger.info(
        "Scan sur %d adaptateur(s) : %s (timeout=%.1fs)",
        len(adapters),
        [a.name for a in adapters],
        timeout,
    )

    # Lancer tous les scans en parallèle
    tasks = [_scan_one_adapter(a, timeout) for a in adapters]
    results: list[dict[str, DotDevice]] = await asyncio.gather(*tasks)

    # Fusion + déduplication (garde meilleur RSSI)
    merged: dict[str, DotDevice] = {}
    for partial in results:
        for addr, dev in partial.items():
            existing = merged.get(addr)
            if existing is None or dev.rssi > existing.rssi:
                merged[addr] = dev

    devices = sorted(merged.values(), key=lambda d: d.rssi, reverse=True)

    # Ré-assigner les adaptateurs en round-robin basé sur les capteurs trouvés
    if devices:
        addresses = [d.address for d in devices]
        try:
            assignment = assign_sensors_round_robin(addresses, adapters, max_per_adapter)
            for dev in devices:
                dev.adapter = assignment[dev.address]
        except RuntimeError as exc:
            logger.warning("Ré-assignation impossible : %s", exc)

    logger.info(
        "Total : %d Xsens DOT détecté(s) sur %d adaptateur(s)",
        len(devices),
        len(adapters),
    )
    return devices


# ---------------------------------------------------------------------------
# Affichage
# ---------------------------------------------------------------------------

def print_scan_results(devices: list[DotDevice]) -> None:
    if not devices:
        print("Aucun capteur Xsens DOT détecté.")
        return

    print(f"\n{'#':<4} {'Adresse MAC':<20} {'Nom':<16} {'RSSI':>6}  {'Adaptateur'}")
    print("-" * 62)
    for i, dev in enumerate(devices, 1):
        adapter_name = dev.adapter.name if dev.adapter else "?"
        print(f"{i:<4} {dev.address:<20} {dev.name:<16} {dev.rssi:>5} dBm  {adapter_name}")
    print()
