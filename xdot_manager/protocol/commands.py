"""
Constructeurs de trames GATT pour les capteurs Xsens DOT / Movella DOT.
Port de xdot-export/src/protocol/commands.rs

Format général d'une trame :
  [MID (1B)] [LEN (1B)] [ReID (1B)] [DATA...] [CS (1B)]
  où LEN = nombre d'octets APRÈS le LEN byte (ReID + DATA + CS)
  et CS est tel que la somme de tous les octets ≡ 0 (mod 256).

Pour le service Sync (MID=0x02) :
  [0x02] [0x07] [0x01] [MAC inversé, 6B] [CS]
"""
from __future__ import annotations
import struct
from typing import Sequence

from .gatt import (
    MID_RECORDING, MID_SYNC,
    REID_GET_STATE, REID_ERASE_FLASH,
    REID_START_RECORDING, REID_STOP_RECORDING, REID_REQUEST_REC_TIME,
    REID_REQUEST_FLASH_INFO, REID_REQUEST_FILE_INFO, REID_REQUEST_FILE_DATA,
    REID_STOP_EXPORT_DATA, REID_SELECT_EXPORT_DATA,
    EXPORT_DATA_TYPES,
    SUPPORTED_OUTPUT_RATES,
    DEV_CTRL_VISIT_OUTPUT_RATE, DEV_CTRL_OFFSET_OUTPUT_RATE, DEV_CTRL_TOTAL_SIZE,
)


# ---------------------------------------------------------------------------
# Primitives internes
# ---------------------------------------------------------------------------

def _checksum(data: Sequence[int]) -> int:
    """Calcule le checksum tel que sum(data) + cs ≡ 0 (mod 256)."""
    return (-sum(data)) & 0xFF


def _build_msg(reid: int, payload: bytes = b"") -> bytes:
    """
    Construit une trame Message Service (MID=0x01).
    LEN = 1 (ReID) + len(payload)  — le CS n'est PAS inclus dans LEN.
    Identique au comportement du code Rust build_msg().
    """
    length = 1 + len(payload)   # ReID + payload uniquement
    header = bytes([MID_RECORDING, length, reid]) + payload
    cs = _checksum(header)
    return header + bytes([cs])


# ---------------------------------------------------------------------------
# Commandes Recording (MID = 0x01)
# ---------------------------------------------------------------------------

def get_state() -> bytes:
    """Lire l'état courant du capteur. Réponse : ACK contenant le state byte."""
    return _build_msg(REID_GET_STATE)


def start_recording(
    recording_time: int = 0xFFFF,
    utc: int | None = None,
) -> bytes:
    """
    Démarrer l'enregistrement sur la mémoire flash.

    Spec §5.2.2.3 : ReDATA = [StartUTC (4B LE)] + [RecordingTime (2B LE)]
    recording_time : durée en secondes ; 0xFFFF = sans limite de durée.
    utc            : horodatage Unix en secondes (défaut = heure actuelle).
    """
    import time as _time
    if utc is None:
        utc = int(_time.time())
    payload = struct.pack("<IH", utc, recording_time)
    return _build_msg(REID_START_RECORDING, payload)


def stop_recording() -> bytes:
    """Arrêter l'enregistrement."""
    return _build_msg(REID_STOP_RECORDING)


def request_recording_time() -> bytes:
    """Lire la durée d'enregistrement courante."""
    return _build_msg(REID_REQUEST_REC_TIME)


def erase_flash(utc_timestamp: int = 0) -> bytes:
    """
    Effacer la mémoire flash.
    utc_timestamp : horodatage UTC 32 bits (optionnel).
    """
    payload = struct.pack("<I", utc_timestamp)
    return _build_msg(REID_ERASE_FLASH, payload)


def request_flash_info() -> bytes:
    """Demander la liste des fichiers enregistrés en flash."""
    return _build_msg(REID_REQUEST_FLASH_INFO)


def request_file_info(file_index: int) -> bytes:
    """
    Demander les métadonnées d'un fichier (nb samples, timestamp).
    file_index : 1-based.
    """
    return _build_msg(REID_REQUEST_FILE_INFO, bytes([file_index]))


def request_file_data(file_index: int) -> bytes:
    """
    Démarrer le transfert des données d'un fichier.
    file_index : 1-based.
    """
    return _build_msg(REID_REQUEST_FILE_DATA, bytes([file_index]))


def stop_export_data() -> bytes:
    """Arrêter le transfert en cours."""
    return _build_msg(REID_STOP_EXPORT_DATA)


