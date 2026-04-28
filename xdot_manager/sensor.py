"""
DotSensor — encapsule un capteur Xsens DOT unique via GATT BLE.

Responsabilités :
- Connexion / déconnexion (avec retries).
- Envoi de commandes sur MSG_CONTROL_UUID.
- Lecture ACK synchrone sur MSG_ACK_UUID.
- Abonnement aux notifications sur MSG_NOTIFY_UUID.
- Méthodes haut niveau : send_syncing_cmd, start_recording, stop_recording.
- L'export flash est délégué à export.py (qui appelle les méthodes bas niveau ici).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Optional

from bleak import BleakClient
from bleak.exc import BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic

from .adapters import BtAdapter
from .protocol.gatt import (
    MSG_CONTROL_UUID, MSG_ACK_UUID, MSG_NOTIFY_UUID,
    DEVICE_CONTROL_UUID,
    ACK_RESULT_SUCCESS,
    REID_GET_STATE, REID_START_RECORDING, REID_STOP_RECORDING,
    STATE_IDLE, STATE_RECORDING, STATE_ERASING, STATE_FLASH_BUSY,
    STATE_NAMES,
    SUPPORTED_OUTPUT_RATES, DEFAULT_OUTPUT_RATE,
    DEV_CTRL_OFFSET_OUTPUT_RATE,
    GATT_TIMEOUT, CONNECT_TIMEOUT, CONNECT_RETRIES,
    MID_RECORDING, MID_SYNC,
)
from .protocol.commands import (
    get_state, start_recording, stop_recording, erase_flash,
    start_syncing, stop_syncing, set_output_rate,
    parse_ack, parse_notification_header,
)

logger = logging.getLogger(__name__)

# Semaphores par adaptateur.
# - critical=1 : opérations sensibles (sync/start/stop/state)
# - bulk=2     : opérations non critiques (connexion/export)
_ADAPTER_SEMAPHORES_CRITICAL: dict[str, asyncio.Semaphore] = {}
_ADAPTER_SEMAPHORES_BULK: dict[str, asyncio.Semaphore] = {}
_SEMAPHORE_CRITICAL_CONCURRENCY = 1
_SEMAPHORE_BULK_CONCURRENCY = 2


def _get_adapter_semaphore(adapter_name: str, *, critical: bool = True) -> asyncio.Semaphore:
    """Retourne (en créant si besoin) le sémaphore associé à un adaptateur."""
    target = _ADAPTER_SEMAPHORES_CRITICAL if critical else _ADAPTER_SEMAPHORES_BULK
    concurrency = _SEMAPHORE_CRITICAL_CONCURRENCY if critical else _SEMAPHORE_BULK_CONCURRENCY
    if adapter_name not in target:
        target[adapter_name] = asyncio.Semaphore(concurrency)
    return target[adapter_name]


# ---------------------------------------------------------------------------
# État d'un capteur du point de vue de l'application
# ---------------------------------------------------------------------------

class DotState(IntEnum):
    DISCONNECTED = 0
    CONNECTING   = 1
    CONNECTED    = 2
    SYNCING      = 3
    RECORDING    = 4
    EXPORTING    = 5
    ERROR        = 99


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DotError(Exception):
    """Erreur générique liée à un capteur DOT."""

class DotConnectError(DotError):
    """Impossible de se connecter au capteur."""

class DotAckError(DotError):
    """Le capteur a renvoyé un ACK d'erreur."""

class DotTimeoutError(DotError):
    """L'opération GATT a expiré."""

