"""
Constantes GATT pour les capteurs Xsens DOT / Movella DOT.
Port de xdot-export/src/protocol/gatt.rs
"""
from uuid import UUID

# ---------------------------------------------------------------------------
# Noms de périphériques BLE
# ---------------------------------------------------------------------------
DOT_NAMES = ("Xsens DOT", "Movella DOT")

# ---------------------------------------------------------------------------
# UUIDs GATT — Service Configuration (0x1000)
# Source : Table 5 spec Movella DOT BLE Services Specifications
# ---------------------------------------------------------------------------
DEVICE_INFO_UUID     = "15171001-4947-11e9-8646-d663bd873d93"  # Read (34 B)
DEVICE_CONTROL_UUID  = "15171002-4947-11e9-8646-d663bd873d93"  # Read/Write (32 B)
                                                                # → output rate, filter profile, tag...
DEVICE_REPORT_UUID   = "15171004-4947-11e9-8646-d663bd873d93"  # Notify (36 B)

# ---------------------------------------------------------------------------
# UUIDs GATT — Service Measurement (0x2000, streaming temps réel)
# ---------------------------------------------------------------------------
CONTROL_UUID         = "15172001-4947-11e9-8646-d663bd873d93"
MEASUREMENT_UUID     = "15172003-4947-11e9-8646-d663bd873d93"
ORIENTATION_RESET_UUID = "15172006-4947-11e9-8646-d663bd873d93"

# ---------------------------------------------------------------------------
# Taux d'acquisition supportés (Hz)
# Source : Table 7 spec — Device Control Characteristic, champ Output rate
# Valeurs valides : 1, 4, 10, 12, 15, 20, 30, 60 (défaut), 120
# Applicable au streaming ET à l'enregistrement flash.
# NOTE : le taux ne peut plus être changé après le démarrage de la mesure ou
# de la synchronisation → configurer AVANT cmd_send_syncing.
# ---------------------------------------------------------------------------
SUPPORTED_OUTPUT_RATES = (1, 4, 10, 12, 15, 20, 30, 60, 120)
DEFAULT_OUTPUT_RATE   = 60

# Offsets dans la structure Device Control (32 octets, §2.2)
# Visit Index (byte 0) : bitmask — b4=1 pour changer le taux, b5=1 pour le filtre
DEV_CTRL_VISIT_OUTPUT_RATE    = 0x10  # bit 4
DEV_CTRL_VISIT_FILTER_PROFILE = 0x20  # bit 5
DEV_CTRL_OFFSET_OUTPUT_RATE   = 24    # uint16 LE
DEV_CTRL_OFFSET_FILTER        = 26    # uint8
DEV_CTRL_TOTAL_SIZE           = 32

# ---------------------------------------------------------------------------
# UUIDs GATT — Message Service (recording + export flash)
# ---------------------------------------------------------------------------
MSG_CONTROL_UUID     = "15177001-4947-11e9-8646-d663bd873d93"  # Write
MSG_ACK_UUID         = "15177002-4947-11e9-8646-d663bd873d93"  # Read
MSG_NOTIFY_UUID      = "15177003-4947-11e9-8646-d663bd873d93"  # Notify

# Alias sémantiques
RECORDING_CONTROL_UUID = MSG_CONTROL_UUID
RECORDING_ACK_UUID     = MSG_ACK_UUID

# ---------------------------------------------------------------------------
# MID — identifiant du service dans la trame
# ---------------------------------------------------------------------------
MID_RECORDING = 0x01
MID_SYNC      = 0x02

# ---------------------------------------------------------------------------
# ReID — identifiants de commandes (Recording service, MID=0x01)
# ---------------------------------------------------------------------------
REID_GET_STATE          = 0x02
REID_ERASE_FLASH        = 0x30
REID_START_RECORDING    = 0x40
REID_STOP_RECORDING     = 0x41
REID_REQUEST_REC_TIME   = 0x42
REID_REQUEST_FLASH_INFO = 0x50
REID_REQUEST_FILE_INFO  = 0x60
REID_REQUEST_FILE_DATA  = 0x70
REID_STOP_EXPORT_DATA   = 0x73
REID_SELECT_EXPORT_DATA = 0x74

# ---------------------------------------------------------------------------
# ReID — Notifications retour (capteur → host)
# ---------------------------------------------------------------------------
REID_EXPORT_FLASH_INFO      = 0x51  # 1 paquet par fichier
REID_EXPORT_FLASH_INFO_DONE = 0x52
REID_EXPORT_FILE_INFO       = 0x61  # nb samples + timestamp début
REID_EXPORT_FILE_INFO_DONE  = 0x62
REID_NO_RECORDING_FILE      = 0x63
REID_EXPORT_FILE_DATA       = 0x71  # paquets de données
REID_EXPORT_FILE_DATA_DONE  = 0x72

