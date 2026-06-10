# -*- coding: utf-8 -*-
"""
ui_swc/timer_panel.py
조건부 타이머 패널
"""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QObject, QPoint, QRect, QDateTime, Q_ARG
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor
from common.signals import Signals
from common.models import RoiItem, MacroStep, MemoOverlayCfg, ScheduleEntry
from common.utils import open_folder, fmt_hms, draw_memo_overlay, _get_font, PIL_AVAILABLE

import json
class TimerPanel(QWidget):
    """
    조건부 타이머 패널 (v6.0 — 통합 AND/OR 조건 구조)

    ── 조건 구조 (v6.0 재설계) ───────────────────────────────────────────
    각 트리거(시작/종료/랩) 마다:
      · AND 그룹: 나열된 모든 조건이 충족돼야 트리거
      · OR  그룹: 나열된 조건 중 하나 이상 충족되면 트리거
      · 최종 트리거: (AND그룹 충족) OR (OR그룹 중 하나 충족)
      · 각 그룹 안에 전원 조건(채널 ON/OFF)과 CAN 조건(신호 비교)을
        자유롭게 섞어서 추가 가능

    예시:
      AND: [B+ ON]  [BLTN_CAM_RecSta_OWD == 1]  [ACC ON]
      OR:  [IGN ON] [BLTN_CAM_Ind_Req == 2]
      → (B+ ON AND CAN==1 AND ACC ON) OR (IGN ON) OR (CAN==2)

    ── 랩 조건 ───────────────────────────────────────────────────────────
    조건이 처음 충족될 때만 랩 기록 (조건 지속돼도 중복 기록 없음)
    해제 후 재충족 시 다시 기록

    ── 커널 API ──────────────────────────────────────────────────────────
    timer.start()           수동 시작
    timer.stop()            수동 종료
    timer.lap()             랩 기록 + 현재 경과시간(초) 반환
    timer.reset()           리셋 (종료 + 0 초기화)
    timer.elapsed           현재 경과시간(초)
    timer.running           실행 중 여부

    ── DB 키 ─────────────────────────────────────────────────────────────
    timer_watch_on          전원 조건 감시 ON/OFF
    timer_{kind}_and_conds  AND 그룹 조건 JSON [{type,ch,on} or {type,sig,op,val}]
    timer_{kind}_or_conds   OR  그룹 조건 JSON
    kind = start | stop | lap
    """

    _CHANNELS = [
        ("bplus",     "B+"),
        ("tg_bplus",  "TG B+"),
        ("acc",       "ACC"),
        ("ign",       "IGN"),
        ("plbm_real", "PLBM 실물"),
        ("plbm_sim",  "PLBM 모사"),
    ]
    _CAN_OPS = ["==", "!=", ">", ">=", "<", "<="]

    def __init__(self, engine, signals, power_ctrl=None, parent=None):
        super().__init__(parent)
        self._engine     = engine
        self._signals    = signals
        self._power_ctrl = power_ctrl
        self._db         = getattr(engine, '_db', None)

        self._running    = False
        self._start_ts   = 0.0
        self._elapsed    = 0.0
        self._laps       = []

        self._cond_watch_enabled = False

        # 랩 조건 중복 기록 방지: 조건이 충족된 상태인지 추적
        self._lap_cond_active = False   # True = 현재 충족 상태 (재충족 무시)

        # ★ [문제1 수정] findChildren 캐시 — 메인 이벤트 루프 부하 감소
        # {(kind, group): [row_widget, ...]} 형태로 조건 행 위젯 목록 캐싱.
        # 행 추가/삭제 시 _invalidate_cond_cache() 호출로 무효화.
        self._cond_row_cache: dict = {}

        self._build()

        self._disp_timer = QTimer(self)
        self._disp_timer.setInterval(100)
        self._disp_timer.timeout.connect(self._update_display)
        self._disp_timer.start()

        self._db_timer = QTimer(self)
        self._db_timer.setSingleShot(True)
        self._db_timer.setInterval(1000)
        self._db_timer.timeout.connect(self._flush_db)

        self._can_poll_timer = QTimer(self)
        # ★ [문제1 수정] 500ms → 150ms: findChildren 캐싱으로 비용 제거됐으므로
        # 더 자주 CAN 조건 평가 가능. 타이머 반응성 대폭 향상.
        self._can_poll_timer.setInterval(150)
        self._can_poll_timer.timeout.connect(self._eval_can_only)

        if power_ctrl is not None:
            self._register_power_callback(power_ctrl)

        self._load_db()

    # ── PowerController 연동 ────────────────────────────────────────────
    def set_power(self, power_ctrl):
        self._power_ctrl = power_ctrl
        self._register_power_callback(power_ctrl)

    def _register_power_callback(self, power_ctrl):
        try:
            power_ctrl.register_state_callback(self._on_power_state_change)
        except Exception:
            pass

    # ── 전원 이벤트 콜백 ────────────────────────────────────────────────
    def _on_power_state_change(self, ch: str, is_on: bool):
        """
        전원 채널 상태 변경 이벤트.
        [문제2 수정] changed 딕셔너리는 _eval_one_row 에서 더 이상 사용하지 않음.
        curr(현재 전체 채널 상태)만 넘겨 평가. changed는 하위 호환용으로 유지.

        [버그 수정] _fire_state_change()는 Arduino RX 백그라운드 스레드에서 호출됨.
        Qt 위젯(_get_cond_rows_cached 등)은 메인 스레드에서만 접근 가능하므로
        QMetaObject.invokeMethod로 메인 스레드에 마샬링.
        백그라운드 스레드에서 직접 _eval_trigger 호출 시 Qt 위젯 접근으로
        데이터 레이스 → 조건 평가 무시/크래시 발생하던 버그 수정.
        """
        from PyQt5.QtCore import QMetaObject, Qt as _Qt2
        QMetaObject.invokeMethod(
            self, "_on_power_state_change_main",
            _Qt2.QueuedConnection,
            Q_ARG(str, ch),
            Q_ARG(bool, is_on)
        )

    @pyqtSlot(str, bool)
    def _on_power_state_change_main(self, ch: str, is_on: bool):
        """메인 스레드에서 실행되는 실제 전원 조건 평가."""
        if not self._cond_watch_enabled:
            return
        curr    = {c: self._power_ctrl.is_on(c) for c, _ in self._CHANNELS}
        changed = {c: False for c, _ in self._CHANNELS}  # 미사용 (하위 호환 유지)
        reason  = f"전원({ch}={'ON' if is_on else 'OFF'})"

        if not self._running:
            if self._eval_trigger("start", curr, changed):
                self._start(reason)
        if self._running:
            # 랩 조건을 stop 보다 먼저 평가
            self._check_lap(curr, changed, reason)
            if self._eval_trigger("stop", curr, changed):
                self._stop(reason)

    # ── CAN 단독 주기 평가 (500ms) ───────────────────────────────────────
    def _eval_can_only(self):
        if not self._cond_watch_enabled:
            return
        curr    = {c: (self._power_ctrl.is_on(c) if self._power_ctrl else False)
                   for c, _ in self._CHANNELS}
        changed = {c: False for c, _ in self._CHANNELS}  # 미사용 (하위 호환 유지)

        if not self._running:
            if self._eval_trigger("start", curr, changed):
                self._start("CAN 조건")
        if self._running:
            self._check_lap(curr, changed, "CAN 조건")
            if self._eval_trigger("stop", curr, changed):
                self._stop("CAN 조건")

    # ── 통합 조건 평가 ───────────────────────────────────────────────────
    def _eval_trigger(self, kind: str, curr: dict, changed: dict) -> bool:
        """
        AND 그룹: 모든 조건 충족 (AND)
        OR  그룹: 하나 이상 충족 (OR)
        최종: AND그룹 결과 OR OR그룹 결과
        둘 다 비어 있으면 False.
        ★ [문제1 수정] _eval_rows_cached() 사용 — findChildren 캐시 재사용.
        """
        and_empty = not self._has_filled_rows(kind, "and")
        or_empty  = not self._has_filled_rows(kind, "or")

        if and_empty and or_empty:
            return False

        and_result = (False if and_empty
                      else self._eval_rows_cached(kind, "and", "and", curr, changed))
        or_result  = (False if or_empty
                      else self._eval_rows_cached(kind, "or",  "or",  curr, changed))

        if and_empty:
            return or_result
        if or_empty:
            return and_result
        return and_result or or_result

    def _get_rows(self, kind: str, group: str):
        """행 컨테이너 위젯 반환."""
        return getattr(self, f'_{kind}_{group}_rows', None)

    def _invalidate_cond_cache(self):
        """조건 행 위젯 캐시 무효화 — 행 추가/삭제 시 호출."""
        self._cond_row_cache.clear()

    def _get_cond_rows_cached(self, kind: str, group: str) -> list:
        """
        findChildren(QWidget, "_cond_row") 결과를 캐싱해 반환.
        ★ [문제1 수정] 매 평가마다 Qt 위젯 트리 재귀 탐색하던 것을
          첫 호출 시 1회만 탐색하고 이후 캐시 재사용.
        """
        key = (kind, group)
        if key not in self._cond_row_cache:
            rows_w = self._get_rows(kind, group)
            if rows_w:
                self._cond_row_cache[key] = rows_w.findChildren(QWidget, "_cond_row")
            else:
                self._cond_row_cache[key] = []
        return self._cond_row_cache[key]

    def _has_filled_rows(self, kind: str, group: str) -> bool:
        """최소 1개 이상의 유효한 조건 행이 있는지. 캐시된 행 목록 사용."""
        for row in self._get_cond_rows_cached(kind, group):
            if self._row_is_filled(row):
                return True
        return False

    def _eval_rows(self, rows_w, logic: str, curr: dict, changed: dict) -> bool:
        """
        행 그룹을 AND 또는 OR로 평가.
        ★ [문제1 수정] rows_w 인자를 직접 쓰는 대신 캐시를 통해 접근.
        rows_w가 None이면 kind/group을 역추적해야 하므로 기존 인터페이스 유지하되,
        _eval_trigger에서 kind/group 기반으로 직접 캐시를 사용하도록 변경.
        """
        if not rows_w:
            return False
        results = []
        for row in rows_w.findChildren(QWidget, "_cond_row"):
            if not self._row_is_filled(row):
                continue
            ok = self._eval_one_row(row, curr, changed)
            results.append(ok)

        if not results:
            return False
        if logic == "and":
            return all(results)
        else:
            return any(results)

    def _eval_rows_cached(self, kind: str, group: str, logic: str,
                          curr: dict, changed: dict) -> bool:
        """캐시된 행 목록으로 AND/OR 평가. _eval_trigger에서 사용."""
        results = []
        for row in self._get_cond_rows_cached(kind, group):
            if not self._row_is_filled(row):
                continue
            results.append(self._eval_one_row(row, curr, changed))
        if not results:
            return False
        return all(results) if logic == "and" else any(results)

    def _row_is_filled(self, row) -> bool:
        """행에 유효한 조건이 입력됐는지."""
        type_cb = row.findChild(QComboBox, "_type_cb")
        if not type_cb:
            return False
        t = type_cb.currentText()
        if t == "전원":
            ch_cb = row.findChild(QComboBox, "_ch_cb")
            return ch_cb is not None and ch_cb.currentText() != ""
        else:  # CAN
            sig_cb = row.findChild(QComboBox, "_sig_cb")
            val_le = row.findChild(QLineEdit, "_val_le")
            return (sig_cb is not None and sig_cb.currentText().strip() != ""
                    and val_le is not None and val_le.text().strip() != "")

    def _eval_rows(self, rows_w, logic: str, curr: dict, changed: dict) -> bool:
        """행 그룹을 AND 또는 OR로 평가."""
        if not rows_w:
            return False
        results = []
        for row in rows_w.findChildren(QWidget, "_cond_row"):
            if not self._row_is_filled(row):
                continue
            ok = self._eval_one_row(row, curr, changed)
            results.append(ok)

        if not results:
            return False
        if logic == "and":
            return all(results)
        else:
            return any(results)

    def _eval_one_row(self, row, curr: dict, changed: dict) -> bool:
        """
        단일 조건 행 평가 (전원 or CAN).

        [문제2 수정] AND 조건 다중 채널 평가 안 됨
          근본 원인: 기존 로직 — changed.get(ch, False) AND curr.get(ch, False)==want
                     전원 이벤트 콜백(_on_power_state_change)에서 changed 딕셔너리는
                     '이번에 변경된 채널' 만 True.
                     예) ACC OFF 이벤트 -> changed={"acc":True, 나머지:False}
                         AND 조건에 ACC OFF + IGN OFF 두 조건이 있으면:
                           acc 행: changed["acc"]=True, curr["acc"]=False → True
                           ign 행: changed["ign"]=False → False  ← 여기서 막힘
                         -> AND 전체 False. ACC/IGN 동시 변경이 아닌 이상 절대 충족 불가.
          수정:
            전원 조건 행을 평가할 때 changed 기반 트리거 체크를 제거하고
            curr(현재 실제 상태) 만 기준으로 want 값과 비교.
            트리거 시점 제어는 _on_power_state_change 콜백이 이미 담당하므로
            (이벤트 발생 시에만 _eval_trigger 호출)
            개별 행은 '현재 상태가 원하는 값과 일치하는가' 만 판단하면 됨.

            랩/종료 조건도 동일 구조이므로 자동으로 함께 수정됨.
        """
        type_cb = row.findChild(QComboBox, "_type_cb")
        if not type_cb:
            return False
        t = type_cb.currentText()

        if t == "전원":
            ch_cb = row.findChild(QComboBox, "_ch_cb")
            on_rb = row.findChild(QRadioButton, "_on_rb")
            if not ch_cb:
                return False
            ch   = ch_cb.currentData() or ch_cb.currentText()
            want = on_rb.isChecked() if on_rb else True
            # ★ [문제2 수정] changed 조건 제거 — 현재 상태(curr)만 비교
            # changed 기반 판단은 이벤트 발생 단일 채널만 True가 되어
            # AND 다중 채널 조건을 절대 충족 불가하게 만드는 버그가 있었음.
            return curr.get(ch, False) == want
        else:  # CAN
            sig_cb = row.findChild(QComboBox, "_sig_cb")
            op_cb  = row.findChild(QComboBox, "_op_cb")
            val_le = row.findChild(QLineEdit, "_val_le")
            if not (sig_cb and op_cb and val_le):
                return False
            sig  = sig_cb.currentText().strip()
            op   = op_cb.currentText()
            vstr = val_le.text().strip()
            if not sig or not vstr:
                return False
            try:
                target = float(vstr)
                actual = _can_manager.read_signal(sig)
                if actual is None:
                    return False
                actual = float(actual)
                return ((op == "==" and actual == target) or
                        (op == "!=" and actual != target) or
                        (op == ">"  and actual >  target) or
                        (op == ">=" and actual >= target) or
                        (op == "<"  and actual <  target) or
                        (op == "<=" and actual <= target))
            except Exception:
                return False

    def _check_lap(self, curr: dict, changed: dict, reason: str):
        """
        랩 조건 평가 — 처음 충족 시 1회만 기록.
        조건이 해제됐다가 다시 충족되면 다시 기록.
        """
        if not self._running:
            return
        cond_now = self._eval_trigger("lap", curr, changed)

        if cond_now and not self._lap_cond_active:
            # 새로 충족 → 랩 기록
            self._lap_cond_active = True
            self._record_lap(reason)
        elif not cond_now and self._lap_cond_active:
            # 조건 해제 → 다음 충족 시 다시 기록 가능
            self._lap_cond_active = False

    # ── UI 빌드 ──────────────────────────────────────────────────────────
    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0, 0, 0, 4); v.setSpacing(8)

        # 타이머 표시
        disp_grp = QGroupBox("⏱ 경과 시간")
        dl = QVBoxLayout(disp_grp)
        self._disp_lbl = QLabel("00:00:00.000")
        self._disp_lbl.setAlignment(Qt.AlignCenter)
        self._disp_lbl.setStyleSheet(
            "font-size:28px;font-weight:bold;color:#00ff88;"
            "font-family:monospace;letter-spacing:2px;")
        dl.addWidget(self._disp_lbl)
        v.addWidget(disp_grp)

        # 전원 조건 감시 토글
        self._watch_toggle = QPushButton("📡 전원 조건 감시: OFF")
        self._watch_toggle.setCheckable(True)
        self._watch_toggle.setMinimumHeight(32)
        self._watch_toggle.setStyleSheet(
            "QPushButton{background:#1a2a1a;color:#8bc;"
            "border:1px solid #2a4a2a;border-radius:4px;"
            "font-size:11px;font-weight:bold;}"
            "QPushButton:checked{background:#0a3a1a;color:#2ecc71;"
            "border:1px solid #1a6a3a;}")
        self._watch_toggle.toggled.connect(self._on_watch_toggled)
        v.addWidget(self._watch_toggle)

        # 시작/종료/랩 탭
        cond_tabs = QTabWidget()
        cond_tabs.setStyleSheet(
            "QTabBar::tab{padding:4px 12px;font-size:10px;}"
            "QTabBar::tab:selected{color:#2ecc71;font-weight:bold;}")
        for kind, label, color in [
                ("start", "▶ 시작 조건", "#2ecc71"),
                ("stop",  "⏹ 종료 조건", "#e74c3c"),
                ("lap",   "🏁 랩 조건",  "#9b59b6")]:
            cond_tabs.addTab(self._build_trigger_tab(kind, color), label)
        v.addWidget(cond_tabs)

        # 수동 버튼
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        self._manual_start_btn = QPushButton("▶ 수동 시작")
        self._manual_start_btn.setMinimumHeight(34)
        self._manual_start_btn.setStyleSheet(
            "QPushButton{background:#0a3a2a;color:#1abc9c;"
            "border:1px solid #1a6a4a;border-radius:5px;"
            "font-size:12px;font-weight:bold;}"
            "QPushButton:hover{background:#0d4a38;}"
            "QPushButton:disabled{background:#1a1a2a;color:#556;"
            "border-color:#2a2a3a;}")
        self._manual_start_btn.clicked.connect(self._manual_start)

        self._manual_stop_btn = QPushButton("⏹ 수동 종료")
        self._manual_stop_btn.setMinimumHeight(34)
        self._manual_stop_btn.setEnabled(False)
        self._manual_stop_btn.setStyleSheet(
            "QPushButton{background:#3a1a0a;color:#e67e22;"
            "border:1px solid #6a3a1a;border-radius:5px;"
            "font-size:12px;font-weight:bold;}"
            "QPushButton:hover{background:#4a2a0d;}"
            "QPushButton:disabled{background:#1a1a2a;color:#556;"
            "border-color:#2a2a3a;}")
        self._manual_stop_btn.clicked.connect(self._manual_stop)

        lap_btn = QPushButton("🏁 랩 기록")
        lap_btn.setMinimumHeight(34)
        lap_btn.setStyleSheet(
            "QPushButton{background:#1a1a3a;color:#9b59b6;"
            "border:1px solid #4a3a6a;border-radius:5px;"
            "font-size:11px;}"
            "QPushButton:hover{background:#22224a;}")
        lap_btn.clicked.connect(lambda: self._record_lap("수동"))

        reset_btn = QPushButton("↺ 리셋")
        reset_btn.setMinimumHeight(34)
        reset_btn.setStyleSheet(
            "QPushButton{background:#1a1a2a;color:#888;"
            "border:1px solid #2a2a3a;border-radius:5px;"
            "font-size:11px;}"
            "QPushButton:hover{background:#22222a;}")
        reset_btn.clicked.connect(self._reset)

        btn_row.addWidget(self._manual_start_btn)
        btn_row.addWidget(self._manual_stop_btn)
        btn_row.addWidget(lap_btn)
        btn_row.addWidget(reset_btn)
        v.addLayout(btn_row)

        # 랩 기록 표시
        lap_grp = QGroupBox("🏁 랩 기록")
        lapl = QVBoxLayout(lap_grp)
        self._lap_txt = QPlainTextEdit()
        self._lap_txt.setReadOnly(True)
        self._lap_txt.setMaximumBlockCount(500)  # ★ 메모리 누수 방지
        self._lap_txt.setFixedHeight(80)
        self._lap_txt.setStyleSheet(
            "background:#0a0a14;color:#9b59b6;"
            "font-family:monospace;font-size:10px;")
        lapl.addWidget(self._lap_txt)
        v.addWidget(lap_grp)

    def _build_trigger_tab(self, kind: str, color: str) -> QWidget:
        """시작/종료/랩 조건 탭 — AND 그룹 + OR 그룹."""
        w = QWidget()
        sv = QScrollArea(); sv.setWidgetResizable(True); sv.setFrameShape(0)
        sv.setMinimumHeight(150)   # ★ 최소 높이 보장 (드래그로 섹션 자체를 키우면 더 커짐)
        inner = QWidget(); vl = QVBoxLayout(inner); vl.setSpacing(6)
        sv.setWidget(inner)
        wl = QVBoxLayout(w); wl.setContentsMargins(0,0,0,0)
        wl.addWidget(sv)

        ss_base = (f"QGroupBox{{font-size:11px;color:{color};"
                   f"border:1px solid #2a3a2a;border-radius:5px;margin-top:6px;}}"
                   f"QGroupBox::title{{subcontrol-origin:margin;left:8px;padding:0 4px;}}")

        for group, grp_label in [("and", "✅ AND 조건 (모두 충족)"),
                                   ("or",  "🔀 OR  조건 (하나 이상)")]:
            grp = QGroupBox(grp_label)
            grp.setStyleSheet(ss_base)
            gv = QVBoxLayout(grp); gv.setSpacing(3)

            rows_w = QWidget(); rows_v = QVBoxLayout(rows_w)
            rows_v.setSpacing(2); rows_v.setContentsMargins(0,0,0,0)
            setattr(self, f'_{kind}_{group}_rows', rows_w)

            add_btn = QPushButton(f"+ 조건 추가")
            add_btn.setFixedHeight(22)
            add_btn.setStyleSheet(
                f"QPushButton{{background:#0a140a;color:{color};"
                f"border:1px solid #1a3a1a;border-radius:3px;font-size:10px;}}")
            add_btn.clicked.connect(
                lambda _, k=kind, g=group, rv=rows_v: self._add_cond_row(k, g, rv))

            gv.addWidget(rows_w)
            gv.addWidget(add_btn)
            vl.addWidget(grp)

        vl.addStretch()
        return w

    def _add_cond_row(self, kind: str, group: str, container_layout: QVBoxLayout,
                      restore: dict = None):
        """
        조건 행 하나 추가.
        restore: DB 복원 시 {type, ch/sig, on/op/val} dict
        """
        sig_names = sorted(set(
            s['name'] for s in (getattr(_can_manager, '_all_sigs_ref', None) or [])
            if 'name' in s))

        row_w = QWidget(); row_w.setObjectName("_cond_row")
        rl = QHBoxLayout(row_w); rl.setContentsMargins(0,0,0,0); rl.setSpacing(4)

        # 타입 선택 (전원/CAN)
        type_cb = QComboBox(); type_cb.setObjectName("_type_cb")
        type_cb.addItems(["전원", "CAN"])
        type_cb.setFixedWidth(46)
        type_cb.setStyleSheet(
            "QComboBox{background:#0d1a0d;color:#adf;"
            "border:1px solid #2a4a2a;border-radius:3px;"
            "font-size:10px;padding:1px 2px;}")
        rl.addWidget(type_cb)

        # 전원 채널 선택
        ch_cb = QComboBox(); ch_cb.setObjectName("_ch_cb")
        for ch, label in self._CHANNELS:
            ch_cb.addItem(label, userData=ch)
        ch_cb.setFixedWidth(72)
        ch_cb.setStyleSheet(
            "QComboBox{background:#0d1a0d;color:#cde;"
            "border:1px solid #2a4a2a;border-radius:3px;"
            "font-size:10px;padding:1px 2px;}")
        rl.addWidget(ch_cb)

        on_rb  = QRadioButton("ON"); on_rb.setObjectName("_on_rb")
        off_rb = QRadioButton("OFF")
        on_rb.setChecked(True)
        on_rb.setStyleSheet("font-size:10px;")
        off_rb.setStyleSheet("font-size:10px;")
        rbg = QButtonGroup(row_w); rbg.addButton(on_rb); rbg.addButton(off_rb)
        rl.addWidget(on_rb); rl.addWidget(off_rb)

        # CAN 신호 선택 (초기 숨김)
        sig_cb = QComboBox(); sig_cb.setObjectName("_sig_cb")
        sig_cb.setEditable(True)
        sig_cb.setInsertPolicy(QComboBox.NoInsert)
        sig_cb.addItems(sig_names)
        sig_cb.setCurrentText("")
        sig_cb.lineEdit().setPlaceholderText("신호명")
        sig_cb.setStyleSheet(
            "QComboBox{background:#0d1a2a;color:#adf;"
            "border:1px solid #1a4a7a;border-radius:3px;"
            "font-size:10px;padding:1px 3px;}"
            "QComboBox QAbstractItemView{background:#0d1a2a;color:#adf;"
            "selection-background-color:#1a3a6a;font-size:10px;}")
        sig_cb.setVisible(False); sig_cb.setFixedWidth(130)
        rl.addWidget(sig_cb)

        op_cb = QComboBox(); op_cb.setObjectName("_op_cb")
        op_cb.addItems(self._CAN_OPS)
        op_cb.setFixedWidth(46)
        op_cb.setStyleSheet(
            "QComboBox{background:#0d1a2a;color:#adf;"
            "border:1px solid #1a4a7a;border-radius:3px;"
            "font-size:10px;}")
        op_cb.setVisible(False)
        rl.addWidget(op_cb)

        val_le = QLineEdit(); val_le.setObjectName("_val_le")
        val_le.setPlaceholderText("값")
        val_le.setFixedWidth(52)
        val_le.setStyleSheet(
            "QLineEdit{background:#0d1a2a;color:#adf;"
            "border:1px solid #1a4a7a;border-radius:3px;"
            "padding:1px 3px;font-size:10px;}")
        val_le.setVisible(False)
        rl.addWidget(val_le)

        del_btn = QPushButton("✕")
        del_btn.setFixedSize(20, 20)
        del_btn.setStyleSheet(
            "QPushButton{background:#2a0a0a;color:#e74c3c;"
            "border:1px solid #5a1a1a;border-radius:3px;font-size:10px;}")
        del_btn.clicked.connect(lambda: (
            row_w.deleteLater(),
            self._invalidate_cond_cache(),   # ★ [문제1] 삭제 시 캐시 무효화
            self._schedule_db_save()))
        rl.addWidget(del_btn)

        def _on_type_changed(t):
            is_pwr = (t == "전원")
            ch_cb.setVisible(is_pwr); on_rb.setVisible(is_pwr)
            off_rb.setVisible(is_pwr)
            sig_cb.setVisible(not is_pwr); op_cb.setVisible(not is_pwr)
            val_le.setVisible(not is_pwr)
            self._schedule_db_save()

        type_cb.currentTextChanged.connect(_on_type_changed)
        ch_cb.currentIndexChanged.connect(self._schedule_db_save)
        on_rb.toggled.connect(self._schedule_db_save)
        sig_cb.currentTextChanged.connect(self._schedule_db_save)
        op_cb.currentIndexChanged.connect(self._schedule_db_save)
        val_le.textChanged.connect(self._schedule_db_save)

        container_layout.addWidget(row_w)

        # DB 복원
        if restore:
            rtype = restore.get("type", "전원")
            type_cb.setCurrentText(rtype)
            _on_type_changed(rtype)
            if rtype == "전원":
                ch = restore.get("ch", "")
                idx = ch_cb.findData(ch)
                if idx >= 0: ch_cb.setCurrentIndex(idx)
                if not restore.get("on", True): off_rb.setChecked(True)
            else:
                sig_cb.setCurrentText(restore.get("sig", ""))
                idx = op_cb.findText(restore.get("op", "=="))
                if idx >= 0: op_cb.setCurrentIndex(idx)
                val_le.setText(restore.get("val", ""))
        else:
            _on_type_changed("전원")

        self._invalidate_cond_cache()   # ★ [문제1] 행 추가 시 캐시 무효화
        self._schedule_db_save()
    def set_can_available(self, available: bool):
        """CAN 연결/해제 시 호출 — 신호 목록 갱신 및 CAN 폴링 타이머 제어."""
        if available:
            sig_names = sorted(set(
                s['name'] for s in (getattr(_can_manager, '_all_sigs_ref', None) or [])
                if 'name' in s))
            for kind in ("start", "stop", "lap"):
                for group in ("and", "or"):
                    rows_w = self._get_rows(kind, group)
                    if not rows_w: continue
                    for row in rows_w.findChildren(QWidget, "_cond_row"):
                        sc = row.findChild(QComboBox, "_sig_cb")
                        if sc:
                            cur = sc.currentText()
                            sc.blockSignals(True)
                            sc.clear(); sc.addItems(sig_names)
                            sc.setCurrentText(cur)
                            sc.blockSignals(False)
            self._can_poll_timer.start()
        else:
            self._can_poll_timer.stop()

    # 호환 별칭
    @property
    def _can_start_grp(self): return None
    @property
    def _can_stop_grp(self): return None

    # ── DB 동기화 ─────────────────────────────────────────────────────
    def _schedule_db_save(self, *_):
        if hasattr(self, '_db_timer'):
            self._db_timer.start()

    def _flush_db(self):
        db = self._db or getattr(self._engine, '_db', None)
        if not db: return
        try:
            import json as _j
            for kind in ("start", "stop", "lap"):
                for group in ("and", "or"):
                    rows_w = self._get_rows(kind, group)
                    conds = []
                    if rows_w:
                        for row in rows_w.findChildren(QWidget, "_cond_row"):
                            tc = row.findChild(QComboBox, "_type_cb")
                            if not tc: continue
                            t = tc.currentText()
                            if t == "전원":
                                cc = row.findChild(QComboBox, "_ch_cb")
                                ob = row.findChild(QRadioButton, "_on_rb")
                                if cc:
                                    conds.append({
                                        "type": "전원",
                                        "ch":   cc.currentData() or cc.currentText(),
                                        "on":   ob.isChecked() if ob else True})
                            else:
                                sc = row.findChild(QComboBox, "_sig_cb")
                                oc = row.findChild(QComboBox, "_op_cb")
                                vl = row.findChild(QLineEdit, "_val_le")
                                if sc and sc.currentText().strip():
                                    conds.append({
                                        "type": "CAN",
                                        "sig":  sc.currentText().strip(),
                                        "op":   oc.currentText() if oc else "==",
                                        "val":  vl.text().strip() if vl else ""})
                    db.set(f"timer_{kind}_{group}_conds",
                           _j.dumps(conds, ensure_ascii=False))
            db.set("timer_watch_on", "1" if self._cond_watch_enabled else "0")
        except Exception:
            pass

    def _load_db(self):
        db = self._db or getattr(self._engine, '_db', None)
        if not db: return
        try:
            import json as _j
            for kind in ("start", "stop", "lap"):
                for group in ("and", "or"):
                    key  = f"timer_{kind}_{group}_conds"
                    raw  = db.get(key, "[]")
                    rows_w = self._get_rows(kind, group)
                    if not rows_w: continue
                    layout = rows_w.layout()
                    if not layout: continue
                    try:
                        conds = _j.loads(raw)
                    except Exception:
                        conds = []
                    for c in conds:
                        self._add_cond_row(kind, group, layout, restore=c)
            watch = db.get("timer_watch_on", "0")
            if watch == "1":
                self._watch_toggle.setChecked(True)
        except Exception:
            pass

    # ── 타이머 제어 ──────────────────────────────────────────────────
    def _start(self, reason: str = "수동"):
        if self._running: return
        import time as _t
        self._running  = True
        self._start_ts = _t.time() - self._elapsed
        self._lap_cond_active = False  # 랩 조건 상태 리셋
        self._manual_start_btn.setEnabled(False)
        self._manual_stop_btn.setEnabled(True)
        self._signals.status_message.emit(f"[타이머] ▶ 시작 — {reason}")

    def _stop(self, reason: str = "수동"):
        if not self._running: return
        import time as _t
        self._elapsed = _t.time() - self._start_ts
        self._running = False
        self._lap_cond_active = False
        self._manual_start_btn.setEnabled(True)
        self._manual_stop_btn.setEnabled(False)
        self._signals.status_message.emit(
            f"[타이머] ⏹ 종료 — {reason}  경과: {self._fmt(self._elapsed)}")

    def _manual_start(self): self._start("수동")
    def _manual_stop(self):  self._stop("수동")

    def _reset(self):
        self._stop("리셋")
        self._elapsed = 0.0
        self._laps.clear()
        self._lap_cond_active = False
        self._disp_lbl.setText("00:00:00.000")
        self._lap_txt.clear()

    def _record_lap(self, reason: str = "수동") -> float:
        import time as _t
        lap_t = (_t.time() - self._start_ts) if self._running else self._elapsed
        desc  = f"랩 {len(self._laps)+1}"
        self._laps.append((lap_t, desc))
        self._lap_txt.appendPlainText(f"{desc}: {self._fmt(lap_t)}  [{reason}]")
        return lap_t

    @staticmethod
    def _fmt(secs: float) -> str:
        s  = int(secs); ms = int((secs - s) * 1000)
        return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}.{ms:03d}"

    def _update_display(self):
        import time as _t
        elapsed = (_t.time() - self._start_ts) if self._running else self._elapsed
        self._disp_lbl.setText(self._fmt(elapsed))
        try:
            self._engine._timer_state_cache = {
                "running": self._running, "elapsed": elapsed,
                "elapsed_str": self._fmt(elapsed), "lap_count": len(self._laps),
            }
        except Exception:
            pass

    def _on_watch_toggled(self, on: bool):
        self._cond_watch_enabled = on
        self._watch_toggle.setText(f"📡 전원 조건 감시: {'ON ✅' if on else 'OFF'}")
        # [버그 수정] 전원 조건 감시 ON 시 _can_poll_timer도 함께 시작.
        # 기존에는 CAN 연결(set_can_available)될 때만 타이머가 시작되어,
        # CAN 없이 전원 조건만 쓰는 경우 150ms 주기 평가가 전혀 동작 안 하는 버그 수정.
        # OFF 시에는 CAN 연결 여부와 무관하게 타이머 중지.
        if on:
            self._can_poll_timer.start()
        else:
            self._can_poll_timer.stop()
        self._schedule_db_save()

    def _poll(self): self._update_display()

    # ── 커널 API ─────────────────────────────────────────────────────
    @property
    def elapsed(self) -> float:
        import time as _t
        return (_t.time() - self._start_ts) if self._running else self._elapsed

    @property
    def running(self) -> bool:
        return self._running

    def start(self):
        """커널 API: timer.start()"""
        self._start("커널")

    def stop(self):
        """커널 API: timer.stop()"""
        self._stop("커널")

    def lap(self) -> float:
        """커널 API: timer.lap() → 경과시간(초) 반환"""
        return self._record_lap("커널")

    def reset(self):
        """커널 API: timer.reset()"""
        self._reset()



