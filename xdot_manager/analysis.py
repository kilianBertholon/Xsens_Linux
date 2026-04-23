"""
Analyse post-enregistrement : mesure du jitter de synchronisation.

Lit les fichiers CSV exportés et calcule l'écart temporel entre le premier
échantillon de chaque capteur — indicateur de qualité de la synchronisation.

Seuil de référence (spéc Xsens DOT) : ≤ 25 ms pour une sync correcte
(≈ 1 échantillon à 40 Hz ; à 120 Hz, ce seuil reste conservateur).
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Seuil de synchronisation acceptable (ms)
JITTER_THRESHOLD_MS = 25.0


# ---------------------------------------------------------------------------
# Résultat d'analyse
# ---------------------------------------------------------------------------

@dataclass
class JitterResult:
    """Résultat de l'analyse de synchronisation sur un groupe de capteurs."""

    # timestamp_ms du premier échantillon de chaque capteur (adresse → ms)
    first_timestamps: dict[str, float] = field(default_factory=dict)

    # Jitter maximum observé (ms)
    jitter_max_ms: float = 0.0

    # Capteur de référence (min timestamp)
    root_address: str = ""

    # Nb de capteurs analysés
    n_sensors: int = 0

    # Nb de fichiers CSV lus avec succès
    n_ok: int = 0

    # Erreurs par adresse
    errors: dict[str, str] = field(default_factory=dict)

    # Détails de diagnostic (messages lisibles)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.jitter_max_ms <= JITTER_THRESHOLD_MS and self.n_ok >= 2

    @property
    def offsets_ms(self) -> dict[str, float]:
        """Offset de chaque capteur par rapport au root (ms)."""
        if not self.root_address or self.root_address not in self.first_timestamps:
            return {}
        t_ref = self.first_timestamps[self.root_address]
        return {
            addr: ts - t_ref
            for addr, ts in sorted(self.first_timestamps.items())
        }

    def __str__(self) -> str:
        state = "OK ✓" if self.success else "⚠ DÉGRADÉ"
        return (
            f"Jitter max : {self.jitter_max_ms:.1f} ms "
            f"(seuil {JITTER_THRESHOLD_MS:.0f} ms) — {state} "
            f"— {self.n_ok}/{self.n_sensors} capteurs"
        )


# ---------------------------------------------------------------------------
# Lecture du premier timestamp d'un CSV exporté
# ---------------------------------------------------------------------------

def _read_first_timestamp(csv_path: Path) -> Optional[float]:
    """
    Lit la valeur de la colonne 'timestamp_ms' de la première ligne de données.
    Retourne None si la colonne est absente ou le fichier vide.
    """
    try:
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None or "timestamp_ms" not in reader.fieldnames:
                logger.debug("Pas de colonne timestamp_ms dans %s", csv_path.name)
                return None
            for row in reader:
                val = row.get("timestamp_ms", "").strip()
                if val:
                    return float(val)
    except Exception as exc:
        logger.warning("Impossible de lire %s : %s", csv_path, exc)
    return None


def _first_timestamp_for_address(output_dir: Path, addr: str) -> tuple[Optional[float], str]:
    """Retourne (timestamp, reason) pour une adresse capteur.

    reason est vide si timestamp trouvé, sinon contient la cause.
    """
    addr_clean = addr.replace(":", "-")
    existing_files: list[Path] = []
    for file_idx in range(1, 20):
        csv_path = output_dir / f"{addr_clean}_file{file_idx:02d}.csv"
        if not csv_path.exists():
            continue
        existing_files.append(csv_path)
        ts = _read_first_timestamp(csv_path)
        if ts is not None:
            return ts, ""

    if not existing_files:
        return None, "Aucun fichier CSV trouvé (file01..file19)"
    return None, "CSV trouvé(s) mais sans colonne/valeur timestamp_ms lisible"


# ---------------------------------------------------------------------------
# API principale
# ---------------------------------------------------------------------------

def analyze_sync_jitter(
    output_dir: Path,
    addresses: list[str],
) -> JitterResult:
    """
    Analyse le jitter de synchronisation à partir des CSV exportés.

    Pour chaque adresse, cherche le fichier `<ADDR_TIRETS>_file01.csv`
    (premier fichier exporté) dans `output_dir` et lit le premier timestamp.

    Args:
        output_dir : répertoire contenant les CSV (ex: ./xdot_export).
        addresses  : liste d'adresses MAC des capteurs (ex: "D4:22:CD:00:49:C7").

    Returns:
        JitterResult avec les timestamps et le jitter calculé.
    """
    result = JitterResult(n_sensors=len(addresses))
    timestamps: dict[str, float] = {}

    if not output_dir.exists():
        result.diagnostics.append(f"Répertoire introuvable : {output_dir}")
        for addr in addresses:
            result.errors[addr] = "Répertoire export absent"
        return result

    for addr in addresses:
        ts, reason = _first_timestamp_for_address(output_dir, addr)
        if ts is not None:
            timestamps[addr] = ts
            result.n_ok += 1
        else:
            result.errors[addr] = reason
            logger.warning("[%s] Timestamp indisponible dans output_dir=%s : %s", addr, output_dir, reason)

    result.first_timestamps = timestamps

    if len(timestamps) >= 2:
        t_min = min(timestamps.values())
        t_max = max(timestamps.values())
        result.jitter_max_ms = t_max - t_min
        # Capteur root = celui avec le timestamp le plus tôt
        result.root_address = min(timestamps, key=timestamps.__getitem__)
    elif len(timestamps) == 1:
        result.jitter_max_ms = 0.0
        result.root_address = next(iter(timestamps))

    if result.n_ok < 2:
        result.diagnostics.append(
            "Analyse impossible : moins de 2 capteurs avec timestamp exploitable."
        )
        result.diagnostics.append(
            "Vérifier qu'un export a été fait et que le payload inclut la colonne timestamp_ms."
        )
    elif result.jitter_max_ms > JITTER_THRESHOLD_MS:
        result.diagnostics.append(
            f"Jitter au-dessus du seuil ({result.jitter_max_ms:.1f} ms > {JITTER_THRESHOLD_MS:.0f} ms)."
        )
    else:
        result.diagnostics.append("Synchronisation dans la plage attendue.")

    logger.info("Analyse jitter : %s", result)
    return result
