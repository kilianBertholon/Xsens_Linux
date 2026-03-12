#!/usr/bin/env python3
"""
test_sync.py — Vérifie la synchronisation temporelle entre 2 capteurs puis N capteurs.

Ce test :
1. Connecte 2 capteurs (ou N avec --count)
2. Les synchronise via start_syncing()
3. Démarre un enregistrement court (5s par défaut)
4. Exporte la flash et compare les timestamps

Exécution :
    python tests/test_sync.py --count 2 --duration 5
    python tests/test_sync.py --count 4 --duration 10
"""
import argparse
import asyncio
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from xdot_manager.adapters import list_adapters, print_adapter_summary
from xdot_manager.scanner import scan_for_dots, print_scan_results
from xdot_manager.sensor import DotSensor, DotError, DotConnectError
from xdot_manager.sync import synchronize_sensors
from xdot_manager.recording import start_all, stop_all, wait_duration
from xdot_manager.export import export_all_sensors, PRESET_EULER


async def connect_sensors(devices) -> list[DotSensor]:
    sensors = [DotSensor(d.address, adapter=d.adapter, name=d.address[:8]) for d in devices]

    async def _c(s):
        try:
            await s.connect()
            return s
        except DotConnectError as exc:
            print(f"  [ERREUR connexion] {exc}")
            return None

    results = await asyncio.gather(*[_c(s) for s in sensors])
    return [s for s in results if s is not None]


def analyze_sync(output_dir: Path, addresses: list[str]) -> None:
    """
    Compare les timestamps du premier échantillon de chaque capteur.
    Un bon résultat : delta max < 1 sample = 20ms (à 50 Hz).
    """
    print("\n" + "=" * 60)
    print("ANALYSE DE LA SYNCHRONISATION TEMPORELLE")
    print("=" * 60)

    first_timestamps: dict[str, float] = {}

    for addr in addresses:
        addr_clean = addr.replace(":", "-")
        csv_files = sorted(output_dir.glob(f"{addr_clean}_file*.csv"))
        if not csv_files:
            print(f"  {addr} : aucun fichier CSV généré")
            continue
        csv_path = csv_files[0]
        try:
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if "timestamp_ms" in row:
                        first_timestamps[addr] = float(row["timestamp_ms"])
                        break
        except Exception as exc:
            print(f"  {addr} : erreur lecture CSV — {exc}")

    if len(first_timestamps) < 2:
        print("  Pas assez de données pour comparer.")
        return

    ts_values = list(first_timestamps.values())
    min_ts = min(ts_values)
    max_ts = max(ts_values)
    delta_ms = max_ts - min_ts

    print(f"\n  Premier timestamp par capteur :")
    for addr, ts in sorted(first_timestamps.items()):
        offset = ts - min_ts
        print(f"    {addr}  {ts:.0f} ms  Δ={offset:+.1f} ms")

    print(f"\n  Jitter maximal observé : {delta_ms:.1f} ms")
    threshold_ms = 25.0  # 1 sample à 40 Hz + marge
    if delta_ms <= threshold_ms:
        print(f"  ✓ Synchronisation correcte (seuil : {threshold_ms:.0f} ms)")
    else:
        print(f"  ✗ Synchronisation insuffisante (seuil : {threshold_ms:.0f} ms)")
        print("    → Essayer d'augmenter settle_time dans synchronize_sensors()")


