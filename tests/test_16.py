#!/usr/bin/env python3
"""
test_16.py — Test de charge complet avec tous les capteurs disponibles.

Ce script est le test de validation finale.
Il reproduit un cycle complet de mesure sur tous les capteurs détectés,
avec statistiques détaillées.

Exécution :
    python tests/test_16.py --duration 10
    python tests/test_16.py --duration 60 --payload euler --output ./resultats
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from xdot_manager.adapters import list_adapters, print_adapter_summary
from xdot_manager.scanner import scan_for_dots, print_scan_results
from xdot_manager.sensor import DotSensor, DotError, DotConnectError
from xdot_manager.sync import synchronize_sensors
from xdot_manager.recording import start_all, stop_all, wait_duration
from xdot_manager.export import export_all_sensors, print_export_summary, PAYLOAD_MAP


def _separator(title: str = "") -> None:
    if title:
        pad = max(0, 58 - len(title) - 4)
        print(f"\n{'='*2} {title} {'='*pad}")
    else:
        print("=" * 60)


async def run(
    duration: float,
    payload: str,
    output: str,
    scan_timeout: float,
    max_per_adapter: int,
) -> None:
    data_types = PAYLOAD_MAP[payload]
    output_dir = Path(output)

    _separator("TEST CHARGE COMPLÈTE — XSENS DOT")
    print(f"  Durée enregistrement : {duration:.0f}s")
    print(f"  Payload              : {payload}")
    print(f"  Répertoire sortie    : {output_dir}")

    # --- Adaptateurs ---
    _separator("ADAPTATEURS")
    adapters = list_adapters()
    if not adapters:
        print("[ERREUR] Aucun adaptateur Bluetooth.")
        sys.exit(1)
    print_adapter_summary(adapters)

    # --- Scan ---
    _separator("SCAN BLE")
    print(f"Scan sur {len(adapters)} adaptateur(s) pendant {scan_timeout:.0f}s...")
    devices = await scan_for_dots(
        timeout=scan_timeout,
        adapters=adapters,
        max_per_adapter=max_per_adapter,
    )
    if not devices:
        print("[ERREUR] Aucun capteur détecté. Vérifiez que les capteurs sont allumés.")
        sys.exit(1)
    print_scan_results(devices)

    if len(devices) < 16:
        print(f"  ⚠ {len(devices)}/16 capteur(s) détecté(s).")

    # --- Connexion ---
    _separator("CONNEXION")
    t0 = time.monotonic()
    sensors: list[DotSensor] = []

    async def _connect_one(d) -> DotSensor | None:
        s = DotSensor(d.address, adapter=d.adapter, name=d.address[:8])
        try:
            await s.connect()
            return s
        except DotConnectError as exc:
            print(f"  [ERREUR] {d.address} : {exc}")
            return None

    results = await asyncio.gather(*[_connect_one(d) for d in devices])
    sensors = [s for s in results if s is not None]
    connect_time = time.monotonic() - t0

    print(f"  {len(sensors)}/{len(devices)} capteurs connectés en {connect_time:.1f}s")
    if not sensors:
        sys.exit(1)

    # Distribution par adaptateur
    adapter_dist: dict[str, int] = {}
    for s in sensors:
        name = s.adapter.name if s.adapter else "?"
        adapter_dist[name] = adapter_dist.get(name, 0) + 1
    for hci, cnt in sorted(adapter_dist.items()):
        print(f"    {hci} : {cnt} capteur(s)")

    try:
        # --- Sync ---
        _separator("SYNCHRONISATION")
        sync_result = await synchronize_sensors(sensors, settle_time=2.0, verify_state=False)
        print(f"  {sync_result}")
        if sync_result.failed_sensors:
            print(f"  Capteurs non synchronisés : {sync_result.failed_sensors}")

        # --- Start recording ---
        _separator("DÉMARRAGE ENREGISTREMENT")
        start_r = await start_all(sensors)
        print(f"  {start_r}")
        if start_r.jitter_ms is not None:
            print(f"  Jitter start ACK : {start_r.jitter_ms:.1f} ms")
        if start_r.failed_sensors:
            print(f"  Capteurs en erreur : {start_r.failed_sensors}")

        # --- Attente ---
        await wait_duration(duration)

        # --- Stop recording ---
        _separator("ARRÊT ENREGISTREMENT")
        stop_r = await stop_all(sensors)
        print(f"  {stop_r}")

        # --- Export ---
        _separator(f"EXPORT FLASH → {output_dir}/")
        export_results = await export_all_sensors(sensors, output_dir, data_types)
        print_export_summary(export_results)

        # --- Rapport final ---
        _separator("RAPPORT FINAL")
        total_samples = sum(r.total_samples for r in export_results)
        ok_exports = sum(1 for r in export_results if r.success)
        print(f"  Connexions réussies  : {len(sensors)}/{len(devices)}")
        print(f"  Exports réussis      : {ok_exports}/{len(sensors)}")
        print(f"  Échantillons total   : {total_samples:,}")
        print(f"  Durée enregistrement : {duration:.0f}s")
        if start_r.jitter_ms:
            print(f"  Jitter sync ACK      : {start_r.jitter_ms:.1f} ms")

        if ok_exports == len(sensors) and len(sensors) == len(devices):
            print("\n  ✓ TEST COMPLET RÉUSSI")
        else:
            print("\n  ✗ TEST PARTIEL — voir les erreurs ci-dessus")

    finally:
        await asyncio.gather(*[s.disconnect() for s in sensors])
        print("\nDéconnexion terminée.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test de charge complet 16 capteurs Xsens DOT")
    parser.add_argument("--duration", type=float, default=10.0,
                        help="Durée d'enregistrement en secondes (défaut: 10)")
    parser.add_argument("--payload", choices=list(PAYLOAD_MAP), default="euler",
                        help="Type de données à exporter (défaut: euler)")
    parser.add_argument("--output", default="./xdot_test_16",
                        help="Répertoire de sortie (défaut: ./xdot_test_16)")
    parser.add_argument("--scan-timeout", type=float, default=10.0, dest="scan_timeout",
                        help="Durée du scan BLE (défaut: 10s)")
    parser.add_argument("--max-per-adapter", type=int, default=8, dest="max_per_adapter",
                        help="Capteurs max par adaptateur (défaut: 8)")
    args = parser.parse_args()

    asyncio.run(run(
        duration=args.duration,
        payload=args.payload,
        output=args.output,
        scan_timeout=args.scan_timeout,
        max_per_adapter=args.max_per_adapter,
    ))


if __name__ == "__main__":
    main()
