# -*- coding: utf-8 -*-
"""
can_swc/make_script_helpers.py
AI 생성 커널 스크립트 전처리 파이프라인 (make_script 6단계).
"""
import re

def _restore_indent(code: str) -> str:
    """Auto-restore Python indentation stripped by AI in JSON code field.
    Stack-based. Also splits lines incorrectly joined without newline
    e.g. 't1=time.time() while not kernel...' -> proper newline split.
    """
    import re as _re

    code = code.replace('\r\n', '\n').replace('\r', '\n')

    # Pre-process: split "expr while/if" that AI wrote without newline
    # e.g. "_t2.time() while not kernel" -> "_t2.time()\nwhile not kernel"
    code = _re.sub(
        r'([)\w\'\"])(\s+)(while\b|if\b(?!\s+__name__))',
        lambda m: m.group(1) + '\n' + m.group(3),
        code
    )

    lines = code.split('\n')

    # If already indented enough, return as-is
    indented = [l for l in lines if l and l[0] == ' ']
    if len(indented) > 2:
        return code

    INDENT = '    '

    BLOCK_OPEN = _re.compile(
        r'^(if |elif |else\s*:|while |for |def |class |try\s*:'
        r'|except[\s:]|except$|finally\s*:|with )')
    DEDENT_KW  = _re.compile(
        r'^(elif |else\s*:|except[\s:]|except$|finally\s*:)')
    ENDS_COLON = _re.compile(r'.*:\s*(#.*)?$')

    depth = 0
    result = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            result.append('')
            continue
        if DEDENT_KW.match(stripped) and depth > 0:
            depth -= 1
        result.append(INDENT * depth + stripped)
        if BLOCK_OPEN.match(stripped) and ENDS_COLON.search(stripped):
            depth += 1

    return '\n'.join(result)