def set_output_rate(rate_hz: int) -> bytes:
    """
    Construit la trame à écrire sur DEVICE_CONTROL_UUID (0x1002) pour
    configurer le taux d'acquisition.

    Structure : 32 octets (Table 7 spec Movella DOT BLE Services Specifications)
      - byte  0     : Visit Index = 0x10 (bit b4 → champ Output rate seulement)
      - bytes 1-23  : 0 (ignorés car leurs bits Visit ne sont pas activés)
      - bytes 24-25 : rate_hz en uint16 little-endian
      - bytes 26-31 : 0 (ignorés / réservés)

    Seuls les taux de SUPPORTED_OUTPUT_RATES sont acceptés par le capteur.
    Configurer AVANT la sync ou le démarrage de mesure.
    """
    if rate_hz not in SUPPORTED_OUTPUT_RATES:
        raise ValueError(
            f"Taux {rate_hz} Hz non supporté. Valeurs valides : {SUPPORTED_OUTPUT_RATES}"
        )
    data = bytearray(DEV_CTRL_TOTAL_SIZE)
    data[0] = DEV_CTRL_VISIT_OUTPUT_RATE                      # Visit Index : bit b4
    struct.pack_into("<H", data, DEV_CTRL_OFFSET_OUTPUT_RATE, rate_hz)
    return bytes(data)


def select_export_data(data_types: list[str]) -> bytes:
    """
    Configurer les types de données à exporter.
    data_types : liste de noms définis dans EXPORT_DATA_TYPES,
                 ex. ["timestamp", "euler"] ou ["timestamp", "quaternion"].

    Chaque type est encodé sur 1 octet (son code).
    """
    payload = bytes([EXPORT_DATA_TYPES[t][0] for t in data_types])
    return _build_msg(REID_SELECT_EXPORT_DATA, payload)


def select_euler_export() -> bytes:
    return select_export_data(["timestamp", "euler"])


def select_quaternion_export() -> bytes:
    return select_export_data(["timestamp", "quaternion"])


def select_imu_export() -> bytes:
    return select_export_data(["timestamp", "calibrated_acc", "calibrated_gyro", "calibrated_mag"])


def select_full_export() -> bytes:
    return select_export_data(list(EXPORT_DATA_TYPES.keys()))


# ---------------------------------------------------------------------------
# Commande Sync (MID = 0x02)
# ---------------------------------------------------------------------------

def start_syncing(root_mac: str) -> bytes:
    """
    Envoyer la commande de synchronisation réseau.
    root_mac : adresse MAC du capteur racine au format "AA:BB:CC:DD:EE:FF".

    Trame : [0x02] [0x07] [0x01] [mac[5]..mac[0]] [CS]
    Les octets MAC sont envoyés en ordre INVERSÉ (little-endian).
    """
    mac_bytes = bytes(int(b, 16) for b in root_mac.split(":"))
    if len(mac_bytes) != 6:
        raise ValueError(f"Adresse MAC invalide : {root_mac!r}")
    payload = bytes([0x01]) + mac_bytes[::-1]   # CMD_START_SYNCING + MAC inversé (7 octets)
    length = len(payload)                        # LEN = len(payload), CS non inclus
    header = bytes([MID_SYNC, length]) + payload
    cs = _checksum(header)
    return header + bytes([cs])


def stop_syncing() -> bytes:
    """Arrêter la synchronisation réseau."""
    payload = bytes([0x02])   # CMD_STOP_SYNCING
    length = len(payload)     # LEN = 1, CS non inclus
    header = bytes([MID_SYNC, length]) + payload
    cs = _checksum(header)
    return header + bytes([cs])


# ---------------------------------------------------------------------------
# Parsing des réponses
# ---------------------------------------------------------------------------

def parse_ack(data: bytes) -> tuple[int, int, int]:
    """
    Analyse un paquet lu sur MSG_ACK_UUID.
    Retourne (mid, reid, result).
    result == 0x00 → succès.
    Lève ValueError si le paquet est trop court.
    """
    if len(data) < 4:
        raise ValueError(f"ACK trop court : {data.hex()}")
    mid    = data[0]
    # data[1] = LEN
    reid   = data[2]
    result = data[3]
    return mid, reid, result


def parse_notification_header(data: bytes) -> tuple[int, bytes]:
    """
    Analyse l'en-tête d'une notification reçue sur MSG_NOTIFY_UUID.
    Format trame : [MID=0x01][LEN][REID][DATA…][CS]

    Retourne (reid, payload_bytes) sans le MID, LEN ni le CS final.
    Conforme à la fonction parse_notification() du Rust (commands.rs).
    """
    if len(data) < 4:
        raise ValueError(f"Notification trop courte : {data.hex()}")
    # data[0] = MID
    # data[1] = LEN  (REID + DATA, CS exclu)
    # data[2] = REID
    # data[3:2+LEN] = DATA
    # data[2+LEN]   = CS
    length  = data[1]
    reid    = data[2]
    payload = data[3:2 + length]   # exclut MID, LEN, REID et CS
    return reid, payload
