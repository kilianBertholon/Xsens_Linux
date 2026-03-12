#!/usr/bin/env python3
"""
test_adapters.py — Vérifie que chaque adaptateur BT détecté peut scanner indépendamment.

Exécution :
    python tests/test_adapters.py

Ce script NE nécessite PAS de capteurs allumés pour fonctionner.
Il vérifie simplement que BlueZ répond sur chaque hciX.
"""
import asyncio
import sys
from pathlib import Path

# Permettre l'import sans installation du package
sys.path.insert(0, str(Path(__file__).parent.parent))

from xdot_manager.adapters import list_adapters, bring_up_adapter
from xdot_manager.scanner import _scan_one_adapter


async def test_one_adapter(adapter, timeout: float = 3.0) -> bool:
    """Lance un scan de 3s sur un adaptateur et retourne True si bleak ne crash pas."""
    print(f"  Test {adapter.name} ({adapter.address})...", end=" ", flush=True)
    try:
        found = await _scan_one_adapter(adapter, timeout)
        xsens = {k: v for k, v in found.items()}
        dot_count = len(xsens)
        print(f"OK — {len(found)} périphérique(s) BLE détecté(s) dont {dot_count} Xsens DOT")
        return True
    except Exception as exc:
        print(f"ERREUR — {exc}")
        return False


async def main() -> None:
    print("=" * 60)
    print("TEST DES ADAPTATEURS BLUETOOTH")
    print("=" * 60)

    adapters = list_adapters(include_down=True)
    if not adapters:
        print("\n[ERREUR] Aucun adaptateur Bluetooth trouvé dans /sys/class/bluetooth/")
        print("  → Vérifiez que le Bluetooth est activé (rfkill list)")
        sys.exit(1)

    print(f"\n{len(adapters)} adaptateur(s) détecté(s) :\n")
    for a in adapters:
        status = "UP" if a.is_up else "DOWN"
        print(f"  {a.name}  {a.address}  [{status}]")

    # Tenter de monter les adaptateurs DOWN
    down_adapters = [a for a in adapters if not a.is_up]
    if down_adapters:
        print(f"\n{len(down_adapters)} adaptateur(s) DOWN — tentative de montée :")
        for a in down_adapters:
            ok = bring_up_adapter(a)
            print(f"  {a.name} : {'UP' if ok else 'impossible (droits insuffisants ?)'}")

    up_adapters = [a for a in adapters if a.is_up]
    if not up_adapters:
        print("\n[ERREUR] Aucun adaptateur actif.")
        sys.exit(1)

    print(f"\nTest de scan (3s) sur {len(up_adapters)} adaptateur(s) :\n")
    results = []
    for adapter in up_adapters:
        ok = await test_one_adapter(adapter)
        results.append((adapter.name, ok))

    print("\n" + "=" * 60)
    print("RÉSUMÉ")
    print("=" * 60)
    all_ok = all(ok for _, ok in results)
    for name, ok in results:
        print(f"  {name}: {'✓ OK' if ok else '✗ ERREUR'}")
    print()
    if all_ok:
        print(f"Tous les {len(results)} adaptateur(s) fonctionnent.")
    else:
        failed = [n for n, ok in results if not ok]
        print(f"Adaptateurs en erreur : {failed}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
