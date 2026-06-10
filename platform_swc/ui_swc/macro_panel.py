# -*- coding: utf-8 -*-
"""
ui_swc/macro_panel.py
입/출력 기록 매크로 패널
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

class MacroPanel(QWidget):
    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine = engine; self.signals = signals
        self._slots: list = []
        self._active_slot = 0
        signals.macro_step_rec.connect(self._on_step)
        # ★ engine에 패널 참조 주입 — macro_load_slot에서 _macro_slots 복구 시 사용
        self.engine._macro_panel_ref = self
        self._build()
        QTimer.singleShot(0, self._init_slots)

    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        # ── 슬롯 관리 ─────────────────────────────────────────────────────
        sg = QGroupBox("🗂 입/출력 장치 기록 슬롯"); sl = QVBoxLayout(sg); sl.setSpacing(5)
        sc = QHBoxLayout(); sc.setSpacing(6)
        sc.addWidget(QLabel("슬롯:"))
        self.slot_cb = QComboBox()
        self.slot_cb.setStyleSheet(
            "QComboBox{background:#0f2a1a;color:#afffcf;border:1px solid #2a8a5a;"
            "border-radius:3px;font-weight:bold;font-size:11px;min-width:100px;}")
        self.slot_cb.currentIndexChanged.connect(self._on_slot_changed)
        sc.addWidget(self.slot_cb, 1)
        for lbl, fn, c in [("＋","_add_slot","#1a4a2a"),
                            ("－","_del_slot","#3a1a1a"),
                            ("✏","_rename_slot","#1a2a3a")]:
            b = QPushButton(lbl); b.setFixedSize(28, 26)
            b.setStyleSheet(
                f"QPushButton{{background:{c};color:#ddd;"
                "border:1px solid #4a6a4a;border-radius:3px;}}")
            b.clicked.connect(getattr(self, fn)); sc.addWidget(b)
        sl.addLayout(sc)
        self.slot_info = QLabel("슬롯 1/1  |  0 스텝")
        self.slot_info.setStyleSheet(
            "color:#556;font-size:10px;font-family:monospace;")
        sl.addWidget(self.slot_info); v.addWidget(sg)

        # ── 기록 ──────────────────────────────────────────────────────────
        rg = QGroupBox("📍 이벤트 기록"); rl = QVBoxLayout(rg); rl.setSpacing(6)
        evt_row = QHBoxLayout(); evt_row.setSpacing(10)
        evt_row.addWidget(QLabel("기록 대상:"))
        self.rec_click_chk = QCheckBox("🖱 클릭")
        self.rec_drag_chk  = QCheckBox("↔ 드래그")
        self.rec_key_chk   = QCheckBox("⌨ 키보드")
        for chk in (self.rec_click_chk, self.rec_drag_chk, self.rec_key_chk):
            chk.setChecked(True)
            chk.setStyleSheet("QCheckBox{font-size:11px;color:#dde;spacing:4px;}")
            evt_row.addWidget(chk)
        evt_row.addStretch(); rl.addLayout(evt_row)

        rb = QHBoxLayout()
        self.rec_btn = QPushButton("⏺  기록 시작")
        self.rec_btn.setCheckable(True); self.rec_btn.setFixedHeight(32)
        self.rec_btn.setStyleSheet(
            "QPushButton{background:#1a4a2a;color:#afffcf;border:1px solid #2a8a5a;"
            "border-radius:5px;font-size:12px;font-weight:bold;}"
            "QPushButton:checked{background:#c0392b;color:#fff;border:2px solid #e74c3c;}")
        self.rec_btn.toggled.connect(self._on_rec_toggle)
        self.rec_st = QLabel("● 대기")
        self.rec_st.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")
        rb.addWidget(self.rec_btn, 1); rb.addWidget(self.rec_st)
        rl.addLayout(rb)
        self.last_evt_lbl = QLabel("마지막 이벤트: —")
        self.last_evt_lbl.setStyleSheet(
            "color:#f0c040;font-size:11px;font-family:monospace;")
        rl.addWidget(self.last_evt_lbl); v.addWidget(rg)

        # ── 스텝 테이블 ───────────────────────────────────────────────────
        tg = QGroupBox("📋 이벤트 스텝"); tv = QVBoxLayout(tg); tv.setSpacing(4)
        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(["#","타입","좌표/키","딜레이(s)","옵션"])
        hdr = self.tbl.horizontalHeader()
        for i, m in enumerate([QHeaderView.Fixed, QHeaderView.Fixed,
                                QHeaderView.Stretch, QHeaderView.Fixed, QHeaderView.Fixed]):
            hdr.setSectionResizeMode(i, m)
        self.tbl.setColumnWidth(0, 30); self.tbl.setColumnWidth(1, 58)
        self.tbl.setColumnWidth(3, 70); self.tbl.setColumnWidth(4, 60)
        self.tbl.setFixedHeight(180)
        self.tbl.setStyleSheet(
            "QTableWidget{background:#0a0a18;color:#ccc;font-size:10px;"
            "border:1px solid #1a2a3a;gridline-color:#1a2030;}"
            "QHeaderView::section{background:#0f1a2a;color:#7ab4d4;"
            "font-size:10px;border:none;padding:3px;}")
        self.tbl.itemChanged.connect(self._on_item_changed)
        tv.addWidget(self.tbl)

        tb = QHBoxLayout(); tb.setSpacing(4)
        for lbl, fn in [("↑","_step_up"),("↓","_step_dn")]:
            b = QPushButton(lbl); b.setFixedSize(28, 24)
            b.clicked.connect(getattr(self, fn)); tb.addWidget(b)
        tb.addStretch()
        for lbl, fn in [("선택 삭제","_del_step"),("전체 삭제","_clear_steps")]:
            b = QPushButton(lbl); b.setFixedHeight(24)
            b.clicked.connect(getattr(self, fn)); tb.addWidget(b)
        tv.addLayout(tb)

        br = QHBoxLayout(); br.addWidget(QLabel("일괄 딜레이:"))
        self.bulk_spin = _make_spinbox(0.0, 60, 0.5, 0.1, 2, "", 70)
        bb = QPushButton("적용"); bb.setFixedHeight(24)
        bb.clicked.connect(self._bulk_delay)
        br.addWidget(self.bulk_spin); br.addWidget(QLabel("초"))
        br.addWidget(bb); br.addStretch()
        tv.addLayout(br); v.addWidget(tg)

        # ── 실행 ──────────────────────────────────────────────────────────
        run = QGroupBox("▶ 실행"); rn = QGridLayout(run); rn.setSpacing(6)
        rn.addWidget(QLabel("반복:"), 0, 0)
        self.rep_spin = _make_spinbox(0, 9999, 1, 1, 0, "∞", 70)
        self.rep_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'macro_repeat', int(v)))
        rn.addWidget(self.rep_spin, 0, 1)
        rn.addWidget(QLabel("루프 간격(초):"), 1, 0)
        self.gap_spin = _make_spinbox(0, 60, 1, 0.5, 1, "", 70)
        self.gap_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'macro_gap', v))
        rn.addWidget(self.gap_spin, 1, 1)

        rb2 = QHBoxLayout(); rb2.setSpacing(6)
        self.run_btn = QPushButton("▶  실행"); self.run_btn.setFixedHeight(32)
        self.run_btn.setStyleSheet(
            "QPushButton{background:#2980b9;color:#fff;border:none;border-radius:5px;"
            "font-size:12px;font-weight:bold;}QPushButton:hover{background:#3498db;}"
            "QPushButton:disabled{background:#1a3a5a;color:#555;}")
        self.run_btn.clicked.connect(self._on_run)
        self.stop_btn = QPushButton("■  중단  [Ctrl+Alt+X]"); self.stop_btn.setFixedHeight(32)
        self.stop_btn.setStyleSheet(
            "QPushButton{background:#5a6a7a;color:#fff;border:none;border-radius:5px;"
            "font-size:12px;}QPushButton:hover{background:#7f8c8d;}"
            "QPushButton:disabled{background:#2a2a2a;color:#555;}")
        self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self._on_stop)
        self.run_st = QLabel("● 대기")
        self.run_st.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")
        rb2.addWidget(self.run_btn, 1); rb2.addWidget(self.stop_btn, 1)
        rn.addLayout(rb2, 2, 0, 1, 2); rn.addWidget(self.run_st, 3, 0, 1, 2)
        v.addWidget(run)

    # ── 슬롯 ─────────────────────────────────────────────────────────────
    def _init_slots(self):
        if not self._slots:
            self._slots.append({'title':'슬롯 1','steps':[]})
            self.slot_cb.blockSignals(True)
            self.slot_cb.addItem('슬롯 1')
            self.slot_cb.blockSignals(False)
        self._active_slot = 0; self.slot_cb.setCurrentIndex(0)
        self._sync(); self._info_upd()
        self._sync_engine_slots()   # ★ 엔진 캐시 초기 동기화

    def _sync(self):
        if 0 <= self._active_slot < len(self._slots):
            self.engine.macro_steps.clear()
            self.engine.macro_steps.extend(self._slots[self._active_slot]['steps'])

    def _save_cur(self):
        if 0 <= self._active_slot < len(self._slots):
            # ★ 기록 중이거나 기록 중단 직후(비동기 _stop 스레드 실행 중)일 때는
            # engine.macro_steps이 불완전할 수 있음 → 이미 _slots에 스텝이 있는데
            # engine.macro_steps가 비어있으면 덮어쓰지 않음 (데이터 보호)
            cur_steps = list(self.engine.macro_steps)
            existing  = self._slots[self._active_slot]['steps']
            if cur_steps or not existing:
                # 현재 스텝이 있거나, 기존에도 없었으면 정상 저장
                self._slots[self._active_slot]['steps'] = cur_steps
            # else: 현재 비어있고 기존에 데이터가 있으면 → 보호 (덮어쓰지 않음)
            self._sync_engine_slots()

    def _sync_engine_slots(self):
        """engine._macro_slots 캐시를 현재 _slots 데이터로 갱신."""
        self.engine._macro_slots = [
            {'title': s['title'],
             'steps': list(s['steps'])}
            for s in self._slots
        ]

    def _info_upd(self):
        n = len(self._slots); idx = self._active_slot
        nm = self._slots[idx]['title'] if 0 <= idx < n else "—"
        self.slot_info.setText(
            f"{nm}  |  슬롯 {idx+1}/{n}  |  {len(self.engine.macro_steps)} 스텝")

    def _on_slot_changed(self, idx):
        # ★ 기록 중 슬롯 전환 방지 (데이터 손실 예방)
        if getattr(self.engine, 'macro_recording', False):
            self.slot_cb.blockSignals(True)
            self.slot_cb.setCurrentIndex(self._active_slot)
            self.slot_cb.blockSignals(False)
            self.signals.status_message.emit("[Macro] ⚠ 기록 중단 후 슬롯을 변경하세요")
            return
        self._save_cur(); self._active_slot = idx
        self._sync(); self._rebuild_tbl(); self._info_upd()

    def _add_slot(self):
        t = f"슬롯 {len(self._slots)+1}"
        self._slots.append({'title':t,'steps':[]})
        self.slot_cb.blockSignals(True); self.slot_cb.addItem(t)
        self.slot_cb.blockSignals(False)
        self.slot_cb.setCurrentIndex(len(self._slots)-1)
        self._sync_engine_slots()   # ★ 슬롯 추가 후 동기화

    def _del_slot(self):
        if len(self._slots) <= 1: return
        idx = self._active_slot; self._slots.pop(idx)
        self.slot_cb.blockSignals(True); self.slot_cb.removeItem(idx)
        self.slot_cb.blockSignals(False)
        new = max(0, idx-1); self._active_slot = new
        self.slot_cb.setCurrentIndex(new)
        self._sync(); self._rebuild_tbl(); self._info_upd()
        self._sync_engine_slots()   # ★ 슬롯 삭제 후 동기화

    def _rename_slot(self):
        idx = self._active_slot
        if not 0 <= idx < len(self._slots): return
        new, ok = QInputDialog.getText(
            self, "슬롯 이름", "새 이름:", text=self._slots[idx]['title'])
        if ok and new.strip():
            self._slots[idx]['title'] = new.strip()
            self.slot_cb.blockSignals(True)
            self.slot_cb.setItemText(idx, new.strip())
            self.slot_cb.blockSignals(False)
            self._info_upd()

    # ── 기록 ─────────────────────────────────────────────────────────────
    def _on_rec_toggle(self, recording):
        if recording:
            self.rec_btn.setText("⏹  기록 중단")
            self.rec_st.setText("● 기록 중")
            self.rec_st.setStyleSheet("color:#e74c3c;font-size:11px;font-weight:bold;")
            self.engine._rec_filter = {
                'click': self.rec_click_chk.isChecked(),
                'drag':  self.rec_drag_chk.isChecked(),
                'key':   self.rec_key_chk.isChecked(),
            }
            self.engine.macro_start_recording()
        else:
            self.rec_btn.setText("⏺  기록 시작")
            self.rec_st.setText("● 완료")
            self.rec_st.setStyleSheet("color:#2ecc71;font-size:11px;font-weight:bold;")
            self.engine.macro_stop_recording()
            # ★ 기록 중단 후 500ms 대기(stop 스레드 완료) 후 슬롯에 저장 + UI 갱신
            def _finalize():
                # _stop 스레드(250ms 대기+pop)가 완료될 때까지 충분히 대기
                self._save_cur()           # engine.macro_steps → _slots 저장
                self._sync_engine_slots()  # engine._macro_slots 캐시 갱신
                self._rebuild_tbl()        # 테이블을 steps 기준으로 재빌드
                self._make_editable()
                self._info_upd()
            QTimer.singleShot(550, _finalize)

    def _on_step(self, step):
        # ★ 기록 중단 후 큐 잔여 이벤트 무시
        if not self.engine.macro_recording:
            return
        self._append_row(step, editable=False)
        self.last_evt_lbl.setText(step.summary())
        self._info_upd()

    # ── 테이블 ───────────────────────────────────────────────────────────
    @staticmethod
    def _type_color(kind):
        return {'click':'#7bc8e0','drag':'#f0c040','key':'#afffcf'}.get(kind,'#ccc')

    def _append_row(self, step, editable=True):
        ef = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable
        rf = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        self.tbl.blockSignals(True)
        r = self.tbl.rowCount(); self.tbl.insertRow(r)
        items = [
            (str(r+1), rf, Qt.AlignCenter, QColor("#556")),
            (step.kind.upper(), rf, Qt.AlignCenter, QColor(self._type_color(step.kind))),
            (self._step_coord_str(step), ef if editable else rf,
             Qt.AlignLeft|Qt.AlignVCenter,
             QColor("#ddd") if editable else QColor("#999")),
            (f"{step.delay:.3f}", ef if editable else rf,
             Qt.AlignCenter,
             QColor("#ddd") if editable else QColor("#999")),
        ]
        for c, (text, flags, align, color) in enumerate(items):
            it = QTableWidgetItem(text)
            it.setFlags(flags); it.setTextAlignment(align); it.setForeground(color)
            self.tbl.setItem(r, c, it)
        opt = ""
        if step.kind == 'click':
            opt = step.button[:1].upper() + (" x2" if step.double else "")
        elif step.kind == 'drag':
            opt = step.button[:1].upper()
        it4 = QTableWidgetItem(opt)
        it4.setFlags(rf); it4.setTextAlignment(Qt.AlignCenter)
        it4.setForeground(QColor("#888"))
        self.tbl.setItem(r, 4, it4)
        self.tbl.scrollToBottom(); self.tbl.blockSignals(False)

    @staticmethod
    def _step_coord_str(step) -> str:
        if step.kind == 'click': return f"({step.x},{step.y})"
        if step.kind == 'drag':  return f"({step.x},{step.y})→({step.x2},{step.y2})"
        if step.kind == 'key':   return step.key_str
        return ""

    def _make_editable(self):
        ef = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable
        self.tbl.blockSignals(True)
        for r in range(self.tbl.rowCount()):
            for c in (2, 3):
                it = self.tbl.item(r, c)
                if it: it.setFlags(ef); it.setForeground(QColor("#ddd"))
        self.tbl.blockSignals(False)

    def _rebuild_tbl(self):
        self.tbl.blockSignals(True); self.tbl.setRowCount(0)
        self.tbl.blockSignals(False)
        for step in self.engine.macro_steps:
            self._append_row(step, editable=True)

    def _on_item_changed(self, item):
        r = item.row(); c = item.column()
        if r >= len(self.engine.macro_steps): return
        step = self.engine.macro_steps[r]
        try:
            txt = item.text().strip()
            if c == 3:
                step.delay = max(0.0, float(txt))
            elif c == 2:
                if step.kind == 'click':
                    m = re.fullmatch(r'\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)', txt)
                    if m: step.x, step.y = int(m.group(1)), int(m.group(2))
                elif step.kind == 'drag':
                    m = re.fullmatch(
                        r'\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)→\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)', txt)
                    if m:
                        step.x, step.y   = int(m.group(1)), int(m.group(2))
                        step.x2, step.y2 = int(m.group(3)), int(m.group(4))
                elif step.kind == 'key':
                    step.key_str = txt
        except: pass

    def _del_step(self):
        rows = sorted({i.row() for i in self.tbl.selectedItems()}, reverse=True)
        self.tbl.blockSignals(True)
        for r in rows:
            self.tbl.removeRow(r)
            if r < len(self.engine.macro_steps): self.engine.macro_steps.pop(r)
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it: it.setText(str(r+1))
        self.tbl.blockSignals(False); self._info_upd()

    def _clear_steps(self):
        self.tbl.blockSignals(True); self.tbl.setRowCount(0)
        self.tbl.blockSignals(False); self.engine.macro_clear(); self._info_upd()

    def _step_up(self):
        r = self.tbl.currentRow(); s = self.engine.macro_steps
        if r <= 0 or r >= len(s): return
        s[r-1], s[r] = s[r], s[r-1]
        self._rebuild_tbl(); self.tbl.setCurrentCell(r-1, 0)

    def _step_dn(self):
        r = self.tbl.currentRow(); s = self.engine.macro_steps
        if r < 0 or r >= len(s)-1: return
        s[r], s[r+1] = s[r+1], s[r]
        self._rebuild_tbl(); self.tbl.setCurrentCell(r+1, 0)

    def _bulk_delay(self):
        d = self.bulk_spin.value(); self.tbl.blockSignals(True)
        for i, step in enumerate(self.engine.macro_steps):
            step.delay = d
            it = self.tbl.item(i, 3)
            if it: it.setText(f"{d:.3f}")
        self.tbl.blockSignals(False)

    def _on_run(self):
        if not self.engine.macro_steps:
            self.signals.status_message.emit("[Macro] 스텝이 없습니다"); return
        self.engine.macro_start_run()
        self.run_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        self.run_st.setText("● 실행 중")
        self.run_st.setStyleSheet("color:#2ecc71;font-size:11px;font-weight:bold;")
        self._watch = QTimer(self)
        self._watch.timeout.connect(self._check_done)
        self._watch.start(300)

    def _check_done(self):
        if not self.engine.macro_running:
            self._watch.stop()
            self.run_btn.setEnabled(True); self.stop_btn.setEnabled(False)
            self.run_st.setText("● 완료")
            self.run_st.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")

    def _on_stop(self):
        self.engine.macro_stop_run()
        self.run_btn.setEnabled(True); self.stop_btn.setEnabled(False)
        self.run_st.setText("● 중단")
        self.run_st.setStyleSheet("color:#e74c3c;font-size:11px;font-weight:bold;")

    def get_slots_data(self) -> list:
        self._save_cur()
        return [{'title': s['title'],
                 'steps': [st.to_dict() for st in s['steps']]}
                for s in self._slots]

    def set_slots_data(self, data: list):
        self._slots.clear()
        self.slot_cb.blockSignals(True); self.slot_cb.clear()
        for s in data:
            steps = [MacroStep.from_dict(st) for st in s.get('steps', [])]
            self._slots.append({'title': s['title'], 'steps': steps})
            self.slot_cb.addItem(s['title'])
        self.slot_cb.blockSignals(False)
        self._active_slot = 0; self.slot_cb.setCurrentIndex(0)
        self._sync(); self._rebuild_tbl(); self._info_upd()
        self._sync_engine_slots()   # ★ DB 복원 후 엔진 캐시 동기화


