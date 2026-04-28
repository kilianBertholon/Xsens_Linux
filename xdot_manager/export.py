"""
Export de la mémoire flash des capteurs Xsens DOT vers CSV.

Port Python de xdot-export/src/sensor.rs (section do_export).

Séquence pour un capteur :
  1. subscribe_notifications()
  2. get_state()  → vérifier STATE_IDLE
  3. select_export_data(types)  → ACK
  4. request_flash_info()  → notifications 0x51 jusqu'à 0x52
  5. Pour chaque fichier :
     a. request_file_info(n)  → notification 0x61
     b. request_file_data(n)  → notifications 0x71… jusqu'à 0x72
     c. Parser les paquets 0x71, écrire CSV
  6. stop_export_data()
  7. unsubscribe_notifications()

Format CSV : colonne par type de donnée, une ligne par échantillon.
Nom du fichier : <ADDRESS>_file<N>.csv
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .sensor import DotSensor, DotState, DotError, DotTimeoutError
from .protocol.gatt import (
    STATE_IDLE, STATE_RECORDING,
    REID_EXPORT_FLASH_INFO, REID_EXPORT_FLASH_INFO_DONE,
    REID_EXPORT_FILE_INFO, REID_EXPORT_FILE_INFO_DONE, REID_NO_RECORDING_FILE,
    REID_EXPORT_FILE_DATA, REID_EXPORT_FILE_DATA_DONE,
    DATA_TIMEOUT, MAX_EXPORT_DURATION,
    EXPORT_DATA_TYPES,
)
from .protocol.commands import (
    get_state, select_export_data,
    request_flash_info, request_file_info, request_file_data,
    stop_export_data,
    parse_notification_header,
)

logger = logging.getLogger(__name__)

# Groupes prédéfinis (noms de types comme dans EXPORT_DATA_TYPES)
PRESET_EULER      = ["timestamp", "euler"]
PRESET_QUATERNION = ["timestamp", "quaternion"]
PRESET_IMU        = ["timestamp", "acc", "ang_vel", "mag"]
PRESET_FULL       = list(EXPORT_DATA_TYPES.keys())

# Mapping nom payload CLI → liste de types
PAYLOAD_MAP: dict[str, list[str]] = {
    "euler":      PRESET_EULER,
    "quaternion": PRESET_QUATERNION,
    "imu":        PRESET_IMU,
    "full":       PRESET_FULL,
}


# ---------------------------------------------------------------------------
# Résultats
# ---------------------------------------------------------------------------

@dataclass
class FileExportResult:
    file_index: int
    sample_count: int
    output_path: Optional[Path]
    duration_s: float
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class SensorExportResult:
    address: str
    success: bool
    files: list[FileExportResult] = field(default_factory=list)
    error: Optional[str] = None
    total_samples: int = 0
    duration_s: float = 0.0

    def __str__(self) -> str:
        status = "OK" if self.success else f"ERREUR ({self.error})"
        return (
            f"{self.address} — {status} — "
            f"{self.total_samples} échantillons dans {len(self.files)} fichier(s) "
            f"({self.duration_s:.1f}s)"
        )


@dataclass
class FileMetadata:
    """Métadonnées d'un fichier flash (sans export des données)."""
    file_index: int
    sample_count: int       # Nombre d'échantillons enregistrés
    start_ts: int           # Timestamp Unix de début (secondes)

    def duration_str(self, rate_hz: float = 120.0) -> str:
        """Durée estimée en secondes à partir du nb d'échantillons."""
        if rate_hz <= 0 or self.sample_count == 0:
            return "?"
        secs = self.sample_count / rate_hz
        return f"{secs:.0f}s"

    def start_datetime(self) -> str:
        """Retourne le timestamp de début formaté (ou '?' si absent)."""
        if self.start_ts == 0:
            return "?"
        from datetime import datetime, timezone
        try:
            return datetime.fromtimestamp(self.start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            return str(self.start_ts)


# ---------------------------------------------------------------------------
# Parsing des paquets de données binaires
# ---------------------------------------------------------------------------

# Taille des données par type
_TYPE_SIZE = {name: info[1] for name, info in EXPORT_DATA_TYPES.items()}

# En-têtes CSV par type
_CSV_HEADERS: dict[str, list[str]] = {
    "timestamp":  ["PacketCounter", "SampleTimeFine", "timestamp_ms"],
    "euler":      ["roll_deg", "pitch_deg", "yaw_deg"],
    "acc":        ["acc_x", "acc_y", "acc_z"],
    "ang_vel":    ["gyr_x", "gyr_y", "gyr_z"],
    "mag":        ["mag_x", "mag_y", "mag_z"],
    "quaternion": ["q_w", "q_x", "q_y", "q_z"],
    "free_acc":   ["free_acc_x", "free_acc_y", "free_acc_z"],
    "imu_raw":    ["raw_acc_x", "raw_acc_y", "raw_acc_z"],
    "status":     ["status"],
}


def _parse_sample(data: bytes, types: list[str]) -> list[float]:
    """
    Parse un échantillon binaire little-endian.
    Retourne une liste de valeurs float dans l'ordre des types.
    """
    offset = 0
    values: list[float] = []
    for t in types:
        size = _TYPE_SIZE[t]
        if t == "timestamp":
            # Couple uint32 little-endian : (PacketCounter, SampleTimeFine)
            packet_counter, sample_time_fine = struct.unpack_from("<II", data, offset)
            values.extend([
                float(packet_counter),
                float(sample_time_fine),
                float(sample_time_fine) / 1000.0,
            ])
            offset += 8
        elif t in ("euler", "acc", "ang_vel", "free_acc", "imu_raw"):
            # 3 floats 32 bits
            x, y, z = struct.unpack_from("<fff", data, offset)
            values.extend([x, y, z])
            offset += 12
        elif t == "quaternion":
            # 4 floats 32 bits
            w, x, y, z = struct.unpack_from("<ffff", data, offset)
            values.extend([w, x, y, z])
            offset += 16
        elif t == "mag":
            # 3 × int16, unité = gauss / 4096
            x, y, z = struct.unpack_from("<hhh", data, offset)
            values.extend([x / 4096.0, y / 4096.0, z / 4096.0])
            offset += 6
        elif t == "status":
            val = struct.unpack_from("<H", data, offset)[0]
            values.append(float(val))
            offset += 2
        else:
            # Type inconnu : avancer quand même
            offset += size
    return values


def _csv_headers(types: list[str]) -> list[str]:
    headers: list[str] = []
    for t in types:
        headers.extend(_CSV_HEADERS.get(t, [t]))
    return headers


# ---------------------------------------------------------------------------
# Export d'un capteur
# ---------------------------------------------------------------------------

async def export_sensor(
    sensor: DotSensor,
    output_dir: Path,
    data_types: list[str] = None,
) -> SensorExportResult:
    """
    Exporte toutes les données flash d'un capteur vers des fichiers CSV.

    Args:
        sensor     : DotSensor connecté.
        output_dir : répertoire de sortie (créé si absent).
        data_types : liste des types à exporter (défaut : PRESET_EULER).

    Returns:
        SensorExportResult.
    """
    if data_types is None:
        data_types = PRESET_EULER

    output_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    sensor.state = DotState.EXPORTING

    result = SensorExportResult(address=sensor.address, success=False)

    try:
        # 1. Activer les notifications
        await sensor.subscribe_notifications(critical=False)
        await sensor.drain_notifications()

        # 2. Vérifier l'état
        state = await sensor.cmd_get_state(critical=False)
        if state == STATE_RECORDING:
            raise DotError(f"[{sensor.name}] Capteur en cours d'enregistrement — arrêter d'abord.")
        logger.info("[%s] État = %#04x — début export", sensor.name, state)

        # 3. Sélectionner les types de données
        await sensor.send_and_ack(select_export_data(data_types), critical=False)

        # 4. Récupérer la liste des fichiers
        file_count = await _get_flash_info(sensor)
        logger.info("[%s] %d fichier(s) en flash", sensor.name, file_count)

        if file_count == 0:
            logger.warning("[%s] Aucun fichier en flash.", sensor.name)
            result.success = True
            return result

        # 5. Exporter chaque fichier
        headers = _csv_headers(data_types)
        for file_idx in range(1, file_count + 1):
            file_result = await _export_file(
                sensor, file_idx, headers, data_types, output_dir
            )
            result.files.append(file_result)
            if file_result.ok:
                result.total_samples += file_result.sample_count
            logger.info(
                "[%s] Fichier %d/%d — %d échantillons",
                sensor.name, file_idx, file_count, file_result.sample_count,
            )

        # 6. Arrêter l'export
        await sensor.write_command(stop_export_data(), critical=False)

        result.success = all(f.ok for f in result.files)

    except DotError as exc:
        result.error = str(exc)
        logger.error("[%s] Export échoué : %s", sensor.name, exc)
        try:
            await sensor.write_command(stop_export_data(), critical=False)
        except Exception:
            pass
    except Exception as exc:
        result.error = f"Erreur inattendue : {exc}"
        logger.exception("[%s] Export : erreur inattendue", sensor.name)
    finally:
        await sensor.unsubscribe_notifications(critical=False)
        sensor.state = DotState.CONNECTED

    result.duration_s = time.monotonic() - t0
    logger.info("%s", result)
    return result


async def _get_flash_info(sensor: DotSensor) -> int:
    """
    Envoie request_flash_info() et collecte les notifications 0x51/0x52.
    Retourne le nombre de fichiers en flash.
    """
    await sensor.write_command(request_flash_info(), critical=False)

    file_count = 0
    deadline = time.monotonic() + DATA_TIMEOUT

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DotTimeoutError(f"[{sensor.name}] request_flash_info timeout")

        raw = await sensor.wait_notification(timeout=remaining)
        reid, payload = parse_notification_header(raw)

        if reid == REID_EXPORT_FLASH_INFO:
            # payload[0] contient un indicateur ; on compte juste les paquets
            file_count += 1
        elif reid == REID_EXPORT_FLASH_INFO_DONE:
            break
        else:
            logger.debug("[%s] Notification inattendue reid=%#04x pendant flash_info", sensor.name, reid)

    return file_count


async def _get_file_metadata(sensor: DotSensor, file_idx: int) -> Optional[FileMetadata]:
    """
    Récupère les métadonnées d'un fichier (sample_count, start_ts) via request_file_info.
    Retourne None si le fichier est introuvable.
    """
    await sensor.write_command(request_file_info(file_idx), critical=False)
    sample_count = 0
    start_ts = 0
    deadline = time.monotonic() + DATA_TIMEOUT

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DotTimeoutError(f"[{sensor.name}] request_file_info({file_idx}) timeout metadata")

        raw = await sensor.wait_notification(timeout=remaining)
        reid, payload = parse_notification_header(raw)

        if reid == REID_EXPORT_FILE_INFO:
            if len(payload) >= 8:
                sample_count = struct.unpack_from("<I", payload, 0)[0]
                start_ts = struct.unpack_from("<I", payload, 4)[0]
            elif len(payload) >= 4:
                sample_count = struct.unpack_from("<I", payload, 0)[0]
        elif reid == REID_EXPORT_FILE_INFO_DONE:
            break
        elif reid == REID_NO_RECORDING_FILE:
            return None

    return FileMetadata(file_index=file_idx, sample_count=sample_count, start_ts=start_ts)


async def get_flash_metadata(sensor: DotSensor) -> list[FileMetadata]:
    """
    Récupère les métadonnées de tous les fichiers flash sans exporter les données.

    Nécessite que le capteur soit connecté (mais pas forcément en état IDLE).
    Retourne une liste de FileMetadata pour chaque fichier présent en flash.
    """
    sensor.state = DotState.EXPORTING
    try:
        await sensor.subscribe_notifications(critical=False)
        await sensor.drain_notifications()
        try:
            file_count = await _get_flash_info(sensor)
            files: list[FileMetadata] = []
            for i in range(1, file_count + 1):
                meta = await _get_file_metadata(sensor, i)
                if meta is not None:
                    files.append(meta)
            await sensor.write_command(stop_export_data(), critical=False)
            return files
        finally:
            await sensor.unsubscribe_notifications(critical=False)
    finally:
        sensor.state = DotState.CONNECTED


async def _export_file(
    sensor: DotSensor,
    file_idx: int,
    headers: list[str],
    data_types: list[str],
    output_dir: Path,
) -> FileExportResult:
    """Exporte un fichier individuel."""
    t0 = time.monotonic()

    # --- file info ---
    await sensor.write_command(request_file_info(file_idx), critical=False)
    sample_count = 0
    deadline = time.monotonic() + DATA_TIMEOUT

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DotTimeoutError(f"[{sensor.name}] request_file_info({file_idx}) timeout")
        raw = await sensor.wait_notification(timeout=remaining)
        reid, payload = parse_notification_header(raw)

        if reid == REID_EXPORT_FILE_INFO:
            # payload : [sample_count : uint32 LE] [start_ts : uint32 LE] ...
            if len(payload) >= 4:
                sample_count = struct.unpack_from("<I", payload, 0)[0]
        elif reid == REID_EXPORT_FILE_INFO_DONE:
            break
        elif reid == REID_NO_RECORDING_FILE:
            logger.info("[%s] Fichier %d introuvable.", sensor.name, file_idx)
            return FileExportResult(
                file_index=file_idx,
                sample_count=0,
                output_path=None,
                duration_s=time.monotonic() - t0,
                error="No recording file",
            )

    # --- file data ---
    addr_clean = sensor.address.replace(":", "-")
    out_path = output_dir / f"{addr_clean}_file{file_idx:02d}.csv"
    meta_path = out_path.with_suffix(".json")
    samples_written = 0
    first_packet_counter: Optional[int] = None
    first_sample_time_fine: Optional[float] = None
    last_packet_counter: Optional[int] = None
    last_sample_time_fine: Optional[float] = None

    await sensor.write_command(request_file_data(file_idx), critical=False)

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)

        deadline = time.monotonic() + MAX_EXPORT_DURATION
        last_packet = time.monotonic()

        while True:
            remaining = min(
                deadline - time.monotonic(),
                last_packet + DATA_TIMEOUT - time.monotonic(),
            )
            if remaining <= 0:
                raise DotTimeoutError(
                    f"[{sensor.name}] Timeout données fichier {file_idx} après {samples_written} échantillons"
                )

            raw = await sensor.wait_notification(timeout=remaining)
            reid, payload = parse_notification_header(raw)

            if reid == REID_EXPORT_FILE_DATA:
                last_packet = time.monotonic()
                # Taille attendue par échantillon
                sample_size = sum(_TYPE_SIZE[t] for t in data_types)
                if sample_size > 0 and len(payload) >= sample_size:
                    n_samples = len(payload) // sample_size
                    for i in range(n_samples):
                        chunk = payload[i * sample_size: (i + 1) * sample_size]
                        row = _parse_sample(chunk, data_types)
                        if "timestamp" in data_types and len(row) >= 3:
                            packet_counter = int(row[0])
                            sample_time_fine = float(row[1])
                            if first_packet_counter is None:
                                first_packet_counter = packet_counter
                                first_sample_time_fine = sample_time_fine
                            last_packet_counter = packet_counter
                            last_sample_time_fine = sample_time_fine
                        writer.writerow([f"{v:.6g}" for v in row])
                        samples_written += 1

            elif reid == REID_EXPORT_FILE_DATA_DONE:
                break
            else:
                logger.debug(
                    "[%s] Notification inattendue reid=%#04x pendant file_data",
                    sensor.name, reid,
                )

    _write_export_sidecar(
        meta_path,
        sensor,
        file_idx,
        out_path,
        data_types,
        samples_written,
        time.monotonic() - t0,
        first_packet_counter,
        first_sample_time_fine,
        last_packet_counter,
        last_sample_time_fine,
    )

    return FileExportResult(
        file_index=file_idx,
        sample_count=samples_written,
        output_path=out_path,
        duration_s=time.monotonic() - t0,
    )


