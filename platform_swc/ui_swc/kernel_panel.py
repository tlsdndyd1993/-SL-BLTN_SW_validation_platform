# -*- coding: utf-8 -*-
"""
ui_swc/kernel_panel.py
커널 스크립트 실행 패널
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

import sys, os, json, time, threading
from kernel_swc.kernel_engine import KernelEngine
from kernel_swc.kernel_script import KernelScript
class KernelPanel(QWidget):
    """
    커널 스크립트 관리 패널.

    구성:
      ┌─ 슬롯 목록 (드래그앤드랍 순서변경) ─────────────────┐
      │  + 추가  /  - 삭제  /  ✏ 이름변경                   │
      └──────────────────────────────────────────────────────┘
      ┌─ 스크립트 에디터 ────────────────────────────────────┐
      │  반복 설정  /  활성화 체크박스                        │
      │  Python 에디터 (QPlainTextEdit)                      │
      │  [가져오기 .py]  [내보내기 .py]  [▶실행]  [■중단]   │
      └──────────────────────────────────────────────────────┘
      ┌─ 실행 로그 ─────────────────────────────────────────┐
      └──────────────────────────────────────────────────────┘
    """
    _SLOT_H = 34
    _BG     = "#0a0a18"
    _BG_HV  = "#141428"
    _BG_DG  = "#1a2a4a"

    def __init__(self, kernel: KernelEngine, signals: 'Signals', parent=None):
        super().__init__(parent)
        self._kernel  = kernel
        self._signals = signals
        self._kernel.log_callback = self._on_log
        self._active_idx = -1
        self._slot_widgets: list = []
        self._dragging   = False
        # ★ maxlen=500 — 저장 후 clear 방식으로 전환, 메모리 상한 500줄
        self._log_lines: deque = deque(maxlen=500)
        self._log_auto_scroll = True    # 사용자가 위로 스크롤하면 False
        kernel._panel_ref = self         # _auto_export에서 _log_lines 접근용
        # ★ 블랙아웃 감지 시 커널 로그에 카운트 출력
        signals.blackout_log.connect(self._on_log)
        # ★ 로그 휘발 명령 수신 (메인스레드 안전)
        signals.status_message.connect(self._on_status_for_clear)
        self._drag_idx   = -1
        self._drag_start = QPoint()
        self._build()
        self._add_script()   # 기본 슬롯 1개
        # ★ kernel_log 시그널 → _on_log_safe (메인스레드에서 안전하게 UI 갱신)
        signals.kernel_log.connect(self._on_log_safe)

    # ── UI 빌드 ───────────────────────────────────────────────────────────────
    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(6)

        # ── Python 인터프리터 설정 ────────────────────────────────────────
        py_grp = QGroupBox("🐍 Python 인터프리터 설정")
        py_grp.setStyleSheet(
            "QGroupBox{font-size:10px;color:#7bc8e0;"
            "border:1px solid #1a3a3a;border-radius:4px;margin-top:8px;"
            "padding-top:6px;background:#060612;}"
            "QGroupBox::title{left:8px;top:-6px;background:#060612;"
            "padding:0 4px;}")
        py_l = QVBoxLayout(py_grp); py_l.setSpacing(4); py_l.setContentsMargins(6,4,6,6)

        # 인터프리터 경로
        py_row = QHBoxLayout(); py_row.setSpacing(4)
        py_row.addWidget(QLabel("python:"))
        self._py_ed = QLineEdit(self._kernel.python_exe)
        self._py_ed.setPlaceholderText("python.exe 경로 (비워두면 현재 프로세스 사용)")
        self._py_ed.setStyleSheet(
            "QLineEdit{background:#0a0a18;color:#7bc8e0;"
            "border:1px solid #1a3a4a;border-radius:3px;"
            "padding:2px 6px;font-size:10px;font-family:monospace;}")
        self._py_ed.textChanged.connect(
            lambda t: setattr(self._kernel, 'python_exe',
                              t.strip() or sys.executable))
        py_row.addWidget(self._py_ed, 1)
        py_browse = QPushButton("📂")
        py_browse.setFixedSize(26, 24); py_browse.setToolTip("python.exe 찾기")
        py_browse.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:3px;font-size:12px;}")
        py_browse.clicked.connect(self._on_browse_python)
        py_row.addWidget(py_browse)
        py_l.addLayout(py_row)

        # 브릿지 + 클라이언트 생성 버튼
        bridge_row = QHBoxLayout(); bridge_row.setSpacing(4)
        self._bridge_btn = QPushButton("🔌 브릿지 서버 시작")
        self._bridge_btn.setFixedHeight(24)
        self._bridge_btn.setStyleSheet(
            "QPushButton{background:#1a1a3a;color:#f0c040;"
            "border:1px solid #4a4a20;border-radius:3px;font-size:10px;}"
            "QPushButton:hover{background:#22221a;}")
        self._bridge_btn.clicked.connect(self._on_bridge_toggle)
        gen_btn = QPushButton("📄 recorder_bridge.py 생성")
        gen_btn.setFixedHeight(24)
        gen_btn.setStyleSheet(
            "QPushButton{background:#1a2a1a;color:#8fa;"
            "border:1px solid #2a6a2a;border-radius:3px;font-size:10px;}"
            "QPushButton:hover{background:#1e3a1e;}")
        gen_btn.clicked.connect(self._on_gen_bridge)
        self._bridge_status = QLabel("● 미시작")
        self._bridge_status.setStyleSheet("color:#556;font-size:9px;")
        bridge_row.addWidget(self._bridge_btn)
        bridge_row.addWidget(gen_btn)
        bridge_row.addStretch()
        bridge_row.addWidget(self._bridge_status)
        py_l.addLayout(bridge_row)

        # 폴더 일괄 가져오기 ── 수정3
        folder_row = QHBoxLayout(); folder_row.setSpacing(4)
        folder_row.addWidget(QLabel("📁 폴더:"))
        self._watch_ed = QLineEdit(self._kernel.watch_dir)
        self._watch_ed.setPlaceholderText(".py 파일이 있는 폴더 경로")
        self._watch_ed.setStyleSheet(
            "QLineEdit{background:#0a0a18;color:#f0c040;"
            "border:1px solid #3a3a20;border-radius:3px;"
            "padding:2px 6px;font-size:10px;font-family:monospace;}")
        self._watch_ed.textChanged.connect(
            lambda t: setattr(self._kernel, 'watch_dir', t.strip()))
        folder_row.addWidget(self._watch_ed, 1)
        folder_browse = QPushButton("📂")
        folder_browse.setFixedSize(26, 24)
        folder_browse.setStyleSheet(py_browse.styleSheet())
        folder_browse.clicked.connect(self._on_browse_folder)
        folder_row.addWidget(folder_browse)
        load_btn = QPushButton("⬇ 전체 가져오기")
        load_btn.setFixedHeight(24)
        load_btn.setToolTip("폴더 내 .py 파일을 슬롯으로 일괄 추가")
        load_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:3px;font-size:10px;}"
            "QPushButton:hover{background:#22334a;}")
        load_btn.clicked.connect(self._on_load_folder)
        folder_row.addWidget(load_btn)
        py_l.addLayout(folder_row)
        v.addWidget(py_grp)

        # ── 슬롯 관리 바 ──────────────────────────────────────────────────────
        slot_hdr = QHBoxLayout(); slot_hdr.setSpacing(6)
        slot_hdr.addWidget(QLabel("📜 스크립트 슬롯"))
        for lbl, fn, tip, c in [
            ("＋","_add_script",   "슬롯 추가","#1a4a2a"),
            ("－","_del_script",   "선택 슬롯 삭제","#3a1a1a"),
            ("✏","_rename_script","이름 변경","#1a2a3a"),
        ]:
            b = QPushButton(lbl); b.setFixedSize(26, 26)
            b.setToolTip(tip)
            b.setStyleSheet(
                f"QPushButton{{background:{c};color:#ddd;"
                "border:1px solid #3a4a3a;border-radius:3px;}}")
            b.clicked.connect(getattr(self, fn))
            slot_hdr.addWidget(b)
        slot_hdr.addStretch()

        # 전체 RUN 버튼
        self._run_all_btn = QPushButton("▶▶  전체 실행")
        self._run_all_btn.setFixedHeight(26)
        self._run_all_btn.setStyleSheet(
            "QPushButton{background:#1a4a2a;color:#afffcf;"
            "border:1px solid #2a8a5a;border-radius:4px;"
            "font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#225a3a;}"
            "QPushButton:disabled{background:#0a1a0a;color:#3a5a3a;}")
        self._run_all_btn.clicked.connect(self._on_run_all)
        slot_hdr.addWidget(self._run_all_btn)
        self._stop_btn = QPushButton("■  중단")
        self._stop_btn.setFixedHeight(26)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            "QPushButton{background:#3a1a1a;color:#f88;"
            "border:1px solid #6a2a2a;border-radius:4px;font-size:11px;}"
            "QPushButton:hover{background:#5a1a1a;}"
            "QPushButton:disabled{background:#1a1a1a;color:#556;}")
        self._stop_btn.clicked.connect(self._on_stop)
        slot_hdr.addWidget(self._stop_btn)
        v.addLayout(slot_hdr)

        # 슬롯 스크롤 리스트
        self._slot_scroll = QScrollArea()
        self._slot_scroll.setWidgetResizable(True)
        self._slot_scroll.setFixedHeight(130)
        self._slot_scroll.setStyleSheet(
            f"QScrollArea{{border:1px solid #1a2a3a;background:{self._BG};}}"
            "QScrollBar:vertical{background:#0a0a18;width:6px;}"
            "QScrollBar::handle:vertical{background:#336;border-radius:3px;}")
        self._slot_w = QWidget(); self._slot_w.setStyleSheet(f"background:{self._BG};")
        self._slot_lay = QVBoxLayout(self._slot_w)
        self._slot_lay.setContentsMargins(2,2,2,2); self._slot_lay.setSpacing(2)
        self._slot_scroll.setWidget(self._slot_w)
        v.addWidget(self._slot_scroll)

        # ── 에디터 영역 ───────────────────────────────────────────────────────
        ed_grp = QGroupBox("📝 스크립트 편집")
        ed_l = QVBoxLayout(ed_grp); ed_l.setSpacing(6)

        # 옵션 행
        opt_row = QHBoxLayout(); opt_row.setSpacing(10)
        self._enabled_chk = QCheckBox("활성화")
        self._enabled_chk.setChecked(True)
        self._enabled_chk.setStyleSheet(
            "QCheckBox{font-size:11px;font-weight:bold;color:#2ecc71;spacing:5px;}")
        self._enabled_chk.toggled.connect(self._on_enabled_toggled)
        opt_row.addWidget(self._enabled_chk)
        opt_row.addWidget(QLabel("반복:"))
        self._rep_spin = QSpinBox(); self._rep_spin.setRange(0, 9999)
        self._rep_spin.setValue(1); self._rep_spin.setFixedWidth(64)
        self._rep_spin.setSpecialValueText("∞")
        self._rep_spin.setStyleSheet(
            "QSpinBox{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:3px;font-size:12px;font-weight:bold;}")
        self._rep_spin.valueChanged.connect(self._on_rep_changed)
        opt_row.addWidget(self._rep_spin)
        opt_row.addStretch()

        # 파일 IO 버튼
        imp_btn = QPushButton("📂 .py 가져오기")
        imp_btn.setFixedHeight(26)
        imp_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:4px;font-size:10px;}")
        imp_btn.clicked.connect(self._on_import)
        exp_btn = QPushButton("💾 .py 내보내기")
        exp_btn.setFixedHeight(26)
        exp_btn.setStyleSheet(
            "QPushButton{background:#1a2a1a;color:#8fa;"
            "border:1px solid #2a6a2a;border-radius:4px;font-size:10px;}")
        exp_btn.clicked.connect(self._on_export)
        opt_row.addWidget(imp_btn); opt_row.addWidget(exp_btn)

        # ── 환경정보 / CMD 실행 버튼 ──────────────────────────────────────
        env_row = QHBoxLayout(); env_row.setSpacing(6)
        env_btn = QPushButton("🐍 Python 환경 정보")
        env_btn.setFixedHeight(26)
        env_btn.setStyleSheet(
            "QPushButton{background:#1a1a3a;color:#f0c040;"
            "border:1px solid #4a4a20;border-radius:4px;font-size:10px;}"
            "QPushButton:hover{background:#22221a;}")
        env_btn.clicked.connect(self._on_env_info)

        cmd_btn = QPushButton("🖥 CMD에서 실행")
        cmd_btn.setFixedHeight(26)
        cmd_btn.setToolTip(
            "현재 스크립트를 임시 .py로 저장 후\n"
            "cmd 창에서 직접 실행합니다.")
        cmd_btn.setStyleSheet(
            "QPushButton{background:#1a2a1a;color:#8fa;"
            "border:1px solid #2a6a2a;border-radius:4px;font-size:10px;}"
            "QPushButton:hover{background:#1e3a1e;}")
        cmd_btn.clicked.connect(self._on_run_cmd)

        env_row.addWidget(env_btn); env_row.addWidget(cmd_btn)
        env_row.addStretch()
        ed_l.addLayout(opt_row)
        ed_l.addLayout(env_row)

        # 에디터
        self._editor = QPlainTextEdit()
        self._editor.setPlaceholderText(
            "# Python 스크립트를 입력하세요.\n"
            "# 사용 가능한 전역: engine, kernel, log, sys, os, np, cv2\n"
            "# 유틸: pip_install('패키지'), pip_list(), env_info()\n"
            "# 예: engine.start_recording()\n"
            "#     kernel.wait(5)\n"
            "#     engine.stop_recording()")
        self._editor.setStyleSheet(
            "QPlainTextEdit{background:#06060e;color:#9fc;"
            "border:1px solid #1a3a1a;font-family:Consolas,Courier New,monospace;"
            "font-size:11px;}")
        self._editor.setMinimumHeight(160)
        self._editor.textChanged.connect(self._on_code_changed)
        ed_l.addWidget(self._editor, 1)

        # 단일 실행 버튼
        run_row = QHBoxLayout(); run_row.setSpacing(6)
        self._run_one_btn = QPushButton("▶  이 스크립트만 실행")
        self._run_one_btn.setFixedHeight(30)
        self._run_one_btn.setStyleSheet(
            "QPushButton{background:#1a3a4a;color:#7be0e0;"
            "border:1px solid #2a6a7a;border-radius:4px;"
            "font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#22505a;}"
            "QPushButton:disabled{background:#0a1a1a;color:#3a5a5a;}")
        self._run_one_btn.clicked.connect(self._on_run_one)
        run_row.addWidget(self._run_one_btn)
        run_row.addStretch()
        self._run_status = QLabel("● 대기")
        self._run_status.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")
        run_row.addWidget(self._run_status)
        ed_l.addLayout(run_row)
        v.addWidget(ed_grp, 1)

        # ── 실행 로그 ─────────────────────────────────────────────────────────
        log_grp = QGroupBox("📋 커널 실행 로그")
        log_l = QVBoxLayout(log_grp); log_l.setSpacing(4)

        # ── [수정1] 오버레이 체크박스 행 ────────────────────────────────────
        ov_row = QHBoxLayout(); ov_row.setSpacing(8)
        ov_lbl = QLabel("로그 오버레이:"); ov_lbl.setStyleSheet("color:#7bc8e0;font-size:10px;")
        self._ov_cam_chk = QCheckBox("웹캠")
        self._ov_scr_chk = QCheckBox("디스플레이")
        for chk in (self._ov_cam_chk, self._ov_scr_chk):
            chk.setStyleSheet(
                "QCheckBox{color:#7bc8e0;font-size:10px;}"
                "QCheckBox::indicator{width:12px;height:12px;}")
        ov_row.addWidget(ov_lbl)
        ov_row.addWidget(self._ov_cam_chk)
        ov_row.addWidget(self._ov_scr_chk)
        ov_row.addStretch()
        log_l.addLayout(ov_row)

        def _ov_toggle():
            eng = getattr(self._kernel, '_engine', None)
            if eng is None: return
            eng.kernel_log_overlay_on_cam = self._ov_cam_chk.isChecked()
            eng.kernel_log_overlay_on_scr = self._ov_scr_chk.isChecked()
        self._ov_cam_chk.toggled.connect(lambda _: _ov_toggle())
        self._ov_scr_chk.toggled.connect(lambda _: _ov_toggle())
        self._log_txt = QPlainTextEdit()
        self._log_txt.setReadOnly(True)
        self._log_txt.setMinimumHeight(160)   # 고정 대신 최소 높이 — 창 크기에 따라 확장
        self._log_txt.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding)
        # ★ 메모리 누수 수정: 화면은 2000줄 제한 (내보내기는 _log_lines deque 사용)
        self._log_txt.setMaximumBlockCount(2000)
        self._log_txt.setStyleSheet(
            "QPlainTextEdit{background:#030308;color:#7bc8e0;"
            "font-family:Consolas,Courier New,monospace;font-size:10px;"
            "border:1px solid #0a1a2a;}")
        # 스마트 스크롤: 사용자가 위로 스크롤하면 자동 스크롤 일시 정지
        self._log_txt.verticalScrollBar().valueChanged.connect(
            self._on_log_scroll_changed)

        log_btn_row = QHBoxLayout(); log_btn_row.setSpacing(6)

        clr_btn = QPushButton("🗑 로그 지우기"); clr_btn.setFixedHeight(22)
        clr_btn.setStyleSheet(
            "QPushButton{background:#0a0a1a;color:#556;"
            "border:1px solid #1a1a2a;border-radius:3px;font-size:10px;}")
        clr_btn.clicked.connect(self._on_clear_log)

        # 로그 내보내기 버튼 (신규)
        self._export_log_btn = QPushButton("📋 로그 내보내기")
        self._export_log_btn.setFixedHeight(22)
        self._export_log_btn.setToolTip(
            "커널 실행 로그 전체를 텍스트 파일로 저장 후 메모장으로 열기")
        self._export_log_btn.setStyleSheet(
            "QPushButton{background:#0a1a2a;color:#7bc8e0;"
            "border:1px solid #1a3a5a;border-radius:3px;font-size:10px;}"
            "QPushButton:hover{background:#0d2035;}")
        self._export_log_btn.clicked.connect(self._on_export_log)

        # 결과 내보내기 버튼 (기존)
        self._export_result_btn = QPushButton("📄 결과 내보내기")
        self._export_result_btn.setFixedHeight(22)
        self._export_result_btn.setToolTip(
            "커널 실행 결과(PASS/FAIL)를 텍스트 파일로 저장 후 메모장으로 열기")
        self._export_result_btn.setStyleSheet(
            "QPushButton{background:#0a1a2a;color:#4a9ad4;"
            "border:1px solid #1a3a5a;border-radius:3px;font-size:10px;}"
            "QPushButton:hover{background:#0d2035;}")
        self._export_result_btn.clicked.connect(self._on_export_result)

        log_btn_row.addStretch()
        log_btn_row.addWidget(self._export_log_btn)
        log_btn_row.addWidget(self._export_result_btn)
        log_btn_row.addWidget(clr_btn)

        log_l.addWidget(self._log_txt)
        log_l.addLayout(log_btn_row)
        v.addWidget(log_grp)

        # 상태 폴링 타이머
        self._poll = QTimer(self); self._poll.timeout.connect(self._poll_status)
        self._poll.start(400)

    # ── 슬롯 위젯 ────────────────────────────────────────────────────────────
    def _rebuild_slots(self):
        # ★ 수정: 기존 row 위젯 완전 제거 — deleteLater()까지 호출
        while self._slot_lay.count():
            it = self._slot_lay.takeAt(0)
            w = it.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self._slot_widgets.clear()
        for i, sc in enumerate(self._kernel.scripts):
            row = self._make_slot_row(i, sc)
            self._slot_widgets.append(row)
            self._slot_lay.addWidget(row)
        self._slot_lay.addStretch()

    def _make_slot_row(self, idx: int, sc: KernelScript) -> QFrame:
        row = QFrame(); row.setFixedHeight(self._SLOT_H)
        row._idx = idx
        is_active = (idx == self._active_idx)
        self._style_slot(row, is_active, sc.enabled)
        row.setCursor(Qt.OpenHandCursor)
        hl = QHBoxLayout(row); hl.setContentsMargins(6,2,6,2); hl.setSpacing(6)
        grip = QLabel("⠿"); grip.setFixedWidth(16)
        grip.setStyleSheet("color:#4a5a8a;font-size:16px;background:transparent;")
        en_lbl = QLabel("●")
        en_lbl.setStyleSheet(
            f"color:{'#2ecc71' if sc.enabled else '#3a3a5a'};"
            "font-size:12px;background:transparent;")
        en_lbl.setFixedWidth(14)
        nm = QLabel(sc.title); nm.setStyleSheet("color:#ccd;font-size:11px;background:transparent;")
        rep_lbl = QLabel(f"×{sc.repeat}" if sc.repeat > 0 else "×∞")
        rep_lbl.setStyleSheet("color:#556;font-size:10px;background:transparent;min-width:30px;")
        hl.addWidget(grip); hl.addWidget(en_lbl); hl.addWidget(nm, 1); hl.addWidget(rep_lbl)
        row.mousePressEvent   = lambda e, i=idx, r=row: self._slot_press(e, i, r)
        row.mouseMoveEvent    = lambda e, r=row: self._slot_move(e, r)
        row.mouseReleaseEvent = lambda e, r=row: self._slot_release(e, r)
        row.mouseDoubleClickEvent = lambda e, i=idx: self._select_slot(i)
        return row

    def _style_slot(self, row, active: bool, enabled: bool):
        c = "#0d1a2a" if active else self._BG
        border = "#3a7aaa" if active else ("#1a1a3a" if enabled else "#0d0d1a")
        row.setStyleSheet(
            f"QFrame{{background:{c};border:1px solid {border};border-radius:4px;}}")

    def _select_slot(self, idx: int):
        self._active_idx = idx
        self._rebuild_slots()
        if 0 <= idx < len(self._kernel.scripts):
            sc = self._kernel.scripts[idx]
            self._editor.blockSignals(True)
            self._editor.setPlainText(sc.code)
            self._editor.blockSignals(False)
            self._enabled_chk.blockSignals(True)
            self._enabled_chk.setChecked(sc.enabled)
            self._enabled_chk.blockSignals(False)
            self._rep_spin.blockSignals(True)
            self._rep_spin.setValue(sc.repeat)
            self._rep_spin.blockSignals(False)

    # ── 드래그앤드랍 순서변경 ────────────────────────────────────────────────
    def _slot_press(self, e, idx, row):
        if e.button() == Qt.LeftButton:
            self._drag_idx   = idx
            self._drag_start = e.pos()
            self._dragging   = False
            self._select_slot(idx)

    def _slot_move(self, e, row):
        if self._drag_idx >= 0 and not self._dragging:
            if (e.pos() - self._drag_start).manhattanLength() > 6:
                self._dragging = True
                row.setStyleSheet(
                    f"QFrame{{background:{self._BG_DG};"
                    "border:1px solid #5a7aaa;border-radius:4px;}}")
                row.setCursor(Qt.ClosedHandCursor)

    def _slot_release(self, e, row):
        if self._dragging and self._drag_idx >= 0:
            gpos = row.mapToGlobal(e.pos())
            tgt  = self._find_slot_at_global(gpos)
            sc   = self._kernel.scripts
            if 0 <= tgt < len(sc) and tgt != self._drag_idx:
                sc.insert(tgt, sc.pop(self._drag_idx))
                self._active_idx = tgt
                self._rebuild_slots()
        self._drag_idx = -1; self._dragging = False

    def _find_slot_at_global(self, gpos: QPoint) -> int:
        for i, row in enumerate(self._slot_widgets):
            local = row.mapFromGlobal(gpos)
            if row.rect().contains(local): return i
        return -1

    # ── 슬롯 CRUD ────────────────────────────────────────────────────────────
    def _add_script(self):
        """슬롯 추가 — 모달창 없이 즉시 생성, 이름은 더블클릭으로 편집."""
        n  = len(self._kernel.scripts) + 1
        sc = KernelScript(f"스크립트 {n}")
        self._kernel.scripts.append(sc)
        self._rebuild_slots()
        new_idx = len(self._kernel.scripts) - 1
        self._select_slot(new_idx)
        # 이름 편집 필요 시 ✏ 버튼 또는 더블클릭 사용 안내
        self._on_log(f"슬롯 추가: '{sc.title}'  — ✏ 버튼으로 이름 변경 가능")

    def _del_script(self):
        idx = self._active_idx
        if not 0 <= idx < len(self._kernel.scripts): return
        self._kernel.scripts.pop(idx)
        new_idx = max(0, idx - 1)
        self._active_idx = new_idx if self._kernel.scripts else -1
        self._rebuild_slots()
        if self._active_idx >= 0:
            self._select_slot(self._active_idx)
        else:
            self._editor.clear()

    def _rename_script(self):
        # ★ 중복 실행 방지 — 이미 다이얼로그가 열려있으면 무시
        if getattr(self, '_renaming', False):
            return
        idx = self._active_idx
        if not 0 <= idx < len(self._kernel.scripts): return
        self._renaming = True
        try:
            sc = self._kernel.scripts[idx]
            new_name, ok = QInputDialog.getText(
                self, "슬롯 이름 변경", "새 이름:", text=sc.title)
            if ok and new_name.strip():
                sc.title = new_name.strip()
                self._rebuild_slots()
        finally:
            self._renaming = False

    # ── 편집 동기화 ───────────────────────────────────────────────────────────
    def _on_code_changed(self):
        idx = self._active_idx
        if 0 <= idx < len(self._kernel.scripts):
            self._kernel.scripts[idx].code = self._editor.toPlainText()

    def _on_enabled_toggled(self, v: bool):
        idx = self._active_idx
        if 0 <= idx < len(self._kernel.scripts):
            self._kernel.scripts[idx].enabled = v
            self._rebuild_slots()

    def _on_rep_changed(self, v: int):
        idx = self._active_idx
        if 0 <= idx < len(self._kernel.scripts):
            self._kernel.scripts[idx].repeat = v

    # ── 파일 IO ───────────────────────────────────────────────────────────────
    # ── 인터프리터·브릿지·폴더 메서드 ──────────────────────────────────────
    def _on_browse_python(self):
        """python.exe 파일 선택 다이얼로그."""
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "python.exe 선택", "",
            "Python Executable (python.exe python3 python3.*);;All Files (*)")
        if path:
            self._py_ed.setText(path)
            self._kernel.python_exe = path
            self._on_log(f"[인터프리터] {path}")

    def _on_browse_folder(self):
        """폴더 선택 다이얼로그."""
        from PyQt5.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(
            self, ".py 파일 폴더 선택", "")
        if folder:
            self._watch_ed.setText(folder)
            self._kernel.watch_dir = folder

    def _on_load_folder(self):
        """
        지정 폴더 내 .py 파일을 모두 슬롯으로 가져오기.
        기존 슬롯과 이름이 겹치면 건너뜀.
        """
        folder = self._watch_ed.text().strip()
        if not folder or not os.path.isdir(folder):
            self._on_log("❌ 유효한 폴더 경로를 입력하세요."); return
        py_files = sorted(
            f for f in os.listdir(folder)
            if f.endswith('.py') and not f.startswith('_'))
        if not py_files:
            self._on_log(f"📁 .py 파일 없음: {folder}"); return

        existing = {sc.title for sc in self._kernel.scripts}
        added = 0
        for fname in py_files:
            title = os.path.splitext(fname)[0]
            if title in existing:
                self._on_log(f"  건너뜀 (이미 있음): {fname}"); continue
            fpath = os.path.join(folder, fname)
            try:
                with open(fpath, encoding='utf-8', errors='replace') as f:
                    code = f.read()
            except Exception as ex:
                self._on_log(f"  ❌ 읽기 실패 {fname}: {ex}"); continue
            sc = KernelScript(title=title, code=code)
            self._kernel.scripts.append(sc)
            existing.add(title)
            added += 1
        self._rebuild_slots()
        self._on_log(f"✅ {added}개 슬롯 추가 (폴더: {folder})")
        if self._kernel.scripts:
            self._select_slot(len(self._kernel.scripts) - 1)

    def _on_bridge_toggle(self):
        """브릿지 서버 시작/중단 토글."""
        if self._kernel._bridge_server and self._kernel._bridge_server.is_alive():
            self._kernel.stop_bridge()
            self._bridge_btn.setText("🔌 브릿지 서버 시작")
            self._bridge_status.setText("● 중단됨")
            self._bridge_status.setStyleSheet("color:#e74c3c;font-size:9px;")
            self._on_log("[Bridge] 서버 중단")
        else:
            self._kernel.start_bridge()
            self._bridge_btn.setText("🔴 브릿지 서버 중단")
            self._bridge_status.setText(f"● 실행 중 :{self._kernel.BRIDGE_PORT}")
            self._bridge_status.setStyleSheet("color:#2ecc71;font-size:9px;")
            self._on_log(f"[Bridge] 서버 시작 — localhost:{self._kernel.BRIDGE_PORT}")

    def _on_gen_bridge(self):
        """recorder_bridge.py 생성 후 폴더 열기."""
        path = self._kernel.generate_bridge_client()
        if path:
            self._on_log(f"✅ recorder_bridge.py 생성: {path}")
            open_folder(os.path.dirname(path))

    def _on_import(self):
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, ".py 파일 가져오기", "", "Python Files (*.py);;All Files (*)")
        if not path: return
        try:
            with open(path, encoding='utf-8') as f:
                code = f.read()
            if self._active_idx >= 0:
                self._kernel.scripts[self._active_idx].code = code
                sc_name = os.path.splitext(os.path.basename(path))[0]
                self._kernel.scripts[self._active_idx].title = sc_name
            self._editor.blockSignals(True)
            self._editor.setPlainText(code)
            self._editor.blockSignals(False)
            self._rebuild_slots()
            self._signals.status_message.emit(f"[Kernel] 가져오기: {path}")
        except Exception as ex:
            self._signals.status_message.emit(f"[Kernel] 가져오기 실패: {ex}")

    def _on_env_info(self):
        """Python 환경 정보를 로그 패널에 출력."""
        info = self._kernel.get_env_info()
        self._on_log("[환경 정보]\n" + info)

    def _on_run_cmd(self):
        """
        현재 스크립트를 임시 .py로 저장 후 CMD 창에서 직접 실행.
        EXE 배포 환경에서도 동일하게 동작.
        """
        self._save_current()
        idx = self._active_idx
        if not 0 <= idx < len(self._kernel.scripts):
            self._on_log("❌ 실행할 스크립트를 선택하세요.")
            return
        sc   = self._kernel.scripts[idx]
        base = self._kernel._engine.base_dir
        os.makedirs(base, exist_ok=True)
        safe = re.sub(r'[\\/:*?"<>|]', '_', sc.title)
        tmp_path = os.path.join(base, f"_kernel_{safe}.py")

        # 헤더 주입: engine/kernel 없이 독립 실행 가능한 래퍼
        header = f"""\
