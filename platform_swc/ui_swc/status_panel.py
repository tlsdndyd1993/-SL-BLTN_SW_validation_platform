# -*- coding: utf-8 -*-
"""
ui_swc/status_panel.py
리셋 + 로그 패널
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

from engine_swc.settings_db import SettingsDB
class ResetPanel(QWidget):
    def __init__(self, engine: CoreEngine, db: SettingsDB,
                 signals: Signals, parent=None):
        super().__init__(parent)
        self.engine = engine; self.db = db; self.signals = signals
        self._build()

    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(12)

        warn = QLabel(
            "⚠  주의: 아래 버튼들은 설정을 영구적으로 초기화합니다.\n"
            "녹화 중에는 사용을 피하세요.")
        warn.setWordWrap(True)
        warn.setStyleSheet(
            "color:#e74c3c;font-size:11px;font-weight:bold;"
            "background:#1a0a0a;border:1px solid #4a1a1a;"
            "border-radius:4px;padding:8px;")
        v.addWidget(warn)

        items = [
            ("🔴 전체 설정 초기화",
             "모든 설정(DB)을 초기화합니다. ROI, 메모, 매크로, 경로 설정 포함.",
             self._reset_all, "#3a1a1a", "#e74c3c"),
            ("⚪ IO채널 DB 초기화",
             "AI/MCP 외부제어 I/O 채널 DB (commands/state/events)를 초기화합니다.",
             self._reset_io,  "#1a1a2a", "#888"),
        ]

        for title, desc, fn, bg, fg in items:
            grp = QGroupBox(); gl = QVBoxLayout(grp); gl.setSpacing(6)
            grp.setStyleSheet(
                f"QGroupBox{{background:{bg};border:1px solid #2a2a3a;"
                "border-radius:4px;margin-top:0px;}")
            d = QLabel(desc); d.setWordWrap(True)
            d.setStyleSheet("color:#889;font-size:10px;")
            btn = QPushButton(title); btn.setMinimumHeight(34)
            btn.setStyleSheet(
                f"QPushButton{{background:#0d0d1e;color:{fg};"
                "border:1px solid #2a2a3a;border-radius:4px;"
                "font-size:11px;font-weight:bold;}}"
                f"QPushButton:hover{{background:{bg};border-color:{fg};}}")
            btn.clicked.connect(fn)
            gl.addWidget(d); gl.addWidget(btn)
            v.addWidget(grp)

        v.addStretch()

    def _confirm(self, msg: str) -> bool:
        r = QMessageBox.question(
            self, "확인", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return r == QMessageBox.Yes

    def _reset_all(self):
        if not self._confirm("전체 설정을 초기화합니까?\n이 작업은 되돌릴 수 없습니다."): return
        self.db.wipe()
        self.engine.screen_rois.clear(); self.engine.camera_rois.clear()
        self.engine.screen_bo_count = self.engine.camera_bo_count = 0
        self.engine.screen_bo_events.clear(); self.engine.camera_bo_events.clear()
        self.engine.ac_count = 0; self.engine.macro_steps.clear()
        self.signals.status_message.emit("✅ 전체 설정 초기화 완료")
        self.signals.roi_list_changed.emit()
        self.signals.ac_count_changed.emit(0)

    def _reset_roi(self):
        if not self._confirm("ROI 목록을 모두 삭제합니까?"): return
        self.engine.screen_rois.clear(); self.engine.camera_rois.clear()
        self.db.save_roi_items([])
        self.signals.roi_list_changed.emit()
        self.signals.status_message.emit("✅ ROI 초기화 완료")

    def _reset_bo(self):
        if not self._confirm("블랙아웃 카운터와 이벤트 로그를 초기화합니까?"): return
        self.engine.screen_bo_count = self.engine.camera_bo_count = 0
        self.engine.screen_bo_events.clear(); self.engine.camera_bo_events.clear()
        self.signals.status_message.emit("✅ 블랙아웃 카운터 초기화 완료")

    def _reset_ac(self):
        self.engine.reset_ac_count()
        self.signals.status_message.emit("✅ 오토클릭 카운터 초기화 완료")

    def _reset_io(self):
        if not self._confirm("AI/MCP I/O채널 DB를 초기화합니까?"): return
        io_ch = getattr(self.engine, 'io_channel', None)
        if io_ch:
            try:
                with io_ch._conn() as c:
                    c.executescript(
                        "DELETE FROM commands;"
                        "DELETE FROM state;"
                        "DELETE FROM events;")
                self.signals.status_message.emit("✅ IO채널 DB 초기화 완료")
            except Exception as ex:
                self.signals.status_message.emit(f"IO채널 초기화 실패: {ex}")
        else:
            self.signals.status_message.emit("IO채널 미초기화 (io_channel=None)")


# =============================================================================
#  LogPanel
# =============================================================================
class LogPanel(QWidget):
    MAX_LINES = 500

    def __init__(self, signals: Signals, parent=None):
        super().__init__(parent)
        self.signals = signals
        self._lines: deque = deque(maxlen=self.MAX_LINES)
        self._build()
        signals.status_message.connect(self._append)

    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(6)

        ctrl = QHBoxLayout(); ctrl.setSpacing(6)
        self.auto_scroll = QCheckBox("자동 스크롤"); self.auto_scroll.setChecked(True)
        self.auto_scroll.setStyleSheet("font-size:11px;color:#9ab;")
        ctrl.addWidget(self.auto_scroll)
        clr = QPushButton("로그 지우기"); clr.setFixedHeight(26)
        clr.setStyleSheet(
            "QPushButton{background:#1a1a2a;color:#888;border:1px solid #334;"
            "border-radius:3px;font-size:10px;}")
        clr.clicked.connect(self._clear)
        ctrl.addWidget(clr); ctrl.addStretch()
        v.addLayout(ctrl)

        self.log_txt = QPlainTextEdit()
        self.log_txt.setReadOnly(True)
        self.log_txt.setMaximumBlockCount(200)  # ★ 메모리 누수 방지
        self.log_txt.setStyleSheet(
            "QPlainTextEdit{background:#060612;color:#9ab;font-family:Consolas,"
            "Courier New,monospace;font-size:11px;border:1px solid #1a2a3a;"
            "border-radius:4px;}")
        v.addWidget(self.log_txt)

    def _append(self, msg: str):
        ts  = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}]  {msg}"
        self._lines.append(line)
        self.log_txt.appendPlainText(line)
        if self.auto_scroll.isChecked():
            self.log_txt.verticalScrollBar().setValue(
                self.log_txt.verticalScrollBar().maximum())

    def _clear(self):
        self._lines.clear(); self.log_txt.clear()


# =============================================================================
#  CameraWindow / DisplayWindow  (플로팅 미리보기)
