# -*- coding: utf-8 -*-
"""
ui_swc/blackout_panel.py
블랙아웃 감지 패널
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

class BlackoutPanel(QWidget):
    def __init__(self, engine: "CoreEngine", signals: "Signals", parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        signals.blackout_detected.connect(self._on_bo)
        self._build()

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        self.rec_chk=QCheckBox("블랙아웃 클립 녹화 활성화")
        self.rec_chk.setChecked(True)
        # UI → engine (사용자가 체크박스 클릭할 때)
        self.rec_chk.toggled.connect(lambda c: setattr(self.engine,'blackout_rec_enabled',c))
        # engine → UI (스크립트/외부에서 engine.blackout_rec_enabled 변경할 때)
        self.signals.blackout_rec_changed.connect(self._sync_rec_chk)
        v.addWidget(self.rec_chk)

        tc_row=QHBoxLayout()
        self.tc_status_lbl=QLabel("🔬 T/C 검증: 비활성")
        self.tc_status_lbl.setStyleSheet("color:#556;font-size:9px;")
        tc_row.addWidget(self.tc_status_lbl); tc_row.addStretch()
        v.addLayout(tc_row)
        self._tc_poll=QTimer(self); self._tc_poll.timeout.connect(self._upd_tc_status); self._tc_poll.start(500)

        tg=QGroupBox("감지 설정"); tl=QGridLayout(tg)
        tl.addWidget(QLabel("밝기 변화 임계값:"),0,0)
        self.thr_spin=_make_spinbox(5,200,30,1,0,"",80)
        self.thr_spin.setSuffix(" (0~255)")
        self.thr_spin.valueChanged.connect(lambda v: setattr(self.engine,'brightness_threshold',v))
        tl.addWidget(self.thr_spin,0,1)
        tl.addWidget(QLabel("쿨다운 (초):"),1,0)
        self.cd_spin=_make_spinbox(0.5,60,5,0.5,1,"",80)
        self.cd_spin.valueChanged.connect(lambda v: setattr(self.engine,'blackout_cooldown',v))
        tl.addWidget(self.cd_spin,1,1)
        v.addWidget(tg)

        cnt=QGroupBox("감지 횟수"); cl=QGridLayout(cnt)
        cl.addWidget(QLabel("화면:"),0,0); self.scr_cnt=QLabel("0")
        self.scr_cnt.setStyleSheet("font-weight:bold;color:#e74c3c;"); cl.addWidget(self.scr_cnt,0,1)
        cl.addWidget(QLabel("카메라:"),1,0); self.cam_cnt=QLabel("0")
        self.cam_cnt.setStyleSheet("font-weight:bold;color:#e74c3c;"); cl.addWidget(self.cam_cnt,1,1)
        open_btn=QPushButton("📂 블랙아웃 폴더"); open_btn.setFixedHeight(26)
        def _open_bo_folder():
            # ★ 수정: _build_path로 실제 저장 경로 계산 후 열기
            p = self.engine._build_path("blackout")
            while p and not os.path.exists(p) and p != os.path.dirname(p):
                p = os.path.dirname(p)
            open_folder(p or self.engine.base_dir)
        open_btn.clicked.connect(_open_bo_folder)
        cl.addWidget(open_btn,2,0,1,2)
        v.addWidget(cnt)

        roi_g=QGroupBox("ROI 밝기 현황"); rl=QVBoxLayout(roi_g)
        self.roi_txt=QTextEdit(); self.roi_txt.setReadOnly(True); self.roi_txt.setFixedHeight(100)
        self.roi_txt.setStyleSheet("font-size:10px;font-family:monospace;background:#0d0d1e;")
        rl.addWidget(self.roi_txt); v.addWidget(roi_g)

        ev_g=QGroupBox("최근 이벤트"); el=QVBoxLayout(ev_g)
        self.ev_txt=QTextEdit(); self.ev_txt.setReadOnly(True); self.ev_txt.setFixedHeight(80)
        self.ev_txt.setStyleSheet("font-size:10px;font-family:monospace;background:#0d0d1e;")
        el.addWidget(self.ev_txt); v.addWidget(ev_g)

    def _upd_tc_status(self):
        if self.engine.tc_blackout_enabled:
            self.tc_status_lbl.setText("🔬 T/C 검증: ✅ 활성화  (블랙아웃 클립 저장 후 PASS/FAIL 적용)")
            self.tc_status_lbl.setStyleSheet("color:#f0a040;font-size:9px;font-weight:bold;")
        else:
            self.tc_status_lbl.setText("🔬 T/C 검증: 비활성  (저장 경로 패널에서 설정)")
            self.tc_status_lbl.setStyleSheet("color:#556;font-size:9px;")

    def _sync_rec_chk(self, enabled: bool):
        """engine.blackout_rec_enabled 변경 시 체크박스 UI 동기화 (시그널 루프 방지)."""
        self.rec_chk.blockSignals(True)
        self.rec_chk.setChecked(enabled)
        self.rec_chk.blockSignals(False)

    def _on_bo(self, source, event):
        self.scr_cnt.setText(str(self.engine.screen_bo_count))
        self.cam_cnt.setText(str(self.engine.camera_bo_count))

    def refresh(self):
        self.scr_cnt.setText(str(self.engine.screen_bo_count))
        self.cam_cnt.setText(str(self.engine.camera_bo_count))

        # ── 블랙아웃/T/C 중 하나라도 켜져 있을 때만 밝기 현황 표시 ★
        if not (self.engine.blackout_rec_enabled or
                self.engine.tc_blackout_enabled):
            self.roi_txt.setPlainText(
                "블랙아웃 기능 꺼짐 — 밝기 계산 중단 중\n"
                "(절약 중: screen/camera 루프에서 ROI 밝기 연산 없음)")
            return

        lines=[]
        for src,avgs,ov in [("Scr",self.engine.screen_roi_avg,self.engine.screen_overall_avg),
                             ("Cam",self.engine.camera_roi_avg,self.engine.camera_overall_avg)]:
            if avgs:
                b,g,r=ov; br=0.114*b+0.587*g+0.299*r
                lines.append(f"[{src}] R{int(r)} G{int(g)} B{int(b)} Br:{int(br)}")
                for i,a in enumerate(avgs[:5]):
                    b2,g2,r2=a; br2=0.114*b2+0.587*g2+0.299*r2
                    lines.append(f"  ROI{i+1}: Br:{int(br2)}")
        self.roi_txt.setPlainText("\n".join(lines))
        ev=[]
        for src,evs in [("화면",self.engine.screen_bo_events),("카메라",self.engine.camera_bo_events)]:
            for e in reversed(list(evs)[-4:]):  # deque는 슬라이스 불가 → list 변환
                ev.append(f"[{src}] {e['time']}  변화:{int(e['brightness_change'])}")
        self.ev_txt.setPlainText("\n".join(ev))


# =============================================================================
#  PathSettingsPanel  (use_custom_path 제거, 라디오버튼, 텍스트 수정)
# =============================================================================
