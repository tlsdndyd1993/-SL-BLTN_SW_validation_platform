# -*- coding: utf-8 -*-
"""
ui_swc/autoclick_panel.py
오토클릭 패널
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

class AutoClickPanel(QWidget):
    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine = engine; self.signals = signals
        signals.ac_count_changed.connect(lambda n: self.lcd.display(n))
        # ★ 외부(AI/커널)에서 start_ac/stop_ac 호출 시 UI 동기화
        signals.ac_state_changed.connect(self._on_ac_state_sync)
        self._build()

    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        # 간격 설정
        ig = QGroupBox("클릭 간격"); il = QGridLayout(ig); il.setSpacing(6)
        il.addWidget(QLabel("간격 (초):"), 0, 0)
        self.interval_spin = _make_spinbox(0.1, 3600, 1, 0.1, 1, "", 90)
        self.interval_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'ac_interval', v))
        il.addWidget(self.interval_spin, 0, 1)

        pr = QHBoxLayout(); pr.setSpacing(4)
        for lbl, val in [("0.1s",.1),("0.5s",.5),("1s",1.),("5s",5.),("10s",10.)]:
            b = QPushButton(lbl); b.setFixedSize(44, 22)
            b.setStyleSheet(
                "QPushButton{background:#1e2a3a;border:1px solid #3a4a6a;"
                "color:#ccd;font-size:10px;border-radius:3px;}")
            b.clicked.connect(lambda _, v=val: self.interval_spin.setValue(v))
            pr.addWidget(b)
        il.addLayout(pr, 1, 0, 1, 2)

        adj_g = QGroupBox("간격 조절 (빠른 증감)")
        adj_v = QVBoxLayout(adj_g); adj_v.setSpacing(5)
        plus_row = QHBoxLayout(); plus_row.setSpacing(6)
        minus_row = QHBoxLayout(); minus_row.setSpacing(6)
        for delta, label in [(10,"+10초"),(60,"+1분"),(600,"+10분")]:
            b = QPushButton(label); b.setFixedHeight(28)
            b.setStyleSheet(
                "QPushButton{background:#1a3a1a;color:#8fa;border:1px solid #2a6a2a;"
                "border-radius:4px;font-size:10px;font-weight:bold;}"
                "QPushButton:hover{background:#225a22;}")
            b.clicked.connect(
                lambda _, d=delta: self.interval_spin.setValue(
                    min(3600, self.interval_spin.value()+d)))
            plus_row.addWidget(b)
        for delta, label in [(10,"-10초"),(60,"-1분"),(600,"-10분")]:
            b = QPushButton(label); b.setFixedHeight(28)
            b.setStyleSheet(
                "QPushButton{background:#3a1a1a;color:#f88;border:1px solid #6a2a2a;"
                "border-radius:4px;font-size:10px;font-weight:bold;}"
                "QPushButton:hover{background:#5a1a1a;}")
            b.clicked.connect(
                lambda _, d=delta: self.interval_spin.setValue(
                    max(0.1, self.interval_spin.value()-d)))
            minus_row.addWidget(b)
        adj_v.addLayout(plus_row); adj_v.addLayout(minus_row)
        il.addWidget(adj_g, 2, 0, 1, 2)
        v.addWidget(ig)

        # 카운터
        cg = QGroupBox("클릭 카운터"); cl = QGridLayout(cg)
        from PyQt5.QtWidgets import QLCDNumber
        self.lcd = QLCDNumber(8)
        self.lcd.setSegmentStyle(QLCDNumber.Flat)
        self.lcd.setFixedHeight(44)
        self.lcd.setStyleSheet(
            "background:#0d1520;border:1px solid #336;color:#2ecc71;")
        cl.addWidget(self.lcd, 0, 0, 1, 2)
        rst = QPushButton("카운터 초기화"); rst.setFixedHeight(26)
        rst.clicked.connect(self.engine.reset_ac_count)
        cl.addWidget(rst, 1, 0, 1, 2)
        v.addWidget(cg)

        # 제어
        ctrl = QGroupBox("제어"); ctl = QVBoxLayout(ctrl); ctl.setSpacing(6)
        self.btn_start = QPushButton("▶  시작  [Ctrl+Alt+A]")
        self.btn_start.setStyleSheet(
            "QPushButton{background:#2980b9;color:white;font-size:12px;"
            "padding:7px;border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#3498db;}"
            "QPushButton:disabled{background:#1a2a3a;color:#4a6a8a;}")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop = QPushButton("■  정지  [Ctrl+Alt+S]")
        self.btn_stop.setStyleSheet(
            "QPushButton{background:#5a6a7a;color:white;font-size:12px;"
            "padding:7px;border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#7f8c8d;}"
            "QPushButton:disabled{background:#2a2a2a;color:#555;}")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)
        self.status_lbl = QLabel("● 정지")
        self.status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;")
        ctl.addWidget(self.btn_start); ctl.addWidget(self.btn_stop)
        ctl.addWidget(self.status_lbl)
        v.addWidget(ctrl)
        self._build_click_options(v)

    def _build_click_options(self, v):
        """클릭 타입 + 좌표 지정 옵션 그룹."""
        og = QGroupBox("클릭 옵션")
        ol = QVBoxLayout(og); ol.setSpacing(6)

        # 클릭 버튼 종류
        btn_row = QHBoxLayout(); btn_row.setSpacing(4)
        btn_row.addWidget(QLabel("버튼:"))
        self._btn_grp_btns = {}
        for label, key in [("좌클릭", "left"), ("우클릭", "right"), ("더블", "double")]:
            b = QPushButton(label); b.setCheckable(True)
            b.setFixedHeight(26)
            b.setStyleSheet(
                "QPushButton{background:#1e2a3a;border:1px solid #3a4a6a;"
                "color:#ccd;font-size:10px;border-radius:3px;}"
                "QPushButton:checked{background:#2980b9;color:white;"
                "border-color:#2980b9;}")
            self._btn_grp_btns[key] = b
            btn_row.addWidget(b)
        self._btn_grp_btns["left"].setChecked(True)
        for key, b in self._btn_grp_btns.items():
            b.clicked.connect(lambda _, k=key: self._on_btn_type(k))
        btn_row.addStretch()
        ol.addLayout(btn_row)

        # 좌표 지정
        pos_row = QHBoxLayout(); pos_row.setSpacing(4)
        self._use_pos_chk = QPushButton("📍 좌표 지정")
        self._use_pos_chk.setCheckable(True)
        self._use_pos_chk.setFixedHeight(26)
        self._use_pos_chk.setStyleSheet(
            "QPushButton{background:#1e2a3a;border:1px solid #3a4a6a;"
            "color:#ccd;font-size:10px;border-radius:3px;}"
            "QPushButton:checked{background:#1a5a2a;color:#7fdb9e;"
            "border-color:#2a7a3a;}")
        self._use_pos_chk.toggled.connect(self._on_use_pos_toggle)
        pos_row.addWidget(self._use_pos_chk)
        pos_row.addWidget(QLabel("X:"))
        self._x_spin = _make_spinbox(0, 9999, 0, 1, 1, "", 70)
        self._x_spin.setEnabled(False)
        pos_row.addWidget(self._x_spin)
        pos_row.addWidget(QLabel("Y:"))
        self._y_spin = _make_spinbox(0, 9999, 0, 1, 1, "", 70)
        self._y_spin.setEnabled(False)
        pos_row.addWidget(self._y_spin)
        cap_btn = QPushButton("🎯 현재 좌표 캡처")
        cap_btn.setFixedHeight(26)
        cap_btn.setStyleSheet(
            "QPushButton{background:#1a3a4a;color:#7bc8e0;"
            "border:1px solid #2a5a7a;border-radius:3px;font-size:10px;}"
            "QPushButton:hover{background:#1e4a5a;}")
        cap_btn.clicked.connect(self._capture_cursor_pos)
        pos_row.addWidget(cap_btn)
        pos_row.addStretch()
        ol.addLayout(pos_row)

        # 상태 안내
        self._click_hint = QLabel("ℹ️  클릭 방식: win32api → ctypes → pynput 순 자동 선택")
        self._click_hint.setStyleSheet("color:#667;font-size:9px;")
        self._click_hint.setWordWrap(True)
        ol.addWidget(self._click_hint)

        v.addWidget(og)

    def _on_btn_type(self, key):
        """클릭 타입 선택 (좌/우/더블 — 단일 선택)."""
        for k, b in self._btn_grp_btns.items():
            b.setChecked(k == key)
        if key == "double":
            self.engine.ac_double = True
            self.engine.ac_btn    = "left"
        else:
            self.engine.ac_double = False
            self.engine.ac_btn    = key

    def _on_use_pos_toggle(self, checked):
        self._x_spin.setEnabled(checked)
        self._y_spin.setEnabled(checked)
        self.engine.ac_use_pos = checked
        if checked:
            self.engine.ac_pos_x = int(self._x_spin.value())
            self.engine.ac_pos_y = int(self._y_spin.value())
        else:
            self.engine.ac_pos_x = None
            self.engine.ac_pos_y = None
        self._x_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'ac_pos_x', int(v)))
        self._y_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'ac_pos_y', int(v)))

    def _capture_cursor_pos(self):
        """현재 마우스 커서 위치를 X/Y 스핀박스에 채우기."""
        try:
            import ctypes
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            x, y = pt.x, pt.y
        except Exception:
            if PYNPUT_AVAILABLE:
                x, y = pynput_mouse.Controller().position
            else:
                x, y = 0, 0
        self._x_spin.setValue(x)
        self._y_spin.setValue(y)
        self.engine.ac_pos_x = x
        self.engine.ac_pos_y = y
        self._click_hint.setText(f"📍 캡처됨: X={x}, Y={y}")

    def _on_start(self):
        # 좌표 업데이트
        if self._use_pos_chk.isChecked():
            self.engine.ac_pos_x = int(self._x_spin.value())
            self.engine.ac_pos_y = int(self._y_spin.value())
        else:
            self.engine.ac_pos_x = None
            self.engine.ac_pos_y = None
        self.engine.start_ac()
        # UI는 ac_state_changed 시그널로 업데이트 (중복 방지)

    def _on_stop(self):
        self.engine.stop_ac()
        # UI는 ac_state_changed 시그널로 업데이트 (중복 방지)

    def _on_ac_state_sync(self, running: bool):
        """ac_state_changed 시그널 — UI 스레드에서 실행 (Qt signal 보장)."""
        if running:
            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(True)
            self.status_lbl.setText("● 실행 중")
            self.status_lbl.setStyleSheet("color:#2ecc71;font-weight:bold;")
        else:
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.status_lbl.setText("● 정지")
            self.status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;")


