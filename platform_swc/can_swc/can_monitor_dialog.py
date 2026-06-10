# -*- coding: utf-8 -*-
"""can_swc/can_monitor_dialog.py — CAN 신호 모니터링 다이얼로그."""
import os, json, time, threading
from typing import Optional, List

from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor

from common.signals import Signals

class CanMonitorDialog(QDialog):
    """
    CAN Bus 실시간 신호 모니터 독립 창 (AI챗봇 UI 방식).

    목적:
      - BLTN 제어기 CAN 송수신 정보 실시간 확인
      - HU/CCU 등 외부 환경 모사용 신호 송신 (canoe_write)
      - 신호값 그래프로 시각화
      - 커널 스크립트에서 can.* API 사용 가이드 제공

    탭 구성:
      1. 📊 신호 탐색 / 그래프 — DBC 로드 → 신호 검색 → 실시간 그래프
      2. 📖 커널 스크립트 가이드 — can.* API 코드 예시
      3. 📋 실시간 로그 — 연결/오류 상태 로그

    스크립트 사용 예:
      can.connect_canoe()
      ok = can.can_wait_value("BLTN_CAM_RecStat_OWD", expected=1, timeout=6)
      if not ok:
          engine.start_recording()   # OWD 미확인 → 증적 녹화
      can.canoe_write("HU_BLTN_CAM_02_200ms", {"BLTN_CAM_Set_EV_BfrTime": 30})
    """
    _instance = None

    # thread-safe 시그널 (백그라운드 → 메인 스레드 UI 업데이트)
    _log_signal        = pyqtSignal(str)          # 로그 텍스트
    _lbl_signal        = pyqtSignal(str, bool)    # (텍스트, ok색상)
    _dbc_apply_signal  = pyqtSignal()             # DBC 파싱 완료 → UI 반영
    _can_prog_signal   = pyqtSignal(int, str)     # CANoe 연결 진행 (step, msg)
    _can_done_signal   = pyqtSignal(bool, str)    # CANoe 연결 완료 (ok, msg)

    @classmethod
    def show_or_raise(cls, parent, engine, kernel=None):
        if cls._instance is None or not cls._instance.isVisible():
            cls._instance = cls(parent, engine, kernel)
            cls._instance.show()
        else:
            cls._instance.raise_()
            cls._instance.activateWindow()

    def __init__(self, parent=None, engine=None, kernel=None):
        super().__init__(parent)
        self._engine   = engine
        self._kernel   = kernel
        self._can      = _can_manager
        self._dbc_path = ""
        self._watched: list = []   # [{name, msg, unit, color, data, labels}]
        self._chart_lbl   = None
        self._all_sigs:   list = []
        self._filtered:   list = []
        self._graph_zoom_pts: int = 200   # X축 기본 20초 표시 (휠로 조절, 1pt=0.1s)
        self._graph_y_center: float = 0.0  # Y축 중심값 (드래그로 이동)
        self._graph_y_range:  float = 10.0 # Y축 표시 범위 (기본 10칸)
        self._graph_pan_off:  int = 0     # 과거 스크롤 오프셋 (0=최신)
        self._graph_paused:   bool = False  # 일시정지 플래그
        self._graph_y_zoom:   float = 1.0   # Y축 확대 배율 (Ctrl+휠)
        self._graph_drag_x:   int = 0     # 드래그 시작 X 좌표
        # [문제2] 드래그 모드 분리
        # · 기본: 드래그 → X/Y pan (과거 탐색)
        # · measure 모드 ON: 드래그 → 구간 시간 표시 (pan 비활성)
        self._measure_mode:   bool = False  # 구간 측정 모드
        self._drag_measuring: bool = False  # 현재 드래그 측정 중
        self._drag_start_px:  int  = 0     # 측정 드래그 시작 X
        self._drag_cur_px:    int  = 0     # 측정 드래그 현재 X
        self._drag_start_ts:  float = 0.0  # 측정 시작 타임스탬프

        self.setWindowTitle("📡  CAN Monitor — 신호 모니터 / CAN 입출력")
        self.setWindowModality(Qt.NonModal)
        self.setWindowFlags(
            Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint |
            Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint)
        self.resize(1060, 760)
        # thread-safe 시그널 연결 (백그라운드 스레드 → 메인 스레드)
        self._log_signal.connect(self._log)
        self._lbl_signal.connect(self._set_dbc_lbl)
        self._dbc_apply_signal.connect(self._apply_dbc_result)
        self._can_prog_signal.connect(self._on_can_progress)
        self._can_done_signal.connect(self._on_can_done)
        self._dbc_done_data = {}

        self.setStyleSheet(
            "QDialog{background:#08081a;color:#ccd;}"
            "QGroupBox{color:#556;font-size:10px;border:1px solid #1a2a3a;"
            "border-radius:4px;margin-top:10px;padding-top:8px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:8px;}"
            "QLineEdit,QComboBox{background:#0d0d1e;color:#dde;"
            "border:1px solid #2a3a5a;border-radius:4px;padding:3px 7px;font-size:11px;}"
            "QLineEdit:focus,QComboBox:focus{border-color:#4a8aaa;}"
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:4px;padding:4px 10px;font-size:11px;}"
            "QPushButton:hover{background:#22334a;}"
            "QTableWidget{background:#06060f;color:#ccd;"
            "border:1px solid #1a2a3a;gridline-color:#0f1828;font-size:11px;}"
            "QHeaderView::section{background:#0d0d1e;color:#7bc8e0;"
            "border:none;padding:3px;font-size:10px;}"
            "QLabel{font-size:11px;}"
            "QPlainTextEdit{background:#04040c;color:#9fc;"
            "border:1px solid #1a3a1a;font-family:Consolas,monospace;font-size:11px;}")
        self._build()

    # ── UI 빌드 ──────────────────────────────────────────────────────────────
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # ── 상단: DBC 로드 + CANoe 연결 바 ──────────────────────────────────
        conn_bar = QHBoxLayout(); conn_bar.setSpacing(6)

        dbc_btn = QPushButton("📂 DBC 로드")
        dbc_btn.setFixedHeight(28)
        dbc_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:4px;"
            "font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#22334a;}")
        dbc_btn.clicked.connect(self._on_load_dbc)

        self._dbc_lbl = QLabel("DBC: 미로드")
        self._dbc_lbl.setStyleSheet(
            "color:#556;font-size:10px;background:#0d0d1e;"
            "border:1px solid #1a2a3a;border-radius:3px;padding:2px 8px;")
        self._dbc_lbl.setMinimumWidth(260)

        self._conn_btn = QPushButton("🔌 CANoe 연결")
        self._conn_btn.setFixedHeight(28)
        self._conn_btn.setStyleSheet(
            "QPushButton{background:#1a3a2a;color:#7fdb9e;"
            "border:1px solid #2a6a4a;border-radius:4px;"
            "font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#1e4a32;}"
            "QPushButton:disabled{background:#0a1a0a;color:#3a5a3a;}")
        self._conn_btn.clicked.connect(self._on_connect)

        self._conn_status = QLabel("● 미연결")
        self._conn_status.setStyleSheet(
            "color:#556;font-size:10px;font-weight:bold;"
            "background:#0d0d1e;border:1px solid #1a2a3a;"
            "border-radius:3px;padding:2px 8px;min-width:200px;")

        conn_bar.addWidget(dbc_btn)
        conn_bar.addWidget(self._dbc_lbl, 1)
        conn_bar.addWidget(self._conn_btn)
        conn_bar.addWidget(self._conn_status)
        root.addLayout(conn_bar)

        # ── 탭 ───────────────────────────────────────────────────────────────
        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabWidget::pane{background:#08081a;border:1px solid #1a2a3a;}"
            "QTabBar::tab{background:#0d0d1e;color:#556;padding:6px 14px;"
            "border:1px solid #1a2a3a;border-bottom:none;font-size:11px;}"
            "QTabBar::tab:selected{background:#0f1a2a;color:#7bc8e0;"
            "border-color:#2a4a6a;font-weight:bold;}")
        root.addWidget(tabs, 1)

        self._build_tab_signal(tabs)   # 탭1: 신호 탐색 / 그래프 + Write 통합
        self._build_tab_guide(tabs)    # 탭2: 커널 스크립트 가이드
        self._build_tab_log(tabs)      # 탭3: 실시간 로그

        # 그래프 업데이트 타이머 (250ms)
        self._graph_timer = QTimer(self)
        # ★ 병목 수정: 50ms → 200ms (Qt 페인트 이벤트 포화 방지)
        # CAN 신호 시각화는 200ms(5fps)로 충분. 50ms는 메인스레드 CPU 낭비.
        self._graph_timer.setInterval(200)
        self._graph_timer.timeout.connect(self._update_graph)
        self._graph_timer.start()

    # ── 탭1: 신호 탐색 / 그래프 ──────────────────────────────────────────────
    def _build_tab_signal(self, tabs):
        tab = QWidget()
        h = QHBoxLayout(tab)
        h.setContentsMargins(4, 4, 4, 4); h.setSpacing(0)

        # ★ QSplitter — 신호 목록 / 그래프 영역 마우스로 크기 조절
        splitter = QSplitter(Qt.Horizontal)
        splitter.setStyleSheet(
            "QSplitter::handle{background:#1a2a3a;width:5px;}"
            "QSplitter::handle:hover{background:#2a5aaa;cursor:ew-resize;}")
        h.addWidget(splitter)

        # 좌측: 신호 검색 패널
        lw = QWidget(); lw.setMinimumWidth(220)
        lv = QVBoxLayout(lw); lv.setContentsMargins(4,0,4,0); lv.setSpacing(4)

        self._search_ed = QLineEdit()
        self._search_ed.setPlaceholderText("신호명 / 메시지명 검색…")
        self._search_ed.textChanged.connect(self._do_search)
        lv.addWidget(self._search_ed)

        fr = QHBoxLayout(); fr.setSpacing(4)
        self._flt_sender = QComboBox(); self._flt_sender.addItem("송신 노드 전체")
        self._flt_sender.currentIndexChanged.connect(self._do_search)
        self._flt_recv   = QComboBox(); self._flt_recv.addItem("수신 노드 전체")
        self._flt_recv.currentIndexChanged.connect(self._do_search)
        fr.addWidget(self._flt_sender); fr.addWidget(self._flt_recv)
        lv.addLayout(fr)

        self._sig_table = QTableWidget(0, 2)
        self._sig_table.setHorizontalHeaderLabels(["신호명", "메시지"])
        # ★ Interactive: 사용자가 컬럼 너비 직접 조절 가능
        self._sig_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Interactive)
        self._sig_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Interactive)
        self._sig_table.horizontalHeader().setStretchLastSection(False)
        self._sig_table.setColumnWidth(0, 180)
        self._sig_table.setColumnWidth(1, 130)
        self._sig_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._sig_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._sig_table.setFixedHeight(256)
        self._sig_table.itemSelectionChanged.connect(self._on_sig_selected)
        lv.addWidget(self._sig_table)

        # 신호 흐름 표시 (송신→메시지→수신)
        self._flow_lbl = QLabel("")
        self._flow_lbl.setWordWrap(True)
        self._flow_lbl.setStyleSheet(
            "color:#aaa;font-size:10px;background:#050510;"
            "border:1px solid #1a2a3a;border-radius:3px;padding:6px 8px;")
        self._flow_lbl.setMinimumHeight(76)
        lv.addWidget(self._flow_lbl)

        add_btn = QPushButton("➕  선택 신호를 그래프에 추가")
        add_btn.setFixedHeight(30)
        add_btn.setStyleSheet(
            "QPushButton{background:#1a3a2a;color:#7fdb9e;"
            "border:1px solid #2a6a4a;border-radius:4px;"
            "font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#1e4a32;}")
        add_btn.clicked.connect(self._add_to_graph)
        lv.addWidget(add_btn)

        # ── CAN 신호 출력 (Write) — 신호 탐색과 동일 선택 활용 ───────────
        write_sep = QLabel("─── 📤 신호 출력 (CANoe Write) ───────────")
        write_sep.setStyleSheet(
            "color:#3a5a7a;font-size:9px;padding:4px 0 2px 0;")
        lv.addWidget(write_sep)

        # 값 입력 행 (스핀박스 + 슬라이더)
        val_row = QHBoxLayout(); val_row.setSpacing(4)
        val_row.addWidget(QLabel("값:"))
        self._write_val_spin = QDoubleSpinBox()
        self._write_val_spin.setRange(-1e9, 1e9)
        self._write_val_spin.setDecimals(3)
        self._write_val_spin.setSingleStep(1.0)
        self._write_val_spin.setFixedWidth(90)
        self._write_val_spin.setStyleSheet(
            "QDoubleSpinBox{background:#0d0d1e;color:#f0c040;"
            "border:1px solid #3a3a1a;border-radius:4px;padding:2px 4px;"
            "font-size:12px;font-weight:bold;}")
        self._write_slider = QSlider(Qt.Horizontal)
        self._write_slider.setRange(0, 255)
        self._write_slider.setValue(0)
        self._write_slider.setStyleSheet(
            "QSlider::groove:horizontal{background:#1a2030;height:5px;border-radius:3px;}"
            "QSlider::handle:horizontal{background:#f0c040;width:14px;height:14px;"
            "margin-top:-5px;margin-bottom:-5px;border-radius:7px;}"
            "QSlider::sub-page:horizontal{background:#4a4a20;border-radius:3px;}")
        self._write_slider.valueChanged.connect(
            lambda v: self._write_val_spin.setValue(float(v)))
        self._write_val_spin.valueChanged.connect(
            lambda v: self._write_slider.setValue(max(0, min(255, int(v)))))
        val_row.addWidget(self._write_val_spin)
        val_row.addWidget(self._write_slider, 1)
        lv.addLayout(val_row)

        # 송신 버튼 행
        send_row = QHBoxLayout(); send_row.setSpacing(4)
        self._write_send_btn = QPushButton("📤 선택 신호 출력")
        self._write_send_btn.setFixedHeight(30)
        self._write_send_btn.setStyleSheet(
            "QPushButton{background:#1a3a2a;color:#7fdb9e;"
            "border:1px solid #2a6a4a;border-radius:4px;"
            "font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#1e4a32;}"
            "QPushButton:disabled{background:#0a0a18;color:#334;}")
        self._write_send_btn.clicked.connect(self._do_can_write)
        self._write_send_btn.setEnabled(False)

        self._write_repeat_chk = QPushButton("🔁")
        self._write_repeat_chk.setCheckable(True)
        self._write_repeat_chk.setFixedSize(30, 30)
        self._write_repeat_chk.setToolTip("주기 반복 송신 ON/OFF")
        self._write_repeat_chk.setStyleSheet(
            "QPushButton{background:#1a1a3a;color:#888;"
            "border:1px solid #2a2a5a;border-radius:4px;font-size:13px;}"
            "QPushButton:checked{background:#2a1a1a;color:#f0c040;"
            "border-color:#5a3a1a;}"
            "QPushButton:hover{background:#22224a;}")
        self._write_repeat_chk.toggled.connect(self._on_write_repeat)

        self._write_period_spin = QSpinBox()
        self._write_period_spin.setRange(10, 10000)
        self._write_period_spin.setValue(200)
        self._write_period_spin.setSuffix("ms")
        self._write_period_spin.setFixedWidth(72)
        self._write_period_spin.setStyleSheet(
            "QSpinBox{background:#0d0d1e;color:#f0c040;"
            "border:1px solid #3a3a1a;border-radius:3px;padding:2px;font-size:10px;}")
        send_row.addWidget(self._write_send_btn, 1)
        send_row.addWidget(self._write_repeat_chk)
        send_row.addWidget(self._write_period_spin)
        lv.addLayout(send_row)

        # 송신 이력 (compact)
        self._write_hist = QPlainTextEdit()
        self._write_hist.setReadOnly(True)
        self._write_hist.setMaximumBlockCount(100)  # ★ 메모리 누수 방지
        self._write_hist.setMaximumHeight(60)
        self._write_hist.setStyleSheet(
            "QPlainTextEdit{background:#030308;color:#7bc8e0;"
            "border:1px solid #0a1a2a;"
            "font-family:Consolas,monospace;font-size:9px;}")
        lv.addWidget(self._write_hist)

        # 반복 타이머
        self._write_timer = QTimer(self)
        self._write_timer.timeout.connect(self._do_can_write_silent)

        lv.addStretch()
        splitter.addWidget(lw)

        # 우측: 그래프 + 컨트롤
        rw = QWidget()
        rv = QVBoxLayout(rw); rv.setContentsMargins(0,0,0,0); rv.setSpacing(4)

        self._graph_w = QWidget()
        self._graph_w.setMinimumHeight(280)
        self._graph_w.setStyleSheet(
            "background:#03030a;border:1px solid #1a2a3a;border-radius:4px;")
        self._graph_w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # ★ 휠 이벤트 활성화
        self._graph_w.setFocusPolicy(Qt.WheelFocus)
        self._graph_w.wheelEvent       = self._on_graph_wheel
        self._graph_w.mousePressEvent  = self._on_graph_mouse_press
        self._graph_w.mouseMoveEvent   = self._on_graph_mouse_move
        self._graph_w.mouseReleaseEvent= self._on_graph_mouse_release
        self._graph_w.setCursor(Qt.OpenHandCursor)
        gi = QVBoxLayout(self._graph_w); gi.setContentsMargins(0,0,0,0)
        self._graph_hint = QLabel(
            "신호를 추가하면 실시간 그래프가 표시됩니다\n"
            "휠: 시간 범위 확대/축소  |  Ctrl+휠: Y축 확대/축소\n"
            "드래그: 과거 탐색(pan)  |  [📏 구간 측정 ON] 후 드래그: 두 지점 간 시간 측정")
        self._graph_hint.setAlignment(Qt.AlignCenter)
        self._graph_hint.setStyleSheet("color:#223;font-size:12px;")
        gi.addWidget(self._graph_hint)
        self._chart_lbl = QLabel(self._graph_w)
        self._chart_lbl.setVisible(False)
        rv.addWidget(self._graph_w, 1)

        # 현재값 칩 행
        cw = QWidget(); cw.setFixedHeight(40)
        cw.setStyleSheet("background:#0a0a18;border:1px solid #1a2a3a;border-radius:3px;")
        self._chips_lay = QHBoxLayout(cw)
        self._chips_lay.setContentsMargins(8,4,8,4); self._chips_lay.setSpacing(6)
        self._chips_lay.addStretch()
        rv.addWidget(cw)

        gc = QHBoxLayout(); gc.setSpacing(6)
        clr_btn = QPushButton("🗑 그래프 초기화")
        clr_btn.setFixedHeight(26)
        clr_btn.setStyleSheet(
            "QPushButton{background:#2a1a1a;color:#f88;"
            "border:1px solid #5a2a2a;border-radius:4px;font-size:10px;}")
        clr_btn.clicked.connect(self._clear_graph)
        # 그래프 컨트롤 버튼
        self._pause_btn = QPushButton("⏸ 일시정지")
        self._pause_btn.setFixedHeight(26)
        self._pause_btn.setCheckable(True)
        self._pause_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#f0c040;"
            "border:1px solid #3a3a1a;border-radius:4px;font-size:10px;}"
            "QPushButton:checked{background:#2a1a1a;color:#f88;"
            "border-color:#5a2a2a;}"
            "QPushButton:hover{background:#22334a;}")
        self._pause_btn.clicked.connect(self._on_graph_pause)

        # [문제2] 구간 측정 모드 버튼 — ON시 드래그=구간시간, OFF시 드래그=pan
        self._measure_btn = QPushButton("📏 구간 측정 OFF")
        self._measure_btn.setFixedHeight(26)
        self._measure_btn.setCheckable(True)
        self._measure_btn.setToolTip(
            "ON: 드래그로 두 지점 간 시간 측정\n"
            "OFF: 드래그로 그래프 과거 탐색 (pan)")
        self._measure_btn.setStyleSheet(
            "QPushButton{background:#1a1a3a;color:#7bc8e0;"
            "border:1px solid #2a3a5a;border-radius:4px;font-size:10px;}"
            "QPushButton:checked{background:#1a3a1a;color:#ffdd88;"
            "border-color:#4a6a1a;font-weight:bold;}"
            "QPushButton:hover{background:#22224a;}")
        self._measure_btn.toggled.connect(self._on_measure_mode_toggled)

        gc.addWidget(self._pause_btn)
        gc.addWidget(self._measure_btn)
        gc.addStretch(); gc.addWidget(clr_btn)
        rv.addLayout(gc)

        # ★ 일시정지 시 과거 이력 스크롤바
        self._graph_scroll = QScrollBar(Qt.Horizontal)
        self._graph_scroll.setRange(0, 0)
        self._graph_scroll.setValue(0)
        self._graph_scroll.setFixedHeight(16)
        self._graph_scroll.setStyleSheet(
            "QScrollBar:horizontal{background:#0a0a18;height:16px;border-radius:4px;}"
            "QScrollBar::handle:horizontal{background:#2a5aaa;border-radius:4px;min-width:30px;}"
            "QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{width:0px;}")
        self._graph_scroll.valueChanged.connect(self._on_graph_scroll)
        self._graph_scroll.setVisible(False)
        rv.addWidget(self._graph_scroll)
        splitter.addWidget(rw)
        splitter.setSizes([300, 700])   # 초기 비율 30:70

        tabs.addTab(tab, "📊 신호 탐색 / 그래프")

    # ── 탭3: 커널 스크립트 가이드 ────────────────────────────────────────────
    def _build_tab_guide(self, tabs):
        tab = QWidget()
        v = QVBoxLayout(tab); v.setContentsMargins(4,4,4,4)

        hint = QLabel(
            "💡 커널 패널 스크립트에서 can.* API로 CAN 버스를 제어하세요.  "
            "CAN Monitor는 신호 확인 / CANoe 연결 전용입니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet(
            "color:#778;font-size:10px;background:#0a0a18;"
            "border:1px solid #1a2a3a;border-radius:4px;padding:8px;")
        v.addWidget(hint)

        doc = QPlainTextEdit()
        doc.setReadOnly(True)
        doc.setStyleSheet(
            "QPlainTextEdit{background:#030308;color:#9fc;"
            "border:1px solid #1a3a1a;font-family:Consolas,monospace;font-size:11px;}")
        doc.setPlainText(
            "# =====================================================\n"
            "# CAN 커널 스크립트 API  (커널 패널에서 사용)\n"
            "# =====================================================\n\n"
            "# 1) CANoe 연결\n"
            "if can.connect_canoe():\n"
            "    log(\'CANoe 연결 완료\')\n\n"
            "# 2) 신호 읽기 (실시간 캐시 — 100ms 주기 폴링)\n"
            "v = can.can_read(\'BLTN_CAM_RecStat_OWD\')\n"
            "log(f\'OWD RecStat = {v}\')\n\n"
            "# 3) 신호값 대기 (BLTN Key on 후 6초 안에 OWD=1 확인)\n"
            "ok = can.can_wait_value(\'BLTN_CAM_RecStat_OWD\',\n"
            "                        expected=1, timeout=6)\n"
            "if not ok:\n"
            "    log(\'OWD 1 미확인 -> 증적 녹화 시작\')\n"
            "    engine.start_recording()\n\n"
            "# 4) 폴링 신호 등록 (등록 후 sig_cache 자동 갱신)\n"
            "can.add_poll_signal(\'BLTN_CAM_RecStat_OWD\',\n"
            "                    \'BLTN_CAM_FD_STAT_01_200ms\')\n\n"
            "# 5) 주변 제어기 모사 (CANoe write)\n"
            "can.canoe_write(\'HU_BLTN_CAM_02_200ms\', {\n"
            "    \'BLTN_CAM_Set_EV_BfrTime\': 30,\n"
            "    \'BLTN_CAM_Set_EV_AftTime\': 10,\n"
            "})\n\n"
            "# =====================================================\n"
            "# 전체 예시: BLTN Key on -> 6초 안에 OWD=1 확인\n"
            "# =====================================================\n"
            "import time\n"
            "can.connect_canoe()\n"
            "can.add_poll_signal(\'BLTN_CAM_RecStat_OWD\',\n"
            "                    \'BLTN_CAM_FD_STAT_01_200ms\')\n"
            "kernel.power_on(\'ign\')\n"
            "kernel.wait(1.0)\n"
            "ok = can.can_wait_value(\'BLTN_CAM_RecStat_OWD\', 1, timeout=6)\n"
            "if ok:\n"
            "    log(\'OWD 1 확인 -> PASS\')\n"
            "    kernel.set_tc_result(\'PASS\')\n"
            "else:\n"
            "    log(\'OWD 1 미확인 -> 증적 녹화 + FAIL\')\n"
            "    engine.start_recording()\n"
            "    kernel.wait(10)\n"
            "    engine.stop_recording()\n"
            "    engine.capture_frame(\'screen\', tc_tag=\'FAIL\')\n"
            "    kernel.set_tc_result(\'FAIL\')\n"
            "kernel.power_off(\'ign\')\n"
        )
        v.addWidget(doc, 1)
        tabs.addTab(tab, "📖 커널 스크립트 가이드")

    # ── 탭3: 실시간 로그 ─────────────────────────────────────────────────────
    def _build_tab_log(self, tabs):
        tab = QWidget()
        v = QVBoxLayout(tab); v.setContentsMargins(4,4,4,4)
        self._can_log = QPlainTextEdit()
        self._can_log.setReadOnly(True)
        self._can_log.setMaximumBlockCount(500)  # ★ 메모리 누수 방지
        self._can_log.setStyleSheet(
            "QPlainTextEdit{background:#030308;color:#7bc8e0;"
            "border:1px solid #0a1a2a;font-family:Consolas,monospace;font-size:10px;}")
        lc = QHBoxLayout()
        clb = QPushButton("🗑 로그 지우기"); clb.setFixedHeight(24)
        clb.setStyleSheet(
            "QPushButton{background:#0a0a1a;color:#556;"
            "border:1px solid #1a1a2a;border-radius:3px;font-size:10px;}")
        clb.clicked.connect(self._can_log.clear)
        lc.addStretch(); lc.addWidget(clb)
        v.addWidget(self._can_log, 1)
        v.addLayout(lc)
        tabs.addTab(tab, "📋 실시간 로그")

    # ── DBC 로드 (백그라운드 파싱 — COM 미사용) ──────────────────────────────
    def _on_load_dbc(self):
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "DBC 파일 선택", "", "DBC Files (*.dbc);;All Files (*)")
        if not path: return
        self._dbc_path = path
        self._log(f"[DBC] 파싱 중: {path}")
        threading.Thread(
            target=self._parse_dbc_bg, args=(path,),
            daemon=True, name="DBC_PARSE").start()

    def _set_dbc_lbl(self, txt: str, ok: bool = True):
        """DBC 상태 레이블 업데이트 (메인 스레드에서 실행)."""
        self._dbc_lbl.setText(txt)
        self._dbc_lbl.setStyleSheet(
            ("color:#7fdb9e;font-size:10px;background:#051a0a;"
             "border:1px solid #2a6a4a;border-radius:3px;padding:2px 8px;")
            if ok else
            ("color:#f0c040;font-size:10px;background:#0d0d05;"
             "border:1px solid #3a3a1a;border-radius:3px;padding:2px 8px;"))

    def _apply_dbc_result(self):
        """DBC 파싱 완료 후 UI 반영 — 반드시 메인 스레드에서 실행."""
        d = getattr(self, '_dbc_done_data', {})
        if not d: return
        all_sigs = d.get('all_sigs', [])
        self._all_sigs = all_sigs
        # ★ CanBusManager에 신호 목록 참조 연결 (can_read 자동 폴링 등록용)
        self._can._all_sigs_ref = all_sigs
        senders = sorted(set(s['sender'] for s in all_sigs))
        recvs   = sorted(set(r for s in all_sigs for r in s['receivers']))
        for cb, items, lbl in [
                (self._flt_sender, senders, "송신 노드 전체"),
                (self._flt_recv,   recvs,   "수신 노드 전체")]:
            cb.blockSignals(True); cb.clear(); cb.addItem(lbl)
            for n in items: cb.addItem(n)
            cb.blockSignals(False)
        n_s = len(all_sigs)
        self._search_ed.setPlaceholderText(f"신호명 / 메시지명 검색 ({n_s}개)…")
        self._do_search()
        self._dbc_done_data = {}

    def _parse_dbc_bg(self, path):
        """
        DBC 파싱 — 순수 Python regex, COM 미사용.
        ★ thread-safe: QTimer.singleShot 대신 pyqtSignal 사용
           (QTimer.singleShot은 non-thread-safe → UI 미반영 버그 원인)
        """
        import re as _re, os as _os, traceback as _tb

        # ★ thread-safe emit (백그라운드 스레드에서 안전)
        def _ui(msg):  self._log_signal.emit(msg)
        def _lbl(txt, ok=True): self._lbl_signal.emit(txt, ok)

        try:
            # ── STEP 1: 파일 열기 ────────────────────────────────────────────
            fname = _os.path.basename(path)
            fsize = _os.path.getsize(path)
            _lbl(f"⏳ 파싱 중: {fname}", ok=False)
            _ui(f"[DBC] ① 파일 열기: {fname}  ({fsize/1024:.1f} KB)")

            try:
                txt = open(path, encoding='utf-8', errors='replace').read()
            except Exception as e:
                _ui(f"[DBC] ❌ 파일 열기 실패: {e}")
                _lbl("DBC: 열기 실패", ok=False); return

            # ── STEP 2: 블록 분할 ────────────────────────────────────────────
            blocks = _re.split(r'\n(?=BO_ )', txt)

            msg_blocks = [b for b in blocks if _re.match(r'BO_ \d+', b)]
            _ui(f"[DBC] ② 메시지 블록 분할: {len(msg_blocks)}개 메시지 발견")
            _lbl(f"⏳ 신호 파싱 중… (0 / {len(msg_blocks)} 메시지)", ok=False)

            # ── STEP 3: 신호 파싱 ────────────────────────────────────────────
            _SIG_RE = (_re.compile(
                r'SG_ (\S+) : \d+\|\d+@\d+[+-]'
                r' \([^,]+,[^)]+\) \[[^|]*\|[^\]]*\]'
                r' "([^"]*)"  (\S+)'))
            all_sigs = []; msgs_cnt = 0
            report_step = max(1, len(msg_blocks) // 10)  # 10% 단위 진행 보고

            for i, block in enumerate(msg_blocks):
                m = _re.match(r'BO_ (\d+) (\S+): \d+ (\S+)', block)
                if not m: continue
                mid, mname, sender = m.groups(); msgs_cnt += 1
                for sm in _SIG_RE.finditer(block):
                    sname, unit, recvs = sm.groups()
                    rv = [r for r in recvs.split(',')
                          if r and r != 'Vector__XXX']
                    all_sigs.append({
                        'name': sname, 'msg': mname, 'id': hex(int(mid)),
                        'sender': sender, 'receivers': rv, 'unit': unit or ''})
                # 10% 단위 진행 보고
                if (i + 1) % report_step == 0 or i == len(msg_blocks) - 1:
                    pct = int((i + 1) / len(msg_blocks) * 100)
                    _lbl(f"⏳ 신호 파싱 중… {pct}%  ({msgs_cnt} 메시지 / {len(all_sigs)} 신호)", ok=False)
                    _ui(f"[DBC] ③ 파싱 진행: {pct}%  {msgs_cnt}개 메시지, {len(all_sigs)}개 신호")

            # ── STEP 4: cantools 로드 시도 ───────────────────────────────────
            _ui(f"[DBC] ④ cantools 로드 시도…")
            ct_ok  = self._can.load_dbc(path)
            ct_str = 'cantools OK' if ct_ok else 'cantools 미설치 (regex 파싱만 사용)'
            _ui(f"[DBC] ④ cantools: {ct_str}")

            # ── STEP 5: UI 반영 ──────────────────────────────────────────────
            n_s = len(all_sigs); n_m = msgs_cnt
            _ui(f"[DBC] ⑤ UI 반영 중…")

            # ★ _upd_signal로 메인 스레드에 완료 데이터 전달
            #   lambda 캡처로 all_sigs, fname 등 전달
            self._dbc_done_data = {
                'all_sigs': all_sigs, 'fname': fname,
                'n_s': n_s, 'n_m': n_m, 'ct_str': ct_str}
            self._log_signal.emit(
                f"[DBC] ✅ 로드 완료: {n_s}개 신호, {n_m}개 메시지  ({ct_str})")
            self._lbl_signal.emit(
                f"DBC: {fname}  ({n_s}개 신호, {n_m}개 메시지)", True)
            # UI 반영 — signal로 메인 스레드에서 실행
            self._dbc_apply_signal.emit()

        except Exception as ex:
            tb = _tb.format_exc()
            self._log_signal.emit(f"[DBC] ❌ 파싱 예외:\n{tb}")
            self._lbl_signal.emit("DBC: 파싱 실패", False)

    # ── CANoe 연결/해제 ───────────────────────────────────────────────────────
    def _on_connect(self):
        """
        CANoe COM 연결 버튼 핸들러.
        ★ thread-safe: QTimer.singleShot 대신 pyqtSignal 사용.
           _can_prog_signal / _can_done_signal → 메인 스레드에서 UI 업데이트.
        """
        if self._can.connected or self._can.canoe_mode:
            self._can.disconnect()
            self._set_conn_state(connected=False, msg="미연결")
            self._conn_btn.setText("🔌 CANoe 연결")
            self._log("[CAN] 연결 해제")
            return

        self._conn_btn.setEnabled(False)
        self._set_conn_state(connecting=True)
        self._log("[CAN] CANoe COM 연결 시작…")

        def _progress(step, msg):
            """백그라운드에서 pyqtSignal.emit — thread-safe."""
            self._can_prog_signal.emit(step, msg)

        def _worker():
            """백그라운드 연결 스레드."""
            ok  = self._can.connect_canoe(progress_cb=_progress)
            msg = self._can.status_msg
            self._can_done_signal.emit(ok, msg)

        threading.Thread(target=_worker, daemon=True,
                         name="CANoe_CONNECT").start()

    def _on_can_progress(self, step: int, msg: str):
        """CANoe 연결 진행 콜백 — 메인 스레드에서 실행 (pyqtSignal)."""
        _OSI_LAYERS = ["L1 물리","L2 데이터링크","L3 네트워크",
                       "L4 전송","L5 세션","L6 표현","L7 응용"]
        layer = _OSI_LAYERS[step] if 0 <= step < 7 else "연결"
        _OSI_BAR = ["▓░░░░░░","▓▓░░░░░","▓▓▓░░░░",
                    "▓▓▓▓░░░","▓▓▓▓▓░░","▓▓▓▓▓▓░","▓▓▓▓▓▓▓"]
        bar = _OSI_BAR[min(step, 6)] if 0 <= step <= 6 else "░░░░░░░"
        # 상태 레이블: OSI 진행바
        clean = msg
        for tag in ["[L1 물리] ","[L2 데이터링크] ","[L3 네트워크] ",
                    "[L4 전송] ","[L5 세션] ","[L6 표현] ","[L7 응용] "]:
            clean = clean.replace(tag, "")
        self._conn_status.setText(f"⏳ [{bar}] {layer}  {clean[:30]}")
        self._conn_status.setStyleSheet(
            "color:#f0c040;font-size:10px;font-weight:bold;"
            "background:#0d0d05;border:1px solid #3a3a1a;"
            "border-radius:3px;padding:2px 8px;min-width:200px;")
        # 실시간 로그
        self._log(f"[CAN] {msg}")

    def _on_can_done(self, ok: bool, msg: str):
        """CANoe 연결 완료 콜백 — 메인 스레드에서 실행 (pyqtSignal)."""
        self._conn_btn.setEnabled(True)
        if ok:
            self._conn_btn.setText("🔴 연결 해제")
            self._set_conn_state(connected=True, msg=msg[:35])
            self._log(f"[CAN] ✅ 연결 완료: {msg}")
            # ★ 커널/AI Chat에 CAN 연결 완료 알림
            try:
                from datetime import datetime as _dt
                _can_status = (f"CANoe 연결됨 | DBC: "
                               f"{'로드됨 ' + str(len(self._all_sigs)) + '개 신호' if self._all_sigs else '미로드'}")
                self._log(f"[CAN] 커널/AI Chat에서 can_read(), can_write() 사용 가능")
                self._log(f"[CAN] 예: can.can_read('BLTN_CAM_RecSta_OWD')")
                # ★ 타이머 패널 CAN 조건 UI 활성화
                # [버그 수정] self._timer_panel_ref는 CanMonitorDialog에 없음
                # → self._engine을 통해 CoreEngine에 저장된 _timer_panel_ref 접근
                try:
                    eng = getattr(self, '_engine', None)
                    if eng is not None:
                        # engine에 플래그 저장 (패널이 나중에 열릴 때 적용)
                        eng._can_available_for_timer = True
                        # 이미 타이머 패널이 열려 있으면 즉시 활성화
                        timer_panel = getattr(eng, '_timer_panel_ref', None)
                        if timer_panel is not None:
                            timer_panel.set_can_available(True)
                except Exception:
                    pass
            except Exception:
                pass
        else:
            self._conn_btn.setText("🔌 CANoe 연결")
            self._set_conn_state(connected=False, msg="연결 실패")
            self._log(f"[CAN] ❌ 연결 실패: {msg}")
            # ★ 타이머 패널 CAN 조건 UI 비활성화
            try:
                eng = getattr(self, '_engine', None)
                if eng is not None:
                    eng._can_available_for_timer = False
                    timer_panel = getattr(eng, '_timer_panel_ref', None)
                    if timer_panel is not None:
                        timer_panel.set_can_available(False)
            except Exception:
                pass
            m = msg.lower()
            if "pywin32" in m or "l1" in m:
                self._log("[CAN]  → pip install pywin32")
            elif "start" in m or "l5" in m or "measurement" in m:
                self._log("[CAN]  → CANoe ▶ Start Measurement 버튼 클릭")
            elif "타임아웃" in m or "l3" in m or "dispatch" in m:
                self._log("[CAN]  → CANoe.exe 실행 중인지 확인")
                self._log("[CAN]  → canoe_test.py 스크립트로 직접 진단해보세요")

    def _set_conn_state(self, connected=False, connecting=False, msg=""):
        """연결 상태 레이블 업데이트."""
        if connecting:
            disp = f"⏳ {msg[:30]}" if msg else "⏳ 연결 중…"
            self._conn_status.setText(disp)
            self._conn_status.setStyleSheet(
                "color:#f0c040;font-size:10px;font-weight:bold;"
                "background:#0d0d05;border:1px solid #3a3a1a;"
                "border-radius:3px;padding:2px 8px;min-width:200px;")
        elif connected:
            disp = f"● 연결됨  {msg}" if msg else "● 연결됨"
            self._conn_status.setText(disp)
            self._conn_status.setStyleSheet(
                "color:#7fdb9e;font-size:10px;font-weight:bold;"
                "background:#051a0a;border:1px solid #2a6a4a;"
                "border-radius:3px;padding:2px 8px;min-width:200px;")
        else:
            disp = f"● {msg}" if msg else "● 미연결"
            self._conn_status.setText(disp)
            self._conn_status.setStyleSheet(
                "color:#e74c3c;font-size:10px;font-weight:bold;"
                "background:#1a0505;border:1px solid #5a2a2a;"
                "border-radius:3px;padding:2px 8px;min-width:200px;")

    # ── 신호 검색 ─────────────────────────────────────────────────────────────
    def _do_search(self):
        q      = self._search_ed.text().lower()
        sender = self._flt_sender.currentText()
        recv   = self._flt_recv.currentText()
        self._filtered = [
            s for s in self._all_sigs
            if (not q or q in s['name'].lower() or q in s['msg'].lower())
            and (sender == "송신 노드 전체" or s['sender'] == sender)
            and (recv   == "수신 노드 전체" or recv in s['receivers'])
        ][:300]
        self._sig_table.setRowCount(0)
        self._sig_table.setRowCount(len(self._filtered))
        for i, sig in enumerate(self._filtered):
            self._sig_table.setItem(i, 0, QTableWidgetItem(sig['name']))
            self._sig_table.setItem(i, 1, QTableWidgetItem(sig['msg']))

    def _on_sig_selected(self):
        row = self._sig_table.currentRow()
        if row < 0 or row >= len(self._filtered): return
        sig = self._filtered[row]
        rv  = ' / '.join(sig['receivers']) if sig['receivers'] else '수신 없음'
        us  = ("  단위: " + sig['unit']) if sig['unit'] else ""
        lines = [
            "\U0001f7e6 송신:    " + sig['sender'],
            "        \u2193",
            "\U0001f4e6 메시지:  " + sig['msg'] + "  (" + sig['id'] + ")",
            "        \u2193",
            "\U0001f7e9 수신:    " + rv,
            "\U0001f537 신호:    " + sig['name'] + us,
        ]
        self._flow_lbl.setText("\n".join(lines))
        # ★ 신호 선택 시 Write 버튼 활성화
        if hasattr(self, '_write_send_btn'):
            self._write_send_btn.setEnabled(True)
            self._write_send_btn.setText(
                f"📤 {sig['name'][:20]} 출력")
            self._write_send_btn.setToolTip(
                f"메시지: {sig['msg']}\n신호: {sig['name']}\n"
                f"값: (슬라이더/스핀박스 설정)")

    # ── 그래프 ────────────────────────────────────────────────────────────────
    _COLORS  = ['#378ADD', '#1D9E75', '#D85A30', '#BA7517',
                '#D4537E', '#8B5CF6', '#639922', '#E24B4A']
    # [문제1] 3분 × 50samples/s = 9000pts (그래프 흘러가는 창)
    # → 사용자에게 보이는 건 zoom_pts(기본 200=20초)이며,
    #   전체 9000pts는 백버퍼로 유지해 드래그로 과거 탐색 가능
    _MAX_PTS = 9000   # 3분치 보관 @ 20ms 폴링

    def _add_to_graph(self):
        row = self._sig_table.currentRow()
        if row < 0 or row >= len(self._filtered): return
        sig = self._filtered[row]
        if any(w['name'] == sig['name'] for w in self._watched):
            self._log(f"[Graph] {sig['name']} 이미 추가됨"); return
        if len(self._watched) >= 8:
            removed = self._watched.pop(0)
            self._log(f"[Graph] 최대 8개 초과 — {removed['name']} 제거")
        color = self._COLORS[len(self._watched) % len(self._COLORS)]
        # ★ _ev_ptr을 현재 이벤트 리스트 끝으로 설정 → 추가 시점 이후 데이터만 표시
        ev_key = f"_EV_{sig['name']}"
        ev_now = self._can.sig_cache.get(ev_key)
        # 신호 추가 시점의 타임스탬프 커서 초기화 (이 이후 데이터만 표시)
        import time as _t_add
        ev_ts_init = (ev_now[-1][0] if isinstance(ev_now, list) and ev_now
                      else _t_add.time())
        self._watched.append({
            'name': sig['name'], 'msg': sig['msg'],
            'unit': sig['unit'], 'color': color,
            'data': [], 'labels': [],
            '_ev_ts_cursor': ev_ts_init})  # 타임스탬프 기반 커서
        # CANoe 폴링 등록 (연결된 경우)
        if self._can.connected or self._can.canoe_mode:
            self._can.add_poll_signal(sig['name'], sig['msg'])
            self._log(f"[Graph] + {sig['name']} 폴링 등록 (msg={sig['msg']}, canoe={self._can.canoe_mode})")
            self._log("[Graph]  → 신호값이 sig_cache에 들어오면 그래프 자동 업데이트")
        else:
            self._log(f"[Graph] + {sig['name']} 추가 (CANoe 미연결 — 연결 후 자동 폴링)")
        self._rebuild_chips()

    def _rebuild_chips(self):
        """현재값 칩 행 재구성."""
        while self._chips_lay.count():
            it = self._chips_lay.takeAt(0)
            if it.widget(): it.widget().setParent(None)
        for i, w in enumerate(self._watched):
            chip = QPushButton(f"● {w['name'][:20]}  ✕")
            chip.setFixedHeight(28)
            chip.setStyleSheet(
                f"QPushButton{{background:#0d0d1e;color:{w['color']};"
                "border:1px solid #1a2a3a;border-radius:10px;"
                "font-size:10px;padding:0 8px;}}"
                "QPushButton:hover{background:#1a1a2a;}")
            chip.setObjectName(f"chip_{i}")
            chip.clicked.connect((lambda _, idx=i: self._remove_watch(idx)))
            self._chips_lay.addWidget(chip)
        self._chips_lay.addStretch()
        if not self._watched:
            self._graph_hint.setVisible(True)
            if self._chart_lbl: self._chart_lbl.setVisible(False)

    def _update_chip_text(self):
        """칩에 최신값 표시."""
        for i in range(self._chips_lay.count()):
            it = self._chips_lay.itemAt(i)
            if not it or not it.widget(): continue
            chip = it.widget()
            oname = chip.objectName()
            if not oname.startswith("chip_"): continue
            try: idx = int(oname[5:])
            except: continue
            if idx >= len(self._watched): continue
            w = self._watched[idx]
            val = w['data'][-1] if w['data'] else None
            vs  = f"{val:.3f}" if val is not None else "--"
            us  = f" {w['unit']}" if w['unit'] else ""
            chip.setText(f"● {w['name'][:16]}  {vs}{us}  ✕")

    def _remove_watch(self, idx):
        if 0 <= idx < len(self._watched):
            self._watched.pop(idx); self._rebuild_chips()

    def _clear_graph(self):
        self._watched.clear(); self._rebuild_chips()

    def _get_selected_sig(self):
        """현재 신호 테이블에서 선택된 신호 dict 반환."""
        row = getattr(self, '_sig_table', None)
        if row is None: return None
        row = self._sig_table.currentRow()
        if row < 0 or row >= len(self._filtered): return None
        return self._filtered[row]

    def _do_can_write(self):
        """선택된 신호를 현재 값으로 CANoe 출력."""
        sig = self._get_selected_sig()
        if not sig:
            self._log("[Write] ❌ 신호를 먼저 선택하세요"); return
        if not (self._can.connected or self._can.canoe_mode):
            self._log("[Write] ❌ CANoe 미연결"); return
        val = self._write_val_spin.value()
        ok  = self._can.canoe_write(sig['msg'], {sig['name']: val})
        ts  = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}] {'✅' if ok else '❌'} {sig['msg']}.{sig['name']} = {val}"
        self._write_hist.appendPlainText(line)
        self._write_hist.verticalScrollBar().setValue(
            self._write_hist.verticalScrollBar().maximum())
        self._log(f"[Write] {line}")
        if not ok:
            err = getattr(self._can, 'status_msg', '') or ''
            # \n 이스케이프 처리 후 줄별 출력
            err_lines = err.replace('\\n', '\n').split('\n')
            for el in err_lines:
                if el.strip(): self._log(f"[Write]    {el.strip()}")
            db = getattr(self._can, '_db', None)
            self._log(f"[Write]  DB: {'✅ cantools 로드됨' if db else '❌ DBC 없음'}")
            self._log(f"[Write]  메시지: {sig['msg']}  신호: {sig['name']}")

    def _do_can_write_silent(self):
        """반복 타이머용 조용한 write."""
        sig = self._get_selected_sig()
        if sig and (self._can.connected or self._can.canoe_mode):
            self._can.canoe_write(
                sig['msg'], {sig['name']: self._write_val_spin.value()})

    def _on_write_repeat(self, on: bool):
        if on:
            period = self._write_period_spin.value()
            self._write_timer.start(period)
            self._log(f"[Write] 반복 송신 시작 ({period}ms)")
        else:
            self._write_timer.stop()
            self._log("[Write] 반복 송신 중지")

    def _on_graph_pause(self, checked: bool):
        """일시정지 버튼 핸들러."""
        self._graph_paused = checked
        self._pause_btn.setText("▶ 재개" if checked else "⏸ 일시정지")
        if checked:
            self._update_graph_scrollbar()
            self._graph_scroll.setVisible(True)
        else:
            self._graph_scroll.setVisible(False)
            self._graph_pan_off = 0
            # Y 중심도 데이터 중심으로 자동 복귀
            all_v = [v for w in self._watched for v in w['data'] if v is not None]
            if all_v:
                self._graph_y_center = (min(all_v) + max(all_v)) / 2.0
                self._graph_y_range  = max(max(all_v) - min(all_v) + 2, 10.0)
            if self._watched: self._draw_graph()

    def _update_graph_scrollbar(self):
        """스크롤바 범위 업데이트 (일시정지 중)."""
        if not self._watched: return
        max_pts = max((len(w['data']) for w in self._watched if w['data']),
                      default=0)
        max_pan = max(0, max_pts - self._graph_zoom_pts)
        self._graph_scroll.blockSignals(True)
        self._graph_scroll.setRange(0, max_pan)
        # 스크롤바 값: 0=최신(오른쪽), max=최오래된(왼쪽) — 반전 표시
        self._graph_scroll.setValue(max_pan - self._graph_pan_off)
        self._graph_scroll.blockSignals(False)

    def _on_graph_scroll(self, val: int):
        """스크롤바 이동 → pan_off 갱신."""
        max_pan = self._graph_scroll.maximum()
        self._graph_pan_off = max(0, max_pan - val)
        if self._watched: self._draw_graph()

    def _update_graph(self):
        """
        50ms 타이머 — sig_cache 기반 그래프 업데이트.

        [문제2/3 근본 수정]
        _ev_ptr(인덱스 기반)는 ev_list가 del 삭제될 때 오염되어
        신규 이벤트를 영원히 못 읽는 버그가 있었다.

        → 타임스탬프 기반 커서(_ev_ts_cursor)로 교체:
          "마지막으로 처리한 ts 이후의 이벤트만 읽는다."
          리스트 앞부분이 삭제되어도 ts는 절대값이므로 오염 없음.
        """
        if not self._watched: return
        changed = False

        for w in self._watched:
            sig_name = w['name']

            # 진단 로그 1회
            diag_entry = self._can.sig_cache.get(f"_DIAG_{sig_name}")
            if diag_entry and not w.get('_diag_logged'):
                self._log(f"[Graph] ✅ {sig_name}: {diag_entry[1]}")
                w['_diag_logged'] = True
            err_entry = self._can.sig_cache.get(f"_POLL_ERR_{sig_name}")
            if err_entry and not w.get('_err_logged'):
                self._log(f"[Graph] ❌ {sig_name}: {err_entry[1]}")
                w['_err_logged'] = True

            # ── 타임스탬프 커서 방식으로 신규 이벤트 추출 ────────────────
            ev_list = self._can.sig_cache.get(f"_EV_{sig_name}")
            if isinstance(ev_list, list) and ev_list:
                # 마지막으로 처리한 타임스탬프 (초기값: 신호 추가 시점)
                last_ts = w.get('_ev_ts_cursor', 0.0)
                # last_ts 이후 항목만 추출 (ev_list는 시간순 정렬)
                new_events = [(ts, v) for ts, v in ev_list if ts > last_ts]
                if new_events:
                    w.pop('_err_logged', None)
                    for ts, val in new_events:
                        if val is not None:
                            w['data'].append(val)
                            w['labels'].append(ts)
                    # 커서를 가장 최신 ts로 전진
                    w['_ev_ts_cursor'] = new_events[-1][0]
                    # MAX_PTS 초과 정리
                    if len(w['data']) > self._MAX_PTS:
                        excess = len(w['data']) - self._MAX_PTS
                        del w['data'][:excess]
                        del w['labels'][:excess]
                    changed = True
            else:
                # ev_list 없으면 sig_cache 직접 폴백
                entry = self._can.sig_cache.get(sig_name)
                if entry:
                    ts, val = entry
                    if val is not None:
                        last_ts = w.get('_ev_ts_cursor', 0.0)
                        if ts > last_ts:
                            w.pop('_err_logged', None)
                            w['data'].append(val)
                            w['labels'].append(ts)
                            w['_ev_ts_cursor'] = ts
                            if len(w['data']) > self._MAX_PTS:
                                w['data'].pop(0); w['labels'].pop(0)
                            changed = True

        if changed:
            self._update_chip_text()
        # [문제1] 확대/축소 후 현재값 미반영 수정:
        # changed 여부와 관계없이 일시정지가 아니면 항상 그래프 갱신
        if not self._graph_paused and self._watched:
            self._draw_graph()


    def _draw_graph(self):
        """
        CAN 신호 스캐터 그래프 (점 방식).
        - 각 폴링 샘플을 점으로 표시 (실선 없음)
        - Y축: 1단위 눈금, 기본 10칸, Ctrl+휠로 1/100배까지 확대
        - X축: 0.1초 단위, 휠로 1/100배까지 축소
        - 점 간 시간 표시 (최근 2점)
        """
        from PyQt5.QtGui import QPainter, QPen, QColor, QFont, QPixmap, QBrush
        gw = self._graph_w.width(); gh = self._graph_w.height()
        if gw <= 20 or gh <= 20: return

        pix = QPixmap(gw, gh); pix.fill(QColor("#03030a"))
        p = QPainter(pix); p.setRenderHint(QPainter.Antialiasing)
        PL, PR, PT, PB = 60, 40, 24, 36  # PR 확장: 오른쪽 레이블 잘림 방지
        gw2 = gw - PL - PR; gh2 = gh - PT - PB

        # ── 표시 범위 ────────────────────────────────────────────────────────
        zoom_pts = max(1, min(getattr(self, '_graph_zoom_pts', 600),
                              self._MAX_PTS))
        pan_off  = max(0, getattr(self, '_graph_pan_off', 0))
        y_range  = max(0.1, getattr(self, '_graph_y_range', 10.0))
        y_center = getattr(self, '_graph_y_center', 0.0)
        paused   = getattr(self, '_graph_paused', False)

        def _slice(w):
            d = w['data']; lbl = w['labels']
            n = len(d)
            if n == 0: return [], []
            end   = n - pan_off
            start = max(0, end - zoom_pts)
            return d[start:end], lbl[start:end]

        # ── Y축 범위 (center ± range/2, 1단위 눈금) ─────────────────────────
        import math
        ymin = y_center - y_range / 2.0
        ymax = y_center + y_range / 2.0

        # nice tick — 1단위 기준
        raw_step = y_range / 10.0
        if raw_step < 0.1:
            tick = 0.1
        elif raw_step < 0.5:
            tick = 0.5
        else:
            mag = 10 ** math.floor(math.log10(max(raw_step, 1e-9)))
            for s in [1, 2, 5, 10, 20, 50, 100]:
                if mag * s >= raw_step:
                    tick = mag * s; break
            else:
                tick = mag * 10

        tick_start = math.ceil(ymin / tick) * tick
        ticks = []
        v = tick_start
        while v <= ymax + tick * 0.01:
            ticks.append(v)
            v = round(v + tick, 10)

        # ── 그리드 ───────────────────────────────────────────────────────────
        p.setPen(QPen(QColor("#0d1828"), 1))
        for tv in ticks:
            if ymin <= tv <= ymax:
                yp = PT + int(gh2 * (1 - (tv - ymin) / max(ymax - ymin, 1e-9)))
                p.drawLine(PL, yp, PL + gw2, yp)
        # 수직 그리드 (X축)
        n_vgrid = min(10, max(2, zoom_pts // 50))
        for i in range(n_vgrid + 1):
            xp = PL + int(gw2 * i / n_vgrid)
            p.drawLine(xp, PT, xp, PT + gh2)

        # ── Y축 레이블 ───────────────────────────────────────────────────────
        p.setFont(QFont("Consolas", 8))
        for tv in ticks:
            if ymin - tick * 0.1 <= tv <= ymax + tick * 0.1:
                yp = PT + int(gh2 * (1 - (tv - ymin) / max(ymax - ymin, 1e-9)))
                p.setPen(QPen(QColor("#7bc8e0"), 1))
                label = str(int(round(tv))) if abs(tv - round(tv)) < 1e-6                         else f"{tv:.2g}"
                p.drawText(0, yp - 8, PL - 4, 16,
                           0x0002 | 0x0080, label)  # AlignRight|AlignVCenter

        # ── 0 기준선 강조 ────────────────────────────────────────────────────
        if ymin <= 0 <= ymax:
            y0p = PT + int(gh2 * (1 - (0 - ymin) / max(ymax - ymin, 1e-9)))
            p.setPen(QPen(QColor("#2a4a6a"), 1))
            p.drawLine(PL, y0p, PL + gw2, y0p)

        # ── 신호 점 그리기 ───────────────────────────────────────────────────
        _DOT_R = 4  # 점 반지름
        for w in self._watched:
            data_s, lbl_s = _slice(w)
            if not data_s: continue
            color = QColor(w['color'])
            p.setBrush(QBrush(color))
            p.setPen(QPen(color, 1))
            n = len(data_s)
            prev_pt = None
            for i, v in enumerate(data_s):
                if v is None: continue
                # Y 범위 밖이면 스킵 (클리핑)
                if v < ymin - (ymax - ymin) * 0.05: continue
                if v > ymax + (ymax - ymin) * 0.05: continue
                xp = PL + int(gw2 * i / max(n - 1, 1))
                yp = PT + int(gh2 * (1 - (v - ymin) / max(ymax - ymin, 1e-9)))
                yp = max(PT, min(PT + gh2, yp))
                # 점 그리기
                p.drawEllipse(xp - _DOT_R, yp - _DOT_R,
                               _DOT_R * 2, _DOT_R * 2)
                # ★ 최근 2점 간 시간 차이 표시
                if prev_pt is not None and i == n - 1:
                    pi, px, pv, pt_prev = prev_pt
                    pt_curr = lbl_s[i] if i < len(lbl_s) else 0
                    if pt_prev and pt_curr:
                        dt_ms = (pt_curr - pt_prev) * 1000
                        p.setPen(QPen(QColor("#556"), 1))
                        p.setFont(QFont("Consolas", 7))
                        p.drawText(xp - 25, yp - 14, 60, 12,
                                   0x0004, f"Δ{dt_ms:.0f}ms")
                        p.setFont(QFont("Consolas", 8))
                        p.setPen(QPen(color, 1))
                prev_pt = (i, xp, v,
                           lbl_s[i] if i < len(lbl_s) else None)

        # ── 범례 ─────────────────────────────────────────────────────────────
        fy = PT + 4
        for w in self._watched:
            data_s, _ = _slice(w)
            p.setPen(QPen(QColor(w['color']), 2))
            p.setBrush(QBrush(QColor(w['color'])))
            p.drawEllipse(PL + 8, fy + 3, 8, 8)
            p.setPen(QPen(QColor("#ccd"), 1))
            us = f" ({w['unit']})" if w['unit'] else ""
            last = data_s[-1] if data_s else None
            if last is not None:
                vs = str(int(round(last))) if abs(last - round(last)) < 1e-6                      else f"{last:.2g}"
            else:
                vs = "--"
            p.drawText(PL + 22, fy, 260, 14,
                       0x0001, f"{w['name'][:22]}{us} = {vs}")
            fy += 15

        # ── X축 시간 레이블 (0.1초 단위) ────────────────────────────────────
        all_ts = []
        for w in self._watched:
            _, lbl_s = _slice(w)
            all_ts.extend(lbl_s)
        if len(all_ts) >= 2:
            t0, t1 = min(all_ts), max(all_ts)
            dur = t1 - t0
            t_now = max(all_ts)  # 가장 최신 시각
            for i in range(n_vgrid + 1):
                frac = i / n_vgrid
                tv = t0 + dur * frac
                xp = PL + int(gw2 * frac)
                from datetime import datetime as _dt
                # ★ 오른쪽 끝 = 현재, 왼쪽 = 과거
                # X축 레이블: 현재로부터 몇 초 전인지 표시
                offset = t_now - tv   # 현재로부터 몇 초 전
                if offset < 1.0:
                    ts_s = _dt.fromtimestamp(tv).strftime("%H:%M:%S.") + f"{_dt.fromtimestamp(tv).microsecond // 100000}"
                else:
                    ts_s = f"-{offset:.1f}s"
                p.setPen(QPen(QColor("#6699aa"), 1))
                p.setFont(QFont("Consolas", 7))
                if i == n_vgrid:
                    p.drawText(xp - 55, PT + gh2 + 4, 55, 16, 0x0001, ts_s)
                else:
                    p.drawText(xp - 28, PT + gh2 + 4, 56, 16, 0x0004, ts_s)

        # ── 상태 표시 ────────────────────────────────────────────────────────
        zoom_sec = zoom_pts * 0.1
        y_r = y_range
        info = f"X:{zoom_sec:.1f}s  Y:±{y_r/2:.1f}"
        if pan_off > 0: info += f"  ←{pan_off*0.1:.1f}s"
        if paused: info += "  ⏸"
        p.setPen(QPen(QColor("#445"), 1))
        p.setFont(QFont("Consolas", 7))
        p.drawText(PL, PT - 2, gw2, 12, 0x0002, info)

        # ── 구간 시간 측정 오버레이 (measure_mode ON일 때만) ────────────────
        # [문제2] measure_mode가 ON이고 두 지점이 설정된 경우에만 표시
        if (self._measure_mode and
                self._drag_start_px and
                abs(self._drag_cur_px - self._drag_start_px) > 3):
            x1  = self._drag_start_px
            x2  = self._drag_cur_px
            ts1 = self._px_to_time(x1)
            ts2 = self._px_to_time(x2)
            dt  = abs(ts2 - ts1)
            rx  = min(x1, x2); rw = max(2, abs(x2 - x1))
            # 구간 하이라이트 영역
            p.setPen(QPen(QColor("#2a6a2a"), 1))
            p.setBrush(QColor(40, 120, 40, 35))
            p.drawRect(rx, PT, rw, gh2)
            # 양쪽 경계선
            p.setPen(QPen(QColor("#ffdd88"), 1))
            p.drawLine(x1, PT, x1, PT + gh2)
            p.drawLine(x2, PT, x2, PT + gh2)
            # 시간 텍스트 박스
            mid_x = (x1 + x2) // 2
            dt_str = (f"Δ{dt:.3f}s" if dt >= 1.0
                      else f"Δ{dt*1000:.1f}ms" if dt >= 0.001
                      else f"Δ{dt*1000:.3f}ms")
            box_w, box_h = 90, 24
            bx = max(PL, min(mid_x - box_w//2, PL + gw2 - box_w))
            by = PT + gh2//2 - box_h//2
            p.setPen(QPen(QColor(0, 0, 0, 0), 0))
            p.setBrush(QColor(0, 0, 0, 190))
            p.drawRoundedRect(bx, by, box_w, box_h, 5, 5)
            p.setPen(QPen(QColor("#ffdd88"), 1))
            p.setFont(QFont("Consolas", 10))
            p.drawText(bx, by, box_w, box_h, 0x0004, dt_str)
            # 시작/끝 타임스탬프
            from datetime import datetime as _dt2
            p.setFont(QFont("Consolas", 7))
            p.setPen(QPen(QColor("#aaa"), 1))
            ts1_str = _dt2.fromtimestamp(ts1).strftime("%H:%M:%S.%f")[:12]
            ts2_str = _dt2.fromtimestamp(ts2).strftime("%H:%M:%S.%f")[:12]
            p.drawText(x1 - 2, PT + gh2 + 4, 80, 14, 0x0001, ts1_str)
            p.drawText(x2 - 78, PT + gh2 + 4, 80, 14, 0x0002, ts2_str)

        p.end()
        self._show_pix(pix)


    def _show_pix(self, pix):
        self._graph_hint.setVisible(False)
        self._chart_lbl.setPixmap(pix)
        self._chart_lbl.setGeometry(
            0, 0, self._graph_w.width(), self._graph_w.height())
        self._chart_lbl.setVisible(True)

    def _on_graph_wheel(self, event):
        """
        휠 이벤트:
          - 기본 휠         → X축 시간 범위 확대/축소
          - Ctrl + 휠       → Y축 배율 확대/축소 (0~255 신호 대응)
        """
        from PyQt5.QtCore import Qt as _Qt
        delta = event.angleDelta().y()
        ctrl  = event.modifiers() & _Qt.ControlModifier

        if ctrl:
            # ★ Ctrl+휠 → Y축 범위 (1/100배까지 축소)
            factor = 0.8 if delta > 0 else 1.25
            self._graph_y_range = max(0.1, min(10000.0,
                getattr(self, '_graph_y_range', 10.0) * factor))
            # 하위 호환
            self._graph_y_zoom = max(0.1, min(100.0,
                                    self._graph_y_zoom * factor))
        else:
            # 기본 휠 → X축 시간 범위 (마우스 위치 기준)
            old_zoom = self._graph_zoom_pts
            step = max(1, int(old_zoom * 0.2))
            new_zoom = (max(1, old_zoom - step) if delta > 0
                        else min(self._MAX_PTS, old_zoom + step))
            # ★ 마우스 위치 기준 pan 조정
            _PL2, _PR2 = 60, 40
            gw2_w = max(1, self._graph_w.width() - _PL2 - _PR2)
            mouse_frac = max(0.0, min(1.0, (event.x() - _PL2) / gw2_w))
            mouse_pt = self._graph_pan_off + mouse_frac * old_zoom
            new_pan = mouse_pt - mouse_frac * new_zoom
            max_pts = max((len(w['data']) for w in self._watched if w['data']),
                          default=0)
            self._graph_zoom_pts = new_zoom
            self._graph_pan_off  = max(0, min(int(new_pan),
                                              max(0, max_pts - new_zoom)))

        if self._watched:
            self._draw_graph()
        event.accept()

    def _px_to_time(self, px: int) -> float:
        """
        그래프 픽셀 X → 타임스탬프(초).
        [문제2] pan_off를 반영하여 현재 화면에 보이는 데이터 구간의 시각 반환.
        """
        PL2, PR2 = 60, 40
        gw2 = max(1, self._graph_w.width() - PL2 - PR2)
        frac = max(0.0, min(1.0, (px - PL2) / gw2))
        # 현재 보이는 슬라이스의 타임스탬프 기반
        slice_ts = []
        for w in self._watched:
            lbl = w.get("labels", [])
            n = len(lbl)
            if n == 0:
                continue
            pan = self._graph_pan_off
            zoom = self._graph_zoom_pts
            end   = n - pan
            start = max(0, end - zoom)
            slice_ts.extend(lbl[start:end])
        if len(slice_ts) < 2:
            # fallback: 전체 범위
            all_ts = [ts for w in self._watched for ts in w.get("labels", [])]
            if len(all_ts) < 2:
                return 0.0
            t0, t1 = min(all_ts), max(all_ts)
        else:
            t0, t1 = min(slice_ts), max(slice_ts)
        return t0 + frac * (t1 - t0)

    def _on_measure_mode_toggled(self, on: bool):
        """[문제2] 구간 측정 모드 ON/OFF."""
        self._measure_mode = on
        self._drag_measuring = False
        self._drag_start_px  = 0
        self._drag_cur_px    = 0
        self._measure_btn.setText("📏 구간 측정 ON" if on else "📏 구간 측정 OFF")
        # 커서 변경
        from PyQt5.QtCore import Qt as _Qt
        self._graph_w.setCursor(
            _Qt.CrossCursor if on else _Qt.OpenHandCursor)
        if self._watched:
            self._draw_graph()

    def _on_graph_mouse_press(self, event):
        """[문제2] 좌클릭 시작 — measure 모드면 측정 시작, 아니면 pan 시작."""
        from PyQt5.QtCore import Qt as _Qt
        if event.button() != _Qt.LeftButton:
            return
        if self._measure_mode:
            # 구간 측정 시작
            self._drag_measuring = True
            self._drag_start_px  = event.x()
            self._drag_cur_px    = event.x()
            self._drag_start_ts  = self._px_to_time(event.x())
        else:
            # pan 모드 시작
            self._drag_measuring = False
            self._graph_drag_x   = event.x()
            self._graph_drag_y   = event.y()
            self._graph_w.setCursor(_Qt.ClosedHandCursor)

    def _on_graph_mouse_move(self, event):
        """[문제2] 드래그 이동 — measure 모드면 측정 끝점 갱신, 아니면 pan."""
        from PyQt5.QtCore import Qt as _Qt
        if not (event.buttons() & _Qt.LeftButton):
            return

        if self._measure_mode:
            # 측정 모드: 끝점만 업데이트, pan 없음
            self._drag_cur_px = event.x()
        else:
            # pan 모드: X/Y 이동
            dx = event.x() - self._graph_drag_x
            dy = event.y() - getattr(self, '_graph_drag_y', event.y())
            self._graph_drag_x = event.x()
            self._graph_drag_y = event.y()

            # X pan (시간축) — 오른쪽 드래그=과거, 왼쪽 드래그=최신
            gw2 = max(1, self._graph_w.width() - 60 - 40)
            pts_per_px = max(0.01, self._graph_zoom_pts / gw2)
            self._graph_pan_off = max(0,
                self._graph_pan_off - int(dx * pts_per_px))
            max_pts = max((len(w['data']) for w in self._watched if w['data']),
                          default=0)
            self._graph_pan_off = min(self._graph_pan_off,
                                      max(0, max_pts - self._graph_zoom_pts))

            # Y pan (값 중심 이동)
            gh2 = max(1, self._graph_w.height() - 24 - 36)
            y_range = getattr(self, '_graph_y_range', 10.0)
            val_per_px = y_range / gh2
            self._graph_y_center = (
                getattr(self, '_graph_y_center', 0.0) + dy * val_per_px)

        if self._watched:
            self._draw_graph()

    def _on_graph_mouse_release(self, event):
        """[문제2] 드래그 종료."""
        from PyQt5.QtCore import Qt as _Qt
        if self._measure_mode:
            # 측정 모드: 끝점 확정 (오버레이 유지)
            self._drag_cur_px = event.x()
        else:
            # pan 모드 종료
            self._graph_w.setCursor(_Qt.OpenHandCursor)
        # 측정 드래그 플래그 해제 (오버레이는 measure_mode ON이면 계속 표시)
        self._drag_measuring = False

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._watched: self._draw_graph()

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._can_log.appendPlainText(f"[{ts}]  {msg}")
        self._can_log.verticalScrollBar().setValue(
            self._can_log.verticalScrollBar().maximum())

    def closeEvent(self, e):
        self._graph_timer.stop()
        CanMonitorDialog._instance = None
        e.accept()


