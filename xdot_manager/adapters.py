"""
Détection et gestion des adaptateurs Bluetooth sous Linux.

Chaque adaptateur hciX est représenté par un objet BtAdapter.
La fonction list_adapters() interroge /sys/class/bluetooth/ directement
(pas de dépendance à bluetoothctl ni à dbus-python).

Exemple d'utilisation :
    adapters = await list_adapters()
    for a in adapters:
        print(a.name, a.address, a.is_up)
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class BtAdapter:
    name: str           # ex. "hci0"
    address: str        # ex. "00:1A:7D:DA:71:11"
    is_up: bool = True

    # état interne : compteur de capteurs assignés (rempli par le scanner)
    assigned: int = field(default=0, repr=False)

    def __str__(self) -> str:
        status = "UP" if self.is_up else "DOWN"
        return f"{self.name} [{self.address}] {status} — {self.assigned} capteur(s) assigné(s)"

    # Utilisé par bleak comme identifiant d'adaptateur
    @property
    def bleak_id(self) -> str:
        """Retourne l'identifiant passé à BleakScanner / BleakClient."""
        return self.name   # ex. "hci0" — BlueZ l'accepte directement


# ---------------------------------------------------------------------------
# Lecture /sys/class/bluetooth/
# ---------------------------------------------------------------------------

def _read_sysfs_adapters() -> list[BtAdapter]:
    """
    Liste les adaptateurs Bluetooth disponibles.
    Stratégie :
      1. Lecture de /sys/class/bluetooth/ pour lister les hciX.
      2. Adresses MAC et état UP/DOWN obtenus via `hciconfig`.
    """
    bt_path = Path("/sys/class/bluetooth")
    if not bt_path.exists():
        return []

    # Noms des adaptateurs depuis sysfs
    # Note : BlueZ crée aussi des entrées hciX:N pour chaque connexion active.
    # On ne garde que les vrais adaptateurs (hci0, hci2, hci3…), pas les handles.
    _HCI_ADAPTER_RE = re.compile(r"^hci\d+$")
    hci_names = sorted(
        e.name for e in bt_path.iterdir() if _HCI_ADAPTER_RE.match(e.name)
    )
    if not hci_names:
        return []

    # Adresses et état depuis hciconfig (une seule invocation)
    info = _parse_hciconfig_all()

    adapters: list[BtAdapter] = []
    for name in hci_names:
        address = info.get(name, {}).get("address", "00:00:00:00:00:00")
        is_up   = info.get(name, {}).get("is_up", True)
        adapters.append(BtAdapter(name=name, address=address, is_up=is_up))

    return adapters


def _parse_hciconfig_all() -> dict[str, dict]:
    """
    Parse la sortie de `hciconfig -a` et retourne un dict
    {hciX: {address: str, is_up: bool}}.
    """
    result: dict[str, dict] = {}
    try:
        proc = subprocess.run(
            ["hciconfig", "-a"],
            capture_output=True, text=True, timeout=5,
        )
        current: str | None = None
        for line in proc.stdout.splitlines():
            # Ligne d'en-tête : "hci0:   Type: Primary  ..."
            hdr = re.match(r"^(hci\d+):", line)
            if hdr:
                current = hdr.group(1)
                result[current] = {"address": "00:00:00:00:00:00", "is_up": False}
                result[current]["is_up"] = "UP RUNNING" in line
                continue
            if current is None:
                continue
            # Ligne BD Address
            addr_m = re.search(r"BD Address:\s*([0-9A-Fa-f:]{17})", line)
            if addr_m:
                result[current]["address"] = addr_m.group(1).upper()
            # Ligne UP RUNNING (parfois sur une ligne séparée)
            if "UP RUNNING" in line:
                result[current]["is_up"] = True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return result


def _adapter_is_up(hci_name: str) -> bool:
    """
    Vérifie si l'adaptateur est actif via hciconfig (si disponible),
    sinon retourne True par défaut.
    """
    try:
        result = subprocess.run(
            ["hciconfig", hci_name],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return "UP RUNNING" in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # hciconfig absent — supposer actif
        return True


# ---------------------------------------------------------------------------
# Montée d'un adaptateur DOWN
# ---------------------------------------------------------------------------

def bring_up_adapter(adapter: BtAdapter) -> bool:
    """
    Tente de monter un adaptateur DOWN avec `hciconfig <name> up`.
    Retourne True si l'opération réussit.
    Nécessite les droits root / l'appartenance au groupe bluetooth.
    """
    try:
        result = subprocess.run(
            ["hciconfig", adapter.name, "up"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            adapter.is_up = True
            return True
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# API principale
# ---------------------------------------------------------------------------

def list_adapters(include_down: bool = False) -> list[BtAdapter]:
    """
    Retourne la liste des adaptateurs Bluetooth détectés sur le système.

    Args:
        include_down: si True, inclut les adaptateurs DOWN (désactivés).

    Returns:
        Liste de BtAdapter triée par nom (hci0, hci1, …).
    """
    adapters = _read_sysfs_adapters()
    if not include_down:
        adapters = [a for a in adapters if a.is_up]
    return adapters


def assign_sensors_round_robin(
    sensor_addresses: list[str],
    adapters: list[BtAdapter],
    max_per_adapter: int = 8,
) -> dict[str, BtAdapter]:
    """
    Répartit les capteurs entre les adaptateurs disponibles en round-robin.

    Args:
        sensor_addresses : liste d'adresses MAC des capteurs.
        adapters         : liste des adaptateurs disponibles.
        max_per_adapter  : nombre max de capteurs par adaptateur.

    Returns:
        Dictionnaire {adresse_capteur → BtAdapter}.

    Raises:
        RuntimeError si la capacité totale est insuffisante.
    """
    if not adapters:
        raise RuntimeError("Aucun adaptateur Bluetooth disponible.")

    capacity = len(adapters) * max_per_adapter
    if len(sensor_addresses) > capacity:
        raise RuntimeError(
            f"{len(sensor_addresses)} capteurs demandés mais capacité max = "
            f"{len(adapters)} adaptateurs × {max_per_adapter} = {capacity}."
        )

    assignment: dict[str, BtAdapter] = {}
    for i, addr in enumerate(sensor_addresses):
        adapter = adapters[i % len(adapters)]
        assignment[addr] = adapter
        adapter.assigned += 1

    return assignment


def assign_sensors_fixed(
    assignments: dict[str, str],
    adapters: list[BtAdapter],
) -> dict[str, BtAdapter]:
    """
    Répartition fixe à partir d'un dict {adresse_capteur → nom_adaptateur}.
    Utile pour la config JSON persistante.

    Raises:
        KeyError si un nom d'adaptateur est inconnu.
    """
    adapter_map = {a.name: a for a in adapters}
    result: dict[str, BtAdapter] = {}
    for sensor_addr, hci_name in assignments.items():
        if hci_name not in adapter_map:
            raise KeyError(f"Adaptateur {hci_name!r} non trouvé parmi {list(adapter_map)}")
        a = adapter_map[hci_name]
        result[sensor_addr] = a
        a.assigned += 1
    return result


def print_adapter_summary(adapters: list[BtAdapter]) -> None:
    """Affiche un tableau récapitulatif des adaptateurs (sans dépendance rich)."""
    print(f"\n{'Adaptateur':<10} {'Adresse MAC':<20} {'État':<8} {'Capteurs'}")
    print("-" * 52)
    for a in adapters:
        state = "UP" if a.is_up else "DOWN"
        print(f"{a.name:<10} {a.address:<20} {state:<8} {a.assigned}")
    print()
