# -*- coding: utf-8 -*-
"""
ui_swc/manual_clip_panel.py
수동녹화 패널
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

class ManualClipPanel(QWidget):
    def __init__(self, engine: "CoreEngine", signals: "Signals", parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        self._led=False; self._led_timer=QTimer(self)
        self._led_timer.timeout.connect(self._blink)
        self._last_saved_dir = ""
        signals.manual_clip_saved.connect(self._on_saved)
        # ★ 외부(커널/AI)에서 set_manual_time() 호출 시 UI 자동 동기화
        signals.manual_settings_changed.connect(self.sync_from_engine)
        signals.manual_source_changed.connect(self._sync_source_chk)
        self._build()
        # ★ engine에 자신을 등록 — AI Chat / 커널이 UI 동기화 시 직접 접근
        engine._manual_panel_ref = self

    def _sync_source_chk(self, source: str):
        """engine.set_manual_source() 호출 시 체크박스 UI 동기화 (시그널 루프 방지)."""
        for widget, flag in [(self.scr_chk, source in ('screen','both')),
                             (self.cam_chk, source in ('camera','both'))]:
            widget.blockSignals(True)
            widget.setChecked(flag)
            widget.blockSignals(False)

    def sync_from_engine(self, pre: float = None, post: float = None):
        """engine.manual_pre_sec / post_sec 값을 UI(스핀박스+슬라이더)에 즉시 반영.
        manual_settings_changed 시그널 슬롯으로도 사용 (pre, post 파라미터 자동 전달).
        """
        try:
            if pre is None:
                pre  = float(getattr(self.engine, 'manual_pre_sec',  10.0))
            if post is None:
                post = float(getattr(self.engine, 'manual_post_sec', 10.0))
            sliders = getattr(self, '_sliders', {})

            if hasattr(self, 'pre_spin'):
                self.pre_spin.blockSignals(True)
                self.pre_spin.setValue(pre)
                self.pre_spin.blockSignals(False)
            # 슬라이더도 함께 갱신 (blockSignals만 하면 슬라이더가 안 따라감)
            sl_pre = sliders.get('manual_pre_sec')
            if sl_pre:
                sl_pre.blockSignals(True)
                sl_pre.setValue(int(round(pre * 10)))
                sl_pre.blockSignals(False)

            if hasattr(self, 'post_spin'):
                self.post_spin.blockSignals(True)
                self.post_spin.setValue(post)
                self.post_spin.blockSignals(False)
            sl_post = sliders.get('manual_post_sec')
            if sl_post:
                sl_post.blockSignals(True)
                sl_post.setValue(int(round(post * 10)))
                sl_post.blockSignals(False)

            # engine 값도 확실히 반영
            self.engine.manual_pre_sec  = pre
            self.engine.manual_post_sec = post
        except Exception:
            pass

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        info=QLabel("버튼을 누르면 해당 시점 기준 전/후 N초를 버퍼에서 추출해 저장합니다.\n최대 ±40초 저장 가능 (버퍼 크기에 따라 달라질 수 있습니다)")
        info.setWordWrap(True); info.setStyleSheet("color:#778;font-size:10px;"); v.addWidget(info)

        # T/C 상태 표시
        tc_row=QHBoxLayout(); tc_row.setSpacing(8)
        self.tc_lbl=QLabel("🔬 T/C 검증:"); self.tc_lbl.setStyleSheet("color:#f0a040;font-size:10px;font-weight:bold;")
        self.tc_status_lbl=QLabel("비활성  (저장 경로 패널에서 설정)")
        self.tc_status_lbl.setStyleSheet("color:#556;font-size:9px;")
        tc_row.addWidget(self.tc_lbl); tc_row.addWidget(self.tc_status_lbl); tc_row.addStretch()
        v.addLayout(tc_row)

        # 소스
        sg=QGroupBox("저장 소스"); sl=QHBoxLayout(sg); sl.setSpacing(12)
        self.scr_chk=QCheckBox("🖥 Display"); self.scr_chk.setChecked(True)
        self.cam_chk=QCheckBox("📷 Camera");  self.cam_chk.setChecked(True)
        self.scr_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#7bc8e0;")
        self.cam_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#f0c040;")
        self.scr_chk.toggled.connect(self._upd_src); self.cam_chk.toggled.connect(self._upd_src)
        sl.addWidget(self.scr_chk); sl.addWidget(self.cam_chk); sl.addStretch(); v.addWidget(sg)

        # 전/후 시간
        tg=QGroupBox("전/후 시간  (최대 40초)"); tgl=QGridLayout(tg); tgl.setSpacing(6)
        # ★ 초기값을 engine에서 읽어 DB-UI 동기화 (default: 10초)
        _pre_val  = float(getattr(self.engine, 'manual_pre_sec',  10.0))
        _post_val = float(getattr(self.engine, 'manual_post_sec', 10.0))
        self.pre_spin  = self._make_row(tgl,0,"🔵 전 (초)","#7bc8e0","manual_pre_sec", 0,40,_pre_val)
        self.post_spin = self._make_row(tgl,1,"🟠 후 (초)","#f0a040","manual_post_sec",0,40,_post_val)
        v.addWidget(tg)

        # 버튼
        br=QHBoxLayout(); br.setSpacing(8)
        self.led_lbl=QLabel("●"); self.led_lbl.setFixedWidth(22)
        self.led_lbl.setAlignment(Qt.AlignCenter)
        self.led_lbl.setStyleSheet("font-size:22px;color:#333;"); br.addWidget(self.led_lbl)
        self.clip_btn=QPushButton("🎬  지금 클립 저장  [Ctrl+Alt+M]")
        self.clip_btn.setMinimumHeight(42)
        self.clip_btn.setStyleSheet(
            "QPushButton{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #7d3c98,stop:1 #e67e22);color:white;font-size:13px;"
            "font-weight:bold;border:none;border-radius:6px;padding:6px;}"
            "QPushButton:hover{background:#9b59b6;}"
            "QPushButton:disabled{background:#2a2a3a;color:#666;border:1px solid #444;}")
        self.clip_btn.clicked.connect(self._on_clip); br.addWidget(self.clip_btn,1); v.addLayout(br)
        self.status_lbl=QLabel("대기 중"); self.status_lbl.setStyleSheet("color:#888;font-size:11px;font-family:monospace;"); v.addWidget(self.status_lbl)
        open_btn=QPushButton("📂 수동클립 폴더"); open_btn.setFixedHeight(26)
        def _open_manual_folder():
            # [버그 수정] 마지막으로 실제 저장된 경로 우선 사용
            # _build_path()로 재계산하면 PASS/FAIL 태그된 폴더를 찾지 못하고
            # blackout 등 다른 기능이 output_dir을 덮어쓴 경우에도 오동작함
            p = self._last_saved_dir
            if not p or not os.path.exists(p):
                # 저장 이력 없으면 _build_path로 폴백
                p = self.engine._build_path("manual")
                while p and not os.path.exists(p) and p != os.path.dirname(p):
                    p = os.path.dirname(p)
            open_folder(p or self.engine.base_dir)
        open_btn.clicked.connect(_open_manual_folder); v.addWidget(open_btn)

        self._poll=QTimer(self); self._poll.timeout.connect(self._check_state); self._poll.start(200)
        self._tc_poll=QTimer(self); self._tc_poll.timeout.connect(self._upd_tc_status); self._tc_poll.start(500)

    def _upd_tc_status(self):
        if self.engine.tc_manual_enabled:
            self.tc_status_lbl.setText("✅ 활성화  (클립 저장 시 PASS/FAIL 선택)")
            self.tc_status_lbl.setStyleSheet("color:#f0a040;font-size:9px;font-weight:bold;")
        else:
            self.tc_status_lbl.setText("비활성  (저장 경로 패널에서 설정)")
            self.tc_status_lbl.setStyleSheet("color:#556;font-size:9px;")

    def _make_row(self, grid, row, label, color, attr, min_v, max_v, val):
        lbl=QLabel(label); lbl.setStyleSheet(f"font-weight:bold;color:{color};")
        sp=QDoubleSpinBox(); sp.setRange(min_v,max_v); sp.setValue(val)
        sp.setSingleStep(0.5); sp.setDecimals(1); sp.setMinimumHeight(28)
        sp.setStyleSheet(f"QDoubleSpinBox{{background:#1a1a3a;color:{color};"
                         "border:1px solid #3a3a5a;border-radius:4px;padding:3px;"
                         "font-size:13px;font-weight:bold;}")
        sl=QSlider(Qt.Horizontal); sl.setRange(0,int(max_v*10)); sl.setValue(int(val*10))
        sl.setStyleSheet(
            "QSlider::groove:horizontal{background:#1a2030;height:6px;border-radius:3px;}"
            f"QSlider::handle:horizontal{{background:{color};width:16px;height:16px;"
            "margin-top:-5px;margin-bottom:-5px;border-radius:8px;}"
            f"QSlider::sub-page:horizontal{{background:{color}55;border-radius:3px;}}")

        # ★ 슬라이더 참조를 인스턴스에 보관 → sync_from_engine에서 직접 접근
        if not hasattr(self, '_sliders'):
            self._sliders = {}
        self._sliders[attr] = sl

        def _on_spin(v, _sl=sl, _a=attr):
            """스핀박스 변경 → 슬라이더 동기화 + engine + DB"""
            _sl.blockSignals(True)
            _sl.setValue(int(round(v * 10)))
            _sl.blockSignals(False)
            setattr(self.engine, _a, v)
            self._save_to_db(_a, v)

        def _on_slider(v, _sp=sp, _a=attr):
            """슬라이더 변경 → 스핀박스 동기화 + engine + DB"""
            val_f = round(v / 10, 1)
            _sp.blockSignals(True)
            _sp.setValue(val_f)
            _sp.blockSignals(False)
            setattr(self.engine, _a, val_f)
            self._save_to_db(_a, val_f)

        sp.valueChanged.connect(_on_spin)
        sl.valueChanged.connect(_on_slider)
        grid.addWidget(lbl,row,0); grid.addWidget(sp,row,1); grid.addWidget(sl,row,2)
        grid.setColumnStretch(2,1)
        return sp

    def _save_to_db(self, attr: str, val):
        """engine 속성 변경 → DB 1초 디바운스 저장."""
        if not hasattr(self, '_db_timer'):
            self._db_timer = QTimer(self)
            self._db_timer.setSingleShot(True)
            self._db_timer.setInterval(1000)
            self._db_timer.timeout.connect(self._flush_db)
        self._db_timer.start()

    def _flush_db(self):
        db = getattr(self.engine, '_db', None)
        if not db: return
        try:
            db.set("manual_pre_sec",  str(self.engine.manual_pre_sec))
            db.set("manual_post_sec", str(self.engine.manual_post_sec))
            db.set("manual_source",   self.engine.manual_source)
        except Exception:
            pass

    def _upd_src(self):
        s=self.scr_chk.isChecked(); c=self.cam_chk.isChecked()
        if not s and not c:
            self.sender().blockSignals(True); self.sender().setChecked(True)
            self.sender().blockSignals(False); return
        self.engine.manual_source=("both" if s and c else "screen" if s else "camera")
        self._save_to_db("manual_source", self.engine.manual_source)

    def _on_clip(self):
        if self.engine.tc_manual_enabled:
            result = show_tc_dialog(self, "수동녹화 저장 전\nT/C 검증 결과를 선택하세요.")
            if result is None:
                return
            self.engine.tc_verify_result = result

        ok = self.engine.save_manual_clip()
        if not ok:
            self.status_lbl.setText("⏳ 쿨다운 중 — 잠시 후 재시도")
            self.status_lbl.setStyleSheet("color:#f0c040;font-size:11px;")

    def _on_saved(self, path):
        self.status_lbl.setText(f"✅ 저장: {os.path.basename(path)}")
        self.status_lbl.setStyleSheet("color:#2ecc71;font-size:11px;")
        # [버그 수정] 마지막으로 저장된 실제 경로를 보관
        # → 폴더 열기 버튼이 PASS/FAIL 태그된 실제 폴더를 열 수 있도록
        self._last_saved_dir = os.path.dirname(path)

    def _check_state(self):
        idle = (self.engine.manual_state == CoreEngine.MANUAL_IDLE)
        if idle:
            self._led_timer.stop(); self.led_lbl.setStyleSheet("font-size:22px;color:#333;")
            self.clip_btn.setEnabled(True)
        else:
            if not self._led_timer.isActive(): self._led_timer.start(400)
            self.clip_btn.setEnabled(False)

    def _blink(self):
        self._led=not self._led
        self.led_lbl.setStyleSheet(f"font-size:22px;color:{'#e74c3c' if self._led else '#7f2a2a'};")


# =============================================================================
#  BlackoutPanel
# =============================================================================
