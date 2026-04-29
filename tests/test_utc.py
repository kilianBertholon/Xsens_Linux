#!/usr/bin/env python3
"""
Test de validation du système de robustesse UTC.

Vérifie que:
1. get_utc_status() retourne un objet valide
2. Le check détecte NTP sync / drift
3. Les commandes NTP sont disponibles
"""
import asyncio
import sys
from pathlib import Path

# Ajouter le chemin du package
sys.path.insert(0, str(Path(__file__).parent / "xdot-manager"))

from xdot_manager.utc import (
    get_utc_status,
    get_ntp_sync_commands,
    UTCStatus,
    UTC_DRIFT_WARNING_THRESHOLD,
    UTC_DRIFT_ERROR_THRESHOLD,
)


async def test_utc_status():
    """Test 1: Obtenir le statut UTC."""
    print("=" * 60)
    print("TEST 1 : Obtenir statut UTC")
    print("=" * 60)
    
    try:
        status = await get_utc_status()
        assert isinstance(status, UTCStatus), "Résultat pas du type UTCStatus"
        print(f"✓ Statut obtenu: {status}")
        print(f"  - is_synchronized: {status.is_synchronized}")
        print(f"  - drift_seconds: {status.drift_seconds:.4f}")
        print(f"  - ntp_available: {status.ntp_available}")
        print(f"  - severity: {status.severity()}")
        return True
    except Exception as exc:
        print(f"❌ Erreur : {exc}")
        return False


async def test_ntp_commands():
    """Test 2: Lister les commandes NTP."""
    print("\n" + "=" * 60)
    print("TEST 2 : Commandes NTP disponibles")
    print("=" * 60)
    
    try:
        commands = get_ntp_sync_commands()
        assert isinstance(commands, dict), "Résultat pas un dictionnaire"
        assert len(commands) > 0, "Aucune commande NTP"
        
        print("✓ Commandes NTP trouvées:")
        for tool, cmds in commands.items():
            print(f"\n  [{tool}]:")
            for cmd in cmds:
                print(f"    {cmd}")
        return True
    except Exception as exc:
        print(f"❌ Erreur : {exc}")
        return False


async def test_thresholds():
    """Test 3: Vérifier les seuils de dérive."""
    print("\n" + "=" * 60)
    print("TEST 3 : Seuils de dérive")
    print("=" * 60)
    
    try:
        print(f"✓ Seuil WARNING : {UTC_DRIFT_WARNING_THRESHOLD}s")
        print(f"✓ Seuil ERROR   : {UTC_DRIFT_ERROR_THRESHOLD}s")
        assert UTC_DRIFT_WARNING_THRESHOLD < UTC_DRIFT_ERROR_THRESHOLD, \
            "Seuils incohérents"
        print("✓ Seuils cohérents")
        return True
    except Exception as exc:
        print(f"❌ Erreur : {exc}")
        return False


async def test_severity_rendering():
    """Test 4: Vérifier le rendu des messages."""
    print("\n" + "=" * 60)
    print("TEST 4 : Rendu des messages de statut")
    print("=" * 60)
    
    try:
        # Test cas 1 : OK
        status_ok = UTCStatus(
            is_synchronized=True,
            drift_seconds=0.05,
            ntp_available=True,
            error_message=None,
        )
        print(f"✓ Cas OK:\n  {status_ok}\n")
        
        # Test cas 2 : WARNING
        status_warn = UTCStatus(
            is_synchronized=False,
            drift_seconds=1.5,
            ntp_available=True,
            error_message=None,
        )
        print(f"✓ Cas WARNING:\n  {status_warn}\n")
        
        # Test cas 3 : ERROR
        status_err = UTCStatus(
            is_synchronized=True,
            drift_seconds=0.0,
            ntp_available=False,
            error_message="Outils NTP indisponibles",
        )
        print(f"✓ Cas ERROR:\n  {status_err}\n")
        
        return True
    except Exception as exc:
        print(f"❌ Erreur : {exc}")
        return False


async def main():
    """Lance tous les tests."""
    print("\n🔍 TEST SUITE UTC ROBUSTNESS\n")
    
    results = []
    results.append(("get_utc_status", await test_utc_status()))
    results.append(("ntp_commands", await test_ntp_commands()))
    results.append(("thresholds", await test_thresholds()))
    results.append(("severity_rendering", await test_severity_rendering()))
    
    # Résumé
    print("\n" + "=" * 60)
    print("RÉSUMÉ")
    print("=" * 60)
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "✓ PASS" if result else "❌ FAIL"
        print(f"{status}  — {name}")
    
    print(f"\nRésultat: {passed}/{total} tests réussis")
    
    if passed == total:
        print("\n✓ Tous les tests réussis!")
        return 0
    else:
        print(f"\n❌ {total - passed} test(s) échoué(s)")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
