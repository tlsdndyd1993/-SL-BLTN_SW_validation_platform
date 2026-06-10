# -*- coding: utf-8 -*-
"""
ui_swc/roi_manager_panel.py
ROI/OCR 관리 패널
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

import numpy as np
import cv2
class RoiEditDialog(QDialog):
    """ROI 추가/편집 시 이름과 설명을 입력하는 다이얼로그."""
    def __init__(self, name="", description="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("ROI 정보 입력")
        self.setFixedSize(380, 200)
        self.setStyleSheet("QDialog{background:#0d0d1e;}QLabel{color:#dde;}")
        lay = QVBoxLayout(self); lay.setSpacing(12); lay.setContentsMargins(20,16,20,16)

        lay.addWidget(QLabel("ROI 제목 (선택)"))
        self._name_ed = QLineEdit(name)
        self._name_ed.setPlaceholderText("예: 클러스터 경고등 영역")
        self._name_ed.setStyleSheet(
            "QLineEdit{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:3px;padding:4px 8px;font-size:12px;}")
        lay.addWidget(self._name_ed)

        lay.addWidget(QLabel("설명 (선택)"))
        self._desc_ed = QLineEdit(description)
        self._desc_ed.setPlaceholderText("예: 전방 카메라 블랙아웃 감지용")
        self._desc_ed.setStyleSheet(
            "QLineEdit{background:#1a1a3a;color:#ccd;border:1px solid #334;"
            "border-radius:3px;padding:4px 8px;font-size:11px;}")
        lay.addWidget(self._desc_ed)

        btn_row = QHBoxLayout(); btn_row.setSpacing(10)
        ok_btn = QPushButton("✅ 확인")
        ok_btn.setMinimumHeight(34)
        ok_btn.setStyleSheet(
            "QPushButton{background:#1a6a3a;color:#afffcf;font-size:12px;"
            "font-weight:bold;border-radius:5px;border:none;}"
            "QPushButton:hover{background:#27ae60;}")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("취소")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setStyleSheet(
            "QPushButton{background:#1a1a2a;color:#888;border:1px solid #334;"
            "border-radius:4px;font-size:11px;}")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn); btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

    @property
    def name(self) -> str:
        return self._name_ed.text().strip()

    @property
    def description(self) -> str:
        return self._desc_ed.text().strip()


class RoiManagerPanel(QWidget):
    """
    ROI 항목을 목록으로 표시하고 드래그앤드랍으로 순서를 변경.
    순서 변경 시 engine.screen_rois / camera_rois 도 동일하게 반영.
    """
    changed = pyqtSignal()   # ROI 추가/삭제/순서변경 시 emit

    _ROW_H  = 46
    _BG     = "#0a0a18"
    _BG_HV  = "#12122a"
    _BG_DG  = "#1a2a4a"

    def __init__(self, engine: "CoreEngine", signals: "Signals", parent=None):
        super().__init__(parent)
        self.engine  = engine
        self.signals = signals
        self._drag_idx   = -1
        self._drag_start = QPoint()
        self._dragging   = False
        self._source_tab = "screen"   # 현재 표시 소스
        self._build()
        signals.roi_list_changed.connect(self._refresh)

    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,4); v.setSpacing(6)

        # ── OCR / 밝기 ON/OFF 제어 바 — [문제2] 총 4개 독립 버튼 ──────────
        # 1행: Display OCR / Display 밝기
        # 2행: Camera OCR  / Camera 밝기
        _ocr_ss = ("QPushButton{background:#0a3a4a;color:#3de;"
                   "border:1px solid #1a6a7a;border-radius:4px;"
                   "font-size:10px;font-weight:bold;}"
                   "QPushButton:!checked{background:#0a0a18;color:#334;"
                   "border-color:#1a1a2a;}"
                   "QPushButton:hover{background:#0d4a5a;}")
        _brt_ss = ("QPushButton{background:#3a3a0a;color:#ff0;"
                   "border:1px solid #6a6a1a;border-radius:4px;"
                   "font-size:10px;font-weight:bold;}"
                   "QPushButton:!checked{background:#0a0a18;color:#334;"
                   "border-color:#1a1a2a;}"
                   "QPushButton:hover{background:#4a4a0d;}")

        # --- 1행: Display ---
        row1 = QHBoxLayout(); row1.setSpacing(4)
        self._ocr_scr_btn = QPushButton("🖥 Display OCR ▶ ON")
        self._ocr_scr_btn.setCheckable(True); self._ocr_scr_btn.setChecked(True)
        self._ocr_scr_btn.setFixedHeight(24)
        self._ocr_scr_btn.setToolTip("Display ROI OCR ON/OFF")
        self._ocr_scr_btn.setStyleSheet(_ocr_ss)
        self._ocr_scr_btn.toggled.connect(self._on_ocr_screen_toggle)

        self._bright_scr_btn = QPushButton("🖥 Display 밝기 ▶ ON")
        self._bright_scr_btn.setCheckable(True); self._bright_scr_btn.setChecked(True)
        self._bright_scr_btn.setFixedHeight(24)
        self._bright_scr_btn.setToolTip("Display ROI 밝기 계산 ON/OFF")
        self._bright_scr_btn.setStyleSheet(_brt_ss)
        self._bright_scr_btn.toggled.connect(self._on_bright_screen_toggle)

        row1.addWidget(self._ocr_scr_btn, 1)
        row1.addWidget(self._bright_scr_btn, 1)
        v.addLayout(row1)

        # --- 2행: Camera ---
        row2 = QHBoxLayout(); row2.setSpacing(4)
        self._ocr_cam_btn = QPushButton("📷 Camera OCR ▶ ON")
        self._ocr_cam_btn.setCheckable(True); self._ocr_cam_btn.setChecked(True)
        self._ocr_cam_btn.setFixedHeight(24)
        self._ocr_cam_btn.setToolTip("Camera ROI OCR ON/OFF")
        self._ocr_cam_btn.setStyleSheet(_ocr_ss)
        self._ocr_cam_btn.toggled.connect(self._on_ocr_camera_toggle)

        self._bright_cam_btn = QPushButton("📷 Camera 밝기 ▶ ON")
        self._bright_cam_btn.setCheckable(True); self._bright_cam_btn.setChecked(True)
        self._bright_cam_btn.setFixedHeight(24)
        self._bright_cam_btn.setToolTip("Camera ROI 밝기 계산 ON/OFF")
        self._bright_cam_btn.setStyleSheet(_brt_ss)
        self._bright_cam_btn.toggled.connect(self._on_bright_camera_toggle)

        row2.addWidget(self._ocr_cam_btn, 1)
        row2.addWidget(self._bright_cam_btn, 1)
        v.addLayout(row2)

        # 하위 호환 별칭
        self._ocr_btn   = self._ocr_scr_btn
        self._bright_btn = self._bright_scr_btn

        # 소스 탭 선택
        tab_row = QHBoxLayout(); tab_row.setSpacing(6)
        self._scr_tab = QPushButton("🖥 Display ROI")
        self._scr_tab.setCheckable(True); self._scr_tab.setChecked(True)
        self._cam_tab = QPushButton("📷 Camera ROI")
        self._cam_tab.setCheckable(True)
        _tab_style = (
            "QPushButton{background:#1a1a3a;color:#888;border:1px solid #334;"
            "border-radius:4px;font-size:11px;padding:4px 10px;}"
            "QPushButton:checked{background:#1a2a4a;color:#7bc8e0;"
            "border-color:#2a6aaa;font-weight:bold;}"
            "QPushButton:hover{background:#22224a;}")
        self._scr_tab.setStyleSheet(_tab_style)
        self._cam_tab.setStyleSheet(_tab_style)
        self._scr_tab.clicked.connect(lambda: self._set_source("screen"))
        self._cam_tab.clicked.connect(lambda: self._set_source("camera"))
        tab_row.addWidget(self._scr_tab); tab_row.addWidget(self._cam_tab)
        tab_row.addStretch()
        v.addLayout(tab_row)

        # 힌트
        hint = QLabel("⠿ 드래그: 순서변경  |  ✕: 삭제  |  ✏: 편집")
        hint.setStyleSheet("color:#446;font-size:9px;")
        v.addWidget(hint)

        # 목록 스크롤 영역
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea{border:1px solid #1a2a3a;background:#08081a;}"
            "QScrollBar:vertical{background:#0d0d1e;width:6px;border-radius:3px;}"
            "QScrollBar::handle:vertical{background:#336;border-radius:3px;min-height:16px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        self._list_w = QWidget()
        self._list_w.setStyleSheet(f"background:{self._BG};")
        self._list_lay = QVBoxLayout(self._list_w)
        self._list_lay.setContentsMargins(4,4,4,4); self._list_lay.setSpacing(3)
        scroll.setWidget(self._list_w)
        scroll.setFixedHeight(160)
        v.addWidget(scroll)

        # 통계 레이블
        self._stat_lbl = QLabel("ROI 없음")
        self._stat_lbl.setStyleSheet("color:#556;font-size:10px;")
        v.addWidget(self._stat_lbl)

        # 전체 삭제
        clr_btn = QPushButton("🗑 현재 소스 ROI 전체 삭제")
        clr_btn.setFixedHeight(26)
        clr_btn.setStyleSheet(
            "QPushButton{background:#2a1a1a;color:#f88;border:1px solid #5a2a2a;"
            "border-radius:4px;font-size:10px;}"
            "QPushButton:hover{background:#3a1a1a;}")
        clr_btn.clicked.connect(self._clear_all)
        v.addWidget(clr_btn)

        self._refresh()

    def _set_source(self, source: str):
        self._source_tab = source
        self._scr_tab.setChecked(source == "screen")
        self._cam_tab.setChecked(source == "camera")
        self._refresh()

    def _current_rois(self) -> List[RoiItem]:
        return (self.engine.screen_rois if self._source_tab == "screen"
                else self.engine.camera_rois)

    def _refresh(self):
        # 목록 재빌드
        while self._list_lay.count():
            it = self._list_lay.takeAt(0)
            if it.widget(): it.widget().setParent(None)

        rois = self._current_rois()
        for i, roi in enumerate(rois):
            row = self._make_row(i, roi)
            self._list_lay.addWidget(row)
        self._list_lay.addStretch()

        total_scr = len(self.engine.screen_rois)
        total_cam = len(self.engine.camera_rois)
        self._stat_lbl.setText(
            f"Display: {total_scr}개  |  Camera: {total_cam}개")

    _ROW_H  = 80   # [문제3] NO→REC 버튼 행 추가로 높이 확장

    def _make_row(self, idx: int, roi: RoiItem) -> QFrame:
        row = QFrame()
        row.setFixedHeight(self._ROW_H)
        row.setObjectName(f"roi_row_{idx}")
        # ★ f-string 제거: Qt가 동적 selector를 파싱 못해 WARN 발생
        row.setStyleSheet(
            "QFrame{background:#0d0d1e;border:1px solid #1a1a3a;"
            "border-radius:4px;}"
            "QFrame:hover{background:#12122e;}")
        row.setCursor(Qt.OpenHandCursor)

        outer = QVBoxLayout(row)
        outer.setContentsMargins(6,3,6,3); outer.setSpacing(2)

        # ── 상단 행: 핸들 + 번호 + 이름/위치 + 실시간값 + 버튼 ──────────
        top = QHBoxLayout(); top.setSpacing(5); top.setContentsMargins(0,0,0,0)

        grip = QLabel("⠿"); grip.setFixedWidth(14)
        grip.setStyleSheet("color:#4a5a8a;font-size:17px;background:transparent;")

        num = QLabel(f"{idx+1}"); num.setFixedWidth(16)
        num.setStyleSheet("color:#556;font-size:10px;background:transparent;")
        num.setAlignment(Qt.AlignCenter)

        name_lbl = QLabel(roi.label())
        name_lbl.setStyleSheet(
            "color:#7bc8e0;font-size:11px;font-weight:bold;background:transparent;")
        pos_lbl = QLabel(f"({roi.x},{roi.y}) {roi.w}×{roi.h}")
        pos_lbl.setStyleSheet("color:#556;font-size:9px;background:transparent;")
        info_col = QVBoxLayout(); info_col.setSpacing(0)
        info_col.addWidget(name_lbl); info_col.addWidget(pos_lbl)

        # ── 실시간 OCR 값 표시 레이블 ★ ────────────────────────────────
        row._ocr_lbl = QLabel("OCR: —")
        row._ocr_lbl.setFixedWidth(110)
        row._ocr_lbl.setStyleSheet(
            "color:#3de;font-size:10px;font-weight:bold;"
            "background:#080818;border:1px solid #1a3a4a;"
            "border-radius:3px;padding:1px 4px;")
        row._ocr_lbl.setAlignment(Qt.AlignCenter)
        row._roi_idx = idx   # 나중에 타이머 업데이트용

        edit_btn = QPushButton("✏"); edit_btn.setFixedSize(22,22)
        edit_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;border:none;"
            "border-radius:3px;font-size:11px;}"
            "QPushButton:hover{background:#223344;}")
        edit_btn.clicked.connect(lambda _, i=idx: self._edit_roi(i))

        del_btn = QPushButton("✕"); del_btn.setFixedSize(22,22)
        del_btn.setStyleSheet(
            "QPushButton{background:#3a1a1a;color:#f88;border:none;"
            "border-radius:3px;font-size:12px;}"
            "QPushButton:hover{background:#7f2020;}")
        del_btn.clicked.connect(lambda _, i=idx: self._del_roi(i))

        top.addWidget(grip); top.addWidget(num)
        top.addLayout(info_col, 1)
        top.addWidget(row._ocr_lbl)
        top.addWidget(edit_btn); top.addWidget(del_btn)
        outer.addLayout(top)

        # ── 하단 행: 조건 입력 UI ★ ─────────────────────────────────────
        # "OCR값이 [  입력  ] 일 때 → 변수: roi_{idx}_match = True"
        bot = QHBoxLayout(); bot.setSpacing(4); bot.setContentsMargins(30,0,4,0)

        cond_lbl = QLabel("OCR =")
        cond_lbl.setStyleSheet("color:#667;font-size:9px;background:transparent;")
        bot.addWidget(cond_lbl)

        row._cond_ed = QLineEdit(roi.cond_value)
        row._cond_ed.setPlaceholderText("비교값 (예: 2  또는  OK)")
        row._cond_ed.setFixedHeight(18)
        row._cond_ed.setStyleSheet(
            "QLineEdit{background:#08080e;color:#f0c040;"
            "border:1px solid #3a3a20;border-radius:2px;"
            "font-size:10px;padding:0 4px;}")
        row._cond_ed.textChanged.connect(
            lambda t, i=idx: self._on_cond_changed(i, t))
        bot.addWidget(row._cond_ed, 1)

        # 매치 결과 표시
        row._match_lbl = QLabel("—")
        row._match_lbl.setFixedWidth(50)
        row._match_lbl.setStyleSheet(
            "color:#556;font-size:9px;background:transparent;")
        row._match_lbl.setAlignment(Qt.AlignCenter)
        bot.addWidget(row._match_lbl)

        # [문제3] NO 판정 시 자동녹화 ON/OFF 버튼 (ROI별)
        row._no_rec_btn = QPushButton("NO→REC OFF")
        row._no_rec_btn.setCheckable(True)
        row._no_rec_btn.setChecked(roi.roi_no_rec_enabled)
        row._no_rec_btn.setFixedHeight(18)
        row._no_rec_btn.setFixedWidth(88)
        row._no_rec_btn.setStyleSheet(
            "QPushButton{background:#1a0a0a;color:#887;border:1px solid #3a2a2a;"
            "border-radius:3px;font-size:9px;font-weight:bold;}"
            "QPushButton:checked{background:#3a0a0a;color:#f87;"
            "border-color:#8a2a2a;}"
            "QPushButton:hover{background:#2a1010;}")
        row._no_rec_btn.toggled.connect(
            lambda on, i=idx, btn=row._no_rec_btn: self._on_no_rec_toggled(i, on, btn))
        bot.addWidget(row._no_rec_btn)

        # [문제3] NO→REC 카운트 레이블
        row._no_count_lbl = QLabel(f"×{roi.no_match_count}")
        row._no_count_lbl.setFixedWidth(28)
        row._no_count_lbl.setStyleSheet(
            "color:#f87;font-size:9px;font-weight:bold;background:transparent;")
        row._no_count_lbl.setAlignment(Qt.AlignCenter)
        row._no_count_lbl.setToolTip("MATCH→NO 전환 횟수")
        bot.addWidget(row._no_count_lbl)

        # [문제3] NO→REC 저장 폴더 열기 버튼
        row._no_folder_btn = QPushButton("📂")
        row._no_folder_btn.setFixedSize(20, 18)
        row._no_folder_btn.setStyleSheet(
            "QPushButton{background:#0a1a0a;color:#7fdb9e;border:1px solid #1a3a1a;"
            "border-radius:3px;font-size:9px;}"
            "QPushButton:hover{background:#0d2a0d;}")
        row._no_folder_btn.setToolTip("NO→REC 저장 폴더 열기")
        row._no_folder_btn.clicked.connect(
            lambda _, i=idx: self._open_no_rec_folder(i))
        bot.addWidget(row._no_folder_btn)

        outer.addLayout(bot)

        # 마우스 이벤트 (드래그앤드랍)
        row.mousePressEvent   = lambda e, i=idx: self._on_press(e, i)
        row.mouseMoveEvent    = lambda e: self._on_move(e)
        row.mouseReleaseEvent = lambda e: self._on_release(e)

        # 행 위젯 참조 저장 (타이머 업데이트용)
        if not hasattr(self, '_row_widgets'):
            self._row_widgets = {}
        self._row_widgets[idx] = row

        return row

    # ── 드래그앤드랍 ─────────────────────────────────────────────────────────
    def _on_press(self, e, idx):
        if e.button() == Qt.LeftButton:
            self._drag_idx   = idx
            self._drag_start = e.globalPos()

    def _on_move(self, e):
        if self._drag_idx >= 0 and not self._dragging:
            if (e.globalPos()-self._drag_start).manhattanLength() > 6:
                self._dragging = True

    def _on_release(self, e):
        if self._dragging and self._drag_idx >= 0:
            y_rel  = self._list_w.mapFromGlobal(e.globalPos()).y()
            tgt    = max(0, min(y_rel // self._ROW_H,
                                len(self._current_rois())-1))
            rois   = self._current_rois()
            if tgt != self._drag_idx and 0 <= tgt < len(rois):
                rois.insert(tgt, rois.pop(self._drag_idx))
                self.signals.roi_list_changed.emit()
                self.changed.emit()
        self._drag_idx  = -1
        self._dragging  = False

    # ── OCR 조건값 변경 ───────────────────────────────────────────────────
    def _on_ocr_toggle(self, on: bool):
        """하위 호환용 — Display OCR 토글과 동일."""
        self._on_ocr_screen_toggle(on)

    def _on_ocr_screen_toggle(self, on: bool):
        """★ v4.5: Display(화면) ROI OCR 독립 ON/OFF."""
        self.engine.ocr_screen_enabled = on
        self.engine.ocr_enabled = on  # 하위 호환 유지
        self._ocr_scr_btn.setText("🖥 Display OCR ▶ ON" if on else "🖥 Display OCR ⏸ OFF")

    def _on_ocr_camera_toggle(self, on: bool):
        """★ v4.5: Camera ROI OCR 독립 ON/OFF."""
        self.engine.ocr_camera_enabled = on
        self._ocr_cam_btn.setText("📷 Camera OCR ▶ ON" if on else "📷 Camera OCR ⏸ OFF")

    def _on_bright_screen_toggle(self, on: bool):
        """[문제2] Display ROI 밝기 ON/OFF."""
        self.engine.brightness_screen_enabled = on
        # 하위 호환: 둘 다 OFF일 때만 전체 밝기 비활성
        cam_on = getattr(self.engine, 'brightness_camera_enabled', True)
        self.engine.brightness_enabled = on or cam_on
        self._bright_scr_btn.setText("🖥 Display 밝기 ▶ ON" if on else "🖥 Display 밝기 ⏸ OFF")
        if not on:
            for roi in self.engine.screen_rois:
                roi.last_brightness = 0.0
                roi.last_avg_bgr    = (0, 0, 0)

    def _on_bright_camera_toggle(self, on: bool):
        """[문제2] Camera ROI 밝기 ON/OFF."""
        self.engine.brightness_camera_enabled = on
        scr_on = getattr(self.engine, 'brightness_screen_enabled', True)
        self.engine.brightness_enabled = scr_on or on
        self._bright_cam_btn.setText("📷 Camera 밝기 ▶ ON" if on else "📷 Camera 밝기 ⏸ OFF")
        if not on:
            for roi in self.engine.camera_rois:
                roi.last_brightness = 0.0
                roi.last_avg_bgr    = (0, 0, 0)

    def _on_bright_toggle(self, on: bool):
        """하위 호환용 — Display 밝기 토글과 동일."""
        self._on_bright_screen_toggle(on)

    def _on_cond_changed(self, idx: int, value: str):
        """사용자가 조건 입력란을 수정하면 RoiItem.cond_value 업데이트."""
        rois = self._current_rois()
        if 0 <= idx < len(rois):
            rois[idx].cond_value = value.strip()

    def _on_no_rec_toggled(self, idx: int, on: bool, btn: "QPushButton"):
        """[문제3] ROI별 NO→자동녹화 ON/OFF."""
        rois = self._current_rois()
        if 0 <= idx < len(rois):
            rois[idx].roi_no_rec_enabled = on
        btn.setText("NO→REC ON" if on else "NO→REC OFF")
        self.changed.emit()

    # ── OCR 레이블 실시간 갱신 (QTimer 30fps) ────────────────────────────
    def start_ocr_refresh(self):
        """미리보기 업데이트와 연동해서 OCR 레이블을 주기적으로 갱신."""
        if hasattr(self, '_ocr_timer') and self._ocr_timer.isActive():
            return
        self._ocr_timer = QTimer()
        self._ocr_timer.setInterval(200)   # 200ms(5fps) — OCR 갱신은 느려도 충분
        self._ocr_timer.timeout.connect(self.refresh_ocr_labels)
        self._ocr_timer.start()

    def refresh_ocr_labels(self):
        """
        각 ROI 행의 OCR 레이블과 매치 결과를 최신 캐시로 업데이트.
        부하 없음 — RoiItem.last_text 캐시만 읽음.
        """
        rois = self._current_rois()
        rows = getattr(self, '_row_widgets', {})
        for idx, roi in enumerate(rois):
            row = rows.get(idx)
            if row is None:
                continue

            # OCR 값 레이블 갱신
            ocr_lbl = getattr(row, '_ocr_lbl', None)
            if ocr_lbl:
                txt = roi.last_text.strip() if roi.last_text else ""
                if txt and not txt.startswith("~"):
                    ocr_lbl.setText(f"OCR: {txt[:14]}")
                    ocr_lbl.setStyleSheet(
                        "color:#3de;font-size:10px;font-weight:bold;"
                        "background:#080818;border:1px solid #1a5a6a;"
                        "border-radius:3px;padding:1px 4px;")
                else:
                    ocr_lbl.setText("OCR: —")
                    ocr_lbl.setStyleSheet(
                        "color:#446;font-size:10px;font-weight:bold;"
                        "background:#080818;border:1px solid #1a2a3a;"
                        "border-radius:3px;padding:1px 4px;")

            # 조건 매치 결과 표시
            match_lbl = getattr(row, '_match_lbl', None)
            cond_ed   = getattr(row, '_cond_ed',   None)
            if match_lbl and cond_ed:
                cond = roi.cond_value.strip()
                txt  = roi.last_text.strip() if roi.last_text else ""
                if not cond:
                    match_lbl.setText("—")
                    match_lbl.setStyleSheet("color:#446;font-size:9px;")
                else:
                    matched = self._eval_cond(txt, cond)
                    prev_match = roi.last_match
                    if matched:
                        match_lbl.setText("✅ MATCH")
                        match_lbl.setStyleSheet(
                            "color:#2ecc71;font-size:9px;font-weight:bold;")
                    else:
                        match_lbl.setText("✗ NO")
                        match_lbl.setStyleSheet(
                            "color:#e74c3c;font-size:9px;font-weight:bold;")
                    # RoiItem에 결과 캐시
                    roi.last_match = matched

                    # [문제3] NO 판정 시 자동녹화 트리거
                    # prev_match=True → matched=False 로 전환된 순간만 트리거
                    # (매 루프마다 반복 트리거 방지)
                    if (not matched and prev_match
                            and roi.roi_no_rec_enabled
                            and cond):
                        self._trigger_no_rec(roi)

            # NO→REC 카운트 레이블 갱신
            no_count_lbl = getattr(row, '_no_count_lbl', None)
            if no_count_lbl:
                no_count_lbl.setText(f"×{roi.no_match_count}")

            # NO→REC 버튼 텍스트 동기화 (DB 복원 후 초기화 시)
            no_rec_btn = getattr(row, '_no_rec_btn', None)
            if no_rec_btn:
                label = "NO→REC ON" if roi.roi_no_rec_enabled else "NO→REC OFF"
                if no_rec_btn.text() != label:
                    no_rec_btn.blockSignals(True)
                    no_rec_btn.setChecked(roi.roi_no_rec_enabled)
                    no_rec_btn.setText(label)
                    no_rec_btn.blockSignals(False)

    @staticmethod
    def _eval_cond(ocr_text: str, cond: str) -> bool:
        """
        OCR 텍스트와 조건값을 비교.
        - 숫자면 수치 비교 (==, !=, >, <, >=, <= 지원)
        - 문자면 대소문자 무시 포함 비교
        """
        import re as _re
        ocr_clean = ocr_text.strip()
        cond_clean = cond.strip()
        if not cond_clean:
            return False

        # 연산자 포함 조건 (예: ">=2", "<100", "!=OK")
        m = _re.match(r'^(==|!=|>=|<=|>|<)\s*(.+)$', cond_clean)
        if m:
            op, val = m.group(1), m.group(2).strip()
            try:
                lhs = float(ocr_clean); rhs = float(val)
                return eval(f"{lhs}{op}{rhs}")
            except ValueError:
                # 문자 비교
                lhs, rhs = ocr_clean.lower(), val.lower()
                if   op == "==": return lhs == rhs
                elif op == "!=": return lhs != rhs
                else:            return False

        # 단순 비교: 숫자 또는 문자열
        try:
            return float(ocr_clean) == float(cond_clean)
        except ValueError:
            return ocr_clean.lower() == cond_clean.lower()

    # ── ROI 편집 ─────────────────────────────────────────────────────────────
    def _edit_roi(self, idx: int):
        rois = self._current_rois()
        if not 0 <= idx < len(rois): return
        roi = rois[idx]
        dlg = RoiEditDialog(name=roi.name, description=roi.description, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            roi.name        = dlg.name
            roi.description = dlg.description
            self.signals.roi_list_changed.emit()
            self.changed.emit()

    def _del_roi(self, idx: int):
        rois = self._current_rois()
        if 0 <= idx < len(rois):
            rois.pop(idx)
            self.signals.roi_list_changed.emit()
            self.changed.emit()

    def _trigger_no_rec(self, roi: RoiItem):
        """
        [문제3] OCR 조건 NO 판정 시 자동녹화 트리거.
        수동녹화(pre=-10초, post=+10초)와 동일한 방식으로 클립 저장.
        블랙아웃 클립과 동시에 실행 가능 (독립 스레드).
        저장 경로: 기존 저장 경로 설정에 따름 (독립 저장).
        """
        engine = self.engine
        # 카운트 증가
        roi.no_match_count += 1
        try:
            # 기존 수동녹화 설정 백업
            orig_pre  = engine.manual_pre_sec
            orig_post = engine.manual_post_sec
            # -10초 ~ +10초 고정 (요구사항)
            engine.manual_pre_sec  = 10.0
            engine.manual_post_sec = 10.0

            roi_label = roi.label()
            self.signals.status_message.emit(
                f"[ROI NO→REC] '{roi_label}' OCR 불일치 → 자동 클립 저장 (×{roi.no_match_count})")

            def _save_clip():
                try:
                    engine.save_manual_clip()
                except Exception:
                    pass
                finally:
                    engine.manual_pre_sec  = orig_pre
                    engine.manual_post_sec = orig_post

            import threading as _th
            _th.Thread(target=_save_clip, daemon=True,
                       name=f"ROI_NO_REC_{roi_label[:10]}").start()
        except Exception:
            pass

    def _open_no_rec_folder(self, idx: int):
        """[문제3] NO→REC 저장 폴더를 탐색기로 열기."""
        try:
            p = self.engine._build_path("manual")
            while p and not os.path.exists(p) and p != os.path.dirname(p):
                p = os.path.dirname(p)
            if p and os.path.exists(p):
                open_folder(p)
            else:
                open_folder(self.engine.base_dir)
        except Exception:
            pass

    def _clear_all(self):
        self._current_rois().clear()
        self.signals.roi_list_changed.emit()
        self.changed.emit()


