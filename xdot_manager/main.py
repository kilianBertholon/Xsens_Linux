"""
Point d'entrée CLI — xdot-manager.

Sous-commandes :
  adapters          Lister les adaptateurs Bluetooth disponibles
  scan              Scanner et lister les capteurs Xsens DOT
  check-utc         Vérifier la synchronisation UTC du système
  record            Synchroniser + démarrer/arrêter l'enregistrement
  export            Exporter la mémoire flash vers CSV
  full              Cycle complet : sync → record → stop → export
  campaign          Campagne fiabilité (runs répétés)

Usage examples :
  xdot adapters
  xdot check-utc
  xdot scan --timeout 8
  xdot record --duration 60 --payload euler
  xdot export --output ./data --payload quaternion
  xdot full --duration 30 --output ./data --payload euler
  xdot campaign --runs 5 --duration 10 --expected-count 12
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

from .adapters import list_adapters, print_adapter_summary, SAFE_DEFAULT_MAX_PER_ADAPTER
from .scanner import scan_for_dots, print_scan_results, DotDevice
from .sensor import DotSensor, DotError, DotConnectError
from .sync import synchronize_sensors, SyncResult
from .recording import start_all, stop_all, wait_duration
from .utc import get_utc_status, get_ntp_sync_commands
from .export import export_all_sensors, print_export_summary, PRESET_EULER, PRESET_QUATERNION, PRESET_IMU, PRESET_FULL
from .campaign import run_reliability_campaign, format_campaign_summary

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Payloads disponibles
# ---------------------------------------------------------------------------
PAYLOAD_MAP = {
    "euler":      PRESET_EULER,
    "quaternion": PRESET_QUATERNION,
    "imu":        PRESET_IMU,
    "full":       PRESET_FULL,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        level=level,
    )
    # Réduire le bruit des bibliothèques tierces
    for noisy in ("bleak", "bleak.backends", "dbus_next"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _scan_and_filter(
    timeout: float,
    max_per_adapter: int,
    addresses: Optional[list[str]] = None,
) -> list[DotDevice]:
    """Lance le scan et filtre optionnellement par adresses."""
    adapters = list_adapters()
    if not adapters:
        print("[ERREUR] Aucun adaptateur Bluetooth trouvé.", file=sys.stderr)
        sys.exit(1)

    print_adapter_summary(adapters)
    print(f"Scan en cours ({timeout:.0f}s)...")
    devices = await scan_for_dots(timeout=timeout, adapters=adapters,
                                  max_per_adapter=max_per_adapter)
    if addresses:
        addresses_upper = [a.upper() for a in addresses]
        devices = [d for d in devices if d.address in addresses_upper]

    print_scan_results(devices)
    return devices


async def _connect_all(devices: list[DotDevice]) -> list[DotSensor]:
    """Crée et connecte tous les capteurs en parallèle."""
    sensors = [
        DotSensor(d.address, adapter=d.adapter, name=d.address)
        for d in devices
    ]

    print(f"Connexion de {len(sensors)} capteur(s)...")

    async def _connect_one(s: DotSensor) -> Optional[DotSensor]:
        try:
            await s.connect()
            return s
        except DotConnectError as exc:
            print(f"  [ERREUR] {exc}", file=sys.stderr)
            return None

    results = await asyncio.gather(*[_connect_one(s) for s in sensors])
    connected = [s for s in results if s is not None]
    failed = len(sensors) - len(connected)

    if failed:
        print(f"  {failed} capteur(s) non connecté(s).")
    print(f"  {len(connected)} capteur(s) connecté(s).\n")
    return connected


async def _disconnect_all(sensors: list[DotSensor]) -> None:
    await asyncio.gather(*[s.disconnect() for s in sensors])


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------

async def cmd_adapters(_args: argparse.Namespace) -> None:
    adapters = list_adapters(include_down=True)
    if not adapters:
        print("Aucun adaptateur Bluetooth trouvé.")
        return
    print_adapter_summary(adapters)


async def cmd_scan(args: argparse.Namespace) -> None:
    await _scan_and_filter(
        timeout=args.timeout,
        max_per_adapter=args.max_per_adapter,
    )


async def cmd_record(args: argparse.Namespace) -> None:
    devices = await _scan_and_filter(
        timeout=args.scan_timeout,
        max_per_adapter=args.max_per_adapter,
    )
    if not devices:
        print("Aucun capteur trouvé.")
        return

    sensors = await _connect_all(devices)
    if not sensors:
        return

    try:
        # Synchronisation
        print("Synchronisation des capteurs...")
        sync_result = await synchronize_sensors(sensors, settle_time=2.0, verify_state=False)
        print(f"  {sync_result}\n")
        if sync_result.failed_sensors:
            print(f"  Capteurs non synchronisés : {sync_result.failed_sensors}")

        # === Vérification UTC (robustesse) ===
        print("Vérification synchronisation UTC système...")
        utc_status = await get_utc_status()
        print(f"  {utc_status}")
        if not utc_status.is_synchronized or utc_status.drift_seconds > 1.0:
            print("  ⚠️  Conseil: pour enregistrements long, synchronisez NTP")
            print("    Commandes disponibles:")
            commands = get_ntp_sync_commands()
            for tool, cmds in commands.items():
                print(f"    [{tool}]:")
                for cmd in cmds:
                    print(f"      {cmd}")
        print()

        # Démarrage enregistrement
        start_result = await start_all(sensors)
        print(f"  {start_result}")
        if start_result.jitter_ms is not None:
            print(f"  Jitter start : {start_result.jitter_ms:.1f} ms")

        if not start_result.success:
            print(f"  Capteurs en erreur : {start_result.failed_sensors}", file=sys.stderr)

        # Attente
        await wait_duration(args.duration)

        # Arrêt
        stop_result = await stop_all(sensors)
        print(f"  {stop_result}")

    finally:
        await _disconnect_all(sensors)


async def cmd_export(args: argparse.Namespace) -> None:
    output_dir = Path(args.output)
    data_types = PAYLOAD_MAP[args.payload]

    devices = await _scan_and_filter(
        timeout=args.scan_timeout,
        max_per_adapter=args.max_per_adapter,
    )
    if not devices:
        print("Aucun capteur trouvé.")
        return

    sensors = await _connect_all(devices)
    if not sensors:
        return

    try:
        print(f"Export flash vers {output_dir}/ (payload={args.payload})...")
        results = await export_all_sensors(sensors, output_dir, data_types)
        print_export_summary(results)
    finally:
        await _disconnect_all(sensors)


async def cmd_full(args: argparse.Namespace) -> None:
    """Cycle complet : sync → record → stop → export."""
    output_dir = Path(args.output)
    data_types = PAYLOAD_MAP[args.payload]

    devices = await _scan_and_filter(
        timeout=args.scan_timeout,
        max_per_adapter=args.max_per_adapter,
    )
    if not devices:
        print("Aucun capteur trouvé.")
        return

    sensors = await _connect_all(devices)
    if not sensors:
        return

    try:
        # --- Sync ---
        print("=" * 55)
        print("ÉTAPE 1 / 4 — Synchronisation")
        print("=" * 55)
        sync_result = await synchronize_sensors(sensors, settle_time=2.0, verify_state=False)
        print(f"  {sync_result}\n")

        # --- Vérification UTC ---
        print("=" * 55)
        print("VÉRIFICATION UTC (robustesse horloge)")
        print("=" * 55)
        utc_status = await get_utc_status()
        print(f"  {utc_status}")
        if not utc_status.is_synchronized or utc_status.drift_seconds > 1.0:
            print("\n  ⚠️  Conseil: pour enregistrements long, synchronisez NTP")
            print("     Voir: timedatectl set-ntp true")
        print()

        # --- Start recording ---
        print("=" * 55)
        print("ÉTAPE 2 / 4 — Démarrage enregistrement")
        print("=" * 55)
        start_result = await start_all(sensors)
        print(f"  {start_result}")
        if start_result.jitter_ms is not None:
            print(f"  Jitter start : {start_result.jitter_ms:.1f} ms\n")

        # --- Wait ---
        await wait_duration(args.duration)

        # --- Stop recording ---
        print("=" * 55)
        print("ÉTAPE 3 / 4 — Arrêt enregistrement")
        print("=" * 55)
        stop_result = await stop_all(sensors)
        print(f"  {stop_result}\n")

        # --- Export ---
        print("=" * 55)
        print(f"ÉTAPE 4 / 4 — Export flash → {output_dir}/")
        print("=" * 55)
        results = await export_all_sensors(sensors, output_dir, data_types)
        print_export_summary(results)

    finally:
        await _disconnect_all(sensors)


async def cmd_campaign(args: argparse.Namespace) -> None:
    """Campagne de reproductibilité: N runs scan→connect→sync→start→stop."""
    print("=" * 72)
    print("CAMPAGNE FIABILITÉ")
    print("=" * 72)
    print(
        f"runs={args.runs}  duration={args.duration:.1f}s  "
        f"scan_timeout={args.scan_timeout:.1f}s  max_per_adapter={args.max_per_adapter}"
    )
    if args.expected_count is not None:
        print(f"expected_count={args.expected_count}")
    print()

    summary = await run_reliability_campaign(
        runs=args.runs,
        duration=args.duration,
        scan_timeout=args.scan_timeout,
        max_per_adapter=args.max_per_adapter,
        expected_count=args.expected_count,
        cooldown=args.cooldown,
        event_callback=print,
    )
    print()
    for line in format_campaign_summary(summary):
        print(line)


async def cmd_check_utc(args: argparse.Namespace) -> None:
    """Vérifie l'état de synchronisation UTC du système."""
    print("=" * 60)
    print("VÉRIFICATION UTC (ROBUSTESSE HORLOGE)")
    print("=" * 60)
    print()
    
    utc_status = await get_utc_status()
    
    # Afficher le statut
    print(str(utc_status))
    print()
    
    # Fournir des recommandations
    severity = utc_status.severity()
    if severity == "ERROR":
        print("❌ ERREUR CRITIQUE — Impossible de vérifier l'UTC")
        print("   Action: installez timedatectl ou ntpq")
        sys.exit(1)
    elif severity == "WARNING":
        if not utc_status.is_synchronized:
            print("⚠️  ACTION RECOMMANDÉE : Synchroniser NTP")
            print()
            commands = get_ntp_sync_commands()
            for tool, cmds in commands.items():
                print(f"   [{tool}]:")
                for cmd in cmds:
                    print(f"      {cmd}")
        elif utc_status.drift_seconds > 1.0:
            print("⚠️  ACTION RECOMMANDÉE : Corriger dérive UTC")
            print(f"   Dérive actuelle : {utc_status.drift_seconds:.2f}s")
            print()
            print("   Essayez:")
            print("      sudo timedatectl set-ntp true")
            print("      sudo timedatectl set-time-zone UTC")
    else:
        print("✓ UTC est correctement synchronisé")
        print(f"  Drift mesurés : {utc_status.drift_seconds:.3f}s")
        print("  OK pour enregistrement synchronisé\n")
        sys.exit(0)
    
    sys.exit(0 if severity == "OK" else 1)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xdot",
        description="Gestionnaire multi-adaptateur BLE pour Xsens DOT",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Activer les logs DEBUG",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # adapters
    sub.add_parser("adapters", help="Lister les adaptateurs Bluetooth disponibles")

    # scan
    p_scan = sub.add_parser("scan", help="Scanner les capteurs Xsens DOT")
    p_scan.add_argument("--timeout", type=float, default=5.0,
                        help="Durée du scan BLE en secondes (défaut : 5)")
    p_scan.add_argument("--max-per-adapter", type=int, default=SAFE_DEFAULT_MAX_PER_ADAPTER,
                        dest="max_per_adapter",
                        help=f"Nombre max de capteurs par adaptateur (défaut : {SAFE_DEFAULT_MAX_PER_ADAPTER})")

    # record
    p_rec = sub.add_parser("record", help="Synchroniser et enregistrer")
    p_rec.add_argument("--duration", type=float, default=60.0,
                       help="Durée d'enregistrement en secondes (défaut : 60)")
    p_rec.add_argument("--scan-timeout", type=float, default=5.0,
                       dest="scan_timeout",
                       help="Durée du scan initial (défaut : 5)")
    p_rec.add_argument("--max-per-adapter", type=int, default=SAFE_DEFAULT_MAX_PER_ADAPTER,
                       dest="max_per_adapter")

    # export
    p_exp = sub.add_parser("export", help="Exporter la mémoire flash vers CSV")
    p_exp.add_argument("--output", default="./xdot_data",
                       help="Répertoire de sortie (défaut : ./xdot_data)")
    p_exp.add_argument("--payload", choices=list(PAYLOAD_MAP), default="euler",
                       help="Type de données à exporter (défaut : euler)")
    p_exp.add_argument("--scan-timeout", type=float, default=5.0,
                       dest="scan_timeout")
    p_exp.add_argument("--max-per-adapter", type=int, default=SAFE_DEFAULT_MAX_PER_ADAPTER,
                       dest="max_per_adapter")

    # full
    p_full = sub.add_parser("full", help="Cycle complet : sync → record → stop → export")
    p_full.add_argument("--duration", type=float, default=60.0,
                        help="Durée d'enregistrement (défaut : 60s)")
    p_full.add_argument("--output", default="./xdot_data",
                        help="Répertoire de sortie CSV (défaut : ./xdot_data)")
    p_full.add_argument("--payload", choices=list(PAYLOAD_MAP), default="euler",
                        help="Type de données (défaut : euler)")
    p_full.add_argument("--scan-timeout", type=float, default=5.0,
                        dest="scan_timeout")
    p_full.add_argument("--max-per-adapter", type=int, default=SAFE_DEFAULT_MAX_PER_ADAPTER,
                        dest="max_per_adapter")

    # campaign
    p_campaign = sub.add_parser(
        "campaign",
        help="Campagne fiabilité (runs répétés sync→record→stop)",
    )
    p_campaign.add_argument("--runs", type=int, default=5,
                            help="Nombre de runs à enchaîner (défaut : 5)")
    p_campaign.add_argument("--duration", type=float, default=10.0,
                            help="Durée d'enregistrement par run en secondes (défaut : 10)")
    p_campaign.add_argument("--scan-timeout", type=float, default=8.0,
                            dest="scan_timeout",
                            help="Durée du scan initial par run (défaut : 8)")
    p_campaign.add_argument("--cooldown", type=float, default=2.0,
                            help="Pause entre deux runs en secondes (défaut : 2)")
    p_campaign.add_argument("--expected-count", type=int, default=None,
                            dest="expected_count",
                            help="Nombre attendu de capteurs connectés (sinon run KO)")
    p_campaign.add_argument("--max-per-adapter", type=int, default=SAFE_DEFAULT_MAX_PER_ADAPTER,
                            dest="max_per_adapter")

    # check-utc
    p_check_utc = sub.add_parser(
        "check-utc",
        help="Vérifier la synchronisation UTC du système (robustesse horloge)",
    )

    return parser


# ---------------------------------------------------------------------------
# Entrée principale
# ---------------------------------------------------------------------------

COMMANDS = {
    "adapters":  cmd_adapters,
    "scan":      cmd_scan,
    "record":    cmd_record,
    "export":    cmd_export,
    "full":      cmd_full,
    "campaign":  cmd_campaign,
    "check-utc": cmd_check_utc,
}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    _setup_logging(args.verbose)

    command = COMMANDS[args.command]
    try:
        asyncio.run(command(args))
    except KeyboardInterrupt:
        print("\nInterrompu par l'utilisateur.")
        sys.exit(0)
    except Exception as exc:
        print(f"\n[ERREUR FATALE] {exc}", file=sys.stderr)
        if args.verbose:
            raise
        sys.exit(1)


if __name__ == "__main__":
    main()
