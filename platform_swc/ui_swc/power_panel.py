# -*- coding: utf-8 -*-
"""
ui_swc/power_panel.py
전원 제어 + BLTN 상태 패널
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

from kernel_swc.power_controller import PowerController
from engine_swc.settings_db import SettingsDB
class PowerControlPanel(QWidget):
    _CONTROLS = [
        ("B+ (Battery)",    "bplus"),
        ("TG B+ (Target)",  "tg_bplus"),
        ("ACC (Accessory)", "acc"),
        ("IGN (Ignition)",  "ign"),
        # [수정3] PLBM 실물/모사 분리 — 상호 배타 (동시 ON 불가)
        ("PLBM 실물 릴레이", "plbm_real"),  # D5/D12
        ("PLBM 모사 릴레이", "plbm_sim"),   # D8/D13
    ]
    # PLBM 상호 배타 쌍 (UI 레벨 참조용)
    _PLBM_MUTEX = {"plbm_real": "plbm_sim", "plbm_sim": "plbm_real"}

    def __init__(self, power_ctrl: 'PowerController', db=None, parent=None):
        super().__init__(parent)
        self._ctrl = power_ctrl
        self._db   = db
        self._ctrl.set_log_callback(self._append_log)
        # ON/OFF 버튼 참조 보관 (상태 강조용)
        self._btn_on:  dict = {}  # ch → QPushButton
        self._btn_off: dict = {}  # ch → QPushButton
        # 아두이노 핀 셀렉트박스 참조
        self._build()
        # 시작 시 포트 자동 탐색
        QTimer.singleShot(500, self._auto_scan_port)

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # ── 포트 선택 + 자동 탐색 ─────────────────────────────────────────
        pg = QGroupBox("🔌 시리얼 포트 (자동 탐색)")
        pl = QHBoxLayout(pg); pl.setSpacing(6)
        self._port_cb = QComboBox()
        self._port_cb.setStyleSheet(
            "QComboBox{background:#1a1a3a;color:#ddd;"
            "border:1px solid #3a4a6a;border-radius:3px;padding:2px 6px;}")

        ref_btn = QPushButton("🔄")
        ref_btn.setFixedSize(28, 28)
        ref_btn.setToolTip("포트 목록 새로고침")
        ref_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#9ab;"
            "border:1px solid #2a3a5a;border-radius:3px;}")
        ref_btn.clicked.connect(self._refresh_ports)

        auto_btn = QPushButton("🔍 자동탐색")
        auto_btn.setFixedHeight(28)
        auto_btn.setToolTip("아두이노 포트 자동 탐색 후 연결")
        auto_btn.setStyleSheet(
            "QPushButton{background:#1a3a2a;color:#7fdb9e;"
            "border:1px solid #2a5a3a;border-radius:4px;font-size:10px;}")
        auto_btn.clicked.connect(self._auto_scan_port)

        self._conn_btn = QPushButton("연결")
        self._conn_btn.setFixedHeight(28)
        self._conn_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:4px;font-weight:bold;}"
            "QPushButton:hover{background:#22334a;}")
        self._conn_btn.clicked.connect(self._toggle_connection)

        self._conn_status = QLabel("● 미연결")
        self._conn_status.setStyleSheet("color:#e74c3c;font-size:10px;font-weight:bold;")

        pl.addWidget(self._port_cb, 1)
        pl.addWidget(ref_btn)
        pl.addWidget(auto_btn)
        pl.addWidget(self._conn_btn)
        pl.addWidget(self._conn_status)
        v.addWidget(pg)
        self._refresh_ports()

        # ── 채널별 ON/OFF + 핀 선택 ─────────────────────────────────────────
        cg = QGroupBox("🔋 전원 채널 제어")
        cl = QVBoxLayout(cg); cl.setSpacing(6)

        _COLORS = {
            "bplus":     "#2ecc71",
            "tg_bplus":  "#3498db",
            "acc":       "#f0c040",
            "ign":       "#e67e22",
            "plbm_real": "#1abc9c",  # 청록 — 실물
            "plbm_sim":  "#9b59b6",  # 보라 — 모사
        }
        for i, (name, ch_key) in enumerate(self._CONTROLS):
            row = QHBoxLayout(); row.setSpacing(6)

            lbl = QLabel(name)
            lbl.setFixedWidth(120)
            lbl.setStyleSheet(
                f"color:{_COLORS.get(ch_key, '#aaa')};"
                "font-size:11px;font-weight:bold;")

            # [추가3] ON/OFF 버튼 — 상태에 따라 밝기 강조
            btn_on = ClickLabel("▶ ON")
            btn_on.setMinimumHeight(32)
            btn_on.setStyleSheet(self._btn_on_ss(False))
            btn_on.clicked.connect(lambda ch=ch_key: self._do_channel_on(ch))
            self._btn_on[ch_key] = btn_on

            btn_off = ClickLabel("■ OFF")
            btn_off.setMinimumHeight(32)
            btn_off.setStyleSheet(self._btn_off_ss(True))
            btn_off.clicked.connect(lambda ch=ch_key: self._do_channel_off(ch))
            self._btn_off[ch_key] = btn_off

            row.addWidget(lbl)
            row.addWidget(btn_on, 1)
            row.addWidget(btn_off, 1)
            cl.addLayout(row)

        v.addWidget(cg)

        # ── [수정3] PLBM 상호 배타 상태 표시 레이블 ─────────────────────────
        self._plbm_mutex_lbl = QLabel("● PLBM 전체 OFF")
        self._plbm_mutex_lbl.setAlignment(Qt.AlignCenter)
        self._plbm_mutex_lbl.setStyleSheet(
            "color:#556;font-size:10px;"
            "background:#0a0a1a;border:1px solid #1a1a3a;"
            "border-radius:3px;padding:2px 6px;")
        v.addWidget(self._plbm_mutex_lbl)



        # ── 전체 OFF ─────────────────────────────────────────────────────────
        all_off = QPushButton("⚠  ALL POWER OFF  (비상)")
        all_off.setMinimumHeight(44)
        all_off.setStyleSheet(
            "QPushButton{background:#7f0000;color:white;"
            "font-size:13px;font-weight:bold;"
            "border:2px solid #e74c3c;border-radius:6px;}"
            "QPushButton:hover{background:#a00000;}")
        all_off.clicked.connect(self._do_all_off)
        v.addWidget(all_off)

        # ── 수신 로그 ─────────────────────────────────────────────────────────
        lg = QGroupBox("📟 수신 로그")
        ll = QVBoxLayout(lg)
        self._log_w = QPlainTextEdit()
        self._log_w.setReadOnly(True)
        self._log_w.setMaximumBlockCount(300)  # ★ 메모리 누수 방지
        self._log_w.setFixedHeight(70)
        self._log_w.setStyleSheet(
            "QPlainTextEdit{background:#030308;color:#7bc8e0;"
            "font-family:Consolas,monospace;font-size:10px;"
            "border:1px solid #0a1a2a;}")
        ll.addWidget(self._log_w)
        v.addWidget(lg)

        # 수신 폴링 타이머
        self._rx_timer = QTimer(self)
        self._rx_timer.timeout.connect(self._poll_rx)
        self._rx_timer.start(100)

        # [문제1] 버튼 상태 동기화 타이머 (200ms)
        # 외부 스위치/UART 수신으로 _ch_state가 바뀐 경우에도 버튼 강조 갱신
        self._btn_sync_timer = QTimer(self)
        self._btn_sync_timer.timeout.connect(self._sync_btn_styles)
        self._btn_sync_timer.start(200)

    # ── 스타일시트 헬퍼 ───────────────────────────────────────────────────────
    @staticmethod
    def _btn_on_ss(active: bool) -> str:
        if active:
            return ("QLabel{background:#00bb44;color:#ffffff;"
                    "border:2px solid #00ff88;border-radius:4px;"
                    "font-weight:bold;font-size:12px;padding:4px;}")
        else:
            return ("QLabel{background:#0d2a1a;color:#3a6a4a;"
                    "border:1px solid #1a4a2a;border-radius:4px;"
                    "font-weight:bold;font-size:12px;padding:4px;}")

    @staticmethod
    def _btn_off_ss(active: bool) -> str:
        if active:
            return ("QLabel{background:#bb2200;color:#ffffff;"
                    "border:2px solid #ff4400;border-radius:4px;"
                    "font-weight:bold;font-size:12px;padding:4px;}")
        else:
            return ("QLabel{background:#2a0d0d;color:#6a3a3a;"
                    "border:1px solid #4a1a1a;border-radius:4px;"
                    "font-weight:bold;font-size:12px;padding:4px;}")

    def _sync_btn_styles(self):
        """200ms마다 _ch_state와 버튼 강조 동기화.
        [수정2] ON/OFF 상태를 명확하게 색상으로 표시.
        [수정3] PLBM 상호 배타 상태도 동기화.
        """
        for _, ch in self._CONTROLS:
            self._update_btn_style(ch, self._ctrl.is_on(ch))

        # [수정3] PLBM 상호 배타 경고 레이블 갱신
        if hasattr(self, '_plbm_mutex_lbl'):
            r_on = self._ctrl.is_on("plbm_real")
            s_on = self._ctrl.is_on("plbm_sim")
            if r_on and s_on:
                # 동시 ON은 발생하면 안 되지만 방어적으로 표시
                self._plbm_mutex_lbl.setText("⚠ PLBM 동시 ON — 오류!")
                self._plbm_mutex_lbl.setStyleSheet("color:#e74c3c;font-size:10px;font-weight:bold;")
            elif r_on:
                self._plbm_mutex_lbl.setText("▶ 실물 릴레이 ON")
                self._plbm_mutex_lbl.setStyleSheet("color:#2ecc71;font-size:10px;")
            elif s_on:
                self._plbm_mutex_lbl.setText("▶ 모사 릴레이 ON")
                self._plbm_mutex_lbl.setStyleSheet("color:#3498db;font-size:10px;")
            else:
                self._plbm_mutex_lbl.setText("● PLBM 전체 OFF")
                self._plbm_mutex_lbl.setStyleSheet("color:#556;font-size:10px;")

    def _update_btn_style(self, ch: str, is_on: bool):
        """채널 상태에 따라 ON/OFF 버튼 강조 업데이트."""
        if ch in self._btn_on:
            self._btn_on[ch].setStyleSheet(self._btn_on_ss(is_on))
        if ch in self._btn_off:
            self._btn_off[ch].setStyleSheet(self._btn_off_ss(not is_on))

    # ── 채널 제어 (DB 즉시 저장) ─────────────────────────────────────────────
    def _do_channel_on(self, ch: str):
        ok = self._ctrl.channel_on(ch)
        if ok:
            self._update_btn_style(ch, True)
            # [수정3] PLBM 상호 배타 — 반대편 OFF UI 갱신
            mutex_ch = self._PLBM_MUTEX.get(ch)
            if mutex_ch:
                self._update_btn_style(mutex_ch, False)
            self._sync_db()

    def _do_channel_off(self, ch: str):
        ok = self._ctrl.channel_off(ch)
        if ok:
            self._update_btn_style(ch, False)
            self._sync_db()

    def _do_all_off(self):
        self._ctrl.all_off()
        for _, ch in self._CONTROLS:
            self._update_btn_style(ch, False)
        self._sync_db()

    def _sync_db(self):
        """전원 상태 변경 시 DB에 즉시 저장."""
        if self._db is None:
            return
        try:
            state = {ch: self._ctrl.is_on(ch) for _, ch in self._CONTROLS}
            self._db.save_kv("power_ch_state", state)
        except Exception:
            pass

    def _refresh_ports(self):
        self._port_cb.clear()
        ports = self._ctrl.list_ports()
        if ports:
            for p in ports:
                self._port_cb.addItem(p)
        else:
            self._port_cb.addItem("(포트 없음)")

    def _auto_scan_port(self):
        """
        [추가1] 아두이노 포트 자동 탐색.
        pyserial의 comports()에서 'Arduino' 또는 'CH340' 등의 키워드로 필터링.
        찾으면 자동 연결 시도, 못 찾으면 소형 모달 경고.
        """
        try:
            import serial.tools.list_ports as _lp
            candidates = []
            for p in _lp.comports():
                desc = (p.description or "").lower()
                mfr  = (getattr(p, 'manufacturer', '') or "").lower()
                hwid = (getattr(p, 'hwid', '') or "").lower()
                # Arduino / CH340 / CH341 / FTDI / CP210x 칩 키워드
                if any(k in desc + mfr + hwid
                       for k in ["arduino", "ch340", "ch341",
                                  "ftdi", "cp210", "usb serial"]):
                    candidates.append(p.device)
            if candidates:
                best = candidates[0]
                # 포트 콤보박스 갱신
                self._port_cb.clear()
                for c in candidates:
                    self._port_cb.addItem(c)
                self._port_cb.setCurrentText(best)
                # 자동 연결
                if not self._ctrl.is_connected:
                    ok = self._ctrl.connect(best, 9600)
                    if ok:
                        self._conn_btn.setText("연결 해제")
                        self._conn_status.setText(f"● {best} (자동)")
                        self._conn_status.setStyleSheet(
                            "color:#2ecc71;font-size:10px;font-weight:bold;")
                        self._append_log(f"[Power] 자동 탐색 연결: {best}")
                    else:
                        self._show_no_device_warning(best)
            else:
                # 포트 목록 갱신 후 경고
                self._refresh_ports()
                self._show_no_device_warning(None)
        except ImportError:
            self._refresh_ports()
        except Exception as ex:
            self._append_log(f"[Power] 자동 탐색 오류: {ex}")
            self._refresh_ports()

    def _show_no_device_warning(self, port):
        """아두이노 미연결 시 소형 모달 경고."""
        msg = QMessageBox(self)
        msg.setWindowTitle("전원 제어 모듈 미감지")
        if port:
            body = (f"포트 {port}에서 연결에 실패했습니다.\n"
                    "USB 케이블 연결 및 드라이버를 확인하세요.")
        else:
            body = ("아두이노(전원 제어 모듈)가 감지되지 않았습니다.\n"
                    "USB를 연결하거나 포트를 직접 선택 후 [연결]을 클릭하세요.")
        msg.setText(body)
        msg.setIcon(QMessageBox.Warning)
        msg.setStandardButtons(QMessageBox.Ok)
        msg.setStyleSheet(
            "QMessageBox{background:#1a0a0a;color:#ddd;}"
            "QLabel{color:#f0c040;font-size:11px;}")
        msg.exec_()

    def _toggle_connection(self):
        if self._ctrl.is_connected:
            self._ctrl.disconnect()
            self._conn_btn.setText("연결")
            self._conn_btn.setStyleSheet(
                "QPushButton{background:#1a2a3a;color:#7bc8e0;"
                "border:1px solid #2a4a6a;border-radius:4px;font-weight:bold;}")
            self._conn_status.setText("● 미연결")
            self._conn_status.setStyleSheet(
                "color:#e74c3c;font-size:10px;font-weight:bold;")
        else:
            port = self._port_cb.currentText()
            if not port or port.startswith("("):
                QMessageBox.warning(self, "오류", "연결할 포트를 선택하세요.")
                return
            ok = self._ctrl.connect(port, 9600)
            if ok:
                self._conn_btn.setText("연결 해제")
                self._conn_btn.setStyleSheet(
                    "QPushButton{background:#1a4a2a;color:#afffcf;"
                    "border:1px solid #2a8a5a;border-radius:4px;font-weight:bold;}")
                self._conn_status.setText(f"● {port} 연결됨")
                self._conn_status.setStyleSheet(
                    "color:#2ecc71;font-size:10px;font-weight:bold;")
            else:
                QMessageBox.critical(self, "연결 실패",
                    f"{port} 연결에 실패했습니다.\npyserial 설치 여부를 확인하세요.")

    def _append_log(self, msg: str):
        self._log_w.appendPlainText(msg)
        self._log_w.verticalScrollBar().setValue(
            self._log_w.verticalScrollBar().maximum())

    def _poll_rx(self):
        """UI 스레드에서 100ms마다 실행 — 로그 큐 소비 전용.
        [스레드 안전 수정] _rx_loop(백그라운드)가 _log_queue에 쌓은 메시지를
        여기서 꺼내 QPlainTextEdit에 표시. UI 위젯은 반드시 메인 스레드에서만 접근.
        """
        # _log_cb가 있을 때만 (패널이 연결된 상태) 소비
        if not self._ctrl._log_cb:
            return
        msgs = []
        q = self._ctrl._log_queue
        while q:
            try:
                msgs.append(q.popleft())
            except IndexError:
                break
        if msgs:
            for m in msgs:
                try:
                    self._ctrl._log_cb(m)
                except Exception:
                    pass

    def closeEvent(self, e):
        super().closeEvent(e)


# =============================================================================
#  BLTNStatusPanel  — BLTN OHCL LED PWM 점멸 상태 UI + OHCL 릴레이 레코딩 트리거
# =============================================================================
class BLTNStatusPanel(QWidget):
    """
    BLTN 상태 패널 (v1.0)

    기능:
      1. BLTN OHCL LED PWM 점멸 상태 표시
         - Arduino A2(PC2)에서 analogRead 샘플링 -> 엣지 간격(반주기ms) 'P<ms>\\n' UART 수신
         - LED ON/OFF 상태 표시, 반주기 표시, 전체주기 표시
      2. OHCL 릴레이 이벤트 표시 ("M\\n" / "N\\n")
         - 싱글클릭(N) -> A1 릴레이 1250ms CLOSE -> 1300ms 미만 -> MAR 목표
         - 더블클릭(M) -> A1 릴레이 1350ms CLOSE -> 1300ms 이상 -> TML 목표
         - 각 이벤트 로그 표시 (타임스탬프 포함)
      3. LED PWM 심플 막대차트: 최근 30트 반주기 표시

    API:
      bltn.led_on            LED PWM ON 여부 (bool)
      bltn.half_period_ms    마지막 수신 반주기 ms (int)
      bltn.last_event        마지막 OHCL 이벤트 dict {type, relay_ms}
    """

    _MAX_CHART_POINTS = 30

    def __init__(self, power_ctrl, parent=None):
        super().__init__(parent)
        self._ctrl = power_ctrl
        self._chart_data = []
        from collections import deque as _dq
        self._event_queue = _dq(maxlen=100)
        self._build()
        self._ctrl.register_ohcl_callback(self._on_ohcl_event)
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start(100)

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        # LED PWM 상태 그룹
        led_grp = QGroupBox("\U0001f4a1 BLTN LED PWM 상태 (A2 입력)")
        led_grp.setStyleSheet(
            "QGroupBox{color:#f0c040;font-weight:bold;font-size:11px;"
            "border:1px solid #3a3a10;border-radius:5px;margin-top:8px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;}")
        led_lay = QVBoxLayout(led_grp)
        led_lay.setSpacing(6)

        self._led_status_lbl = QLabel("\u25cb LED OFF")
        self._led_status_lbl.setAlignment(Qt.AlignCenter)
        self._led_status_lbl.setMinimumHeight(32)
        self._led_status_lbl.setStyleSheet(
            "color:#556;font-size:13px;font-weight:bold;"
            "background:#0a0a0a;border:2px solid #1a1a1a;border-radius:6px;padding:4px;")
        led_lay.addWidget(self._led_status_lbl)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self._half_lbl = QLabel("반주기: — ms")
        self._half_lbl.setStyleSheet("color:#9ab;font-size:11px;")
        self._full_lbl = QLabel("전체주기: — ms")
        self._full_lbl.setStyleSheet("color:#7bc8e0;font-size:11px;")
        row2.addWidget(self._half_lbl, 1)
        row2.addWidget(self._full_lbl, 1)
        led_lay.addLayout(row2)

        self._chart_widget = _LedPwmChart(self)
        self._chart_widget.setFixedHeight(60)
        led_lay.addWidget(self._chart_widget)
        v.addWidget(led_grp)

        # OHCL 이벤트 그룹
        ohcl_grp = QGroupBox("\U0001f527 OHCL 스위치 (D6) → 릴레이 (A1)")
        ohcl_grp.setStyleSheet(
            "QGroupBox{color:#ffb347;font-weight:bold;font-size:11px;"
            "border:1px solid #3a2a10;border-radius:5px;margin-top:8px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;}")
        ohcl_lay = QVBoxLayout(ohcl_grp)
        ohcl_lay.setSpacing(6)

        # ── 릴레이 시간 설정 행 ─────────────────────────────────────────
        time_row = QHBoxLayout(); time_row.setSpacing(6)

        mar_time_lbl = QLabel("MAR (ms):")
        mar_time_lbl.setStyleSheet("color:#00cc88;font-size:10px;font-weight:bold;")
        self._mar_spin = QSpinBox()
        self._mar_spin.setRange(100, 1299)
        self._mar_spin.setValue(1250)
        self._mar_spin.setSingleStep(10)
        self._mar_spin.setMinimumWidth(90)
        self._mar_spin.setFixedHeight(28)
        self._mar_spin.setKeyboardTracking(True)
        self._mar_spin.setToolTip("MAR 릴레이 시간 (100~1299ms)\n▲▼ 또는 직접 숫자 입력 가능\n※ 1300ms 미만 = MAR 녹화")
        self._mar_spin.setStyleSheet(
            "QSpinBox{background:#0a1a0a;color:#00cc88;"
            "border:1px solid #1a4a1a;border-radius:3px;"
            "padding:2px 4px;font-size:12px;font-weight:bold;}"
            "QSpinBox::up-button{width:18px;border-left:1px solid #1a4a1a;}"
            "QSpinBox::down-button{width:18px;border-left:1px solid #1a4a1a;}")

        tml_time_lbl = QLabel("TML (ms):")
        tml_time_lbl.setStyleSheet("color:#7bc8ff;font-size:10px;font-weight:bold;")
        self._tml_spin = QSpinBox()
        self._tml_spin.setRange(1300, 9999)
        self._tml_spin.setValue(1350)
        self._tml_spin.setSingleStep(10)
        self._tml_spin.setMinimumWidth(90)
        self._tml_spin.setFixedHeight(28)
        self._tml_spin.setKeyboardTracking(True)
        self._tml_spin.setToolTip("TML 릴레이 시간 (1300~9999ms)\n▲▼ 또는 직접 숫자 입력 가능\n※ 1300ms 이상 = TML 녹화")
        self._tml_spin.setStyleSheet(
            "QSpinBox{background:#0a0a1a;color:#7bc8ff;"
            "border:1px solid #1a1a4a;border-radius:3px;"
            "padding:2px 4px;font-size:12px;font-weight:bold;}"
            "QSpinBox::up-button{width:18px;border-left:1px solid #1a1a4a;}"
            "QSpinBox::down-button{width:18px;border-left:1px solid #1a1a4a;}")

        time_row.addWidget(mar_time_lbl)
        time_row.addWidget(self._mar_spin)
        time_row.addSpacing(12)
        time_row.addWidget(tml_time_lbl)
        time_row.addWidget(self._tml_spin)
        time_row.addStretch()
        ohcl_lay.addLayout(time_row)

        # ── MAR / TML 버튼 행 ──────────────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)

        self._mar_btn = QPushButton("▶  MAR 녹화 트리거")
        self._mar_btn.setMinimumHeight(36)
        self._mar_btn.setStyleSheet(
            "QPushButton{background:#003a18;color:#00ff88;"
            "border:2px solid #00cc66;border-radius:6px;"
            "font-size:12px;font-weight:bold;}"
            "QPushButton:hover{background:#005a28;}"
            "QPushButton:pressed{background:#007a38;}"
            "QPushButton:disabled{background:#0a1a0a;color:#335;border-color:#1a2a1a;}")
        self._mar_btn.setToolTip("A1 릴레이를 MAR 시간(ms)만큼 CLOSE → Arduino로 전송")
        self._mar_btn.clicked.connect(self._trigger_mar)

        self._tml_btn = QPushButton("▶  TML 녹화 트리거")
        self._tml_btn.setMinimumHeight(36)
        self._tml_btn.setStyleSheet(
            "QPushButton{background:#001a3a;color:#7bc8ff;"
            "border:2px solid #3a8aff;border-radius:6px;"
            "font-size:12px;font-weight:bold;}"
            "QPushButton:hover{background:#002a5a;}"
            "QPushButton:pressed{background:#003a7a;}"
            "QPushButton:disabled{background:#0a0a1a;color:#335;border-color:#1a1a2a;}")
        self._tml_btn.setToolTip("A1 릴레이를 TML 시간(ms)만큼 CLOSE → Arduino로 전송")
        self._tml_btn.clicked.connect(self._trigger_tml)

        btn_row.addWidget(self._mar_btn, 1)
        btn_row.addWidget(self._tml_btn, 1)
        ohcl_lay.addLayout(btn_row)

        # ── 수신 이벤트 상태 표시 행 (싱글/더블/결과) ──────────────────
        row3 = QHBoxLayout(); row3.setSpacing(6)
        self._ohcl_single_lbl = QLabel("N (싱글SW)")
        self._ohcl_single_lbl.setAlignment(Qt.AlignCenter)
        self._ohcl_single_lbl.setMinimumHeight(26)
        self._ohcl_single_lbl.setStyleSheet(
            "color:#556;font-size:10px;font-weight:bold;"
            "background:#0a0808;border:1px solid #1a1a1a;border-radius:4px;")
        self._ohcl_double_lbl = QLabel("M (더블SW)")
        self._ohcl_double_lbl.setAlignment(Qt.AlignCenter)
        self._ohcl_double_lbl.setMinimumHeight(26)
        self._ohcl_double_lbl.setStyleSheet(
            "color:#556;font-size:10px;font-weight:bold;"
            "background:#0a0808;border:1px solid #1a1a1a;border-radius:4px;")
        self._rec_type_lbl = QLabel("—")
        self._rec_type_lbl.setAlignment(Qt.AlignCenter)
        self._rec_type_lbl.setMinimumHeight(26)
        self._rec_type_lbl.setStyleSheet(
            "color:#888;font-size:10px;font-weight:bold;"
            "background:#0d0d1a;border:1px solid #1a1a2a;border-radius:4px;")
        row3.addWidget(self._ohcl_single_lbl, 1)
        row3.addWidget(self._ohcl_double_lbl, 1)
        row3.addWidget(self._rec_type_lbl, 1)
        ohcl_lay.addLayout(row3)

        self._ohcl_log = QPlainTextEdit()
        self._ohcl_log.setReadOnly(True)
        self._ohcl_log.setMaximumBlockCount(200)  # ★ 메모리 누수 방지
        self._ohcl_log.setFixedHeight(70)
        self._ohcl_log.setStyleSheet(
            "QPlainTextEdit{background:#060610;color:#f0c040;"
            "font-family:Consolas,monospace;font-size:10px;"
            "border:1px solid #1a1a0a;}")
        ohcl_lay.addWidget(self._ohcl_log)
        v.addWidget(ohcl_grp)
        v.addStretch()

    # ── MAR / TML 버튼 핸들러 ─────────────────────────────────────────────
    def _trigger_mar(self):
        """MAR 녹화 트리거: 스핀박스 값(ms)으로 A1 릴레이 CLOSE."""
        if not self._ctrl.is_connected:
            self._ohcl_log.appendPlainText("[!] Arduino 미연결 — 트리거 불가")
            return
        ms = self._mar_spin.value()
        self._mar_btn.setEnabled(False)
        self._tml_btn.setEnabled(False)
        ok = self._ctrl.ohcl_trigger(ms)
        if not ok:
            self._ohcl_log.appendPlainText(f"[!] MAR 트리거 전송 실패 ({ms}ms)")
        # 버튼은 K패킷 수신(완료 보고) 후 _refresh_ui에서 재활성화
        # 안전장치: 최대 (ms+500)ms 후 강제 재활성화
        QTimer.singleShot(ms + 500, self._re_enable_btns)

    def _trigger_tml(self):
        """TML 녹화 트리거: 스핀박스 값(ms)으로 A1 릴레이 CLOSE."""
        if not self._ctrl.is_connected:
            self._ohcl_log.appendPlainText("[!] Arduino 미연결 — 트리거 불가")
            return
        ms = self._tml_spin.value()
        self._mar_btn.setEnabled(False)
        self._tml_btn.setEnabled(False)
        ok = self._ctrl.ohcl_trigger(ms)
        if not ok:
            self._ohcl_log.appendPlainText(f"[!] TML 트리거 전송 실패 ({ms}ms)")
        QTimer.singleShot(ms + 500, self._re_enable_btns)

    def _re_enable_btns(self):
        self._mar_btn.setEnabled(True)
        self._tml_btn.setEnabled(True)

    # OHCL 콜백 (백그라운드 스레드에서 호출) — 큐 추가만, UI 조작 금지
    def _on_ohcl_event(self, event_type, flag, value):
        import time as _t
        ts = _t.strftime("%H:%M:%S")
        if event_type == "ohcl_event":
            relay_ms = value if value is not None else 0
            label    = "더블클릭(M)" if flag else "싱글클릭(N)"
            rec      = "TML" if relay_ms >= 1300 else "MAR"
            msg      = f"[{ts}] {label} -> A1 {relay_ms}ms CLOSE -> {rec} 목표"
            self._event_queue.append(("ohcl", flag, relay_ms, msg))
        elif event_type == "led_period":
            half_ms = value if value is not None else 0
            self._event_queue.append(("period", flag, half_ms, ""))

    # UI 갱신 타이머 (100ms, UI 스레드)
    def _refresh_ui(self):
        led_on = self._ctrl._led_pwm_on
        if led_on:
            self._led_status_lbl.setText("\u25cf LED ON")
            self._led_status_lbl.setStyleSheet(
                "color:#f0c040;font-size:13px;font-weight:bold;"
                "background:#1a1800;border:2px solid #aaa000;border-radius:6px;padding:4px;")
        else:
            self._led_status_lbl.setText("\u25cb LED OFF")
            self._led_status_lbl.setStyleSheet(
                "color:#556;font-size:13px;font-weight:bold;"
                "background:#0a0a0a;border:2px solid #1a1a1a;border-radius:6px;padding:4px;")

        half_ms = self._ctrl._led_half_period_ms
        if led_on and half_ms > 0:
            self._half_lbl.setText(f"반주기: {half_ms} ms")
            self._full_lbl.setText(f"전체주기: {half_ms * 2} ms")
        else:
            self._half_lbl.setText("반주기: — ms")
            self._full_lbl.setText("전체주기: — ms")

        while self._event_queue:
            try:
                ev = self._event_queue.popleft()
            except IndexError:
                break
            ev_type, flag, value, msg = ev
            if ev_type == "ohcl":
                relay_ms = value
                rec_type = "TML" if relay_ms >= 1300 else "MAR"
                # K패킷(PC 트리거 완료) 수신 시 버튼 즉시 재활성화
                self._re_enable_btns()
                if flag:  # 더블클릭
                    self._ohcl_double_lbl.setStyleSheet(
                        "color:#ff8800;font-size:11px;font-weight:bold;"
                        "background:#2a1800;border:2px solid #ff8800;border-radius:4px;")
                    self._ohcl_single_lbl.setStyleSheet(
                        "color:#556;font-size:11px;font-weight:bold;"
                        "background:#0a0808;border:1px solid #1a1a1a;border-radius:4px;")
                    QTimer.singleShot(2000, lambda: self._ohcl_double_lbl.setStyleSheet(
                        "color:#556;font-size:11px;font-weight:bold;"
                        "background:#0a0808;border:1px solid #1a1a1a;border-radius:4px;"))
                else:  # 싱글클릭
                    self._ohcl_single_lbl.setStyleSheet(
                        "color:#00cc88;font-size:11px;font-weight:bold;"
                        "background:#002a18;border:2px solid #00cc88;border-radius:4px;")
                    self._ohcl_double_lbl.setStyleSheet(
                        "color:#556;font-size:11px;font-weight:bold;"
                        "background:#0a0808;border:1px solid #1a1a1a;border-radius:4px;")
                    QTimer.singleShot(2000, lambda: self._ohcl_single_lbl.setStyleSheet(
                        "color:#556;font-size:11px;font-weight:bold;"
                        "background:#0a0808;border:1px solid #1a1a1a;border-radius:4px;"))
                if rec_type == "MAR":
                    self._rec_type_lbl.setText("\u25b6 MAR 녹화")
                    self._rec_type_lbl.setStyleSheet(
                        "color:#00ff88;font-size:11px;font-weight:bold;"
                        "background:#002a10;border:2px solid #00ff88;border-radius:4px;")
                else:
                    self._rec_type_lbl.setText("\u25b6 TML 녹화")
                    self._rec_type_lbl.setStyleSheet(
                        "color:#7bc8ff;font-size:11px;font-weight:bold;"
                        "background:#001a2a;border:2px solid #7bc8ff;border-radius:4px;")
                QTimer.singleShot(3000, self._reset_rec_lbl)
                if msg:
                    self._ohcl_log.appendPlainText(msg)
                    self._ohcl_log.verticalScrollBar().setValue(
                        self._ohcl_log.verticalScrollBar().maximum())
            elif ev_type == "period":
                half_ms = value
                import time as _t
                self._chart_data.append((_t.time(), half_ms))
                if len(self._chart_data) > self._MAX_CHART_POINTS:
                    self._chart_data = self._chart_data[-self._MAX_CHART_POINTS:]
                self._chart_widget.update_data(self._chart_data)

    def _reset_rec_lbl(self):
        self._rec_type_lbl.setText("—")
        self._rec_type_lbl.setStyleSheet(
            "color:#888;font-size:11px;font-weight:bold;"
            "background:#0d0d1a;border:1px solid #1a1a2a;border-radius:4px;")

    @property
    def led_on(self):
        return self._ctrl._led_pwm_on

    @property
    def half_period_ms(self):
        return self._ctrl._led_half_period_ms

    @property
    def last_event(self):
        return dict(self._ctrl._ohcl_last_event)


# =============================================================================
#  _LedPwmChart  — LED 반주기 심플 막대차트
# =============================================================================
class _LedPwmChart(QWidget):
    """LED 반주기(ms) 심플 막대 차트. update_data([(타임스탬프, ms)])로 갱신."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = []
        self.setMinimumHeight(40)

    def update_data(self, data):
        self._data = list(data)
        self.update()

    def paintEvent(self, event):
        from PyQt5.QtGui import QPainter, QColor, QPen
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.fillRect(0, 0, w, h, QColor("#060610"))
        if len(self._data) < 2:
            p.setPen(QPen(QColor("#334"), 1))
            p.drawText(0, 0, w, h, Qt.AlignCenter, "LED 반주기 데이터 부재...")
            p.end()
            return
        vals = [v for _, v in self._data]
        mn, mx = min(vals), max(vals)
        span = mx - mn if mx != mn else 1
        n = len(vals)
        pad = 4
        for i in range(n):
            x = pad + int(i * (w - pad * 2) / max(n - 1, 1))
            bar_h = int((vals[i] - mn) / span * (h - pad * 2)) + pad
            bar_h = max(4, min(bar_h, h - pad))
            p.fillRect(x - 2, h - bar_h, 4, bar_h, QColor("#1a7aaa"))
        p.setPen(QPen(QColor("#7bc8e0"), 1))
        p.drawText(w - 52, 2, 50, 14, Qt.AlignRight, f"{vals[-1]}ms")
        p.end()


# =============================================================================
