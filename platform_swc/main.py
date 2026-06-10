# -*- coding: utf-8 -*-
"""
main.py  —  Screen & Camera Recorder
Layer 4: MainWindow + main()

DI 조립 순서 (§2.5):
  Signals → SettingsDB → IoChannelDB → CoreEngine
  → PowerController → KernelEngine
  → CanBusManager → (패널 생성 시 주입)

종료 해제 순서 (§1.7):
  UI 패널 → KernelEngine.stop() → CanBusManager.disconnect()
  → CoreEngine.stop() → PowerController.disconnect()
  → SettingsDB 최종저장
"""
import os, sys, json, threading, time, platform, queue, re
from datetime import datetime

# ── 필수 의존성 검증 (ImportError 시 명확한 안내 후 종료) ────────────────────
_MISSING = []
try:
    import cv2
    try: cv2.setLogLevel(0)
    except Exception: pass
except ImportError:
    _MISSING.append("opencv-python")
try:
    import numpy as np
except ImportError:
    _MISSING.append("numpy"); np = None
try:
    import mss as _mss_lib
except ImportError:
    _MISSING.append("mss"); _mss_lib = None

if _MISSING:
    _msg = ("필수 라이브러리가 설치되지 않았습니다:\n\n"
            + "\n".join(f"  pip install {p}" for p in _MISSING)
            + "\n\n위 명령어를 실행한 후 다시 시작하세요.")
    try:
        import tkinter as _tk, tkinter.messagebox as _mb
        _r = _tk.Tk(); _r.withdraw()
        _mb.showerror("Screen & Camera Recorder — 시작 실패", _msg)
        _r.destroy()
    except Exception:
        print(_msg)
    sys.exit(1)

# ── 선택 의존성 ───────────────────────────────────────────────────────────────
try:
    from pynput import keyboard as pynput_keyboard, mouse as pynput_mouse
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False

try:
    import fastapi, uvicorn; FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

# ── Qt ────────────────────────────────────────────────────────────────────────
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QGroupBox, QCheckBox, QDoubleSpinBox,
    QScrollArea, QScrollBar, QFrame, QGridLayout, QTextEdit, QSizePolicy,
    QDialog, QDateTimeEdit, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QPlainTextEdit, QAbstractItemView, QSpinBox, QSlider,
    QTabWidget, QComboBox, QLineEdit, QSplitter, QInputDialog,
    QButtonGroup, QRadioButton, QTextBrowser,
)
from PyQt5.QtCore import (Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint,
                           QRect, QDateTime, Q_ARG)
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor

# ── SWC 임포트 ────────────────────────────────────────────────────────────────
from common.constants import BASE_DIR, DB_PATH, IO_DB_PATH, _DARK_QSS, _SECTION_DEFS
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, _get_font, PIL_AVAILABLE, resource_path

from engine_swc.settings_db import SettingsDB, IoChannelDB
from engine_swc.core_engine import CoreEngine

from kernel_swc.power_controller import PowerController
from kernel_swc.kernel_engine import KernelEngine

from can_swc.can_bus_manager import CanBusManager
from can_swc.ai_chat_controller import AIChatController
from can_swc.can_monitor_dialog import CanMonitorDialog
from can_swc.llm_chat_dialog import LLMChatDialog
from can_swc.api_doc_dialog import ApiDocDialog

from ui_swc.widgets import ClickLabel, CollapsibleSection, MemoOverlayRow, _FeatDragList, _make_spinbox
from ui_swc.preview_window import PreviewLabel, CameraWindow, DisplayWindow
from ui_swc.recording_panel import RecordingPanel, show_tc_dialog
from ui_swc.manual_clip_panel import ManualClipPanel
from ui_swc.blackout_panel import BlackoutPanel
from ui_swc.path_settings_panel import PathSettingsPanel
from ui_swc.roi_manager_panel import RoiManagerPanel, RoiEditDialog
from ui_swc.autoclick_panel import AutoClickPanel
from ui_swc.macro_panel import MacroPanel
from ui_swc.schedule_panel import SchedulePanel
from ui_swc.memo_panel import MemoPanel, TimestampMemoEdit
from ui_swc.kernel_panel import KernelPanel, KernelDialog, _DummyKernel, _DummyEngine
from ui_swc.power_panel import PowerControlPanel, BLTNStatusPanel, _LedPwmChart
from ui_swc.timer_panel import TimerPanel
from ui_swc.status_panel import ResetPanel, LogPanel

