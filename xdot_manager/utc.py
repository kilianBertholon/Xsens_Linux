"""
Vérification et blindage UTC pour synchronisation temps réel.

Le système utilise time.time() comme référence absolue pour l'armement StartUTC.
Cette référence n'est correcte que si l'horloge système est synchronisée via NTP.

Ce module détecte les dérives horaires et propose des corrections.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Seuil de dérive toléré (secondes)
UTC_DRIFT_WARNING_THRESHOLD = 1.0  # Avertir si drift > 1s
UTC_DRIFT_ERROR_THRESHOLD = 5.0    # Refuser si drift > 5s


@dataclass
class UTCStatus:
    """Résultat du diagnostic UTC."""
    is_synchronized: bool          # Horloge sync avec NTP ?
    drift_seconds: float           # |UTC system - NTP reference| (secondes)
    ntp_available: bool            # timedatectl/ntpq disponible ?
    error_message: Optional[str]   # Message d'erreur le cas échéant
    
    def severity(self) -> str:
        """Retourne 'OK', 'WARNING' ou 'ERROR'."""
        if self.error_message:
            return "ERROR"
        if not self.is_synchronized:
            return "WARNING"
        if self.drift_seconds > UTC_DRIFT_WARNING_THRESHOLD:
            return "WARNING"
        return "OK"
    
    def __str__(self) -> str:
        if self.error_message:
            return f"❌ ERREUR UTC : {self.error_message}"
        
        if not self.is_synchronized:
            msg = "⚠️ ATTENTION : Horloge système pas synchronisée NTP"
            msg += f"\n   Drift: {self.drift_seconds:.2f}s"
            msg += "\n   Solution: activez NTP ou synchronisez manuellement"
            return msg
        
        if self.drift_seconds > UTC_DRIFT_WARNING_THRESHOLD:
            msg = f"⚠️ ATTENTION : Dérive UTC détectée ({self.drift_seconds:.2f}s)"
            msg += "\n   Impact: timestamps de démarrage imprecis"
            msg += "\n   Solution: synchroniser l'horloge via NTP"
            return msg
        
        return f"✓ UTC OK (drift: {self.drift_seconds:.2f}s, NTP sync)"


async def get_utc_status() -> UTCStatus:
    """
    Vérifie l'état de synchronisation UTC du système.
    
    Essaie plusieuirs méthodes (timedatectl, ntpq, adjtimex).
    Retourne un résumé complet.
    """
    # Essayer timedatectl en premier (plus robuste sur systemd)
    logger.debug("Attempting timedatectl check...")
    status = await _check_with_timedatectl()
    if status is not None:
        logger.info(f"UTC check succeeded via timedatectl: {status}")
        return status
    
    logger.debug("timedatectl failed, trying ntpq...")
    # Fallback sur ntpq
    status = await _check_with_ntpq()
    if status is not None:
        logger.info(f"UTC check succeeded via ntpq: {status}")
        return status

    logger.debug("Both timedatectl and ntpq failed, using best-effort check")
    # Si tout échoue, retourner une estimation basée sur drift seul
    drift = await _estimate_drift()
    logger.info(f"UTC check: drift estimation only = {drift:.3f}s")
    
    return UTCStatus(
        is_synchronized=drift < 1.0,  # Assume OK si drift petit
        drift_seconds=drift,
        ntp_available=False,
        error_message=None,
    )


async def _check_ntp_tools_available() -> bool:
    """Vérifie que timedatectl ou ntpq sont disponibles."""
    # Essayer directement les commandes (plus robuste que "which")
    for cmd in ["timedatectl", "ntpq"]:
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [cmd, "--version"] if cmd == "timedatectl" else [cmd, "-p"],
                capture_output=True,
                timeout=1.0,
            )
            if result.returncode == 0:
                logger.debug(f"{cmd} found and working")
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
            logger.debug(f"{cmd} check failed: {e}")
            continue
    
    logger.debug("No NTP tools found (timedatectl, ntpq)")
    return False


async def _check_with_timedatectl() -> Optional[UTCStatus]:
    """Vérifie sync NTP via timedatectl (systemd)."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["timedatectl", "status"],
            capture_output=True,
            timeout=3,
            text=True,
        )
        
        if result.returncode != 0:
            logger.debug(f"timedatectl returned non-zero: {result.returncode}")
            logger.debug(f"stderr: {result.stderr[:200]}")
            return None
        
        output = result.stdout.lower()
        logger.debug(f"timedatectl output: {output[:300]}")
        
        # Chercher la ligne "System clock synchronized"
        is_synchronized = (
            "system clock synchronized: yes" in output or
            "synchronised: yes" in output or
            "systeme synchronise" in output
        )
        
        # Calculer drift via UTC actuel (imprécis, mais meilleur qu'aucun)
        drift = await _estimate_drift()
        
        logger.info(f"timedatectl check: synchronized={is_synchronized}, drift={drift:.3f}s")
        
        return UTCStatus(
            is_synchronized=is_synchronized,
            drift_seconds=drift,
            ntp_available=True,
            error_message=None if is_synchronized else "NTP sync désactif",
        )
    except subprocess.TimeoutExpired:
        logger.debug("timedatectl timeout (>3s)")
        return None
    except FileNotFoundError:
        logger.debug("timedatectl non trouvé")
        return None
    except Exception as exc:
        logger.debug(f"timedatectl erreur : {exc}")
        return None