def _write_export_sidecar(
    meta_path: Path,
    sensor: DotSensor,
    file_idx: int,
    output_path: Path,
    data_types: list[str],
    sample_count: int,
    duration_s: float,
    first_packet_counter: Optional[int],
    first_sample_time_fine: Optional[float],
    last_packet_counter: Optional[int],
    last_sample_time_fine: Optional[float],
) -> None:
    payload = {
        "address": sensor.address,
        "name": sensor.name,
        "file_index": file_idx,
        "output_csv": output_path.name,
        "data_types": data_types,
        "sample_count": sample_count,
        "duration_s": round(duration_s, 3),
        "packet_counter_start": first_packet_counter,
        "packet_counter_end": last_packet_counter,
        "sample_time_fine_start": first_sample_time_fine,
        "sample_time_fine_end": last_sample_time_fine,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("[%s] Impossible d'écrire le sidecar %s : %s", sensor.name, meta_path.name, exc)


async def export_sensor_files(
    sensor: DotSensor,
    output_dir: Path,
    data_types: list[str] = None,
    file_indices: Optional[list[int]] = None,
) -> SensorExportResult:
    """
    Exporte une sélection de fichiers flash d'un capteur.

    Args:
        sensor       : DotSensor connecté.
        output_dir   : répertoire de sortie.
        data_types   : types de données (défaut : PRESET_EULER).
        file_indices : liste 1-based des fichiers à exporter (défaut : tous).
    """
    if data_types is None:
        data_types = PRESET_EULER

    output_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    sensor.state = DotState.EXPORTING

    result = SensorExportResult(address=sensor.address, success=False)

    try:
        await sensor.subscribe_notifications(critical=False)
        await sensor.drain_notifications()

        state = await sensor.cmd_get_state(critical=False)
        if state == STATE_RECORDING:
            raise DotError(f"[{sensor.name}] Capteur en cours d'enregistrement — arrêter d'abord.")

        await sensor.send_and_ack(select_export_data(data_types), critical=False)

        file_count = await _get_flash_info(sensor)
        if file_count == 0:
            result.success = True
            return result

        # Filtrer les indices fichiers
        if file_indices:
            indices_to_export = [i for i in file_indices if 1 <= i <= file_count]
        else:
            indices_to_export = list(range(1, file_count + 1))

        headers = _csv_headers(data_types)
        for file_idx in indices_to_export:
            file_result = await _export_file(sensor, file_idx, headers, data_types, output_dir)
            result.files.append(file_result)
            if file_result.ok:
                result.total_samples += file_result.sample_count
            logger.info(
                "[%s] Fichier %d/%d — %d échantillons",
                sensor.name, file_idx, file_count, file_result.sample_count,
            )

        await sensor.write_command(stop_export_data(), critical=False)
        result.success = all(f.ok for f in result.files)

    except DotError as exc:
        result.error = str(exc)
        logger.error("[%s] Export échoué : %s", sensor.name, exc)
        try:
            await sensor.write_command(stop_export_data(), critical=False)
        except Exception:
            pass
    except Exception as exc:
        result.error = f"Erreur inattendue : {exc}"
        logger.exception("[%s] Export : erreur inattendue", sensor.name)
    finally:
        await sensor.unsubscribe_notifications(critical=False)
        sensor.state = DotState.CONNECTED

    result.duration_s = time.monotonic() - t0
    return result


# ---------------------------------------------------------------------------
# Export en parallèle de plusieurs capteurs
# ---------------------------------------------------------------------------

async def export_all_sensors(
    sensors: list[DotSensor],
    output_dir: Path,
    data_types: Optional[list[str]] = None,
    file_indices_map: Optional[dict[str, list[int]]] = None,
) -> list[SensorExportResult]:
    """
    Lance l'export flash de tous les capteurs en parallèle.

    Args:
        sensors          : liste de DotSensor connectés.
        output_dir       : répertoire de sortie global.
        data_types       : types de données à exporter (défaut : PRESET_EULER).
        file_indices_map : dict adresse→liste d'indices fichiers. Si absent,
                           exporte tous les fichiers. Par ex. :
                           {"D4:22:CD:00:49:C7": [1, 2], "D4:22:CD:00:49:C8": [1]}

    Returns:
        Liste de SensorExportResult dans le même ordre que `sensors`.
    """
    if data_types is None:
        data_types = PRESET_EULER

    tasks = [
        export_sensor_files(
            s,
            output_dir,
            data_types=data_types,
            file_indices=(file_indices_map or {}).get(s.address.upper()),
        )
        for s in sensors
    ]
    results: list[SensorExportResult] = await asyncio.gather(*tasks)
    return results


def print_export_summary(results: list[SensorExportResult]) -> None:
    print(f"\n{'Capteur':<22} {'Staat':<10} {'Échantillons':>13} {'Durée':>8}  Fichiers")
    print("-" * 70)
    total_samples = 0
    for r in results:
        status = "OK" if r.success else f"ERREUR"
        print(f"{r.address:<22} {status:<10} {r.total_samples:>13} {r.duration_s:>7.1f}s  {len(r.files)}")
        total_samples += r.total_samples
    print(f"\nTotal : {total_samples} échantillons sur {len(results)} capteur(s).\n")
