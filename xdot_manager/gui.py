"""
Dashboard graphique Xsens DOT Manager — PyQt6 + qasync.

Usage :
    python -m xdot_manager.gui
    ou via l'entrée de script : xdot-gui
"""
from __future__ import annotations

import asyncio
import re
import signal
import subprocess
import sys
from datetime import datetime
from statistics import mean
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCloseEvent, QColor, QFont, QIcon, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QGroupBox,
    QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QStatusBar,
    QSpinBox,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)
import qasync

from .adapters import list_adapters
from .protocol.gatt import STATE_NAMES, STATE_IDLE, STATE_RECORDING, STATE_ERASING
from .scanner import scan_for_dots
from .sensor import DotSensor, DotConnectError, DotError

# ── Constantes visuelles ──────────────────────────────────────────────────────

_COLS = ["#", "Adresse MAC", "Nom", "Adaptateur", "RSSI", "État"]

_STATE_COLORS: dict[str, str] = {
    "Connecting":  "#8be9fd",   # bleu clair
    "Idle":        "#27ae60",   # vert
    "Syncing":     "#f1c40f",   # jaune
    "Synced":      "#00bcd4",   # cyan
    "Recording":   "#e74c3c",   # rouge
    "Erasing":     "#e67e22",   # orange
    "Exporting":   "#2980b9",   # bleu
    "ERREUR":      "#f38ba8",   # rose
    "—":           "#95a5a6",   # gris
}

_STYLE_SHEET = """
QMainWindow { background: #1e1e2e; }
QGroupBox {
    font-weight: bold;
    border: 1px solid #44475a;
    border-radius: 4px;
    margin-top: 6px;
    color: #cdd6f4;
}
QGroupBox::title { subcontrol-origin: margin; left: 8px; }
QTableWidget {
    background: #181825;
    color: #cdd6f4;
    gridline-color: #313244;
    selection-background-color: #45475a;
}
QHeaderView::section {
    background: #313244;
    color: #cdd6f4;
    padding: 4px;
    border: none;
}
QTextEdit {
    background: #181825;
    color: #a6e3a1;
    font-family: monospace;
    font-size: 9pt;
    border: none;
}
QPushButton {
    background: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 5px 14px;
    font-size: 9pt;
}
QPushButton:hover { background: #45475a; }
QPushButton:disabled { color: #6c7086; }
QLabel { color: #cdd6f4; }
QStatusBar { color: #a6adc8; background: #181825; }
QFrame#toolbar {
    background: #24273a;
    border-bottom: 1px solid #44475a;
    border-radius: 0;
}
"""