async def run(count: int, duration: float, scan_timeout: float, erase: bool = False) -> None:
    print("=" * 60)
    print(f"TEST DE SYNCHRONISATION ({count} capteurs, {duration:.0f}s)")
    print("=" * 60)

    adapters = list_adapters()
    if not adapters:
        print("[ERREUR] Aucun adaptateur Bluetooth.")
        sys.exit(1)
    print_adapter_summary(adapters)

    print(f"Scan ({scan_timeout:.0f}s)...")
    devices = await scan_for_dots(timeout=scan_timeout, adapters=adapters)
    if not devices:
        print("[ERREUR] Aucun capteur détecté.")
        sys.exit(1)

    devices = devices[:count * 2]   # marge x2 pour absorber les échecs de connexion
    print_scan_results(devices)

    sensors = await connect_sensors(devices)
    if len(sensors) < 2:
        print("[ERREUR] Moins de 2 capteurs connectés.")
        await asyncio.gather(*[s.disconnect() for s in sensors])
        sys.exit(1)

    # Limiter au nombre demandé
    if len(sensors) > count:
        extra = sensors[count:]
        sensors = sensors[:count]
        await asyncio.gather(*[s.disconnect() for s in extra])

    output_dir = Path("./xdot_sync_test")
    addresses = [s.address for s in sensors]

    # Afficher l'état réel de chaque capteur après connexion
    from xdot_manager.protocol.gatt import STATE_NAMES
    print("\n  État des capteurs après connexion :")
    offline: list[DotSensor] = []
    for s in sensors:
        try:
            st = await s.cmd_get_state()
            print(f"    {s.address}  →  {STATE_NAMES.get(st, f'0x{st:02x}')}")
        except Exception as exc:
            print(f"    {s.address}  →  [ERREUR: {exc}]")
            offline.append(s)
    if offline:
        print(f"  [AVERTISSEMENT] {len(offline)} capteur(s) inaccessible(s) — retirés de la liste.")
        for s in offline:
            sensors.remove(s)
            await s.disconnect()

    try:
        # Effacement flash (optionnel)
        if erase:
            print(f"\n[0/3] Effacement flash ({len(sensors)} capteurs)... (peut prendre 1-5 min)")
            erase_tasks = [s.cmd_erase_flash(timeout=300.0) for s in sensors]
            erase_results = await asyncio.gather(*erase_tasks, return_exceptions=True)
            ok_count = sum(1 for r in erase_results if not isinstance(r, Exception))
            print(f"  Effacement : {ok_count}/{len(sensors)} OK")
            for s, r in zip(sensors, erase_results):
                if isinstance(r, Exception):
                    print(f"    {s.address} : ERREUR — {r}")
            if ok_count < len(sensors):
                print("  [ATTENTION] Certains effacements échoués.")
                print("              L'enregistrement peut échouer si la flash n'est pas libérée.")

        # Sync
        print("\n[1/3] Synchronisation...")
        result = await synchronize_sensors(sensors, settle_time=2.0, verify_state=False)
        print(f"  {result}")

        # Record
        print("\n[2/3] Enregistrement...")
        start_r = await start_all(sensors)
        print(f"  {start_r}")
        if start_r.jitter_ms:
            print(f"  Jitter start ACK : {start_r.jitter_ms:.1f} ms")
        await wait_duration(duration, "Test sync")
        stop_r = await stop_all(sensors)
        print(f"  {stop_r}")

        # Export
        print(f"\n[3/3] Export flash → {output_dir}/")
        export_results = await export_all_sensors(sensors, output_dir, PRESET_EULER)
        for r in export_results:
            print(f"  {r}")

    finally:
        await asyncio.gather(*[s.disconnect() for s in sensors])

    # Analyse
    analyze_sync(output_dir, addresses)


def main() -> None:
    parser = argparse.ArgumentParser(description="Test de synchronisation Xsens DOT")
    parser.add_argument("--count", type=int, default=2,
                        help="Nombre de capteurs (défaut: 2)")
    parser.add_argument("--duration", type=float, default=5.0,
                        help="Durée d'enregistrement en secondes (défaut: 5)")
    parser.add_argument("--scan-timeout", type=float, default=8.0, dest="scan_timeout")
    parser.add_argument("--erase", action="store_true",
                        help="Effacer la flash avant d'enregistrer (détruit les données existantes)")
    args = parser.parse_args()
    asyncio.run(run(args.count, args.duration, args.scan_timeout, args.erase))


if __name__ == "__main__":
    main()