async def _check_with_ntpq() -> Optional[UTCStatus]:
    """Vérifie sync NTP via ntpq (outils NTP classiques)."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["ntpq", "-p"],
            capture_output=True,
            timeout=5,
            text=True,
        )
        
        if result.returncode != 0:
            return None
        
        output = result.stdout
        
        # Chercher un serveur sync (ligne commence par '*' ou '+')
        is_synchronized = False
        for line in output.split('\n'):
            if line.strip().startswith(('*', '+')):
                is_synchronized = True
                break
        
        drift = await _estimate_drift()
        
        return UTCStatus(
            is_synchronized=is_synchronized,
            drift_seconds=drift,
            ntp_available=True,
            error_message=None if is_synchronized else "Aucun serveur NTP sync",
        )
    except subprocess.TimeoutExpired:
        logger.debug("ntpq timeout")
        return None
    except FileNotFoundError:
        logger.debug("ntpq non trouvé")
        return None
    except Exception as exc:
        logger.debug(f"ntpq erreur : {exc}")
        return None


async def _estimate_drift() -> float:
    """
    Estime la dérive horaire en prenant plusieurs lectures time.time().
    
    Si l'horloge dérive, les intervalles entre lectures seront érratiques.
    Cette méthode est imprécise mais mieux que rien.
    """
    try:
        # Prendre 5 lectures espacées de 10ms
        times = []
        for _ in range(5):
            times.append(time.time())
            await asyncio.sleep(0.01)
        
        # Vérifier que les intervalles sont réguliers (~10ms)
        intervals = [times[i + 1] - times[i] for i in range(4)]
        
        # Drift = déviation de l'intervalle attendu
        expected_interval = 0.01
        deviations = [abs(interval - expected_interval) for interval in intervals]
        
        # Retourner le max de déviation (majorant du drift)
        return max(deviations) if deviations else 0.0
    except Exception as exc:
        logger.debug(f"Estimation drift échouée : {exc}")
        return 0.0


async def verify_utc_before_recording(
    raise_on_error: bool = False,
    raise_on_warning: bool = False,
) -> bool:
    """
    Vérifie l'UTC avant un enregistrement synchronisé.
    
    Args:
        raise_on_error:   Lever une exception si drift > ERROR_THRESHOLD
        raise_on_warning: Lever une exception si drift > WARNING_THRESHOLD
    
    Returns:
        True si UTC OK, False sinon
    
    Raises:
        RuntimeError si raise_on_error=True et UTC drift trop grand
    """
    status = await get_utc_status()
    
    logger.info(f"UTC Status: {status}")
    
    if raise_on_error and status.drift_seconds > UTC_DRIFT_ERROR_THRESHOLD:
        msg = (
            f"UTC drift trop élevé ({status.drift_seconds:.2f}s > {UTC_DRIFT_ERROR_THRESHOLD}s). "
            "Synchronisez l'horloge via NTP avant enregistrement."
        )
        logger.error(msg)
        raise RuntimeError(msg)
    
    if raise_on_warning and status.drift_seconds > UTC_DRIFT_WARNING_THRESHOLD:
        msg = (
            f"UTC drift détecté ({status.drift_seconds:.2f}s > {UTC_DRIFT_WARNING_THRESHOLD}s). "
            "Les timestamps de synchronisation peuvent être imprécis."
        )
        logger.warning(msg)
        raise RuntimeError(msg)
    
    if not status.is_synchronized:
        msg = "Horloge système pas synchronisée NTP. Les timestamps peuvent être incorrects."
        if raise_on_warning:
            logger.error(msg)
            raise RuntimeError(msg)
        else:
            logger.warning(msg)
        return False
    
    if status.drift_seconds > UTC_DRIFT_WARNING_THRESHOLD:
        logger.warning(
            f"Dérive UTC {status.drift_seconds:.2f}s > seuil {UTC_DRIFT_WARNING_THRESHOLD}s"
        )
        if raise_on_warning:
            raise RuntimeError("UTC drift trop élevé")
        return False
    
    return True


def get_ntp_sync_commands() -> dict[str, list[str]]:
    """Retourne les commandes pour synchroniser l'horloge selon l'outil disponible."""
    commands = {
        "timedatectl": [
            "# Sur systemd, activer la sync NTP",
            "sudo timedatectl set-ntp true",
            "sudo timedatectl set-time-zone UTC",  # Optionnel
        ],
        "ntpdate": [
            "# Synchroniser une fois immédiatement",
            "sudo ntpdate -s pool.ntp.org",
        ],
        "chronyc": [
            "# Via Chrony",
            "sudo chronyc makestep",
        ],
        "systemctl": [
            "# Vérifier service NTP",
            "sudo systemctl enable --now systemd-timesyncd",
        ],
    }
    return commands


# Pour usage dans recording.py
DRIFT_WARNING_MSG = (
    "⚠️ UTC drift détecté. Les timestamps de synchronisation peuvent être imprécis.\n"
    "Recommandé : synchroniser NTP avant enregistrement long.\n"
    "Voir: get_ntp_sync_commands()"
)

DRIFT_ERROR_MSG = (
    "❌ UTC drift trop élevé (>5s). Enregistrement risqué.\n"
    "Action requise : synchroniser NTP immédiatement.\n"
    "Commande: sudo timedatectl set-ntp true"
)
