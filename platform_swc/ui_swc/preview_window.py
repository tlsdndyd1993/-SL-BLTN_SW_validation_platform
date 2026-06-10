# -*- coding: utf-8 -*-
"""
ui_swc/preview_window.py
미리보기 창 위젯
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

import numpy as np
import cv2
class PreviewLabel(QLabel):
    """
    미리보기 위젯 — 휠 줌 + 패닝 + ROI 드래그 지원.

    ── 조작법 ──
    마우스 휠 위/아래  : 확대/축소 (최소 1x, 최대 10x)
    중클릭 드래그      : 화면 이동(패닝)
    좌클릭 드래그      : ROI 영역 지정
    우클릭             : 마지막 ROI 삭제
    더블클릭           : 줌 리셋 (1x, 원점)
    """
    roi_changed = pyqtSignal()

    # 줌 설정
    ZOOM_MIN  = 1.0
    ZOOM_MAX  = 10.0
    ZOOM_STEP = 0.15   # 휠 1칸당 배율 변화

    def __init__(self, source: str, engine: "CoreEngine", parent=None):
        super().__init__(parent)
        self.source = source
        self.engine = engine
        self._drawing = False
        self._pt1 = self._pt2 = QPoint()
        self._raw_size = (1, 1)
        self._active   = True

        # ── 줌/패닝 상태 ──────────────────────────────────────────────
        self._zoom        = 1.0         # 현재 배율
        self._pan_x       = 0.0         # 패닝 오프셋 (raw 픽셀 단위)
        self._pan_y       = 0.0
        self._pan_drag    = False        # 중클릭 드래그 중
        self._pan_start   = QPoint()     # 드래그 시작점 (위젯 좌표)
        self._pan_ox      = 0.0          # 드래그 시작 시 오프셋 백업
        self._pan_oy      = 0.0
        self._last_frame: np.ndarray = None   # 최신 프레임 캐시

        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAlignment(Qt.AlignCenter)
        self.setMouseTracking(True)
        self._idle()

    def _idle(self):
        self.setStyleSheet("background:#0d0d1e;border:1px solid #334;")

    def set_active(self, v: bool):
        self._active = v
        if not v:
            self.clear(); self.setText("⏸ Thread Paused")
            self.setStyleSheet("background:#0d0d1e;border:1px solid #334;"
                               "color:#555;font-size:18px;font-weight:bold;")
        else:
            self.clear(); self._idle()

    def _rois(self) -> List[RoiItem]:
        return (self.engine.screen_rois if self.source == "screen"
                else self.engine.camera_rois)

    # ── 좌표 변환 ─────────────────────────────────────────────────────
    def _widget_to_raw(self, qp: QPoint) -> tuple:
        """위젯 좌표 → 원본 프레임 좌표 (줌/패닝 반영)."""
        pw, ph = self.width(), self.height()
        rw, rh = self._raw_size
        # 줌 적용 후 가시 영역 크기
        vw = rw / self._zoom
        vh = rh / self._zoom
        # 가시 영역의 원본 프레임 내 시작점
        sx = max(0, min(self._pan_x, rw - vw))
        sy = max(0, min(self._pan_y, rh - vh))
        # 위젯에서 가시 영역 배치 (aspect-ratio 유지)
        disp_sc = min(pw / vw, ph / vh)
        ox = (pw - vw * disp_sc) / 2
        oy = (ph - vh * disp_sc) / 2
        raw_x = sx + (qp.x() - ox) / disp_sc
        raw_y = sy + (qp.y() - oy) / disp_sc
        return int(max(0, min(raw_x, rw-1))), int(max(0, min(raw_y, rh-1)))

    def _clamp_pan(self):
        rw, rh = self._raw_size
        vw = rw / self._zoom
        vh = rh / self._zoom
        self._pan_x = max(0, min(self._pan_x, rw - vw))
        self._pan_y = max(0, min(self._pan_y, rh - vh))

    def _reset_zoom(self):
        self._zoom  = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0

    # ── 마우스 이벤트 ─────────────────────────────────────────────────
    def wheelEvent(self, e):
        """휠 줌 — 커서 위치를 중심으로 확대/축소."""
        if not self._active:
            return
        delta = e.angleDelta().y()
        if delta == 0:
            return

        # 줌 전 커서의 raw 좌표
        raw_before_x, raw_before_y = self._widget_to_raw(e.pos())

        # 배율 변경
        factor = 1 + self.ZOOM_STEP * (1 if delta > 0 else -1)
        new_zoom = max(self.ZOOM_MIN, min(self.ZOOM_MAX, self._zoom * factor))
        if new_zoom == self._zoom:
            return
        self._zoom = new_zoom

        # 줌 후에도 커서 위치가 같은 raw 좌표를 가리키도록 패닝 조정
        rw, rh = self._raw_size
        vw = rw / self._zoom
        vh = rh / self._zoom
        pw, ph = self.width(), self.height()
        disp_sc = min(pw / vw, ph / vh)
        ox = (pw - vw * disp_sc) / 2
        oy = (ph - vh * disp_sc) / 2
        self._pan_x = raw_before_x - (e.pos().x() - ox) / disp_sc
        self._pan_y = raw_before_y - (e.pos().y() - oy) / disp_sc
        self._clamp_pan()

        # 줌 표시 오버레이 (잠깐)
        self._zoom_hint_frames = 10
        if self._last_frame is not None:
            self._render_frame(self._last_frame)
        e.accept()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drawing = True
            self._pt1 = self._pt2 = e.pos()
        elif e.button() == Qt.MidButton:
            # 중클릭: 패닝 시작
            self._pan_drag  = True
            self._pan_start = e.pos()
            self._pan_ox    = self._pan_x
            self._pan_oy    = self._pan_y
            self.setCursor(Qt.ClosedHandCursor)
        elif e.button() == Qt.RightButton:
            rois = self._rois()
            if rois:
                rois.pop()
                self.roi_changed.emit()

    def mouseMoveEvent(self, e):
        if self._drawing:
            self._pt2 = e.pos()
            self.update()
        elif self._pan_drag:
            rw, rh = self._raw_size
            vw = rw / self._zoom
            vh = rh / self._zoom
            pw, ph = self.width(), self.height()
            disp_sc = min(pw / vw, ph / vh)
            dx = (e.pos().x() - self._pan_start.x()) / disp_sc
            dy = (e.pos().y() - self._pan_start.y()) / disp_sc
            self._pan_x = self._pan_ox - dx
            self._pan_y = self._pan_oy - dy
            self._clamp_pan()
            if self._last_frame is not None:
                self._render_frame(self._last_frame)

    def mouseReleaseEvent(self, e):
        if self._drawing and e.button() == Qt.LeftButton:
            self._drawing = False
            x1, y1 = self._widget_to_raw(self._pt1)
            x2, y2 = self._widget_to_raw(self._pt2)
            rx, ry = min(x1,x2), min(y1,y2)
            rw, rh = abs(x1-x2), abs(y1-y2)
            if rw > 5 and rh > 5 and len(self._rois()) < 10:
                dlg = RoiEditDialog(parent=self)
                if dlg.exec_() == QDialog.Accepted:
                    roi = RoiItem(x=rx, y=ry, w=rw, h=rh,
                                  name=dlg.name, description=dlg.description,
                                  source=self.source)
                else:
                    roi = RoiItem(x=rx, y=ry, w=rw, h=rh, source=self.source)
                self._rois().append(roi)
                self.roi_changed.emit()
            self.update()
        elif e.button() == Qt.MidButton:
            self._pan_drag = False
            self.setCursor(Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, e):
        """더블클릭 → 줌 리셋."""
        if e.button() == Qt.LeftButton:
            self._reset_zoom()
            if self._last_frame is not None:
                self._render_frame(self._last_frame)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._drawing:
            p = QPainter(self)
            p.setPen(QPen(QColor(255, 80, 80), 2, Qt.DashLine))
            p.drawRect(QRect(self._pt1, self._pt2).normalized())
            p.end()

    # ── 프레임 렌더링 ─────────────────────────────────────────────────
    def _render_frame(self, frame: np.ndarray):
        """줌/패닝 적용 후 미리보기에 표시. 오버레이(ROI, 정보바) 포함."""
        rw, rh = frame.shape[1], frame.shape[0]
        self._raw_size = (rw, rh)
        self._last_frame = frame

        # ── 가시 영역 크롭 (줌 적용) ──────────────────────────────────
        vw = rw / self._zoom
        vh = rh / self._zoom
        sx = int(max(0, min(self._pan_x, rw - vw)))
        sy = int(max(0, min(self._pan_y, rh - vh)))
        ex = int(min(rw, sx + vw))
        ey = int(min(rh, sy + vh))
        cropped = frame[sy:ey, sx:ex]
        if cropped.size == 0:
            return

        rgb  = cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB)
        disp = rgb.copy()
        ch, cw = disp.shape[:2]

        # ── ROI 오버레이 (크롭 좌표로 변환) ───────────────────────────
        # [문제1 수정] PIL 기반 한글 레이블 렌더링
        _pil_fnt = _get_font(11)   # 11px 한글 폰트 (캐시에서 즉시)

        def _draw_label_pil(pil_img, draw_obj, text, x, y,
                            color=(255, 80, 80), bg_alpha=0.45):
            """PIL로 한글 포함 레이블 렌더링."""
            if not text:
                return 0
            try:
                if _pil_fnt:
                    bb = _pil_fnt.getbbox(text)
                    tw, th = (bb[2]-bb[0]), (bb[3]-bb[1])
                else:
                    tw, th = len(text)*7, 12
                px, py = 3, 2
                x1, y1 = max(x, 0), max(y - th - py, 0)
                x2, y2 = min(x + tw + px*2, pil_img.width-1), min(y + py + 2, pil_img.height-1)
                # 반투명 배경
                if x2 > x1 and y2 > y1:
                    region = pil_img.crop((x1, y1, x2, y2))
                    bg = _PIL_Image.new("RGB", region.size, (0, 0, 0))
                    blended = _PIL_Image.blend(region, bg, bg_alpha)
                    pil_img.paste(blended, (x1, y1))
                # 텍스트
                draw_obj.text((x + px, y - th - py),
                              text, font=_pil_fnt, fill=color)
                return tw + px * 2 + 8
            except Exception:
                return len(text) * 7 + 8

        # ROI 사각형은 cv2로 (속도), 레이블은 PIL로 (한글)
        pil_img = _PIL_Image.fromarray(disp) if PIL_AVAILABLE else None
        draw_obj = _PIL_Draw.Draw(pil_img) if pil_img is not None else None

        INFO_SCALE = 0.40
        LABEL_H    = 14

        for i, roi in enumerate(self._rois()):
            # raw 좌표 → 크롭 좌표
            rx  = roi.x - sx;  ry  = roi.y - sy
            rx2 = rx + roi.w;  ry2 = ry + roi.h
            # 완전히 벗어난 ROI 스킵
            if rx2 < 0 or ry2 < 0 or rx > cw or ry > ch:
                continue
            # 클리핑
            drx  = max(rx, 0);  dry  = max(ry, 0)
            drx2 = min(rx2, cw); dry2 = min(ry2, ch)

            # PIL로 렌더링 시 np array → PIL array 동기화
            if pil_img is not None:
                disp = np.array(pil_img)
            cv2.rectangle(disp, (drx, dry), (drx2, dry2), (255, 60, 60), 2)
            if pil_img is not None:
                pil_img = _PIL_Image.fromarray(disp)
                draw_obj = _PIL_Draw.Draw(pil_img)

            above_y = ry - 4
            if above_y < LABEL_H:
                above_y = ry2 + LABEL_H

            name_lbl = roi.name if roi.name else f"ROI{i+1}"
            cur_x = drx

            if pil_img is not None and draw_obj is not None:
                # PIL 한글 렌더링
                cur_x += _draw_label_pil(pil_img, draw_obj, name_lbl,
                                          cur_x, above_y, color=(255, 80, 80))
                if getattr(self.engine, 'brightness_enabled', True):
                    bright_str = f"L:{roi.last_brightness:.0f}"
                    cur_x += _draw_label_pil(pil_img, draw_obj, bright_str,
                                              cur_x, above_y, color=(255, 200, 60))
                # OCR 표시: 소스별 플래그 확인
                _src = getattr(roi, 'source', 'screen')
                _ocr_on = (getattr(self.engine, 'ocr_screen_enabled', True)
                           if _src == 'screen'
                           else getattr(self.engine, 'ocr_camera_enabled', True))
                if _ocr_on:
                    ocr_txt = (roi.last_text or "").strip()
                    if ocr_txt.startswith("[ERR:"):
                        ocr_show, ocr_col = "OCR:ERR", (255, 80, 80)
                    elif ocr_txt and not ocr_txt.startswith("~"):
                        ocr_show, ocr_col = f"OCR:{ocr_txt[:14]}", (60, 230, 255)
                    else:
                        ocr_show, ocr_col = "OCR:—", (100, 100, 120)
                    _draw_label_pil(pil_img, draw_obj, ocr_show,
                                    cur_x, above_y, color=ocr_col)
            else:
                # PIL 없을 때 cv2 fallback (ASCII만 올바르게 표시)
                def _draw_label_cv(img, text, x, y, color=(255,255,255),
                                    scale=0.40, thickness=1):
                    font = cv2.FONT_HERSHEY_SIMPLEX
                    (tw, th), bl = cv2.getTextSize(text, font, scale, thickness)
                    cv2.putText(img, text, (x, y), font,
                                scale, color, thickness, cv2.LINE_AA)
                    return tw + 8
                cur_x += _draw_label_cv(disp, name_lbl, cur_x, above_y,
                                         color=(255, 80, 80))
                if getattr(self.engine, 'brightness_enabled', True):
                    bright_str = f"L:{roi.last_brightness:.0f}"
                    cur_x += _draw_label_cv(disp, bright_str, cur_x, above_y,
                                             color=(255, 200, 60))
                _src = getattr(roi, 'source', 'screen')
                _ocr_on = (getattr(self.engine, 'ocr_screen_enabled', True)
                           if _src == 'screen'
                           else getattr(self.engine, 'ocr_camera_enabled', True))
                if _ocr_on:
                    ocr_txt = (roi.last_text or "").strip()
                    ocr_show = ("OCR:ERR" if ocr_txt.startswith("[ERR:") else
                                f"OCR:{ocr_txt[:14]}" if ocr_txt and not ocr_txt.startswith("~") else
                                "OCR:--")
                    _draw_label_cv(disp, ocr_show, cur_x, above_y,
                                    color=(60, 230, 255))

        # PIL 최종 결과를 disp에 반영
        if pil_img is not None:
            disp = np.array(pil_img)

        # ── 줌 표시 (우상단) ──────────────────────────────────────────
        if self._zoom > 1.01:
            zoom_txt = f"x{self._zoom:.1f}"
            font  = cv2.FONT_HERSHEY_SIMPLEX
            (ztw, zth), _ = cv2.getTextSize(zoom_txt, font, 0.55, 1)
            zx = disp.shape[1] - ztw - 8
            zy = zth + 6
            sub = disp[max(0,zy-zth-3):zy+4, max(0,zx-3):zx+ztw+5]
            if sub.size > 0:
                disp[max(0,zy-zth-3):zy+4, max(0,zx-3):zx+ztw+5] = \
                    (sub * 0.35).astype(sub.dtype)
            cv2.putText(disp, zoom_txt, (zx, zy),
                        font, 0.55, (255, 220, 60), 1, cv2.LINE_AA)

        # ── QLabel에 표시 ─────────────────────────────────────────────
        # ★ 크래시 수정: QImage(disp.data) 는 numpy 버퍼를 소유하지 않음.
        #   카메라 스레드가 프레임을 교체하는 순간 dangling pointer → segfault.
        #   np.ascontiguousarray() 로 연속 메모리 보장 후
        #   QImage 생성 즉시 .copy() 로 Qt 자체 메모리로 복사.
        disp = np.ascontiguousarray(disp)          # C-contiguous 보장
        h, w = disp.shape[:2]
        qi  = QImage(disp.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        pix = QPixmap.fromImage(qi).scaled(
            self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.setPixmap(pix)

    def update_frame(self, frame: np.ndarray):
        if not self._active:
            return
        # ── 밝기 캐시 업데이트 (소스별 brightness_enabled 플래그) ───────
        _src = self.source  # "screen" | "camera"
        if _src == "screen":
            _brt_on = getattr(self.engine, 'brightness_screen_enabled',
                              getattr(self.engine, 'brightness_enabled', True))
        else:
            _brt_on = getattr(self.engine, 'brightness_camera_enabled',
                              getattr(self.engine, 'brightness_enabled', True))
        if _brt_on:
            for roi in self._rois():
                rx, ry, rw, rh = roi.rect()
                region = frame[ry:ry+rh, rx:rx+rw]
                if region.size > 0:
                    avg = region.mean(axis=0).mean(axis=0)
                    b, g, r_ = float(avg[0]), float(avg[1]), float(avg[2])
                    roi.last_brightness = round(0.114*b + 0.587*g + 0.299*r_, 2)
                    roi.last_avg_bgr    = (round(b,1), round(g,1), round(r_,1))
        self._render_frame(frame)

class _FloatingPreview(QWidget):
    """카메라 또는 스크린 플로팅 미리보기 창."""
    def __init__(self, title: str, source: str,
                 engine: CoreEngine, parent=None):
        super().__init__(parent,
            Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint |
            Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint)
        self.setWindowTitle(title)
        self.source  = source
        self.engine  = engine
        self._visible = False
        v = QVBoxLayout(self); v.setContentsMargins(4,4,4,4); v.setSpacing(4)

        # ── 내부 ON/OFF 버튼 (창 닫지 않고 영상처리만 중지) ── ★ v2.9.3
        ctrl = QHBoxLayout(); ctrl.setSpacing(6)
        _is_disp = (source == "screen")
        _color   = "#7bc8e0" if _is_disp else "#f0c040"
        self._preview_on_btn = QPushButton("▶ 출력 ON")
        self._preview_on_btn.setCheckable(True)
        self._preview_on_btn.setChecked(True)
        self._preview_on_btn.setFixedHeight(24)
        self._preview_on_btn.setStyleSheet(
            f"QPushButton{{background:#1a2a3a;color:{_color};"
            "border:1px solid #2a4a6a;border-radius:3px;font-size:10px;font-weight:bold;}"
            f"QPushButton:!checked{{background:#0a0a18;color:#334;border-color:#1a1a2a;}}"
            "QPushButton:hover{background:#22334a;}")
        self._preview_on_btn.toggled.connect(self._on_preview_toggle)

        _src_lbl = "🖥 Display" if _is_disp else "📷 Camera"
        src_info = QLabel(_src_lbl)
        src_info.setStyleSheet(f"color:{_color};font-size:10px;font-weight:bold;")
        # ★ Always-on-top 토글 버튼
        self._top_btn = QPushButton("📌 최상위")
        self._top_btn.setCheckable(True)
        self._top_btn.setFixedHeight(24)
        self._top_btn.setStyleSheet(
            "QPushButton{background:#1a1a3a;color:#aaa;"
            "border:1px solid #2a2a5a;border-radius:3px;font-size:10px;}"
            "QPushButton:checked{background:#2a1a4a;color:#c0a0ff;"
            "border-color:#5a2a8a;}"
            "QPushButton:hover{background:#22224a;}")
        self._top_btn.setToolTip("이 창을 항상 최상위에 표시")
        self._top_btn.toggled.connect(self._on_always_on_top)

        ctrl.addWidget(src_info)
        ctrl.addStretch()
        ctrl.addWidget(self._top_btn)
        ctrl.addWidget(self._preview_on_btn)
        v.addLayout(ctrl)

        self.prev = PreviewLabel(source, engine)
        v.addWidget(self.prev, 1)
        info = QLabel("좌클릭+드래그: ROI 추가  |  우클릭: 마지막 ROI 제거")
        info.setStyleSheet("color:#445;font-size:9px;")
        info.setAlignment(Qt.AlignCenter); v.addWidget(info)
        self.resize(640, 420)

    def _on_preview_toggle(self, on: bool):
        """창 내부 ON/OFF — PreviewLabel 렌더만 중지 (스레드는 유지)."""
        self.prev.set_active(on)
        self._preview_on_btn.setText("▶ 출력 ON" if on else "⏸ 출력 OFF")

    def _on_always_on_top(self, on: bool):
        """항상 최상위 토글 — 윈도우 플래그 재설정."""
        self._top_btn.setText("📌 최상위 ON" if on else "📌 최상위")
        flags = (Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint |
                 Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint)
        if on:
            flags |= Qt.WindowStaysOnTopHint
        pos  = self.pos()
        size = self.size()
        self.setWindowFlags(flags)
        self.move(pos); self.resize(size)
        if self._visible:
            self.show()

    def set_always_on_top(self, on: bool):
        """외부(커널/AI)에서 최상위 설정."""
        self._top_btn.setChecked(on)

    def show_win(self):
        self._visible = True; self.show(); self.activateWindow()

    def hide_win(self):
        self._visible = False; self.hide()

    def toggle(self):
        if self._visible: self.hide_win()
        else: self.show_win()

    def closeEvent(self, e):
        self._visible = False; e.accept()

    def update_frame(self, frame):
        if self._visible and frame is not None:
            self.prev.update_frame(frame)


class CameraWindow(_FloatingPreview):
    def __init__(self, engine: CoreEngine, parent=None):
        super().__init__("📷 Camera Preview", "camera", engine, parent)

class DisplayWindow(_FloatingPreview):
    def __init__(self, engine: CoreEngine, parent=None):
        super().__init__("🖥 Display Preview", "screen", engine, parent)

