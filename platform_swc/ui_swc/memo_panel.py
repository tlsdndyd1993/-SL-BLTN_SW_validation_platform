# -*- coding: utf-8 -*-
"""
ui_swc/memo_panel.py
메모 + 화면 오버레이 패널
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

class TimestampMemoEdit(QPlainTextEdit):
    _TS_RE = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.timestamp_enabled = True

    @classmethod
    def _pat(cls):
        if cls._TS_RE is None:
            cls._TS_RE = re.compile(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ')
        return cls._TS_RE

    def contextMenuEvent(self, e): e.accept()

    def mousePressEvent(self, e):
        super().mousePressEvent(e)
        if e.button() == Qt.RightButton and self.timestamp_enabled:
            cur = self.textCursor()
            cur.movePosition(QTextCursor.StartOfBlock)
            cur.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            line = cur.selectedText()
            m = self._pat().match(line)
            if m:
                sc = self.textCursor()
                sc.movePosition(QTextCursor.StartOfBlock)
                sc.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, m.end())
                sc.removeSelectedText()
                self.setTextCursor(sc)

    def mouseDoubleClickEvent(self, e):
        super().mouseDoubleClickEvent(e)
        if not self.timestamp_enabled: return
        if e.button() == Qt.LeftButton:
            cur = self.textCursor()
            cur.movePosition(QTextCursor.StartOfBlock)
            cur.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            line = cur.selectedText()
            ts_now = datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
            m = self._pat().match(line)
            sc = self.textCursor()
            sc.movePosition(QTextCursor.StartOfBlock)
            if m:
                sc.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, m.end())
            sc.insertText(ts_now)
            self.setTextCursor(sc)


# =============================================================================
#  CollapsibleSection
class MemoPanel(QWidget):
    overlay_changed = pyqtSignal()

    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine = engine; self.signals = signals
        self._editors: list = []; self._overlay_rows: list = []
        self._build()
        signals.rec_started.connect(lambda _: QTimer.singleShot(
            500, lambda: self._export_txt_to_rec_dir()))

    def _build(self):
        root = QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)

        # ★ QSplitter: 상단(설정+버튼) / 하단(에디터+오버레이) 크기 조절
        splitter = QSplitter(Qt.Vertical)
        splitter.setStyleSheet(
            "QSplitter::handle{background:#1a2a3a;height:5px;}"
            "QSplitter::handle:hover{background:#2a5aaa;cursor:ns-resize;}")
        root.addWidget(splitter)

        top_w = QWidget()
        v = QVBoxLayout(top_w); v.setContentsMargins(0,4,0,4); v.setSpacing(6)
        splitter.addWidget(top_w)

        # 폰트/타임스탬프 설정
        cg = QGroupBox("📝 현재 탭 설정")
        cl = QVBoxLayout(cg); cl.setSpacing(6)
        font_row = QHBoxLayout(); font_row.setSpacing(6)
        font_row.addWidget(QLabel("에디터 글꼴:"))
        self.font_spin = QSpinBox(); self.font_spin.setRange(8,36); self.font_spin.setValue(11)
        self.font_spin.setFixedWidth(52)
        self.font_spin.setStyleSheet(
            "QSpinBox{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:3px;font-weight:bold;font-size:13px;}")
        self.font_sl = QSlider(Qt.Horizontal); self.font_sl.setRange(8,36); self.font_sl.setValue(11)
        self.font_sl.setStyleSheet(
            "QSlider::groove:horizontal{background:#1a2030;height:6px;border-radius:3px;}"
            "QSlider::handle:horizontal{background:#f0c040;width:16px;height:16px;"
            "margin-top:-5px;margin-bottom:-5px;border-radius:8px;}"
            "QSlider::sub-page:horizontal{background:#5a5a20;border-radius:3px;}")
        self.font_spin.valueChanged.connect(
            lambda v: (self.font_sl.blockSignals(True), self.font_sl.setValue(v),
                       self.font_sl.blockSignals(False), self._apply_font_cur(v)))
        self.font_sl.valueChanged.connect(
            lambda v: (self.font_spin.blockSignals(True), self.font_spin.setValue(v),
                       self.font_spin.blockSignals(False), self._apply_font_cur(v)))
        font_row.addWidget(self.font_sl, 1); font_row.addWidget(self.font_spin)
        for lbl, sz in [("S",10),("M",13),("L",16),("XL",20)]:
            b = QPushButton(lbl); b.setFixedSize(28, 22)
            b.setStyleSheet(
                "QPushButton{background:#2a2a1a;color:#f0c040;"
                "border:1px solid #4a4a20;border-radius:3px;font-size:10px;}")
            b.clicked.connect(lambda _, s=sz: self.font_spin.setValue(s))
            font_row.addWidget(b)
        cl.addLayout(font_row)
        ts_row = QHBoxLayout()
        self.ts_chk = QCheckBox("더블클릭 타임스탬프 삽입"); self.ts_chk.setChecked(True)
        self.ts_chk.setStyleSheet("font-size:11px;font-weight:bold;color:#7bc8e0;")
        self.ts_chk.toggled.connect(self._on_ts_toggled)
        ts_row.addWidget(self.ts_chk); ts_row.addWidget(QLabel("(우클릭: 제거)"))
        ts_row.addStretch(); cl.addLayout(ts_row)
        v.addWidget(cg)

        # 탭 컨트롤 버튼
        tc = QHBoxLayout(); tc.setSpacing(6)
        btns = [
            ("＋ 탭","_add_tab_new","#1a3a1a","#8fa","#2a6a2a"),
            ("－ 탭","_del_tab","#3a1a1a","#f88","#6a2a2a"),
            ("현재 탭 지우기","_clear_cur","#1a1a3a","#aaa","#334"),
            ("📄 .txt 내보내기","_export_txt","#1a2a3a","#7bc8e0","#2a4a6a"),
            ("📁 녹화폴더 저장","_export_to_rec","#1a2a1a","#8fa","#2a6a2a"),
        ]
        for text, fn, bg, fg, border in btns:
            b = QPushButton(text); b.setFixedHeight(26)
            b.setStyleSheet(
                f"QPushButton{{background:{bg};color:{fg};"
                f"border:1px solid {border};border-radius:4px;font-size:11px;}}")
            b.clicked.connect(getattr(self, fn)); tc.addWidget(b)
        tc.addStretch(); v.addLayout(tc)

        # ★ 하단 위젯 (에디터 탭 + 오버레이 설정)
        bottom_w = QWidget()
        vb = QVBoxLayout(bottom_w); vb.setContentsMargins(0,4,0,0); vb.setSpacing(4)
        splitter.addWidget(bottom_w)
        splitter.setSizes([160, 400])
        v = vb   # 이하 v는 bottom_w 레이아웃으로 사용

        # 탭 위젯
        self.tabs = QTabWidget()
        self.tabs.setMinimumHeight(120)   # ★ 최소 높이 (섹션 드래그로 더 키울 수 있음)
        self.tabs.setStyleSheet(
            "QTabWidget::pane{border:1px solid #2a2a4a;background:#0d0d1e;}"
            "QTabBar::tab{background:#1a1a3a;color:#888;border:1px solid #334;"
            "border-bottom:none;border-radius:4px 4px 0 0;padding:4px 10px;"
            "font-size:11px;min-width:60px;}"
            "QTabBar::tab:selected{background:#2a2a5a;color:#dde;border-color:#446;}"
            "QTabBar::tab:hover{background:#22224a;color:#bbd;}")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._add_tab("메모 1"); v.addWidget(self.tabs)

        # 오버레이 설정
        ovg = QGroupBox("🖼 오버레이 설정"); ovl = QVBoxLayout(ovg); ovl.setSpacing(4)
        self._ov_cont = QWidget()
        self._ov_lay  = QVBoxLayout(self._ov_cont)
        self._ov_lay.setContentsMargins(0,0,0,0); self._ov_lay.setSpacing(3)
        ovl.addWidget(self._ov_cont)
        add_ov = QPushButton("＋ 오버레이 추가"); add_ov.setFixedHeight(26)
        add_ov.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:4px;font-size:11px;}")
        add_ov.clicked.connect(self._add_overlay); ovl.addWidget(add_ov)
        v.addWidget(ovg)
        self._rebuild_ov_rows()

    def _add_tab(self, title, content="", font_size=11, ts_enabled=True):
        ed = TimestampMemoEdit()
        ed.setPlaceholderText("메모 입력… (더블클릭: 타임스탬프 | 우클릭: 제거)")
        ed.setStyleSheet("background:#0d0d1e;color:#ffe;border:none;font-family:monospace;")
        ed.timestamp_enabled = ts_enabled
        f = ed.font(); f.setPointSize(max(8, font_size)); ed.setFont(f)
        if content: ed.setPlainText(content)
        ed.textChanged.connect(self._sync_texts)
        self._editors.append(ed); self.tabs.addTab(ed, title)
        while len(self.engine.memo_texts) < len(self._editors):
            self.engine.memo_texts.append("")
        self._upd_ov_max()

    def _sync_texts(self):
        for i, ed in enumerate(self._editors):
            if i < len(self.engine.memo_texts):
                self.engine.memo_texts[i] = ed.toPlainText()
            else:
                self.engine.memo_texts.append(ed.toPlainText())
        self.overlay_changed.emit()

    def _on_tab_changed(self, idx):
        if not 0 <= idx < len(self._editors): return
        ed = self._editors[idx]
        fs = ed.font().pointSize(); fs = fs if fs > 0 else 11
        for w in (self.font_spin, self.font_sl):
            w.blockSignals(True); w.setValue(fs); w.blockSignals(False)
        self.ts_chk.blockSignals(True)
        self.ts_chk.setChecked(ed.timestamp_enabled)
        self.ts_chk.blockSignals(False)

    def _apply_font_cur(self, size):
        idx = self.tabs.currentIndex()
        if 0 <= idx < len(self._editors):
            f = self._editors[idx].font(); f.setPointSize(max(8,size))
            self._editors[idx].setFont(f)

    def _on_ts_toggled(self, v):
        idx = self.tabs.currentIndex()
        if 0 <= idx < len(self._editors):
            self._editors[idx].timestamp_enabled = v

    def _add_tab_new(self):
        n = self.tabs.count() + 1
        self._add_tab(f"메모 {n}")
        self.tabs.setCurrentIndex(self.tabs.count()-1)

    def _del_tab(self):
        if self.tabs.count() <= 1: return
        idx = self.tabs.currentIndex()
        self.tabs.removeTab(idx)
        if idx < len(self._editors): self._editors.pop(idx)
        if idx < len(self.engine.memo_texts): self.engine.memo_texts.pop(idx)
        self._upd_ov_max(); self._on_tab_changed(self.tabs.currentIndex())

    def _clear_cur(self):
        idx = self.tabs.currentIndex()
        if idx < len(self._editors): self._editors[idx].clear()

    def _export_txt(self):
        idx = self.tabs.currentIndex()
        if not 0 <= idx < len(self._editors): return
        content   = self._editors[idx].toPlainText()
        tab_name  = self.tabs.tabText(idx)
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', tab_name)
        ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_dir  = os.path.join(self.engine.base_dir, "memo_export")
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, f"{safe_name}_{ts}.txt")
        try:
            with open(path, 'w', encoding='utf-8') as f: f.write(content)
            self.signals.status_message.emit(f"[메모] → {path}")
            open_folder(save_dir)
        except Exception as ex:
            self.signals.status_message.emit(f"[메모] 실패: {ex}")

    def _export_to_rec(self):
        self._export_txt_to_rec_dir()

    def _export_txt_to_rec_dir(self):
        save_dir = (self.engine.output_dir
                    if (self.engine.output_dir and os.path.isdir(self.engine.output_dir))
                    else os.path.join(self.engine.base_dir, "memo_export"))
        os.makedirs(save_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        lines_all = []
        for i in range(self.tabs.count()):
            if i >= len(self._editors): break
            title   = self.tabs.tabText(i)
            content = self._editors[i].toPlainText().strip()
            if content: lines_all.append(f"=== {title} ===\n{content}\n")
        path = os.path.join(save_dir, f"memo_all_{ts}.txt")
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write("\n".join(lines_all))
            self.signals.status_message.emit(f"[메모→녹화폴더] → {path}")
        except Exception as ex:
            self.signals.status_message.emit(f"[메모→녹화폴더] 실패: {ex}")

    def _rebuild_ov_rows(self):
        while self._ov_lay.count():
            it = self._ov_lay.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        self._overlay_rows.clear()
        for cfg in self.engine.memo_overlays: self._add_ov_row(cfg)

    def _add_ov_row(self, cfg):
        n = max(self.tabs.count(), 1)
        row = MemoOverlayRow(cfg, n)
        row.removed.connect(self._rm_ov_row)
        row.changed.connect(self.overlay_changed)
        self._overlay_rows.append(row); self._ov_lay.addWidget(row)

    def _rm_ov_row(self, row):
        if len(self.engine.memo_overlays) <= 1: return
        if row.cfg in self.engine.memo_overlays:
            self.engine.memo_overlays.remove(row.cfg)
        self._ov_lay.removeWidget(row); row.deleteLater()
        if row in self._overlay_rows: self._overlay_rows.remove(row)

    def _add_overlay(self):
        cfg = MemoOverlayCfg(0, "bottom-right", "both", True)
        self.engine.memo_overlays.append(cfg)
        self._add_ov_row(cfg); self.overlay_changed.emit()

    def _upd_ov_max(self):
        n = max(self.tabs.count(), 1)
        for row in self._overlay_rows: row.update_tab_max(n)

    def get_tab_data(self) -> list:
        tabs = []
        for i in range(self.tabs.count()):
            if i >= len(self._editors): break
            ed = self._editors[i]
            fs = ed.font().pointSize(); fs = fs if fs > 0 else 11
            tabs.append({'title':self.tabs.tabText(i), 'content':ed.toPlainText(),
                         'font_size':fs, 'ts_enabled':ed.timestamp_enabled})
        return tabs

    def set_tab_data(self, tabs: list):
        while self.tabs.count() > 0: self.tabs.removeTab(0)
        self._editors.clear(); self.engine.memo_texts.clear()
        for t in tabs:
            self._add_tab(t['title'], t['content'],
                          t.get('font_size',11), t.get('ts_enabled',True))
        self._on_tab_changed(self.tabs.currentIndex())


# =============================================================================
#  ResetPanel
# =============================================================================

# ═══ v2.9 추가 모듈 ═════════════════════════════════════════════════
