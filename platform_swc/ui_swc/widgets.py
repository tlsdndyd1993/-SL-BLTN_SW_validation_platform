# -*- coding: utf-8 -*-
"""
ui_swc/widgets.py
공용 소형 위젯 모음
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

class ClickLabel(QLabel):
    """클릭 가능한 QLabel. QPushButton hover 오버라이드 문제 우회."""
    clicked = pyqtSignal()
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(ev)

def _make_spinbox(min_v, max_v, val, step=1, decimals=0,
                  special=None, width=None) -> QDoubleSpinBox:
    sb = QDoubleSpinBox()
    sb.setRange(min_v, max_v); sb.setValue(val)
    sb.setSingleStep(step); sb.setDecimals(int(decimals))
    if special: sb.setSpecialValueText(special)
    if width:   sb.setFixedWidth(width)
    sb.setStyleSheet("QDoubleSpinBox{background:#1a1a3a;color:#ddd;"
                     "border:1px solid #3a4a6a;padding:2px 4px;border-radius:3px;}")
    return sb


class CollapsibleSection(QWidget):
    def __init__(self, title: str, color: str="#3a7bd5", parent=None):
        super().__init__(parent)
        self._collapsed = False
        self._drag_y    = None          # 드래그 시작 Y 좌표
        self._drag_h    = None          # 드래그 시작 시 content 높이
        self._resizable = False         # 높이 조절 모드 활성 여부

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._btn = QPushButton()
        self._btn.setCheckable(True)
        self._btn.setChecked(False)
        self._btn.setFixedHeight(34)
        self._btn.clicked.connect(self._toggle)
        self._title = title; self._color = color; self._style(False)
        outer.addWidget(self._btn)

        self._content = QWidget()
        self._content.setStyleSheet(
            "QWidget{background:#10102a;border:1px solid #2a2a4a;"
            "border-top:none;border-radius:0 0 6px 6px;}")
        cl = QVBoxLayout(self._content)
        cl.setContentsMargins(8, 8, 8, 18)   # 하단 여유: 드래그 핸들 공간
        cl.setSpacing(6)
        self._cl = cl
        outer.addWidget(self._content)

        # ── 하단 드래그 핸들 ─────────────────────────────────────────────
        # _content 안에 절대 위치로 표시 (QLabel 오버레이)
        self._grip = QLabel("⠿⠿⠿", self._content)
        self._grip.setAlignment(Qt.AlignCenter)
        self._grip.setFixedHeight(14)
        self._grip.setStyleSheet(
            "QLabel{background:#1a2a3a;color:#3a5a8a;"
            "border-top:1px solid #2a3a5a;font-size:9px;"
            "letter-spacing:3px;}")
        self._grip.setCursor(Qt.SizeVerCursor)
        self._grip.setToolTip("드래그하여 높이 조절")

        # 드래그 이벤트
        self._grip.mousePressEvent   = self._grip_press
        self._grip.mouseMoveEvent    = self._grip_move
        self._grip.mouseReleaseEvent = self._grip_release

        # content 리사이즈 시 grip 위치 갱신
        self._content.resizeEvent = self._on_content_resize

    def _on_content_resize(self, ev):
        """content 크기 변경 시 grip을 하단에 붙임."""
        w = self._content.width()
        h = self._content.height()
        self._grip.setGeometry(0, h - 14, w, 14)
        QWidget.resizeEvent(self._content, ev)

    def _grip_press(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag_y = ev.globalY()
            self._drag_h = self._content.height()

    def _grip_move(self, ev):
        if self._drag_y is not None:
            delta = ev.globalY() - self._drag_y
            new_h = max(60, self._drag_h + delta)
            self._content.setMinimumHeight(new_h)
            self._content.setMaximumHeight(new_h)

    def _grip_release(self, ev):
        self._drag_y = None
        self._drag_h = None

    def _style(self, collapsed):
        arrow = "▶" if collapsed else "▼"
        self._btn.setText(f"  {arrow}  {self._title}")
        bot = "1px solid #2a2a4a" if collapsed else "none"
        rad = "6px" if collapsed else "6px 6px 0 0"
        self._btn.setStyleSheet(f"""
            QPushButton{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #1e2240,stop:1 #12122a);
                color:#7ab4d4;font-size:12px;font-weight:bold;text-align:left;
                padding:0 12px;border-left:3px solid {self._color};
                border-top:1px solid #2a2a4a;border-right:1px solid #2a2a4a;
                border-bottom:{bot};border-radius:{rad};}}
            QPushButton:hover{{background:#22224a;color:#9ad4f4;}}""")

    def _toggle(self):
        self._collapsed = not self._collapsed
        self._content.setVisible(not self._collapsed)
        self._style(self._collapsed)

    def add_widget(self, w): self._cl.addWidget(w)
    def set_collapsed(self, v):
        if v != self._collapsed: self._toggle()
    def is_collapsed(self): return self._collapsed


# =============================================================================
class MemoOverlayRow(QWidget):
    changed = pyqtSignal()
    removed = pyqtSignal(object)
    _POSITIONS = ["top-left","top-right","bottom-left","bottom-right","center"]
    _TARGETS   = ["both","screen","camera"]
    _POS_KR    = ["좌상","우상","좌하","우하","중앙"]
    _TGT_KR    = ["Both","Display","Camera"]

    def __init__(self, cfg: MemoOverlayCfg, tab_count: int, parent=None):
        super().__init__(parent); self.cfg=cfg
        lay=QHBoxLayout(self); lay.setContentsMargins(2,2,2,2); lay.setSpacing(4)

        self._en=QCheckBox("ON"); self._en.setChecked(cfg.enabled)
        self._en.setStyleSheet("QCheckBox{font-size:10px;font-weight:bold;color:#2ecc71;}")
        self._en.toggled.connect(self._upd); lay.addWidget(self._en)

        lay.addWidget(QLabel("탭:"))
        self._tab=QSpinBox(); self._tab.setRange(1,max(tab_count,1))
        self._tab.setValue(cfg.tab_idx+1); self._tab.setFixedWidth(44)
        self._tab.valueChanged.connect(self._upd); lay.addWidget(self._tab)

        lay.addWidget(QLabel("위치:"))
        self._pos=QComboBox()
        for p in self._POS_KR: self._pos.addItem(p)
        if cfg.position in self._POSITIONS:
            self._pos.setCurrentIndex(self._POSITIONS.index(cfg.position))
        self._pos.currentIndexChanged.connect(self._upd)
        self._pos.setFixedWidth(58); lay.addWidget(self._pos)

        lay.addWidget(QLabel("대상:"))
        self._tgt=QComboBox()
        for t in self._TGT_KR: self._tgt.addItem(t)
        if cfg.target in self._TARGETS:
            self._tgt.setCurrentIndex(self._TARGETS.index(cfg.target))
        self._tgt.currentIndexChanged.connect(self._upd)
        self._tgt.setFixedWidth(66); lay.addWidget(self._tgt)

        lay.addWidget(QLabel("크기:"))
        self._fsz=QSpinBox(); self._fsz.setRange(8,72)
        self._fsz.setValue(getattr(cfg,'overlay_font_size',18))
        self._fsz.setFixedWidth(50)
        self._fsz.setStyleSheet(
            "QSpinBox{background:#1a1a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:3px;font-size:11px;}")
        self._fsz.valueChanged.connect(self._upd); lay.addWidget(self._fsz)

        rm=QPushButton("✕"); rm.setFixedSize(22,22)
        rm.setStyleSheet("QPushButton{background:#3a1a1a;color:#f88;border:none;"
                         "border-radius:3px;}QPushButton:hover{background:#7f2020;}")
        rm.clicked.connect(lambda: self.removed.emit(self)); lay.addWidget(rm)

    def _upd(self):
        self.cfg.enabled           = self._en.isChecked()
        self.cfg.tab_idx           = self._tab.value()-1
        self.cfg.position          = self._POSITIONS[self._pos.currentIndex()]
        self.cfg.target            = self._TARGETS[self._tgt.currentIndex()]
        self.cfg.overlay_font_size = self._fsz.value()
        self.changed.emit()

    def update_tab_max(self, n): self._tab.setMaximum(max(n,1))

class _FeatDragList(QWidget):
    """
    기능 패널 순서를 드래그앤드랍으로 변경하는 컴팩트 리스트.
    각 행: [⠿ 핸들] [☑ ON/OFF] [색상 레이블]
    - 드래그: 순서 변경
    - 더블클릭: 해당 섹션으로 스크롤
    - 체크박스: 섹션 표시/숨기기
    """
    order_changed      = pyqtSignal(list)   # 새 순서 [key,...]
    visibility_changed = pyqtSignal(str, bool)  # (key, visible)
    scroll_to          = pyqtSignal(str)    # 더블클릭 key

    _ROW_H = 26
    _BG    = "#080818"
    _BG_HV = "#101028"
    _BG_DG = "#1a2a4a"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background:{self._BG};")
        self._order:   List[str] = []
        self._labels:  dict = {}
        self._colors:  dict = {}
        self._visible: dict = {}
        self._rows:    list = []   # QFrame 위젯
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(2,2,2,2); self._lay.setSpacing(1)
        self._drag_idx   = -1
        self._drag_start = QPoint()
        self._dragging   = False

    def populate(self, order: list, labels: dict,
                 colors: dict, visible: dict):
        self._order   = list(order)
        self._labels  = labels
        self._colors  = colors
        self._visible = dict(visible)
        self._rebuild()

    def _rebuild(self):
        while self._lay.count():
            it = self._lay.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        self._rows.clear()
        for key in self._order:
            row = self._make_row(key)
            self._rows.append(row)
            self._lay.addWidget(row)
        self._lay.addStretch()

    def _make_row(self, key: str) -> QFrame:
        row = QFrame(); row.setFixedHeight(self._ROW_H)
        row._key = key
        row.setStyleSheet(
            f"QFrame{{background:{self._BG};border-radius:3px;}}"
            f"QFrame:hover{{background:{self._BG_HV};}}")
        row.setCursor(Qt.OpenHandCursor)
        hl = QHBoxLayout(row); hl.setContentsMargins(4,1,4,1); hl.setSpacing(4)

        grip = QLabel("⠿"); grip.setFixedWidth(14)
        grip.setStyleSheet("color:#3a4a6a;font-size:16px;background:transparent;")

        chk = QCheckBox(); chk.setChecked(self._visible.get(key, True))
        chk.setStyleSheet(
            "QCheckBox::indicator{width:13px;height:13px;"
            "border:1px solid #3a4a6a;border-radius:2px;background:#0d0d1e;}"
            "QCheckBox::indicator:checked{background:#2980b9;border-color:#3a9ad9;}")
        chk.toggled.connect(lambda v, k=key: self._on_chk(k, v))

        color = self._colors.get(key, "#7bc8e0")
        lbl = QLabel(self._labels.get(key, key))
        lbl.setStyleSheet(
            f"color:{color};font-size:10px;font-weight:bold;background:transparent;")

        hl.addWidget(grip); hl.addWidget(chk); hl.addWidget(lbl, 1)

        # 이벤트 바인딩
        row.mousePressEvent   = lambda e, r=row: self._press(e, r)
        row.mouseMoveEvent    = lambda e, r=row: self._move(e, r)
        row.mouseReleaseEvent = lambda e, r=row: self._release(e, r)
        row.mouseDoubleClickEvent = lambda e, k=key: self.scroll_to.emit(k)
        return row

    def _on_chk(self, key: str, visible: bool):
        self._visible[key] = visible
        self.visibility_changed.emit(key, visible)

    def _press(self, e, row):
        if e.button() == Qt.LeftButton:
            idx = self._rows.index(row) if row in self._rows else -1
            self._drag_idx   = idx
            self._drag_start = e.globalPos()
            self._dragging   = False

    def _move(self, e, row):
        if self._drag_idx >= 0 and not self._dragging:
            if (e.globalPos()-self._drag_start).manhattanLength() > 6:
                self._dragging = True
                row.setStyleSheet(
                    f"QFrame{{background:{self._BG_DG};"
                    "border:1px solid #5a7aaa;border-radius:3px;}}")
                row.setCursor(Qt.ClosedHandCursor)

    def _release(self, e, row):
        if self._dragging and self._drag_idx >= 0:
            # 마우스 위치에서 대상 행 찾기
            gpos = e.globalPos()
            tgt  = -1
            for i, r in enumerate(self._rows):
                local = r.mapFromGlobal(gpos)
                if r.rect().contains(local):
                    tgt = i; break
            if tgt >= 0 and tgt != self._drag_idx:
                self._order.insert(tgt, self._order.pop(self._drag_idx))
                self._rebuild()
                self.order_changed.emit(list(self._order))
            else:
                self._rebuild()   # 스타일 복원
        self._drag_idx = -1; self._dragging = False

    def set_order(self, order: list):
        valid = [k for k in order if k in self._labels]
        for k in self._order:
            if k not in valid: valid.append(k)
        self._order = valid
        self._rebuild()

    def set_visible(self, key: str, v: bool):
        self._visible[key] = v
        self._rebuild()


