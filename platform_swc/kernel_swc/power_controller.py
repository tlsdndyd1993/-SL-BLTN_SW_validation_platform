# -*- coding: utf-8 -*-
"""
kernel_swc/power_controller.py
Arduino 기반 전원 제어기 (ACC/IGN/TG/PLBM).

설계 원칙
 · §2.4 스레드 안전: _rx_loop는 백그라운드 스레드, 콜백은 시그널로 메인 전달
 · §1.8 물리적 예외 복구: USB 단선 시 재연결 retry 포함
"""
import os, sys, time, threading
from typing import Optional

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

class PowerController:
    """
    Arduino 시리얼 전원 제어기 v3 (핀맵 B v4.0 대응).
    KernelEngine과 PowerControlPanel이 같은 인스턴스를 공유한다.

    채널 명령 표 (v4.0):
      채널             ON      OFF    설명
      B+ (Battery)     "1"     "Q"
      TG B+            "2"     "W"
      ACC              "3"     "E"
      IGN              "4"     "R"
      PLBM 실물        "5"     "T"    D5 스위치 / D12 릴레이 (상호 배타)
      PLBM 모사        "6"     "Y"    D8 스위치 / D13 릴레이 (상호 배타)
      ALL OFF          "0"            전체 OFF

    ※ OHCL 시간제한 기능은 v4.0에서 삭제됨 (D13 → PLBM 모사 릴레이로 변경)

    Arduino → PC 프로토콜:
      "S<HEX2>\\n"  상태 비트맵
        bit7:B+  bit6:TG  bit5:ACC  bit4:IGN
        bit3:PLBM_REAL  bit2:PLBM_SIM  bit1:LED_ON  bit0:rsvd
      "L1\\n" / "L0\\n"  LED PWM 상태
      "SW<ch>\\n"  택트스위치 이벤트 (ch=1~6) — Python에서 Z 쿼리로 상태 확인
    """

    CHANNELS: dict = {
        "bplus":      ("1", "Q"),
        "tg_bplus":   ("2", "W"),
        "acc":        ("3", "E"),
        "ign":        ("4", "R"),
        # [수정3] PLBM 실물(D5/D12) / PLBM 모사(D8/D13) 분리
        # 두 채널은 상호 배타: 한쪽 ON → 반대쪽 자동 OFF
        "plbm_real":  ("5", "T"),   # D5 스위치/D12 릴레이 — PLBM 실물
        "plbm_sim":   ("6", "Y"),   # D8 스위치/D13 릴레이 — PLBM 모사
    }
    # PLBM 상호 배타 쌍
    _PLBM_MUTEX: dict = {"plbm_real": "plbm_sim", "plbm_sim": "plbm_real"}
    ALL_OFF_CMD = "0"

    def __init__(self):
        self._ser     = None
        self._port    = ""
        self._lock    = threading.Lock()
        self._log_cb  = None
        # ★ None = 아직 Arduino로부터 수신 안 됨 (초기화 전)
        # False = 수신됐고 OFF 상태, True = 수신됐고 ON 상태
        self._ch_state: dict = {k: None for k in list(self.CHANNELS)}
        self._state_change_cbs: list = []
        self._rx_thread  = None
        self._rx_running = False
        # TX 큐 — _rx_loop에서 send()를 직접 호출하지 않고 여기에 쌓음
        self._tx_queue: "deque" = deque(maxlen=32)
        # [스레드 안전] 로그 큐 — _rx_loop(백그라운드 스레드)가 쌓고
        # UI 스레드(_poll_rx 타이머)가 꺼내서 표시. UI 직접 접근 금지.
        self._log_queue: "deque" = deque(maxlen=200)
        # OHCL / LED PWM 상태 — BLTNStatusPanel에서 콜백 방식으로 취득
        self._led_pwm_on: bool        = False
        self._led_half_period_ms: int = 0
        self._ohcl_last_event: dict   = {}
        self._ohcl_cbs: list          = []
        # ★ 연결 후 첫 번째 S<HEX> 수신 시 강제 전체 반영 플래그
        self._state_synced: bool      = False

    def register_state_callback(self, cb):
        if cb not in self._state_change_cbs:
            self._state_change_cbs.append(cb)

    def register_ohcl_callback(self, cb):
        """OHCL/LED 이벤트 콜백 로듬.
        cb(event_type: str, flag: bool, value)
          event_type="led_pwm"   flag=True/False(ON/OFF)  value=None
          event_type="led_period" flag=True                value=half_ms(int)
          event_type="ohcl_event" flag=True(더블)/False(싱글)  value=relay_ms(int)
        """
        if cb not in self._ohcl_cbs:
            self._ohcl_cbs.append(cb)

    def _fire_state_change(self, ch: str, is_on: bool):
        for cb in self._state_change_cbs:
            try: cb(ch, is_on)
            except Exception: pass

    def connect(self, port: str, baudrate: int = 9600) -> bool:
        try:
            import serial as _serial
        except ImportError:
            self._log("❌ pyserial 미설치"); return False
        with self._lock:
            if self._ser and self._ser.is_open:
                self._ser.close()
            try:
                self._ser          = _serial.Serial()
                self._ser.port     = port
                self._ser.baudrate = baudrate
                self._ser.timeout  = 0.1
                # ★ [문제2 수정] DTR/RTS False 설정 후 open — Arduino 리셋 억제
                # Windows pyserial은 open() 직후 DTR을 잠깐 HIGH 할 수 있으므로
                # open() 후에도 즉시 False 강제.
                self._ser.dtr      = False
                self._ser.rts      = False
                self._ser.open()
                try:
                    self._ser.dtr = False
                    self._ser.rts = False
                except Exception:
                    pass
                # ★ [문제2 핵심 수정] reset_input_buffer() 제거
                # 기존: open() 직후 reset_input_buffer() → Arduino 부팅 시 전송한
                #        초기 상태 패킷 3개(uart_send_state × 3)를 모두 지워버림
                #        → _rx_loop 스레드가 올라와도 읽을 데이터가 없어
                #          _state_synced=False 상태가 유지되고 이후 Z 쿼리 응답
                #          타이밍에 따라 간헐적으로 수신 실패.
                # 수정: 버퍼를 지우지 않고 그대로 남겨둠.
                #        _rx_loop 스레드가 올라오면 즉시 버퍼를 읽어
                #        초기 상태 패킷을 처리할 수 있음.
                # ★ output 버퍼만 초기화 (이전 연결에서 미전송된 TX 명령 제거)
                try:
                    self._ser.reset_output_buffer()
                except Exception:
                    pass
                self._port = port
                self._log(f"[Power] 연결: {port} @ {baudrate}bps")
            except Exception as ex:
                self._log(f"[Power] 연결 실패: {ex}"); return False

        # ── _rx_loop 스레드 먼저 시작 ────────────────────────────────────
        # 반드시 스레드를 먼저 올린 뒤 Z 쿼리 전송해야
        # 부팅 응답 패킷을 스레드가 수신할 수 있음
        self._rx_running = True
        self._rx_thread  = threading.Thread(
            target=self._rx_loop, daemon=True, name="PowerRX")
        self._rx_thread.start()

        # ── 초기 상태 쿼리를 백그라운드 스레드에서 실행 ─────────────────
        # UI 스레드 블로킹 방지: connect()는 즉시 True 반환
        # 백그라운드에서 Z 쿼리 재시도 → _ch_state 업데이트 → UI 자동 반영
        def _initial_query():
            import time as _t
            # ★ [문제2 수정] _rx_loop 스레드 완전 기동 대기 (50ms)
            # 스레드 시작 직후 in_waiting 체크 전에 OS 스케줄러에 의해
            # 스레드가 실제 실행되지 않을 수 있음 → 짧은 대기로 안정화.
            _t.sleep(0.05)

            # ★ [문제2 수정] 입력 버퍼에 이미 데이터가 있으면 Z 쿼리 생략
            # Arduino 부팅 패킷이 버퍼에 남아있다면 _rx_loop가 처리함.
            # 데이터가 없는 경우에만 Z 쿼리로 상태 요청.
            has_initial_data = False
            with self._lock:
                try:
                    if self._ser and self._ser.is_open:
                        has_initial_data = self._ser.in_waiting > 0
                except Exception:
                    pass

            if not has_initial_data:
                # 버퍼 비어있음 → Arduino가 이미 부팅 완료(warm connect) 또는
                # DTR 리셋 후 부팅 중. 0.3초 후 Z 쿼리.
                _t.sleep(0.3)
                self.send("Z")
                _t.sleep(0.2)
            else:
                self._log("[Power] 부팅 패킷 버퍼 감지 — Z 쿼리 생략, 패킷 처리 대기")
                _t.sleep(0.3)

            # _ch_state가 여전히 모두 None/False이면 Arduino가 리셋된 것
            # → 부팅 완료까지 추가 대기 후 재쿼리
            _all_none = all(v is None for v in self._ch_state.values())
            if _all_none:
                self._log("[Power] 초기 응답 없음 — Arduino 부팅 대기 중 (최대 2초)...")
                _t.sleep(1.5)
                self.send("Z")
                _t.sleep(0.2)
                self.send("Z")  # 재전송 (누락 방어)
                _t.sleep(0.15)
            # ★ 추가: 연결 후 5초간 1초마다 Z 재쿼리
            # 간헐적 패킷 소실 방어 — _state_synced=True가 되면 중단
            for _ in range(5):
                if self._state_synced:
                    break
                _t.sleep(1.0)
                self.send("Z")
                _t.sleep(0.1)
            self._log("[Power] 초기 상태 쿼리 완료")

        threading.Thread(
            target=_initial_query, daemon=True,
            name="PowerInitQuery").start()

        return True

    def disconnect(self):
        self._rx_running = False
        with self._lock:
            if self._ser and self._ser.is_open:
                self._ser.close()
            self._ser = None; self._port = ""
        self._state_synced = False   # ★ 재연결 시 강제 반영 다시 활성화
        self._log("[Power] 연결 해제")

    @property
    def is_connected(self) -> bool:
        return bool(self._ser and self._ser.is_open)

    @property
    def port(self) -> str:
        return self._port

    def _rx_loop(self):
        """Arduino → PC 상태 보고 수신 스레드.

        [문제1 수정] blocking read(1) 제거 → in_waiting==0이면 sleep 후 continue.
          기존 코드는 in_waiting==0일 때 blocking read(1, timeout=0.1s)를 호출해
          타임아웃마다 시리얼 버스에 wake를 줘서 Arduino RX LED를 불필요하게 점등시킴.

        [문제4 수정] SW 수신 후 send("Z")를 _tx_queue를 통해 비동기 전송.
          _rx_loop 내에서 send()를 직접 호출하면 self._lock 경합 가능성 있음.
          _tx_queue(deque)에 넣어 두면, 루프 상단에서 lock 없이 꺼내 안전하게 전송.

        수신 패킷 형식:
          "S<HEX2>\\n"  — 릴레이 상태 비트맵 (항상 처리)
          "L1\\n"/"L0\\n" — LED PWM ON/OFF
          "SW<ch>\\n"   — 택트스위치 이벤트 (ch='1'~'8')
          "L1\\n"/"L0\\n" — BLTN LED PWM ON/OFF
          "P<ms>\\n"    — BLTN LED 반주기 (ms, 10진수)
          "SW<ch>\\n"   — 택트스위치 이벤트 (ch='1'~'8')
          "M\\n"        — OHCL 더블클릭 (A1 릴레이 1350ms CLOSE 완료)
          "N\\n"        — OHCL 싱글클릭 (A1 릴레이 1250ms CLOSE 완료)
        """
        import time as _t
        buf = ""
        while self._rx_running:
            try:
                # ── TX 큐 처리 (send("Z") 등 비동기 전송) ────────────────
                while self._tx_queue:
                    cmd = self._tx_queue.popleft()
                    self.send(cmd)

                # ── 시리얼 포트 참조 획득 (짧은 락) ─────────────────────
                with self._lock:
                    ser = self._ser
                if not (ser and ser.is_open):
                    _t.sleep(0.05); continue

                # ── [문제1 수정] in_waiting==0이면 skip → blocking read 없음 ──
                waiting = ser.in_waiting
                if not waiting:
                    _t.sleep(0.02)   # 20ms 슬립 — CPU 절약, LED 불자극
                    continue

                raw = ser.read(waiting)
                if not raw:
                    continue
                buf += raw.decode(errors="replace")

                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    # ── SW<ch> : 택트스위치 이벤트 (반드시 S<HEX> 보다 먼저!) ──
                    if line.startswith("SW") and len(line) == 3:
                        ch_idx = line[2]  # '1'~'6'
                        self._log(f"[Power←SW] 스위치 이벤트 ch={ch_idx}")
                        self._tx_queue.append("Z")

                    # ── S<HEX2> : 릴레이 상태 비트맵 ────────────────────────
                    # ※ SW 체크 이후에 위치해야 "SW1" 등을 S-packet으로 오파싱 안 함
                    elif line.startswith("S") and len(line) == 3:
                        try:
                            self._parse_state_byte(int(line[1:], 16))
                        except ValueError:
                            pass

                    # —— L1 / L0 : BLTN LED PWM ON/OFF ——————————————
                    elif line in ("L1", "L0"):
                        is_on = (line == "L1")
                        self._log(f"[Power←LED] LED PWM {'ON' if is_on else 'OFF'}")
                        self._led_pwm_on = is_on
                        for cb in list(self._ohcl_cbs):
                            try: cb("led_pwm", is_on, None)
                            except Exception: pass

                    # —— P<ms> : LED 반주기 (ms) —————————————————
                    elif line.startswith("P") and len(line) >= 2:
                        try:
                            half_ms = int(line[1:])
                            self._led_half_period_ms = half_ms
                            self._log(f"[Power←LED] 반주기={half_ms}ms")
                            for cb in list(self._ohcl_cbs):
                                try: cb("led_period", True, half_ms)
                                except Exception: pass
                        except ValueError:
                            pass

                    # —— M / N : OHCL 스위치 이벤트 ————————————
                    elif line in ("M", "N"):
                        is_double = (line == "M")
                        relay_ms  = 1350 if is_double else 1250
                        label = "더블클릭" if is_double else "싱글클릭"
                        self._log(f"[Power←OHCL] {label} 릴레이 {relay_ms}ms CLOSE 완료")
                        self._ohcl_last_event = {"type": line, "relay_ms": relay_ms}
                        for cb in list(self._ohcl_cbs):
                            try: cb("ohcl_event", is_double, relay_ms)
                            except Exception: pass

                    # —— K<ms> : PC 트리거(A<ms>) 완료 보고 ——————————————
                    elif line.startswith("K") and len(line) >= 2:
                        try:
                            relay_ms = int(line[1:])
                            rec_type = "TML" if relay_ms >= 1300 else "MAR"
                            self._log(
                                f"[Power←OHCL] PC트리거 {relay_ms}ms CLOSE 완료 → {rec_type}")
                            self._ohcl_last_event = {"type": "K", "relay_ms": relay_ms}
                            for cb in list(self._ohcl_cbs):
                                try: cb("ohcl_event", relay_ms >= 1300, relay_ms)
                                except Exception: pass
                        except ValueError:
                            pass

            except Exception:
                _t.sleep(0.05)

    def _parse_state_byte(self, val: int):
        """Arduino 상태 바이트를 _ch_state에 반영.

        Arduino g_state 비트맵 (v4.0 핀맵 B):
          bit7:B+  bit6:TG  bit5:ACC  bit4:IGN
          bit3:PLBM_REAL(D12)  bit2:PLBM_SIM(D13)  bit1:LED  bit0:rsvd

        ★ _state_synced=False (첫 수신)이면 변화 여부 무관하게 전채널 강제 반영.
        ★ Arduino가 2초마다 주기 보고하므로 연결 후 최대 2초 내 반영 보장.
        """
        mapping = [
            ("bplus",     7), ("tg_bplus", 6), ("acc",      5), ("ign",  4),
            ("plbm_real", 3), ("plbm_sim", 2),
        ]
        force = not self._state_synced   # 첫 수신이면 강제 반영
        self._state_synced = True

        for ch, bit in mapping:
            new_st = bool(val & (1 << bit))
            prev   = self._ch_state.get(ch, None)   # None = 아직 한 번도 설정 안 됨
            # ★ 조건: 강제 반영 OR 실제 변화 OR 아직 한 번도 설정 안 된 채널
            if force or prev is None or new_st != prev:
                self._ch_state[ch] = new_st
                self._fire_state_change(ch, new_st)
                tag = "[초기화]" if force else "[변화]"
                self._log(
                    f"[Power←Arduino]{tag} {ch.upper()} "
                    f"{'ON' if new_st else 'OFF'}")

    def send(self, cmd: str) -> bool:
        with self._lock:
            if not (self._ser and self._ser.is_open):
                self._log("[Power] ❌ 미연결"); return False
            try:
                self._ser.write(cmd.encode())
                self._log(f"[Power TX] {cmd!r}"); return True
            except Exception as ex:
                self._log(f"[Power TX ERR] {ex}"); return False

    def channel_on(self, channel: str) -> bool:
        ch = channel.lower().strip()
        if ch not in self.CHANNELS:
            self._log(f"[Power] ❌ 알 수 없는 채널: {channel!r}"); return False
        # [수정3] PLBM 상호 배타: 반대편 채널 먼저 OFF
        mutex_ch = self._PLBM_MUTEX.get(ch)
        if mutex_ch and self._ch_state.get(mutex_ch, False):
            off_cmd = self.CHANNELS.get(mutex_ch, (None, None))[1]
            if off_cmd:
                self.send(off_cmd)
                self._ch_state[mutex_ch] = False
                self._fire_state_change(mutex_ch, False)
                self._log(f"[Power] PLBM 배타: {mutex_ch.upper()} 자동 OFF")
                import time as _t; _t.sleep(0.05)
        result = self.send(self.CHANNELS[ch][0])
        if result:
            self._ch_state[ch] = True
            self._fire_state_change(ch, True)
        return result

    def channel_off(self, channel: str) -> bool:
        ch = channel.lower().strip()
        if ch not in self.CHANNELS:
            self._log(f"[Power] ❌ 알 수 없는 채널: {channel!r}"); return False
        result = self.send(self.CHANNELS[ch][1])
        if result:
            self._ch_state[ch] = False
            self._fire_state_change(ch, False)
        return result

    def is_on(self, channel: str) -> bool:
        return bool(self._ch_state.get(channel.lower().strip(), False))

    def all_off(self) -> bool:
        self._log("[Power] ⚠ ALL OFF")
        result = self.send(self.ALL_OFF_CMD)
        if result:
            for ch in ["bplus", "tg_bplus", "acc", "ign", "plbm_real", "plbm_sim"]:
                if self._ch_state.get(ch):
                    self._ch_state[ch] = False
                    self._fire_state_change(ch, False)
        return result

    def query_state(self) -> bool:
        """Arduino에 상태 쿼리."""
        return self.send("Z")

    def ohcl_trigger(self, relay_ms: int) -> bool:
        """PC에서 OHCL A1 릴레이를 직접 트리거 (v10.1).

        Arduino에 "A<ms>\\n" 명령 전송 → 아두이노가 해당 ms만큼 A1 CLOSE 후
        "K<ms>\\n"으로 완료 보고. 범위: 100~9999ms.
        relay_ms >= 1300 → TML 목표, < 1300 → MAR 목표.
        """
        ms = int(relay_ms)
        if ms < 100 or ms > 9999:
            self._log(f"[Power] ❌ ohcl_trigger 범위 초과: {ms}ms (100~9999)")
            return False
        cmd = f"A{ms}\n"
        self._log(f"[Power TX] OHCL 트리거 {ms}ms → {cmd!r}")
        return self.send(cmd)

    def list_ports(self) -> list:
        try:
            import serial.tools.list_ports as _lp
            return [p.device for p in _lp.comports()]
        except ImportError:
            self._log("[Power] ❌ pyserial 미설치"); return []

    def read(self, timeout: float = 0.0) -> str:
        """하위 호환용 — 수신은 _rx_loop에서 처리."""
        return ""

    def _log(self, msg: str):
        # [스레드 안전 수정] _rx_loop는 백그라운드 스레드에서 실행됨.
        # _log_cb → _append_log → QPlainTextEdit.appendPlainText()를
        # 백그라운드 스레드에서 직접 호출하면 PyQt5 크래시 발생.
        # → 로그 큐(_log_queue)에만 쌓고, UI 스레드의 _poll_rx 타이머가 소비.
        self._log_queue.append(msg)

    def set_log_callback(self, cb):
        self._log_cb = cb

