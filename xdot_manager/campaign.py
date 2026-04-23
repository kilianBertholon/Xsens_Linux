from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from statistics import mean
from typing import Callable, Optional

from .adapters import list_adapters
from .scanner import scan_for_dots
from .sensor import DotSensor, DotConnectError
from .sync import synchronize_sensors
from .recording import start_all, stop_all


@dataclass
class CampaignRunResult:
    run_index: int
    ok: bool
    detected: int
    connected: int
    sync_ms: float
    start_ms: float
    stop_ms: float
    jitter_ms: Optional[float]
    errors: list[str] = field(default_factory=list)


@dataclass
class CampaignSummary:
    runs: int
    run_results: list[CampaignRunResult]

    @property
    def ok_runs(self) -> int:
        return sum(1 for r in self.run_results if r.ok)

    @property
    def ko_runs(self) -> int:
        return self.runs - self.ok_runs

    @property
    def success_pct(self) -> float:
        return (self.ok_runs / self.runs * 100.0) if self.runs else 0.0


def format_campaign_summary(summary: CampaignSummary) -> list[str]:
    sync_vals = [r.sync_ms for r in summary.run_results if r.sync_ms > 0]
    start_vals = [r.start_ms for r in summary.run_results if r.start_ms > 0]
    stop_vals = [r.stop_ms for r in summary.run_results if r.stop_ms > 0]
    jitter_vals = [r.jitter_ms for r in summary.run_results if r.jitter_ms is not None]

    lines: list[str] = [
        "=" * 72,
        "RÉSUMÉ CAMPAGNE",
        "=" * 72,
        (
            f"Runs: {summary.runs} | OK: {summary.ok_runs} | KO: {summary.ko_runs} | "
            f"Success: {summary.success_pct:.1f}%"
        ),
    ]

    if sync_vals:
        lines.append(
            f"Sync   ms: avg={mean(sync_vals):.0f}  min={min(sync_vals):.0f}  max={max(sync_vals):.0f}"
        )
    if start_vals:
        lines.append(
            f"Start  ms: avg={mean(start_vals):.0f}  min={min(start_vals):.0f}  max={max(start_vals):.0f}"
        )
    if stop_vals:
        lines.append(
            f"Stop   ms: avg={mean(stop_vals):.0f}  min={min(stop_vals):.0f}  max={max(stop_vals):.0f}"
        )
    if jitter_vals:
        lines.append(
            f"Jitter ms: avg={mean(jitter_vals):.1f}  min={min(jitter_vals):.1f}  max={max(jitter_vals):.1f}"
        )

    error_counts: dict[str, int] = {}
    for r in summary.run_results:
        for e in r.errors:
            key = e.split(":", 1)[0]
            error_counts[key] = error_counts.get(key, 0) + 1

    if error_counts:
        lines.append("Erreurs fréquentes:")
        for key, cnt in sorted(error_counts.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  {key}: {cnt}")

    return lines


async def run_reliability_campaign(
    runs: int = 5,
    duration: float = 10.0,
    scan_timeout: float = 8.0,
    max_per_adapter: int = 8,
    expected_count: Optional[int] = None,
    cooldown: float = 2.0,
    force_output_rate: Optional[int] = 120,
    event_callback: Optional[Callable[[str], None]] = None,
) -> CampaignSummary:
    """Exécute N runs scan→connect→sync→start→stop et retourne un résumé."""

    def _emit(msg: str) -> None:
        if event_callback:
            event_callback(msg)

    run_results: list[CampaignRunResult] = []

    for run_idx in range(1, runs + 1):
        sensors: list[DotSensor] = []
        detected = 0
        connected = 0
        sync_ms = 0.0
        start_ms = 0.0
        stop_ms = 0.0
        jitter_ms: Optional[float] = None
        errors: list[str] = []

        _emit("-" * 72)
        _emit(f"RUN {run_idx}/{runs}")
        _emit("-" * 72)

        try:
            adapters = list_adapters()
            if not adapters:
                errors.append("Aucun adaptateur Bluetooth trouvé")
                run_results.append(
                    CampaignRunResult(
                        run_index=run_idx,
                        ok=False,
                        detected=detected,
                        connected=connected,
                        sync_ms=sync_ms,
                        start_ms=start_ms,
                        stop_ms=stop_ms,
                        jitter_ms=jitter_ms,
                        errors=errors,
                    )
                )
                continue

            devices = await scan_for_dots(
                timeout=scan_timeout,
                adapters=adapters,
                max_per_adapter=max_per_adapter,
            )
            detected = len(devices)
            if detected == 0:
                errors.append("Aucun capteur détecté")
                run_results.append(
                    CampaignRunResult(
                        run_index=run_idx,
                        ok=False,
                        detected=detected,
                        connected=connected,
                        sync_ms=sync_ms,
                        start_ms=start_ms,
                        stop_ms=stop_ms,
                        jitter_ms=jitter_ms,
                        errors=errors,
                    )
                )
                continue

            async def _connect_one(d) -> Optional[DotSensor]:
                s = DotSensor(d.address, adapter=d.adapter, name=d.address)
                try:
                    await s.connect()
                    return s
                except DotConnectError as exc:
                    errors.append(f"connect:{d.address}:{exc}")
                    return None

            conn = await asyncio.gather(*[_connect_one(d) for d in devices])
            sensors = [s for s in conn if s is not None]
            connected = len(sensors)

            if connected == 0:
                errors.append("Aucun capteur connecté")
                run_results.append(
                    CampaignRunResult(
                        run_index=run_idx,
                        ok=False,
                        detected=detected,
                        connected=connected,
                        sync_ms=sync_ms,
                        start_ms=start_ms,
                        stop_ms=stop_ms,
                        jitter_ms=jitter_ms,
                        errors=errors,
                    )
                )
                continue

            if expected_count is not None and connected < expected_count:
                errors.append(f"count:{connected}/{expected_count}")

            if force_output_rate is not None:
                _emit(f"Forçage taux acquisition → {force_output_rate} Hz")
                rate_errors: list[str] = []
                for s in sensors:
                    try:
                        await s.cmd_set_output_rate(force_output_rate)
                    except Exception as exc:
                        rate_errors.append(f"rate:{s.address}:{exc}")
                if rate_errors:
                    errors.extend(rate_errors)

            sync_result = await synchronize_sensors(
                sensors,
                settle_time=2.0,
                verify_state=False,
                await_sync_ack=False,
            )
            sync_ms = sync_result.duration_ms
            if not sync_result.success:
                errors.extend([f"sync:{addr}:{msg}" for addr, msg in sync_result.errors.items()])

            start_result = await start_all(sensors)
            start_ms = start_result.total_duration_ms
            jitter_ms = start_result.jitter_ms
            if not start_result.success:
                errors.extend([f"start:{addr}:{msg}" for addr, msg in start_result.errors.items()])

            await asyncio.sleep(max(duration, 0.0))

            stop_result = await stop_all(sensors)
            stop_ms = stop_result.total_duration_ms
            if not stop_result.success:
                errors.extend([f"stop:{addr}:{msg}" for addr, msg in stop_result.errors.items()])

            ok = len(errors) == 0
            run_results.append(
                CampaignRunResult(
                    run_index=run_idx,
                    ok=ok,
                    detected=detected,
                    connected=connected,
                    sync_ms=sync_ms,
                    start_ms=start_ms,
                    stop_ms=stop_ms,
                    jitter_ms=jitter_ms,
                    errors=errors,
                )
            )

            _emit(
                f"RUN {run_idx}: {'OK' if ok else 'KO'} | "
                f"detected={detected} connected={connected} | "
                f"sync={sync_ms:.0f}ms start={start_ms:.0f}ms stop={stop_ms:.0f}ms "
                f"jitter={'?' if jitter_ms is None else f'{jitter_ms:.1f}ms'}"
            )
            if errors:
                for e in errors[:10]:
                    _emit(f"  - {e}")
                if len(errors) > 10:
                    _emit(f"  - ... {len(errors) - 10} erreur(s) supplémentaire(s)")

        except Exception as exc:
            errors.append(f"run_exception:{exc}")
            run_results.append(
                CampaignRunResult(
                    run_index=run_idx,
                    ok=False,
                    detected=detected,
                    connected=connected,
                    sync_ms=sync_ms,
                    start_ms=start_ms,
                    stop_ms=stop_ms,
                    jitter_ms=jitter_ms,
                    errors=errors,
                )
            )
            _emit(f"RUN {run_idx}: KO (exception) — {exc}")

        finally:
            if sensors:
                await asyncio.gather(*[s.disconnect() for s in sensors], return_exceptions=True)
            if run_idx < runs and cooldown > 0:
                await asyncio.sleep(cooldown)

    return CampaignSummary(runs=runs, run_results=run_results)
