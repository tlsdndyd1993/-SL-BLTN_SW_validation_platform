# -*- coding: utf-8 -*-
"""
ui_swc/path_settings_panel.py
저장 경로 설정 패널
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

from engine_swc.settings_db import SettingsDB
class PathSettingsPanel(QWidget):
    """
    저장 경로 설정 v2.8
    ─ T/C 검증 목적 (기능별) — 수정:
        · Recording/블랙아웃/캡처에도 수동녹화처럼 TC 검증 UI 반영
        · '(PASS)|(FAIL) 반영 할 폴더' 텍스트
    ─ 기능별 저장경로 소기능(use_custom_path_*) 삭제
        · TC ON → 구성 경로 사용, TC OFF → 기본 경로
    ─ TC-ID 번호형식: RadioButton (개별|범위 단일 선택)
    ─ 범위 선택 시 끝번호 항상 시작번호+1 이상 강제
    """

    class _PathDragList(QWidget):
        """경로 세그먼트 드래그앤드랍 순서변경."""
        order_changed = pyqtSignal()
        _ROW_H = 32; _BG = "#0d0d1e"; _BG_HV = "#1a1a3a"; _BG_DG = "#1a2a4a"

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setStyleSheet(f"background:{self._BG};")
            self._items: list = []
            self._lay = QVBoxLayout(self)
            self._lay.setContentsMargins(2,2,2,2); self._lay.setSpacing(3)
            self._drag_idx = -1; self._drag_start = QPoint(); self._dragging = False

        def set_items(self, items: list):
            while self._lay.count():
                it=self._lay.takeAt(0)
                if it.widget(): it.widget().setParent(None)
            self._items.clear()
            for item in items:
                row = self._make_row(item); item['widget'] = row
                self._items.append(item); self._lay.addWidget(row)
            self._lay.addStretch()

        def _make_row(self, item: dict) -> QWidget:
            row = QWidget(); row.setFixedHeight(self._ROW_H)
            row.setStyleSheet(f"QWidget{{background:{self._BG};border:1px solid #1a1a3a;border-radius:4px;}}")
            row.setCursor(Qt.OpenHandCursor)
            hl = QHBoxLayout(row); hl.setContentsMargins(6,2,6,2); hl.setSpacing(6)
            grip = QLabel("⠿"); grip.setFixedWidth(16)
            grip.setStyleSheet("color:#4a5a8a;font-size:18px;")
            lbl = QLabel(item['label'])
            lbl.setStyleSheet("color:#ccd;font-size:11px;background:transparent;")
            hl.addWidget(grip); hl.addWidget(lbl, 1)
            return row

        def get_order(self) -> list:
            return [item['key'] for item in self._items]

        def _row_at_y(self, y):
            for i, item in enumerate(self._items):
                w = item.get('widget')
                if w and w.y() <= y < w.y() + w.height(): return i
            return -1

        def mousePressEvent(self, e):
            if e.button() == Qt.LeftButton:
                idx = self._row_at_y(e.pos().y())
                if idx >= 0: self._drag_idx = idx; self._drag_start = e.pos()
            super().mousePressEvent(e)

        def mouseMoveEvent(self, e):
            if self._drag_idx >= 0 and not self._dragging:
                if (e.pos()-self._drag_start).manhattanLength() > 6:
                    self._dragging = True
                    w = self._items[self._drag_idx].get('widget')
                    if w: w.setStyleSheet(f"QWidget{{background:{self._BG_DG};border:1px solid #5a7aaa;border-radius:4px;}}"); w.setCursor(Qt.ClosedHandCursor)
            super().mouseMoveEvent(e)

        def mouseReleaseEvent(self, e):
            if self._dragging and self._drag_idx >= 0:
                tgt = self._row_at_y(e.pos().y())
                if 0 <= tgt < len(self._items) and tgt != self._drag_idx:
                    self._items.insert(tgt, self._items.pop(self._drag_idx))
                    while self._lay.count():
                        it = self._lay.takeAt(0)
                        if it.widget(): it.widget().setParent(None)
                    for item in self._items:
                        row = self._make_row(item); item['widget'] = row; self._lay.addWidget(row)
                    self._lay.addStretch(); self.order_changed.emit()
                else:
                    w = self._items[self._drag_idx].get('widget')
                    if w: w.setStyleSheet(f"QWidget{{background:{self._BG};border:1px solid #1a1a3a;border-radius:4px;}}"); w.setCursor(Qt.OpenHandCursor)
            self._drag_idx = -1; self._dragging = False
            super().mouseReleaseEvent(e)

    # ── PathSettingsPanel 본체 ────────────────────────────────────────────────
    def __init__(self, engine: "CoreEngine", db: "SettingsDB", parent=None):
        super().__init__(parent)
        self.engine = engine; self.db = db
        self._seg_rows: list = []
        self._build()

    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,6); v.setSpacing(0)

        def _make_collapsible(title: str, color: str):
            btn = QPushButton(f"▼  {title}")
            btn.setCheckable(True); btn.setChecked(False); btn.setFixedHeight(30)
            btn.setStyleSheet(
                f"QPushButton{{background:#1a1a2e;color:{color};border:1px solid #2a2a4a;"
                "border-radius:4px;font-size:11px;font-weight:bold;text-align:left;"
                "padding:0 10px;margin-top:6px;}}"
                f"QPushButton:hover{{background:#22223a;}}"
                f"QPushButton:checked{{background:#0d0d1a;color:{color};}}")
            body = QWidget()
            body.setStyleSheet("QWidget{background:#0a0a1a;border:1px solid #1a1a3a;border-top:none;border-radius:0 0 4px 4px;}")
            bl = QVBoxLayout(body); bl.setContentsMargins(10,8,10,10); bl.setSpacing(6)
            body.setVisible(True)
            def _toggle(collapsed, b=body, bt=btn, t=title, col=color):
                body.setVisible(not collapsed)
                bt.setText(f"{'▶' if collapsed else '▼'}  {t}")
            btn.toggled.connect(_toggle)
            return btn, body, bl

        # ══ 1. T/C 검증 목적 ══════════════════════════════════════════════════
        tc_btn, tc_body, tc_l = _make_collapsible("🔬 T/C 검증 목적  (기능별 개별 활성화)", "#f0c040")
        v.addWidget(tc_btn); v.addWidget(tc_body)

        tc_hint = QLabel(
            "활성화된 기능 완료 후 PASS/FAIL 선택 → 아래 지정 폴더명 앞에 태그 삽입\n"
            "③ 선택 시: 녹화→Rec_폴더, 수동녹화→manual_clip폴더, 캡처→capture폴더, 블랙아웃→blackout폴더\n"
            "⚠ TC ON 시 아래 경로 구성 사용  |  TC OFF 시 기본 경로(날짜/기능폴더) 사용\n"
            "⚠ TC-ID를 먼저 입력하고 저장해야 T/C 검증 활성화 가능합니다.")
        tc_hint.setWordWrap(True); tc_hint.setStyleSheet("color:#888;font-size:9px;")
        tc_l.addWidget(tc_hint)

        _TC_ITEMS = [
            ("tc_rec_chk",      "tc_rec_enabled",      "⏺ 녹화",      "#27ae60"),
            ("tc_manual_chk",   "tc_manual_enabled",   "🎬 수동녹화",  "#e67e22"),
            ("tc_blackout_chk", "tc_blackout_enabled", "🔲 블랙아웃 판독",  "#e74c3c"),
            ("tc_capture_chk",  "tc_capture_enabled",  "📸 캡처",      "#3498db"),
        ]
        chk_grid = QGridLayout(); chk_grid.setSpacing(8)
        for i, (attr_name, engine_attr, label, color) in enumerate(_TC_ITEMS):
            chk = QCheckBox(label); chk.setChecked(False); chk.setMinimumHeight(28)
            chk.setStyleSheet(
                f"QCheckBox{{font-size:11px;font-weight:bold;color:{color};"
                "spacing:6px;padding:4px 8px;border:1px solid #2a2a3a;border-radius:5px;background:#0d0d1e;}}"
                f"QCheckBox:checked{{background:#0d1525;border-color:{color};}}"
                "QCheckBox:hover{background:#14142a;}")
            chk.toggled.connect(self._make_tc_toggle(engine_attr))
            setattr(self, attr_name, chk)
            chk_grid.addWidget(chk, i//2, i%2)
        tc_l.addLayout(chk_grid)

        # (PASS)|(FAIL) 반영 할 폴더 — 텍스트 수정
        folder_row = QHBoxLayout(); folder_row.setSpacing(8)
        folder_row.addWidget(QLabel("(PASS)|(FAIL) 반영 할 폴더:"))
        self.tc_folder_combo = QComboBox()
        self.tc_folder_combo.addItems([
            "① TC-ID 폴더  (기본 — 모든 기능 공통)",
            "② 차종_버전 폴더",
            "③ 각 기능 폴더  (Rec_ / manual_clip / capture / blackout)",
            "④ 직접 입력",
        ])
        self.tc_folder_combo.setStyleSheet(
            "QComboBox{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:3px;padding:2px 6px;font-size:11px;}")
        self.tc_folder_combo.currentIndexChanged.connect(self._on_tc_folder_changed)
        folder_row.addWidget(self.tc_folder_combo, 1)
        tc_l.addLayout(folder_row)
        self.tc_custom_folder_ed = QLineEdit()
        self.tc_custom_folder_ed.setPlaceholderText("폴더 절대경로 직접 입력")
        self.tc_custom_folder_ed.setVisible(False)
        self.tc_custom_folder_ed.setStyleSheet(
            "QLineEdit{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:3px;padding:3px 8px;font-size:11px;}")
        self.tc_custom_folder_ed.textChanged.connect(self._sync_tc_tag_target)
        tc_l.addWidget(self.tc_custom_folder_ed)

        # ══ 2. TC-ID 설정 ══════════════════════════════════════════════════════
        tc_id_btn, tc_id_body, tc_id_l = _make_collapsible("🔢 TC-ID 설정", "#7bc8e0")
        v.addWidget(tc_id_btn); v.addWidget(tc_id_body)

        prow = QHBoxLayout(); prow.setSpacing(8)
        prow.addWidget(QLabel("prefix"))
        self.tc_prefix_ed = QLineEdit()
        self.tc_prefix_ed.setPlaceholderText("예: BLTN_CAM_TC_3-")
        self.tc_prefix_ed.setMinimumHeight(28)
        self.tc_prefix_ed.setStyleSheet(
            "QLineEdit{background:#1a1a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:3px;padding:3px 8px;font-size:12px;}")
        self.tc_prefix_ed.textChanged.connect(self._upd_tc_id)
        prow.addWidget(self.tc_prefix_ed, 1)
        tc_id_l.addLayout(prow)

        # ★ 번호형식 — QButtonGroup RadioButton (단일 선택)
        mode_grp = QGroupBox("번호 형식")
        mode_grp.setStyleSheet("QGroupBox{font-size:10px;color:#9bc;border:1px solid #2a2a4a;"
                               "border-radius:4px;margin-top:12px;padding-top:8px;}")
        mode_l = QHBoxLayout(mode_grp); mode_l.setSpacing(16)
        self._mode_group = QButtonGroup(self)
        self.tc_mode_single = QRadioButton("개별  (예: -0041)")
        self.tc_mode_range  = QRadioButton("범위  (예: -0041~51)")
        self.tc_mode_single.setChecked(True)
        for rb in (self.tc_mode_single, self.tc_mode_range):
            rb.setStyleSheet("QRadioButton{font-size:11px;color:#ccd;spacing:5px;}")
            self._mode_group.addButton(rb)
            mode_l.addWidget(rb)
        mode_l.addStretch()
        self.tc_mode_single.toggled.connect(self._on_tc_mode_changed)
        tc_id_l.addWidget(mode_grp)

        # 번호 스핀박스
        num_grid = QGridLayout(); num_grid.setSpacing(8)
        self._tc_start_lbl = QLabel("번호"); num_grid.addWidget(self._tc_start_lbl, 0, 0)
        self.tc_start_spin = QSpinBox()
        self.tc_start_spin.setRange(0, 99999); self.tc_start_spin.setValue(1)
        self.tc_start_spin.setFixedWidth(88)
        self.tc_start_spin.setToolTip("개별 모드: 이 번호 사용  /  범위 모드: 시작 번호")
        self.tc_start_spin.setStyleSheet(
            "QSpinBox{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:3px;padding:3px;font-size:13px;font-weight:bold;}")
        self.tc_start_spin.valueChanged.connect(self._on_start_changed)
        num_grid.addWidget(self.tc_start_spin, 0, 1)
        num_grid.addWidget(QLabel("자릿수"), 0, 2)
        self.tc_digits_spin = QSpinBox()
        self.tc_digits_spin.setRange(1, 6); self.tc_digits_spin.setValue(4)
        self.tc_digits_spin.setFixedWidth(44); self.tc_digits_spin.setPrefix("0")
        self.tc_digits_spin.setStyleSheet(
            "QSpinBox{background:#1a1a3a;color:#888;border:1px solid #334;"
            "border-radius:3px;padding:2px;font-size:10px;}")
        self.tc_digits_spin.valueChanged.connect(self._upd_tc_id)
        num_grid.addWidget(self.tc_digits_spin, 0, 3)
        tc_id_l.addLayout(num_grid)

        # 끝 번호 행 (범위 모드에서만 표시)
        self._tc_end_row = QWidget(); self._tc_end_row.setStyleSheet("background:transparent;")
        end_row_l = QHBoxLayout(self._tc_end_row)
        end_row_l.setContentsMargins(0,0,0,0); end_row_l.setSpacing(8)
        end_row_l.addWidget(QLabel("끝 번호"))
        self.tc_end_spin = QSpinBox()
        self.tc_end_spin.setRange(0, 99999)
        # ★ 초기값 = 시작번호+1
        self.tc_end_spin.setValue(2)
        self.tc_end_spin.setFixedWidth(88)
        self.tc_end_spin.setToolTip("범위 모드 끝 번호 (항상 시작번호+1 이상)")
        self.tc_end_spin.setStyleSheet(
            "QSpinBox{background:#1a1a3a;color:#2ecc71;border:1px solid #2a6a2a;"
            "border-radius:3px;padding:3px;font-size:13px;font-weight:bold;}")
        self.tc_end_spin.valueChanged.connect(self._on_end_changed)
        end_hint = QLabel("  ← 시작번호보다 큰 값")
        end_hint.setStyleSheet("color:#556;font-size:9px;")
        end_row_l.addWidget(self.tc_end_spin); end_row_l.addWidget(end_hint); end_row_l.addStretch()
        self._tc_end_row.setVisible(False)
        tc_id_l.addWidget(self._tc_end_row)

        self.tc_preview_lbl = QLabel("TC-ID: —")
        self.tc_preview_lbl.setStyleSheet(
            "color:#f0c040;font-size:12px;font-weight:bold;"
            "background:#0d0d1e;border:1px solid #2a2a1a;border-radius:3px;padding:4px 8px;")
        tc_id_l.addWidget(self.tc_preview_lbl)

        # ══ 3. 저장 경로 구성 ══════════════════════════════════════════════════
        path_btn, path_body, path_l = _make_collapsible(
            "📁 저장 경로 구성  (드래그로 순서 변경)", "#7bc8e0")
        v.addWidget(path_btn); v.addWidget(path_body)

        path_hint = QLabel("⚠ T/C 검증 ON 시 아래 구성 경로 사용  /  OFF 시 기본 경로(bltn_rec/날짜/기능폴더)")
        path_hint.setStyleSheet("color:#888;font-size:9px;"); path_hint.setWordWrap(True)
        path_l.addWidget(path_hint)

        path_l.addWidget(QLabel("차종_버전:"))
        self.vehicle_ed = QLineEdit()
        self.vehicle_ed.setPlaceholderText("예: TK1_2541  또는  NX5_2633")
        self.vehicle_ed.setMinimumHeight(30)
        self.vehicle_ed.setStyleSheet(
            "QLineEdit{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:4px;padding:4px 10px;font-size:12px;font-weight:bold;}")
        self.vehicle_ed.textChanged.connect(
            lambda t: setattr(self.engine, 'vehicle_type', t.strip()))
        self.vehicle_ed.textChanged.connect(self._upd_preview)
        path_l.addWidget(self.vehicle_ed)

        self._drag_list = PathSettingsPanel._PathDragList(self)
        self._drag_list.order_changed.connect(self._upd_preview)
        path_l.addWidget(self._drag_list)

        self._extra_container = QWidget(); self._extra_container.setStyleSheet("background:transparent;")
        self._extra_lay = QVBoxLayout(self._extra_container)
        self._extra_lay.setContentsMargins(0,0,0,0); self._extra_lay.setSpacing(3)
        path_l.addWidget(self._extra_container)

        add_btn = QPushButton("＋  경로 항목 추가"); add_btn.setFixedHeight(28)
        add_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:5px;font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#1e3548;}")
        add_btn.clicked.connect(lambda: self._add_segment())
        path_l.addWidget(add_btn)

        # ══ 4. 경로 미리보기 ═══════════════════════════════════════════════════
        prev_btn, prev_body, prev_l = _make_collapsible("🔍 경로 미리보기", "#556")
        v.addWidget(prev_btn); v.addWidget(prev_body)
        self.preview_lbl = QLabel("")
        self.preview_lbl.setStyleSheet(
            "color:#f0c040;font-size:10px;font-family:monospace;"
            "background:#06060e;border:1px solid #3a3a1a;border-radius:4px;padding:8px 10px;")
        self.preview_lbl.setWordWrap(True)
        prev_l.addWidget(self.preview_lbl)

        # ══ 5. 저장/폴더 버튼 ═════════════════════════════════════════════════
        btn_row = QHBoxLayout(); btn_row.setSpacing(8); btn_row.setContentsMargins(0,8,0,0)
        save_btn = QPushButton("💾  경로 설정 저장")
        save_btn.setMinimumHeight(36)
        save_btn.setStyleSheet(
            "QPushButton{background:#1a3a2a;color:#afffcf;border:1px solid #2a8a5a;"
            "border-radius:5px;font-size:12px;font-weight:bold;}"
            "QPushButton:hover{background:#225a3a;}")
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn, 3)
        open_btn2 = QPushButton("📂 기본 폴더")
        open_btn2.setMinimumHeight(36)
        open_btn2.setStyleSheet(
            "QPushButton{background:#1a1a3a;color:#9ab;border:1px solid #2a2a5a;"
            "border-radius:5px;font-size:11px;}"
            "QPushButton:hover{background:#22224a;}")
        open_btn2.clicked.connect(lambda: open_folder(self.engine.base_dir))
        btn_row.addWidget(open_btn2, 1)
        v.addLayout(btn_row)

        self.result_lbl = QLabel("")
        self.result_lbl.setStyleSheet("font-size:11px;padding:3px 6px;border-radius:4px;")
        v.addWidget(self.result_lbl)
        v.addStretch()

        self._seg_rows = []
        self._rebuild_drag_list()
        self._upd_tc_id()
        self._upd_preview()

    # ── TC toggle 팩토리 ──────────────────────────────────────────────────────
    def _make_tc_toggle(self, engine_attr: str):
        def _toggle(checked: bool):
            if not checked:
                setattr(self.engine, engine_attr, False); return
            if not self.engine.tc_id or not self.engine.tc_id.strip():
                QMessageBox.warning(
                    self, "저장 경로 설정 필요",
                    "T/C 검증 목적을 활성화하려면\nTC-ID를 먼저 입력하고 저장하세요.",
                    QMessageBox.Ok)
                _attr_to_chk = {
                    'tc_rec_enabled': 'tc_rec_chk', 'tc_manual_enabled': 'tc_manual_chk',
                    'tc_blackout_enabled': 'tc_blackout_chk', 'tc_capture_enabled': 'tc_capture_chk',
                }
                chk_name = _attr_to_chk.get(engine_attr)
                if chk_name:
                    chk = getattr(self, chk_name, None)
                    if chk: chk.blockSignals(True); chk.setChecked(False); chk.blockSignals(False)
                return
            setattr(self.engine, engine_attr, True)
        return _toggle

    def _on_tc_folder_changed(self, idx):
        self.tc_custom_folder_ed.setVisible(idx == 3)
        # idx==2: 각 기능이 자신의 저장 폴더(save_dir)를 tag_dir로 직접 사용
        #         → tc_tag_target_dir을 비워서 각 기능이 자체 처리하도록 함
        self._sync_tc_tag_target()

    def get_tc_target_folder(self) -> str:
        """
        TC 태그 대상 폴더 경로 반환.
        이미 태그된 폴더가 존재하면 그 경로를 우선 반환
        (동일 TC에 계속 증적 누적을 위해).
        """
        idx = self.tc_folder_combo.currentIndex()
        eng = self.engine
        ts_date = datetime.now().strftime("%Y%m%d")

        if idx == 0:
            # TC-ID 폴더: base_dir/[vehicle_type]/날짜/tc_id
            tc = (eng.tc_id or "").strip()
            if not tc:
                return ""
            parts = [eng.base_dir]
            if eng.vehicle_type: parts.append(eng.vehicle_type)
            parts.append(ts_date)
            parent = os.path.join(*parts)
            # 기존 태그 폴더 우선 반환
            existing = eng._find_existing_tagged_folder(parent, tc)
            if existing:
                return existing
            return os.path.join(parent, tc)

        elif idx == 1:
            # 차종_버전 폴더: base_dir/vehicle_type
            vt = (eng.vehicle_type or "").strip()
            if not vt:
                return eng.base_dir
            parent = eng.base_dir
            existing = eng._find_existing_tagged_folder(parent, vt)
            if existing:
                return existing
            return os.path.join(parent, vt)

        elif idx == 2:
            # 각 기능 폴더: 빈 문자열 반환 → 각 기능이 자체 save_dir 사용
            return eng.output_dir or ""

        else:
            return self.tc_custom_folder_ed.text().strip()

    def _sync_tc_tag_target(self):
        idx = self.tc_folder_combo.currentIndex()
        # idx==2(Rec 폴더) : stale output_dir을 저장하지 않고 빈 문자열로 표시.
        # start_recording()에서 새 output_dir 확정 후 채워짐.
        self.engine.tc_folder_idx = idx
        if idx == 2:
            self.engine.tc_tag_target_dir = ''
        else:
            path = self.get_tc_target_folder()
            self.engine.tc_tag_target_dir = path

    def _on_tc_mode_changed(self, single: bool):
        """★ RadioButton 방식 — single=True: 개별, False: 범위."""
        self._tc_end_row.setVisible(not single)
        if not single:
            # 범위 모드: 끝 번호 = 시작번호+1 보장
            self._ensure_end_gt_start()
        self._upd_tc_id()

    def _on_start_changed(self, val: int):
        """시작번호 변경 시 끝번호가 시작번호 이하면 시작+1로 강제."""
        if not self.tc_mode_single.isChecked():
            self._ensure_end_gt_start()
        self._upd_tc_id()

    def _on_end_changed(self, val: int):
        """끝번호가 시작번호 이하면 시작+1로 강제."""
        self._ensure_end_gt_start()
        self._upd_tc_id()

    def _ensure_end_gt_start(self):
        start = self.tc_start_spin.value()
        end   = self.tc_end_spin.value()
        if end <= start:
            self.tc_end_spin.blockSignals(True)
            self.tc_end_spin.setValue(start + 1)
            self.tc_end_spin.blockSignals(False)

    def _upd_tc_id(self):
        prefix = self.tc_prefix_ed.text()
        start  = self.tc_start_spin.value()
        digits = self.tc_digits_spin.value()
        is_range = self.tc_mode_range.isChecked()
        if is_range:
            end = self.tc_end_spin.value()
            tc_val = f"{prefix}{start:0{digits}d}~{end}"
        else:
            tc_val = f"{prefix}{start:0{digits}d}"
        self.tc_preview_lbl.setText(f"TC-ID: {tc_val}")
        # ★ 수정: TC-ID 변경 시 이전 녹화의 tc_tag_target_dir 잔재 초기화
        if self.engine.tc_id != tc_val:
            self.engine.tc_tag_target_dir = ""
        self.engine.tc_id = tc_val
        self._upd_preview()
        self._sync_tc_tag_target()
        # ★ 번호 변경 시 0.5초 디바운스 자동 저장
        #   (휠 스크롤로 급격히 변경 시 매번 저장하면 과부하 방지)
        if not hasattr(self, "_autosave_timer"):
            from PyQt5.QtCore import QTimer as _QT
            self._autosave_timer = _QT(self)
            self._autosave_timer.setSingleShot(True)
            self._autosave_timer.timeout.connect(self._autosave_path)
        self._autosave_timer.start(500)   # 500ms 디바운스

    def _autosave_path(self):
        """TC 번호 변경 후 0.5초 디바운스 자동 저장."""
        self._save()
        self.result_lbl.setText("💾 자동 저장")
        self.result_lbl.setStyleSheet("color:#7bc8e0;font-size:11px;")
        from PyQt5.QtCore import QTimer as _QT
        _QT.singleShot(1500, lambda: self.result_lbl.setText(""))

    # ── 동적 경로 항목 ────────────────────────────────────────────────────────
    def _rebuild_drag_list(self):
        items = []
        vt = self.engine.vehicle_type or '미입력'
        tc = self.engine.tc_id        or '미설정'
        items.append({'key': '__vehicle__', 'label': f"🚗 차종_버전  [{vt}]",   'fixed': False})
        items.append({'key': '__date__',    'label': '📅 YYYYMMDD  (자동)',     'fixed': False})
        items.append({'key': '__tcid__',    'label': f"🔢 TC-ID  [{tc}]",       'fixed': False})
        for i, seg in enumerate(self._seg_rows):
            items.append({'key': f'seg_{i}', 'label': f"➕ {seg.get('val','') or '(비어있음)'}", 'fixed': False})
        items.append({'key': '__suffix__', 'label': '📁 Rec_HHMMSS / blackout / … (자동)', 'fixed': False})
        self._drag_list.set_items(items)

    def _add_segment(self, value: str = ""):
        idx   = len(self._seg_rows)
        row_w = QWidget(); row_w.setStyleSheet("background:transparent;")
        row_l = QHBoxLayout(row_w); row_l.setContentsMargins(0,0,0,0); row_l.setSpacing(5)
        num_lbl = QLabel(f"항목 {idx+1}:"); num_lbl.setStyleSheet("color:#aaa;font-size:10px;min-width:46px;")
        ed = QLineEdit(str(value) if value else "")
        ed.setPlaceholderText("추가 폴더명 (예: Feature_A)")
        ed.setStyleSheet("QLineEdit{background:#1a1a3a;color:#ccd;border:1px solid #334;border-radius:3px;padding:3px 6px;font-size:11px;}")
        del_btn = QPushButton("✕"); del_btn.setFixedSize(24, 24)
        del_btn.setStyleSheet("QPushButton{background:#3a1a1a;color:#f88;border:none;border-radius:3px;}QPushButton:hover{background:#7f2020;}")
        row_l.addWidget(num_lbl); row_l.addWidget(ed, 1); row_l.addWidget(del_btn)
        seg_entry = {'row_w': row_w, 'ed': ed, 'lbl': num_lbl, 'val': value}
        self._seg_rows.append(seg_entry)
        self._extra_lay.addWidget(row_w)
        ed.textChanged.connect(lambda t, s=seg_entry: self._on_seg_text(s, t))
        del_btn.clicked.connect(lambda _=False, s=seg_entry: self._del_segment(s))
        self._rebuild_drag_list(); self._upd_preview()

    def _on_seg_text(self, seg_entry, text):
        seg_entry['val'] = text; self._upd_extra_engine(); self._rebuild_drag_list(); self._upd_preview()

    def _del_segment(self, seg_entry):
        if seg_entry not in self._seg_rows: return
        self._seg_rows.remove(seg_entry)
        w = seg_entry['row_w']
        self._extra_lay.removeWidget(w); w.setParent(None); w.deleteLater()
        for i, s in enumerate(self._seg_rows): s['lbl'].setText(f"항목 {i+1}:")
        self._upd_extra_engine(); self._rebuild_drag_list(); self._upd_preview()

    def _upd_extra_engine(self):
        self.engine.extra_segments = [s['val'] for s in self._seg_rows if s['val'].strip()]

    def _upd_preview(self):
        vv  = self.vehicle_ed.text().strip() if hasattr(self, 'vehicle_ed') else ""
        tc  = self.engine.tc_id
        segs = [s['val'].strip() for s in self._seg_rows if s.get('val','').strip()]
        lines = []
        for label, tc_flag in [
            ("⏺ Recording (TC ON)",   True),
            ("⏺ Recording (TC OFF)",  False),
            ("🎬 수동녹화 (TC ON)",   True),
            ("⚡ 블랙아웃 (TC ON)",   True),
            ("📸 캡처 (TC ON)",       True),
        ]:
            if tc_flag:
                parts = ["bltn_rec"]
                if vv: parts.append(vv)
                parts.append("YYYYMMDD")
                if tc: parts.append(tc)
                parts.extend(segs)
            else:
                parts = ["bltn_rec", "YYYYMMDD"]
            lines.append(f"{label}: ~/{'/'.join(parts)}/<기능폴더>")
        if hasattr(self, 'preview_lbl'):
            self.preview_lbl.setText("경로 미리보기:\n" + "\n".join(lines))

    # ── 저장 / 복원 ───────────────────────────────────────────────────────────
    def _save(self):
        segs = [s['val'].strip() for s in self._seg_rows if s['val'].strip()]
        # root_dir: root_dir_ed 위젯 있으면 그 값, 없으면 engine.base_dir
        _root = ""
        if hasattr(self, 'root_dir_ed'):
            _root = self.root_dir_ed.text().strip()
        if not _root:
            _root = getattr(self.engine, 'base_dir', '')
        self.db.set_path_settings(
            vehicle_type  = self.vehicle_ed.text().strip(),
            tc_id         = self.engine.tc_id,
            extra_segments = segs,
            tc_rec        = self.tc_rec_chk.isChecked(),
            tc_manual     = self.tc_manual_chk.isChecked(),
            tc_blackout   = self.tc_blackout_chk.isChecked(),
            tc_capture    = self.tc_capture_chk.isChecked(),
            tc_folder_idx = self.tc_folder_combo.currentIndex(),
            root_dir      = _root,
        )
        # ★ 핵심 수정: DB 저장 직후 engine 플래그 즉시 동기화
        #   누락 시 체크박스 ON 후 저장해도 engine.tc_*_enabled=False 유지
        #   → _build_path()가 기본 경로 사용 → 사용자 지정 경로에 저장 안 됨
        self.engine.vehicle_type        = self.vehicle_ed.text().strip()
        self.engine.extra_segments      = segs
        if _root: self.engine.base_dir  = _root  # ★ base_dir 즉시 동기화
        self.engine.tc_rec_enabled      = self.tc_rec_chk.isChecked()
        self.engine.tc_manual_enabled   = self.tc_manual_chk.isChecked()
        self.engine.tc_blackout_enabled = self.tc_blackout_chk.isChecked()
        self.engine.tc_capture_enabled  = self.tc_capture_chk.isChecked()
        self._sync_tc_tag_target()
        self.result_lbl.setText("✅ 저장 완료")
        self.result_lbl.setStyleSheet("color:#2ecc71;font-size:11px;")
        QTimer.singleShot(2500, lambda: self.result_lbl.setText(""))

    def load_from_db(self):
        ps = self.db.get_path_settings()
        self.vehicle_ed.setText(ps.get('vehicle_type', ''))
        self.engine.vehicle_type = ps.get('vehicle_type', '')
        tc_raw = ps.get('tc_id', '')
        m_range = re.match(r'^(.*?)(\d+)~(\d+)$', tc_raw)
        m_single = re.match(r'^(.*?)(\d+)$', tc_raw)
        if m_range:
            self.tc_prefix_ed.setText(m_range.group(1))
            self.tc_start_spin.setValue(int(m_range.group(2)))
            self.tc_end_spin.setValue(int(m_range.group(3)))
            self.tc_digits_spin.setValue(max(1, len(m_range.group(2))))
            self.tc_mode_range.setChecked(True)
        elif m_single:
            self.tc_prefix_ed.setText(m_single.group(1))
            self.tc_start_spin.setValue(int(m_single.group(2)))
            self.tc_digits_spin.setValue(max(1, len(m_single.group(2))))
            self.tc_mode_single.setChecked(True)
        else:
            self.tc_prefix_ed.setText(tc_raw)
        for s in list(self._seg_rows): self._del_segment(s)
        for seg in ps.get('extra_segments', []):
            self._add_segment(seg)
        self.tc_rec_chk.setChecked(     ps.get('tc_rec',      False))
        self.tc_manual_chk.setChecked(  ps.get('tc_manual',   False))
        self.tc_blackout_chk.setChecked(ps.get('tc_blackout', False))
        self.tc_capture_chk.setChecked( ps.get('tc_capture',  False))
        self.engine.tc_rec_enabled      = ps.get('tc_rec',      False)
        self.engine.tc_manual_enabled   = ps.get('tc_manual',   False)
        self.engine.tc_blackout_enabled = ps.get('tc_blackout', False)
        self.engine.tc_capture_enabled  = ps.get('tc_capture',  False)
        # tc_folder_combo 복원
        fi = ps.get('tc_folder_idx', 0)
        self.tc_folder_combo.blockSignals(True)
        self.tc_folder_combo.setCurrentIndex(fi)
        self.tc_folder_combo.blockSignals(False)
        self.tc_custom_folder_ed.setVisible(fi == 3)
        self._upd_preview(); self._sync_tc_tag_target()
        # ★ engine.base_dir 반영
        root = ps.get('root_dir', '') or ''
        if root: self.engine.base_dir = root
        elif hasattr(self, 'root_dir_ed'):
            ui_root = self.root_dir_ed.text().strip()
            if ui_root: self.engine.base_dir = ui_root