class DotStateError(DotError):
    """Le capteur est dans un état inattendu."""


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class DotSensor:
    """
    Représente un capteur Xsens DOT avec toutes ses opérations GATT.

    Usage typique :
        async with DotSensor(address, adapter) as sensor:
            await sensor.send_syncing_cmd(root_mac)
            await sensor.cmd_start_recording()
            # ... attendre la fin de l'enregistrement ...
            await sensor.cmd_stop_recording()
    """

    def __init__(
        self,
        address: str,
        adapter: Optional[BtAdapter] = None,
        name: str = "",
    ) -> None:
        self.address  = address.upper()
        self.adapter  = adapter
        self.name     = name or self.address
        self.state    = DotState.DISCONNECTED

        # BleakClient, créé à la connexion
        self._client: Optional[BleakClient] = None

        # File d'attente des notifications entrantes (notify → queue)
        self._notify_queue: asyncio.Queue[bytes] = asyncio.Queue()

        # Callback optionnel appelé pour chaque notification brute
        self._notify_callback: Optional[Callable[[bytes], None]] = None

        self.battery_level: Optional[int] = None
        self.on_disconnected: Optional[Callable[["DotSensor"], None]] = None


    def _check_disconnect_error(self, exc: Exception) -> None:
        "Détecte une déconnexion silencieuse sur erreur GATT."
        err_msg = str(exc).lower()
        if "not connected" in err_msg or "unreachable" in err_msg or "unlikely error" in err_msg:
            if self.state != DotState.DISCONNECTED:
                logger.warning("[%s] Erreur GATT fatale détectée (%s) -> Force déconnexion.", self.name, exc)
                if self._client is not None:
                    self._bleak_disconnected_cb(self._client)

    def _bleak_disconnected_cb(self, client: BleakClient) -> None:
        """Callback interne de Bleak en cas de déconnexion."""
        if self.state != DotState.DISCONNECTED:
            logger.warning("[%s] Connexion BLE perdue.", self.name)
            self.state = DotState.ERROR
            if self.on_disconnected:
                # Appeler en asynchrone / sans bloquer la boucle
                loop = asyncio.get_event_loop()
                loop.call_soon_threadsafe(self.on_disconnected, self)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "DotSensor":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------
    # Connexion / Déconnexion
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """
        Établit la connexion BLE avec retries.
        Met à jour self.state → CONNECTED.
        """
        if self.state == DotState.CONNECTED:
            return

        adapter_id = self.adapter.bleak_id if self.adapter else None
        self.state = DotState.CONNECTING

        # Acquérir le sémaphore de l'adaptateur pour sérialiser les tentatives
        sem = _get_adapter_semaphore(adapter_id or "default", critical=False)

        last_exc: Optional[Exception] = None
        for attempt in range(1, CONNECT_RETRIES + 1):
            try:
                logger.info(
                    "[%s] Connexion tentative %d/%d (adapter=%s)...",
                    self.name, attempt, CONNECT_RETRIES, adapter_id,
                )
                if adapter_id:
                    client = BleakClient(
                        self.address,
                        timeout=CONNECT_TIMEOUT,
                        adapter=adapter_id,
                        disconnected_callback=self._bleak_disconnected_cb,
                    )
                else:
                    client = BleakClient(
                        self.address,
                        timeout=CONNECT_TIMEOUT,
                        disconnected_callback=self._bleak_disconnected_cb,
                    )
                async with sem:
                    await client.connect()
                    # Vérifier que les services GATT sont bien découverts
                    # BlueZ peut marquer le serveur connecté avant d'avoir
                    # énuméré tous les services — on vérifie MSG_CONTROL_UUID.
                    for wait in (0.5, 1.0, 2.0):
                        char = client.services.get_characteristic(MSG_CONTROL_UUID)
                        if char is not None:
                            break
                        await asyncio.sleep(wait)
                    else:
                        await client.disconnect()
                        raise DotConnectError(
                            f"[{self.name}] Services GATT non disponibles après connexion"
                        )
                    await asyncio.sleep(0.3)  # stabilisation avant prochaine connexion
                self._client = client
                self.state = DotState.CONNECTED
                logger.info("[%s] Connecté sur %s", self.name, adapter_id or "adaptateur par défaut")
                return

            except (BleakError, asyncio.TimeoutError, OSError) as exc:
                last_exc = exc
                logger.warning("[%s] Tentative %d échouée : %s", self.name, attempt, exc)
                if attempt < CONNECT_RETRIES:
                    await asyncio.sleep(2.0)

        self.state = DotState.ERROR
        raise DotConnectError(
            f"Impossible de se connecter à {self.address} après {CONNECT_RETRIES} tentatives : {last_exc}"
        )

    async def disconnect(self) -> None:
        """Déconnecte proprement le capteur."""
        self.state = DotState.DISCONNECTED
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception as exc:
                logger.debug("[%s] Erreur déconnexion : %s", self.name, exc)
        self._client = None
        logger.info("[%s] Déconnecté.", self.name)

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def is_active(self) -> bool:
        """Indique si le capteur est en cours d'utilisation ou en tentative de connexion."""
        active_state = self.state in (DotState.CONNECTING, DotState.CONNECTED, DotState.SYNCING, DotState.RECORDING, DotState.EXPORTING)
        return active_state and getattr(self._client, 'is_connected', False) if self.state != DotState.CONNECTING else active_state

    # ------------------------------------------------------------------
    # Primitives GATT bas niveau
    # ------------------------------------------------------------------

    def _require_connected(self) -> BleakClient:
        if not self._client or not self._client.is_connected:
            raise DotConnectError(f"[{self.name}] Non connecté.")
        return self._client

    async def write_command(self, data: bytes, response: bool = False, critical: bool = True) -> None:
        """Écrit une trame sur MSG_CONTROL_UUID.
        response=False : write without response (défaut, plus rapide).
        response=True  : write with response (GATT ACK BlueZ, plus fiable).
        """
        client = self._require_connected()
        # Sérialiser les accès GATT par adaptateur
        adapter_name = self.adapter.bleak_id if self.adapter else "local"
        sem = _get_adapter_semaphore(adapter_name, critical=critical)
        try:
            async with sem:
                await asyncio.wait_for(
                    client.write_gatt_char(MSG_CONTROL_UUID, data, response=response),
                    timeout=GATT_TIMEOUT,
                )
            logger.debug("[%s] CMD%s → %s", self.name, "(rsp)" if response else "", data.hex())
        except asyncio.TimeoutError:
            raise DotTimeoutError(f"[{self.name}] write_command timeout (data={data.hex()})")
        except (BleakError, OSError) as exc:
            self._check_disconnect_error(exc)
            raise

    async def read_ack(self, critical: bool = True) -> tuple[int, int, int]:
        """
        Lit la réponse ACK sur MSG_ACK_UUID.
        Retourne (mid, reid, result). Lève DotAckError si result != SUCCESS.
        """
        client = self._require_connected()
        adapter_name = self.adapter.bleak_id if self.adapter else "local"
        sem = _get_adapter_semaphore(adapter_name, critical=critical)
        try:
            async with sem:
                raw = await asyncio.wait_for(
                    client.read_gatt_char(MSG_ACK_UUID),
                    timeout=GATT_TIMEOUT,
                )
        except asyncio.TimeoutError:
            raise DotTimeoutError(f"[{self.name}] read_ack timeout")
        except (BleakError, OSError) as exc:
            self._check_disconnect_error(exc)
            raise

        logger.debug("[%s] ACK ← %s", self.name, raw.hex())
        mid, reid, result = parse_ack(bytes(raw))
        if result != ACK_RESULT_SUCCESS:
            raise DotAckError(
                f"[{self.name}] ACK erreur (mid={mid:#04x} reid={reid:#04x} result={result:#04x})"
            )
        return mid, reid, result

    async def send_and_ack(
        self,
        data: bytes,
        retries: int = 6,
        retry_delay: float = 0.05,
        pre_delay: float = 0.0,
        write_with_response: bool = False,
        critical: bool = True,
    ) -> tuple[int, int, int]:
        """Raccourci : write_command + read_ack avec validation du MID.

        Réessaie jusqu'à `retries` fois si l'ACK lu correspond à une commande
        précédente (MID différent) — situation typique quand le registre ACK
        n'a pas encore été mis à jour par le capteur après un précédent write.

        Args:
            pre_delay            : délai (s) avant la première lecture ACK.  Utile
                                   pour laisser le capteur mettre à jour son registre
                                   ACK (ex : après stop_recording qui suit une sync).
            write_with_response  : si True, utilise un write GATT with-response pour
                                   s'assurer que la commande a bien été reçue avant
                                   de lire l'ACK applicatif.
        """
        expected_mid = data[0]   # MID byte en tête du message
        # Utiliser write-with-response pour les services critiques (Recording / Sync)
        use_response = write_with_response or (expected_mid in (MID_RECORDING, MID_SYNC))
        await self.write_command(data, response=use_response, critical=critical)
        if pre_delay > 0.0:
            await asyncio.sleep(pre_delay)
        client = self._require_connected()
        adapter_name = self.adapter.bleak_id if self.adapter else "local"
        sem = _get_adapter_semaphore(adapter_name, critical=critical)
        last_raw = b""
        # Timeout par lecture plus court pour éviter de bloquer longuement la boucle
        per_read_timeout = min(1.0, GATT_TIMEOUT)
        for attempt in range(retries):
            try:
                async with sem:
                    raw = await asyncio.wait_for(
                        client.read_gatt_char(MSG_ACK_UUID),
                        timeout=per_read_timeout,
                    )
            except asyncio.TimeoutError:
                raise DotTimeoutError(f"[{self.name}] read_ack timeout")
            raw = bytes(raw)
            last_raw = raw
            logger.debug("[%s] ACK raw (essai %d) ← %s", self.name, attempt + 1, raw.hex())
            mid, reid, result = parse_ack(raw)
            if mid != expected_mid:
                logger.debug(
                    "[%s] ACK périmé ignoré (mid=%#04x ≠ attendu %#04x) — retry %d/%d",
                    self.name, mid, expected_mid, attempt + 1, retries,
                )
                await asyncio.sleep(retry_delay)
                continue
            if result != ACK_RESULT_SUCCESS:
                raise DotAckError(
                    f"[{self.name}] ACK erreur (mid={mid:#04x} reid={reid:#04x} result={result:#04x})"
                    f" raw={raw.hex()}"
                )
            return mid, reid, result
        raise DotAckError(
            f"[{self.name}] ACK stale après {retries} essais (mid attendu={expected_mid:#04x})"
            f" — dernier raw={last_raw.hex()}"
        )

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _on_notification(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        """Callback interne — met les données dans la queue."""
        raw = bytes(data)
        logger.debug("[%s] NOTIFY ← %s", self.name, raw.hex())
        self._notify_queue.put_nowait(raw)
        if self._notify_callback:
            self._notify_callback(raw)

    async def subscribe_notifications(self, critical: bool = True) -> None:
        """Active les notifications sur MSG_NOTIFY_UUID."""
        client = self._require_connected()
        adapter_name = self.adapter.bleak_id if self.adapter else "local"
        sem = _get_adapter_semaphore(adapter_name, critical=critical)
        async with sem:
            await client.start_notify(MSG_NOTIFY_UUID, self._on_notification)
        logger.debug("[%s] Notifications activées.", self.name)

    async def unsubscribe_notifications(self, critical: bool = True) -> None:
        """Désactive les notifications."""
        if self._client and self._client.is_connected:
            try:
                adapter_name = self.adapter.bleak_id if self.adapter else "local"
                sem = _get_adapter_semaphore(adapter_name, critical=critical)
                async with sem:
                    await self._client.stop_notify(MSG_NOTIFY_UUID)
            except Exception:
                pass

    def set_notify_callback(self, cb: Optional[Callable[[bytes], None]]) -> None:
        """Enregistre un callback appelé en plus de la queue pour chaque notification."""
        self._notify_callback = cb

    async def wait_notification(self, timeout: float = 10.0) -> bytes:
        """
        Attend la prochaine notification dans la queue.
        Lève DotTimeoutError si le timeout expire.
        """
        try:
            return await asyncio.wait_for(self._notify_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            raise DotTimeoutError(f"[{self.name}] Pas de notification reçue depuis {timeout:.0f}s")

    async def drain_notifications(self) -> None:
        """Vide la queue sans bloquer."""
        while not self._notify_queue.empty():
            try:
                self._notify_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    # ------------------------------------------------------------------
    # Commandes haut niveau
    # ------------------------------------------------------------------

    async def cmd_get_state(self, critical: bool = True) -> int:
        """
        Récupère l'état courant du capteur via GET_STATE (REID=0x02).

        Format ACK réel (spéc Xsens DOT §5.2.3, Table 26) :
          [MID=0x01][LEN][ReID_ACK=0x01][result][orig_reid][...][CS]
          - raw[3] = result = code d'état (TABLE 26)
            0x06 = Idle, 0x40 = Recording, 0x03 = Flash busy, ...
          - raw[4] = orig_reid = écho du REID de la commande (= 0x02 = REID_GET_STATE)

        NOTE : raw[4] vaut toujours 0x02 (= REID_GET_STATE). L'ancienne
        implémentation lisait raw[4] et le comparait à STATE_SYNCING=0x02,
        générant des faux positifs permanents ("toujours en Syncing").
        """
        await self.write_command(get_state(), critical=critical)
        client = self._require_connected()
        adapter_name = self.adapter.bleak_id if self.adapter else "local"
        sem = _get_adapter_semaphore(adapter_name, critical=critical)
        # read under semaphore to serialize with writes
        try:
            async with sem:
                raw = await asyncio.wait_for(
                    client.read_gatt_char(MSG_ACK_UUID),
                    timeout=GATT_TIMEOUT,
                )
        except asyncio.TimeoutError:
            raise DotTimeoutError(f"[{self.name}] cmd_get_state timeout")
        except (BleakError, OSError) as exc:
            self._check_disconnect_error(exc)
            raise DotConnectError(f"Erreur GATT (Not connected): {exc}")
        raw = bytes(raw)
        logger.debug("[%s] STATE ACK raw ← %s", self.name, raw.hex())
        if len(raw) >= 4:
            return raw[3]  # result = code d'état réel
        return STATE_IDLE

    async def cmd_start_recording(self, recording_time: int = 0xFFFF) -> None:
        """
        Démarre l'enregistrement. Vérifie l'ACK.
        recording_time : durée en secondes (0xFFFF = sans limite de durée).
        """
        logger.info("[%s] START RECORDING (duration=%ss)", self.name,
                    "illimitée" if recording_time == 0xFFFF else recording_time)
        # Le registre ACK BlueZ peut être périmé juste après une sync ou une
        # lecture d'état. On attend un court instant et on augmente les essais
        # pour favoriser la stabilité plutôt que la latence.
        await self.send_and_ack(
            start_recording(recording_time=recording_time),
            retries=12,
            retry_delay=0.10,
            pre_delay=0.15,
        )
        self.state = DotState.RECORDING

    async def cmd_stop_recording(self) -> None:
        """Arrête l'enregistrement et attend que le DOT repasse en IDLE.

        La caractéristique MSG_ACK_UUID ne reflète pas l'ACK du service
        Recording (MID=0x01) — elle conserve la dernière réponse du service
        Sync (MID=0x02). On envoie donc la commande sans attendre un ACK
        applicatif, puis on poll cmd_get_state() jusqu'à STATE_IDLE.
        Timeout total : ~12s (40 polls × 0.3s).
        """
        logger.info("[%s] STOP RECORDING", self.name)
        data = stop_recording()
        try:
            await self.write_command(data, response=True)
        except DotTimeoutError:
            raise DotTimeoutError(f"[{self.name}] stop_recording write timeout")

        # Attendre ~0.5s que le DOT traite la commande avant de poller
        await asyncio.sleep(0.5)

        # Poll get_state jusqu'à STATE_IDLE
        for _ in range(40):
            state = await self.cmd_get_state()
            if state == STATE_IDLE:
                self.state = DotState.CONNECTED
                logger.info("[%s] STOP OK — état IDLE confirmé", self.name)
                return
            await asyncio.sleep(0.3)

        # Timeout : vérifier une dernière fois
        state = await self.cmd_get_state()
        if state == STATE_IDLE:
            self.state = DotState.CONNECTED
            return
        raise DotTimeoutError(
            f"[{self.name}] stop_recording : capteur non en IDLE après 12s (état={state:#04x})"
        )

    async def cmd_send_syncing(self, root_mac: str, read_ack: bool = True) -> None:
        """
        Envoie la commande de synchronisation réseau et lit l'ACK.
        Un result ≠ 0x00 (ex : 0x06 = rejeté/déjà en Syncing) est loggé
        en warning mais ne lève pas d'exception (le capteur va quand même
        tenter la sync).

        Args:
            read_ack: si False, n'attend pas l'ACK applicatif après write
                      (utile pour envoyer la sync au plus simultanément
                      possible sur plusieurs capteurs).
        """
        logger.info("[%s] SEND SYNCING (root=%s)", self.name, root_mac)
        if not read_ack:
            await self.write_command(start_syncing(root_mac), response=False)
            self.state = DotState.SYNCING
            return
        try:
            await self.send_and_ack(start_syncing(root_mac), write_with_response=True)
        except DotAckError as exc:
            logger.warning("[%s] start_syncing ACK erreur : %s", self.name, exc)
        except DotTimeoutError:
            logger.warning("[%s] start_syncing : pas d'ACK reçu (timeout).", self.name)
        self.state = DotState.SYNCING

    async def cmd_stop_syncing(self) -> None:
        """Envoie la commande d'arrêt de synchronisation (write with response)."""
        logger.info("[%s] STOP SYNCING", self.name)
        await self.write_command(stop_syncing(), response=True)
        if self.state == DotState.SYNCING:
            self.state = DotState.CONNECTED

    async def cmd_set_output_rate(self, rate_hz: int) -> None:
        """
        Configure le taux d'acquisition (Hz) via DEVICE_CONTROL_UUID (0x1002).

        Valeurs supportées : 1, 4, 10, 12, 15, 20, 30, 60 (défaut), 120 Hz.
        DOIT être appelé AVANT la synchronisation ou le démarrage de la mesure.
        """
        if rate_hz not in SUPPORTED_OUTPUT_RATES:
            raise ValueError(f"Taux {rate_hz} Hz non supporté. Valeurs : {SUPPORTED_OUTPUT_RATES}")
        logger.info("[%s] SET OUTPUT RATE → %d Hz", self.name, rate_hz)
        client = self._require_connected()
        data = set_output_rate(rate_hz)
        try:
            await asyncio.wait_for(
                client.write_gatt_char(DEVICE_CONTROL_UUID, data, response=True),
                timeout=GATT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise DotTimeoutError(f"[{self.name}] set_output_rate timeout")
        except (BleakError, OSError) as exc:
            self._check_disconnect_error(exc)
            raise

    async def cmd_get_output_rate(self) -> int:
        """
        Lit le taux d'acquisition courant depuis DEVICE_CONTROL_UUID (0x1002).
        Retourne le taux en Hz (int).
        """
        client = self._require_connected()
        try:
            raw = await asyncio.wait_for(
                client.read_gatt_char(DEVICE_CONTROL_UUID),
                timeout=GATT_TIMEOUT,
            )
            raw = bytes(raw)
            logger.debug("[%s] DEVICE_CONTROL raw ← %s", self.name, raw.hex())
            if len(raw) >= DEV_CTRL_OFFSET_OUTPUT_RATE + 2:
                import struct
                rate = struct.unpack_from("<H", raw, DEV_CTRL_OFFSET_OUTPUT_RATE)[0]
                return rate if rate in SUPPORTED_OUTPUT_RATES else DEFAULT_OUTPUT_RATE
            return DEFAULT_OUTPUT_RATE
        except asyncio.TimeoutError:
            raise DotTimeoutError(f"[{self.name}] get_output_rate timeout")
        except (BleakError, OSError) as exc:
            self._check_disconnect_error(exc)
            raise DotConnectError(f"Erreur GATT: {exc}")

    async def cmd_erase_flash(self, poll_interval: float = 2.0, timeout: float = 300.0) -> None:
        """
        Efface la mémoire flash du capteur et attend la fin de l'opération.
        Toutes les données enregistrées sont détruites.

        ACK result=0x30 (STATE_ERASING) = effacement démarré avec succès.
        ACK result=0x06 (STATE_IDLE)    = flash déjà vide, efface instantanée.
        On poll get_state jusqu'à STATE_IDLE (0x06).
        Timeout par défaut : 300 s (5 min) pour les grosses flash.
        """
        logger.info("[%s] ERASE FLASH", self.name)
        try:
            await self.send_and_ack(erase_flash())
            # ACK success (0x00) = rare mais possible
        except DotAckError as exc:
            # 0x30 = effacement démarré, ou 0x06 = flash déjà vide — normal
            logger.debug("[%s] erase_flash ACK = non-0x00 (normal) : %s", self.name, exc)
        logger.info("[%s] Effacement lancé, attente fin (timeout=%.0fs, poll=%.0fs)...",
                    self.name, timeout, poll_interval)
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)
            state = await self.cmd_get_state()
            logger.debug("[%s] Effacement état : %s", self.name,
                         STATE_NAMES.get(state, f"0x{state:02x}"))
            if state == STATE_IDLE:
                logger.info("[%s] Flash effacée, capteur Idle.", self.name)
                return
        raise DotTimeoutError(
            f"[{self.name}] Effacement flash non terminé après {timeout:.0f}s "
            f"(dernier état : {STATE_NAMES.get(state, f'0x{state:02x}')})"
        )

    async def cmd_get_battery(self) -> Optional[int]:
        """Lit le niveau de batterie (0-100)."""
        client = self._require_connected()
        try:
            raw = await asyncio.wait_for(
                client.read_gatt_char("00002a19-0000-1000-8000-00805f9b34fb"),
                timeout=GATT_TIMEOUT,
            )
            val = int.from_bytes(raw, byteorder='little')
            self.battery_level = val
            return val
        except Exception as exc:
            self._check_disconnect_error(exc)
            logger.debug("[%s] Impossible de lire la batterie : %s", self.name, exc)
            return None

    # ------------------------------------------------------------------
    # Représentation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        adapter_name = self.adapter.name if self.adapter else "?"
        return f"DotSensor(address={self.address!r}, adapter={adapter_name!r}, state={self.state.name})"
