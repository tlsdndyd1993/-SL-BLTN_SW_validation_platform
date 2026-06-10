# -*- coding: utf-8 -*-
"""
ui_swc/schedule_panel.py
예약 실행 패널
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

class SchedulePanel(QWidget):
    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine = engine; self.signals = signals
        self._build()
        self._warn_timer = QTimer(self)
        self._warn_timer.timeout.connect(self._check_past)
        self._warn_timer.start(1000)

    def _dt_edit(self, color, border) -> QDateTimeEdit:
        dte = QDateTimeEdit()
        dte.setDisplayFormat("yyyy-MM-dd  HH:mm:ss")
        dte.setCalendarPopup(True); dte.setMinimumHeight(30)
        dte.setStyleSheet(
            f"QDateTimeEdit{{background:#1a1a3a;color:{color};"
            f"border:1px solid {border};border-radius:4px;padding:4px 6px;"
            "font-size:12px;font-family:monospace;}}"
            "QDateTimeEdit::drop-down{border:none;}"
            "QDateTimeEdit::down-arrow{image:none;width:0;}")
        return dte

    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        inp = QGroupBox("새 예약 추가"); ig = QVBoxLayout(inp); ig.setSpacing(8)

        ig.addWidget(QLabel("🟢  녹화 시작 시각"))
        rs = QHBoxLayout(); rs.setSpacing(6)
        self.s_chk = QCheckBox("사용"); self.s_chk.setChecked(True)
        self.s_dt  = self._dt_edit("#2ecc71", "#2a6a3a")
        self.s_dt.setDateTime(QDateTime.currentDateTime().addSecs(60))
        b = QPushButton("지금"); b.setFixedSize(44, 30)
        b.clicked.connect(lambda: self.s_dt.setDateTime(QDateTime.currentDateTime()))
        rs.addWidget(self.s_chk); rs.addWidget(self.s_dt, 1); rs.addWidget(b)
        ig.addLayout(rs)

        ig.addWidget(QLabel("🔴  녹화 종료 시각"))
        re_ = QHBoxLayout(); re_.setSpacing(6)
        self.e_chk = QCheckBox("사용"); self.e_chk.setChecked(True)
        self.e_dt  = self._dt_edit("#e74c3c", "#6a2a2a")
        self.e_dt.setDateTime(QDateTime.currentDateTime().addSecs(3660))
        b2 = QPushButton("지금"); b2.setFixedSize(44, 30)
        b2.clicked.connect(lambda: self.e_dt.setDateTime(QDateTime.currentDateTime()))
        re_.addWidget(self.e_chk); re_.addWidget(self.e_dt, 1); re_.addWidget(b2)
        ig.addLayout(re_)

        mac_row = QHBoxLayout()
        self.mac_chk = QCheckBox("입/출력 장치 기록도 실행"); self.mac_chk.setChecked(False)
        mac_row.addWidget(self.mac_chk)
        mac_row.addWidget(QLabel("반복:"))
        self.mac_rep = QSpinBox(); self.mac_rep.setRange(0,9999)
        self.mac_rep.setValue(1); self.mac_rep.setSpecialValueText("∞")
        self.mac_rep.setFixedWidth(60); mac_row.addWidget(self.mac_rep)
        mac_row.addWidget(QLabel("간격(초):"))
        self.mac_gap = QDoubleSpinBox()
        self.mac_gap.setRange(0,60); self.mac_gap.setValue(1.0)
        self.mac_gap.setFixedWidth(60); mac_row.addWidget(self.mac_gap)
        mac_row.addStretch(); ig.addLayout(mac_row)

        # ── [추가1] 예약 전원 제어 ────────────────────────────────────────
        pwr_grp = QGroupBox("⚡ 전원 제어 예약")
        pwr_grp.setCheckable(True); pwr_grp.setChecked(False)
        pwr_grp.setStyleSheet(
            "QGroupBox{font-size:10px;color:#e74c3c;border:1px solid #3a1a1a;"
            "border-radius:4px;margin-top:6px;padding-top:4px;}"
            "QGroupBox:checked{border-color:#e74c3c;}"
            "QGroupBox::title{subcontrol-origin:margin;left:8px;}")
        pl = QVBoxLayout(pwr_grp); pl.setSpacing(4)

        # 시작 시각 전원 ON
        pon_row = QHBoxLayout(); pon_row.setSpacing(6)
        self.pwr_on_chk = QCheckBox("시작 시각에 전원 ON")
        self.pwr_on_chk.setStyleSheet("font-size:10px;color:#2ecc71;")
        self.pwr_on_ch = QComboBox()
        self.pwr_on_ch.addItems(["B+ (bplus)", "TG B+ (tg_bplus)", "ACC (acc)",
                                  "IGN (ign)", "PLBM 실물 (plbm_real)", "PLBM 모사 (plbm_sim)"])
        self.pwr_on_ch.setFixedHeight(24)
        self.pwr_on_ch.setStyleSheet("font-size:10px;background:#0a1a0a;color:#2ecc71;")
        pon_row.addWidget(self.pwr_on_chk); pon_row.addWidget(self.pwr_on_ch, 1)
        pl.addLayout(pon_row)

        # 종료 시각 전원 OFF
        poff_row = QHBoxLayout(); poff_row.setSpacing(6)
        self.pwr_off_chk = QCheckBox("종료 시각에 전원 OFF")
        self.pwr_off_chk.setStyleSheet("font-size:10px;color:#e74c3c;")
        self.pwr_off_ch = QComboBox()
        self.pwr_off_ch.addItems(["B+ (bplus)", "TG B+ (tg_bplus)", "ACC (acc)",
                                   "IGN (ign)", "PLBM 실물 (plbm_real)", "PLBM 모사 (plbm_sim)",
                                   "전체 OFF (all)"])
        self.pwr_off_ch.setCurrentIndex(6)  # 기본: 전체 OFF
        self.pwr_off_ch.setFixedHeight(24)
        self.pwr_off_ch.setStyleSheet("font-size:10px;background:#1a0a0a;color:#e74c3c;")
        poff_row.addWidget(self.pwr_off_chk); poff_row.addWidget(self.pwr_off_ch, 1)
        pl.addLayout(poff_row)

        self._pwr_sched_grp = pwr_grp
        ig.addWidget(pwr_grp)

        add_btn = QPushButton("＋  예약 추가"); add_btn.setMinimumHeight(30)
        add_btn.setStyleSheet(
            "QPushButton{background:#1a4a2a;color:#afffcf;border:1px solid #2a8a5a;"
            "border-radius:4px;font-weight:bold;}QPushButton:hover{background:#225a3a;}")
        add_btn.clicked.connect(self._add); ig.addWidget(add_btn)
        v.addWidget(inp)

        lst = QGroupBox("예약 목록"); ll = QVBoxLayout(lst)
        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(["#","시작","종료","액션","상태"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setFixedHeight(140)
        self.tbl.setStyleSheet(
            "QTableWidget{background:#0d0d1e;color:#ccc;font-size:11px;"
            "border:1px solid #334;}"
            "QHeaderView::section{background:#1a1a3a;color:#9ab;font-size:11px;"
            "border:none;padding:4px;}")
        ll.addWidget(self.tbl)
        br = QHBoxLayout()
        for lbl, fn in [("선택 삭제","_del"),("전체 삭제","_clear")]:
            b = QPushButton(lbl); b.setFixedHeight(26)
            b.clicked.connect(getattr(self, fn)); br.addWidget(b)
        ll.addLayout(br); v.addWidget(lst)

        cd = QGroupBox("다음 예약까지"); cl = QVBoxLayout(cd)
        self.cd_lbl = QLabel("예약 없음")
        self.cd_lbl.setStyleSheet(
            "color:#f0c040;font-family:monospace;font-size:12px;")
        cl.addWidget(self.cd_lbl); v.addWidget(cd)

    def _add(self):
        start_dt = stop_dt = None; now = datetime.now()
        if self.s_chk.isChecked():
            qd = self.s_dt.dateTime(); d = qd.date(); t = qd.time()
            start_dt = datetime(d.year(),d.month(),d.day(),t.hour(),t.minute(),t.second())
        if self.e_chk.isChecked():
            qd = self.e_dt.dateTime(); d = qd.date(); t = qd.time()
            stop_dt = datetime(d.year(),d.month(),d.day(),t.hour(),t.minute(),t.second())
        if start_dt and start_dt < now:
            QMessageBox.warning(self,"오류","시작 시각이 과거입니다."); return
        if stop_dt and stop_dt < now:
            QMessageBox.warning(self,"오류","종료 시각이 과거입니다."); return
        if start_dt and stop_dt and stop_dt <= start_dt:
            QMessageBox.warning(self,"오류","종료 > 시작이어야 합니다."); return
        if not start_dt and not stop_dt:
            QMessageBox.warning(self,"오류","시작/종료 중 하나 이상 설정하세요."); return
        actions = ['rec_start','rec_stop']
        if self.mac_chk.isChecked(): actions.append('macro_run')

        # [추가1] 전원 제어 예약
        _CH_MAP = {"B+ (bplus)":"bplus","TG B+ (tg_bplus)":"tg_bplus",
                   "ACC (acc)":"acc","IGN (ign)":"ign",
                   "PLBM 실물 (plbm_real)":"plbm_real","PLBM 모사 (plbm_sim)":"plbm_sim",
                   "전체 OFF (all)":"all"}
        pwr_on_ch = pwr_off_ch = None
        if (self._pwr_sched_grp.isChecked()
                and self.pwr_on_chk.isChecked()):
            pwr_on_ch = _CH_MAP.get(self.pwr_on_ch.currentText())
        if (self._pwr_sched_grp.isChecked()
                and self.pwr_off_chk.isChecked()):
            pwr_off_ch = _CH_MAP.get(self.pwr_off_ch.currentText())

        e = ScheduleEntry(start_dt, stop_dt, actions,
                          self.mac_rep.value(), self.mac_gap.value())
        # 전원 제어 정보를 entry에 동적 첨부
        e.pwr_on_ch  = pwr_on_ch
        e.pwr_off_ch = pwr_off_ch
        self.engine.schedules.append(e); self._add_row(e)

    def _add_row(self, e: ScheduleEntry):
        r = self.tbl.rowCount(); self.tbl.insertRow(r)
        s  = e.start_dt.strftime("%m/%d %H:%M:%S") if e.start_dt else "—"
        en = e.stop_dt.strftime("%m/%d %H:%M:%S")  if e.stop_dt  else "—"
        for c, val in enumerate([str(e.id), s, en, "+".join(e.actions), "대기"]):
            it = QTableWidgetItem(val)
            it.setTextAlignment(Qt.AlignCenter); self.tbl.setItem(r, c, it)

    def _del(self):
        rows = sorted({i.row() for i in self.tbl.selectedItems()}, reverse=True)
        for r in rows:
            it = self.tbl.item(r, 0)
            if it:
                self.engine.schedules = [
                    s for s in self.engine.schedules if s.id != int(it.text())]
            self.tbl.removeRow(r)

    def _clear(self):
        self.engine.schedules.clear(); self.tbl.setRowCount(0)

    def _check_past(self):
        now = QDateTime.currentDateTime()
        ok_s = ("QDateTimeEdit{background:#1a1a3a;color:#2ecc71;"
                "border:1px solid #2a6a3a;border-radius:4px;"
                "padding:4px 6px;font-size:12px;font-family:monospace;}"
                "QDateTimeEdit::drop-down{border:none;}"
                "QDateTimeEdit::down-arrow{image:none;width:0;}")
        ok_e = ok_s.replace("#2ecc71","#e74c3c").replace("#2a6a3a","#6a2a2a")
        bad  = ("QDateTimeEdit{background:#2a0a0a;color:#ff6b6b;"
                "border:2px solid #e74c3c;border-radius:4px;"
                "padding:4px 6px;font-size:12px;font-family:monospace;}"
                "QDateTimeEdit::drop-down{border:none;}"
                "QDateTimeEdit::down-arrow{image:none;width:0;}")
        self.s_dt.setStyleSheet(bad if self.s_dt.dateTime() < now else ok_s)
        self.e_dt.setStyleSheet(bad if self.e_dt.dateTime() < now else ok_e)

    def refresh_tbl(self):
        now = datetime.now()
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if not it: continue
            e = next((s for s in self.engine.schedules if s.id==int(it.text())), None)
            if not e: continue
            st = self.tbl.item(r, 4)
            if st:
                if e.done:       st.setText("완료"); st.setForeground(QColor("#888"))
                elif e.started:  st.setText("진행중"); st.setForeground(QColor("#2ecc71"))
                else:            st.setText("대기");   st.setForeground(QColor("#f0c040"))
        pending = [s for s in self.engine.schedules if not s.done]
        if pending:
            nxt = min(pending, key=lambda s: (s.start_dt or s.stop_dt or datetime.max))
            ref = nxt.start_dt or nxt.stop_dt
            if ref:
                secs = int((ref-now).total_seconds())
                if secs >= 0:
                    self.cd_lbl.setText(
                        f"#{nxt.id}까지  {secs//3600:02d}h {(secs%3600)//60:02d}m {secs%60:02d}s")
                else:
                    self.cd_lbl.setText(f"#{nxt.id} 진행 중…")
        else:
            self.cd_lbl.setText("예약 없음")


# =============================================================================
#  MemoPanel
# =============================================================================