# -*- coding: utf-8 -*-
# 자동 생성된 커널 스크립트 실행 파일 — {sc.title}
# 실행: python "{tmp_path}"
import sys, os, time, datetime, json, re, threading, platform, subprocess

# ── 더미 kernel/engine (CMD 독립 실행 시) ────────────────────────────
class _DummyKernel:
    def is_stopped(self): return False
    def wait(self, s): time.sleep(s)
    def set_tc_result(self, r): print(f"[TC] {{r}}")
    def read_roi_value(self, *a, **kw): return None
    def get_roi_avg(self, *a, **kw): return None
    def read_roi_text(self, *a, **kw): return ""
    def read_all_roi(self, source="screen"): return {{}}
    def read_roi_number(self, *a, **kw): return None
    def get_roi_last_text(self, *a, **kw): return ""
    def roi_text_equals(self, *a, **kw): return False
    def roi_text_contains(self, *a, **kw): return False
    def roi_number_equals(self, *a, **kw): return False
    def roi_number_compare(self, *a, **kw): return False
    def roi_match(self, *a, **kw): return False
    def emit_status(self, m): print(f"[STATUS] {{m}}")
    def power_connect(self, port, baudrate=9600): print(f"[PWR] connect {{port}}@{{baudrate}}"); return False
    def power_disconnect(self): print("[PWR] disconnect")
    def power_on(self, ch): print(f"[PWR] ON {{ch}}"); return False
    def power_off(self, ch): print(f"[PWR] OFF {{ch}}"); return False
    def power_all_on(self, delay=0.3): print("[PWR] ALL ON"); return False
    def power_all_off(self): print("[PWR] ALL OFF"); return False
    def power_send(self, cmd): print(f"[PWR] send {{cmd!r}}"); return False
    def power_read(self, timeout=0.2): return ""
    def power_list_ports(self): return []
    @property
    def power_connected(self): return False