# ── 전역 CAN 매니저 (하위 호환 — 향후 DI로 완전 교체 예정) ─────────────────
# [TODO §2.5] _can_manager는 MainWindow 생성 후 DI 주입으로 교체할 것
_can_manager = CanBusManager(None)   # Signals는 MainWindow에서 set_signals()로 주입

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Screen & Camera Recorder  v5.21")
        self.resize(1400, 900)
        self.setStyleSheet(_DARK_QSS)

        # ── 코어 ──────────────────────────────────────────────────────────
        self._signals = Signals()
        self._engine  = CoreEngine(self._signals)
        self._db      = SettingsDB()
        self._kernel  = KernelEngine(self._engine, self._signals)  # ★ v2.9
        # ★ 전원 제어기 — KernelEngine과 PowerControlPanel이 공유
        self._power_ctrl = PowerController()
        self._kernel.power = self._power_ctrl

        # ── 미리보기 창 (ROI 관리 연동은 ROI 패널 생성 후 연결) ──────────
        self._cam_win  = CameraWindow(self._engine)
        self._disp_win = DisplayWindow(self._engine)

        # ── UI 빌드 ───────────────────────────────────────────────────────
        self._build_ui()
        self._connect_signals()
        self._start_timers()
        self._load_settings()

        # ★ 전역 1초 디바운스 자동저장 타이머
        self._global_autosave_timer = QTimer(self)
        self._global_autosave_timer.setSingleShot(True)
        self._global_autosave_timer.setInterval(1000)
        self._global_autosave_timer.timeout.connect(self._save_settings)

        # ★ 3초마다 설정 변경 감지 + 자동저장 (패널 훅 없이도 동작)
        self._settings_snapshot = {}   # 마지막 저장 상태 스냅샷
        self._settings_check_timer = QTimer(self)
        self._settings_check_timer.setInterval(3000)
        self._settings_check_timer.timeout.connect(self._check_and_save_settings)
        self._settings_check_timer.start()
        self._gc_timer = QTimer(self)
        self._gc_timer.setInterval(6 * 3600 * 1000)
        self._gc_timer.timeout.connect(self._periodic_gc)
        self._gc_timer.start()

        # ★ 장기 실행 안정화 — 6시간마다 GC 강제 수행
        self._gc_timer = QTimer(self)
        self._gc_timer.setInterval(6 * 3600 * 1000)  # 6시간
        self._gc_timer.timeout.connect(self._periodic_gc)
        self._gc_timer.start()

        # ── 엔진 시작 ─────────────────────────────────────────────────────
        self._engine.start()

        # ★ v5.6 수정: UI 완전 빌드 후 300ms 뒤 재스캔
        # engine.start()의 scan 스레드가 _connect_signals() 이전에 emit해버리는
        # 타이밍 문제를 방어 — 시그널 연결 완료 후 다시 스캔해서 콤보박스 채움
        QTimer.singleShot(300, self._scan_devices)


        # [추가1] 타이머 패널에 PowerController 후 주입 (패널 빌드 이후에만 가능)
        if hasattr(self, '_timer_panel') and self._timer_panel is not None:
            self._timer_panel.set_power(self._power_ctrl)


        # [절대조건 12] 프로그램 시작 시 AI 자기 파악 파일 생성/읽기
        threading.Thread(
            target=self._init_ai_self_knowledge,
            daemon=True, name="AISelfKnowledge").start()

    # ── UI 빌드 ──────────────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget(); self.setCentralWidget(central)
        root_h  = QHBoxLayout(central)
        root_h.setContentsMargins(6,6,6,6); root_h.setSpacing(6)

        # ════════════════════════════════════════════════════════════════
        #  좌측: 기능 패널 (세로 스크롤 + 섹션 토글 + 순서 이동)
        # ════════════════════════════════════════════════════════════════
        left_w = QWidget()
        left_v = QVBoxLayout(left_w)
        left_v.setContentsMargins(0,0,0,0); left_v.setSpacing(4)

        # ── 기능 제어 툴바 ───────────────────────────────────────────────
        tool_bar = QFrame()
        tool_bar.setStyleSheet(
            "QFrame{background:#0a0a1e;border-bottom:1px solid #1a2a3a;}")
        tool_bar.setFixedHeight(38)
        tb_lay = QHBoxLayout(tool_bar)
        tb_lay.setContentsMargins(6,4,6,4); tb_lay.setSpacing(4)

        tb_lbl = QLabel("⚙ 기능 패널")
        tb_lbl.setStyleSheet(
            "color:#7ab4d4;font-size:11px;font-weight:bold;background:transparent;")
        tb_lay.addWidget(tb_lbl)

        tb_hint = QLabel("⠿ 드래그로 순서변경  |  ☑ 체크로 ON/OFF")
        tb_hint.setStyleSheet("color:#334;font-size:9px;background:transparent;")
        tb_lay.addWidget(tb_hint)

        # 전체 펼치기/접기
        expand_btn = QPushButton("⊞")
        expand_btn.setFixedSize(26, 26)
        expand_btn.setToolTip("전체 펼치기")
        expand_btn.setStyleSheet(
            "QPushButton{background:#1a1a3a;color:#9ab;"
            "border:1px solid #2a2a4a;border-radius:3px;font-size:13px;}"
            "QPushButton:hover{background:#22224a;}")
        expand_btn.clicked.connect(lambda: self._set_all_collapsed(False))

        collapse_btn = QPushButton("⊟")
        collapse_btn.setFixedSize(26, 26)
        collapse_btn.setToolTip("전체 접기")
        collapse_btn.setStyleSheet(expand_btn.styleSheet())
        collapse_btn.clicked.connect(lambda: self._set_all_collapsed(True))

        api_btn2 = QPushButton("📖 API")
        api_btn2.setFixedHeight(26)
        api_btn2.setStyleSheet(
            "QPushButton{background:#1a1a2a;color:#f0c040;"
            "border:1px solid #4a4a20;border-radius:3px;font-size:10px;padding:0 6px;}"
            "QPushButton:hover{background:#22221a;}")
        api_btn2.clicked.connect(self._show_api_doc)

        tb_lay.addStretch()
        tb_lay.addWidget(expand_btn)
        tb_lay.addWidget(collapse_btn)
        tb_lay.addWidget(api_btn2)
        left_v.addWidget(tool_bar)

        # ── 기능 순서 리스트 (드래그앤드랍 + 체크박스 ON/OFF) ────────────
        self._feat_list = _FeatDragList(self)
        self._feat_list.order_changed.connect(self._on_feat_order_changed)
        self._feat_list.visibility_changed.connect(self._on_feat_visibility_changed)
        self._feat_list.scroll_to.connect(self._select_and_scroll)

        feat_scroll = QScrollArea()
        feat_scroll.setWidgetResizable(True)
        feat_scroll.setFixedHeight(140)
        feat_scroll.setStyleSheet(
            "QScrollArea{border:1px solid #1a2a3a;background:#080818;}"
            "QScrollBar:vertical{background:#080818;width:5px;}"
            "QScrollBar::handle:vertical{background:#2a3a5a;border-radius:2px;}")
        feat_scroll.setWidget(self._feat_list)
        left_v.addWidget(feat_scroll)

        self._section_order: List[str] = [d[0] for d in _SECTION_DEFS]
        self._section_visible: dict = {d[0]: True for d in _SECTION_DEFS}
        self._feat_rows: dict = {}
        self._selected_feat: str = ""

        # ── 기능 섹션 스크롤 영역 ────────────────────────────────────────
        self._sect_scroll = QScrollArea()
        self._sect_scroll.setWidgetResizable(True)
        self._sect_scroll.setStyleSheet(
            "QScrollArea{border:none;background:#0d0d1e;}"
            "QScrollBar:vertical{background:#080812;width:7px;}"
            "QScrollBar::handle:vertical{background:#2a3a5a;border-radius:3px;min-height:20px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        self._sect_w = QWidget()
        self._sect_w.setStyleSheet("background:#0d0d1e;")
        self._sect_lay = QVBoxLayout(self._sect_w)
        self._sect_lay.setContentsMargins(6,6,6,12); self._sect_lay.setSpacing(6)
        self._sect_scroll.setWidget(self._sect_w)
        left_v.addWidget(self._sect_scroll, 1)

        # ── 패널 + 섹션 생성 ─────────────────────────────────────────────
        self._panels    = {}
        self._sections  = {}
        self._roi_mgr   = None

        for key, label, color in _SECTION_DEFS:
            panel = self._make_panel(key)
            self._panels[key] = panel

            sec = CollapsibleSection(label, color)
            sec.add_widget(panel)
            self._sections[key] = sec
            self._sect_lay.addWidget(sec)

        self._sect_lay.addStretch()

        # 기능 리스트 초기 빌드
        self._rebuild_feat_list()

        # ════════════════════════════════════════════════════════════════
        #  우측: 미리보기 + 상태 (QWidget으로 감싸서 Splitter에 추가)
        # ════════════════════════════════════════════════════════════════
        right_w = QWidget()
        right_v = QVBoxLayout(right_w); right_v.setSpacing(4)
        right_v.setContentsMargins(0,0,0,0)

        # ── 버튼 행 ──────────────────────────────────────────────────────
        # ★ v2.9.3: 미리보기 관련 모든 ON/OFF 독립 제어
        #
        #  행1: [🖥 인라인 Display] [📷 인라인 Camera] │ [🔄 스캔] [📖 API]
        #  행2: [🖥 플로팅 창 Display] [📷 플로팅 창 Camera]
        #  행3: [🖥 영상처리 Display ▶ ON] [📷 영상처리 Camera ▶ ON]

        _btn_ss_disp = (
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:4px;font-size:10px;font-weight:bold;}"
            "QPushButton:checked{background:#0d3a5a;border-color:#3a7aaa;}"
            "QPushButton:!checked{background:#0a0a18;color:#334;border-color:#1a1a2a;}"
            "QPushButton:hover{background:#22334a;}")
        _btn_ss_cam = (
            "QPushButton{background:#1a2a1a;color:#f0c040;"
            "border:1px solid #4a4a20;border-radius:4px;font-size:10px;font-weight:bold;}"
            "QPushButton:checked{background:#0d2a0d;border-color:#7a7a30;}"
            "QPushButton:!checked{background:#0a0a0a;color:#334;border-color:#1a1a1a;}"
            "QPushButton:hover{background:#22331a;}")
        _btn_ss_util = (
            "QPushButton{background:#1a1a2a;color:#9ab;"
            "border:1px solid #2a2a4a;border-radius:4px;font-size:10px;}"
            "QPushButton:hover{background:#22224a;}")

        btn_row1 = QHBoxLayout(); btn_row1.setSpacing(4)
        btn_row2 = QHBoxLayout(); btn_row2.setSpacing(4)
        btn_row3 = QHBoxLayout(); btn_row3.setSpacing(4)

        # ── 행1: 인라인 미리보기 ON/OFF ─────────────────────────────────
        self._scr_inline_btn = QPushButton("🖥 인라인 Display ▶ ON")
        self._scr_inline_btn.setCheckable(True)
        self._scr_inline_btn.setChecked(True)
        self._scr_inline_btn.setFixedHeight(26)
        self._scr_inline_btn.setStyleSheet(_btn_ss_disp)
        self._scr_inline_btn.toggled.connect(self._on_scr_inline_toggle)

        self._cam_inline_btn = QPushButton("📷 인라인 Camera ▶ ON")
        self._cam_inline_btn.setCheckable(True)
        self._cam_inline_btn.setChecked(True)
        self._cam_inline_btn.setFixedHeight(26)
        self._cam_inline_btn.setStyleSheet(_btn_ss_cam)
        self._cam_inline_btn.toggled.connect(self._on_cam_inline_toggle)

        scan_btn = QPushButton("🔄 스캔")
        scan_btn.setFixedHeight(26)
        scan_btn.setStyleSheet(_btn_ss_util)
        scan_btn.clicked.connect(self._scan_devices)

        api_btn = QPushButton("📖 API")
        api_btn.setFixedHeight(26)
        api_btn.setStyleSheet(
            "QPushButton{background:#1a1a2a;color:#f0c040;"
            "border:1px solid #4a4a20;border-radius:4px;font-size:10px;}"
            "QPushButton:hover{background:#22221a;}")
        api_btn.clicked.connect(self._show_api_doc)

        llm_btn = QPushButton("AI")
        llm_btn.setFixedHeight(26)
        llm_btn.setStyleSheet(
            "QPushButton{background:#1a0a2e;color:#c080ff;"
            "border:1px solid #5a2a8a;border-radius:4px;"
            "font-size:10px;font-weight:bold;}"
            "QPushButton:hover{background:#2a1a4a;}")
        llm_btn.setToolTip("AI Chat — Groq(무료)/Gemini/Claude")
        llm_btn.clicked.connect(self._show_llm_chat)
        # 📡 CAN Monitor 버튼
        can_mon_btn = QPushButton("📡 CAN")
        can_mon_btn.setFixedHeight(26)
        can_mon_btn.setStyleSheet(
            "QPushButton{background:#0a1a2a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:4px;"
            "font-size:10px;font-weight:bold;}"
            "QPushButton:hover{background:#12223a;}")
        can_mon_btn.setToolTip(
            "CAN Monitor — DBC 신호 확인 / CANoe COM 연결\n"
            "커널 스크립트: can.can_read() / canoe_write()")
        can_mon_btn.clicked.connect(self._show_can_monitor)

        # ★ v5.6: 커널 스크립트 버튼 — 우측 상단 독립 모달
        kernel_btn = QPushButton("🧠 커널")
        kernel_btn.setFixedHeight(26)
        kernel_btn.setStyleSheet(
            "QPushButton{background:#1a0a2e;color:#e080ff;"
            "border:1px solid #6a2a9a;border-radius:4px;"
            "font-size:10px;font-weight:bold;}"
            "QPushButton:hover{background:#2a1a4a;}")
        kernel_btn.setToolTip("스크립트 커널 — Python 자동화 스크립트 편집/실행")
        kernel_btn.clicked.connect(self._show_kernel_dialog)

        btn_row1.addWidget(self._scr_inline_btn, 1)
        btn_row1.addWidget(self._cam_inline_btn, 1)
        btn_row1.addWidget(scan_btn)
        btn_row1.addWidget(api_btn)
        btn_row1.addWidget(llm_btn)
        btn_row1.addWidget(can_mon_btn)
        btn_row1.addWidget(kernel_btn)

        # ★ CAN 그래프 ON/OFF 버튼
        self._can_graph_btn = QPushButton("📊 CAN그래프 ON")
        self._can_graph_btn.setCheckable(True)
        self._can_graph_btn.setChecked(True)
        self._can_graph_btn.setFixedHeight(26)
        self._can_graph_btn.setStyleSheet(
            "QPushButton{background:#0a1a0a;color:#7fdb9e;"
            "border:1px solid #1a5a2a;border-radius:3px;font-size:10px;}"
            "QPushButton:checked{background:#0f2a0f;color:#affcbf;}"
            "QPushButton:!checked{background:#1a0a0a;color:#f88;"
            "border-color:#5a1a1a;}")
        self._can_graph_btn.toggled.connect(self._on_can_graph_toggle)
        btn_row1.addWidget(self._can_graph_btn)

        # ── 행2: 플로팅 창 ON/OFF ────────────────────────────────────────
        self._disp_btn = QPushButton("🖥 플로팅 Display 창")
        self._disp_btn.setCheckable(True)
        self._disp_btn.setFixedHeight(26)
        self._disp_btn.setStyleSheet(_btn_ss_disp)
        self._disp_btn.toggled.connect(
            lambda c: self._disp_win.show_win() if c else self._disp_win.hide_win())

        self._cam_btn = QPushButton("📷 플로팅 Camera 창")
        self._cam_btn.setCheckable(True)
        self._cam_btn.setFixedHeight(26)
        self._cam_btn.setStyleSheet(_btn_ss_cam)
        self._cam_btn.toggled.connect(
            lambda c: self._cam_win.show_win() if c else self._cam_win.hide_win())

        btn_row2.addWidget(self._disp_btn, 1)
        btn_row2.addWidget(self._cam_btn, 1)

        # ── 행3: 영상처리 스레드 ON/OFF (자원 절약) ──────────────────────
        self._scr_preview_btn = QPushButton("🖥 Display 영상처리 ▶ ON")
        self._scr_preview_btn.setCheckable(True)
        self._scr_preview_btn.setChecked(True)
        self._scr_preview_btn.setFixedHeight(26)
        self._scr_preview_btn.setStyleSheet(_btn_ss_disp)
        self._scr_preview_btn.toggled.connect(self._on_scr_preview_toggle)

        self._cam_preview_btn = QPushButton("📷 Camera 영상처리 ▶ ON")
        self._cam_preview_btn.setCheckable(True)
        self._cam_preview_btn.setChecked(True)
        self._cam_preview_btn.setFixedHeight(26)
        self._cam_preview_btn.setStyleSheet(_btn_ss_cam)
        self._cam_preview_btn.toggled.connect(self._on_cam_preview_toggle)

        btn_row3.addWidget(self._scr_preview_btn, 1)
        btn_row3.addWidget(self._cam_preview_btn, 1)

        right_v.addLayout(btn_row1)
        right_v.addLayout(btn_row2)
        right_v.addLayout(btn_row3)

        # ── 인라인 미리보기 — 각각 독립 ON/OFF 가능 ★ v2.9.3 ────────────
        self._scr_inline = PreviewLabel("screen", self._engine)
        self._scr_inline.setMinimumHeight(140)

        self._cam_inline = PreviewLabel("camera", self._engine)
        self._cam_inline.setMinimumHeight(140)

        # 각각 QWidget으로 감싸서 Splitter에 추가 (hide/show 독립 제어)
        scr_wrap = QWidget()
        scr_wrap_v = QVBoxLayout(scr_wrap)
        scr_wrap_v.setContentsMargins(0,0,0,0); scr_wrap_v.setSpacing(0)
        scr_wrap_v.addWidget(self._scr_inline)
        self._scr_inline_wrap = scr_wrap

        cam_wrap = QWidget()
        cam_wrap_v = QVBoxLayout(cam_wrap)
        cam_wrap_v.setContentsMargins(0,0,0,0); cam_wrap_v.setSpacing(0)
        cam_wrap_v.addWidget(self._cam_inline)
        self._cam_inline_wrap = cam_wrap

        inline_splitter = QSplitter(Qt.Vertical)
        inline_splitter.addWidget(scr_wrap)
        inline_splitter.addWidget(cam_wrap)
        inline_splitter.setSizes([280, 280])
        inline_splitter.setChildrenCollapsible(True)
        right_v.addWidget(inline_splitter, 1)

        # ── 상태바 ───────────────────────────────────────────────────────
        stat_grp = QGroupBox("상태"); sl = QGridLayout(stat_grp); sl.setSpacing(4)
        self._status_lbl = QLabel("대기 중")
        self._status_lbl.setStyleSheet(
            "color:#7bc8e0;font-family:monospace;font-size:10px;")
        self._status_lbl.setWordWrap(True)
        sl.addWidget(self._status_lbl, 0, 0, 1, 4)

        sl.addWidget(QLabel("모니터:"), 1, 0)
        self._mon_cb = QComboBox(); self._mon_cb.setMinimumWidth(150)
        self._mon_cb.currentIndexChanged.connect(self._on_mon_changed)
        sl.addWidget(self._mon_cb, 1, 1)
        sl.addWidget(QLabel("카메라:"), 1, 2)
        self._cam_cb = QComboBox(); self._cam_cb.setMinimumWidth(150)
        self._cam_cb.currentIndexChanged.connect(self._on_cam_changed)
        sl.addWidget(self._cam_cb, 1, 3)

        self._fps_lbl = QLabel("Screen: — fps  |  Camera: — fps")
        self._fps_lbl.setStyleSheet("color:#556;font-size:9px;font-family:monospace;")
        sl.addWidget(self._fps_lbl, 2, 0, 1, 4)

        self._io_lbl = QLabel("IO채널: —")
        self._io_lbl.setStyleSheet("color:#334;font-size:9px;font-family:monospace;")
        sl.addWidget(self._io_lbl, 3, 0, 1, 4)

        right_v.addWidget(stat_grp)

        # ── 좌/우 QSplitter로 연결 (마우스로 비율 조절) ── 수정2
        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.setStyleSheet(
            "QSplitter::handle{background:#1a2a3a;width:5px;}"
            "QSplitter::handle:hover{background:#2a5a9a;}")
        main_splitter.addWidget(left_w)
        main_splitter.addWidget(right_w)
        main_splitter.setSizes([500, 900])   # 초기 비율 (픽셀)
        main_splitter.setChildrenCollapsible(False)
        root_h.addWidget(main_splitter)

    def _rebuild_feat_list(self):
        """_FeatDragList 위젯에 현재 순서/가시성 반영."""
        _labels  = {d[0]: d[1] for d in _SECTION_DEFS}
        _colors  = {d[0]: d[2] for d in _SECTION_DEFS}
        if hasattr(self, '_feat_list') and isinstance(self._feat_list, _FeatDragList):
            self._feat_list.populate(
                self._section_order, _labels, _colors, self._section_visible)

    def _on_feat_order_changed(self, new_order: list):
        """_FeatDragList 드래그앤드랍 → 섹션 위젯 순서 재배치."""
        self._section_order = new_order
        while self._sect_lay.count():
            it = self._sect_lay.takeAt(0)
            if it.widget(): it.widget().setParent(None)
        for k in new_order:
            sec = self._sections.get(k)
            if sec:
                sec.setParent(self._sect_w)
                self._sect_lay.addWidget(sec)
        self._sect_lay.addStretch()

    def _on_feat_visibility_changed(self, key: str, visible: bool):
        """체크박스 ON/OFF → 섹션 표시/숨기기."""
        self._section_visible[key] = visible
        sec = self._sections.get(key)
        if sec: sec.setVisible(visible)

    def _select_and_scroll(self, key: str):
        """기능명 더블클릭 → 해당 섹션으로 스크롤."""
        self._selected_feat = key
        sec = self._sections.get(key)
        if sec:
            if sec.is_collapsed(): sec.set_collapsed(False)
            QTimer.singleShot(50, lambda: self._scroll_to(sec))

    def _scroll_to(self, widget: QWidget):
        pos = widget.mapTo(self._sect_w, QPoint(0,0))
        vsb = self._sect_scroll.verticalScrollBar()
        vsb.setValue(max(0, pos.y() - 10))

    def _set_all_collapsed(self, collapsed: bool):
        for sec in self._sections.values():
            sec.set_collapsed(collapsed)

    # ── 미리보기 ON/OFF (자원 절약) ★ 수정1 복구 ────────────────────────
    # ── 미리보기 ON/OFF 핸들러 6개 독립 제어 ★ v2.9.3 ──────────────────
    def _on_scr_inline_toggle(self, on: bool):
        """인라인 Display 미리보기 표시/숨기기."""
        if hasattr(self, '_scr_inline_wrap'):
            self._scr_inline_wrap.setVisible(on)
        self._scr_inline.set_active(on)
        self._scr_inline_btn.setText(
            "🖥 인라인 Display ▶ ON" if on else "🖥 인라인 Display ⏸ OFF")

    def _on_cam_inline_toggle(self, on: bool):
        """인라인 Camera 미리보기 표시/숨기기."""
        if hasattr(self, '_cam_inline_wrap'):
            self._cam_inline_wrap.setVisible(on)
        self._cam_inline.set_active(on)
        self._cam_inline_btn.setText(
            "📷 인라인 Camera ▶ ON" if on else "📷 인라인 Camera ⏸ OFF")

    def _on_scr_preview_toggle(self, on: bool):
        """Display 영상처리 스레드 ON/OFF (자원 절약)."""
        self._scr_inline.set_active(on)
        self._scr_preview_btn.setText(
            "🖥 Display 영상처리 ▶ ON" if on else "🖥 Display 영상처리 ⏸ OFF")
        if on:
            self._engine.start_screen()
        else:
            self._engine.stop_screen()
        if hasattr(self._disp_win, 'prev'):
            self._disp_win.prev.set_active(on)

    def _on_cam_preview_toggle(self, on: bool):
        """Camera 영상처리 스레드 ON/OFF (자원 절약)."""
        self._cam_inline.set_active(on)
        self._cam_preview_btn.setText(
            "📷 Camera 영상처리 ▶ ON" if on else "📷 Camera 영상처리 ⏸ OFF")
        if on:
            # ★ 카메라 목록이 비어있으면 자동 재스캔 후 시작
            if not self._engine.camera_list:
                def _rescan_and_start():
                    self._engine.scan_cameras()
                    if self._engine.camera_list:
                        self._engine.start_camera()
                    else:
                        self._engine.signals.status_message.emit(
                            "❌ 카메라를 찾지 못했습니다 — USB 연결 확인 후 재탐색하세요")
                threading.Thread(target=_rescan_and_start, daemon=True,
                                 name="CamRescanStart").start()
            else:
                self._engine.start_camera()
        else:
            self._engine.stop_camera()
        if hasattr(self._cam_win, 'prev'):
            self._cam_win.prev.set_active(on)

    def _make_panel(self, key: str) -> QWidget:
        e = self._engine; s = self._signals; db = self._db

        if key == "rec":
            p = RecordingPanel(e, s); return p
        elif key == "manual":
            return ManualClipPanel(e, s)
        elif key == "blackout":
            return BlackoutPanel(e, s)
        elif key == "roi":
            # ★ 추가1: ROI 관리 탭 — RoiManagerPanel(engine, signals) 올바른 호출
            w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(8)

            # RoiManagerPanel은 내부 탭(Display/Camera)으로 둘 다 관리
            self._roi_mgr = RoiManagerPanel(e, s)
            self._roi_mgr.changed.connect(lambda: db.save_roi_items(
                e.screen_rois + e.camera_rois))
            self._roi_mgr.start_ocr_refresh()   # ★ OCR 레이블 자동 갱신 시작
            v.addWidget(self._roi_mgr)

            hint = QLabel(
                "💡 ROI 추가: Display/Camera 미리보기 창에서 좌클릭 드래그\n"
                "   드래그 완료 → 팝업 다이얼로그에서 이름/설명 입력\n"
                "   우클릭 → 마지막 ROI 제거  |  ✏ → 이름/설명 편집\n"
                "   드래그앤드랍 → 감지 순서 변경")
            hint.setWordWrap(True)
            hint.setStyleSheet(
                "color:#567;font-size:10px;background:#090912;"
                "border:1px solid #1a1a2a;border-radius:4px;padding:8px;")
            v.addWidget(hint)
            v.addStretch()
            return w

        elif key == "ac":
            return AutoClickPanel(e, s)
        elif key == "timer":
            # [추가1] 타이머 패널 — PowerController는 나중에 주입
            self._timer_panel = TimerPanel(e, s, power_ctrl=getattr(self, '_power_ctrl', None))
            # AI가 타이머를 직접 제어할 수 있도록 engine에 참조 등록
            e._timer_panel_ref = self._timer_panel
            # CANoe가 이미 연결된 상태라면 즉시 CAN 조건 UI 활성화
            if getattr(e, '_can_available_for_timer', False):
                self._timer_panel.set_can_available(True)
            return self._timer_panel
        elif key == "macro":
            self._macro_panel = MacroPanel(e, s); return self._macro_panel
        elif key == "schedule":
            self._sched_panel = SchedulePanel(e, s); return self._sched_panel
        elif key == "memo":
            self._memo_panel = MemoPanel(e, s); return self._memo_panel
        elif key == "path":
            p = PathSettingsPanel(e, db)
            self._path_panel = p; return p
        elif key == "log":
            return LogPanel(s)
        elif key == "power":
            # ★ v5.6: BLTN 상태 패널을 전원 제어 패널에 흡수
            # QWidget 래퍼로 감싸서 두 패널을 안전하게 수직 배치
            container = QWidget()
            c_lay = QVBoxLayout(container)
            c_lay.setContentsMargins(0, 0, 0, 0)
            c_lay.setSpacing(4)
            pcp = PowerControlPanel(self._power_ctrl, db=self._db)
            c_lay.addWidget(pcp)
            self._bltn_panel = BLTNStatusPanel(self._power_ctrl)
            c_lay.addWidget(self._bltn_panel)
            return container
        elif key == "reset":
            return ResetPanel(e, db, s)
        return QLabel(f"(미구현: {key})")

    # ── 시그널 연결 ──────────────────────────────────────────────────────
    def _connect_signals(self):
        self._signals.status_message.connect(self._on_status)
        self._signals.monitors_scanned.connect(self._on_monitors)
        self._signals.cameras_scanned.connect(self._on_cameras)
        self._signals.roi_list_changed.connect(self._on_roi_changed)
        # ROI 변경 시 ROI 매니저 UI 동기화
        self._signals.roi_list_changed.connect(self._refresh_roi_panels)
        # 인라인 미리보기 PreviewLabel에서 ROI 드래그/우클릭 시 ROI 매니저 갱신
        self._scr_inline.roi_changed.connect(self._signals.roi_list_changed.emit)
        self._cam_inline.roi_changed.connect(self._signals.roi_list_changed.emit)

    def _on_status(self, msg: str):
        self._status_lbl.setText(msg)

    def _on_monitors(self, monitors: list):
        self._mon_cb.blockSignals(True); self._mon_cb.clear()
        for m in monitors:
            self._mon_cb.addItem(m["name"], m["idx"])
        idx = next((i for i, m in enumerate(monitors)
                    if m["idx"] == self._engine.active_monitor_idx), 0)
        self._mon_cb.setCurrentIndex(idx)
        self._mon_cb.blockSignals(False)

    def _on_cameras(self, cameras: list):
        self._cam_cb.blockSignals(True)
        self._cam_cb.clear()
        if cameras:
            for c in cameras:
                self._cam_cb.addItem(c["name"], c["idx"])
            idx = next((i for i, c in enumerate(cameras)
                        if c["idx"] == self._engine.active_cam_idx), 0)
            self._cam_cb.setCurrentIndex(idx)
        else:
            # ★ [버그 수정3] 카메라 없을 때 명시적으로 표시
            self._cam_cb.addItem("카메라 없음 (연결 후 재탐색)")
        self._cam_cb.blockSignals(False)

    def _on_mon_changed(self, i):
        if i < 0: return
        self._engine.active_monitor_idx = self._mon_cb.itemData(i)
        self._engine.restart_screen()

    def _on_cam_changed(self, i):
        if i < 0: return
        self._engine.active_cam_idx = self._cam_cb.itemData(i)
        cams = self._engine.camera_list
        cam  = next((c for c in cams if c["idx"]==self._engine.active_cam_idx), None)
        if cam: self._engine.actual_camera_fps = cam["fps"]
        self._engine.restart_camera()

    def _on_roi_changed(self):
        pass  # roi_list_changed 시그널 수신 → refresh는 _refresh_roi_panels에서

    def _refresh_roi_panels(self):
        mgr = getattr(self, '_roi_mgr', None)
        if mgr and hasattr(mgr, '_refresh'):
            mgr._refresh()

    def _scan_devices(self):
        # 카메라 스캔은 scan_cameras() 내부의 _cam_scanning 플래그로 중복 방지됨
        threading.Thread(target=self._engine.scan_monitors, daemon=True).start()
        threading.Thread(target=self._engine.scan_cameras,  daemon=True).start()

    def _show_api_doc(self):
        """★ v2.9.3: API 레퍼런스 — NonModal 싱글턴 창."""
        ApiDocDialog.show_or_raise(self)

    def _show_llm_chat(self):
        """AI 챗봇 창 — Groq/Gemini/Claude, NonModal 싱글턴."""
        LLMChatDialog.show_or_raise(self, self._engine)
        dlg = LLMChatDialog._instance
        if dlg and hasattr(dlg, "_ctrl"):
            dlg._ctrl.set_power(getattr(self, "_power_ctrl", None))
            # KernelEngine 주입 (ROI 읽기 등 전체 API 접근)
            dlg._ctrl._kernel = getattr(self, "_kernel", None)

    def _on_can_graph_toggle(self, on: bool):
        """CAN Monitor 그래프 ON/OFF — 타이머 제어."""
        self._can_graph_btn.setText("📊 CAN그래프 ON" if on else "📊 CAN그래프 OFF")
        # CanMonitorDialog 인스턴스에 직접 접근
        try:
            inst = CanMonitorDialog._instance
            if inst and hasattr(inst, '_graph_timer'):
                inst._graph_timer.start() if on else inst._graph_timer.stop()
            if inst:
                inst.show() if on else inst.hide()
        except Exception:
            pass

    def _show_can_monitor(self):
        """CAN Monitor 창 — DBC 신호 탐색 / CANoe COM 연결, NonModal."""
        CanMonitorDialog.show_or_raise(
            self, self._engine,
            kernel=getattr(self, "_kernel", None))

    def _show_kernel_dialog(self):
        """★ v5.6: 스크립트 커널 — 기능 패널에서 분리, NonModal 모달 창으로 표시."""
        KernelDialog.show_or_raise(self, self._kernel, self._signals)

    # ── 타이머 ───────────────────────────────────────────────────────────
    def _start_timers(self):
        # 미리보기 업데이트 (30fps)
        self._prev_timer = QTimer(self)
        self._prev_timer.timeout.connect(self._update_previews)
        self._prev_timer.start(33)

        # OCR 백그라운드 루프 — 별도 스레드 (UI 차단 없음)
        self._ocr_stop = threading.Event()
        self._ocr_thread = threading.Thread(
            target=self._ocr_loop, daemon=True, name="OCRLoop")
        self._ocr_thread.start()

        # ★ 전역 핫키 등록 — 포커스 무관하게 동작
        self._register_global_hotkeys()

        # 블랙아웃/예약/FPS/IO 갱신 (1초)
        self._slow_timer = QTimer(self)
        self._slow_timer.timeout.connect(self._slow_tick)
        self._slow_timer.start(1000)

        # 예약 체크 (1초)
        self._sched_timer = QTimer(self)
        self._sched_timer.timeout.connect(self._sched_tick)
        self._sched_timer.start(1000)

    def _update_previews(self):
        try:
            frame = self._engine.screen_queue.get_nowait()
            stamped = CoreEngine.stamp_preview(frame, self._engine, "screen")
            self._scr_inline.update_frame(stamped)
            self._disp_win.update_frame(stamped)
        except queue.Empty: pass
        try:
            frame = self._engine.camera_queue.get_nowait()
            stamped = CoreEngine.stamp_preview(frame, self._engine, "camera")
            self._cam_inline.update_frame(stamped)
            self._cam_win.update_frame(stamped)
        except queue.Empty: pass

    def _ocr_loop(self):
        """
        ★ v2.9.3 — OCR 백그라운드 루프.
        ROI가 있는 소스에 대해 주기적으로 OCR 실행 후 RoiItem.last_text 캐시.
        UI 스레드를 차단하지 않음 (별도 daemon 스레드).

        주기: 500ms (빠른 OCR 필요 시 줄일 수 있으나 CPU 주의)
        """
        import textwrap as _tw

        OCR_INTERVAL = 0.5   # 초 — 필요 시 줄이기 (최소 0.2 권장)
        ocr_self = self      # 클로저에서 self 참조용

        def _extract_region(source: str):
            """엔진 버퍼에서 최신 프레임 추출."""
            with self._engine._buf_lock:
                buf = (self._engine._scr_buf if source == "screen"
                       else self._engine._cam_buf)
                return buf[-1].copy() if buf else None

        def _run_ocr_on_roi(roi, frame, zoom_hint: float = 1.0):
            """
            단일 ROI OCR 실행 — HEX 전용 고정밀 모드.

            ★ v4.4 OCR 개선:
            · zoom_hint 의존 제거 → 물리 픽셀 기준 최소 400px 강제 확대
              (화면 줌은 표시 배율일 뿐 실픽셀 증가 없음)
            · CLAHE 대비 강화 + 언샤프 마스킹(샤프닝) 추가
            · 이진화 후보: Otsu 정/역 + 적응형(Gaussian) + 고정(127) + dilate
            · psm: 7(단행) / 8(단어) / 6(블록) / 13(원시행) 4종
            · 다자리 수 보존: image_to_data parts를 공백 없이 join
            · confidence 임계값 40으로 완화 (소형 숫자 대응)
            """
            rx, ry, rw, rh = roi.rect()
            region = frame[ry:ry+rh, rx:rx+rw]
            if region.size == 0:
                return

            # ── 물리 픽셀 기준 강제 업스케일 ─────────────────────────
            # zoom_hint는 표시 배율일 뿐 실픽셀과 무관.
            # 실픽셀 h 가 작으면 OCR이 글자를 인식 못함.
            # → h·w 모두 독립적으로 최소 400px 보장.
            MIN_DIM = 300   # ★ v4.5: 과확대 억제 (400→300)
            MAX_DIM = 900   # 메모리 방어선

            def _force_upscale(img):
                """
                물리 픽셀 기준 강제 확대 — 극소 ROI(15×15 등) 최적화.

                [개선] 15×15 같은 초극소 ROI 처리 전략:
                  1. 15px 미만: NEAREST×4 선확대 (획 형태 보존)
                  2. 30px 미만: NEAREST×2 추가 확대
                  3. Bilateral Filter: 경계 보존 스무딩 (노이즈 제거 + 획 보존)
                  4. 목표 MIN_DIM=400px bicubic 확대
                컴퓨터 자원: Bilateral은 극소 이미지에만 적용 → CPU 부담 최소화
                """
                h, w = img.shape[:2]
                if h <= 0 or w <= 0:
                    return img
                # 종횡비 극단 보정 (가로/세로 > 8)
                if h > 0 and w / h > 8:
                    pad = int(w / 8) - h
                    val = img[0,0].tolist() if img.ndim == 3 else int(img[0,0])
                    img = cv2.copyMakeBorder(
                        img, pad//2, pad - pad//2, 0, 0,
                        cv2.BORDER_CONSTANT, value=val)
                    h, w = img.shape[:2]
                short = min(h, w)

                # ★ [개선] 초극소(15px 미만) → NEAREST×4 선확대
                if short < 15:
                    img = cv2.resize(img, (w*4, h*4),
                                     interpolation=cv2.INTER_NEAREST)
                    h, w = img.shape[:2]; short = min(h, w)

                # ★ [개선] 극소(30px 미만) → NEAREST×2 추가 확대
                if short < 30:
                    img = cv2.resize(img, (w*2, h*2),
                                     interpolation=cv2.INTER_NEAREST)
                    h, w = img.shape[:2]; short = min(h, w)

                # ★ [개선] 극소 이미지에만 Bilateral Filter 적용
                # 경계 보존 스무딩: 획 경계는 선명하게, 내부 노이즈는 제거
                # 컴퓨터 자원: 작은 이미지(< 150px)에만 적용하여 부담 최소화
                if short < 150 and img.ndim == 2:
                    img = cv2.bilateralFilter(img, d=5, sigmaColor=50, sigmaSpace=50)

                # 목표 MIN_DIM=400px bicubic 확대
                MIN_DIM = 400
                MAX_DIM = 1200
                if short < MIN_DIM:
                    s   = MIN_DIM / short
                    nh  = min(int(h * s), MAX_DIM)
                    nw  = min(int(w * s), MAX_DIM)
                    img = cv2.resize(img, (nw, nh),
                                     interpolation=cv2.INTER_CUBIC)
                return img

            def _sharpen(gray):
                """언샤프 마스킹 — 획 경계 강화."""
                blur = cv2.GaussianBlur(gray, (0, 0), 2.0)
                return cv2.addWeighted(gray, 1.8, blur, -0.8, 0)

            def _clahe(gray):
                """CLAHE 대비 강화 — 저조도·낮은 대비 숫자 대응."""
                h  = gray.shape[0]
                ts = max(2, min(8, h // 30))
                return cv2.createCLAHE(
                    clipLimit=4.0, tileGridSize=(ts, ts)).apply(gray)

            def _dilate(th, ksize=2):
                k = cv2.getStructuringElement(cv2.MORPH_RECT, (ksize, ksize))
                return cv2.dilate(th, k, iterations=1)

            def _make_candidates(bgr):
                """
                전처리 후보 생성 — 극소 ROI 최적화판.

                ★ v4.5 변경:
                  · medianBlur 추가 → 소금후추 노이즈 제거
                    (극소 ROI 확대 후 나타나는 점 노이즈 억제)
                  · 적응형 이진화 제거 → 극소 ROI에서 노이즈 증폭 문제
                  · 고정 임계값(120/160) 후보 추가
                    → 배경/전경 밝기가 일정한 Display에 유리
                  · dilate 후보 유지 (가는 획 보강)
                """
                cands = []
                gray  = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                gray  = _force_upscale(gray)

                # ★ 노이즈 제거: medianBlur (극소 ROI 확대 시 발생하는 점 노이즈)
                med = cv2.medianBlur(gray, 3)

                # ── 경로 A: CLAHE + 샤프닝 (주력) ──────────────────────
                enhanced = _sharpen(_clahe(med))

                # ① Otsu 정방향 (밝은 배경 + 어두운 글자)
                _, th_otsu = cv2.threshold(
                    enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                cands.append((th_otsu, "clahe_otsu"))

                # ② Otsu 반전 (어두운 배경 + 밝은 글자 — LCD)
                _, th_inv = cv2.threshold(
                    enhanced, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
                cands.append((th_inv, "clahe_otsu_inv"))

                # ③ dilate (가는 획 보강)
                cands.append((_dilate(th_otsu), "clahe_otsu_dilate"))

                # ── 경로 B: 원본 gray (보험용) ──────────────────────────
                blur_g = cv2.GaussianBlur(med, (3, 3), 0)

                # ④ Otsu 정방향 원본
                _, th_raw = cv2.threshold(
                    blur_g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                cands.append((th_raw, "raw_otsu"))

                # ⑤ Otsu 반전 원본
                _, th_raw_inv = cv2.threshold(
                    blur_g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
                cands.append((th_raw_inv, "raw_otsu_inv"))

                # ⑥⑦ 고정 임계값 (Display 배경이 균일한 경우 강점)
                _, th_fix1 = cv2.threshold(blur_g, 120, 255, cv2.THRESH_BINARY)
                _, th_fix2 = cv2.threshold(blur_g, 160, 255, cv2.THRESH_BINARY_INV)
                cands.append((th_fix1, "fixed_120"))
                cands.append((th_fix2, "fixed_160_inv"))

                # ★ [개선] 극소 ROI 전용: 적응형 이진화
                # 확대 후에도 글자 크기가 작으면 적응형이 유리
                h_g = gray.shape[0]
                if h_g < 200:   # 확대 후에도 작은 경우
                    block = max(11, (h_g // 4) | 1)  # 홀수 보장
                    th_adap = cv2.adaptiveThreshold(
                        med, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                        cv2.THRESH_BINARY, block, 4)
                    th_adap_inv = cv2.adaptiveThreshold(
                        med, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                        cv2.THRESH_BINARY_INV, block, 4)
                    cands.append((th_adap,     "adap_gauss"))
                    cands.append((th_adap_inv, "adap_gauss_inv"))

                return cands

            def _pad(img, px=25):
                """여백 추가 — Tesseract는 여백이 충분할 때 잘 인식함."""
                return cv2.copyMakeBorder(
                    img, px, px, px, px,
                    cv2.BORDER_CONSTANT, value=255)

            def _clean_number(text: str) -> str:
                """
                숫자 + HEX 문자만 남기고 정규화.
                · 0~9, A~F (대소문자) 허용
                · 흔한 오인식 교정 후 대문자 정규화
                """
                import re as _re
                # 오인식 교정 테이블 (OCR이 혼동하는 문자 → HEX 의미상 올바른 값)
                _fix = {
                    'o': '0', 'O': '0', 'Q': '0',        # 0 오인식
                    'l': '1', 'I': '1', 'i': '1', '|': '1',  # 1 오인식
                    'Z': '2', 'z': '2',                   # 2 오인식
                    'S': '5', 's': '5',                   # 5 오인식
                    'G': '6',                              # 6 오인식
                    'T': '7',                              # 7 오인식
                    'B': '8',                              # 8 오인식
                    'g': '9', 'q': '9',                   # 9 오인식
                    # HEX 문자 소문자 → 대문자
                    'a': 'A', 'b': 'B', 'c': 'C',
                    'd': 'D', 'e': 'E', 'f': 'F',
                }
                t = text.strip()
                result = ""
                for ch in t:
                    fixed = _fix.get(ch, ch)
                    if fixed in '0123456789ABCDEFabcdef':
                        result += fixed.upper()
                    # 그 외 문자 제거 (공백, 특수문자 등)
                return result

            def _score(text: str, conf: float = 0.0) -> float:
                """
                HEX 또는 순수 숫자 결과에 점수 부여.

                ★ v4.5 개선:
                  · 단일 순수 숫자(0~9): 보너스 +50 (오탐 억제 핵심)
                    예) "1" conf=30 → (12+50)*(1+0.3) = 80.6
                        "FF" conf=10 → (20)*(1+0.1) = 22  → "1" 채택
                  · confidence 가중치 유지
                  · 다자리(2~3자)는 여전히 digit_only 우대
                  · 4자 이상 hex: 오탐 가능성으로 점수 하향
                """
                t = text.strip()
                if not t:
                    return 0.0
                leng = len(t)
                digit_only = all(c.isdigit() for c in t)
                hex_only   = all(c in '0123456789ABCDEF' for c in t.upper())
                conf_mult  = 1.0 + conf / 100.0

                if digit_only:
                    base = leng * 12
                    # ★ 단일 숫자: 보너스 +50 → 'FF' 같은 오탐보다 확실히 높게
                    if leng == 1:
                        base += 50
                    return base * conf_mult
                if hex_only:
                    base = leng * 10
                    # 4자 이상 hex는 오탐 가능성 → 점수 하향
                    if leng >= 4:
                        base = leng * 6
                    return base * conf_mult
                return float(leng) * conf_mult

            # ── OCR 1: pytesseract — 2단계 고속 전략 ────────────────
            #
            # ★ v4.5 고속화:
            #   기존: 이진화 7종 × psm 4종 = 최대 28 Tesseract 호출
            #         → ROI 3개면 ~3초 소요
            #
            #   신규: 1차 빠른 패스(max 4회) + 2차 보완 패스(max 10회)
            #         → 1차에서 conf ≥ 60이면 즉시 종료 (~0.1초)
            #         → 전체 최악 케이스에도 ~0.3초 이내 완료
            #
            best_text       = ""
            best_score      = -1.0
            best_confidence = -1.0
            tess_ran        = False

            try:
                import pytesseract as _tess
                from PIL import Image as _PIL

                exe = _tess.pytesseract.tesseract_cmd
                if not exe or not os.path.isfile(exe):
                    status = _init_tesseract()
                    if status.startswith("not_found"):
                        roi.last_text = "[ERR:Tesseract없음→UB-Mannheim설치]"
                        return

                WL = "0123456789ABCDEFabcdef"
                cands = _make_candidates(region)

                def _tess_once(pil_img, psm_n):
                    """단일 psm 호출 → (joined, avg_conf, score)."""
                    cfg = (f"--psm {psm_n} --oem 3"
                           f" -c tessedit_char_whitelist={WL}")
                    try:
                        data = _tess.image_to_data(
                            pil_img, config=cfg,
                            output_type=_tess.Output.DICT)
                        parts, csum, ccnt = [], 0.0, 0
                        for t, c in zip(data.get('text', []),
                                        data.get('conf', [])):
                            ts   = t.strip()
                            cv   = float(c) if str(c) != '-1' else 0.0
                            if ts and cv >= 0:
                                cl = _clean_number(ts)
                                if cl:
                                    parts.append(cl)
                                    if cv > 0:
                                        csum += cv; ccnt += 1
                        if not parts:
                            return "", 0.0, 0.0
                        joined = "".join(parts[:4])
                        aconf  = csum / ccnt if ccnt else 0.0
                        return joined, aconf, _score(joined, aconf)
                    except Exception:
                        return "", 0.0, 0.0

                def _upd(txt, conf, sc):
                    nonlocal best_text, best_score, best_confidence
                    if sc > best_score:
                        best_text, best_score, best_confidence = txt, sc, conf

                # ── 1차 빠른 패스: 상위 2종 이진화 × psm 10·7 (4회) ──
                for img_bin, _ in cands[:2]:
                    pil = _PIL.fromarray(_pad(img_bin))
                    for psm in (10, 7):
                        _upd(*_tess_once(pil, psm))
                    if best_confidence >= 60:
                        break   # 높은 신뢰도 → 즉시 채택

                # ── 2차 보완 패스: 1차 미확보 시만 실행 ─────────────
                if best_confidence < 60:
                    for img_bin, _ in cands[2:]:   # 나머지 이진화 후보
                        pil = _PIL.fromarray(_pad(img_bin))
                        for psm in (10, 8, 7):
                            _upd(*_tess_once(pil, psm))
                        if best_confidence >= 40:
                            break
                    # 여전히 미확보면 psm 6(블록)으로 상위 2종 재시도
                    if best_confidence < 40:
                        for img_bin, _ in cands[:2]:
                            pil = _PIL.fromarray(_pad(img_bin))
                            _upd(*_tess_once(pil, 6))
                            if best_confidence >= 40:
                                break

                tess_ran = True

            except ImportError:
                pass
            except Exception as ex:
                roi.last_text = f"[ERR:{str(ex)[:40]}]"
                return

            # ── OCR 2: easyocr fallback ──────────────────────────────
            if not best_text:
                try:
                    import easyocr as _eocr
                    if not hasattr(ocr_self, '_easyocr_reader'):
                        ocr_self._easyocr_reader = _eocr.Reader(
                            ['en'], gpu=False, verbose=False)
                    results = ocr_self._easyocr_reader.readtext(
                        region, detail=1, allowlist='0123456789ABCDEFabcdef')
                    if results:
                        parts = [_clean_number(str(r[1]))
                                 for r in results if float(r[2]) >= 0.3]
                        best_text = "".join(p for p in parts if p)
                    if not best_text and results:
                        best_text = "".join(
                            _clean_number(str(r[1])) for r in results)
                except ImportError:
                    if not tess_ran:
                        roi.last_text = "[ERR:OCR없음-pip install easyocr]"
                        return
                except Exception as ex:
                    roi.last_text = f"[ERR:easy-{str(ex)[:30]}]"
                    return

            # ── 최종 저장 ─────────────────────────────────────────────
            # 숫자만 남기기 (최후 방어선)
            roi.last_text = _clean_number(best_text) if best_text else ""


        # ── 메인 루프 ─────────────────────────────────────────────────
        # ★ v4.5 고속화:
        #   · OCR_INTERVAL 0.3s → 0.05s (루프 대기 단축)
        #     실제 처리 시간이 줄었으므로 CPU 부담 증가 없음
        #   · ROI 병렬 처리: ThreadPoolExecutor
        #     ROI 3개 → 각 ROI를 별도 스레드에서 동시 실행
        #     → 가장 오래 걸리는 ROI 1개의 시간만 소요
        #   · 프레임 캡처 1회 → 모든 ROI가 공유 (중복 캡처 제거)
        OCR_INTERVAL = 0.05  # ★ 0.3→0.05: 루프 대기 단축

        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Tesseract는 프로세스당 1개 인스턴스가 적합.
        # ROI별 병렬화는 Python 스레드 수준으로 충분
        # (GIL이 있어도 Tesseract 내부 C 코드는 GIL 해제)
        _ocr_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="ocr_worker")

        while not self._ocr_stop.is_set():
            try:
                has_roi = bool(self._engine.screen_rois or
                               self._engine.camera_rois)
                scr_ocr_on = getattr(self._engine, 'ocr_screen_enabled', True)
                cam_ocr_on = getattr(self._engine, 'ocr_camera_enabled', True)
                any_ocr_on = (
                    (bool(self._engine.screen_rois) and scr_ocr_on) or
                    (bool(self._engine.camera_rois) and cam_ocr_on)
                )
                if not has_roi or not any_ocr_on:
                    self._ocr_stop.wait(OCR_INTERVAL)
                    continue

                # zoom 배율 읽기
                scr_zoom = cam_zoom = 1.0
                mw = ocr_self
                try:
                    si = getattr(mw, '_scr_inline', None)
                    sf = getattr(getattr(mw, '_disp_win', None), 'prev', None)
                    if si: scr_zoom = max(scr_zoom, getattr(si, '_zoom', 1.0))
                    if sf: scr_zoom = max(scr_zoom, getattr(sf, '_zoom', 1.0))
                    ci = getattr(mw, '_cam_inline', None)
                    cf = getattr(getattr(mw, '_cam_win', None), 'prev', None)
                    if ci: cam_zoom = max(cam_zoom, getattr(ci, '_zoom', 1.0))
                    if cf: cam_zoom = max(cam_zoom, getattr(cf, '_zoom', 1.0))
                except Exception:
                    pass

                zoom_map = {"screen": scr_zoom, "camera": cam_zoom}

                # ── ROI 작업 목록 구성 ────────────────────────────────
                tasks = []
                for source, rois in (
                    ("screen", self._engine.screen_rois),
                    ("camera", self._engine.camera_rois),
                ):
                    if not rois or self._ocr_stop.is_set():
                        continue
                    if source == "screen" and not scr_ocr_on:
                        continue
                    if source == "camera" and not cam_ocr_on:
                        continue
                    # ★ 프레임 1회만 캡처 → 모든 ROI 공유
                    frame = _extract_region(source)
                    if frame is None:
                        continue
                    zh = zoom_map.get(source, 1.0)
                    for roi in rois:
                        tasks.append((roi, frame, zh))

                if not tasks:
                    self._ocr_stop.wait(OCR_INTERVAL)
                    continue

                if len(tasks) == 1:
                    # ROI 1개: 오버헤드 없이 직접 실행
                    _run_ocr_on_roi(*tasks[0])
                else:
                    # ROI 여러 개: 병렬 실행
                    futs = [
                        _ocr_executor.submit(_run_ocr_on_roi, roi, fr, zh)
                        for roi, fr, zh in tasks
                        if not self._ocr_stop.is_set()
                    ]
                    for fut in as_completed(futs):
                        try:
                            fut.result()
                        except Exception:
                            pass

            except Exception:
                pass
            self._ocr_stop.wait(OCR_INTERVAL)

    def _slow_tick(self):
        # FPS 표시
        sf = self._engine.measured_fps(self._engine._scr_fps_ts)
        cf = self._engine.measured_fps(self._engine._cam_fps_ts)
        self._fps_lbl.setText(
            f"Screen: {sf:.1f} fps  |  Camera: {cf:.1f} fps")

        # 녹화 패널 FPS
        rec_panel = self._panels.get("rec")
        if rec_panel and hasattr(rec_panel, 'update_fps'):
            rec_panel.update_fps(sf, cf)

        # 블랙아웃 패널 갱신
        bo_panel = self._panels.get("blackout")
        if bo_panel and hasattr(bo_panel, 'refresh'): bo_panel.refresh()

        # IO채널 상태
        io_ch = getattr(self._engine, 'io_channel', None)
        if io_ch:
            try:
                recording = io_ch.get_state("recording", False)
                self._io_lbl.setText(
                    f"IO채널: {IO_DB_PATH}  |  recording={recording}")
                self._io_lbl.setStyleSheet("color:#446;font-size:9px;font-family:monospace;")
            except: pass

        # [절대조건 12] 60초마다 ai_knowledge.json 갱신 (상태 변경 반영)
        self._slow_tick_count = getattr(self, '_slow_tick_count', 0) + 1
        if self._slow_tick_count % 60 == 0:  # 1초×60 = 60초마다
            threading.Thread(
                target=self._init_ai_self_knowledge,
                daemon=True, name="AISelfKnowledgeRefresh").start()

    def _sched_tick(self):
        actions = self._engine.schedule_tick()
        for act, entry in actions:
            if act == 'start':   self._engine.start_recording()
            elif act == 'stop':  self._engine.stop_recording()
            elif act == 'macro_run':
                self._engine.macro_start_run(
                    entry.macro_repeat, entry.macro_gap)
        sched_panel = self._panels.get("schedule")
        if sched_panel and hasattr(sched_panel, 'refresh_tbl'):
            sched_panel.refresh_tbl()

    # ── 설정 저장/복원 ───────────────────────────────────────────────────
    def _load_settings(self):
        db = self._db; e = self._engine

        # 일반 설정
        e.buffer_seconds       = db.get_int("buffer_seconds", 40)
        e.brightness_threshold = db.get_float("brightness_threshold", 30.0)
        e.blackout_cooldown    = db.get_float("blackout_cooldown", 5.0)
        e.manual_pre_sec       = db.get_float("manual_pre_sec", 10.0)
        e.manual_post_sec      = db.get_float("manual_post_sec", 10.0)
        e.manual_source        = db.get("manual_source", "both")
        e.actual_screen_fps    = db.get_float("screen_fps", 30.0)

        # ROI OCR / 밝기 ON/OFF + 블랙아웃 복원
        e.ocr_enabled           = db.get_bool("ocr_enabled", True)
        e.ocr_screen_enabled    = db.get_bool("ocr_screen_enabled", True)
        e.ocr_camera_enabled    = db.get_bool("ocr_camera_enabled", True)
        e.brightness_enabled    = db.get_bool("brightness_enabled", True)
        e.blackout_rec_enabled  = db.get_bool("blackout_rec_enabled", True)
        # ★ [버그2 수정] brightness_screen/camera_enabled DB 복원 (누락됐던 항목)
        e.brightness_screen_enabled = db.get_bool("brightness_screen_enabled", True)
        e.brightness_camera_enabled = db.get_bool("brightness_camera_enabled", True)

        # ROI 복원
        all_rois = db.load_roi_items()
        e.screen_rois = [r for r in all_rois if r.source == "screen"]
        e.camera_rois = [r for r in all_rois if r.source == "camera"]

        # 경로 설정
        if hasattr(self, '_path_panel'):
            self._path_panel.load_from_db()

        # ★ 패널 UI 값을 DB/engine 값으로 동기화 (처음 실행 시 default 적용)
        self._sync_panels_from_engine()

        # 메모 탭
        if hasattr(self, '_memo_panel'):
            tabs = db.load_memo_tabs()
            if tabs: self._memo_panel.set_tab_data(tabs)

        # 매크로 슬롯
        slots = db.load_macro_slots()
        if slots:
            if hasattr(self, '_macro_panel'):
                self._macro_panel.set_slots_data(slots)
            else:
                # ★ 패널이 아직 없어도 engine 캐시에 직접 주입 (커널/AI 실행 보장)
                try:
                    parsed = []
                    for s in slots:
                        steps = [MacroStep.from_dict(st) for st in s.get('steps', [])]
                        parsed.append({'title': s['title'], 'steps': steps})
                    self._engine._macro_slots = parsed
                except Exception:
                    pass

        # 커널 스크립트
        if hasattr(self, '_kernel_panel'):
            raw = db.get("kernel_scripts", "")
            if raw:
                try:
                    self._kernel_panel.set_scripts_data(json.loads(raw))
                except Exception:
                    pass

        # ── 섹션 순서·가시성 복원 ──────────────────────────────────────
        order_raw = db.get("section_order", "")
        if order_raw:
            try:
                saved = json.loads(order_raw)
                all_keys = [d[0] for d in _SECTION_DEFS]
                valid = [k for k in saved if k in all_keys]
                for k in all_keys:
                    if k not in valid: valid.append(k)
                self._section_order = valid
            except Exception:
                pass

        vis_raw = db.get("section_visible", "")
        if vis_raw:
            try:
                saved_vis = json.loads(vis_raw)
                for k, v in saved_vis.items():
                    self._section_visible[k] = bool(v)
            except Exception:
                pass

        # 순서·가시성 적용
        while self._sect_lay.count():
            it = self._sect_lay.takeAt(0)
            if it.widget(): it.widget().setParent(None)
        for k in self._section_order:
            sec = self._sections.get(k)
            if sec:
                sec.setParent(self._sect_w)
                sec.setVisible(self._section_visible.get(k, True))
                self._sect_lay.addWidget(sec)
        self._sect_lay.addStretch()
        self._rebuild_feat_list()

        # 섹션 접힘 상태 복원
        for key in self._section_order:
            collapsed = db.get_bool(f"sec_col_{key}", key not in ("rec",))
            sec = self._sections.get(key)
            if sec: sec.set_collapsed(collapsed)

        # ROI 패널 갱신
        self._refresh_roi_panels()

        # ROI 패널 버튼 상태 동기화 (복원된 플래그 반영)
        mgr = getattr(self, '_roi_mgr', None)
        if mgr:
            ocr_on    = getattr(e, 'ocr_enabled', True)
            bright_on = getattr(e, 'brightness_enabled', True)
            # ★ v4.5: 독립 버튼 복원
            scr_ocr = getattr(e, 'ocr_screen_enabled', True)
            cam_ocr = getattr(e, 'ocr_camera_enabled', True)
            if hasattr(mgr, '_ocr_scr_btn'):
                mgr._ocr_scr_btn.blockSignals(True)
                mgr._ocr_scr_btn.setChecked(scr_ocr)
                mgr._ocr_scr_btn.setText("🖥 OCR ▶ ON" if scr_ocr else "🖥 OCR ⏸ OFF")
                mgr._ocr_scr_btn.blockSignals(False)
            if hasattr(mgr, '_ocr_cam_btn'):
                mgr._ocr_cam_btn.blockSignals(True)
                mgr._ocr_cam_btn.setChecked(cam_ocr)
                mgr._ocr_cam_btn.setText("📷 OCR ▶ ON" if cam_ocr else "📷 OCR ⏸ OFF")
                mgr._ocr_cam_btn.blockSignals(False)
            if hasattr(mgr, '_ocr_btn'):
                mgr._ocr_btn.blockSignals(True)
                mgr._ocr_btn.setChecked(ocr_on)
                mgr._ocr_btn.setText("🖥 OCR ▶ ON" if ocr_on else "🖥 OCR ⏸ OFF")
                mgr._ocr_btn.blockSignals(False)
            if hasattr(mgr, '_bright_btn'):
                mgr._bright_btn.blockSignals(True)
                mgr._bright_btn.setChecked(bright_on)
                mgr._bright_btn.setText("💡 밝기 ▶ ON" if bright_on else "💡 밝기 ⏸ OFF")
                mgr._bright_btn.blockSignals(False)

        # ★ 수정: BlackoutPanel rec_chk 체크박스를 복원된 엔진 상태와 동기화
        bo_sec = self._sections.get('blackout') if hasattr(self, '_sections') else None
        bo_panel = bo_sec._content.findChild(BlackoutPanel) if bo_sec else None
        # _content의 직접 자식 위젯 탐색
        if bo_sec:
            for child in bo_sec._content.findChildren(BlackoutPanel):
                if hasattr(child, 'rec_chk'):
                    child.rec_chk.blockSignals(True)
                    child.rec_chk.setChecked(e.blackout_rec_enabled)
                    child.rec_chk.blockSignals(False)
                    break

        self._signals.status_message.emit("설정 복원 완료")

    def _save_settings(self):
        """엔진 설정을 DB에 저장. 종료 시 + 1초 디바운스 자동저장 시 호출."""
        db = self._db; e = self._engine

        db.set("buffer_seconds",       str(e.buffer_seconds))
        db.set("brightness_threshold", str(e.brightness_threshold))
        db.set("blackout_cooldown",    str(e.blackout_cooldown))
        db.set("manual_pre_sec",       str(e.manual_pre_sec))
        db.set("manual_post_sec",      str(e.manual_post_sec))
        db.set("manual_source",        e.manual_source)
        db.set("screen_fps",           str(e.actual_screen_fps))

        # ROI OCR / 밝기 ON/OFF + 블랙아웃 저장
        db.set("ocr_enabled",           e.ocr_enabled)
        db.set("ocr_screen_enabled",    getattr(e, "ocr_screen_enabled", True))
        db.set("ocr_camera_enabled",    getattr(e, "ocr_camera_enabled", True))
        db.set("brightness_enabled",    e.brightness_enabled)
        db.set("blackout_rec_enabled",  e.blackout_rec_enabled)
        # ★ [버그2 수정] brightness_screen/camera_enabled DB 저장 (누락됐던 항목)
        db.set("brightness_screen_enabled", getattr(e, "brightness_screen_enabled", True))
        db.set("brightness_camera_enabled", getattr(e, "brightness_camera_enabled", True))

        # ROI 저장
        db.save_roi_items(e.screen_rois + e.camera_rois)

        # 경로 설정
        if hasattr(self, '_path_panel'):
            self._path_panel._save()

        # 메모 탭
        if hasattr(self, '_memo_panel'):
            db.save_memo_tabs(self._memo_panel.get_tab_data())

        # 매크로 슬롯
        if hasattr(self, '_macro_panel'):
            db.save_macro_slots(self._macro_panel.get_slots_data())

        # 커널 스크립트
        if hasattr(self, '_kernel_panel'):
            try:
                db.set("kernel_scripts",
                       json.dumps(self._kernel_panel.get_scripts_data(),
                                  ensure_ascii=False))
            except Exception:
                pass

        # ── 섹션 순서·가시성·접힘 저장 ────────────────────────────────
        if hasattr(self, '_section_order'):
            db.set("section_order",
                   json.dumps(self._section_order, ensure_ascii=False))
        if hasattr(self, '_section_visible'):
            db.set("section_visible",
                   json.dumps(self._section_visible, ensure_ascii=False))
        if hasattr(self, '_sections'):
            for key, sec in self._sections.items():
                db.set(f"sec_col_{key}", sec.is_collapsed())

    # ── 전역 핫키 ────────────────────────────────────────────────────────
    def _register_global_hotkeys(self):
        """
        전역 핫키 등록 — 포커스 무관하게 동작 (AI Chat 창 활성화 중에도 작동).
        Windows: pywin32 RegisterHotKey 사용.
        Fallback: pynput keyboard listener.
        단축키 충돌 없는 조합 확인:
          Ctrl+Alt+W : 녹화 시작   (기존)
          Ctrl+Alt+E : 녹화 종료   (기존)
          Ctrl+Alt+M : 수동녹화    (기존)
          Ctrl+Alt+A : 오토클릭 시작 (기존)
          Ctrl+Alt+S : 오토클릭 정지 (기존) ← 한글 Windows에서 IME 충돌 가능
          Ctrl+Alt+X : 매크로 중단  (기존)
        ※ Ctrl+Alt+S는 일부 IME에서 한/영 전환과 충돌할 수 있으므로
          Ctrl+Shift+F12 를 보조 정지 키로도 등록.
        """
        self._hotkey_thread = None
        self._hotkey_stop   = threading.Event()

        try:
            import win32con as _wc
            import win32api as _wa
            import win32gui as _wg

            # ID 정의
            _HK = {
                1: (_wc.MOD_CONTROL | _wc.MOD_ALT, ord('W')),  # 녹화 시작
                2: (_wc.MOD_CONTROL | _wc.MOD_ALT, ord('E')),  # 녹화 종료
                3: (_wc.MOD_CONTROL | _wc.MOD_ALT, ord('M')),  # 수동녹화
                4: (_wc.MOD_CONTROL | _wc.MOD_ALT, ord('A')),  # AC 시작
                5: (_wc.MOD_CONTROL | _wc.MOD_ALT, ord('S')),  # AC 정지
                6: (_wc.MOD_CONTROL | _wc.MOD_ALT, ord('X')),  # 매크로 중단
                7: (_wc.MOD_CONTROL | _wc.MOD_SHIFT, _wc.VK_F12), # AC 정지 보조
            }

            _HK_ACTIONS = {
                1: lambda: self._engine.start_recording() if not self._engine.recording else None,
                2: lambda: self._engine.stop_recording()  if self._engine.recording else None,
                3: lambda: self._engine.save_manual_clip(),
                4: lambda: self._engine.start_ac() if not self._engine.ac_enabled else None,
                5: lambda: self._engine.stop_ac()  if self._engine.ac_enabled else None,
                6: lambda: self._engine.macro_stop_run(),
                7: lambda: self._engine.stop_ac()  if self._engine.ac_enabled else None,
            }

            hwnd = int(self.winId())
            registered = []
            for hk_id, (mod, key) in _HK.items():
                try:
                    _wa.RegisterHotKey(None, hk_id, mod, key)
                    registered.append(hk_id)
                except Exception:
                    pass

            def _hotkey_loop():
                import ctypes
                msg = ctypes.wintypes.MSG()
                while not self._hotkey_stop.is_set():
                    if ctypes.windll.user32.PeekMessageW(
                            ctypes.byref(msg), None, 0x312, 0x312, 1):  # WM_HOTKEY
                        hk_id = msg.wParam
                        if hk_id in _HK_ACTIONS:
                            try:
                                _HK_ACTIONS[hk_id]()
                            except Exception:
                                pass
                    self._hotkey_stop.wait(0.05)
                for hk_id in registered:
                    try:
                        _wa.UnregisterHotKey(None, hk_id)
                    except Exception:
                        pass

            self._hotkey_thread = threading.Thread(
                target=_hotkey_loop, daemon=True, name="GlobalHotkey")
            self._hotkey_thread.start()
            self._signals.status_message.emit(
                f"[단축키] 전역 핫키 등록 완료 ({len(registered)}개) — "
                "포커스 무관 동작")

        except ImportError:
            # pywin32 없으면 pynput fallback
            try:
                from pynput import keyboard as _kb

                _COMBO_STOP_AC = {_kb.Key.ctrl_l, _kb.Key.alt_l,
                                  _kb.KeyCode.from_char('s')}
                _COMBO_STOP_AC2 = {_kb.Key.ctrl_r, _kb.Key.alt_r,
                                   _kb.KeyCode.from_char('s')}
                _pressed: set = set()

                def _on_press(key):
                    _pressed.add(key)
                    if (_COMBO_STOP_AC.issubset(_pressed) or
                            _COMBO_STOP_AC2.issubset(_pressed)):
                        if self._engine.ac_enabled:
                            self._engine.stop_ac()

                def _on_release(key):
                    _pressed.discard(key)

                _listener = _kb.Listener(on_press=_on_press, on_release=_on_release)
                _listener.daemon = True
                _listener.start()
                self._pynput_hotkey_listener = _listener
                self._signals.status_message.emit(
                    "[단축키] pynput fallback 핫키 등록 (Ctrl+Alt+S: AC 정지)")
            except Exception as ex:
                self._signals.status_message.emit(
                    f"[단축키] 전역 핫키 등록 실패: {ex} — "
                    "창에 포커스가 있을 때만 단축키 동작")

    def _cleanup_global_hotkeys(self):
        """앱 종료 시 핫키 해제."""
        self._hotkey_stop.set() if hasattr(self, '_hotkey_stop') else None

    # ── 키보드 단축키 ────────────────────────────────────────────────────
    def keyPressEvent(self, e):
        mod = e.modifiers()
        key = e.key()
        CTRL_ALT = Qt.ControlModifier | Qt.AltModifier
        if mod == CTRL_ALT:
            if key == Qt.Key_W:
                if not self._engine.recording: self._engine.start_recording()
            elif key == Qt.Key_E:
                if self._engine.recording:
                    if self._engine.tc_rec_enabled:
                        result = show_tc_dialog(self, "녹화 종료 — T/C 검증 결과 선택")
                        if result is None:
                            pass
                        else:
                            self._engine.tc_verify_result = result
                            self._engine.stop_recording()
                    else:
                        self._engine.stop_recording()
            elif key == Qt.Key_M:
                self._engine.save_manual_clip()
            elif key == Qt.Key_A:
                if not self._engine.ac_enabled: self._engine.start_ac()
            elif key == Qt.Key_S:
                if self._engine.ac_enabled: self._engine.stop_ac()
            elif key == Qt.Key_X:
                self._engine.macro_stop_run()
        super().keyPressEvent(e)

    def _sync_panels_from_engine(self):
        """엔진 설정값으로 각 패널 UI를 동기화. 처음 실행 시 default 적용."""
        e = self._engine
        try:
            # 수동녹화 패널
            if hasattr(self, '_manual_panel'):
                mp = self._manual_panel
                if hasattr(mp, 'pre_spin'):
                    mp.pre_spin.blockSignals(True)
                    mp.pre_spin.setValue(float(getattr(e, 'manual_pre_sec', 10.0)))
                    mp.pre_spin.blockSignals(False)
                if hasattr(mp, 'post_spin'):
                    mp.post_spin.blockSignals(True)
                    mp.post_spin.setValue(float(getattr(e, 'manual_post_sec', 10.0)))
                    mp.post_spin.blockSignals(False)
        except Exception:
            pass
        try:
            # 버퍼 설정 패널
            if hasattr(self, '_buffer_panel'):
                bp = self._buffer_panel
                if hasattr(bp, 'buf_spin'):
                    bp.buf_spin.blockSignals(True)
                    bp.buf_spin.setValue(int(getattr(e, 'buffer_seconds', 40)))
                    bp.buf_spin.blockSignals(False)
        except Exception:
            pass

    def _get_settings_snapshot(self) -> dict:
        """현재 엔진 설정값 스냅샷 (변경 감지용)."""
        e = self._engine
        return {
            'buffer_seconds':       getattr(e, 'buffer_seconds', 40),
            'brightness_threshold': getattr(e, 'brightness_threshold', 30.0),
            'blackout_cooldown':    getattr(e, 'blackout_cooldown', 5.0),
            'manual_pre_sec':       getattr(e, 'manual_pre_sec', 10.0),
            'manual_post_sec':      getattr(e, 'manual_post_sec', 10.0),
            'manual_source':        getattr(e, 'manual_source', 'both'),
            'screen_fps':           getattr(e, 'actual_screen_fps', 30.0),
            'ocr_enabled':          getattr(e, 'ocr_enabled', True),
            'brightness_enabled':   getattr(e, 'brightness_enabled', True),
            'blackout_rec_enabled': getattr(e, 'blackout_rec_enabled', True),
        }

    def _periodic_gc(self):
        """
        6시간마다 호출 — 장기 실행 시 순환 참조·미회수 객체 정리.
        UI 스레드에서 실행되므로 안전. gc.collect()는 수 ms 이내 완료.
        """
        import gc as _gc
        collected = _gc.collect()
        # 스크린/카메라 버퍼 maxlen 재조정 (FPS 변경 반영)
        try:
            with self._engine._buf_lock:
                # deque maxlen은 재생성 없이는 변경 불가 — 크기 편차가 클 때만
                cur_scr_max = self._engine._scr_buf.maxlen or 1
                new_scr_max = self._engine._scr_buf_max
                if abs(cur_scr_max - new_scr_max) > new_scr_max * 0.2:
                    self._engine._scr_buf = deque(
                        list(self._engine._scr_buf)[-new_scr_max:],
                        maxlen=new_scr_max)
                cur_cam_max = self._engine._cam_buf.maxlen or 1
                new_cam_max = self._engine._cam_buf_max
                if abs(cur_cam_max - new_cam_max) > new_cam_max * 0.2:
                    self._engine._cam_buf = deque(
                        list(self._engine._cam_buf)[-new_cam_max:],
                        maxlen=new_cam_max)
        except Exception:
            pass
        self._signals.status_message.emit(
            f'[GC] 주기적 정리 완료 — 회수 객체: {collected}개')

    def _periodic_gc(self):
        import gc as _gc
        collected = _gc.collect()
        try:
            with self._engine._buf_lock:
                cur_scr = self._engine._scr_buf.maxlen or 1
                new_scr = self._engine._scr_buf_max
                if abs(cur_scr - new_scr) > new_scr * 0.2:
                    self._engine._scr_buf = deque(
                        list(self._engine._scr_buf)[-new_scr:], maxlen=new_scr)
                cur_cam = self._engine._cam_buf.maxlen or 1
                new_cam = self._engine._cam_buf_max
                if abs(cur_cam - new_cam) > new_cam * 0.2:
                    self._engine._cam_buf = deque(
                        list(self._engine._cam_buf)[-new_cam:], maxlen=new_cam)
        except Exception:
            pass
        self._signals.status_message.emit(f'[GC] 완료 — 회수: {collected}개')

    def _check_and_save_settings(self):
        """3초마다 설정 변경 감지 → 변경 시 DB 저장 (CPU 최소화)."""
        current = self._get_settings_snapshot()
        if current != self._settings_snapshot:
            self._settings_snapshot = current
            self._save_settings()

    def trigger_autosave(self):
        """패널에서 설정 변경 시 호출 — 1초 후 DB 자동저장."""
        if hasattr(self, '_global_autosave_timer'):
            self._global_autosave_timer.start()


    def _init_ai_self_knowledge(self):
        """
        프로그램 시작 시 AI 자기 인식 파일(ai_knowledge.json)을 기본 경로에 생성.
        파일이 이미 있으면 읽어서 user_notes를 AI 시스템 컨텍스트에 반영.

        [개선] AI가 실제로 보는 동적 프롬프트 스냅샷도 ai_knowledge.json 에 기록.
        → 사용자가 "AI가 뭘 보고 이해하는지" 직접 확인 가능.
        """
        import time as _t
        _t.sleep(2.0)  # 엔진/ROI 완전 초기화 후 실행

        knowledge_path = os.path.join(BASE_DIR, "ai_knowledge.json")
        os.makedirs(BASE_DIR, exist_ok=True)

        default_knowledge = {
            "_description": (
                "Screen & Camera Recorder AI 자기 파악 파일 (자동 생성)\n"
                "※ AI가 실제로 보는 정보는 'ai_sees_this_prompt' 섹션을 확인하세요.\n"
                "※ 'user_notes'에 추가 교육 내용을 작성 후 저장 → 재실행 시 AI가 반영합니다."
            ),
            "_generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "program_purpose": (
                "차량 카메라/디스플레이 화면을 녹화하고 OCR로 신호값을 감시하며 "
                "CAN 신호와 연동하여 자동화 T/C 검증을 수행하는 영상 녹화·분석 프로그램."
            ),
            "how_ai_works": {
                "step1": "사용자 입력 → AIChatController._process() 호출",
                "step2": "_build_sys_prompt()로 실시간 상태 반영 시스템 프롬프트 생성",
                "step3": "Groq/Gemini/Claude API에 system= 으로 전달",
                "step4": "LLM이 JSON 액션 반환 → _execute()가 실제 함수 호출",
                "key_insight": (
                    "_SYS_STATIC(고정 API 명세) + 실시간 ROI목록 + 설정값 + "
                    "CAN신호목록 + 전원상태 + user_notes = 실제 AI 지식"
                ),
            },
            "ai_sees_this_prompt": "(프로그램 실행 후 자동 갱신됨)",
            "user_notes": (
                "※ 이 섹션에 AI에게 가르치고 싶은 내용을 자유롭게 작성하세요.\n"
                "예: 특정 CAN 신호 의미, 차량 동작 조건, 커스텀 T/C 시나리오 등\n"
                "저장 후 프로그램 재실행 시 AI가 이 내용을 시스템 프롬프트에 반영합니다."
            ),
        }

        # 기존 파일의 user_notes 보존
        user_extra = ""
        if os.path.isfile(knowledge_path):
            try:
                with open(knowledge_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
                saved_notes = existing.get("user_notes", "")
                if saved_notes and "※ 이 섹션에" not in saved_notes:
                    user_extra = saved_notes
                    default_knowledge["user_notes"] = saved_notes
                    self._signals.status_message.emit(
                        f"[AI] 사용자 교육 내용 로드 완료 ({len(saved_notes)}자)")
                else:
                    # 기존 user_notes가 기본값이면 그대로 유지
                    default_knowledge["user_notes"] = saved_notes or default_knowledge["user_notes"]
            except Exception:
                pass

        # AI가 실제로 보는 동적 프롬프트 스냅샷 생성
        # → AIChatController가 LLM Chat dialog에 있으면 거기서 생성
        dynamic_prompt_snapshot = "(AI Chat 연결 후 갱신됩니다)"
        try:
            # LLMChatDialog가 열려있으면 실제 프롬프트 생성
            from PyQt5.QtWidgets import QApplication as _QApp
            for widget in _QApp.topLevelWidgets():
                if hasattr(widget, '_ctrl') and hasattr(widget._ctrl, '_build_sys_prompt'):
                    dynamic_prompt_snapshot = widget._ctrl._build_sys_prompt()
                    break
        except Exception:
            pass

        default_knowledge["ai_sees_this_prompt"] = dynamic_prompt_snapshot
        default_knowledge["_generated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 파일 저장
        try:
            with open(knowledge_path, 'w', encoding='utf-8') as f:
                json.dump(default_knowledge, f, ensure_ascii=False, indent=2)
            self._signals.status_message.emit(
                f"[AI] 자기 파악 파일 갱신: {knowledge_path}")
        except Exception as ex:
            self._signals.status_message.emit(f"[AI] 파일 생성 실패: {ex}")

        # user_notes를 AI Chat 컨트롤러 히스토리에 주입
        if user_extra:
            try:
                from PyQt5.QtWidgets import QApplication as _QApp2
                for widget in _QApp2.topLevelWidgets():
                    if hasattr(widget, '_ctrl') and hasattr(widget._ctrl, '_lock'):
                        with widget._ctrl._lock:
                            widget._ctrl._history.insert(0, {
                                "role": "model",
                                "parts": [f"[사용자 교육 데이터]\n{user_extra[:800]}"]
                            })
                        break
            except Exception:
                pass

    def closeEvent(self, e):
        # 종료 시 즉시 저장 (타이머 대기 없이)
        if hasattr(self, '_global_autosave_timer'):
            self._global_autosave_timer.stop()
        if hasattr(self, '_settings_check_timer'):
            self._settings_check_timer.stop()
        if hasattr(self, '_gc_timer'):
            self._gc_timer.stop()
        if hasattr(self, '_gc_timer'):
            self._gc_timer.stop()
        self._save_settings()
        # 전역 핫키 해제
        self._cleanup_global_hotkeys()
        # OCR 루프 중단
        if hasattr(self, '_ocr_stop'):
            self._ocr_stop.set()
        # 종료 전 커널 결과·로그 자동 저장 (크래시 포함)
        try: self._kernel._auto_export()
        except Exception: pass
        self._kernel.stop()
        self._engine.stop()
        # 전원 제어기 연결 해제
        if hasattr(self, '_power_ctrl') and self._power_ctrl:
            self._power_ctrl.disconnect()
        try: _can_manager.disconnect()
        except Exception: pass
        self._cam_win.close(); self._disp_win.close()
        e.accept()

def main():
    # PyInstaller DPI awareness (Windows)
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    import os as _os_qt; _os_qt.environ.setdefault("QT_LOGGING_RULES", "*.warning=false")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 다크 팔레트
    from PyQt5.QtGui import QPalette
    pal = QPalette()
    pal.setColor(QPalette.Window,          QColor(13,13,30))
    pal.setColor(QPalette.WindowText,      QColor(200,200,220))
    pal.setColor(QPalette.Base,            QColor(8,8,18))
    pal.setColor(QPalette.AlternateBase,   QColor(20,20,40))
    pal.setColor(QPalette.ToolTipBase,     QColor(13,13,30))
    pal.setColor(QPalette.ToolTipText,     QColor(200,200,220))
    pal.setColor(QPalette.Text,            QColor(200,200,220))
    pal.setColor(QPalette.Button,          QColor(26,26,58))
    pal.setColor(QPalette.ButtonText,      QColor(200,200,220))
    pal.setColor(QPalette.BrightText,      QColor(255,80,80))
    pal.setColor(QPalette.Link,            QColor(42,130,218))
    pal.setColor(QPalette.Highlight,       QColor(42,130,218))
    pal.setColor(QPalette.HighlightedText, QColor(0,0,0))
    app.setPalette(pal)

    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()