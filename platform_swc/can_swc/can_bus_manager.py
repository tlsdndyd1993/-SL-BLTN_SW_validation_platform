# -*- coding: utf-8 -*-
"""
can_swc/can_bus_manager.py
CANoe COM API + XL Driver 기반 CAN 버스 관리.

의존: common/* 만.
"""
import os, sys, json, time, threading, queue
from typing import Optional, Dict, Any, List

from common.signals import Signals

class CanBusManager:
    """
    python-can 기반 CAN 버스 관리자.
    connect() 후 자동으로 수신 루프 시작 → DBC 디코딩 → 구독자 콜백 호출.
    WebSocket 브릿지 서버가 subscribe()로 등록 → 실시간 신호값 푸시.

    ★ CAN 쓰기 방식 2가지:
      1. canoe_write()  — CANoe COM API 경유 (CAPL 신호 공간 사용, 충돌 가능)
      2. xl_write()     — XL Driver(vxlapi64.dll) 직접 → 물리 CAN 버스 직접 송신
                          CANoe 실행 중에도 동시 사용 가능 (without init access)
                          CAPL 신호 공간과 완전 분리 → 충돌 없음
    """
    def __init__(self):
        self._bus        = None
        self._db         = None
        self._lock       = threading.Lock()
        self._rx_stop    = threading.Event()
        self._rx_thread  = None
        self.connected   = False
        self.status_msg  = ""
        # 구독자: {sig_name: [callback, ...]} — WebSocket 클라이언트마다 등록
        self._subscribers: dict = {}
        self._sub_lock   = threading.Lock()
        import queue as _initq
        self._write_queue = _initq.Queue()  # COM STA 스레드 write 큐
        # 최신 신호값 캐시: {sig_name: (timestamp, value)}
        self.sig_cache: dict = {}\
        # CANoe COM 모드 (python-can 대신 CANoe COM API 사용)
        self._canoe_app  = None   # win32com CANoe.Application
        self.canoe_mode  = False  # True = CANoe COM 폴링 사용
        self._poll_sigs: dict = {}  # {sig_name: msg_name} 폴링 대상
        self._poll_lock  = threading.Lock()
        # ── XL Driver 직접 쓰기 (without init access) ──────────────────
        self._xl_port    = None   # XL Driver 포트 핸들
        self._xl_mask    = None   # 채널 액세스 마스크
        self._xl_ch_idx  = None   # 채널 인덱스
        self.xl_ready    = False  # XL Driver 준비 완료 여부

    def connect_canoe(self, progress_cb=None) -> bool:
        """
        CANoe COM API 방식으로 연결.

        ★ COM STA 올바른 구조:
          COM 객체(CANoe.Application)는 생성한 스레드에서만 사용 가능.
          CoInitialize → Dispatch → 검증 → 폴링 루프 모두
          단일 COM 전용 스레드(_canoe_com_thread)에서 실행.
          connect_canoe()는 결과 큐를 통해 성공/실패만 수신.

        Returns:
            True = 연결 성공, False = 실패
        """
        import threading as _th, queue as _q, time as _t

        def _step(s, msg):
            self.status_msg = msg
            if progress_cb:
                try: progress_cb(s, msg)
                except Exception: pass

        # 이미 연결된 경우
        if self.connected and self._canoe_app:
            _step(6, "이미 연결됨")
            return True

        _result_q = _q.Queue()

        def _com_thread_main():
            """
            ★ 단일 COM STA 스레드 — Dispatch부터 폴링 루프까지 여기서 실행.
            CoInitialize/CoUninitialize는 이 스레드의 생애와 일치.
            """
            _coin = False
            try:
                # L1: pywin32 확인
                _step(0, "[L1 물리] pywin32 확인 중…")
                try:
                    import win32com.client as _w32
                except ImportError:
                    _result_q.put((False, "❌ [L1] pywin32 미설치 — pip install pywin32"))
                    return

                # L2: CoInitialize (STA)
                _step(1, "[L2 데이터링크] CoInitialize STA…")
                try:
                    import pythoncom as _pycom
                    _pycom.CoInitialize()
                    _coin = True
                    _step(1, "[L2 데이터링크] CoInitialize ✅")
                except ImportError:
                    _step(1, "[L2 데이터링크] pythoncom 없음 (무시)")
                except Exception as ce:
                    _step(1, f"[L2 데이터링크] CoInitialize 경고: {ce}")

                # L3: Dispatch (레지스트리 탐색)
                _step(2, "[L3 네트워크] CANoe.Application COM 탐색…")
                try:
                    app = _w32.Dispatch("CANoe.Application")
                except Exception as ex3:
                    _result_q.put((False,
                        "[L3] CANoe COM 탐색 실패: " + str(ex3) +
                        "  → CANoe 설치됐나요? HKEY_CLASSES_ROOT\\CANoe.Application 확인"))
                    return
                _step(2, "[L3 네트워크] CANoe.Application 획득 ✅")

                # L4: Application 응답 확인
                _step(3, "[L4 전송] Application 객체 응답 확인…")
                try:
                    _ = app.Name
                    _step(3, "[L4 전송] Application 응답 ✅")
                except Exception as ex4:
                    _result_q.put((False, "L4: Application 응답 없음 - " + str(ex4)))
                    return
                # L5: Measurement.Running 확인
                _step(4, "[L5 세션] Measurement.Running 확인…")
                try:
                    running = app.Measurement.Running
                except Exception as ex5:
                    _result_q.put((False, "L5: Measurement 접근 실패 - " + str(ex5)))
                    return
                if not running:
                    _result_q.put((False, "L5: Measurement 미실행 - CANoe Start 버튼을 눌러주세요"))
                _step(4, "[L5 세션] Measurement.Running = True ✅")

                # L6: 버전 정보
                _step(5, "[L6 표현] 버전 정보 조회…")
                try:
                    ver_str = str(app.Version)
                    _step(5, f"[L6 표현] CANoe v{ver_str} ✅")
                except Exception:
                    ver_str = "unknown"
                    _step(5, "[L6 표현] 버전 정보 조회 실패 (무시)")

                # L7: 연결 확정 + 폴링 루프 시작
                _step(6, "[L7 응용] 폴링 루프 시작…")
                self._canoe_app  = app
                self.canoe_mode  = True
                self.connected   = True
                self.status_msg  = f"CANoe COM 연결 완료  (v{ver_str})"
                self._rx_stop.clear()
                _result_q.put((True, self.status_msg))
                _step(6, f"[L7 응용] ✅ 연결 완료! {self.status_msg}")

                # ── 연결 직후 GetBus + GetSignal 진단 (1회만 실행) ──────────────
                _diag_bus = None
                _diag_lines = []  # 진단 결과 수집 후 1회 emit
                for _bc in ["CAN", "CAN 1", "CAN1", "CANFD", "CAN FD 1"]:
                    try:
                        _tb2 = app.GetBus(_bc)
                        if _tb2 is not None:
                            _diag_lines.append(f"[L7] GetBus('{_bc}') ✅")
                            if _diag_bus is None:
                                _diag_bus = _tb2
                        else:
                            _diag_lines.append(f"[L7] GetBus('{_bc}') = None")
                    except Exception as _be:
                        _diag_lines.append(f"[L7] GetBus('{_bc}') ❌")
                # 진단 결과를 한 줄로 압축해서 1회만 로그
                _ok_buses  = [l for l in _diag_lines if "✅" in l]
                _fail_buses = [l for l in _diag_lines if "❌" in l]
                _step(6, f"[L7] GetBus 진단: {len(_ok_buses)}개 OK, "
                          f"{len(_fail_buses)}개 실패  "
                          f"({', '.join(l.split("'")[1] for l in _ok_buses[:2])})")

                # ── GetSignal 방식 즉시 진단 (잘 알려진 신호로 테스트) ──────────
                if _diag_bus is not None:
                    _step(6, "[L7] GetSignal 방식 진단 중…")
                    # DBC에서 자주 쓰이는 신호들로 테스트
                    _test_pairs = [
                        ("BLTN_CAM_FD_02_200ms",     "BLTN_CAM_RecSet_OWD"),
                        ("BLTN_CAM_FD_STAT_01_200ms","BLTN_CAM_RecSta_OWD"),
                        ("BLTN_CAM_FD_02_200ms",     "BLTN_CAM_RecSet_OWP"),
                    ]
                    _gs_methods = [
                        ("GetSignal(1,msg,sig)", lambda b,m,s: b.GetSignal(1, m, s)),
                        ("GetSignal(1,sig)",     lambda b,m,s: b.GetSignal(1, s)),
                        ("GetSignal(2,msg,sig)", lambda b,m,s: b.GetSignal(2, m, s)),
                        ("GetSignal(2,sig)",     lambda b,m,s: b.GetSignal(2, s)),
                    ]
                    _found_method = False
                    for _tm, _ts in _test_pairs:
                        if _found_method: break
                        for _mn, _mf in _gs_methods:
                            try:
                                _sig_obj = _mf(_diag_bus, _tm, _ts)
                                _val = float(_sig_obj.Value)
                                _step(6, f"[L7] ✅ GetSignal 방식 확인: "
                                          f"{_mn}  신호={_ts}  값={_val:.3f}")
                                _found_method = True
                                break
                            except Exception:
                                continue
                    if not _found_method:
                        _step(6, "[L7] GetSignal 테스트 신호 미확인 (신호 추가 시 자동 탐색)")

                # ── 폴링 루프 (이 COM STA 스레드에서 직접 실행) ──────────────
                # ★ GetBus 채널명 자동 탐색 (CANoe 설정에 따라 다름)
                _bus_cache: dict = {}       # {sig_name: bus_obj} 캐시
                _err_log: set   = set()     # 이미 로그한 오류 중복 방지
                _POLL_ERR_MAX   = 3         # 동일 신호 오류 최대 로그 횟수
                _err_cnt: dict  = {}        # {sig_name: 오류 횟수}

                # ── GetSignal 시도 방식 목록 ──────────────────────────────────
                # CANoe 버전/설정에 따라 파라미터 방식이 다름
                # 진단: 처음 실패 시 모든 방식 시도 + 로그 출력
                _sig_method_cache: dict = {}   # {sig_name: callable} 성공한 방식 캐시
                _sig_diag_done:    set  = set() # 이미 진단한 신호

                def _read_signal_value(bus, sig_name, msg_name):
                    """
                    CANoe 17 COM API 올바른 GetSignal 호출.

                    ★ 근원 원인 확인:
                      GetSignal() 첫 파라미터는 채널 번호(int).
                      문자열(메시지명)을 넘기면 int 변환 실패 오류 발생.

                    올바른 시그니처:
                      bus.GetSignal(channel: int, msg_name: str, sig_name: str)
                      bus.GetSignal(channel: int, sig_name: str)
                    """
                    # 캐시된 방식이 있으면 바로 사용
                    if sig_name in _sig_method_cache:
                        try:
                            getter = _sig_method_cache[sig_name]
                            return float(getter().Value)
                        except Exception:
                            del _sig_method_cache[sig_name]

                    # ★ 채널 번호를 첫 파라미터로 — 여러 채널 번호 시도
                    methods = []
                    for ch in [1, 2, 3, 4, 5, 6, 7, 8]:
                        if msg_name:
                            methods.append(
                                (f"GetSignal({ch},msg,sig)",
                                 lambda _b=bus,_c=ch,_m=msg_name,_s=sig_name:
                                     _b.GetSignal(_c, _m, _s)))
                        methods.append(
                            (f"GetSignal({ch},sig)",
                             lambda _b=bus,_c=ch,_s=sig_name:
                                 _b.GetSignal(_c, _s)))

                    errors = []
                    for method_name, getter in methods:
                        try:
                            sig_obj = getter()
                            val = float(sig_obj.Value)
                            _sig_method_cache[sig_name] = getter
                            if sig_name not in _sig_diag_done:
                                _sig_diag_done.add(sig_name)
                                self.sig_cache[f"_DIAG_{sig_name}"] = (
                                    _t.time(),
                                    f"✅ {method_name} 성공 val={val:.3f}")
                            return val
                        except Exception as e:
                            errors.append(f"{method_name}: {str(e)[:40]}")
                            continue

                    if sig_name not in _sig_diag_done:
                        _sig_diag_done.add(sig_name)
                        self.sig_cache[f"_POLL_ERR_{sig_name}"] = (
                            _t.time(),
                            "GetSignal 실패: " + " | ".join(errors[:2]))
                    return None

                _bus_name_found = "CAN"   # GetBus('CAN') ✅ 확인됨

                def _do_write_in_sta(bus_obj, msg_name, signals):
                    """COM STA 스레드 전용 write — GetBus/OutputMessage 안전 실행."""
                    if not hasattr(self, '_alvcnt_cache'):
                        self._alvcnt_cache = {}
                    # ★ 진단 로그: COM STA 스레드에서 직접 signal emit
                    def _wlog(msg):
                        try: self._log_signal.emit(f"[Write-STA] {msg}")
                        except Exception: pass
                    _wlog(f"write 시작: {msg_name} {signals}")
                    all_ok = True
                    for sig_name, val in signals.items():
                        written = False
                        errs = []
                        # 방법 0: SysVarSig 네임스페이스 직접 쓰기 (최우선)
                        # CAPL .cin 파일이 모두 on sysvar(SysVarSig::xxx) 패턴으로
                        # 설계되어 있으므로, 이 방법만이 output(msg) → CAN BUS 송신을 트리거함.
                        # GetSignal().Value 는 수신 읽기 전용이라 송신 불가.
                        if not written:
                            try:
                                _ns = app.System.Namespaces("SysVarSig")
                                _ns.Variables(sig_name).Value = float(val)
                                written = True
                                _wlog(f"✅ SysVarSig.{sig_name}={val} 성공")
                            except Exception as _es:
                                errs.append(f"SysVar:{str(_es)[:50]}")
                        # 방법 1: ForcedValue / Value
                        for _ch in range(1, 9):
                            try:
                                so = bus_obj.GetSignal(_ch, msg_name, sig_name)
                                if so is None:
                                    errs.append(f"GetSig{_ch}:None"); continue
                            except Exception as eg:
                                errs.append(f"GetSig{_ch}:{str(eg)[:30]}"); continue
                            for attr in ('ForcedValue', 'Value'):
                                try:
                                    setattr(so, attr, float(val))
                                    written = True
                                    _wlog(f"✅ {attr}(ch{_ch}) 성공: {sig_name}={val}")
                                    break
                                except Exception as ew:
                                    errs.append(f"{attr}{_ch}:{str(ew)[:30]}")
                            if written: break
                        # 방법 2: CAN IG TriggerSend (스크린샷에서 확인된 방법)
                        if not written:
                            for _ig_name in ['GeneratorMessages', 'CANIG']:
                                try:
                                    ig_root = getattr(app, _ig_name, None)
                                    if ig_root is None:
                                        errs.append(f"IG.{_ig_name}:속성없음"); continue
                                    ig_msg = ig_root.Item(msg_name)
                                    ig_sig = ig_msg.Signals.Item(sig_name)
                                    ig_sig.Value = float(val)
                                    for send_method in ['TriggerSend', 'Send', 'Trigger']:
                                        try:
                                            getattr(ig_msg, send_method)()
                                            written = True
                                            _wlog(f"✅ IG.{_ig_name}.{send_method}() 성공")
                                            break
                                        except Exception as esm:
                                            errs.append(f"IG.{send_method}:{str(esm)[:25]}")
                                    if written: break
                                except Exception as eig:
                                    errs.append(f"IG.{_ig_name}:{str(eig)[:40]}")

                        # 방법 3: cantools 인코딩 후 OutputFDMessage (CAN FD 전용)
                        if not written and hasattr(self, '_db') and self._db:
                            try:
                                import cantools as _ct
                                md = self._db.get_message_by_name(msg_name)
                                sv = {}
                                for _s in md.signals:
                                    if _s.name == sig_name:
                                        sv[_s.name] = float(val)
                                    elif any(k in _s.name for k in ('AlvCnt','AliveCnt')):
                                        prev = self._alvcnt_cache.get(_s.name, 0)
                                        nxt = (int(prev)+1) % 256
                                        self._alvcnt_cache[_s.name] = nxt
                                        sv[_s.name] = float(nxt)
                                    elif any(k in _s.name for k in ('Crc','CRC')):
                                        sv[_s.name] = 0.0
                                    else:
                                        c = self.sig_cache.get(_s.name)
                                        sv[_s.name] = float(c[1]) if c else 0.0
                                data = list(md.encode(sv, padding=True))
                                frame_id = md.frame_id
                                # ★ CAN FD: OutputFDMessage, Classic: OutputMessage
                                is_fd = (md.length > 8)
                                _wlog(f"OutputMsg 시도: is_fd={is_fd} frame_id={hex(frame_id)} len={len(data)}")
                                for _ch in range(1, 9):
                                    for out_fn in (['OutputFDMessage'] if is_fd else
                                                   ['OutputMessage', 'OutputFDMessage']):
                                        try:
                                            fn = getattr(bus_obj, out_fn, None)
                                            if fn is None:
                                                errs.append(f"{out_fn}:API없음"); continue
                                            if out_fn == 'OutputFDMessage':
                                                fn(_ch, frame_id, 0, data)
                                            else:
                                                fn(_ch, frame_id, len(data), data)
                                            written = True
                                            _wlog(f"✅ {out_fn}(ch{_ch}) 성공!")
                                            break
                                        except Exception as eo:
                                            errs.append(f"{out_fn}{_ch}:{str(eo)[:35]}")
                                    if written: break
                            except Exception as ed:
                                errs.append(f"DBC:{str(ed)[:35]}")
                        elif not written:
                            errs.append("DBC 없음(cantools 필요)")
                        if not written:
                            short_lines = " | ".join(e for e in errs if e)
                            self.status_msg = "canoe_write 실패: " + sig_name + " | " + short_lines
                            _wlog(f"❌ 모든 방법 실패: {short_lines}")
                            all_ok = False

                    return all_ok   # ★ [버그 수정] 누락된 return 추가
                _bus_fail_cnt = 0   # GetBus 연속 실패 카운터
                _bus = None         # ★ [CAN 읽기 버그 수정] v1.1 대비 누락된 초기화.
                                    # 이 줄이 없으면 폴링 루프의 "if _bus is None:" 에서
                                    # NameError 발생 → except pass로 조용히 삼켜짐
                                    # → GetSignal이 한 번도 호출되지 않아 값이 "--" 로 표시됨

                while not self._rx_stop.is_set():
                    # ★ write 큐 처리를 루프 최상단에 — _bus None/continue에 영향 없음
                    try:
                        while True:
                            _wmsg, _wsigs, _wresq = self._write_queue.get_nowait()
                            # _bus 없으면 즉시 획득 시도
                            if _bus is None:
                                try: _bus = app.GetBus(_bus_name_found)
                                except Exception: pass
                            if _bus is None:
                                self.status_msg = "write 실패: GetBus None"
                                _wresq.put((False, self.status_msg)); continue
                            try:
                                _wok = _do_write_in_sta(_bus, _wmsg, _wsigs)
                                _wresq.put((_wok, self.status_msg))
                            except Exception as _we:
                                import traceback as _wtb
                                self.status_msg = ("write 예외: " + str(_we)
                                    + " | " + _wtb.format_exc()[:200])
                                _wresq.put((False, self.status_msg))
                    except Exception:
                        pass  # 큐 비었으면 정상 종료

                    try:
                        with self._poll_lock:
                            poll_sigs = dict(self._poll_sigs)
                        with self._sub_lock:
                            sub_sigs = set(self._subscribers.keys())
                        all_sigs = {s: poll_sigs.get(s, "")
                                    for s in (sub_sigs | set(poll_sigs))}
                        ts_now = _t.time()

                        # ★ [3초 지연 근본 원인 수정]
                        # GetBus()는 COM IDispatch 획득 — 매 루프 호출 시 수백ms~수초 소요.
                        # _bus 객체를 캐시하고, 실패(예외 발생) 시에만 재획득.
                        # → 정상 동작 중 GetBus() 호출 횟수: 루프당 0회 (캐시 재사용)
                        if _bus is None:
                            try:
                                _bus = app.GetBus(_bus_name_found)
                                _bus_fail_cnt = 0
                            except Exception as _be:
                                _bus_fail_cnt += 1
                                # 연속 실패 5회까지는 짧게 재시도, 이후 100ms 대기
                                _t.sleep(0.02 if _bus_fail_cnt < 5 else 0.1)
                                continue
                        if _bus is None:
                            _t.sleep(0.1); continue

                        for sig_name, msg_name in all_sigs.items():
                            try:
                                val = _read_signal_value(_bus, sig_name, msg_name)
                            except Exception:
                                # GetSignal 예외 = _bus 객체 무효화 → 다음 루프에서 재획득
                                _bus = None
                                break
                            if val is None:
                                continue

                            # 타임스탬프: 현재 시각 사용 (GetBus 캐시로 ts_now 정확도 향상)
                            ts = ts_now

                            # sig_cache 업데이트
                            # ★ sig_cache 키 수 상한 (DBC 신호 수천 개 방어)
                            if len(self.sig_cache) > 5000 and sig_name not in self.sig_cache:
                                pass  # 상한 초과 시 신규 신호 추가 스킵
                            else:
                                self.sig_cache[sig_name] = (ts, val)
                            ev_key  = f"_EV_{sig_name}"
                            ev_list = self.sig_cache.get(ev_key)
                            if not isinstance(ev_list, list):
                                ev_list = []
                            ev_list.append((ts, val))
                            # ★ 메모리 누수 수정: 상한 1500개 (신호 N개 × 1500 = 감당 가능)
                            # 이전: 9000개 × 신호 10개 = 90,000 튜플 RAM 상주
                            # 수정: 1500개. 5분 @ 500ms 폴링 기준 충분한 히스토리
                            if len(ev_list) > 1650:   # 10% 여유
                                del ev_list[:150]      # 150개 제거 → 1500개 유지
                            self.sig_cache[ev_key] = ev_list
                            # 값 변화 시에만 구독자 콜백
                            prev = self.sig_cache.get(f"_PREV_{sig_name}")
                            if prev is None or abs(val - prev) > 1e-9:
                                self.sig_cache[f"_PREV_{sig_name}"] = val
                                with self._sub_lock:
                                    for cb in self._subscribers.get(sig_name, []):
                                        try: cb(sig_name, val, ts)
                                        except Exception: pass
                    except Exception:
                        pass
                    # 20ms 대기 — GetBus 캐시로 실제 폴링 지연은 ~GetSignal(1ms) + 20ms
                    self._rx_stop.wait(0.02)

            except Exception as ex:
                import traceback as _tb
                _result_q.put((False, f"❌ COM 스레드 예외: {ex}\n{_tb.format_exc()}"))
            finally:
                self._canoe_app = None
                self.connected  = False
                self.canoe_mode = False
                if _coin:
                    try:
                        import pythoncom as _pycom2
                        _pycom2.CoUninitialize()
                    except Exception:
                        pass

        # COM 전용 스레드 시작
        self._com_thread = _th.Thread(
            target=_com_thread_main,
            daemon=True, name="CANoe_COM_STA")
        self._com_thread.start()

        # 결과 대기 (최대 12초)
        try:
            ok, msg = _result_q.get(timeout=12)
        except _q.Empty:
            self.status_msg = "❌ COM 연결 타임아웃 (12초)"
            _step(-1, self.status_msg)
            return False

        if not ok:
            self.status_msg = msg
            _step(-1, msg)
            return False

        self.status_msg = msg
        return True


    def _canoe_poll_loop(self):
        """
        ★ 이 메서드는 사용되지 않습니다.
        폴링 루프는 connect_canoe()의 _com_thread_main 내부에서 직접 실행됩니다.
        COM STA 스레드 안전성 보장을 위해 COM 객체 생성 스레드에서 일괄 처리.
        """
        pass   # 하위 호환성 유지용 stub

    def add_poll_signal(self, sig_name: str, msg_name: str = ""):
        """폴링 대상 신호 등록 (CANoe 모드에서 주기적으로 읽힘)."""
        with self._poll_lock:
            self._poll_sigs[sig_name] = msg_name

    def remove_poll_signal(self, sig_name: str):
        """폴링 대상 신호 해제."""
        with self._poll_lock:
            self._poll_sigs.pop(sig_name, None)

    def canoe_write(self, msg_name: str, signals: dict) -> bool:
        """
        CANoe COM을 통해 신호값 쓰기 (CCU/HU 등 주변 제어기 모사).

        DBC 없이도 동작 (CANoe가 이미 DB를 알고 있으므로).

        Args:
            msg_name: 메시지명 (CANoe Environment 변수명으로도 사용 가능)
            signals:  {신호명: 값} 딕셔너리

        Example::
            # HU가 BLTN_CAM에 이벤트 녹화 전/후 시간 설정
            can.canoe_write("HU_BLTN_CAM_02_200ms", {
                "BLTN_CAM_Set_EV_BfrTime": 30,
                "BLTN_CAM_Set_EV_AftTime": 10,
            })
        """
        if not self.canoe_mode or not self._canoe_app:
            return self.can_write(msg_name, signals)

        # ★ COM STA 큐 방식: UI 스레드에서 직접 GetBus() 호출 불가
        #   → _write_queue에 작업 넣기 → COM STA 스레드에서 실행
        if not hasattr(self, '_write_queue'):
            self.status_msg = "canoe_write: 큐 미초기화"
            return False
        import queue as _wq2
        _rq = _wq2.Queue()
        self._write_queue.put((msg_name, signals, _rq))
        try:
            ok, msg = _rq.get(timeout=3.0)
            self.status_msg = msg or self.status_msg
            return ok
        except _wq2.Empty:
            self.status_msg = "canoe_write: 타임아웃 3초 — COM STA 스레드 응답 없음"
            return False

    # ══════════════════════════════════════════════════════════════════════
    #  XL Driver 직접 쓰기 (vxlapi64.dll / vxlapi.dll)
    #  CANoe 실행 중 동시 사용 가능 — without init access
    #  CAPL 신호 공간과 완전 분리 → 충돌 없음
    # ══════════════════════════════════════════════════════════════════════

    def xl_connect(self, app_name: str = "BltnPlatform",
                   hw_channel: int = -1,
                   bitrate: int = 500000) -> bool:
        """
        XL Driver(vxlapi64.dll)를 without init access로 연결.
        CANoe가 이미 실행 중인 채널에 동시 접근 가능.

        ★ 채널 자동 탐색:
          1) xlGetApplConfig(app_name) → Vector HW Manager에 등록된 채널 사용
          2) 없으면 xlGetDriverConfig() → 연결된 CAN 채널 자동 탐색

        Args:
            app_name  : Vector HW Manager 앱 이름 (자동 등록됨)
            hw_channel: 특정 채널 강제 지정 (-1=자동, 0=ch1, 1=ch2 ...)
            bitrate   : 참고용 (without init access라 실제 변경 안 됨)

        Example (커널 스크립트)::
            ok = can.xl_connect()          # 자동 탐색
            ok = can.xl_connect(hw_channel=0)   # VN1640A CH1 강제
            if ok:
                log(can.status_msg)        # 연결 정보 확인
        """
        try:
            import ctypes, ctypes.wintypes as _wt, sys as _sys

            # ── DLL 로드 ────────────────────────────────────────────────
            dll_name = "vxlapi64.dll" if _sys.maxsize > 2**32 else "vxlapi.dll"
            search_paths = [
                dll_name,
                r"C:\Program Files\Vector XL Driver Library\bin\vxlapi64.dll",
                r"C:\Program Files\Vector XL Driver Library\bin\vxlapi.dll",
                r"C:\Program Files (x86)\Vector XL Driver Library\bin\vxlapi.dll",
            ]
            _xl = None
            for p in search_paths:
                try:
                    _xl = ctypes.WinDLL(p)
                    break
                except OSError:
                    continue
            if _xl is None:
                self.status_msg = ("vxlapi DLL 로드 실패\n"
                    "Vector Driver Package 설치 필요: "
                    "https://www.vector.com/latest-driver-setup-windows")
                return False
            self._xl_dll = _xl

            XL_SUCCESS           = 0
            XL_BUS_TYPE_CAN      = 0x00000001
            XL_BUS_ACTIVE_CAP_CAN = 0x00000001
            XL_INTERFACE_VERSION_V4 = 4
            # without init access: 버스에 올라타기만, 채널 파라미터 변경 불가
            XL_NO_COMMAND        = 0x00

            # ── xlOpenDriver ────────────────────────────────────────────
            _xl.xlOpenDriver.restype  = ctypes.c_short
            status = _xl.xlOpenDriver()
            if status != XL_SUCCESS:
                self.status_msg = f"xlOpenDriver 실패 (code={status})"
                return False

            # ── XLchannelConfig 구조체 정의 ─────────────────────────────
            class _XLchannelConfig(ctypes.Structure):
                _pack_ = 1
                _fields_ = [
                    ("name",               ctypes.c_char * 32),
                    ("hwType",             ctypes.c_ubyte),
                    ("hwIndex",            ctypes.c_ubyte),
                    ("hwChannel",          ctypes.c_ubyte),
                    ("transceiverType",    ctypes.c_ushort),
                    ("transceiverState",   ctypes.c_ushort),
                    ("configError",        ctypes.c_ushort),
                    ("channelIndex",       ctypes.c_ubyte),
                    ("channelMask",        ctypes.c_ulonglong),
                    ("channelCapabilities",ctypes.c_uint),
                    ("channelBusCapabilities", ctypes.c_uint),
                    ("isOnBus",            ctypes.c_ubyte),
                    ("busParams",          ctypes.c_ubyte * 32),
                    ("_reserved",          ctypes.c_uint),
                    ("_reserved2",         ctypes.c_ushort),
                    ("driverVersion",      ctypes.c_uint),
                    ("interfaceVersion",   ctypes.c_uint),
                    ("raw_data",           ctypes.c_uint * 10),
                    ("serialNumber",       ctypes.c_uint),
                    ("articleNumber",      ctypes.c_uint),
                    ("transceiverName",    ctypes.c_char * 32),
                    ("specialCabFlags",    ctypes.c_ushort),
                    ("dominantTimeout",    ctypes.c_ushort),
                    ("dominantRecessiveDelay", ctypes.c_ubyte),
                    ("recessiveDominantDelay", ctypes.c_ubyte),
                    ("connectionInfo",     ctypes.c_ubyte),
                    ("currentlyAvailable", ctypes.c_ubyte),
                    ("_reserved3",         ctypes.c_ushort),
                    ("channelBusActiveCapabilities", ctypes.c_uint),
                    ("breakOffset",        ctypes.c_ushort),
                    ("_padding",           ctypes.c_ubyte * 60),
                ]

            class _XLdriverConfig(ctypes.Structure):
                _pack_ = 1
                _fields_ = [
                    ("dllVersion",   ctypes.c_uint),
                    ("channelCount", ctypes.c_uint),
                    ("reserved",     ctypes.c_uint * 10),
                    ("channel",      _XLchannelConfig * 64),
                ]

            drv_cfg = _XLdriverConfig()
            _xl.xlGetDriverConfig.restype  = ctypes.c_short
            _xl.xlGetDriverConfig.argtypes = [ctypes.POINTER(_XLdriverConfig)]
            status = _xl.xlGetDriverConfig(ctypes.byref(drv_cfg))
            if status != XL_SUCCESS:
                self.status_msg = f"xlGetDriverConfig 실패 (code={status})"
                _xl.xlCloseDriver()
                return False

            n_ch = drv_cfg.channelCount
            ch_info_lines = []
            chosen_mask         = None
            chosen_name         = ""
            chosen_idx          = -1
            chosen_hw_type      = 0
            chosen_hw_index     = 0
            chosen_hw_channel   = 0

            # ★ 방법 1: xlGetApplConfig — HW Manager에 등록된 채널 직접 읽기
            # BltnPlatform CAN1 = VN1640A Channel 4 이 이미 등록돼 있으므로 이걸 씀
            _xl.xlGetApplConfig.restype  = ctypes.c_short
            _xl.xlGetApplConfig.argtypes = [
                ctypes.c_char_p,              # appName
                ctypes.c_uint,                # appChannel
                ctypes.POINTER(ctypes.c_uint),# pHwType  (out)
                ctypes.POINTER(ctypes.c_uint),# pHwIndex (out)
                ctypes.POINTER(ctypes.c_uint),# pHwChannel (out)
                ctypes.c_uint,                # busType
            ]
            _hw_type    = ctypes.c_uint(0)
            _hw_index   = ctypes.c_uint(0)
            _hw_channel = ctypes.c_uint(0)
            appl_status = _xl.xlGetApplConfig(
                app_name.encode("ascii"),
                0,              # appChannel 0 (CAN 1)
                ctypes.byref(_hw_type),
                ctypes.byref(_hw_index),
                ctypes.byref(_hw_channel),
                XL_BUS_TYPE_CAN,
            )
            if appl_status == XL_SUCCESS and _hw_type.value > 0:
                # HW Manager에 등록된 채널 찾음 → channelMask 획득
                _xl.xlGetChannelMask.restype  = ctypes.c_ulonglong
                _xl.xlGetChannelMask.argtypes = [
                    ctypes.c_int, ctypes.c_int, ctypes.c_int]
                appl_mask = _xl.xlGetChannelMask(
                    _hw_type.value, _hw_index.value, _hw_channel.value)
                if appl_mask:
                    chosen_mask       = appl_mask
                    chosen_hw_type    = _hw_type.value
                    chosen_hw_index   = _hw_index.value
                    chosen_hw_channel = _hw_channel.value
                    # channelIndex 찾기 (로그용)
                    for i in range(min(n_ch, 64)):
                        ch = drv_cfg.channel[i]
                        if ch.channelMask == appl_mask:
                            chosen_name = ch.name.decode("ascii", "replace")
                            chosen_idx  = i
                            break
                    ch_info_lines.append(
                        f"  [xlGetApplConfig] hwType={chosen_hw_type} "
                        f"hwIdx={chosen_hw_index} hwCh={chosen_hw_channel} "
                        f"mask=0x{chosen_mask:X} name={chosen_name}")

            # ★ 방법 2: xlGetApplConfig 실패 시 xlGetDriverConfig 탐색
            if chosen_mask is None:
                for i in range(min(n_ch, 64)):
                    ch = drv_cfg.channel[i]
                    is_can = bool(ch.channelBusActiveCapabilities & XL_BUS_ACTIVE_CAP_CAN)
                    ch_info_lines.append(
                        f"  ch[{i}] hwType={ch.hwType} hwIdx={ch.hwIndex} "
                        f"hwCh={ch.hwChannel} mask=0x{ch.channelMask:X} "
                        f"name={ch.name.decode('ascii','replace')} CAN={is_can}")
                    if is_can and chosen_mask is None:
                        if hw_channel < 0 or ch.hwChannel == hw_channel:
                            chosen_mask       = ch.channelMask
                            chosen_name       = ch.name.decode("ascii", "replace")
                            chosen_idx        = i
                            chosen_hw_type    = ch.hwType
                            chosen_hw_index   = ch.hwIndex
                            chosen_hw_channel = ch.hwChannel

            if chosen_mask is None:
                detail = "\n".join(ch_info_lines) or "채널 없음"
                self.status_msg = (
                    f"CAN 채널 없음\n{detail}\n"
                    "Vector Hardware Manager → BltnPlatform → CAN 1 채널 확인")
                _xl.xlCloseDriver()
                return False

            status_appl = XL_SUCCESS  # 이미 등록돼 있으므로 SetApplConfig 불필요

            # xlGetChannelMask: xlSetApplConfig 이후 정확한 mask 재획득
            _xl.xlGetChannelMask.restype  = ctypes.c_ulonglong
            _xl.xlGetChannelMask.argtypes = [
                ctypes.c_int, ctypes.c_int, ctypes.c_int]
            remask = _xl.xlGetChannelMask(
                chosen_hw_type, chosen_hw_index, chosen_hw_channel)
            if remask:
                chosen_mask = remask   # 재획득 성공 시 업데이트

            self._xl_mask = chosen_mask

            # ── xlOpenPort (without init access) ──────────────────
            # VN1640A 등 CAN FD 장비는 XL_INTERFACE_VERSION_V4 + rxQueueSize>=8192 필요
            # Classic CAN민 256 최소, FD민 8192 최소 (추권: 32768)
            _xl.xlOpenPort.restype    = ctypes.c_short
            _xl.xlOpenPort.argtypes   = [
                ctypes.POINTER(ctypes.c_long),
                ctypes.c_char_p,
                ctypes.c_ulonglong,
                ctypes.POINTER(ctypes.c_ulonglong),
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.c_uint,
            ]
            port_handle  = ctypes.c_long(-1)
            perm_mask_in = ctypes.c_ulonglong(0)   # 0 = without init access

            # ① CAN FD 시도 (VN1640A은 FD 장비, rxQueueSize ���쬨 있어야 함)
            status = _xl.xlOpenPort(
                ctypes.byref(port_handle),
                app_name.encode("ascii"),
                chosen_mask,
                ctypes.byref(perm_mask_in),
                32768,                         # ★ CAN FD: 8192 이상 (2의 배수)
                XL_INTERFACE_VERSION_V4,       # V4 = CAN FD 취미
                XL_BUS_TYPE_CAN,
            )
            self._xl_is_fd = True

            if status != XL_SUCCESS:
                # ② Classic CAN 시도 (FD 추권 안 한다뮼 연결 싴팼민)
                port_handle  = ctypes.c_long(-1)
                perm_mask_in = ctypes.c_ulonglong(0)
                status = _xl.xlOpenPort(
                    ctypes.byref(port_handle),
                    app_name.encode("ascii"),
                    chosen_mask,
                    ctypes.byref(perm_mask_in),
                    256,                           # Classic CAN: 256~32768
                    XL_INTERFACE_VERSION_V4,
                    XL_BUS_TYPE_CAN,
                )
                self._xl_is_fd = False

            if status != XL_SUCCESS:
                _xl.xlCloseDriver()
                self.status_msg = (
                    f"xlOpenPort 싴팼 (code={status})\n"
                    "\xe2\x86\x92 제안 해결 방안:\n"
                    "  Vector Hardware Manager 열기 → Application 탭\n"
                    f"  '{app_name}' 추가 후 채널 할당\n"
                    "  또는 xlSetApplConfig() 자동 등록 싵니다")
                return False

            self._xl_port = port_handle.value
            got_init = bool(perm_mask_in.value & chosen_mask)
            fd_str   = "CAN FD" if self._xl_is_fd else "Classic CAN"

            # ── xlActivateChannel ────────────────────────────────────────
            _xl.xlActivateChannel.restype  = ctypes.c_short
            _xl.xlActivateChannel.argtypes = [
                ctypes.c_long, ctypes.c_ulonglong, ctypes.c_uint, ctypes.c_uint]
            status = _xl.xlActivateChannel(
                self._xl_port, chosen_mask, XL_BUS_TYPE_CAN, XL_NO_COMMAND)
            if status != XL_SUCCESS:
                self.status_msg = f"xlActivateChannel 실패 (code={status})"
                _xl.xlClosePort(ctypes.c_long(self._xl_port))
                _xl.xlCloseDriver()
                self._xl_port = None
                return False

            self.xl_ready = True
            init_str = "init access" if got_init else "without init access ✅"
            self.status_msg = (
                f"XL Driver 연결 완료\n"
                f"  채널: {chosen_name} (ch[{chosen_idx}])\n"
                f"  mask: 0x{chosen_mask:X}\n"
                f"  모드: {fd_str}\n"
                f"  권한: {init_str}\n"
                f"  전체 채널: {n_ch}개\n"
                + "\n".join(ch_info_lines))
            return True

        except Exception as ex:
            import traceback
            self.status_msg = f"xl_connect 예외: {ex}\n{traceback.format_exc()[-400:]}"
            self.xl_ready = False
            return False


    def xl_disconnect(self):
        """XL Driver 포트 닫기."""
        if not self.xl_ready:
            return
        try:
            import ctypes
            _xl = getattr(self, '_xl_dll', None)
            if _xl and self._xl_port is not None:
                _xl.xlDeactivateChannel(
                    ctypes.c_long(self._xl_port),
                    ctypes.c_ulonglong(self._xl_mask or 0))
                _xl.xlClosePort(ctypes.c_long(self._xl_port))
                _xl.xlCloseDriver()
        except Exception:
            pass
        self._xl_port = None
        self._xl_mask = None
        self.xl_ready = False
        self.status_msg = "XL Driver 연결 해제"

    def xl_write(self, msg_name: str, signals: dict,
                 msg_id: int = None) -> bool:
        """
        XL Driver를 통해 물리 CAN 버스에 직접 프레임 송신.
        CANoe 실행 중 동시 사용 가능 — CAPL 신호 공간과 완전 분리.

        ★ DBC 필수: load_dbc()로 CANoe와 동일한 .dbc 파일 로드 필요.
           메시지명 → ID, 신호 비트 인코딩을 DBC에서 읽음.

        Args:
            msg_name : DBC 메시지명 (예: "HU_BLTN_CAM_02_200ms")
            signals  : {신호명: 값} 딕셔너리
            msg_id   : 메시지 ID 직접 지정 (DBC 없을 때 사용, 기본=None)

        Returns:
            True = 송신 성공, False = 실패

        Example (커널 스크립트)::
            # xl_connect()는 프로그램 시작 시 한 번만 호출하면 됨
            can.xl_write("HU_BLTN_CAM_02_200ms", {
                "BLTN_CAM_Set_EV_BfrTime": 30,
                "BLTN_CAM_Set_EV_AftTime": 10,
            })
        """
        if not self.xl_ready or self._xl_port is None:
            self.status_msg = "xl_write: XL Driver 미연결 — xl_connect() 먼저 호출"
            return False

        try:
            import ctypes, ctypes.wintypes as _wt

            # ── DBC로 메시지 인코딩 ───────────────────────────────────
            frame_id   = msg_id
            frame_data = bytearray(8)
            frame_dlc  = 8

            if self._db is not None:
                try:
                    import cantools as _ct
                    msg_def   = self._db.get_message_by_name(msg_name)
                    frame_id  = msg_def.frame_id
                    frame_dlc = msg_def.length

                    # ★ 모든 신호를 채워서 인코딩
                    # - 사용자가 지정한 값 우선
                    # - CRC/AlvCnt는 별도 처리
                    # - 나머지 미지정 신호는 0 (sig_cache 사용 안 함 — 방향 오염 방지)
                    _CRC_KEYWORDS  = ('crc', 'Crc', 'CRC', 'chk', 'Chk', 'CHK')
                    _ALVCNT_KEYWORDS = ('alv', 'Alv', 'ALV', 'alive', 'Alive',
                                        'cnt', 'Cnt', 'counter', 'Counter')

                    full_signals = {}
                    crc_sig_name = None
                    alv_sig_name = None

                    for sig in msg_def.signals:
                        if sig.name in signals:
                            full_signals[sig.name] = signals[sig.name]
                        elif any(k in sig.name for k in _CRC_KEYWORDS):
                            crc_sig_name = sig.name
                            full_signals[sig.name] = 0   # 나중에 계산
                        elif any(k in sig.name for k in _ALVCNT_KEYWORDS):
                            alv_sig_name = sig.name
                            # AliveCounter: xl_write 호출 횟수마다 증가
                            if not hasattr(self, '_xl_alv_cnt'):
                                self._xl_alv_cnt = {}
                            cnt = self._xl_alv_cnt.get(msg_name, 0)
                            full_signals[sig.name] = cnt & 0xFF
                            self._xl_alv_cnt[msg_name] = (cnt + 1) & 0xFF
                        else:
                            full_signals[sig.name] = 0   # 미지정 신호 = 0

                    # 1차 인코딩 (CRC=0 상태)
                    encoded    = msg_def.encode(full_signals, padding=True)
                    frame_data = bytearray(encoded)

                    # CRC 계산 후 재인코딩 (AUTOSAR CRC8/CRC16)
                    if crc_sig_name:
                        crc_sig = msg_def.get_signal_by_name(crc_sig_name)
                        crc_len = crc_sig.length  # 8 or 16 bit
                        # CRC 범위: CRC 바이트 제외한 나머지 바이트
                        # AUTOSAR CRC8: 0x1D poly, init=0xFF
                        # 간단한 XOR 체크섬으로 근사 (실제 AUTOSAR CRC 필요 시 추가)
                        if crc_len <= 8:
                            # CRC8 (XOR 합산 근사)
                            crc_val = 0xFF
                            for i, b in enumerate(frame_data):
                                if i < crc_sig.start // 8 or i >= (crc_sig.start + crc_sig.length) // 8:
                                    crc_val ^= b
                            full_signals[crc_sig_name] = crc_val & 0xFF
                        else:
                            # CRC16: 전체 바이트 합산
                            crc_val = 0
                            for b in frame_data:
                                crc_val = (crc_val + b) & 0xFFFF
                            full_signals[crc_sig_name] = crc_val & 0xFFFF
                        encoded    = msg_def.encode(full_signals, padding=True)
                        frame_data = bytearray(encoded)

                except KeyError:
                    self.status_msg = f"xl_write: DBC에 '{msg_name}' 없음"
                    return False
                except Exception as _enc_ex:
                    self.status_msg = f"xl_write: 인코딩 실패 — {_enc_ex}"
                    return False
            elif frame_id is None:
                self.status_msg = ("xl_write: DBC 없음 — load_dbc() 또는 msg_id 직접 지정 필요\n"
                                   "예) can.xl_write('MSG', {}, msg_id=0x123)")
                return False

            # ── XLevent 구조체 구성 (CAN 2.0 표준 프레임) ──────────────
            # struct XLevent { u8 tag; u8 chanIdx; u16 transId; u16 portHandle;
            #                  u8 flags; u8 reserved;
            #                  XLuint64 timeStamp;
            #                  union { s_xl_can_msg msg; ... } tagData; }
            # s_xl_can_msg: u32 id; u16 flags; u16 dlc; u64 res1;
            #               u8 data[8]; u64 res2;  (총 32바이트 tagData)

            XL_TRANSMIT_MSG = 0x0A   # tag for TX

            class _XLCanMsg(ctypes.Structure):
                _pack_ = 1
                _fields_ = [
                    ("id",    ctypes.c_uint),
                    ("flags", ctypes.c_ushort),
                    ("dlc",   ctypes.c_ushort),
                    ("res1",  ctypes.c_ulonglong),
                    ("data",  ctypes.c_ubyte * 8),
                    ("res2",  ctypes.c_ulonglong),
                ]

            class _XLTagData(ctypes.Union):
                _pack_ = 1
                _fields_ = [("msg", _XLCanMsg)]

            class _XLevent(ctypes.Structure):
                _pack_ = 1
                _fields_ = [
                    ("tag",         ctypes.c_ubyte),
                    ("chanIdx",     ctypes.c_ubyte),
                    ("transId",     ctypes.c_ushort),
                    ("portHandle",  ctypes.c_ushort),
                    ("flags",       ctypes.c_ubyte),
                    ("reserved",    ctypes.c_ubyte),
                    ("timeStamp",   ctypes.c_ulonglong),
                    ("tagData",     _XLTagData),
                ]

            xl_event = _XleventInstance = _XLevent()
            ctypes.memset(ctypes.byref(xl_event), 0, ctypes.sizeof(xl_event))
            xl_event.tag                = XL_TRANSMIT_MSG
            xl_event.tagData.msg.id     = frame_id
            xl_event.tagData.msg.flags  = 0
            xl_event.tagData.msg.dlc    = frame_dlc
            for i, b in enumerate(frame_data[:8]):
                xl_event.tagData.msg.data[i] = b

            # ── 전송: CAN FD면 xlCanTransmitEx, Classic CAN이면 xlCanTransmit ──
            _xl = self._xl_dll
            XL_SUCCESS = 0
            is_fd = getattr(self, '_xl_is_fd', False)

            if is_fd:
                # ── xlCanTransmitEx (CAN FD) ──────────────────────────────
                # XLcanTxEvent 구조체
                XL_CAN_EV_TAG_TX_MSG = 0x0440
                XL_CAN_TXMSG_FLAG_EDL = 0x0004  # CAN FD extended data length

                class _XLCANTXMSG(ctypes.Structure):
                    _pack_ = 1
                    _fields_ = [
                        ("canId",    ctypes.c_uint),
                        ("msgFlags", ctypes.c_uint),
                        ("dlc",      ctypes.c_ubyte),
                        ("reserved", ctypes.c_ubyte * 7),
                        ("data",     ctypes.c_ubyte * 64),
                    ]

                class _XLcanTxTagData(ctypes.Union):
                    _pack_ = 1
                    _fields_ = [("canMsg", _XLCANTXMSG)]

                class _XLcanTxEvent(ctypes.Structure):
                    _pack_ = 1
                    _fields_ = [
                        ("tag",         ctypes.c_ushort),
                        ("transId",     ctypes.c_ushort),
                        ("channelIndex",ctypes.c_ubyte),
                        ("reserved",    ctypes.c_ubyte * 3),
                        ("tagData",     _XLcanTxTagData),
                    ]

                tx_ev = _XLcanTxEvent()
                ctypes.memset(ctypes.byref(tx_ev), 0, ctypes.sizeof(tx_ev))
                tx_ev.tag                    = XL_CAN_EV_TAG_TX_MSG
                tx_ev.tagData.canMsg.canId   = frame_id
                # ★ EDL 플래그: length > 8 일 때만 세팅 (CAN FD 확장 데이터)
                # HU_BLTN_CAM_02_200ms 는 8바이트 이하 → EDL 없이 CAN FD로 전송
                if frame_dlc > 8:
                    tx_ev.tagData.canMsg.msgFlags = XL_CAN_TXMSG_FLAG_EDL
                else:
                    tx_ev.tagData.canMsg.msgFlags = 0   # Classic frame over FD port
                # ★ DLC 필드: CAN FD DLC 코드표 변환
                # 0~8 바이트: dlc = length 그대로
                # 12→9, 16→10, 20→11, 24→12, 32→13, 48→14, 64→15
                _dlc_map = {12:9, 16:10, 20:11, 24:12, 32:13, 48:14, 64:15}
                tx_ev.tagData.canMsg.dlc = _dlc_map.get(frame_dlc, min(frame_dlc, 8))
                for i, b in enumerate(frame_data[:min(frame_dlc, 64)]):
                    tx_ev.tagData.canMsg.data[i] = b

                _xl.xlCanTransmitEx.restype  = ctypes.c_short
                _xl.xlCanTransmitEx.argtypes = [
                    ctypes.c_long,           # portHandle
                    ctypes.c_ulonglong,      # accessMask
                    ctypes.c_uint,           # msgCnt
                    ctypes.POINTER(ctypes.c_uint),  # pMsgCntSent
                    ctypes.c_void_p,         # pXlCanTxEvt
                ]
                sent = ctypes.c_uint(0)
                status = _xl.xlCanTransmitEx(
                    ctypes.c_long(self._xl_port),
                    ctypes.c_ulonglong(self._xl_mask),
                    1,
                    ctypes.byref(sent),
                    ctypes.byref(tx_ev),
                )
                fn_name = "xlCanTransmitEx(FD)"

            else:
                # ── xlCanTransmit (Classic CAN) ───────────────────────────
                XL_TRANSMIT_MSG = 0x0A

                class _XLCanMsg(ctypes.Structure):
                    _pack_ = 1
                    _fields_ = [
                        ("id",    ctypes.c_uint),
                        ("flags", ctypes.c_ushort),
                        ("dlc",   ctypes.c_ushort),
                        ("res1",  ctypes.c_ulonglong),
                        ("data",  ctypes.c_ubyte * 8),
                        ("res2",  ctypes.c_ulonglong),
                    ]

                class _XLTagData(ctypes.Union):
                    _pack_ = 1
                    _fields_ = [("msg", _XLCanMsg)]

                class _XLevent(ctypes.Structure):
                    _pack_ = 1
                    _fields_ = [
                        ("tag",        ctypes.c_ubyte),
                        ("chanIdx",    ctypes.c_ubyte),
                        ("transId",    ctypes.c_ushort),
                        ("portHandle", ctypes.c_ushort),
                        ("flags",      ctypes.c_ubyte),
                        ("reserved",   ctypes.c_ubyte),
                        ("timeStamp",  ctypes.c_ulonglong),
                        ("tagData",    _XLTagData),
                    ]

                xl_event = _XLevent()
                ctypes.memset(ctypes.byref(xl_event), 0, ctypes.sizeof(xl_event))
                xl_event.tag                = XL_TRANSMIT_MSG
                xl_event.tagData.msg.id     = frame_id
                xl_event.tagData.msg.dlc    = min(frame_dlc, 8)
                for i, b in enumerate(frame_data[:8]):
                    xl_event.tagData.msg.data[i] = b

                _xl.xlCanTransmit.restype  = ctypes.c_short
                _xl.xlCanTransmit.argtypes = [
                    ctypes.c_long,
                    ctypes.c_ulonglong,
                    ctypes.POINTER(ctypes.c_uint),
                    ctypes.c_void_p,
                ]
                msg_count = ctypes.c_uint(1)
                status = _xl.xlCanTransmit(
                    ctypes.c_long(self._xl_port),
                    ctypes.c_ulonglong(self._xl_mask),
                    ctypes.byref(msg_count),
                    ctypes.byref(xl_event),
                )
                fn_name = "xlCanTransmit(Classic)"

            if status == XL_SUCCESS:
                self.status_msg = (f"xl_write ✅ {msg_name} "
                                   f"ID=0x{frame_id:03X} dlc={frame_dlc} "
                                   f"data={list(frame_data[:frame_dlc])} "
                                   f"[{fn_name}]")
                return True
            else:
                self.status_msg = (f"xl_write ❌ {fn_name} 실패 "
                                   f"(code={status}, msg={msg_name})")
                return False

        except Exception as ex:
            import traceback
            self.status_msg = f"xl_write 예외: {ex}\n{traceback.format_exc()[-300:]}"
            return False




    def connect(self, channel=0, bitrate=500000,
                app_name="BltnRecorder", interface="vector"):
        if not CAN_AVAILABLE:
            self.status_msg = "python-can 미설치 — pip install python-can"
            return False
        try:
            with self._lock:
                self._bus = _can_lib.interface.Bus(
                    interface=interface, app_name=app_name,
                    channel=channel, bitrate=bitrate)
            self.connected = True
            self.status_msg = f"CAN 연결 (ch={channel}, {bitrate}bps)"
            # 수신 루프 시작
            self._rx_stop.clear()
            self._rx_thread = threading.Thread(
                target=self._rx_loop, daemon=True, name="CAN_RX")
            self._rx_thread.start()
            return True
        except Exception as ex:
            self.status_msg = f"CAN 연결 실패: {ex}"
            self.connected = False; return False

    def get_vector_devices(self) -> list:
        """연결된 Vector 하드웨어 장비 목록 조회."""
        devices = []
        if not self.canoe_mode or not self._canoe_app:
            try:
                import can as _can
                for c in _can.detect_available_configs(interfaces=['vector']):
                    devices.append({"name":c.get('app_name','Vector'),
                        "type":c.get('interface','vector'),
                        "channel":c.get('channel',0),
                        "serial":str(c.get('serial',''))})
            except Exception as e:
                devices.append({"name":str(e),"type":"error","channel":0,"serial":""})
            return devices
        for _bn in ["CAN","CAN 1","CAN 2","CAN FD 1"]:
            try:
                if self._canoe_app.GetBus(_bn):
                    devices.append({"name":_bn,"type":"CANoe Bus","channel":1,"serial":""})
            except Exception:
                pass
        return devices

    def disconnect(self):
        self._rx_stop.set()
        with self._lock:
            if self._bus:
                try: self._bus.shutdown()
                except Exception: pass
                self._bus = None
        self.connected = False; self.status_msg = "연결 해제"

    def subscribe(self, sig_name: str, callback) -> None:
        """신호 구독 등록. callback(sig_name, value, timestamp) 형태로 호출됨."""
        with self._sub_lock:
            self._subscribers.setdefault(sig_name, []).append(callback)

    def unsubscribe_all(self, callback) -> None:
        """특정 콜백의 모든 구독 해제 (WebSocket 연결 종료 시)."""
        with self._sub_lock:
            for sig in list(self._subscribers):
                self._subscribers[sig] = [
                    cb for cb in self._subscribers[sig] if cb != callback]

    def _rx_loop(self):
        """CAN 수신 루프 — DBC 디코딩 후 구독자에게 푸시."""
        import time as _t
        while not self._rx_stop.is_set():
            try:
                with self._lock:
                    bus = self._bus
                if bus is None:
                    _t.sleep(0.1); continue
                msg = bus.recv(timeout=0.1)
                if msg is None: continue
                ts = _t.time()
                if self._db:
                    try:
                        decoded = self._db.decode_message(
                            msg.arbitration_id, msg.data)
                        with self._sub_lock:
                            subs = dict(self._subscribers)
                        for sig_name, val in decoded.items():
                            numeric = float(val) if isinstance(
                                val, (int, float)) else None
                            self.sig_cache[sig_name] = (ts, numeric)
                            if sig_name in subs:
                                for cb in subs[sig_name]:
                                    try: cb(sig_name, numeric, ts)
                                    except Exception: pass
                    except Exception:
                        pass
            except Exception:
                _t.sleep(0.05)

    def send(self, arb_id, data, is_extended=False):
        if not CAN_AVAILABLE or not self.connected: return False
        try:
            import can as _c
            with self._lock:
                self._bus.send(_c.Message(
                    arbitration_id=arb_id, data=bytes(data[:8]),
                    is_extended_id=is_extended))
            return True
        except Exception as ex:
            self.status_msg = f"송신 실패: {ex}"; return False

    def send_signal(self, msg_name, signals):
        if not self._db:
            self.status_msg = "DBC 미로드"; return False
        try:
            md = self._db.get_message_by_name(msg_name)
            return self.send(md.frame_id, list(md.encode(signals)))
        except Exception as ex:
            self.status_msg = f"신호 송신 실패: {ex}"; return False

    def recv(self, timeout=1.0):
        if not CAN_AVAILABLE or not self.connected: return None
        try:
            with self._lock: msg = self._bus.recv(timeout=timeout)
            if msg is None: return None
            dec = None
            if self._db:
                try: dec = self._db.decode_message(msg.arbitration_id, msg.data)
                except Exception: pass
            return {"id": hex(msg.arbitration_id),
                    "data": msg.data.hex(), "decoded": dec}
        except Exception as ex:
            self.status_msg = f"수신 실패: {ex}"; return None

    def load_dbc(self, path):
        try:
            import cantools as _ct
            self._db = _ct.database.load_file(path)
            self.status_msg = f"DBC 로드: {os.path.basename(path)}"
            return True
        except ImportError:
            self.status_msg = "cantools 미설치 — pip install cantools"; return False
        except Exception as ex:
            self.status_msg = f"DBC 로드 실패: {ex}"; return False

    def info(self):
        s = "연결:" + ("OK" if self.connected else "X") + "  "
        if self._db:
            s += f"DBC:{len(self._db.messages)}개  "
        return s + self.status_msg

    # ── 커널 스크립트용 고수준 API ──────────────────────────────────────
    def can_read(self, sig_name: str, timeout: float = 0.0) -> "float | None":
        """
        CAN 신호값 읽기 (커널 스크립트용).
        DBC 로드 + CAN 연결 상태에서만 동작.

        Args:
            sig_name: DBC 신호명 (예: "BLTN_CAM_RecStat")
            timeout:  0이면 캐시 즉시 반환, >0이면 최대 timeout초 대기 후 반환

        Returns:
            float 값 또는 None (신호 미수신 / 미연결)

        Example (커널 스크립트)::
            v = can.read("BLTN_CAM_RecStat")
            if v is not None and v == 1.0:
                engine.start_recording()
        """
        import time as _t
        # [버그 수정] CANoe COM 모드일 때 self.connected=False이므로 canoe_mode도 체크
        is_active = self.connected or self.canoe_mode
        if not is_active:
            return None
        if timeout <= 0:
            # 캐시에서 즉시 반환
            entry = self.sig_cache.get(sig_name)
            return entry[1] if entry else None
        # timeout 동안 폴링
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            entry = self.sig_cache.get(sig_name)
            if entry is not None:
                return entry[1]
            _t.sleep(0.02)
        return None

    def read_signal(self, sig_name: str) -> "float | None":
        """[추가] sig_cache에서 최신 신호값 즉시 반환.
        타이머 CAN 조건 평가 등 내부에서 사용.
        CANoe 모드 / python-can 모드 모두 지원.
        """
        entry = self.sig_cache.get(sig_name)
        return entry[1] if entry else None

    def can_wait_value(
            self, sig_name: str, expected,
            timeout: float = 10.0,
            tolerance: float = 0.0,
            poll_interval: float = 0.05) -> bool:
        """
        CAN 신호값이 expected가 될 때까지 대기.

        Args:
            sig_name:      DBC 신호명
            expected:      목표값 (float 또는 int)
            timeout:       최대 대기 시간(초), 기본 10초
            tolerance:     허용 오차 (기본 0 = 정확 일치)
            poll_interval: 폴링 주기(초), 기본 50ms

        Returns:
            True = 조건 충족, False = timeout

        Example::
            # BLTN_CAM_SD_State 가 1이 될 때까지 최대 30초 대기
            ok = can.wait_value("BLTN_CAM_SD_State", 1, timeout=30)
            if ok:
                engine.start_recording()
        """
        import time as _t
        deadline = _t.time() + timeout
        expected = float(expected)
        while _t.time() < deadline:
            v = self.can_read(sig_name)
            if v is not None:
                if tolerance <= 0:
                    if v == expected:
                        return True
                else:
                    if abs(v - expected) <= tolerance:
                        return True
            _t.sleep(poll_interval)
        return False

    def can_write(self, msg_name: str, signals: dict) -> bool:
        """
        CAN 메시지 신호값 송신 (커널 스크립트용).
        DBC에서 msg_name으로 메시지를 찾아 signals 딕셔너리를 인코딩 후 송신.

        Args:
            msg_name: DBC 메시지명 (예: "HU_Car_01_200ms")
            signals:  {신호명: 값} 딕셔너리
                      예: {"HU_LKA_Set": 1, "HU_FCA_Set": 1}

        Returns:
            True = 송신 성공, False = 실패

        Example::
            # HU 가 BLTN_CAM에 녹화 설정 신호 전송
            can.write("HU_BLTN_CAM_02_200ms", {
                "BLTN_CAM_Set_EV_BfrTime": 30,
                "BLTN_CAM_Set_EV_AftTime": 10,
            })
        """
        if not self.connected:
            return False
        if not self._db:
            return False
        return self.send_signal(msg_name, signals)

    def can_get_latest(self, sig_name: str) -> "tuple[float, float] | None":
        """
        캐시에서 (timestamp, value) 반환.
        캐시 미등록 시 None.

        Example::
            ts, val = can.get_latest("BLTN_CAM_RecStat") or (None, None)
        """
        return self.sig_cache.get(sig_name)

    def can_read_all(self) -> dict:
        """
        현재 캐시에 있는 모든 신호의 최신값 딕셔너리 반환.
        Returns: {sig_name: value}
        """
        return {k: v[1] for k, v in self.sig_cache.items()}


try:
    import can as _can_lib
    CAN_AVAILABLE = True
except ImportError:
    _can_lib = None
    CAN_AVAILABLE = False

_can_manager = CanBusManager()