# ── Widget principal ──────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    """Dashboard principal Xsens DOT Manager."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Xsens DOT Manager")
        self.setMinimumSize(1100, 680)

        self._devices: list = []          # résultats du scan
        self._sensors: list[DotSensor] = []
        self._recording = False
        self._recording_start: Optional[datetime] = None
        self._output_dir = Path("./xdot_export")

        # Résultats du dernier export (pour analyse jitter)
        self._last_export_results: list = []
        self._last_export_dir: Optional[Path] = None

        # Métadonnées flash mises en cache (adresse → liste de FileMetadata)
        self._flash_metadata: dict[str, list] = {}
        # Taux d'acquisition observé par capteur (adresse → Hz)
        self._flash_sample_rates: dict[str, int] = {}
        # Cache adaptateurs visibles dans la toolbar
        self._known_adapters: list = []

        # Compteurs de progression sync
        self._sync_total = 0
        self._sync_idle_count = 0
        self._sync_synced_count = 0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick_timer)

        self._build_ui()
        self._refresh_adapter_indicator()
        self._log("Xsens DOT Manager démarré.")
        self._refresh_buttons()

    # ── Construction UI ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Toolbar
        root.addWidget(self._make_toolbar())

        # Corps (table | log)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(8, 8, 8, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QGroupBox("Capteurs détectés / connectés")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(4, 8, 4, 4)
        self._table = self._make_table()
        ll.addWidget(self._table)
        splitter.addWidget(left)

        right = QGroupBox("Journal")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 8, 4, 4)
        self._log_widget = QTextEdit()
        self._log_widget.setReadOnly(True)
        rl.addWidget(self._log_widget)
        # Bouton effacer le journal
        clear_btn = QPushButton("Effacer le journal")
        clear_btn.clicked.connect(self._log_widget.clear)
        rl.addWidget(clear_btn)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        body_layout.addWidget(splitter)
        root.addWidget(body, 1)

        # Status bar
        self._lbl_status = QLabel("Prêt — aucun capteur connecté")
        self._lbl_timer  = QLabel("")
        sb = self.statusBar()
        sb.addWidget(self._lbl_status, 1)
        sb.addPermanentWidget(self._lbl_timer)

    def _make_toolbar(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("toolbar")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        # Titre
        title = QLabel("🎯  Xsens DOT Manager")
        title.setStyleSheet("font-size: 12pt; font-weight: bold; color: #cba6f7;")
        layout.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #44475a;")
        layout.addWidget(sep)

        def _btn(label: str, slot, color: str = "") -> QPushButton:
            b = QPushButton(label)
            b.clicked.connect(slot)
            if color:
                b.setStyleSheet(
                    f"background-color: {color}; color: white; "
                    f"font-weight: bold; border: none; border-radius: 4px; "
                    f"padding: 5px 14px;"
                )
            return b

        self._btn_scan    = _btn("🔍 Scanner",       self._on_scan)
        self._btn_connect = _btn("🔗 Connecter",     self._on_connect)
        self._btn_sync    = _btn("⟳ Synchroniser",   self._on_sync, "#7c3aed")
        self._btn_rec     = _btn("⏺ Enregistrer",    self._on_start_rec, "#c0392b")
        self._btn_stop    = _btn("⏹ Arrêter",        self._on_stop_rec, "#7f8c8d")
        self._btn_tests   = _btn("🧪 Tests",         self._on_tests, "#6d28d9")
        self._btn_flash   = _btn("💽 Flash info",     self._on_flash_info, "#2e4057")
        self._btn_export  = _btn("💾 Exporter",       self._on_export, "#1565c0")
        self._btn_erase   = _btn("🗑 Effacer flash",  self._on_erase, "#b45309")

        for b in [self._btn_scan, self._btn_connect, self._btn_sync,
              self._btn_rec, self._btn_stop, self._btn_tests,
                  self._btn_flash, self._btn_export, self._btn_erase,
              ]:
            layout.addWidget(b)

        layout.addStretch()

        # Indicateur adaptateurs/dongles (refreshable)
        self._btn_refresh_adapters = _btn("↻ Dongles", self._on_refresh_adapters, "#334155")
        self._btn_refresh_adapters.setToolTip("Rafraîchir la disponibilité des dongles")
        layout.addWidget(self._btn_refresh_adapters)

        self._lbl_adapters = QLabel("Dongles : …")
        self._lbl_adapters.setStyleSheet("color: #89dceb; font-size: 8pt;")
        self._lbl_adapters.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._lbl_adapters)

        return frame

    def _make_table(self) -> QTableWidget:
        t = QTableWidget(0, len(_COLS))
        t.setHorizontalHeaderLabels(_COLS)
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        t.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        t.setAlternatingRowColors(True)
        t.verticalHeader().setVisible(False)
        t.setColumnWidth(0, 32)
        t.setColumnWidth(4, 80)
        t.setColumnWidth(5, 120)
        return t

    # ── Boutons → coroutines ──────────────────────────────────────────────

    def _on_scan(self)        : asyncio.ensure_future(self._scan())
    def _on_connect(self)     : asyncio.ensure_future(self._connect())
    def _on_sync(self)        : asyncio.ensure_future(self._sync())
    def _on_start_rec(self)   : asyncio.ensure_future(self._start_recording())
    def _on_stop_rec(self)    : asyncio.ensure_future(self._stop_recording())
    def _on_flash_info(self)  : asyncio.ensure_future(self._flash_info())
    def _on_tests(self)       : asyncio.ensure_future(self._open_tests_hub())
    def _on_export(self)      : asyncio.ensure_future(self._export_with_dialog())
    def _on_erase(self)       : asyncio.ensure_future(self._erase())
    def _on_analyse(self)     : self._show_jitter_dialog()
    def _on_campaign(self)    : asyncio.ensure_future(self._campaign())
    def _on_refresh_adapters(self): self._refresh_adapter_indicator(log=True)

    async def _open_tests_hub(self) -> None:
        """Ouvre le hub d'outils de test (campagne, réglages, analyse sync)."""
        dlg = TestToolsDialog(
            has_connected=(len(self._sensors) > 0 and not self._recording),
            has_export=bool(self._last_export_results),
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        action = dlg.selected_action()
        if action == "settings":
            await self._configure_rate()
        elif action == "campaign":
            await self._campaign()
        elif action == "analysis":
            self._show_jitter_dialog()

    def _refresh_adapter_indicator(self, log: bool = False) -> None:
        """Met à jour l'indicateur visuel des dongles (UP/DOWN + charge)."""
        try:
            adapters = list_adapters(include_down=True)
        except Exception as exc:
            self._lbl_adapters.setText("Dongles : indisponibles")
            self._lbl_adapters.setToolTip(str(exc))
            if log:
                self._log(f"<span style='color:#f38ba8'>[ERREUR dongles] {exc}</span>")
            return

        self._known_adapters = adapters
        up_count = sum(1 for a in adapters if a.is_up)
        total = len(adapters)

        scanned_by_adapter: dict[str, int] = {}
        for d in self._devices:
            name = d.adapter.bleak_id if getattr(d, "adapter", None) else "?"
            scanned_by_adapter[name] = scanned_by_adapter.get(name, 0) + 1

        connected_by_adapter: dict[str, int] = {}
        for s in self._sensors:
            name = s.adapter.name if s.adapter else "?"
            connected_by_adapter[name] = connected_by_adapter.get(name, 0) + 1

        parts: list[str] = []
        tips: list[str] = []
        for a in adapters:
            icon = "🟢" if a.is_up else "🔴"
            sc = scanned_by_adapter.get(a.name, 0)
            cc = connected_by_adapter.get(a.name, 0)
            parts.append(f"{icon}<b>{a.name}</b> {cc}/{sc}")
            tips.append(
                f"{a.name} [{a.address}] — {'UP' if a.is_up else 'DOWN'} — "
                f"connectés={cc}, scannés={sc}"
            )

        detail = "  ".join(parts) if parts else "—"
        self._lbl_adapters.setText(f"Dongles : <b>{up_count}/{total}</b> UP  |  {detail}")
        self._lbl_adapters.setToolTip("\n".join(tips) if tips else "Aucun dongle détecté")
        if log:
            self._log(f"Dongles : {up_count}/{total} UP")

    # ── Logique async ─────────────────────────────────────────────────────

    async def _scan(self) -> None:
        self._log("Scan BLE en cours (8 s)...")
        self._set_status("Scan en cours...")
        self._set_busy(True)
        try:
            adapters = list_adapters()
            self._devices = await scan_for_dots(timeout=8.0, adapters=adapters)
            self._table.setRowCount(0)
            for i, d in enumerate(self._devices):
                self._table.insertRow(i)
                self._table.setItem(i, 0, _cell(str(i + 1), align=Qt.AlignmentFlag.AlignCenter))
                self._table.setItem(i, 1, _cell(d.address))
                self._table.setItem(i, 2, _cell(d.name or ""))
                self._table.setItem(i, 3, _cell(d.adapter.bleak_id))
                self._table.setItem(i, 4, _cell(f"{d.rssi} dBm", align=Qt.AlignmentFlag.AlignCenter))
                self._table.setItem(i, 5, _state_cell("—"))
            n = len(self._devices)
            adap_dist: dict[str, int] = {}
            for d in self._devices:
                adap_dist[d.adapter.bleak_id] = adap_dist.get(d.adapter.bleak_id, 0) + 1
            dist_str = "  ".join(f"{hci}: {cnt}" for hci, cnt in sorted(adap_dist.items()))
            self._log(
                f"Scan terminé — <b>{n} capteur(s)</b> détecté(s)."
                + (f"  [{dist_str}]" if dist_str else "")
            )
            self._set_status(f"{n} capteur(s) détecté(s)")
        except Exception as exc:
            self._log(f"<span style='color:#f38ba8'>[ERREUR scan] {exc}</span>")
        finally:
            self._refresh_adapter_indicator()
            self._set_busy(False)
            self._refresh_buttons()

    async def _connect(self) -> None:
        if not self._devices:
            self._log("[AVERTISSEMENT] Lancer d'abord un scan.")
            return
        self._log(f"Connexion de {len(self._devices)} capteur(s)...")
        self._set_status("Connexion en cours...")
        self._set_busy(True)

        # Déconnecter les précédents
        if self._sensors:
            await asyncio.gather(*[s.disconnect() for s in self._sensors], return_exceptions=True)
            self._sensors = []

        # Marquer tous les capteurs "Connecting" immédiatement
        for d in self._devices:
            self._set_row_state(d.address, "Connecting")

        async def _c(d):
            s = DotSensor(d.address, adapter=d.adapter, name=d.address[-5:])
            try:
                await s.connect()
                try:
                    st = await s.cmd_get_state()
                    self._set_row_state(s.address, STATE_NAMES.get(st, f"0x{st:02x}"))
                except Exception:
                    self._set_row_state(s.address, "Idle")
                return s
            except DotConnectError as exc:
                self._log(f"  <span style='color:#f38ba8'>{d.address} : {exc}</span>")
                self._set_row_state(d.address, "ERREUR")
                return None

        results = await asyncio.gather(*[_c(d) for d in self._devices])
        self._sensors = [s for s in results if s is not None]

        n = len(self._sensors)
        adap_dist: dict[str, int] = {}
        for s in self._sensors:
            nm = s.adapter.name if s.adapter else "?"
            adap_dist[nm] = adap_dist.get(nm, 0) + 1
        dist_str = "  ".join(f"{h}: {c}" for h, c in sorted(adap_dist.items()))
        self._log(
            f"<b>{n}/{len(self._devices)}</b> capteur(s) connecté(s)."
            + (f"  [{dist_str}]" if dist_str else "")
        )
        self._set_status(f"{n} connecté(s)")
        self._refresh_adapter_indicator()
        self._set_busy(False)
        self._refresh_buttons()

    async def _sync(self) -> None:
        if not self._sensors:
            return
        self._purge_disconnected()
        if not self._sensors:
            self._log("[AVERTISSEMENT] Aucun capteur connecté après purge.")
            return
        await self._ensure_output_rate(120)
        n = len(self._sensors)
        self._sync_total = n
        self._sync_idle_count = 0
        self._sync_synced_count = 0

        self._log(f"Synchronisation de {n} capteur(s)…")
        self._set_status(f"Sync — 0/{n} envoyés")
        self._set_busy(True)

        def _on_progress(address: str, status: str) -> None:
            if status == "syncing":
                self._set_row_state(address, "Syncing")
            elif status == "synced":
                self._sync_synced_count += 1
                self._set_row_state(address, "Synced")
                self._set_status(
                    f"Sync — {self._sync_synced_count}/{self._sync_total} envoyés"
                )
            elif status == "idle":
                self._sync_idle_count += 1
                self._set_row_state(address, "Synced")
                self._set_status(
                    f"Sync — {self._sync_idle_count}/{self._sync_total} prêts"
                )
            elif status == "error":
                self._set_row_state(address, "ERREUR")

        try:
            from .sync import synchronize_sensors
            result = await synchronize_sensors(
                self._sensors,
                settle_time=2.0,
                verify_state=False,
                progress_callback=_on_progress,
                await_sync_ack=False,
            )
            for s in self._sensors:
                if result.per_sensor.get(s.address, False):
                    self._set_row_state(s.address, "Synced")
            ok = sum(result.per_sensor.values())
            color = "#a6e3a1" if result.success else "#f1c40f"
            self._log(
                f"<span style='color:{color}'>Sync {ok}/{n} OK</span> "
                f"— durée {result.duration_ms:.0f} ms"
            )
            if result.failed_sensors:
                self._log(
                    f"<span style='color:#f38ba8'>  Capteurs en échec : "
                    f"{', '.join(result.failed_sensors)}</span>"
                )
            self._set_status(f"Sync {ok}/{n} OK")
        except Exception as exc:
            for s in self._sensors:
                self._set_row_state(s.address, "Idle")
            self._log(f"<span style='color:#f38ba8'>[ERREUR sync] {exc}</span>")
        finally:
            # Retirer les capteurs qui se sont déconnectés pendant la sync
            self._purge_disconnected()
            self._set_busy(False)
            self._refresh_buttons()

    async def _start_recording(self) -> None:
        if not self._sensors:
            return
        self._purge_disconnected()
        if not self._sensors:
            self._log("[AVERTISSEMENT] Aucun capteur connecté après purge.")
            return
        await self._ensure_output_rate(120)
        self._log("Démarrage de l'enregistrement...")
        self._set_busy(True)
        try:
            from .recording import start_all
            result = await start_all(self._sensors)
            self._recording = True
            self._recording_start = datetime.now()
            self._timer.start(1000)
            for s in self._sensors:
                self._set_row_state(s.address, "Recording")
            jitter_info = ""
            if result.jitter_ms is not None:
                jitter_info = f" — jitter ACK : <b>{result.jitter_ms:.1f} ms</b>"
            self._log(
                f"<span style='color:#f38ba8'>⏺ Enregistrement démarré</span>"
                f" — {len(self._sensors)} capteur(s){jitter_info}"
            )
            self._set_status(f"Enregistrement — {len(self._sensors)} capteur(s)")
        except Exception as exc:
            self._log(f"<span style='color:#f38ba8'>[ERREUR recording] {exc}</span>")
        finally:
            self._set_busy(False)
            self._refresh_buttons()

    async def _stop_recording(self) -> None:
        if not self._sensors:
            return
        if not self._recording:
            return  # guard : ignorer un double-clic
        # Marquer immédiatement pour bloquer un 2e appel concurrent
        self._recording = False
        self._log("Arrêt de l'enregistrement...")
        self._set_busy(True)
        try:
            from .recording import stop_all
            result = await stop_all(self._sensors)
            self._recording = False
            self._timer.stop()
            self._lbl_timer.setText("")
            for s in self._sensors:
                self._set_row_state(s.address, "Idle")
            self._log(f"<span style='color:#a6e3a1'>⏹ Enregistrement arrêté</span> — {result}")
            self._set_status("Arrêté")
        except Exception as exc:
            self._log(f"<span style='color:#f38ba8'>[ERREUR stop] {exc}</span>")
        finally:
            # Purger les capteurs qui se sont déconnectés pendant l'enregistrement
            self._purge_disconnected()
            self._set_busy(False)
            self._refresh_buttons()

    async def _configure_rate(self) -> None:
        """
        Lit le taux courant du premier capteur, ouvre RecordingSettingsDialog,
        puis applique le taux choisi à tous les capteurs connectés.
        """
        if not self._sensors:
            self._log("[AVERTISSEMENT] Aucun capteur connecté.")
            return

        from .protocol.gatt import SUPPORTED_OUTPUT_RATES, DEFAULT_OUTPUT_RATE

        # Lire le taux courant du premier capteur
        current_rate = DEFAULT_OUTPUT_RATE
        try:
            current_rate = await self._sensors[0].cmd_get_output_rate()
        except Exception:
            pass

        dlg = RecordingSettingsDialog(current_rate, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        rate = dlg.selected_rate()
        self._log(f"Configuration du taux d'acquisition → <b>{rate} Hz</b> sur {len(self._sensors)} capteur(s)...")
        self._set_busy(True)
        ok_count = 0
        errors = []
        for s in self._sensors:
            try:
                await s.cmd_set_output_rate(rate)
                ok_count += 1
            except Exception as exc:
                errors.append(f"{s.address}: {exc}")

        if ok_count:
            self._log(
                f"<span style='color:#a6e3a1'>Taux {rate} Hz appliqué à {ok_count}/{len(self._sensors)} capteur(s).</span>"
            )
        for err in errors:
            self._log(f"<span style='color:#f38ba8'>  {err}</span>")
        self._set_status(f"Taux d'acquisition : {rate} Hz")
        self._set_busy(False)
        self._refresh_buttons()

    async def _ensure_output_rate(self, target_rate: int = 120) -> bool:
        """Force le taux d'acquisition cible si nécessaire.

        Retourne True si tout a été appliqué correctement (ou déjà OK).
        """
        if not self._sensors:
            return False

        current_rate = None
        try:
            current_rate = int(await self._sensors[0].cmd_get_output_rate())
        except Exception:
            pass

        if current_rate == target_rate:
            return True

        self._log(
            f"Forçage du taux d'acquisition à <b>{target_rate} Hz</b> "
            f"(taux courant: {current_rate if current_rate is not None else 'inconnu'})..."
        )
        ok_count = 0
        errors = []
        for s in self._sensors:
            try:
                await s.cmd_set_output_rate(target_rate)
                ok_count += 1
            except Exception as exc:
                errors.append(f"{s.address}: {exc}")

        if ok_count:
            self._log(
                f"<span style='color:#a6e3a1'>Taux {target_rate} Hz appliqué à {ok_count}/{len(self._sensors)} capteur(s).</span>"
            )
        for err in errors:
            self._log(f"<span style='color:#f38ba8'>  {err}</span>")
        return ok_count == len(self._sensors)

    async def _flash_info(self) -> None:
        """Interroge la mémoire flash de chaque capteur connecté et ouvre le dialogue."""
        if not self._sensors:
            self._log("[AVERTISSEMENT] Aucun capteur connecté.")
            return
        self._purge_disconnected()
        if not self._sensors:
            return
        self._log(f"Lecture des métadonnées flash ({len(self._sensors)} capteur(s))...")
        self._set_status("Flash info en cours...")
        self._set_busy(True)
        for s in self._sensors:
            self._set_row_state(s.address, "Exporting")
        try:
            from .export import get_flash_metadata
            tasks = {s.address: get_flash_metadata(s) for s in self._sensors}
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            new_meta: dict[str, list] = {}
            for addr, res in zip(tasks.keys(), results):
                if isinstance(res, Exception):
                    self._log(f"  <span style='color:#f38ba8'>{addr} — erreur : {res}</span>")
                    new_meta[addr] = []
                else:
                    new_meta[addr] = res
                    self._log(f"  {addr} — {len(res)} fichier(s) trouvé(s)")
                self._set_row_state(addr, "Idle")

            # Lire le taux d'acquisition courant par capteur.
            # Si indisponible, fallback à 120 Hz.
            sample_rates: dict[str, int] = {}
            for s in self._sensors:
                try:
                    rate = int(await s.cmd_get_output_rate())
                except Exception:
                    rate = 120
                sample_rates[s.address] = rate

            self._flash_metadata = new_meta
            self._flash_sample_rates = sample_rates
            total_files = sum(len(v) for v in new_meta.values())
            self._set_status(f"Flash info — {total_files} fichier(s) au total")

            dlg = FlashInfoDialog(self._sensors, new_meta, sample_rates=sample_rates, parent=self)
            dlg.exec()

        except Exception as exc:
            self._log(f"<span style='color:#f38ba8'>[ERREUR flash info] {exc}</span>")
        finally:
            for s in self._sensors:
                self._set_row_state(s.address, "Idle")
            self._set_busy(False)
            self._refresh_buttons()

    async def _export_with_dialog(self) -> None:
        """Ouvre le dialogue d'export (payload + sélection de fichiers) puis lance l'export."""
        if not self._sensors:
            return
        self._purge_disconnected()
        if not self._sensors:
            return
        dlg = ExportDialog(
            self._sensors,
            self._flash_metadata,
            sample_rates=self._flash_sample_rates,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        data_types = dlg.selected_data_types()
        file_indices_map = dlg.selected_file_indices()
        await self._export(data_types=data_types, file_indices_map=file_indices_map)

    async def _export(
        self,
        data_types: Optional[list] = None,
        file_indices_map: Optional[dict] = None,
    ) -> None:
        if not self._sensors:
            return
        if data_types is None:
            from .export import PRESET_EULER
            data_types = PRESET_EULER
        payload_label = "/".join(data_types[:2]) + ("..." if len(data_types) > 2 else "")
        self._log(f"Export flash [{payload_label}] → {self._output_dir}/")
        self._set_status("Export en cours...")
        self._set_busy(True)
        try:
            from .export import export_all_sensors
            from .analysis import analyze_sync_jitter
            self._output_dir.mkdir(parents=True, exist_ok=True)
            for s in self._sensors:
                self._set_row_state(s.address, "Exporting")
            results = await export_all_sensors(
                self._sensors,
                self._output_dir,
                data_types=data_types,
                file_indices_map=file_indices_map or {},
            )
            self._last_export_results = results
            self._last_export_dir = self._output_dir

            total = sum(r.total_samples for r in results)
            ok_count = sum(1 for r in results if r.success)
            self._log(
                f"<span style='color:#a6e3a1'>Export {ok_count}/{len(results)} OK "
                f"— {total} échantillons</span>"
            )
            for r in results:
                status_html = (
                    f"<span style='color:#a6e3a1'>OK</span>"
                    if r.success
                    else f"<span style='color:#f38ba8'>ERREUR: {r.error}</span>"
                )
                self._log(
                    f"  {r.address} — {r.total_samples} éch. "
                    f"{r.duration_s:.1f}s — {status_html}"
                )
                self._set_row_state(r.address, "Idle")

            # ── Analyse jitter automatique ──────────────────────────────────────
            addrs = [s.address for s in self._sensors]
            jitter = analyze_sync_jitter(self._output_dir, addrs)
            self._log_jitter(jitter)
            self._set_status(
                f"Export OK — {total} éch. — Jitter {jitter.jitter_max_ms:.1f} ms"
            )
        except Exception as exc:
            self._log(f"<span style='color:#f38ba8'>[ERREUR export] {exc}</span>")
            self._set_status("Export terminé avec erreurs")
        finally:
            # Toujours remettre les capteurs encore en "Exporting" à "Idle"
            for s in self._sensors:
                self._set_row_state(s.address, "Idle")
            self._set_busy(False)
            self._refresh_buttons()

    async def _erase(self) -> None:
        if not self._sensors:
            return
        reply = QMessageBox.question(
            self, "Confirmer l'effacement",
            f"Effacer la flash de {len(self._sensors)} capteur(s) ?\n"
            "Toutes les données enregistrées seront perdues.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._log("Effacement flash (peut prendre 1–5 min)...")
        self._set_status("Effacement en cours...")
        self._set_busy(True)
        for s in self._sensors:
            self._set_row_state(s.address, "Erasing")
        try:
            tasks = [s.cmd_erase_flash(timeout=300.0) for s in self._sensors]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            ok = sum(1 for r in results if not isinstance(r, Exception))
            self._log(f"<span style='color:#a6e3a1'>Effacement : {ok}/{len(self._sensors)} OK</span>")
            for s, r in zip(self._sensors, results):
                st = "Idle" if not isinstance(r, Exception) else "ERREUR"
                self._set_row_state(s.address, st)
            self._set_status(f"Flash effacé ({ok}/{len(self._sensors)})")
        except Exception as exc:
            self._log(f"<span style='color:#f38ba8'>[ERREUR erase] {exc}</span>")
            for s in self._sensors:
                self._set_row_state(s.address, "Idle")
        finally:
            self._set_busy(False)
            self._refresh_buttons()

    async def _campaign(self) -> None:
        """Lance une campagne de reproductibilité depuis la GUI."""
        dlg = CampaignSettingsDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        cfg = dlg.values()
        self._log(
            "Lancement campagne fiabilité — "
            f"runs={cfg['runs']}, duration={cfg['duration']:.1f}s, "
            f"scan_timeout={cfg['scan_timeout']:.1f}s, "
            f"cooldown={cfg['cooldown']:.1f}s"
            + (
                f", expected_count={cfg['expected_count']}"
                if cfg['expected_count'] is not None else ""
            )
        )
        self._set_status("Campagne fiabilité en cours...")
        self._set_busy(True)

        try:
            from .campaign import run_reliability_campaign, format_campaign_summary

            def _cb(msg: str) -> None:
                self._log(msg)

            summary = await run_reliability_campaign(
                runs=cfg["runs"],
                duration=cfg["duration"],
                scan_timeout=cfg["scan_timeout"],
                max_per_adapter=cfg["max_per_adapter"],
                expected_count=cfg["expected_count"],
                cooldown=cfg["cooldown"],
                force_output_rate=120,
                event_callback=_cb,
            )

            lines = format_campaign_summary(summary)
            color = "#a6e3a1" if summary.success_pct >= 99.9 else "#f1c40f"
            self._log(f"<span style='color:{color}'><b>{lines[3]}</b></span>")
            for line in lines[4:]:
                self._log(line)

            jitter_vals = [
                r.jitter_ms for r in summary.run_results if r.jitter_ms is not None
            ]
            jitter_avg = mean(jitter_vals) if jitter_vals else 0.0
            self._set_status(
                f"Campagne terminée — success {summary.success_pct:.1f}%"
                + (f" — jitter moyen {jitter_avg:.1f} ms" if jitter_vals else "")
            )
        except Exception as exc:
            self._log(f"<span style='color:#f38ba8'>[ERREUR campagne] {exc}</span>")
            self._set_status("Campagne terminée avec erreurs")
        finally:
            self._set_busy(False)
            self._refresh_adapter_indicator()
            self._refresh_buttons()

    # ── Helpers UI ────────────────────────────────────────────────────────

    def _tick_timer(self) -> None:
        if self._recording_start:
            elapsed = int((datetime.now() - self._recording_start).total_seconds())
            h, r = divmod(elapsed, 3600)
            m, s = divmod(r, 60)
            self._lbl_timer.setText(f"⏺ {h:02d}:{m:02d}:{s:02d}")

    def _set_row_state(self, address: str, state: str) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 1)
            if item and item.text() == address:
                self._table.setItem(row, 5, _state_cell(state))
                break

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_widget.append(
            f"<span style='color:#6c7086'>[{ts}]</span> {msg}"
        )

    def _set_status(self, msg: str) -> None:
        n = len(self._sensors)
        self._lbl_status.setText(f"{n} capteur(s) connecté(s)  —  {msg}")

    def _set_busy(self, busy: bool) -> None:
        """Désactive les boutons pendant une opération.
        Le bouton Arrêter reste actif si l'enregistrement est en cours.
        """
        for b in [self._btn_scan, self._btn_connect, self._btn_sync,
                                    self._btn_rec, self._btn_tests, self._btn_flash,
                                    self._btn_export, self._btn_erase, self._btn_refresh_adapters]:
            b.setEnabled(not busy)
        # btn_stop : actif si enregistrement en cours, même pendant busy
        self._btn_stop.setEnabled(self._recording)

    def _purge_disconnected(self) -> int:
        """Retire de self._sensors les capteurs dont la connexion BLE est tombée.
        Retourne le nombre de capteurs retirés.
        Un seul log est émis pour tous les capteurs retirés.
        """
        lost = [s for s in self._sensors if not s.is_connected]
        if lost:
            addrs = ", ".join(s.address for s in lost)
            self._log(
                f"<span style='color:#f38ba8'>⚠ {len(lost)} capteur(s) déconnecté(s) "
                f"et retirés : {addrs}</span>"
            )
            for s in lost:
                self._set_row_state(s.address, "ERREUR")
            self._sensors = [s for s in self._sensors if s.is_connected]
            self._refresh_adapter_indicator()
            self._refresh_buttons()
        return len(lost)

    def _refresh_buttons(self) -> None:
        connected = len(self._sensors) > 0
        has_devices = len(self._devices) > 0
        has_export = bool(self._last_export_results)
        self._btn_scan.setEnabled(True)
        self._btn_connect.setEnabled(has_devices)
        self._btn_sync.setEnabled(connected and not self._recording)
        self._btn_rec.setEnabled(connected and not self._recording)
        self._btn_stop.setEnabled(connected and self._recording)
        self._btn_tests.setEnabled(not self._recording)
        self._btn_flash.setEnabled(connected and not self._recording)
        self._btn_export.setEnabled(connected and not self._recording)
        self._btn_erase.setEnabled(connected and not self._recording)
        self._btn_refresh_adapters.setEnabled(True)

    # ── Analyse jitter ────────────────────────────────────────────────────

    def _log_jitter(self, jitter) -> None:
        """Affiche le résultat d'analyse de synchronisation dans le journal."""
        from .analysis import JITTER_THRESHOLD_MS
        self._log("─── Analyse synchronisation ─────────────────────────")
        color_j = "#a6e3a1" if jitter.jitter_max_ms <= JITTER_THRESHOLD_MS else "#f38ba8"
        state_str = "✓ OK" if jitter.success else "⚠ DÉGRADÉ"
        self._log(
            f"  Jitter max : <span style='color:{color_j}'>"
            f"<b>{jitter.jitter_max_ms:.1f} ms</b></span> "
            f"(seuil {JITTER_THRESHOLD_MS:.0f} ms) — <b>{state_str}</b>"
        )
        self._log(f"  Capteurs exploitables : {jitter.n_ok}/{jitter.n_sensors}")
        offsets = jitter.offsets_ms
        if offsets:
            self._log("  Offsets par capteur (réf = plus tôt) :")
            for addr, off in sorted(offsets.items(), key=lambda x: x[1]):
                bar_len = min(int(abs(off) * 2), 40)
                bar = "█" * max(bar_len, 1) if off != 0 else "│"
                sign = "+" if off >= 0 else ""
                col = "#a6e3a1" if abs(off) <= JITTER_THRESHOLD_MS else "#f38ba8"
                self._log(
                    f"    <span style='font-family:monospace'>{addr[-11:]}</span>  "
                    f"<span style='color:{col}'>{sign}{off:.1f} ms  {bar}</span>"
                )
        if getattr(jitter, "diagnostics", None):
            self._log("  Diagnostic :")
            for msg in jitter.diagnostics:
                self._log(f"    {msg}")
        if jitter.errors:
            for addr, err in jitter.errors.items():
                self._log(f"  <span style='color:#f38ba8'>  {addr}: {err}</span>")

    def _show_jitter_dialog(self) -> None:
        """Ouvre la boîte de dialogue graphique de dispersion."""
        if not self._last_export_results or not self._last_export_dir:
            QMessageBox.information(
                self, "Analyse sync",
                "Lancez d'abord un export pour obtenir des données à analyser."
            )
            return
        from .analysis import analyze_sync_jitter
        addrs = [s.address for s in self._sensors] if self._sensors else \
                [r.address for r in self._last_export_results]
        jitter = analyze_sync_jitter(self._last_export_dir, addrs)
        dlg = JitterDialog(jitter, parent=self)
        dlg.exec()

    # ── Fermeture propre ──────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        """Intercepte la fermeture de fenêtre pour déconnecter proprement."""
        if not self._sensors:
            # Aucun capteur connecté : fermeture immédiate
            _busctl_disconnect_all_dots()
            event.accept()
            QApplication.quit()
            return
        # Capteurs connectés : on déconnecte en async avant de quitter
        event.ignore()
        asyncio.ensure_future(self._async_close())

    async def _async_close(self) -> None:
        """Déconnecte tous les capteurs puis quitte l'application."""
        if self._sensors:
            self._log("Fermeture — déconnexion des capteurs en cours...")
            self._set_busy(True)
            await asyncio.gather(
                *[s.disconnect() for s in self._sensors],
                return_exceptions=True,
            )
            self._sensors = []
        # Fallback : forcer via D-Bus les connexions BlueZ encore ouvertes.
        # Appelé directement (synchrone) pour éviter que run_in_executor crée
        # un Future en attente quand la boucle asyncio est stoppée par quit().
        _busctl_disconnect_all_dots()
        QApplication.quit()


def _busctl_disconnect_all_dots() -> None:
    """Déconnecte via busctl tous les devices D4:22:CD encore enregistrés dans BlueZ."""
    try:
        tree = subprocess.run(
            ["busctl", "tree", "org.bluez"],
            capture_output=True, text=True, timeout=5,
        )
        paths = re.findall(r'/org/bluez/hci\d+/dev_D4_22_CD_[\w]+', tree.stdout)
        paths = list(dict.fromkeys(paths))  # dédoublonner
        for path in paths:
            subprocess.run(
                ["busctl", "call", "org.bluez", path, "org.bluez.Device1", "Disconnect"],
                capture_output=True, timeout=3,
            )
    except Exception:
        pass


# ── Cellules tableau ──────────────────────────────────────────────────────────

def _cell(text: str,
          align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignVCenter) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment(int(align | Qt.AlignmentFlag.AlignLeft))
    return item


def _state_cell(state: str) -> QTableWidgetItem:
    item = QTableWidgetItem(state)
    item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
    color = _STATE_COLORS.get(state, "#cdd6f4")
    item.setForeground(QColor(color))
    if state not in ("—", "ERREUR"):
        f = QFont()
        f.setBold(True)
        item.setFont(f)
    return item


# ── Dialogue infos flash ──────────────────────────────────────────────────────

class FlashInfoDialog(QDialog):
    """
    Affiche les métadonnées flash (fichiers, nb samples, durées) de tous les
    capteurs connectés dans un tableau.
    """

    def __init__(self, sensors, flash_meta: dict, sample_rates: Optional[dict[str, int]] = None, parent=None) -> None:
        super().__init__(parent)
        self._sample_rates = sample_rates or {}
        self.setWindowTitle("Mémoire flash — informations par capteur")
        self.setMinimumSize(820, 400)
        self.setStyleSheet(
            "QDialog { background: #1e1e2e; color: #cdd6f4; }"
            "QTableWidget { background: #181825; color: #cdd6f4; gridline-color: #313244; }"
            "QHeaderView::section { background: #313244; color: #cdd6f4; padding: 4px; border: none; }"
            "QPushButton { background: #313244; color: #cdd6f4; border: 1px solid #45475a; "
            "border-radius: 4px; padding: 4px 12px; }"
            "QPushButton:hover { background: #45475a; }"
            "QLabel { color: #cdd6f4; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Résumé
        total_files = sum(len(v) for v in flash_meta.values())
        total_samples = sum(f.sample_count for v in flash_meta.values() for f in v)
        summary = QLabel(
            f"<b>{len(sensors)} capteur(s)</b> — "
            f"<b>{total_files}</b> fichier(s) au total — "
            f"<b>{total_samples}</b> échantillons"
        )
        summary.setStyleSheet("font-size: 10pt; color: #89dceb;")
        summary.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(summary)

        # Tableau
        cols = ["Adresse", "Fichier", "Nb samples", "Durée estimée", "Début (UTC)"]
        table = QTableWidget(0, len(cols))
        table.setHorizontalHeaderLabels(cols)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)

        for sensor in sensors:
            meta_list = flash_meta.get(sensor.address, [])
            if not meta_list:
                row = table.rowCount()
                table.insertRow(row)
                table.setItem(row, 0, QTableWidgetItem(sensor.address))
                empty = QTableWidgetItem("— (flash vide)")
                empty.setForeground(QColor("#6c7086"))
                table.setItem(row, 1, empty)
                for c in range(2, len(cols)):
                    table.setItem(row, c, QTableWidgetItem("—"))
            else:
                for meta in meta_list:
                    row = table.rowCount()
                    table.insertRow(row)
                    table.setItem(row, 0, QTableWidgetItem(sensor.address))
                    table.setItem(row, 1, QTableWidgetItem(f"Fichier {meta.file_index}"))
                    rate = int(self._sample_rates.get(sensor.address, 120))
                    item_samp = QTableWidgetItem(f"{meta.sample_count:,}")
                    item_samp.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    table.setItem(row, 2, item_samp)
                    dur = QTableWidgetItem(f"{meta.duration_str(rate)} ({rate} Hz)")
                    dur.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    table.setItem(row, 3, dur)
                    table.setItem(row, 4, QTableWidgetItem(meta.start_datetime()))

        layout.addWidget(table, 1)

        # Bouton fermer
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.setStyleSheet(
            "QPushButton { background: #313244; color: #cdd6f4; border: 1px solid #45475a; "
            "border-radius: 4px; padding: 4px 14px; } "
            "QPushButton:hover { background: #45475a; }"
        )
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)


# ── Dialogue réglages enregistrement ─────────────────────────────────────────

class RecordingSettingsDialog(QDialog):
    """
    Dialogue de configuration du taux d'acquisition avant synchronisation :
    - ComboBox avec les taux supportés (1, 4, 10, 12, 15, 20, 30, 60, 120 Hz)
    - Taux actuel mis en évidence
    """

    def __init__(self, current_rate: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙ Réglages d'acquisition")
        self.setMinimumWidth(340)
        self._rate: int = current_rate

        from .protocol.gatt import SUPPORTED_OUTPUT_RATES

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Titre
        title = QLabel("<b>Taux d'acquisition</b>")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        info = QLabel(
            "Sélectionnez la fréquence d'enregistrement.\n"
            "Ce réglage doit être appliqué <b>avant la synchronisation</b>."
        )
        info.setWordWrap(True)
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info)

        # ComboBox
        row = QHBoxLayout()
        row.addWidget(QLabel("Fréquence :"))
        self._combo = QComboBox()
        for r in sorted(SUPPORTED_OUTPUT_RATES):
            self._combo.addItem(f"{r} Hz", r)
        # Sélectionner le taux courant
        idx = self._combo.findData(current_rate)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)
        row.addWidget(self._combo)
        layout.addLayout(row)

        # Boutons OK / Annuler
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _on_accept(self):
        self._rate = self._combo.currentData()
        self.accept()

    def selected_rate(self) -> int:
        return self._rate


class CampaignSettingsDialog(QDialog):
    """Dialogue de configuration de la campagne fiabilité."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("🧪 Campagne fiabilité")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        info = QLabel(
            "Exécute plusieurs runs : scan → connexion → sync → start → stop\n"
            "et calcule le taux de succès + timings + jitter."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        def _row(label: str, widget: QWidget) -> None:
            r = QHBoxLayout()
            r.addWidget(QLabel(label))
            r.addWidget(widget)
            layout.addLayout(r)

        self._runs = QSpinBox()
        self._runs.setRange(1, 100)
        self._runs.setValue(5)
        _row("Runs :", self._runs)

        self._duration = QDoubleSpinBox()
        self._duration.setRange(1.0, 3600.0)
        self._duration.setDecimals(1)
        self._duration.setSingleStep(1.0)
        self._duration.setValue(10.0)
        self._duration.setSuffix(" s")
        _row("Durée run :", self._duration)

        self._scan_timeout = QDoubleSpinBox()
        self._scan_timeout.setRange(1.0, 60.0)
        self._scan_timeout.setDecimals(1)
        self._scan_timeout.setSingleStep(1.0)
        self._scan_timeout.setValue(8.0)
        self._scan_timeout.setSuffix(" s")
        _row("Scan timeout :", self._scan_timeout)

        self._cooldown = QDoubleSpinBox()
        self._cooldown.setRange(0.0, 60.0)
        self._cooldown.setDecimals(1)
        self._cooldown.setSingleStep(0.5)
        self._cooldown.setValue(2.0)
        self._cooldown.setSuffix(" s")
        _row("Cooldown :", self._cooldown)

        self._expected = QSpinBox()
        self._expected.setRange(0, 128)
        self._expected.setValue(12)
        self._expected.setToolTip("0 = ne pas valider le nombre de capteurs attendu")
        _row("Expected count :", self._expected)

        self._max_per_adapter = QSpinBox()
        self._max_per_adapter.setRange(1, 32)
        self._max_per_adapter.setValue(8)
        _row("Max/adaptateur :", self._max_per_adapter)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def values(self) -> dict:
        expected = self._expected.value()
        return {
            "runs": int(self._runs.value()),
            "duration": float(self._duration.value()),
            "scan_timeout": float(self._scan_timeout.value()),
            "cooldown": float(self._cooldown.value()),
            "expected_count": int(expected) if expected > 0 else None,
            "max_per_adapter": int(self._max_per_adapter.value()),
        }


class TestToolsDialog(QDialog):
    """Hub GUI des outils de test."""

    def __init__(self, has_connected: bool, has_export: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("🧪 Outils de test")
        self.setMinimumWidth(360)
        self._action: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        info = QLabel(
            "Choisir une action de test :\n"
            "- Campagne fiabilité\n"
            "- Réglages (fréquence)\n"
            "- Analyse synchronisation"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        btn_campaign = QPushButton("🧪 Campagne fiabilité")
        btn_campaign.setToolTip("Runs répétés scan→sync→record→stop avec résumé")
        btn_campaign.clicked.connect(lambda: self._choose("campaign"))
        layout.addWidget(btn_campaign)

        btn_settings = QPushButton("⚙ Réglages acquisition")
        btn_settings.setToolTip("Configurer le taux d'acquisition (ex: 120 Hz)")
        btn_settings.setEnabled(has_connected)
        if not has_connected:
            btn_settings.setToolTip("Nécessite au moins un capteur connecté")
        btn_settings.clicked.connect(lambda: self._choose("settings"))
        layout.addWidget(btn_settings)

        btn_analysis = QPushButton("📊 Analyse sync")
        btn_analysis.setToolTip("Analyse le jitter sur le dernier export")
        btn_analysis.setEnabled(has_export)
        if not has_export:
            btn_analysis.setToolTip("Nécessite un export déjà réalisé")
        btn_analysis.clicked.connect(lambda: self._choose("analysis"))
        layout.addWidget(btn_analysis)

        close_btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_btns.rejected.connect(self.reject)
        layout.addWidget(close_btns)

    def _choose(self, action: str) -> None:
        self._action = action
        self.accept()

    def selected_action(self) -> Optional[str]:
        return self._action


# ── Dialogue d'export ─────────────────────────────────────────────────────────

class ExportDialog(QDialog):
    """
    Dialogue de configuration avant export :
    - Choix du payload (type de données)
    - Sélection des fichiers par capteur (si métadonnées flash disponibles)
    """

    _PAYLOAD_LABELS = [
        ("Euler (roulis/tangage/lacet)", "euler"),
        ("Quaternion (orientation complète)", "quaternion"),
        ("IMU (acc + gyro + mag)", "imu"),
        ("Complet (tous les types)", "full"),
    ]

    def __init__(
        self,
        sensors,
        flash_meta: dict,
        sample_rates: Optional[dict[str, int]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._sensors = sensors
        self._flash_meta = flash_meta
        self._sample_rates = sample_rates or {}
        # Checkboxes par adresse → liste de (file_index, QCheckBox)
        self._file_checks: dict[str, list[tuple[int, QCheckBox]]] = {}

        self.setWindowTitle("Configuration de l'export")
        self.setMinimumWidth(580)
        self.setStyleSheet(
            "QDialog { background: #1e1e2e; color: #cdd6f4; }"
            "QGroupBox { font-weight: bold; border: 1px solid #44475a; border-radius: 4px; "
            "margin-top: 6px; color: #cdd6f4; padding: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
            "QComboBox { background: #313244; color: #cdd6f4; border: 1px solid #45475a; "
            "border-radius: 4px; padding: 4px; }"
            "QComboBox QAbstractItemView { background: #313244; color: #cdd6f4; "
            "selection-background-color: #45475a; }"
            "QCheckBox { color: #cdd6f4; spacing: 6px; }"
            "QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #45475a; "
            "border-radius: 2px; background: #181825; }"
            "QCheckBox::indicator:checked { background: #89b4fa; }"
            "QPushButton { background: #313244; color: #cdd6f4; border: 1px solid #45475a; "
            "border-radius: 4px; padding: 5px 14px; }"
            "QPushButton:hover { background: #45475a; }"
            "QLabel { color: #cdd6f4; }"
            "QScrollArea { border: 1px solid #44475a; background: #181825; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # ── Section payload ───────────────────────────────────────────────
        grp_payload = QGroupBox("Type de données à exporter")
        grp_payload_layout = QVBoxLayout(grp_payload)
        lbl_payload = QLabel("Payload :")
        self._combo_payload = QComboBox()
        for label, _ in self._PAYLOAD_LABELS:
            self._combo_payload.addItem(label)
        grp_payload_layout.addWidget(lbl_payload)
        grp_payload_layout.addWidget(self._combo_payload)
        layout.addWidget(grp_payload)

        # ── Section sélection de fichiers ─────────────────────────────────
        has_meta = any(flash_meta.get(s.address) for s in sensors)
        grp_files = QGroupBox("Fichiers à exporter")
        grp_files_layout = QVBoxLayout(grp_files)

        if not has_meta:
            info = QLabel(
                "ℹ Informations flash non chargées — tous les fichiers seront exportés.\n"
                "Cliquer sur « 💽 Flash info » pour sélectionner des fichiers précis."
            )
            info.setStyleSheet("color: #89dceb; font-size: 8pt;")
            info.setWordWrap(True)
            grp_files_layout.addWidget(info)
        else:
            # Boutons Tout sélectionner / Tout désélectionner
            btn_row = QHBoxLayout()
            btn_all = QPushButton("Tout sélectionner")
            btn_none = QPushButton("Tout désélectionner")
            btn_all.clicked.connect(self._select_all)
            btn_none.clicked.connect(self._select_none)
            btn_row.addWidget(btn_all)
            btn_row.addWidget(btn_none)
            btn_row.addStretch()
            grp_files_layout.addLayout(btn_row)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setMaximumHeight(280)
            inner = QWidget()
            inner_layout = QVBoxLayout(inner)
            inner_layout.setContentsMargins(4, 4, 4, 4)
            inner_layout.setSpacing(6)

            for sensor in sensors:
                metas = flash_meta.get(sensor.address, [])
                addr_lbl = QLabel(f"<b>{sensor.address}</b>")
                addr_lbl.setStyleSheet("color: #89b4fa; font-size: 9pt;")
                addr_lbl.setTextFormat(Qt.TextFormat.RichText)
                inner_layout.addWidget(addr_lbl)

                self._file_checks[sensor.address] = []
                if not metas:
                    empty_lbl = QLabel("  (flash vide)")
                    empty_lbl.setStyleSheet("color: #6c7086; font-size: 8pt;")
                    inner_layout.addWidget(empty_lbl)
                else:
                    for meta in metas:
                        rate = int(self._sample_rates.get(sensor.address, 120))
                        cb = QCheckBox(
                            f"  Fichier {meta.file_index} — "
                            f"{meta.sample_count:,} éch., "
                            f"{meta.duration_str(rate)} à {rate} Hz, "
                            f"débuté le {meta.start_datetime()}"
                        )
                        cb.setChecked(True)
                        inner_layout.addWidget(cb)
                        self._file_checks[sensor.address].append((meta.file_index, cb))

            inner_layout.addStretch()
            scroll.setWidget(inner)
            grp_files_layout.addWidget(scroll)

        layout.addWidget(grp_files)

        # ── Boutons OK/Annuler ────────────────────────────────────────────
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.setStyleSheet(
            "QPushButton { background: #313244; color: #cdd6f4; border: 1px solid #45475a; "
            "border-radius: 4px; padding: 5px 14px; } "
            "QPushButton:hover { background: #45475a; }"
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _select_all(self) -> None:
        for checks in self._file_checks.values():
            for _, cb in checks:
                cb.setChecked(True)

    def _select_none(self) -> None:
        for checks in self._file_checks.values():
            for _, cb in checks:
                cb.setChecked(False)

    def selected_data_types(self) -> list[str]:
        """Retourne la liste de types de données choisie."""
        from .export import PAYLOAD_MAP
        idx = self._combo_payload.currentIndex()
        key = self._PAYLOAD_LABELS[idx][1]
        return PAYLOAD_MAP[key]

    def selected_file_indices(self) -> dict[str, list[int]]:
        """
        Retourne un dict adresse→liste d'indices sélectionnés.
        Si aucune métadonnée disponible, retourne {} (= exporter tout).
        Si des fichiers sont décochés, retourne seulement les cochés.
        """
        result: dict[str, list[int]] = {}
        for addr, checks in self._file_checks.items():
            selected = [idx for idx, cb in checks if cb.isChecked()]
            if selected:
                result[addr] = selected
        return result


# ── Dialogue graphique de dispersion temporelle ───────────────────────────────

class JitterDialog(QDialog):
    """
    Boîte de dialogue affichant un graphique de dispersion des offsets
    temporels entre capteurs — dessiné en PyQt6 natif (QPainter).
    """

    def __init__(self, jitter, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Analyse synchronisation — Dispersion temporelle")
        self.setMinimumSize(720, 460)
        self.setStyleSheet("QDialog { background: #1e1e2e; color: #cdd6f4; }")

        from .analysis import JITTER_THRESHOLD_MS
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # En-tête
        color = "#a6e3a1" if jitter.jitter_max_ms <= JITTER_THRESHOLD_MS else "#f38ba8"
        state = "✓ Sync OK" if jitter.success else "⚠ Sync dégradée"
        header = QLabel(
            f"Jitter max : <span style='color:{color}; font-weight:bold;'>"
            f"{jitter.jitter_max_ms:.1f} ms</span>  —  {state}"
            f"  —  {jitter.n_ok}/{jitter.n_sensors} capteurs analysés"
        )
        header.setStyleSheet("font-size: 11pt; color: #cdd6f4; background: transparent;")
        header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(header)

        # Canvas scrollable
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background: #181825; border: 1px solid #44475a;")
        canvas = _JitterCanvas(jitter)
        scroll.setWidget(canvas)
        layout.addWidget(scroll, 1)

        # Bouton Fermer
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.setStyleSheet(
            "QPushButton { background: #313244; color: #cdd6f4; border: 1px solid #45475a; "
            "border-radius: 4px; padding: 4px 14px; } "
            "QPushButton:hover { background: #45475a; }"
        )
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)


class _JitterCanvas(QWidget):
    """Canvas Qt pour le graphique de dispersion (QPainter, zéro dépendance externe)."""

    _BAR_H     = 20
    _BAR_GAP   = 8
    _LEFT_M    = 195
    _RIGHT_M   = 65
    _TOP_M     = 42
    _BOTTOM_M  = 28

    def __init__(self, jitter) -> None:
        super().__init__()
        self.setMinimumWidth(500)
        self._jitter = jitter
        n = max(len(jitter.offsets_ms), 1)
        self.setMinimumHeight(
            self._TOP_M + n * (self._BAR_H + self._BAR_GAP) + self._BOTTOM_M
        )
        self.setStyleSheet("background: #181825;")

    def paintEvent(self, _event) -> None:
        from .analysis import JITTER_THRESHOLD_MS
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        offsets = self._jitter.offsets_ms

        # Fond
        painter.fillRect(0, 0, w, h, QColor("#181825"))

        if not offsets:
            painter.setPen(QColor("#6c7086"))
            painter.drawText(20, 40, "Aucune donnée disponible — lancez d'abord un export.")
            painter.end()
            return

        # Calcul de l'échelle — au moins ±JITTER_THRESHOLD pour toujours voir le seuil
        max_abs = max(abs(v) for v in offsets.values())
        max_abs = max(max_abs * 1.1, JITTER_THRESHOLD_MS * 1.5, 1.0)
        avail_w = w - self._LEFT_M - self._RIGHT_M
        scale = avail_w / (2.0 * max_abs)
        zero_x = self._LEFT_M + avail_w // 2

        # ── Grille et axe ────────────────────────────────────────────────
        pen_grid = QPen(QColor("#2a2a3e"), 1)
        painter.setPen(pen_grid)
        # Graduations tous les 10 ms (ou ajustées)
        step = 5.0
        for mult in (5, 10, 25, 50, 100):
            if (avail_w / (2 * max_abs / mult)) >= 30:
                step = float(mult)
                break
        t = step
        while t <= max_abs * 1.05:
            for sign in (-1, 1):
                gx = zero_x + int(sign * t * scale)
                if self._LEFT_M <= gx <= w - self._RIGHT_M:
                    painter.drawLine(gx, self._TOP_M - 14, gx, h - self._BOTTOM_M)
            t += step

        # Axe horizontal
        pen_axis = QPen(QColor("#44475a"), 1)
        painter.setPen(pen_axis)
        painter.drawLine(self._LEFT_M, self._TOP_M - 14, w - self._RIGHT_M, self._TOP_M - 14)

        # Labels de l'axe
        f_small = QFont("monospace", 7)
        painter.setFont(f_small)
        t = step
        while t <= max_abs * 1.05:
            for sign in (-1, 1):
                gx = zero_x + int(sign * t * scale)
                if self._LEFT_M <= gx <= w - self._RIGHT_M:
                    painter.setPen(QColor("#6c7086"))
                    lbl = f"{sign * t:+.0f}"
                    painter.drawText(gx - 12, self._TOP_M - 16, lbl)
            t += step
        # Zéro
        painter.setPen(QColor("#a6adc8"))
        painter.drawText(zero_x - 4, self._TOP_M - 16, "0")

        # ── Ligne de seuil ───────────────────────────────────────────────
        pen_thresh = QPen(QColor("#f1c40f"), 1, Qt.PenStyle.DashLine)
        painter.setPen(pen_thresh)
        for sign in (-1, 1):
            tx = zero_x + int(sign * JITTER_THRESHOLD_MS * scale)
            if self._LEFT_M <= tx <= w - self._RIGHT_M:
                painter.drawLine(tx, self._TOP_M - 14, tx, h - self._BOTTOM_M)
        # Label seuil
        f_thresh = QFont("monospace", 7)
        painter.setFont(f_thresh)
        painter.setPen(QColor("#f1c40f"))
        tx_r = zero_x + int(JITTER_THRESHOLD_MS * scale)
        painter.drawText(tx_r + 2, self._TOP_M - 17, f"±{JITTER_THRESHOLD_MS:.0f} ms")

        # ── Ligne zéro ───────────────────────────────────────────────────
        pen_zero = QPen(QColor("#585b70"), 1, Qt.PenStyle.DotLine)
        painter.setPen(pen_zero)
        painter.drawLine(zero_x, self._TOP_M - 14, zero_x, h - self._BOTTOM_M)

        # ── Barres par capteur ───────────────────────────────────────────
        sorted_items = sorted(offsets.items(), key=lambda x: x[1])
        f_addr = QFont("monospace", 8)
        f_val  = QFont("monospace", 8)

        for i, (addr, off) in enumerate(sorted_items):
            y = self._TOP_M + i * (self._BAR_H + self._BAR_GAP)
            is_ok = abs(off) <= JITTER_THRESHOLD_MS

            # Étiquette adresse (tronquée à 14 chars depuis la fin)
            painter.setPen(QColor("#cdd6f4"))
            painter.setFont(f_addr)
            painter.drawText(4, y + self._BAR_H - 4, addr[-17:])

            # Barre
            bar_w = max(abs(int(off * scale)), 3)
            bar_x = zero_x if off >= 0 else zero_x - bar_w
            fill_color = QColor("#a6e3a1") if is_ok else QColor("#f38ba8")
            # Fond légèrement plus sombre
            painter.fillRect(
                bar_x, y + 2, bar_w, self._BAR_H - 4,
                fill_color.darker(130),
            )
            painter.fillRect(bar_x, y + 2, bar_w, self._BAR_H - 4, fill_color)

            # Valeur de l'offset à droite de la barre
            painter.setPen(QColor("#cdd6f4"))
            painter.setFont(f_val)
            val_x = zero_x + bar_w + 5 if off >= 0 else zero_x - bar_w - 52
            painter.drawText(val_x, y + self._BAR_H - 4, f"{off:+.1f} ms")

        # ── Légende ──────────────────────────────────────────────────────
        y_leg = h - self._BOTTOM_M + 14
        painter.fillRect(w - self._RIGHT_M - 60, y_leg - 10, 12, 10, QColor("#a6e3a1"))
        painter.setPen(QColor("#a6e3a1"))
        painter.setFont(QFont("sans-serif", 7))
        painter.drawText(w - self._RIGHT_M - 45, y_leg - 1, "≤ seuil")
        painter.fillRect(w - self._RIGHT_M + 5, y_leg - 10, 12, 10, QColor("#f38ba8"))
        painter.setPen(QColor("#f38ba8"))
        painter.drawText(w - self._RIGHT_M + 20, y_leg - 1, "> seuil")

        painter.end()


# ── Point d'entrée ────────────────────────────────────────────────────────────

def run_gui() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(_STYLE_SHEET)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow()
    window.show()

    # SIGTERM / SIGINT (kill, Ctrl+C) → même fermeture propre que closeEvent
    def _sig_close(*_):
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(window._async_close())
        )

    signal.signal(signal.SIGTERM, _sig_close)
    signal.signal(signal.SIGINT,  _sig_close)

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    run_gui()