_API_DOC = """
╔══════════════════════════════════════════════════════════════════════════════╗
║   Screen & Camera Recorder  —  Kernel / AI Chat API Reference  (v5.6)     ║
║   ★ 이 목록에 없는 함수는 AI Chat / 커널 스크립트에서 절대 사용 불가.     ║
╚══════════════════════════════════════════════════════════════════════════════╝

【커널 스크립트 전역 객체】
  engine   : CoreEngine    (녹화·캡처·수동녹화·ROI·오토클릭·매크로 등)
  kernel   : KernelEngine  (대기·조건판단·TC결과·전원·CAN·타이머)
  can      : CanBusManager (CAN 신호 읽기/쓰기)
  timer    : TimerPanel    (조건부 타이머 직접 제어)
  power    : PowerController (전원·OHCL 릴레이 직접 제어)
  log(msg) : 커널 로그 출력 (시간 prefix 자동)
  sys, os, time, datetime, re, json, threading, subprocess, platform
  np (numpy), cv2 (opencv)
  pip_install(*pkgs)  패키지 설치
  pip_list()          설치된 패키지 목록 출력
  env_info()          Python 환경 정보 출력

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 1. 대기 / 흐름 제어  ★ 필수
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  kernel.wait(seconds)          슬립 + 중단 감지  ⚠ time.sleep() 금지
  kernel.is_stopped() → bool    ■ 중단 버튼 눌렸는지 확인
  kernel.emit_status(msg)       하단 상태바 메시지 출력

  예시: 타임아웃 루프 (time은 이미 주입됨 — import 불필요)
    t0 = time.time()
    while not kernel.is_stopped():
        if time.time() - t0 > 10:
            log("타임아웃"); break
        kernel.wait(0.2)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 2. 녹화  [패널: 녹화]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  engine.start_recording()               녹화 시작
  engine.stop_recording()                녹화 중지
  engine.recording : bool                녹화 중 여부
  engine.output_dir : str                저장 경로
  engine.base_dir : str                  기본 경로

  AI Chat 액션:
    {"action":"start_recording"}
    {"action":"stop_recording","result":"PASS"}   result: "PASS"|"FAIL"|""

  예시: 10초 녹화
    engine.start_recording()
    kernel.wait(10.0)
    engine.stop_recording()

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 3. 수동녹화  [패널: 수동녹화]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  engine.set_manual_time(pre_sec, post_sec)
      ★ 수동녹화 시간 설정 + UI 동기화. 반드시 이 메서드 사용.
      ★ engine.manual_pre_sec = N 직접 대입 금지 (UI 미반영)
      예) engine.set_manual_time(20, 30)  # 앞 20초, 뒤 30초

  engine.save_manual_clip() → bool
      수동 클립 저장 (비동기 실행, 즉시 리턴)

  kernel.wait_manual_clip(timeout: float = 120.0)
      ★ 수동녹화 완료까지 대기. save_manual_clip() 후 반드시 호출.
      timeout: pre+post+여유(초). 예) post=30초 → timeout=60 이상.
      ⚠ while not engine.recording 패턴으로 완료 감지 불가.
         recording은 메인 녹화 상태이며 수동녹화와 무관하게 유지됨.

  engine.set_manual_source(source)  source: "screen"|"camera"|"both"  ← UI 동기화 포함
  engine.manual_source : str   "screen"|"camera"|"both"  ← 직접 대입 시 UI 미동기화

  ★ 올바른 사용 순서:
    engine.set_manual_time(20, 30)       # 1. 시간 설정 (UI 동기화)
    engine.save_manual_clip()            # 2. 수동녹화 시작 (비동기)
    kernel.wait_manual_clip(timeout=70)  # 3. 완료 대기 (post+여유)
    engine.stop_recording()              # 4. 메인 녹화 종료

  AI Chat 액션:
    {"action":"set_manual_clip_time","before":20,"after":30}  ← 시간 설정
    {"action":"manual_clip"}                                   ← 즉시 저장

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 4. 캡처  [기능: 캡처]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  engine.capture_frame(source, tc_tag="") → str (저장 경로)
    source : "screen" | "camera" | "both"
    tc_tag : TC 태그 문자열 (선택)

  AI Chat 액션:
    {"action":"capture","source":"screen","tc_tag":"TC001"}

  예시: CAN 조건 충족 시 캡처
    while not kernel.is_stopped():
        if can.can_read("BLTN_CAM_Ind_Req") == 2:
            engine.capture_frame("screen", "TC001")
            break
        kernel.wait(0.3)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 5. TC 결과  [기능: TC 검증]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  kernel.set_tc_result("PASS" | "FAIL")
  engine.tc_verify_result : str          현재 TC 결과

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 6. 전원 제어  [패널: 전원 제어 / BLTN]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  kernel.power_on(ch)         채널 ON
  kernel.power_off(ch)        채널 OFF
  kernel.power_all_off()      전체 OFF
  kernel.power_connected : bool  Arduino 연결 여부

  ch 값 (정확히 아래 키 사용):
    "bplus"     → B+ (배터리)         한국어: "배터리", "B+"
    "tg_bplus"  → TG B+ (타겟)        한국어: "타겟", "TG"
    "acc"       → ACC (악세서리)       한국어: "ACC", "악세서리"
    "ign"       → IGN (점화)           한국어: "IGN", "점화"
    "plbm_real" → PLBM 실물 릴레이    한국어: "실물", "실물릴레이"
    "plbm_sim"  → PLBM 모사 릴레이    한국어: "모사", "모사릴레이"
  ※ plbm_real/plbm_sim 상호 배타 — ON 시 반대쪽 자동 OFF

  AI Chat 액션:
    {"action":"power_on","channel":"bplus"}
    {"action":"power_off","channel":"acc"}
    {"action":"power_all_off"}
    {"action":"power_status"}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 7. OHCL 릴레이 제어  [패널: 전원 제어 / BLTN]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ★ BLTN 제어기 OHCL LED 연동 기능 (Arduino A1 릴레이)
  ★ MAR: relay_ms < 1300ms / TML: relay_ms >= 1300ms

  power.ohcl_trigger(relay_ms: int) → bool
    relay_ms: 100~9999 (ms). A1 릴레이를 해당 시간만큼 CLOSE.
    Arduino로 "A<ms>\n" 전송 → "K<ms>\n" 완료 수신.

  power.is_connected : bool    Arduino 연결 여부
  power._led_pwm_on : bool     BLTN LED PWM 현재 ON 여부 (A2 입력)
  power._led_half_period_ms : int  LED 반주기 ms (전체주기 = ×2)
  power._ohcl_last_event : dict   마지막 OHCL 이벤트 {type, relay_ms}

  AI Chat 액션:
    {"action":"ohcl_trigger","relay_ms":1250}   → MAR 녹화용 (< 1300ms)
    {"action":"ohcl_trigger","relay_ms":1350}   → TML 녹화용 (>= 1300ms)
    {"action":"ohcl_status"}                     → OHCL/LED 현재 상태

  예시: MAR 녹화 트리거 후 결과 기록
    power.ohcl_trigger(1250)          # MAR: 1250ms < 1300ms
    kernel.wait(2.0)
    engine.save_manual_clip()

  예시: TML 녹화 트리거
    power.ohcl_trigger(1350)          # TML: 1350ms >= 1300ms
    kernel.wait(2.0)
    engine.save_manual_clip()

  예시: LED PWM 점멸 확인 후 트리거
    while not kernel.is_stopped():
        if power._led_pwm_on:
            log(f"LED 반주기={power._led_half_period_ms}ms")
            power.ohcl_trigger(1250)
            break
        kernel.wait(0.1)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 8. ROI / OCR  [패널: ROI/OCR 관리]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  kernel.read_roi_text(N, source="screen") → str
  kernel.read_roi_number(N, source="screen") → float|None
  kernel.read_roi_value(N, source) → float|None
  kernel.roi_text_equals(N, val) → bool
  kernel.roi_number_compare(N, op, val) → bool
    N     : ROI 인덱스 (1-based, UI 번호 그대로)
    source: "screen" | "camera"
    op    : "==" | "!=" | ">" | ">=" | "<" | "<="

  AI Chat 액션:
    {"action":"roi_read_text","roi":1,"source":"screen"}
    {"action":"roi_read_number","roi":1,"source":"screen"}
    {"action":"roi_read_all","source":"screen"}
    {"action":"roi_set_no_rec","roi":1,"source":"screen","enabled":true}
    {"action":"roi_set_cond","roi":1,"source":"screen","cond_value":"2"}
    {"action":"roi_no_count","source":"screen"}
    {"action":"roi_open_folder","roi":1}
    {"action":"ocr_set","source":"screen","enabled":true}
    {"action":"brightness_set","source":"camera","enabled":false}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 9. CAN  [패널: CAN Monitor]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ━ CAN 읽기 (CANoe COM 폴링) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  can.can_read(sig_name) → float|None     실시간 신호값 (sig_cache 직접)
  can.can_read_all() → dict               전체 신호 snapshot
  can.add_poll_signal(sig_name)           폴링 신호 등록 (can_read 전 필수)
  can.load_dbc(path: str)                 DBC 파일 로드 (cantools 사용)

  ━ CAN 쓰기 방식 비교 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  방식 1: canoe_write() — CANoe COM 경유 (CAPL 신호 공간, 충돌 가능)
    can.canoe_write(msg_name, {sig: val}) → bool
    DBC 불필요. CANoe가 실행 중일 때만 사용 가능.
    ⚠ CAPL이 같은 신호를 주기적으로 Write하면 덮어써짐.

  방식 2: xl_write() — XL Driver 직접 → 물리 CAN 버스 (★ 권장)
    can.xl_connect(channel=0)             XL Driver 연결 (1회만 호출)
    can.xl_write(msg_name, {sig: val})    물리 버스에 직접 CAN 프레임 송신
    can.xl_disconnect()                   연결 해제 (프로그램 종료 시)
    can.xl_ready : bool                   연결 여부
    ✅ CANoe 실행 중 동시 사용 가능 (without init access)
    ✅ CAPL 신호 공간과 완전 분리 → 충돌 없음
    ✅ CANoe와 동일한 .dbc 파일 사용 (load_dbc() 필수)
    DBC 필수: load_dbc()로 CANoe와 동일한 .dbc 파일 먼저 로드.

  ★ xl_write 사용 순서:
    can.load_dbc("C:/path/CANoe_DB.dbc")  # CANoe DB와 동일한 파일
    can.xl_connect(channel=0)             # channel은 Vector HW Manager 기준
    can.xl_write("HU_BLTN_CAM_02_200ms", {"BLTN_CAM_Set_EV_BfrTime": 30})
    can.xl_disconnect()

  AI Chat 액션:
    {"action":"can_read","signal":"신호명"}
    {"action":"can_read_all"}
    {"action":"can_write","message":"메시지명","signal":"신호명","value":1}
    {"action":"can_status"}
    {"action":"load_dbc","path":"C:/path/file.dbc"}

  예시: CAN 조건 충족 시 녹화 중지
    can.add_poll_signal("BLTN_CAM_RecSta_OWD")
    engine.start_recording()
    t0 = time.time()
    while not kernel.is_stopped():
        if can.can_read("BLTN_CAM_RecSta_OWD") == 1: break
        if time.time() - t0 > 15:
            kernel.set_tc_result("FAIL"); break
        kernel.wait(0.3)
    engine.stop_recording()

  예시: xl_write로 HU 모사 신호 송신 (CAPL 충돌 없음)
    can.load_dbc("C:/Vector/CANdb/BLTN_CAM.dbc")
    ok = can.xl_connect(channel=0)
    if ok:
        can.xl_write("HU_BLTN_CAM_02_200ms", {
            "BLTN_CAM_Set_EV_BfrTime": 30,
            "BLTN_CAM_Set_EV_AftTime": 10,
        })
        log(f"xl_write 결과: {can.status_msg}")


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 10. 조건부 타이머  [패널: 조건부 타이머]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  timer.start()          시작
  timer.stop()           종료
  timer.lap() → float    랩 기록 + 경과시간(초) 반환
  timer.reset()          리셋 (종료 + 초기화)
  timer.elapsed : float  현재 경과시간(초)
  timer.running : bool   실행 중 여부

  AI Chat 액션:
    {"action":"timer_start"} / {"action":"timer_stop"}
    {"action":"timer_reset"} / {"action":"timer_lap"}
    {"action":"timer_status"}

  예시: 전원 천이 시간 측정
    kernel.power_on("acc")
    timer.start()
    can.add_poll_signal("BLTN_CAM_RecSta_OWD")
    while not kernel.is_stopped():
        if can.can_read("BLTN_CAM_RecSta_OWD") == 1:
            elapsed = timer.lap()
            log(f"ACC ON → CAN=1 천이 시간: {elapsed:.3f}초"); break
        kernel.wait(0.1)
    timer.stop()

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 11. 입/출력 장치 기록  [패널: 입/출력 장치 기록]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ★ 슬롯 번호 규칙 (중요):
    · 모든 슬롯 번호: 1-based (슬롯 1 = slot=1, 슬롯 2 = slot=2)

  engine.macro_start_run(repeat=1, gap=1.0, slot=None, wait=False)
      repeat : 반복 횟수 (0=무한)
      gap    : 반복 간 대기(초)
      slot   : 1-based (슬롯 1 = slot=1, 슬롯 2 = slot=2)
      ★ wait : True이면 실행 완료까지 블로킹 — 순차 실행 시 반드시 사용!
               False(기본)이면 비동기 — 즉시 리턴 (슬롯 바로 이어 실행 불가)
  engine.macro_stop_run()
  engine.macro_running : bool

  AI Chat 액션:
    {"action":"macro_run","slot":1,"repeat":1}
    {"action":"macro_run","slot":1,"repeat":3,"gap":2.0}
    {"action":"macro_stop"}

  ★★★ 슬롯 순차 실행 — 반드시 wait=True 사용 ★★★
  ⚠ wait=False(기본)로 연속 호출하면 슬롯 1이 끝나기 전에 슬롯 2가 호출되어
    macro_running==True 상태라 슬롯 2가 무시됨.

  예시: 슬롯 1 → 슬롯 2 순차 실행 (wait=True)
    engine.macro_start_run(slot=1, wait=True)   # 슬롯 1 완료까지 대기
    engine.macro_start_run(slot=2, wait=True)   # 슬롯 2 완료까지 대기
    log("모두 완료")

  예시: 슬롯 1 3회 반복 후 슬롯 2 실행
    engine.macro_start_run(repeat=3, slot=1, wait=True)
    engine.macro_start_run(slot=2, wait=True)

  예시: wait 없이 슬롯 1 완료 대기 패턴 (while 루프)
    engine.macro_start_run(slot=1)
    while not kernel.is_stopped():
        if not engine.macro_running: break
        kernel.wait(0.2)
    engine.macro_start_run(slot=2)

  예시: 슬롯 1을 3회 반복 (단독)
    engine.macro_start_run(repeat=3, gap=2.0, slot=1, wait=True)
    log("슬롯 1 완료")

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 12. 오토클릭  [패널: 오토클릭]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  engine.start_ac()             시작
  engine.stop_ac()              중단
  engine.reset_ac_count()       카운터 초기화
  engine.ac_interval : float    클릭 간격(초)
  engine.ac_count    : int      누적 클릭 횟수

  AI Chat 액션:
    {"action":"start_ac"} / {"action":"stop_ac"}

  예시: 50번 클릭 후 중지
    engine.ac_interval = 0.5
    engine.reset_ac_count(); engine.start_ac()
    while not kernel.is_stopped():
        if engine.ac_count >= 50:
            engine.stop_ac(); log("50회 완료"); break
        kernel.wait(0.1)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 13. 블랙아웃 판독  [패널: 블랙아웃 판독]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ★ 속성명 주의 — CoreEngine 실제 속성명을 정확히 사용할 것.
  engine.brightness_threshold : float  밝기 변화 임계값(기본 30.0) ← blackout_threshold 아님
  engine.blackout_rec_enabled : bool   블랙아웃 감지 ON/OFF        ← blackout_enabled 아님
  engine.blackout_cooldown    : float  쿨다운(초, 기본 5.0)

  AI Chat 액션:
    {"action":"blackout_set_threshold","value":25}    ← brightness_threshold 설정
    {"action":"blackout_set_enabled","enabled":true}  ← blackout_rec_enabled 설정
    {"action":"blackout_set_cooldown","value":7.0}    ← blackout_cooldown 설정

  ★ 커널 스크립트에서 직접 설정:
    engine.brightness_threshold = 25.0
    engine.blackout_rec_enabled = True
    engine.blackout_cooldown    = 7.0

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 13-b. 녹화 설정  [패널: 녹화]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  engine.buffer_seconds    : int    버퍼 크기(초, 기본 40)
  engine.actual_screen_fps : float  화면 캡처 FPS(기본 30.0)

  AI Chat 액션:
    {"action":"set_buffer_seconds","value":60}
    {"action":"set_screen_fps","value":25.0}

  ★ 커널 스크립트에서 직접 설정:
    engine.buffer_seconds    = 60
    engine.actual_screen_fps = 25.0

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

 14. 예약  [패널: 예약]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  지정 시각(절대·상대)에 자동으로 녹화 시작/종료를 예약.
  전원 예약은 schedule_power로 독립 처리.

  ━ schedule_add (녹화 예약) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  파라미터 (하나 이상 필수):
    start_after_minutes : 지금부터 N분 후 녹화 시작  ← 상대 시간 권장
    start_after_seconds : 지금부터 N초 후 녹화 시작
    stop_after_minutes  : 지금(또는 시작)부터 N분 후 녹화 종료
    stop_after_seconds  : 지금(또는 시작)부터 N초 후 녹화 종료
    start  : 절대 시각 "YYYY-MM-DD HH:MM:SS" (미래만 허용)
    stop   : 절대 시각 "YYYY-MM-DD HH:MM:SS"

  예시: "4분 뒤 녹화 시작"
    {"action":"schedule_add","start_after_minutes":4}

  예시: "5분 뒤 녹화 종료"
    {"action":"schedule_add","stop_after_minutes":5}

  예시: "4분 뒤 시작, 9분 뒤 종료"
    {"action":"schedule_add","start_after_minutes":4,"stop_after_minutes":9}

  예시: 절대 시각 예약
    {"action":"schedule_add","start":"2026-05-13 17:00:00","stop":"2026-05-13 17:30:00"}

  ━ schedule_power (전원 예약 — 녹화와 독립) ━━━━━━━━━━━━━━━━━━━━━━━━━━━
    pwr_on  : ON 채널 ("bplus","acc","ign","plbm_real","plbm_sim")
    pwr_off : OFF 채널 (위와 동일, "all" 가능)
    on_after_minutes  : N분 후 ON
    off_after_minutes : N분 후 OFF
    on_at  / off_at   : 절대 시각 "YYYY-MM-DD HH:MM:SS"

  예시: "2분 뒤 ACC ON"
    {"action":"schedule_power","pwr_on":"acc","on_after_minutes":2}

  예시: "10분 뒤 전체 전원 OFF"
    {"action":"schedule_power","pwr_off":"all","off_after_minutes":10}

  ━ 예약 관리 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {"action":"schedule_list"}              현재 예약 전체 조회
    {"action":"schedule_clear"}             전체 삭제
    {"action":"schedule_clear","id":1}      특정 ID 삭제

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 15. 스크립트 커널  [버튼: 🧠 커널 — 우측 상단 독립 창]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  AI Chat 액션:
    {"action":"kernel_run_all"}     커널 전체 스크립트 실행
    {"action":"kernel_stop"}        커널 실행 중지
    {"action":"kernel_status"}      커널 상태 조회
    {"action":"make_script","code":"파이썬코드"}  스크립트 생성 및 삽입

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 15. 메모리 / 시스템
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  AI Chat 액션:
    {"action":"memory_status"}
    {"action":"memory_compress"}
    {"action":"memory_clear"}
    {"action":"status"}             전체 프로그램 상태
    {"action":"chat","message":"답변"}   일반 대화

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 16. 환경 / 패키지
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  env_info()                          Python 버전·경로·패키지 확인
  pip_list()                          설치된 패키지 목록
  pip_install("numpy", "requests")    패키지 설치

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 ※ DB 동기화 정책
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  · UI 설정값 변경 → 1초 디바운스 후 DB 저장 (병목 방지)
  · 커널/AI Chat → DB 설정값 읽어 기능 실행
  · CAN 신호값  → DB 비저장. sig_cache 실시간 직접 읽기.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 ※ 스크립트 작성 공통 규칙
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  · 루프: while not kernel.is_stopped():
  · 대기: kernel.wait(초)  ⚠ time.sleep() 사용 금지
  · ROI 인덱스: 1-based (UI 번호 그대로)
  · CAN 값: DB 불가, sig_cache 실시간 → can.can_read() 사용
  · 코드 줄바꿈 (AI Chat make_script): \\n 사용
  · ★ 들여쓰기: \\n 뒤에 스페이스 4개로 표현 (예: if x:\\n    body)
    들여쓰기 없으면 자동 복원되지만 정확히 쓸 것.
  · 2중 들여쓰기 (while 안의 if 등): \\n 뒤 스페이스 8개
"""

# =============================================================================
#  CanBusManager
# =============================================================================
# =============================================================================
#  CanBusManager — CAN/LIN 버스 관리자 (python-can 기반)
# =============================================================================