class _DummyEngine:
    recording = False
    def start_recording(self): print("[ENG] start_recording")
    def stop_recording(self): print("[ENG] stop_recording")
    def save_manual_clip(self): print("[ENG] save_manual_clip"); return True
    def capture_frame(self, src="screen", tc_tag=""): print(f"[ENG] capture {{src}} {{tc_tag}}"); return ""
    def start_ac(self): print("[ENG] start_ac")
    def stop_ac(self): print("[ENG] stop_ac")
    def macro_start_run(self, *a, **kw): print("[ENG] macro_run")
    def macro_stop_run(self): print("[ENG] macro_stop")

def pip_install(*pkgs):
    for pkg in pkgs:
        subprocess.run([sys.executable, "-m", "pip", "install", pkg])

def pip_list():
    subprocess.run([sys.executable, "-m", "pip", "list"])

def env_info():
    print(f"Python {{sys.version}}")
    print(f"실행파일: {{sys.executable}}")

kernel = _DummyKernel()
engine = _DummyEngine()
log    = print

try: import numpy as np
except ImportError: np = None
try: import cv2
except ImportError: cv2 = None

# ── 사용자 스크립트 ────────────────────────────────────────────────────
if __name__ == "__main__":
"""
        # 사용자 코드를 들여쓰기하여 if __name__ 블록 안에 삽입
        indented = "\n".join(
            "    " + ln for ln in sc.code.split("\n"))
        full_code = header + indented + "\n"

        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write(full_code)
            self._on_log(f"✅ 스크립트 저장: {tmp_path}")

            # 플랫폼별 CMD 실행
            if platform.system() == "Windows":
                subprocess.Popen(
                    f'start cmd /K "{sys.executable}" "{tmp_path}"',
                    shell=True)
            elif platform.system() == "Darwin":
                subprocess.Popen(
                    ["open", "-a", "Terminal",
                     sys.executable, tmp_path])
            else:
                subprocess.Popen(
                    ["x-terminal-emulator", "-e",
                     sys.executable, tmp_path])
            self._on_log("✅ CMD 창에서 스크립트 실행 시작")
        except Exception as ex:
            self._on_log(f"❌ CMD 실행 실패: {ex}")

    def _on_export(self):
        from PyQt5.QtWidgets import QFileDialog
        idx = self._active_idx
        if not 0 <= idx < len(self._kernel.scripts): return
        sc = self._kernel.scripts[idx]
        safe = re.sub(r'[\\/:*?"<>|]', '_', sc.title)
        path, _ = QFileDialog.getSaveFileName(
            self, ".py 파일 내보내기",
            os.path.join(self._kernel._engine.base_dir, f"{safe}.py"),
            "Python Files (*.py);;All Files (*)")
        if not path: return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(sc.code)
            self._signals.status_message.emit(f"[Kernel] 내보내기: {path}")
            open_folder(os.path.dirname(path))
        except Exception as ex:
            self._signals.status_message.emit(f"[Kernel] 내보내기 실패: {ex}")

    def _on_export_result(self):
        """
        커널 실행 결과를 텍스트 파일로 저장 후 메모장으로 열기.
        결과가 없으면 안내 메시지 표시.
        """
        log = self._kernel.result_log
        if not log._entries:
            QMessageBox.information(
                self, "결과 없음",
                "아직 커널 실행 결과가 없습니다.\n"
                "스크립트를 실행한 후 사용하세요.")
            return

        base_dir = (getattr(self._kernel._engine, 'base_dir', None) or
                    os.path.join(os.path.expanduser("~"), "Desktop", "bltn_rec")).strip()
        # ★ [수정1] 저장 경로 폴더 트리를 kernel_logs 아래에도 반영
        _now_date = datetime.now().strftime("%Y%m%d")
        _parts = [base_dir, "kernel_logs"]
        _vt = (getattr(self._kernel._engine, 'vehicle_type', None) or '').strip()
        if _vt: _parts.append(_vt)
        _parts.append(_now_date)
        _tc = (getattr(self._kernel._engine, 'tc_id', None) or '').strip()
        if _tc: _parts.append(_tc)
        for _seg in (getattr(self._kernel._engine, 'extra_segments', None) or []):
            if _seg and _seg.strip(): _parts.append(_seg.strip())
        log_dir = os.path.join(*_parts)
        os.makedirs(log_dir, exist_ok=True)
        # 현재 실행 중이거나 마지막 실행된 스크립트 제목으로 고정 파일명
        _title = getattr(self._kernel, 'cur_title', '') or ''
        if not _title:
            # 마지막 기록된 스크립트 제목
            try: _title = self._kernel.result_log._entries[-1]['script']
            except Exception: _title = ''
        path     = log.open_in_notepad(log_dir, script_title=_title)

        if path:
            self._signals.status_message.emit(
                f"[Kernel] 결과 저장: {path}")
            self._on_log(f"📄 결과 내보내기: {path}")
        else:
            QMessageBox.warning(self, "저장 실패",
                                "결과 파일 저장에 실패했습니다.")

    # ── 실행 제어 ─────────────────────────────────────────────────────────────
    def _on_run_all(self):
        if self._kernel.running: return
        self._save_current()
        self._kernel.start(list(self._kernel.scripts))

    def _on_run_one(self):
        idx = self._active_idx
        if not 0 <= idx < len(self._kernel.scripts): return
        self._save_current()
        sc = self._kernel.scripts[idx]
        self._kernel.start([sc])

    def _on_stop(self):
        self._kernel.stop()

    def _save_current(self):
        """편집 중인 내용 저장."""
        idx = self._active_idx
        if 0 <= idx < len(self._kernel.scripts):
            self._kernel.scripts[idx].code = self._editor.toPlainText()

    def _poll_status(self):
        running = self._kernel.running
        self._run_all_btn.setEnabled(not running)
        self._run_one_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        if running:
            self._run_status.setText(f"● 실행 중: {self._kernel.cur_title}")
            self._run_status.setStyleSheet("color:#2ecc71;font-size:11px;font-weight:bold;")
        else:
            self._run_status.setText("● 대기")
            self._run_status.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")

    def _on_status_for_clear(self, msg: str):
        """status_message 수신 — 로그 휘발 명령(__CLEAR_KERNEL_LOG__) 처리."""
        if msg == "__CLEAR_KERNEL_LOG__":
            self._log_lines.clear()
            self._log_txt.clear()
            self._log_auto_scroll = True
            # ── [수정1] 오버레이 라인도 함께 초기화 ─────────────────────
            eng = getattr(self._kernel, '_engine', None)
            if eng is not None and hasattr(eng, 'kernel_log_overlay_lines'):
                with eng._kernel_log_lock:
                    eng.kernel_log_overlay_lines.clear()

    def _on_log(self, msg: str):
        """KernelEngine 로그 콜백 (daemon 스레드에서 호출).
        ★ 여기서는 버퍼 누적 + 시그널 emit만 수행.
           UI(appendPlainText)는 절대 직접 호출하지 않음 → 크로스스레드 crash 방지.
        """
        self._log_lines.append(msg)          # 내보내기용 전체 히스토리 (deque maxlen 자동 제거)
        # ★ 크로스스레드 안전: 시그널 emit → Qt가 메인스레드 큐에 자동 삽입
        self._signals.kernel_log.emit(msg)

    def _on_log_safe(self, msg: str):
        """kernel_log 시그널 수신 → 메인스레드에서만 실행되는 UI 갱신."""
        self._log_txt.appendPlainText(msg)
        if self._log_auto_scroll:
            sb = self._log_txt.verticalScrollBar()
            sb.setValue(sb.maximum())
        # ── [수정1] 커널 로그 오버레이 동기화 ─────────────────────────────
        # KernelEngine이 Engine 참조를 갖고 있으면 overlay_lines 갱신
        eng = getattr(self._kernel, '_engine', None)
        if eng is not None and hasattr(eng, 'kernel_log_overlay_lines'):
            with eng._kernel_log_lock:
                eng.kernel_log_overlay_lines.append(msg)
                # maxlen 100줄로 제한 (화면 넘침 방지)
                if len(eng.kernel_log_overlay_lines) > 100:
                    eng.kernel_log_overlay_lines = eng.kernel_log_overlay_lines[-100:]

    def _on_log_scroll_changed(self, value: int):
        """스크롤바 값 변경 감지 — 맨 아래 근처면 자동 스크롤 ON, 위로 올리면 OFF."""
        sb = self._log_txt.verticalScrollBar()
        # 맨 아래에서 3줄(약 40px) 이내면 자동 스크롤 재활성화
        self._log_auto_scroll = (sb.maximum() - value) <= 40

    def _on_clear_log(self):
        """로그 화면 + 히스토리 버퍼 모두 초기화."""
        self._log_txt.clear()
        self._log_lines.clear()
        self._log_auto_scroll = True
        # ── [수정1] 오버레이 라인도 함께 초기화 ─────────────────────────
        eng = getattr(self._kernel, '_engine', None)
        if eng is not None and hasattr(eng, 'kernel_log_overlay_lines'):
            with eng._kernel_log_lock:
                eng.kernel_log_overlay_lines.clear()

    def _on_export_log(self):
        """커널 실행 로그 전체를 .txt 파일로 저장 후 메모장으로 열기."""
        if not self._log_lines:
            QMessageBox.information(
                self, "로그 없음",
                "아직 커널 실행 로그가 없습니다.\n"
                "스크립트를 실행한 후 사용하세요.")
            return
        _bd = (getattr(self._kernel._engine, 'base_dir', None) or '').strip()
        if not _bd:
            _bd = os.path.join(os.path.expanduser('~'), 'Desktop', 'bltn_rec')
        # ★ [수정1] _auto_export와 동일한 폴더 트리 경로 사용
        _now_date = datetime.now().strftime("%Y%m%d")
        _parts = [_bd, 'kernel_logs']
        _vt = (getattr(self._kernel._engine, 'vehicle_type', None) or '').strip()
        if _vt: _parts.append(_vt)
        _parts.append(_now_date)
        _tc = (getattr(self._kernel._engine, 'tc_id', None) or '').strip()
        if _tc: _parts.append(_tc)
        for _seg in (getattr(self._kernel._engine, 'extra_segments', None) or []):
            if _seg and _seg.strip(): _parts.append(_seg.strip())
        log_dir = os.path.join(*_parts)
        os.makedirs(log_dir, exist_ok=True)
        # 로그도 스크립트 제목 기반 고정 파일명으로 덮어쓰기
        _title = ''
        try: _title = self._kernel.result_log._entries[-1]['script']
        except Exception: pass
        if _title:
            path = self._kernel.result_log.log_path(log_dir, _title)
        else:
            ts   = datetime.now().strftime('%Y%m%d_%H%M%S')
            path = os.path.join(log_dir, f'kernel_log_{ts}.txt')
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(f'=== 커널 실행 로그  {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} ===\n')
                f.write('\n'.join(self._log_lines))
                f.write('\n')
            # 메모장으로 열기
            try:
                import subprocess as _sp
                _sp.Popen(['notepad.exe', path])
            except Exception:
                try:
                    import subprocess as _sp
                    _sp.Popen(['xdg-open', path])
                except Exception:
                    pass
            self._on_log(f'📋 로그 내보내기: {path}')
            self._signals.status_message.emit(f'[Kernel] 로그 저장: {path}')
        except Exception as ex:
            QMessageBox.warning(self, '저장 실패',
                                f'로그 파일 저장에 실패했습니다.\n{ex}')

    # ── DB 저장/복원 ─────────────────────────────────────────────────────────
    def get_scripts_data(self) -> list:
        self._save_current()
        return self._kernel.get_scripts_data()

    def set_scripts_data(self, data: list):
        # ★ 수정: 에디터 textChanged 시그널 차단 후 슬롯 교체
        #   _select_slot 내부의 setPlainText가 textChanged를 일으켜
        #   _save_current가 엉뚱한 슬롯에 덮어쓰는 것을 방지
        self._editor.blockSignals(True)
        try:
            self._kernel.set_scripts_data(data)
            self._active_idx = -1   # 초기화 후 재선택
            self._rebuild_slots()
            if self._kernel.scripts:
                self._select_slot(0)
        finally:
            self._editor.blockSignals(False)


# =============================================================================
#  KernelDialog  — 스크립트 커널 NonModal 싱글턴 창 (v5.6: 기능 패널에서 분리)
# =============================================================================
class KernelDialog(QDialog):
    """
    스크립트 커널을 기능 패널에서 분리해 독립 창으로 제공 (v5.6).
    NonModal 싱글턴 — 다른 창 조작 중에도 열려 있음.
    최상위 고정 ON/OFF 토글 포함.
    """
    _instance = None

    def __init__(self, kernel, signals, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🧠  스크립트 커널")
        self.setWindowModality(Qt.NonModal)
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint |
            Qt.WindowMinimizeButtonHint |
            Qt.WindowMaximizeButtonHint)
        self.resize(820, 680)
        self.setStyleSheet(
            "QDialog{background:#0d0d1e;color:#dde;}"
            "QPushButton{background:#1a1a2a;color:#9ab;"
            "border:1px solid #2a2a4a;border-radius:4px;"
            "padding:4px 10px;font-size:11px;}"
            "QPushButton:hover{background:#22224a;}")

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        # ── 상단: 창 제목 + 최상위 토글 ────────────────────────────────
        top_row = QHBoxLayout()
        hdr = QLabel("🧠  스크립트 커널  —  Python 자동화 스크립트")
        hdr.setStyleSheet(
            "color:#e080ff;font-size:12px;font-weight:bold;"
            "padding:4px 8px;background:#0d0818;"
            "border:1px solid #3a1a5a;border-radius:4px;")
        top_row.addWidget(hdr, 1)

        self._pin_btn = QPushButton("📌 최상위 OFF")
        self._pin_btn.setCheckable(True)
        self._pin_btn.setFixedHeight(26)
        self._pin_btn.setStyleSheet(
            "QPushButton{background:#1a1a2a;color:#888;"
            "border:1px solid #2a2a3a;border-radius:4px;font-size:10px;}"
            "QPushButton:checked{background:#1a0a2e;color:#e080ff;"
            "border-color:#6a2a9a;}"
            "QPushButton:hover{background:#22224a;}")
        self._pin_btn.toggled.connect(self._on_pin_toggled)
        top_row.addWidget(self._pin_btn)
        v.addLayout(top_row)

        # ── KernelPanel 임베드 ───────────────────────────────────────────
        self._kp = KernelPanel(kernel, signals, self)
        v.addWidget(self._kp, 1)

    def _on_pin_toggled(self, on: bool):
        """최상위 고정 ON/OFF."""
        flags = self.windowFlags()
        if on:
            self.setWindowFlags(flags | Qt.WindowStaysOnTopHint)
            self._pin_btn.setText("📌 최상위 ON")
        else:
            self.setWindowFlags(flags & ~Qt.WindowStaysOnTopHint)
            self._pin_btn.setText("📌 최상위 OFF")
        self.show()

    def closeEvent(self, e):
        KernelDialog._instance = None
        super().closeEvent(e)

    @classmethod
    def show_or_raise(cls, parent, kernel, signals):
        if cls._instance is None:
            cls._instance = cls(kernel, signals, parent)
        cls._instance.show()
        cls._instance.raise_()
        cls._instance.activateWindow()


# =============================================================================
#  PowerController  — 시리얼 전원 제어 캡슐화 (KernelEngine / UI 공용)
# =============================================================================