# ---------------------------------------------------------------------------
# Codes résultat ACK (Table 26 spec Xsens DOT — source gatt.rs Rust)
# ---------------------------------------------------------------------------
ACK_RESULT_SUCCESS        = 0x00  # Commande acceptée
ACK_RESULT_NACK           = 0x01  # NACK générique
ACK_RESULT_INVALID_CMD    = 0x02  # Commande invalide dans l'état courant
ACK_RESULT_FLASH_BUSY     = 0x03  # Flash occupée (effacement en cours)

# Codes retournés par GET_STATE (dans le champ result de l'ACK)
ACK_RESULT_IDLE           = 0x06  # DataLog Idle
ACK_RESULT_ON_ERASING     = 0x30  # Effacement flash en cours
ACK_RESULT_ON_RECORDING   = 0x40  # DataLog enregistrement en cours
ACK_RESULT_ON_FLASH_INFO  = 0x50  # Export flash info en cours
ACK_RESULT_ON_FILE_INFO   = 0x60  # Export file info en cours
ACK_RESULT_ON_FILE_DATA   = 0x70  # Export file data en cours

# ---------------------------------------------------------------------------
# État du capteur (résultat de cmd_get_state = raw[3] de l'ACK)
# ATTENTION : ces codes correspondent aux ACK_RESULT_* ci-dessus, PAS à
# des constantes arbitraires 0..3.
# ---------------------------------------------------------------------------
STATE_IDLE         = ACK_RESULT_IDLE           # 0x06
STATE_ERASING      = ACK_RESULT_ON_ERASING     # 0x30
STATE_RECORDING    = ACK_RESULT_ON_RECORDING   # 0x40
STATE_FLASH_BUSY   = ACK_RESULT_FLASH_BUSY     # 0x03
# Note : l'état Syncing n'est pas exposé via get_state — la sync est
# transparente au sous-système flash (état = Idle pendant la sync).
STATE_SYNCING      = 0xFF  # inconnu / non applicable via get_state

STATE_NAMES = {
    0x03: "FlashBusy",
    0x06: "Idle",
    0x30: "Erasing",
    0x40: "Recording",
    0x50: "OnFlashInfo",
    0x60: "OnFileInfo",
    0x70: "OnFileData",
}

# ---------------------------------------------------------------------------
# Payload IDs (type de données à exporter)
# ---------------------------------------------------------------------------
PAYLOAD_QUATERNION  = 0x02
PAYLOAD_EULER       = 0x10   # = 16
PAYLOAD_IMU_RAW     = 0x14   # = 20
PAYLOAD_CUSTOM1     = 0x64
PAYLOAD_CUSTOM2     = 0x65
PAYLOAD_CUSTOM3     = 0x66

# ---------------------------------------------------------------------------
# Types de données individuels pour select_export_data()
# Source : Table 25 spec Xsens DOT (codes validés depuis gatt.rs Rust)
# Chaque entrée : (code_byte, taille_en_octets)
# ---------------------------------------------------------------------------
EXPORT_DATA_TYPES = {
    # Type 0x00 contient un couple (PacketCounter, SampleTimeFine) en uint32 LE.
    # On le mappe sous le nom historique "timestamp" pour conserver l'API existante.
    "timestamp":   (0x00, 8),
    "quaternion":  (0x01, 16),  # 4×float (w,x,y,z)
    "euler":       (0x04, 12),  # 3×float degrés
    "acc":         (0x07, 12),  # 3×float m/s²
    "ang_vel":     (0x08, 12),  # 3×float dps
    "mag":         (0x09, 6),   # 3×int16/4096
    "status":      (0x0A, 2),   # uint16
}

# Groupes prédéfinis
EXPORT_PRESET_EULER = ["timestamp", "euler"]
EXPORT_PRESET_QUATERNION = ["timestamp", "quaternion"]
EXPORT_PRESET_IMU = ["timestamp", "calibrated_acc", "calibrated_gyro", "calibrated_mag"]
EXPORT_PRESET_FULL = list(EXPORT_DATA_TYPES.keys())

# ---------------------------------------------------------------------------
# Timeouts (secondes)
# ---------------------------------------------------------------------------
GATT_TIMEOUT         = 10.0   # write / read ACK
DATA_TIMEOUT         = 15.0   # entre deux paquets 0x71 consécutifs
CONNECT_TIMEOUT      = 15.0   # tentative de connexion BLE
SYNC_SETTLE_TIME     = 2.0    # attente après envoi start_syncing
MAX_EXPORT_DURATION  = 7200.0 # durée maximale export (2h)
CONNECT_RETRIES      = 3      # tentatives de connexion
