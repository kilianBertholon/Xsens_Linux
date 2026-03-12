#!/usr/bin/env python3
"""
test_connections.py — Teste N connexions BLE simultanées.

Allumez vos capteurs Xsens DOT avant de lancer ce script.
Commencez avec --count 3, puis montez progressivement jusqu'à 16.

Exécution :
    python tests/test_connections.py --count 3 --scan-timeout 8
    python tests/test_connections.py --count 16 --scan-timeout 10
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from xdot_manager.adapters import list_adapters, print_adapter_summary
from xdot_manager.scanner import scan_for_dots, print_scan_results
from xdot_manager.sensor import DotSensor, DotError


async def connect_and_get_state(sensor: DotSensor) -> tuple[bool, str, float]:
    """
    Connecte un capteur, lit son état, le déconnecte.
    Retourne (success, state_description, duration_s).
    """
    t0 = time.monotonic()
    try:
        await sensor.connect()
        await asyncio.sleep(0.3)
        try:
            state = await sensor.cmd_get_state()
            from xdot_manager.protocol.gatt import STATE_NAMES
            state_str = STATE_NAMES.get(state, f"Unknown({state:#04x})")
        except Exception as e:
            state_str = f"ConnOK/StateErr({type(e).__name__})"
        await sensor.disconnect()
        return True, state_str, time.monotonic() - t0
    except DotError as exc:
        try:
            await sensor.disconnect()
        except Exception:
            pass
        return False, str(exc)[:60], time.monotonic() - t0


async def test_connections(count: int, scan_timeout: float) -> None:
    print("=" * 60)
    print(f"TEST DE {count} CONNEXIONS SIMULTANÉES")
    print("=" * 60)

    adapters = list_adapters()
    if not adapters:
        print("[ERREUR] Aucun adaptateur Bluetooth.")
        sys.exit(1)

    print_adapter_summary(adapters)
    print(f"Scan ({scan_timeout:.0f}s)...")

    devices = await scan_for_dots(timeout=scan_timeout, adapters=adapters)
    print_scan_results(devices)

    if not devices:
        print("[ERREUR] Aucun capteur détecté. Vérifiez que les capteurs sont allumés.")
        sys.exit(1)

    # Limiter au nombre demandé
    devices = devices[:count]
    print(f"\nTest sur {len(devices)} capteur(s)...\n")

    sensors = [
        DotSensor(d.address, adapter=d.adapter, name=d.address[:8])
        for d in devices
    ]

    # Connexion en parallèle
    t_start = time.monotonic()
    tasks = [connect_and_get_state(s) for s in sensors]
    results = await asyncio.gather(*tasks)
    total_time = time.monotonic() - t_start

    # Rapport
    print(f"\n{'Adresse':<20} {'Adaptateur':<10} {'Résultat':<12} {'État':<12} {'Durée':>7}")
    print("-" * 68)
    ok_count = 0
    for dev, (success, state_str, dur) in zip(devices, results):
        adapter_name = dev.adapter.name if dev.adapter else "?"
        status = "OK" if success else "ERREUR"
        print(f"{dev.address:<20} {adapter_name:<10} {status:<12} {state_str:<12} {dur:>6.2f}s")
        if success:
            ok_count += 1

    print("\n" + "=" * 60)
    print(f"RÉSULTAT : {ok_count}/{len(devices)} connexions réussies en {total_time:.1f}s")
    if ok_count == len(devices):
        print("✓ Toutes les connexions ont réussi !")
    else:
        failed = len(devices) - ok_count
        print(f"✗ {failed} connexion(s) échouée(s)")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Test de connexions BLE multiples")
    parser.add_argument("--count", type=int, default=3,
                        help="Nombre de capteurs à connecter simultanément (défaut: 3)")
    parser.add_argument("--scan-timeout", type=float, default=8.0,
                        dest="scan_timeout",
                        help="Durée du scan BLE (défaut: 8s)")
    args = parser.parse_args()
    asyncio.run(test_connections(args.count, args.scan_timeout))


if __name__ == "__main__":
    main()
