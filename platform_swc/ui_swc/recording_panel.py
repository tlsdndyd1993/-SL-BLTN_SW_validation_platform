# -*- coding: utf-8 -*-
"""
ui_swc/recording_panel.py
녹화 제어 패널
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

def show_tc_dialog(parent, prompt: str = "T/C 검증 결과를 선택하세요.",
                   cancel_label: str = "취소") -> Optional[str]:
    """
    PASS/FAIL 선택 모달 다이얼로그.
    반환: "PASS" | "FAIL" | None(취소)
    모든 패널에서 공통으로 사용.
    """
    result_box = [None]
    dlg = QDialog(parent)
    dlg.setWindowTitle("T/C 검증 결과 입력")
    dlg.setWindowModality(Qt.ApplicationModal)
    dlg.setWindowFlags(
        Qt.Dialog | Qt.WindowTitleHint |
        Qt.WindowCloseButtonHint | Qt.MSWindowsFixedSizeDialogHint)
    dlg.setFixedSize(420, 220)
    dlg.setStyleSheet(
        "QDialog{background:#0d0d1e;}"
        "QLabel{color:#dde;}"
        "QPushButton{border-radius:6px;border:none;}")
    lay = QVBoxLayout(dlg)
    lay.setSpacing(20); lay.setContentsMargins(28, 24, 28, 20)
    lbl = QLabel(prompt)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet("font-size:14px;font-weight:bold;color:#f0c040;")
    lay.addWidget(lbl)
    btn_row = QHBoxLayout(); btn_row.setSpacing(20)
    btn_pass = QPushButton("✅  PASS")
    btn_pass.setMinimumHeight(48); btn_pass.setMinimumWidth(140)
    btn_pass.setStyleSheet(
        "QPushButton{background:#1a6a3a;color:#afffcf;font-size:17px;"
        "font-weight:bold;border-radius:8px;border:2px solid #2a9a5a;}"
        "QPushButton:hover{background:#27ae60;color:#fff;}"
        "QPushButton:pressed{background:#1a5a30;}")
    btn_fail = QPushButton("❌  FAIL")
    btn_fail.setMinimumHeight(48); btn_fail.setMinimumWidth(140)
    btn_fail.setStyleSheet(
        "QPushButton{background:#6a1a1a;color:#ffaaaa;font-size:17px;"
        "font-weight:bold;border-radius:8px;border:2px solid #aa3a3a;}"
        "QPushButton:hover{background:#c0392b;color:#fff;}"
        "QPushButton:pressed{background:#5a1010;}")
    def _pick(r):
        result_box[0] = r; dlg.accept()
    btn_pass.clicked.connect(lambda: _pick("PASS"))
    btn_fail.clicked.connect(lambda: _pick("FAIL"))
    btn_row.addWidget(btn_pass); btn_row.addWidget(btn_fail)
    lay.addLayout(btn_row)
    btn_cancel = QPushButton(cancel_label)
    btn_cancel.setFixedHeight(30)
    btn_cancel.setStyleSheet(
        "QPushButton{background:#1a1a2a;color:#888;border:1px solid #3a3a5a;"
        "font-size:11px;padding:4px 16px;border-radius:4px;}"
        "QPushButton:hover{background:#252535;color:#aab;}")
    btn_cancel.clicked.connect(dlg.reject)
    lay.addWidget(btn_cancel, alignment=Qt.AlignCenter)
    dlg.exec_()
    return result_box[0]


# =============================================================================
class RecordingPanel(QWidget):
    def __init__(self, engine: "CoreEngine", signals: "Signals", parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        self._build()
        signals.rec_started.connect(lambda _: self._on_rec_started())
        signals.rec_stopped.connect(self._on_rec_stopped)
        signals.capture_saved.connect(self._on_capture_saved)

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        # ── 상태 ─────────────────────────────────────────────────────────────
        st=QGroupBox("상태"); g=QGridLayout(st); g.setSpacing(6)
        self.status_lbl=QLabel("● 대기")
        self.status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;font-size:14px;")
        self.timer_lbl=QLabel("00:00:00")
        self.timer_lbl.setStyleSheet("font-size:28px;font-weight:bold;color:#2ecc71;font-family:monospace;")
        self.datetime_lbl=QLabel("—")
        self.datetime_lbl.setStyleSheet("color:#7bc8e0;font-size:11px;font-family:monospace;")
        g.addWidget(self.status_lbl,0,0,1,2)
        g.addWidget(self.timer_lbl,1,0,1,2,Qt.AlignCenter)
        g.addWidget(self.datetime_lbl,2,0,1,2,Qt.AlignCenter)
        g.addWidget(QLabel("Screen FPS:"),3,0)
        self.scr_fps_lbl=QLabel("—"); g.addWidget(self.scr_fps_lbl,3,1)
        g.addWidget(QLabel("Camera FPS:"),4,0)
        self.cam_fps_lbl=QLabel("—"); g.addWidget(self.cam_fps_lbl,4,1)
        self._folder_btn=QPushButton("📂 녹화 폴더"); self._folder_btn.setFixedHeight(26)
        self._folder_btn.clicked.connect(
            lambda: open_folder(self.engine.output_dir or self.engine.base_dir))
        g.addWidget(self._folder_btn,5,0,1,2)
        v.addWidget(st)

        # ── 녹화 소스 ─────────────────────────────────────────────────────────
        src_g=QGroupBox("📹 녹화 소스  (미리보기 출력 ON/OFF 와 독립)")
        src_l=QHBoxLayout(src_g); src_l.setSpacing(16)
        self.scr_chk=QCheckBox("🖥 Display 저장")
        self.scr_chk.setChecked(True)
        self.scr_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#7bc8e0;")
        self.scr_chk.toggled.connect(lambda c: setattr(self.engine,'screen_rec_enabled',c))
        self.cam_chk=QCheckBox("📷 Camera 저장")
        self.cam_chk.setChecked(True)
        self.cam_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#f0c040;")
        self.cam_chk.toggled.connect(lambda c: setattr(self.engine,'camera_rec_enabled',c))
        src_l.addWidget(self.scr_chk); src_l.addWidget(self.cam_chk); src_l.addStretch()
        v.addWidget(src_g)

        # ── 현재 프레임 캡처 ─────────────────────────────────────────────────
        cap_g=QGroupBox("📸 현재 프레임 캡처 (PNG 저장)")
        cap_v=QVBoxLayout(cap_g); cap_v.setSpacing(6)
        cap_btn_row=QHBoxLayout(); cap_btn_row.setSpacing(8)
        self.cap_scr_btn=QPushButton("🖥 Display 캡처"); self.cap_scr_btn.setFixedHeight(30)
        self.cap_scr_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:4px;font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#223344;}")
        self.cap_scr_btn.clicked.connect(self._on_cap_scr)
        self.cap_cam_btn=QPushButton("📷 Camera 캡처"); self.cap_cam_btn.setFixedHeight(30)
        self.cap_cam_btn.setStyleSheet(
            "QPushButton{background:#1a2a1a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:4px;font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#222a10;}")
        self.cap_cam_btn.clicked.connect(self._on_cap_cam)
        cap_btn_row.addWidget(self.cap_scr_btn); cap_btn_row.addWidget(self.cap_cam_btn)
        cap_v.addLayout(cap_btn_row)
        self.cap_lbl=QLabel(""); self.cap_lbl.setStyleSheet("color:#2ecc71;font-size:10px;")
        cap_v.addWidget(self.cap_lbl)
        v.addWidget(cap_g)

        # ── 컨트롤 버튼 ──────────────────────────────────────────────────────
        ctrl=QGroupBox("컨트롤"); cv=QVBoxLayout(ctrl); cv.setSpacing(8)
        self.btn_start=QPushButton("⏺  녹화 시작  [Ctrl+Alt+W]")
        self.btn_start.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;font-size:12px;padding:8px;"
            "border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#2ecc71;}"
            "QPushButton:disabled{background:#1a3a28;color:#4a7a5a;}")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop=QPushButton("⏹  녹화 종료  [Ctrl+Alt+E]")
        self.btn_stop.setStyleSheet(
            "QPushButton{background:#c0392b;color:white;font-size:12px;padding:8px;"
            "border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#e74c3c;}"
            "QPushButton:disabled{background:#3a1a1a;color:#7a4a4a;}")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)
        cv.addWidget(self.btn_start); cv.addWidget(self.btn_stop); v.addWidget(ctrl)

        # ── FPS / 배속 ────────────────────────────────────────────────────────
        fg=QGroupBox("FPS & 배속"); fl=QGridLayout(fg); fl.setSpacing(8)
        fl.addWidget(QLabel("화면 Target FPS:"),0,0)
        self.fps_spin=_make_spinbox(1,120,30,1,1); self.fps_spin.setFixedWidth(80)
        self.fps_spin.valueChanged.connect(lambda val: setattr(self.engine,'actual_screen_fps',val))
        fl.addWidget(self.fps_spin,0,1)
        fl.addWidget(QLabel("저장 배속:"),1,0)
        self.speed_spin=_make_spinbox(0.1,10,1,0.25,2); self.speed_spin.setFixedWidth(80)
        self.speed_spin.setStyleSheet(
            "QDoubleSpinBox{background:#1a1a2a;color:#f0c040;border:1px solid #5a5a20;"
            "border-radius:4px;padding:3px;font-size:13px;font-weight:bold;}")
        self.speed_spin.valueChanged.connect(self._on_speed)
        fl.addWidget(self.speed_spin,1,1)
        pr=QHBoxLayout(); pr.setSpacing(4)
        for lbl,val in [("0.5×",.5),("1×",1.),("2×",2.),("4×",4.)]:
            b=QPushButton(lbl); b.setFixedSize(40,24)
            b.setStyleSheet("QPushButton{background:#2a2a1a;color:#f0c040;"
                            "border:1px solid #4a4a20;border-radius:3px;font-size:10px;}")
            b.clicked.connect(lambda _,val=val: self.speed_spin.setValue(val))
            pr.addWidget(b)
        fl.addLayout(pr,2,0,1,2)
        self.speed_info=QLabel("정배속"); self.speed_info.setStyleSheet("color:#888;font-size:10px;")
        fl.addWidget(self.speed_info,3,0,1,2)
        self.speed_lock=QLabel("🔒 녹화 중 배속 변경 불가")
        self.speed_lock.setStyleSheet("color:#e74c3c;font-size:10px;font-weight:bold;")
        self.speed_lock.setVisible(False); fl.addWidget(self.speed_lock,4,0,1,2)
        v.addWidget(fg)

        # ── 용량 절감 ─────────────────────────────────────────────────────────
        cg=QGroupBox("🗜 영상 용량 절감"); cl=QGridLayout(cg); cl.setSpacing(6)
        cl.addWidget(QLabel("코덱:"),0,0)
        self.codec_cb=QComboBox()
        self.codec_cb.addItems(["mp4v (기본/안전)","avc1 / H.264 (소형, 지원 시)","xvid (호환성)"])
        self.codec_cb.setStyleSheet(
            "QComboBox{background:#1a1a3a;color:#ddd;border:1px solid #3a4a6a;border-radius:3px;padding:2px;}")
        self.codec_cb.currentIndexChanged.connect(self._on_codec)
        cl.addWidget(self.codec_cb,0,1)
        self.codec_warn=QLabel("※ avc1 지원 불가 시 자동으로 mp4v로 전환")
        self.codec_warn.setStyleSheet("color:#666;font-size:9px;"); self.codec_warn.setVisible(False)
        cl.addWidget(self.codec_warn,1,0,1,2)
        cl.addWidget(QLabel("해상도:"),2,0)
        self.scale_cb=QComboBox()
        self.scale_cb.addItems(["100% 원본","75%","50%"])
        self.scale_cb.setStyleSheet(
            "QComboBox{background:#1a1a3a;color:#ddd;border:1px solid #3a4a6a;border-radius:3px;padding:2px;}")
        self.scale_cb.currentIndexChanged.connect(self._on_scale)
        cl.addWidget(self.scale_cb,2,1)
        v.addWidget(cg)

        self._timer=QTimer(self); self._timer.timeout.connect(self._tick)

    # ── 슬롯 ─────────────────────────────────────────────────────────────────
    def _on_start(self):
        self.engine.start_recording()

    def _on_stop(self):
        if self.engine.tc_rec_enabled and self.engine.recording:
            result = show_tc_dialog(
                self, "녹화를 종료합니다.\nT/C 검증 결과를 선택하세요.",
                cancel_label="취소  (녹화 계속)")
            if result is None:
                return
            self.engine.tc_verify_result = result
        self.engine.stop_recording()

    def _on_cap_scr(self):
        if self.engine.tc_capture_enabled:
            result = show_tc_dialog(self, "캡처 전 T/C 검증 결과를 선택하세요.")
            if result is None: return
            # ★ 수정: tc_verify_result 설정 누락 → capture_frame에서 PASS/FAIL 경로 미적용
            self.engine.tc_verify_result = result
            self.engine.capture_frame("screen", tc_tag=result)
        else:
            self.engine.capture_frame("screen")

    def _on_cap_cam(self):
        if self.engine.tc_capture_enabled:
            result = show_tc_dialog(self, "캡처 전 T/C 검증 결과를 선택하세요.")
            if result is None: return
            # ★ 수정: tc_verify_result 설정 누락
            self.engine.tc_verify_result = result
            self.engine.capture_frame("camera", tc_tag=result)
        else:
            self.engine.capture_frame("camera")

    def _on_rec_started(self):
        self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True)
        self.status_lbl.setText("● 녹화 중")
        self.status_lbl.setStyleSheet("color:#2ecc71;font-weight:bold;font-size:14px;")
        self.speed_spin.setEnabled(False); self.speed_lock.setVisible(True)
        self._timer.start(500)

    def _on_rec_stopped(self):
        self.btn_start.setEnabled(True); self.btn_stop.setEnabled(False)
        self.status_lbl.setText("● 대기")
        self.status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;font-size:14px;")
        self.timer_lbl.setText("00:00:00"); self.datetime_lbl.setText("—")
        self.speed_spin.setEnabled(True); self.speed_lock.setVisible(False)
        self._timer.stop()
        # ★ 수정: TC 결과로 폴더명이 바뀌었을 수 있으므로 _folder_btn 경로 갱신
        try:
            self._folder_btn.clicked.disconnect()
        except Exception:
            pass
        final_dir = self.engine.output_dir or self.engine.base_dir
        self._folder_btn.clicked.connect(lambda: open_folder(final_dir))

    def _tick(self):
        if self.engine.recording and self.engine.start_time:
            e=time.time()-self.engine.start_time
            self.timer_lbl.setText(fmt_hms(e))
            self.datetime_lbl.setText(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))

    def _on_capture_saved(self, source, path):
        self.cap_lbl.setText(f"✅ {source}: {os.path.basename(path)}")
        QTimer.singleShot(4000, lambda: self.cap_lbl.setText(""))

    def _on_speed(self, val):
        self.engine.playback_speed=val
        if val==1.0: t="정배속"
        elif val<1.0: t=f"{val:.2f}× 슬로우모션"
        else:          t=f"{val:.2f}× 타임랩스"
        self.speed_info.setText(t)

    def _on_codec(self, i):
        codecs=["mp4v","avc1","xvid"]
        self.engine.video_codec=codecs[i] if i<len(codecs) else "mp4v"
        self.codec_warn.setVisible(i==1)

    def _on_scale(self, i):
        self.engine.video_scale=[1.0,0.75,0.5][i]

    def update_fps(self, scr: float, cam: float):
        self.scr_fps_lbl.setText(f"{scr:.1f} fps")
        self.cam_fps_lbl.setText(f"{cam:.1f} fps")


# =============================================================================
