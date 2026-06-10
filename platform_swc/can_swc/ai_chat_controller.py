# -*- coding: utf-8 -*-
"""
can_swc/ai_chat_controller.py
LLM 기반 AI 채팅 컨트롤러 — Groq / Gemini / Claude API 지원.

설계 원칙
 · §6.3: AI Chat은 _SYS_STATIC API 명세를 읽고 커널 스크립트 생성 가능
 · §7.2: user_notes 반영으로 사용자 교육 내용을 동적 프롬프트에 주입
 · CoreEngine/KernelEngine 접근은 반드시 public 메서드 경유 (§3.2)
"""
import os, sys, json, time, threading, re
from typing import Optional, List, Dict, Any

try:
    import groq as _groq_lib; GROQ_AVAILABLE = True
except ImportError:
    _groq_lib = None; GROQ_AVAILABLE = False
try:
    import google.genai as _genai; GEMINI_AVAILABLE = True
except ImportError:
    _genai = None; GEMINI_AVAILABLE = False
try:
    import anthropic as _anthropic_lib; CLAUDE_AVAILABLE = True
except ImportError:
    _anthropic_lib = None; CLAUDE_AVAILABLE = False

from PyQt5.QtCore import QObject, pyqtSignal
from common.signals import Signals
from can_swc.make_script_helpers import make_script

# =============================================================================
#  GeminiChatController — Google Gemini API 기반 AI 컨트롤러
# =============================================================================
_SYS_STATIC = """\
당신은 Screen & Camera Recorder 프로그램의 AI 비서입니다.
사용자 요청을 분석해 반드시 JSON 형식 하나만 출력하세요. 다른 텍스트 절대 금지.

【현재 시각 활용 규칙】
- 현재 시각은 매 대화마다 시스템 프롬프트에 제공됨 (【현재 시각】 섹션 참고).
- "N분 뒤", "M초 후" 등 상대 시간 → start_after_minutes / stop_after_minutes 파라미터 사용.
- 현재 시각을 직접 계산해서 절대 시각으로 변환 금지 (계산 오류 방지).
- 복합 시나리오 스크립트 작성 시 kernel.wait(초) 사용, time.sleep() 금지.

【복합 시나리오 스크립트 예시】
# 예: "10분 뒤 녹화 시작, 5분 뒤 오토클릭 시작, 1분 뒤 오토클릭 중지,
#      수동녹화 -20~+30, 수동녹화 실행, 수동녹화 끝나면 녹화 종료,
#      5초 뒤 전원 전부 OFF, 20초 뒤 B+/TG B+/ACC/IGN/실물 PLBM ON,
#      다시 녹화 시작, 20초 뒤 ACC/IGN OFF,
#      10초 뒤 BLTN_CAM_RecSta_OWP==1 이면 PASS 처리 후 녹화 종료"
# → make_script 액션으로 아래와 같은 Python 커널 스크립트 생성:
#
# kernel.wait(600.0)                              # 10분 대기
# engine.start_recording()
# kernel.wait(300.0)                              # 5분 대기
# engine.start_ac()
# kernel.wait(60.0)                               # 1분 대기
# engine.stop_ac()
# engine.set_manual_time(20, 30)    # 앞 20초, 뒤 30초 (설정+UI동기 동시)
# engine.save_manual_clip()
# kernel.wait_manual_clip(timeout=70)  # post(30)+츨랑 완료 대기
# engine.stop_recording()
# # ⚠️ while not engine.recording 패턴 사용 금지 — 수동녹화 완료를 감지하지 않음
# kernel.wait(5.0)
# kernel.power_all_off()
# kernel.wait(20.0)
# for ch in ["bplus","tg_bplus","acc","ign","plbm_real"]:
#     kernel.power_on(ch)
# engine.start_recording()
# kernel.wait(20.0)
# kernel.power_off("acc"); kernel.power_off("ign")
# kernel.wait(10.0)
# can.add_poll_signal("BLTN_CAM_RecSta_OWP")
# t1 = time.time()                                    # import 불필요, time은 이미 주입됨
# while not kernel.is_stopped():
#     if time.time() - t1 > 30: kernel.set_tc_result("FAIL"); break
#     if can.can_read("BLTN_CAM_RecSta_OWP") == 1:
#         kernel.set_tc_result("PASS"); break
#     kernel.wait(0.3)
# engine.stop_recording()


  전원 제어 / BLTN | 녹화 | 수동녹화 | 블랙아웃 판독 | ROI/OCR 관리
  조건부 타이머 | 입/출력 장치 기록 | 예약 | 메모(화면 오버레이) | CAN Monitor
  스크립트 커널 (🧠 커널 버튼 — 우측 상단 독립 창)
【절대 규칙】
1. ★★★ 아래 【허용 액션 목록】에 없는 action 키 절대 사용 금지. 지어내지 말 것. ★★★
2. 프로그램 무관 일반 대화/질문 → chat 액션으로 답변.
3. CAN 신호값 질문 → can_read 액션 사용.
4. ★★★ "스크립트 짜줘" / "코드 만들어줘" / "자동화해줘" 등 스크립트 작성 요청
   → 반드시 make_script 액션 사용. macro_run 등 즉시 실행 액션 절대 사용 금지. ★★★
   예) "슬롯 1 실행하는 스크립트 짜줘" → make_script 액션에 Python 코드 포함.
   예) "슬롯 1 실행해줘" (실행 요청) → macro_run 액션 사용. (이 경우만 직접 실행)
5. ★★★ "시각에 녹화 예약", "~시에 예약", "~분에 녹화 시작/종료" 등 미래 시각 예약 요청
   → 반드시 schedule_add 액션 사용. start_recording 직접 호출 절대 금지. ★★★
   ★ 상대 시간("4분 뒤", "30초 후" 등) → start_after_minutes / start_after_seconds 파라미터 사용
   ★ 절대 시각("17:00에", "2026-05-13 17:00:00") → start 파라미터 사용
   예) "4분 뒤 녹화 시작" → {"action":"schedule_add","start_after_minutes":4}
   예) "5분 뒤 녹화 종료" → {"action":"schedule_add","stop_after_minutes":5}
   예) "4분 뒤 시작, 5분 뒤 종료" → {"action":"schedule_add","start_after_minutes":4,"stop_after_minutes":5}
   예) "17:00:00에 녹화 예약" → {"action":"schedule_add","start":"2026-05-13 17:00:00"}
   예) "지금 바로 녹화해줘" → start_recording 사용 (즉시 실행)
   ★ 전원 예약(녹화 없이 전원만) → schedule_power 액션 사용 (schedule_add와 독립)
5. 요청 로직을 존재하는 함수만으로 구현. 불가능/애매하면
   스크립트 먼저 작성 후 "이렇게 이해했는데 맞나요?" 되물을 것.
6. 수동녹화 시간 설정 → set_manual_clip_time 액션 (AI Chat) / engine.set_manual_time(pre, post) (커널).
   engine.manual_pre_sec = N 직접 대입 금지 (UI 미반영).
   수동녹화 완료 대기: kernel.wait_manual_clip(timeout) 사용.
   ⚠️ while not engine.recording 패턴으로 수동녹화 완료 감지 불가 — recording은 메인 녹화 상태.
7. plbm_real / plbm_sim ON → power_on 액션 1번만. 반대쪽 OFF 먼저 호출 금지.
8. CAN 신호값은 실시간 sig_cache에서만 읽음. DB 저장 불가. can_read 사용.
9. OHCL 트리거: relay_ms < 1300 → MAR, relay_ms >= 1300 → TML.
10. ★ 매크로 슬롯 번호: 모두 1-based (슬롯 1 = slot=1, 0-based 절대 사용 금지)
    - AI Chat: {"action":"macro_run","slot":1} → 슬롯 1 실행
    - 커널 스크립트: engine.macro_start_run(slot=1) → 슬롯 1
11. ★★★ 매크로 순차 실행 규칙 (매우 중요) ★★★
    macro_start_run은 기본 비동기 — 즉시 리턴. 연속 호출 시 두 번째가 무시됨.
    슬롯 1 후 슬롯 2 등 순차 실행 → 반드시 wait=True 사용:
      engine.macro_start_run(slot=1, wait=True)
      engine.macro_start_run(slot=2, wait=True)
    "슬롯 1 실행 후 슬롯 2 실행" 스크립트 요청 → 위 패턴(wait=True)으로 생성.

【engine API (커널 스크립트 + AI Chat)】
engine.start_recording() / stop_recording() / recording: bool
engine.capture_frame(source, tc_tag="") → str
engine.save_manual_clip() → bool
engine.set_manual_time(pre_sec, post_sec)
  ★ 수동녹화 시간 설정 + UI 동기화. 반드시 이 메서드 사용.
  ★ engine.manual_pre_sec = N 직접 대입 금지 (UI 미반영)
  예) engine.set_manual_time(20, 30)  # 앞 20초, 뒤 30초
engine.manual_source  (str: "screen"|"camera"|"both")

【power API (커널 스크립트)】
power.ohcl_trigger(relay_ms)   OHCL 릴레이 트리거 (100~9999ms)
  relay_ms < 1300 → MAR,  relay_ms >= 1300 → TML
  예) power.ohcl_trigger(1250)  # MAR 트리거
power.channel_on(ch)  / power.channel_off(ch)   채널 직접 ON/OFF
power.is_connected : bool
★ 커널 스크립트에서 power 객체 사용 가능 (v5.21부터 _globals에 주입)

【CAN 주의사항】
★ can.can_read(sig) 가 None 반환 시 → 신호명 대소문자 불일치 또는 폴링 미등록
  반드시 DBC 파일의 정확한 신호명 사용 (대소문자 구분)
  예) BLTN_CAM_Ind_Req (대문자 I,R) ≠ BLTN_CAM_ind_req (소문자)
  None 방어 패턴:
    val = can.can_read('SIG_NAME')
    if val is None:
        log("⚠️ SIG_NAME 신호 미수신 — 신호명/연결 확인")
    elif val == 1:
        kernel.set_tc_result('PASS')
engine.start_ac() / stop_ac() / ac_interval / ac_count / reset_ac_count()
engine.macro_start_run(repeat=1, gap=1.0, slot=None, wait=False)
  ★ slot: 1-based (슬롯 1 = slot=1, 슬롯 2 = slot=2)
  ★★★ 순차 실행(슬롯1 후 슬롯2)은 반드시 wait=True 사용 ★★★
      wait=False(기본)는 비동기 — 연속 호출 시 두 번째 슬롯이 무시됨
  순차 예시: engine.macro_start_run(slot=1, wait=True)
             engine.macro_start_run(slot=2, wait=True)
engine.macro_stop_run()
engine.macro_running : bool
engine.brightness_threshold / blackout_rec_enabled / blackout_cooldown  ← 실제 속성명
engine.tc_verify_result / output_dir / base_dir

【kernel API (커널 스크립트 전용)】
kernel.wait(seconds)                슬립+중단감지  ⚠ time.sleep() 금지
kernel.is_stopped() → bool          중단 여부
kernel.set_tc_result("PASS"|"FAIL")
kernel.emit_status(msg)
kernel.read_roi_text(N, source="screen") / read_roi_number(N, source) → float|None
kernel.read_roi_value(N, source) → float|None
kernel.roi_text_equals(N, val) → bool / roi_number_compare(N, op, val) → bool
kernel.power_on(ch) / power_off(ch) / power_all_off()
kernel.power_connected : bool
  ch: "bplus"|"tg_bplus"|"acc"|"ign"|"plbm_real"|"plbm_sim"
  ★ "실물 릴레이" → "plbm_real" / "모사 릴레이" → "plbm_sim"
  ※ plbm_real/plbm_sim 상호 배타 — ON 시 반대쪽 자동 OFF

【power API (커널 스크립트 전용) — 전원 제어 / BLTN 패널】
power.ohcl_trigger(relay_ms: int) → bool
  relay_ms: 100~9999ms. A1 릴레이 CLOSE. < 1300 → MAR, >= 1300 → TML.
power.is_connected : bool
power._led_pwm_on : bool            BLTN LED PWM ON 여부 (A2 입력)
power._led_half_period_ms : int     LED 반주기 ms
power._ohcl_last_event : dict       마지막 OHCL 이벤트 {type, relay_ms}

【can API (커널 스크립트 전용)】
can.can_read(sig_name) → float|None / can.can_read_all() → dict
can.canoe_write(msg_name, {sig: val}) → bool
can.add_poll_signal(sig_name, msg_name="")
can.load_dbc(path: str)

【timer API (커널 스크립트 전용)】
timer.start() / stop() / lap() → float / reset()
timer.elapsed : float / timer.running : bool

【허용 액션 목록 — 이 외 action 절대 사용 금지】
── 녹화 ──
{"action":"start_recording"}
{"action":"stop_recording","result":"PASS"}
{"action":"capture","source":"screen","tc_tag":""}
{"action":"manual_clip"}
{"action":"set_manual_clip_time","before":15,"after":20}
── 예약 (★ 시각 지정 녹화 요청은 반드시 schedule_add 사용) ──
{"action":"schedule_add","start_after_minutes":4}              ← 4분 뒤 녹화 시작
{"action":"schedule_add","stop_after_minutes":5}               ← 5분 뒤 녹화 종료
{"action":"schedule_add","start_after_minutes":4,"stop_after_minutes":9}
{"action":"schedule_add","start":"YYYY-MM-DD HH:MM:SS"}        ← 절대 시각
{"action":"schedule_add","start":"YYYY-MM-DD HH:MM:SS","stop":"YYYY-MM-DD HH:MM:SS"}
{"action":"schedule_power","pwr_on":"acc","on_after_minutes":2}  ← 전원만 예약(녹화 독립)
{"action":"schedule_power","pwr_off":"all","off_at":"YYYY-MM-DD HH:MM:SS"}
{"action":"schedule_list"}
{"action":"schedule_clear"}
{"action":"schedule_clear","id":1}
── 오토클릭 / 매크로 ──
{"action":"start_ac"} / {"action":"stop_ac"}
{"action":"macro_run","slot":1,"repeat":1} / {"action":"macro_stop"}
── ROI/OCR ──
{"action":"roi_read_text","roi":1,"source":"screen"}
{"action":"roi_read_number","roi":1,"source":"screen"}
{"action":"roi_read_all","source":"screen"}
{"action":"roi_set_no_rec","roi":1,"source":"screen","enabled":true}
{"action":"roi_set_cond","roi":1,"source":"screen","cond_value":"2"}
{"action":"roi_no_count","source":"screen"}
{"action":"roi_open_folder","roi":1}
{"action":"ocr_set","source":"screen","enabled":true}
{"action":"brightness_set","source":"camera","enabled":false}
── 전원 제어 ──
{"action":"power_on","channel":"bplus"}
{"action":"power_off","channel":"bplus"}
{"action":"power_all_off"} / {"action":"power_status"}
{"action":"ohcl_trigger","relay_ms":1250}
{"action":"ohcl_status"}
── CAN ──
{"action":"can_read","signal":"신호명"}
{"action":"can_read_all"}
{"action":"can_write","message":"메시지명","signal":"신호명","value":1}
{"action":"can_status"}
{"action":"load_dbc","path":"C:/path/file.dbc"}
── 타이머 ──
{"action":"timer_start"} / {"action":"timer_stop"}
{"action":"timer_reset"} / {"action":"timer_lap"} / {"action":"timer_status"}
── 블랙아웃 ──
{"action":"blackout_set_threshold","value":30}
{"action":"blackout_set_enabled","enabled":true}
── 커널 / 메모리 / 기타 ──
{"action":"kernel_run_all"} / {"action":"kernel_stop"} / {"action":"kernel_status"}
{"action":"memory_status"} / {"action":"memory_compress"} / {"action":"memory_clear"}
{"action":"make_script","code":"python코드"}
{"action":"status"}
{"action":"chat","message":"답변"}

【스크립트 작성 규칙】
- 루프: while not kernel.is_stopped():
- 대기: kernel.wait(초)  ⚠ time.sleep() 금지
- ROI 인덱스: 1-based
- CAN값: can.can_read() 사용 (DB 불가)
- 코드 줄바꿈: \\n 사용. 반드시 각 문장을 \\n 으로 구분. 세미콜론으로 연결 금지.
- ★ 예약은 커널 스크립트가 아닌 schedule_add 액션 사용
- ★★ import 금지 목록 (이미 전역에 주입됨):
    import time        → 금지. t0 = time.time() 으로 바로 사용
    import os          → 금지. os.path 바로 사용
    import sys/re/json/threading → 금지
  ⚠ 별칭 임포트도 금지: import time as _t2 → 금지
- ★★★ 타임아웃 루프 — 반드시 아래 패턴 그대로 사용 (절대 변형 금지):
    t0 = time.time()\\n
    while not kernel.is_stopped():\\n
        if time.time() - t0 > 30:\\n
            kernel.set_tc_result('FAIL')\\n
            break\\n
        if can.can_read('신호명') == 1:\\n
            kernel.set_tc_result('PASS')\\n
            break\\n
        kernel.wait(0.3)
  ⚠ 위 패턴에서 절대 금지:
    - import time / import time as _t 삽입 금지
    - 세미콜론으로 if-break 한 줄 연결 금지: if x: break  → OK,  if x: y(); break → OK
    - while 줄과 if 줄 사이 줄바꿈(\\n) 누락 금지
"""

# _SYS는 AIChatController._build_sys_prompt()가 동적으로 생성.
# 하위 호환용으로 정적 버전도 남겨둠.
_SYS = _SYS_STATIC


_AI_PROVIDERS = {
    "groq":   {"label": "Groq (무료)",       "models": ["llama-3.1-8b-instant", "llama-3.3-70b-versatile", "gemma2-9b-it"]},
    "gemini": {"label": "Google Gemini",     "models": ["gemini-2.0-flash", "gemini-1.5-flash"]},
    "claude": {"label": "Anthropic Claude",  "models": ["claude-haiku-4-5", "claude-3-5-haiku-20241022"]},
}

class AIChatController(QObject):
    """Groq / Gemini / Claude 통합 AI 컨트롤러."""
    reply_ready   = pyqtSignal(str, bool)
    status_update = pyqtSignal(str)

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self._engine   = engine
        self._power    = None
        self._can      = _can_manager
        self._kernel   = None   # KernelEngine (MainWindow에서 주입)
        self._history  = []     # Layer 3: 단기 대화 (최근 6턴)
        self._lock     = threading.Lock()
        self._provider = "groq"
        self._model    = "llama-3.1-8b-instant"
        self._api_key  = ""
        self._client   = None

        # ── 3-레이어 메모리 ────────────────────────────────────────────
        # Layer 1: 장기기억 — 압축된 대화 요약 (ai_knowledge.json에 저장)
        self._long_term_summary: str = ""   # 압축 요약 문자열
        self._long_term_tokens:  int = 0    # 추정 토큰 수
        # Layer 2: 동적 상태 — _build_sys_prompt()가 매번 생성
        # Layer 3: 단기 히스토리 — self._history (최대 6턴=12개 항목)

        # 토큰 경고 임계값
        self._TOKEN_WARN  = 2500   # 이 이상이면 사용자에게 경고
        self._TOKEN_COMPRESS = 3000  # 이 이상이면 자동 압축
        self._last_token_est = 0   # 마지막 요청의 추정 토큰 수

        # 장기기억 로드 (시작 시 ai_knowledge.json에서 복원)
        self._load_long_term_memory()

    # ── Layer 1: 장기기억 관리 ────────────────────────────────────────────
    def _knowledge_path(self) -> str:
        base = getattr(self._engine, 'base_dir',
                       os.path.join(os.path.expanduser("~/Desktop"), "bltn_rec"))
        return os.path.join(base, "ai_knowledge.json")

    def _load_long_term_memory(self):
        """시작 시 ai_knowledge.json에서 장기기억 복원."""
        try:
            path = self._knowledge_path()
            if not os.path.isfile(path):
                return
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            summary = data.get("long_term_summary", "")
            if summary:
                self._long_term_summary = summary
                self._long_term_tokens  = self._estimate_tokens(summary)
        except Exception:
            pass

    def _save_long_term_memory(self):
        """장기기억을 ai_knowledge.json에 저장.
        ★ 대화 이력(요청/응답 요약)도 누적 로그로 기록 — 사용자가 무엇을 물었는지 파악 가능.
        """
        try:
            path = self._knowledge_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = {}
            if os.path.isfile(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            data["long_term_summary"] = self._long_term_summary
            data["_summary_updated"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # ★ 최근 대화 요약 로그 누적 (최대 50개 유지)
            interaction_log = data.get("interaction_log", [])
            with self._lock:
                history_snapshot = list(self._history[-4:])  # 최근 2턴
            for msg in history_snapshot:
                role  = msg.get("role", "")
                parts = msg.get("parts", [])
                text  = parts[0] if parts else ""
                if not text: continue
                entry = {
                    "ts":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "role": role,
                    "text": text[:300]   # 300자 요약
                }
                # 중복 방지
                if not interaction_log or interaction_log[-1].get("text") != entry["text"]:
                    interaction_log.append(entry)
            if len(interaction_log) > 50:
                interaction_log = interaction_log[-50:]
            data["interaction_log"] = interaction_log

            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """
        토큰 수 추정 (실제 토크나이저 없이 근사).
        한글: 1자 ≈ 1.5토큰, 영문: 1단어 ≈ 1.3토큰.
        """
        korean_chars = sum(1 for c in text if '\uAC00' <= c <= '\uD7A3')
        other_words  = len(text.split()) - korean_chars
        return int(korean_chars * 1.5 + max(0, other_words) * 1.3)

    def _compress_history_to_long_term(self):
        """
        단기 히스토리(Layer 3)를 요약해서 장기기억(Layer 1)으로 이동.
        요약은 LLM API를 사용하지 않고 규칙 기반으로 수행 (API 비용 절약).
        """
        with self._lock:
            if not self._history:
                return
            # 대화에서 실행된 액션 / 사용자 요청 키워드 추출
            actions_done = []
            user_topics  = []
            for h in self._history:
                role  = h.get("role", "")
                parts = h.get("parts", [])
                text  = parts[0] if parts else ""
                if role == "user" and text:
                    # 핵심 키워드만 (앞 30자)
                    user_topics.append(text[:30].replace("\n", " "))
                elif role == "model" and text:
                    # 완료된 액션 추출
                    if "✅" in text:
                        actions_done.append(text[:50].replace("\n", " "))

            summary_parts = []
            if actions_done:
                summary_parts.append("【완료된 작업】" + " / ".join(actions_done[-5:]))
            if user_topics:
                summary_parts.append("【이전 대화 주제】" + " / ".join(user_topics[-5:]))

            new_summary = "\n".join(summary_parts)
            # 기존 장기기억에 누적 (오래된 것은 앞쪽으로 밀어내고 최신 유지)
            combined = (self._long_term_summary + "\n" + new_summary).strip()
            # 장기기억도 너무 길어지면 앞부분 잘라냄 (최대 800토큰)
            while self._estimate_tokens(combined) > 800:
                lines = combined.split("\n")
                combined = "\n".join(lines[max(1, len(lines)//4):])
            self._long_term_summary = combined
            self._long_term_tokens  = self._estimate_tokens(combined)
            self._history.clear()  # 단기 히스토리 초기화

        self._save_long_term_memory()

    def _check_token_pressure(self, sys_prompt: str, history_copy: list) -> int:
        """
        현재 요청의 총 추정 토큰 계산.
        반환: 추정 토큰 수
        """
        total = self._estimate_tokens(sys_prompt)
        for h in history_copy:
            parts = h.get("parts", [])
            total += self._estimate_tokens(parts[0] if parts else "")
        return total

    def set_power(self, power_ctrl):
        self._power = power_ctrl

    def _build_sys_prompt(self) -> str:
        """
        실행 시점의 프로그램 상태를 반영한 동적 시스템 프롬프트 생성.

        포함 내용:
          1. 정적 API 명세 (_SYS_STATIC)
          2. 현재 등록된 ROI 목록 (이름·소스·OCR 조건·NO→REC 상태)
          3. 현재 핵심 설정값 (버퍼, pre/post, 저장경로, FPS 등)
          4. 전원 제어 연결 상태
          5. CAN 연결 상태 + 로드된 신호 목록 (최대 40개)
          6. 타이머 패널 상태
          7. ai_knowledge.json 의 user_notes (사용자 추가 교육 내용)
        """
        e   = self._engine
        can = self._can
        lines = [_SYS_STATIC]

        # ── 0. 현재 시각 (예약/상대 시간 계산용) ────────────────────────
        from datetime import datetime as _dt_now
        _now = _dt_now.now()
        lines.append(
            f"\n【현재 시각】{_now.strftime('%Y-%m-%d %H:%M:%S')}  "
            f"(예약 시 이 시각 기준으로 상대 시간 계산)")


        scr_rois = getattr(e, 'screen_rois', []) or []
        cam_rois = getattr(e, 'camera_rois', []) or []
        if scr_rois or cam_rois:
            lines.append("\n【현재 등록된 ROI】")
            for src, rois in (("screen", scr_rois), ("camera", cam_rois)):
                for i, roi in enumerate(rois):
                    cond = f" | OCR조건={roi.cond_value!r}" if roi.cond_value else ""
                    norec = f" | NO→REC={'ON' if roi.roi_no_rec_enabled else 'OFF'}" \
                            if roi.cond_value else ""
                    cnt = f" (NO횟수:{roi.no_match_count})" if roi.no_match_count else ""
                    lines.append(
                        f"  ROI {i+1} [{src}] 이름={roi.label()!r}"
                        f" 위치=({roi.x},{roi.y}) 크기={roi.w}×{roi.h}"
                        f"{cond}{norec}{cnt}"
                        f" OCR최근값={roi.last_text!r}"
                    )
        else:
            lines.append("\n【현재 등록된 ROI】없음 (ROI 관리 패널에서 추가 필요)")

        # ── 2. 현재 설정값 ───────────────────────────────────────────────
        lines.append("\n【현재 프로그램 설정값】")
        lines.append(f"  녹화중={getattr(e,'recording',False)}")
        lines.append(f"  버퍼={getattr(e,'buffer_seconds',40)}초")
        lines.append(f"  수동녹화 pre={getattr(e,'manual_pre_sec',10)}초"
                     f" / post={getattr(e,'manual_post_sec',10)}초"
                     f" / 소스={getattr(e,'manual_source','both')}")
        lines.append(f"  화면FPS={getattr(e,'actual_screen_fps',30)}"
                     f"  카메라FPS={getattr(e,'actual_camera_fps',30)}")
        lines.append(f"  저장경로={getattr(e,'base_dir','?')}")
        out = getattr(e, 'output_dir', '')
        if out:
            lines.append(f"  현재출력경로={out}")
        lines.append(f"  Display OCR={getattr(e,'ocr_screen_enabled',True)}"
                     f"  Camera OCR={getattr(e,'ocr_camera_enabled',True)}")
        lines.append(f"  Display밝기={getattr(e,'brightness_screen_enabled',True)}"
                     f"  Camera밝기={getattr(e,'brightness_camera_enabled',True)}")
        lines.append(f"  블랙아웃녹화={getattr(e,'blackout_rec_enabled',True)}"
                     f"  밝기임계값={getattr(e,'brightness_threshold',30)}")

        # TC 설정
        tc_id = getattr(e, 'tc_id', '') or ''
        if tc_id:
            lines.append(f"  TC-ID={tc_id}"
                         f" TC녹화={getattr(e,'tc_rec_enabled',False)}"
                         f" TC수동녹화={getattr(e,'tc_manual_enabled',False)}")

        # ── 3. 전원 제어 상태 ────────────────────────────────────────────
        try:
            pwr = self._power
            if pwr:
                conn = getattr(pwr, 'is_connected', False)
                port = getattr(pwr, 'port', '')
                lines.append(f"\n【전원 제어】연결={'✅ ' + port if conn else '❌ 미연결'}")
                if conn:
                    ch_state = getattr(pwr, '_ch_state', {})
                    _CH_KR = {
                        "bplus": "B+(배터리)", "tg_bplus": "TG B+",
                        "acc": "ACC", "ign": "IGN",
                        "plbm_real": "PLBM실물(plbm_real)",
                        "plbm_sim":  "PLBM모사(plbm_sim)",
                    }
                    st = " / ".join(
                        f"{_CH_KR.get(ch, ch.upper())}={'ON' if v else 'OFF'}"
                        for ch, v in ch_state.items()
                        if ch in _CH_KR
                    )
                    lines.append(f"  채널상태: {st}")
                    lines.append("  ※ 전원ON/OFF 요청 시 위 채널키(plbm_real 등)를 channel 파라미터에 사용")
            else:
                lines.append("\n【전원 제어】미초기화")
        except Exception as _pwr_err:
            lines.append(f"\n【전원 제어】상태 읽기 오류: {_pwr_err}")

        # ── 4. CAN 상태 + 신호 목록 ─────────────────────────────────────
        try:
          if can:
            can_conn = getattr(can, 'connected', False)
            canoe = getattr(can, 'canoe_mode', False)
            lines.append(f"\n【CAN】연결={'✅' if can_conn else '❌'}"
                         f" CANoe모드={canoe}")
            # DBC 로드된 신호 목록 (최대 40개, 이름만)
            all_sigs = getattr(can, '_all_sigs_ref', None) or []
            if all_sigs:
                sig_names = [s['name'] for s in all_sigs[:40]]
                lines.append(f"  로드된신호({len(all_sigs)}개 중 앞40개): "
                              + ", ".join(sig_names))
                if len(all_sigs) > 40:
                    lines.append(f"  ... 외 {len(all_sigs)-40}개 (can_read_all로 전체 조회 가능)")
            # 최근 캐시값
            sig_cache = getattr(can, 'sig_cache', {})
            if sig_cache:
                recent = list(sig_cache.items())[-10:]  # 최근 10개
                cache_parts = []
                for k, v in recent:
                    # ★ 방어: v가 (ts, val) 튜플인 경우만 처리
                    # _PREV_, _DIAG_, ev_list 등 다른 형식은 건너뜀
                    try:
                        if isinstance(v, (list, tuple)) and len(v) >= 2:
                            val = v[1]
                            if val is not None and not k.startswith("_"):
                                cache_parts.append(f"{k}={val}")
                    except (TypeError, IndexError):
                        pass
                if cache_parts:
                    lines.append("  최근캐시값(최대10): " + ", ".join(cache_parts))
          else:
            lines.append("\n【CAN】미초기화")
        except Exception as _can_err:
            lines.append(f"\n【CAN】상태 읽기 오류: {_can_err}")

        # ── 5. 타이머 상태 ───────────────────────────────────────────────
        try:
            timer_state = getattr(e, '_timer_state_cache', None)
            if timer_state:
                lines.append(f"\n【타이머】실행중={timer_state.get('running',False)}"
                             f" 경과={timer_state.get('elapsed_str','00:00:00.000')}")
            else:
                lines.append("\n【타이머】상태 미확인 (timer_state_cache 없음)")
        except Exception as _tmr_err:
            lines.append(f"\n【타이머】상태 읽기 오류: {_tmr_err}")

        # ── 6. 예약 목록 ─────────────────────────────────────────────────
        try:
          schedules = getattr(e, 'schedules', [])
          if schedules:
            from datetime import datetime as _dt
            _FMT = "%Y-%m-%d %H:%M:%S"
            sched_lines = [f"\n【현재 예약 목록 ({len(schedules)}개)】"]
            for s in schedules[:10]:   # 최대 10개 표시
                s_str = s.start_dt.strftime(_FMT) if s.start_dt else "—"
                t_str = s.stop_dt.strftime(_FMT)  if s.stop_dt  else "—"
                stat  = "완료" if s.done else ("실행중" if s.started else "대기")
                sched_lines.append(
                    f"  ID:{s.id} 시작:{s_str} 종료:{t_str} "
                    f"동작:{'+'.join(s.actions)} [{stat}]")
            lines.extend(sched_lines)
          else:
            lines.append("\n【현재 예약 목록】없음")
        except Exception as _sch_err:
            lines.append(f"\n【예약목록】상태 읽기 오류: {_sch_err}")

        # ── 6. Layer 1 장기기억 (압축 요약) ────────────────────────────
        if self._long_term_summary:
            lines.append(
                f"\n【AI 장기기억 (이전 대화 압축 요약)】\n"
                f"{self._long_term_summary}")

        # ── 7. user_notes (ai_knowledge.json) ───────────────────────────
        try:
            knowledge_path = self._knowledge_path()
            if os.path.isfile(knowledge_path):
                with open(knowledge_path, 'r', encoding='utf-8') as f:
                    kdata = json.load(f)
                notes = kdata.get("user_notes", "")
                if notes and "※ 이 섹션에" not in notes and len(notes.strip()) > 10:
                    lines.append(
                        f"\n【사용자 추가 교육 내용 (ai_knowledge.json)】\n"
                        f"{notes[:600]}")
        except Exception:
            pass

        # ── 8. OHCL / LED 실시간 상태 추가 (동적) ─────────────────────
        pwr = self._power
        if pwr and pwr.is_connected:
            led_on  = getattr(pwr, "_led_pwm_on", False)
            half_ms = getattr(pwr, "_led_half_period_ms", 0)
            last_ev = getattr(pwr, "_ohcl_last_event", {})
            ohcl_lines = ["\n【OHCL / LED PWM 실시간 상태】"]
            ohcl_lines.append(
                f"  LED PWM: {'ON' if led_on else 'OFF'}"
                + (f"  반주기={half_ms}ms 전체주기={half_ms*2}ms" if led_on and half_ms else ""))
            if last_ev:
                lms = last_ev.get("relay_ms", 0)
                lt  = last_ev.get("type", "?")
                rec = "TML" if lms >= 1300 else "MAR"
                ohcl_lines.append(
                    f"  마지막 OHCL 이벤트: type={lt} {lms}ms → {rec}")
            ohcl_lines.append(
                "  ★ OHCL 트리거: {\"action\":\"ohcl_trigger\",\"relay_ms\":1250}  "
                "← relay_ms < 1300 → MAR / >= 1300 → TML")
            ohcl_lines.append(
                "  ★ OHCL 상태:   {\"action\":\"ohcl_status\"}")
            lines.extend(ohcl_lines)

        return "\n".join(lines)

    def set_provider(self, provider: str, model: str, api_key: str):
        """제공자/모델/키 설정 및 연결 테스트."""
        self._provider = provider
        self._model    = model
        self._api_key  = api_key.strip()
        self._client   = None

        if not self._api_key:
            self.status_update.emit("⚠️ API 키를 입력해주세요.")
            return

        if provider == "groq":
            if not GROQ_AVAILABLE:
                self.status_update.emit("❌ groq 미설치\npip install groq")
                return
            try:
                self._client = _groq_lib.Groq(api_key=self._api_key)
                self.status_update.emit(f"✅ Groq 연결 완료 ({model})")
            except Exception as ex:
                self.status_update.emit(f"❌ Groq 초기화 실패: {ex}")

        elif provider == "gemini":
            if not GEMINI_AVAILABLE:
                self.status_update.emit("❌ google-genai 미설치\npip install google-genai")
                return
            try:
                self._client = _genai.Client(api_key=self._api_key)
                self.status_update.emit(f"✅ Gemini 연결 완료 ({model})")
            except Exception as ex:
                self.status_update.emit(f"❌ Gemini 초기화 실패: {ex}")

        elif provider == "claude":
            if not CLAUDE_AVAILABLE:
                self.status_update.emit("❌ anthropic 미설치\npip install anthropic")
                return
            try:
                self._client = _anthropic_lib.Anthropic(api_key=self._api_key)
                self.status_update.emit(f"✅ Claude 연결 완료 ({model})")
            except Exception as ex:
                self.status_update.emit(f"❌ Claude 초기화 실패: {ex}")

    @property
    def is_ready(self): return self._client is not None

    def clear_history(self):
        """단기 히스토리만 삭제. 장기기억(long_term_summary)은 유지."""
        with self._lock:
            self._history.clear()

    def clear_all_memory(self):
        """단기 히스토리 + 장기기억 모두 초기화."""
        with self._lock:
            self._history.clear()
        self._long_term_summary = ""
        self._long_term_tokens  = 0
        self._save_long_term_memory()

    def send(self, user_text: str):
        if not self.is_ready:
            self.reply_ready.emit(
                "⚠️ API 키를 입력하고 [연결] 버튼을 눌러주세요.\n"
                "Groq 무료 키: https://console.groq.com", True)
            return
        threading.Thread(target=self._process, args=(user_text,),
                         daemon=True, name="AIChatProc").start()

    def _process(self, user_text: str):
        """
        AI 응답 처리 — 3-레이어 메모리 + 토큰 압력 감지.

        토큰 압력 초과 시:
          · WARN 임계값 초과 → 사용자에게 경고 메시지
          · COMPRESS 임계값 초과 → 히스토리 자동 압축 후 계속
        ★ 최상위 try/except: 어떤 예외에도 reply_ready를 emit해 입력창 복구
        """
        try:
            self._process_inner(user_text)
        except Exception as ex:
            import traceback
            err_msg = f"❌ 내부 오류: {ex}\n{traceback.format_exc()[-300:]}"
            self.reply_ready.emit(err_msg, True)
        finally:
            # ★ 대화 후 항상 ai_knowledge.json에 누적 저장
            try:
                self._save_long_term_memory()
            except Exception:
                pass

    def _process_inner(self, user_text: str):
        """실제 처리 로직 — _process 안전망 안에서 실행."""
        import json as _j, re as _re
        with self._lock:
            self._history.append({"role": "user", "parts": [user_text]})
            history_copy = list(self._history[-12:])

        # ── 동적 프롬프트 생성 (Layer 2) — 예외 방어 ─────────────────
        try:
            sys_prompt = self._build_sys_prompt()
        except Exception as ex:
            sys_prompt = _SYS_STATIC   # fallback: 정적 프롬프트만 사용
            import traceback
            self.reply_ready.emit(
                f"⚠️ 시스템 프롬프트 생성 오류 (정적 모드로 진행): {ex}", False)

        # ── 토큰 압력 체크 ─────────────────────────────────────────────
        token_est = self._check_token_pressure(sys_prompt, history_copy)
        self._last_token_est = token_est
        warn_msg = ""

        if token_est >= self._TOKEN_COMPRESS:
            # 자동 압축: 히스토리 → 장기기억
            self.reply_ready.emit(
                f"⚠️ 대화 기록이 누적되어 응답이 지체될 수 있습니다 "
                f"(추정 {token_est}토큰). 이전 대화를 자동 압축합니다…", False)
            self._compress_history_to_long_term()
            # 압축 후 히스토리 재구성
            with self._lock:
                self._history.append({"role": "user", "parts": [user_text]})
                history_copy = list(self._history[-12:])
            sys_prompt = self._build_sys_prompt()  # 장기기억 반영된 프롬프트 재생성
            token_est  = self._check_token_pressure(sys_prompt, history_copy)

        elif token_est >= self._TOKEN_WARN:
            warn_msg = (
                f"\n\n💡 (대화 기록이 쌓여 응답이 다소 느릴 수 있습니다 — "
                f"추정 {token_est}토큰. 대화가 더 쌓이면 자동 압축됩니다.)"
            )

        try:
            if self._provider == "groq":
                raw = self._call_groq_with_prompt(sys_prompt, history_copy, user_text)
            elif self._provider == "gemini":
                raw = self._call_gemini_with_prompt(sys_prompt, history_copy, user_text)
            elif self._provider == "claude":
                raw = self._call_claude_with_prompt(sys_prompt, history_copy, user_text)
            else:
                raw = ""
        except Exception as ex:
            self.reply_ready.emit(f"❌ API 오류: {ex}", True)
            return

        # ── 다중 JSON 추출 ─────────────────────────────────────────────────
        clean = raw.strip()
        for pfx in ("```json", "```"):
            if clean.startswith(pfx): clean = clean[len(pfx):]
        clean = clean.rstrip("`").strip()

        # 모든 JSON 객체 추출 ({...} 패턴)
        cmds = []
        depth = 0; start = -1
        for i, ch in enumerate(clean):
            if ch == "{":
                if depth == 0: start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        obj = _j.loads(clean[start:i+1])
                        cmds.append(obj)
                    except Exception:
                        pass
                    start = -1

        if not cmds:
            # JSON 없으면 그냥 텍스트 응답
            reply = raw + warn_msg
            with self._lock:
                self._history.append({"role": "model", "parts": [raw]})
            self.reply_ready.emit(reply, False)
            return

        # ── 시퀀스 실행 (백그라운드 스레드에서 대기 포함) ─────────────────
        import threading as _th, time as _tm

        def _run_sequence():
            results = []
            for cmd in cmds:
                action = cmd.get("action", "")
                if action == "kernel.wait":
                    secs = float(cmd.get("seconds", cmd.get("sec", 1)))
                    self.reply_ready.emit(f"⏳ {secs}초 대기 중…", False)
                    _tm.sleep(secs)
                    results.append(f"⏱ {secs}초 대기 완료")
                elif action == "engine.start_recording":
                    r = self._execute({"action": "start_recording"})
                    results.append(r)
                elif action == "engine.stop_recording":
                    r = self._execute({"action": "stop_recording"})
                    results.append(r)
                else:
                    r = self._execute(cmd)
                    results.append(r)

            reply = "\n".join(str(r) for r in results if r) + warn_msg
            with self._lock:
                self._history.append({"role": "model", "parts": [reply]})
            self.reply_ready.emit(reply, False)

        if len(cmds) == 1 and cmds[0].get("action") not in ("kernel.wait",
                                                              "engine.start_recording",
                                                              "engine.stop_recording"):
            reply = self._execute(cmds[0]) + warn_msg
            with self._lock:
                self._history.append({"role": "model", "parts": [reply]})
            self.reply_ready.emit(reply, False)
        else:
            _th.Thread(target=_run_sequence, daemon=True, name="AI_SEQ").start()

    def _call_groq_with_prompt(self, sys_prompt, history, user_text):
        msgs = [{"role": "system", "content": sys_prompt}]
        for h in history[:-1]:
            role    = "user" if h.get("role") == "user" else "assistant"
            parts   = h.get("parts", [])
            content = parts[0] if parts else ""
            msgs.append({"role": role, "content": content})
        msgs.append({"role": "user", "content": user_text})
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=msgs,
            temperature=0.1,
            max_tokens=1024,
        )
        return resp.choices[0].message.content.strip()

    def _call_gemini_with_prompt(self, sys_prompt, history, user_text):
        contents = []
        for h in history[:-1]:
            role  = "user" if h.get("role") == "user" else "model"
            parts = h.get("parts", [])
            txt   = parts[0] if parts else ""
            contents.append(
                _genai.types.Content(
                    role=role,
                    parts=[_genai.types.Part.from_text(text=txt)]))
        contents.append(
            _genai.types.Content(
                role="user",
                parts=[_genai.types.Part.from_text(text=user_text)]))
        resp = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=_genai.types.GenerateContentConfig(
                system_instruction=sys_prompt,
                temperature=0.1,
                max_output_tokens=1024,
            ),
        )
        return resp.text.strip()

    def _call_claude_with_prompt(self, sys_prompt, history, user_text):
        msgs = []
        for h in history[:-1]:
            role  = "user" if h.get("role") == "user" else "assistant"
            parts = h.get("parts", [])
            txt   = parts[0] if parts else ""
            msgs.append({"role": role, "content": txt})
        msgs.append({"role": "user", "content": user_text})
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=sys_prompt,
            messages=msgs,
        )
        return resp.content[0].text.strip()

    # 하위 호환용 (직접 호출 시 자동으로 프롬프트 빌드)
    def _call_groq(self, history, user_text):
        return self._call_groq_with_prompt(self._build_sys_prompt(), history, user_text)
    def _call_gemini(self, history, user_text):
        return self._call_gemini_with_prompt(self._build_sys_prompt(), history, user_text)
    def _call_claude(self, history, user_text):
        return self._call_claude_with_prompt(self._build_sys_prompt(), history, user_text)

    def _execute(self, cmd: dict) -> str:
        a = cmd.get("action", "")
        e = self._engine
        k = self._kernel

        # ── 채널명 정규화 (한국어/별칭 → 내부 채널 키) ───────────────────
        def _normalize_ch(ch: str) -> str:
            """
            AI가 생성한 다양한 채널 표현을 내부 채널 키로 정규화.
            예) "b+", "배터리", "battery" → "bplus"
                "plbm 실물", "실물", "plbm_real" → "plbm_real"
                "plbm 모사", "모사", "plbm_sim"  → "plbm_sim"
            """
            _MAP = {
                # bplus
                "bplus": "bplus", "b+": "bplus", "b_plus": "bplus",
                "배터리": "bplus", "battery": "bplus",
                # tg_bplus
                "tg_bplus": "tg_bplus", "tg b+": "tg_bplus", "tgb+": "tg_bplus",
                "tg": "tg_bplus", "target": "tg_bplus", "타겟": "tg_bplus",
                # acc
                "acc": "acc", "accessory": "acc", "악세서리": "acc",
                # ign
                "ign": "ign", "ignition": "ign", "점화": "ign",
                # plbm_real
                "plbm_real": "plbm_real", "plbm real": "plbm_real",
                "실물": "plbm_real", "실물릴레이": "plbm_real",
                "plbm실물": "plbm_real", "plbm 실물": "plbm_real",
                "real": "plbm_real",
                # plbm_sim
                "plbm_sim": "plbm_sim", "plbm sim": "plbm_sim",
                "모사": "plbm_sim", "모사릴레이": "plbm_sim",
                "plbm모사": "plbm_sim", "plbm 모사": "plbm_sim",
                "sim": "plbm_sim", "simulation": "plbm_sim",
            }
            ch = ch.strip().lower().replace("-", "_")
            return _MAP.get(ch, ch)   # 못 찾으면 원본 반환 (이미 올바른 키일 수 있음)

        try:
            # ── 녹화 ──────────────────────────────────────────────────────
            if a == "start_recording":
                if getattr(e, "recording", False):
                    return "⚠️ 이미 녹화 중입니다."
                e.start_recording()
                # output_dir은 start_recording() 내부에서 자동 생성됨
                out = getattr(e, "output_dir", "") or "(경로 자동 생성)"
                return f"✅ 녹화 시작  →  {out}"

            elif a == "stop_recording":
                if not getattr(e, "recording", False):
                    return "⚠️ 현재 녹화 중이 아닙니다."
                r = cmd.get("result","").upper()
                if r in ("PASS","FAIL"): e.tc_verify_result = r
                e.stop_recording()
                return "✅ 녹화 종료" + (f"  결과:{r}" if r in ("PASS","FAIL") else "")

            elif a == "capture":
                if not getattr(e, "output_dir", ""):
                    import os as _osa
                    try: e.output_dir = e._build_path("capture"); _osa.makedirs(e.output_dir, exist_ok=True)
                    except Exception: e.output_dir = e.base_dir; _osa.makedirs(e.output_dir, exist_ok=True)
                src = cmd.get("source","screen")
                tag = cmd.get("tc_tag","")
                p   = e.capture_frame(src, tag)
                return f"✅ 캡처 완료: {p}" if p else "⚠️ 캡처 실패 (화면 버퍼 없음)"

            elif a == "manual_clip":
                if not getattr(e, "output_dir", ""):
                    import os as _osa
                    try: e.output_dir = e._build_path("manual"); _osa.makedirs(e.output_dir, exist_ok=True)
                    except Exception: e.output_dir = e.base_dir; _osa.makedirs(e.output_dir, exist_ok=True)
                pre  = getattr(e, "manual_pre_sec",  5)
                post = getattr(e, "manual_post_sec", 5)
                src  = getattr(e, "manual_source", "screen")
                ok   = e.save_manual_clip()
                if ok:
                    return (f"✅ 수동 클립 저장 시작\n"
                            f"  소스: {src} / 앞:{pre}초 뒤:{post}초\n"
                            f"  저장 경로: {e.output_dir}\n"
                            f"  ※ {pre+post}초 후 파일이 생성됩니다.")
                return "⚠️ 수동 클립 저장 실패 (이미 진행 중이거나 버퍼 없음)"

            # ★ [문제2 수정] set_manual_clip_time — 설정만 변경, 녹화 절대 실행 안 함
            # 시스템 프롬프트에 이 액션이 없었기 때문에 AI가 manual_clip으로 대체 실행했던 버그.
            # before/after 값만 engine에 적용하고, UI 스핀박스도 동기화 후 DB 저장.
            elif a == "set_manual_clip_time":
                before = float(cmd.get("before", cmd.get("pre",  getattr(e, "manual_pre_sec",  10.0))))
                after  = float(cmd.get("after",  cmd.get("post", getattr(e, "manual_post_sec", 10.0))))
                before = max(0.0, min(before, 300.0))
                after  = max(0.0, min(after,  300.0))
                # ① engine 값 갱신
                e.manual_pre_sec  = before
                e.manual_post_sec = after
                # ② [버그 수정] engine._manual_panel_ref로 직접 UI 동기화
                #    기존의 gc.get_referrers() 방식은 패널을 못 찾아 UI가 갱신 안 됐음
                mp = getattr(e, '_manual_panel_ref', None)
                if mp is not None and hasattr(mp, 'sync_from_engine'):
                    mp.sync_from_engine()
                # DB 동기화 (1초 디바운스 타이머 경유)
                try:
                    db = getattr(self, '_db', None) or getattr(e, '_db', None)
                    if db:
                        db.set("manual_pre_sec",  str(before))
                        db.set("manual_post_sec", str(after))
                except Exception:
                    pass
                return (f"✅ 수동녹화 시간 설정 완료\n"
                        f"  앞(before): {before}초\n"
                        f"  뒤(after):  {after}초\n"
                        f"  ※ 녹화는 실행하지 않았습니다.")

            elif a == "start_ac":
                if not getattr(e, "output_dir", ""):
                    import os as _osa
                    try: e.output_dir = e._build_path("capture"); _osa.makedirs(e.output_dir, exist_ok=True)
                    except Exception: e.output_dir = e.base_dir; _osa.makedirs(e.output_dir, exist_ok=True)
                e.start_ac(); return "✅ 자동캡처 시작"

            elif a == "stop_ac":
                e.stop_ac(); return "✅ 자동캡처 중지"

            # ── 예약 ──────────────────────────────────────────────────────────
            elif a == "schedule_add":
                from datetime import datetime as _dt, timedelta as _td
                _FMT  = "%Y-%m-%d %H:%M:%S"
                now   = _dt.now()

                start_str  = cmd.get("start",   "")
                stop_str   = cmd.get("stop",    "")
                # ★ 상대 시간 지원: minutes / seconds
                start_after_min = cmd.get("start_after_minutes", None)
                start_after_sec = cmd.get("start_after_seconds", None)
                stop_after_min  = cmd.get("stop_after_minutes",  None)
                stop_after_sec  = cmd.get("stop_after_seconds",  None)
                actions    = cmd.get("actions", ["rec_start", "rec_stop"])
                start_dt   = stop_dt = None

                # ── 시작 시각 결정 ────────────────────────────────────────
                if start_after_min is not None:
                    start_dt = now + _td(minutes=float(start_after_min))
                elif start_after_sec is not None:
                    start_dt = now + _td(seconds=float(start_after_sec))
                elif start_str:
                    try:
                        start_dt = _dt.strptime(start_str.strip(), _FMT)
                    except ValueError:
                        return (f"⚠️ 시작 시각 형식 오류: {start_str!r}\n"
                                f"올바른 형식: 'YYYY-MM-DD HH:MM:SS'\n"
                                f"또는 start_after_minutes:4 처럼 상대 시간 사용")
                    if start_dt < now:
                        return (f"⚠️ 시작 시각이 현재({now.strftime(_FMT)})보다 과거입니다.\n"
                                f"입력: {start_str}\n"
                                f"💡 '4분 뒤' 같은 요청은: "
                                f'{{\"action\":\"schedule_add\",\"start_after_minutes\":4}}')

                # ── 종료 시각 결정 ────────────────────────────────────────
                if stop_after_min is not None:
                    base = start_dt or now
                    stop_dt = base + _td(minutes=float(stop_after_min))
                elif stop_after_sec is not None:
                    base = start_dt or now
                    stop_dt = base + _td(seconds=float(stop_after_sec))
                elif stop_str:
                    try:
                        stop_dt = _dt.strptime(stop_str.strip(), _FMT)
                    except ValueError:
                        return (f"⚠️ 종료 시각 형식 오류: {stop_str!r}\n"
                                f"올바른 형식: 'YYYY-MM-DD HH:MM:SS'")
                    if stop_dt < now:
                        return "⚠️ 종료 시각이 현재보다 과거입니다."

                if start_dt and stop_dt and stop_dt <= start_dt:
                    return "⚠️ 종료 시각이 시작 시각보다 늦어야 합니다."
                if not start_dt and not stop_dt:
                    return ("⚠️ 시작 또는 종료 시각을 지정해야 합니다.\n"
                            "예) 4분 뒤 시작: start_after_minutes:4\n"
                            "예) 절대 시각: start:'2026-05-13 17:00:00'")

                # ── 액션 결정 ─────────────────────────────────────────────
                _VALID_ACTIONS = {"rec_start", "rec_stop", "macro_run"}
                actions = [ac for ac in actions if ac in _VALID_ACTIONS]
                if not actions:
                    # 시작만 있으면 rec_start, 종료만 있으면 rec_stop
                    if start_dt and not stop_dt:
                        actions = ["rec_start"]
                    elif stop_dt and not start_dt:
                        actions = ["rec_stop"]
                    else:
                        actions = ["rec_start", "rec_stop"]

                # ── stop_only 처리: start_dt 없이 stop_dt만 있는 경우 ────
                # schedule_tick은 s.started가 True여야 stop 조건 확인.
                # stop_only는 start_dt=now로 설정해 즉시 started=True로 처리.
                if not start_dt and stop_dt:
                    start_dt = now   # 즉시 started 처리용 더미 시작

                entry = ScheduleEntry(start_dt, stop_dt, actions)
                entry.pwr_on_ch  = cmd.get("pwr_on",  None)
                entry.pwr_off_ch = cmd.get("pwr_off", None)
                e.schedules.append(entry)

                # ★ SchedulePanel UI 즉시 반영
                try:
                    import gc as _gc
                    for obj in _gc.get_objects():
                        if type(obj).__name__ == 'SchedulePanel' and hasattr(obj, '_add_row'):
                            obj._add_row(entry)
                            break
                except Exception:
                    pass

                s_str = start_dt.strftime(_FMT) if start_dt else "즉시"
                t_str = stop_dt.strftime(_FMT)  if stop_dt  else "수동 종료"
                act_str = "+".join(actions)
                return (f"✅ 예약 추가됨 (ID:{entry.id})\n"
                        f"  시작: {s_str}\n"
                        f"  종료: {t_str}\n"
                        f"  동작: {act_str}\n"
                        f"  💡 예약 패널에서 확인하세요.")

            elif a == "schedule_list":
                schedules = getattr(e, "schedules", [])
                if not schedules:
                    return "📅 등록된 예약 없음"
                _FMT = "%Y-%m-%d %H:%M:%S"
                lines = [f"📅 예약 목록 ({len(schedules)}개):"]
                for s in schedules:
                    s_str = s.start_dt.strftime(_FMT) if s.start_dt else "—"
                    t_str = s.stop_dt.strftime(_FMT)  if s.stop_dt  else "—"
                    stat  = "완료" if s.done else ("실행중" if s.started else "대기")
                    lines.append(f"  ID:{s.id} 시작:{s_str} 종료:{t_str} "
                                 f"동작:{'+'.join(s.actions)} [{stat}]")
                return "\n".join(lines)

            elif a == "schedule_clear":
                sid = cmd.get("id", None)
                if sid is not None:
                    before = len(e.schedules)
                    e.schedules = [s for s in e.schedules if s.id != int(sid)]
                    removed = before - len(e.schedules)
                    # UI 테이블에서도 제거
                    try:
                        import gc as _gc
                        for obj in _gc.get_objects():
                            if type(obj).__name__ == 'SchedulePanel' and hasattr(obj, 'refresh_tbl'):
                                obj.refresh_tbl()
                                break
                    except Exception:
                        pass
                    return f"✅ 예약 ID:{sid} 삭제 ({removed}개 제거)"
                else:
                    count = len(e.schedules)
                    e.schedules.clear()
                    try:
                        import gc as _gc
                        for obj in _gc.get_objects():
                            if type(obj).__name__ == 'SchedulePanel' and hasattr(obj, 'tbl'):
                                obj.tbl.setRowCount(0)
                                break
                    except Exception:
                        pass
                    return f"✅ 전체 예약 {count}개 삭제"

            elif a == "schedule_power":
                # ★ 전원 예약 — 녹화 예약과 독립 동작
                from datetime import datetime as _dt, timedelta as _td
                _FMT = "%Y-%m-%d %H:%M:%S"
                now  = _dt.now()
                pwr_on_ch  = cmd.get("pwr_on",  None)
                pwr_off_ch = cmd.get("pwr_off", None)
                on_at_str  = cmd.get("on_at",   "")
                off_at_str = cmd.get("off_at",  "")
                on_after_min  = cmd.get("on_after_minutes",  None)
                off_after_min = cmd.get("off_after_minutes", None)

                on_dt = off_dt = None
                if on_after_min is not None:
                    on_dt = now + _td(minutes=float(on_after_min))
                elif on_at_str:
                    try: on_dt = _dt.strptime(on_at_str.strip(), _FMT)
                    except ValueError: return f"⚠️ on_at 형식 오류: '{on_at_str}' (YYYY-MM-DD HH:MM:SS)"
                if off_after_min is not None:
                    off_dt = now + _td(minutes=float(off_after_min))
                elif off_at_str:
                    try: off_dt = _dt.strptime(off_at_str.strip(), _FMT)
                    except ValueError: return f"⚠️ off_at 형식 오류: '{off_at_str}' (YYYY-MM-DD HH:MM:SS)"

                if not pwr_on_ch and not pwr_off_ch:
                    return "⚠️ pwr_on 또는 pwr_off 채널을 지정해야 합니다.\n예) {\"action\":\"schedule_power\",\"pwr_on\":\"acc\",\"on_after_minutes\":2}"
                if not on_dt and not off_dt:
                    return "⚠️ on_at/on_after_minutes 또는 off_at/off_after_minutes를 지정해야 합니다."

                # 전원 전용 예약 — actions=[] (녹화 없음)
                entry = ScheduleEntry(on_dt, off_dt, actions=[])
                entry.pwr_on_ch  = pwr_on_ch
                entry.pwr_off_ch = pwr_off_ch
                e.schedules.append(entry)
                try:
                    import gc as _gc
                    for obj in _gc.get_objects():
                        if type(obj).__name__ == 'SchedulePanel' and hasattr(obj, '_add_row'):
                            obj._add_row(entry); break
                except Exception:
                    pass
                on_s  = on_dt.strftime(_FMT)  if on_dt  else "—"
                off_s = off_dt.strftime(_FMT) if off_dt else "—"
                return (f"✅ 전원 예약 추가됨 (ID:{entry.id})\n"
                        f"  ON 채널: {pwr_on_ch or '없음'}  시각: {on_s}\n"
                        f"  OFF 채널: {pwr_off_ch or '없음'}  시각: {off_s}")

            # ── 매크로 ─────────────────────────────────────────────────────
            elif a == "macro_run":
                slot_ui = cmd.get("slot", None)   # 1-based (슬롯 1 = 1)
                rep     = int(cmd.get("repeat", 1))
                gap     = float(cmd.get("gap", 1.0))
                slots   = getattr(e, "_macro_slots", None) or []
                n_slots = len(slots)

                # ── 슬롯 지정 시 유효성 검사 ──────────────────────────
                if slot_ui is not None:
                    slot_ui = int(slot_ui)
                    if n_slots == 0:
                        return ("⚠️ 저장된 매크로 슬롯이 없습니다.\n"
                                "입/출력 장치 기록 패널에서 기록 후 저장하세요.")
                    if not (1 <= slot_ui <= n_slots):
                        return (f"⚠️ 슬롯 {slot_ui}번이 없습니다.\n"
                                f"현재 슬롯: {n_slots}개 (1~{n_slots})")

                if getattr(e, "macro_running", False):
                    return "⚠️ 이미 매크로 실행 중입니다. 먼저 '매크로 중지'를 실행하세요."

                # ★ slot_ui 그대로 전달 (macro_start_run 내부에서 -1 변환)
                e.macro_start_run(repeat=rep, gap=gap, slot=slot_ui)

                if slot_ui is not None:
                    slot_name = slots[slot_ui - 1]['title'] if slots else f"슬롯{slot_ui}"
                    return f"✅ {slot_name} (슬롯 {slot_ui}) {rep}회 실행 시작"
                return f"✅ 현재 매크로 {rep}회 실행 시작"

            elif a == "macro_stop":
                if not getattr(e, "macro_running", False):
                    return "⚠️ 실행 중인 매크로가 없습니다."
                e.macro_stop_run(); return "✅ 매크로 중지"

            elif a == "macro_load":
                slot_ui = int(cmd.get("slot", 1))   # 1-based
                slots   = getattr(e, "_macro_slots", None) or []
                if not slots:
                    return ("⚠️ 저장된 매크로 슬롯이 없습니다.\n"
                            "입/출력 장치 기록 패널에서 기록 후 저장하세요.")
                if not (1 <= slot_ui <= len(slots)):
                    return (f"⚠️ 슬롯 {slot_ui}번이 없습니다.\n"
                            f"현재 슬롯: {len(slots)}개 (1~{len(slots)})")
                ok = e.macro_load_slot(slot_ui - 1)   # ★ 1-based → 0-based
                if ok:
                    slot_name = slots[slot_ui-1]['title']
                    return f"✅ 슬롯 {slot_ui} '{slot_name}' 로드 완료 ({len(e.macro_steps)} 스텝)"
                return f"❌ 슬롯 {slot_ui} 로드 실패"

            # ── ROI 조회 ───────────────────────────────────────────────────
            elif a == "roi_read_text":
                roi = int(cmd.get("roi", 1))
                src = cmd.get("source","screen")
                if k is None:
                    return "⚠️ KernelEngine 미연결 — 프로그램을 재시작해주세요."
                rois = (e.screen_rois if src=="screen" else e.camera_rois) or []
                if roi > len(rois):
                    return (f"⚠️ ROI {roi}번이 없습니다.\n"
                            f"현재 등록된 ROI: {len(rois)}개")
                txt = k.read_roi_text(roi, src)
                return f"📖 ROI {roi}번 텍스트: {repr(txt)}"

            elif a == "roi_read_number":
                roi = int(cmd.get("roi", 1))
                src = cmd.get("source","screen")
                if k is None:
                    return "⚠️ KernelEngine 미연결 — 프로그램을 재시작해주세요."
                rois = (e.screen_rois if src=="screen" else e.camera_rois) or []
                if roi > len(rois):
                    return (f"⚠️ ROI {roi}번이 없습니다.\n"
                            f"현재 등록된 ROI: {len(rois)}개")
                v = k.read_roi_number(roi, src)
                return f"🔢 ROI {roi}번 숫자: {v}"

            elif a == "roi_read_value":
                roi = int(cmd.get("roi", 1))
                src = cmd.get("source","screen")
                if k is None:
                    return "⚠️ KernelEngine 미연결 — 프로그램을 재시작해주세요."
                rois = (e.screen_rois if src=="screen" else e.camera_rois) or []
                if roi > len(rois):
                    return (f"⚠️ ROI {roi}번이 없습니다.\n"
                            f"현재 등록된 ROI: {len(rois)}개")
                v = k.read_roi_value(roi, src)
                return f"💡 ROI {roi}번 밝기: {v:.1f}" if v is not None else f"ROI {roi}번: None"

            elif a == "roi_read_all":
                src = cmd.get("source","screen")
                if k is None:
                    return "⚠️ KernelEngine 미연결"
                rois = (e.screen_rois if src=="screen" else e.camera_rois) or []
                if not rois:
                    return f"⚠️ 등록된 ROI가 없습니다. ROI 관리 패널에서 먼저 ROI를 추가해주세요."
                d = k.read_all_roi(src)
                lines = [f"  ROI {i+1}: {v}" for i,v in enumerate(d.values())]
                return "📊 전체 ROI\n" + "\n".join(lines)

            # ── 전원 제어 ──────────────────────────────────────────────────
            elif a == "power_connect":
                port = str(cmd.get("port",""))
                baud = int(cmd.get("baudrate",9600))
                if not port:
                    # 포트 목록 안내
                    ports = []
                    if k:
                        try: ports = k.power_list_ports()
                        except Exception: pass
                    if ports:
                        return (f"⚠️ 연결할 포트를 지정해주세요.\n"
                                f"사용 가능한 포트: {', '.join(ports)}\n"
                                f"예: 'COM3으로 전원 연결해줘'")
                    return ("⚠️ 연결할 포트를 지정해주세요.\n"
                            "예: 'COM7으로 전원 연결해줘'")
                if k:
                    ok = k.power_connect(port, baud)
                elif self._power:
                    ok = self._power.connect(port, baud)
                else:
                    return "⚠️ 전원 제어기 미초기화"
                return f"✅ 전원 연결: {port}" if ok else f"❌ 전원 연결 실패: {port}\n다른 포트를 시도해보세요."

            elif a == "power_disconnect":
                if k: k.power_disconnect()
                elif self._power: self._power.disconnect()
                else: return "⚠️ 전원 제어기 미초기화"
                return "✅ 전원 연결 해제"

            elif a == "power_on":
                ch = _normalize_ch(str(cmd.get("channel","")).lower())
                if not ch:
                    return ("⚠️ 채널을 지정해주세요.\n"
                            "채널명: bplus(B+배터리) | tg_bplus(TG B+) | acc(ACC) | ign(IGN) "
                            "| plbm_real(PLBM실물릴레이) | plbm_sim(PLBM모사릴레이)")
                ok = False
                if k and getattr(k, "power_connected", False):
                    ok = k.power_on(ch)
                elif k and k.power and k.power.is_connected:
                    ok = k.power.channel_on(ch)
                elif self._power and self._power.is_connected:
                    ok = self._power.channel_on(ch)
                else:
                    return ("⚠️ 전원 제어기가 연결되지 않았습니다.\n"
                            "전원 제어 패널에서 COM 포트 연결 상태를 확인해주세요.")
                return f"✅ 전원 ON: {ch}" if ok else f"❌ ON 실패: {ch} (Arduino 응답 없음 — COM 포트 확인)"

            elif a == "power_off":
                ch = _normalize_ch(str(cmd.get("channel","")).lower())
                if not ch:
                    return ("⚠️ 채널을 지정해주세요.\n"
                            "채널명: bplus(B+배터리) | tg_bplus(TG B+) | acc(ACC) | ign(IGN) "
                            "| plbm_real(PLBM실물릴레이) | plbm_sim(PLBM모사릴레이)")
                # [수정] power_off는 연결 상태를 엄격하게 체크하되
                # KernelEngine 경유 시 self._power도 fallback으로 시도
                ok = False
                if k and getattr(k, "power_connected", False):
                    ok = k.power_off(ch)
                elif k and k.power and k.power.is_connected:
                    # k.power_connected 프로퍼티 오류 방어: 직접 접근
                    ok = k.power.channel_off(ch)
                elif self._power and self._power.is_connected:
                    ok = self._power.channel_off(ch)
                else:
                    return ("⚠️ 전원 제어기가 연결되지 않았습니다.\n"
                            "전원 제어 패널에서 COM 포트 연결 상태를 확인해주세요.")
                return f"✅ 전원 OFF: {ch}" if ok else f"❌ OFF 실패: {ch} (Arduino 응답 없음 — COM 포트 확인)"

            elif a == "power_all_on":
                if k:
                    conn = getattr(k, "power_connected", False)
                    if not conn:
                        return ("⚠️ 전원 제어기가 연결되지 않았습니다.\n"
                                "먼저 'COM포트로 전원 연결해줘'를 실행해주세요.")
                    ok = k.power_all_on()
                    return "✅ 전체 전원 ON" if ok else "❌ 전원 ON 실패"
                elif self._power:
                    import time as _t3
                    for ch in ["bplus", "tg_bplus", "acc", "ign"]:
                        self._power.channel_on(ch)
                        _t3.sleep(0.3)
                    return "✅ 전체 전원 ON"
                return "⚠️ 전원 제어기 미초기화"

            elif a == "power_all_off":
                ok = False
                if k and getattr(k, "power_connected", False):
                    ok = k.power_all_off()
                elif k and k.power and k.power.is_connected:
                    ok = k.power.all_off()
                elif self._power and self._power.is_connected:
                    ok = self._power.all_off()
                else:
                    return ("⚠️ 전원 제어기가 연결되지 않았습니다.\n"
                            "전원 제어 패널에서 COM 포트 연결 상태를 확인해주세요.")
                return "✅ 전체 전원 OFF" if ok else "❌ ALL OFF 실패"

            elif a == "power_status":
                if k:
                    conn = getattr(k, "power_connected", False)
                    return "✅ 전원 연결됨" if conn else "전원 미연결 (COM 포트 연결 필요)"
                if not self._power: return "⚠️ 전원 제어기 미초기화"
                return f"✅ 전원: {self._power.port}" if self._power.is_connected else "전원 미연결"

            # ── OHCL 릴레이 직접 트리거 (v5.6) ───────────────────────────
            elif a == "ohcl_trigger":
                pwr = (k.power if k and hasattr(k, "power") else None) or self._power
                if not pwr or not pwr.is_connected:
                    return ("⚠️ Arduino 미연결 — ohcl_trigger 불가.\n"
                            "전원 제어 패널에서 COM 포트 연결 상태를 확인해주세요.")
                try:
                    relay_ms = int(cmd.get("relay_ms", 0))
                except (ValueError, TypeError):
                    return "⚠️ relay_ms 값이 올바르지 않습니다. 예: {\"action\":\"ohcl_trigger\",\"relay_ms\":1250}"
                if relay_ms < 100 or relay_ms > 9999:
                    return f"⚠️ relay_ms 범위 초과: {relay_ms}ms (허용: 100~9999ms)"
                rec_type = "TML" if relay_ms >= 1300 else "MAR"
                ok = pwr.ohcl_trigger(relay_ms)
                if ok:
                    return f"✅ OHCL 트리거: A1 릴레이 {relay_ms}ms CLOSE → {rec_type} 녹화 목표"
                return f"❌ OHCL 트리거 전송 실패 ({relay_ms}ms) — Arduino 응답 확인"

            elif a == "ohcl_status":
                pwr = (k.power if k and hasattr(k, "power") else None) or self._power
                if not pwr:
                    return "⚠️ 전원 제어기 미초기화"
                connected = pwr.is_connected
                led_on = getattr(pwr, "_led_pwm_on", False)
                half_ms = getattr(pwr, "_led_half_period_ms", 0)
                last_ev = getattr(pwr, "_ohcl_last_event", {})
                lines = [
                    f"🔌 Arduino: {'연결됨' if connected else '미연결'}",
                    f"💡 LED PWM: {'ON' if led_on else 'OFF'}"
                    + (f"  반주기={half_ms}ms 전체주기={half_ms*2}ms" if led_on and half_ms else ""),
                ]
                if last_ev:
                    lms = last_ev.get("relay_ms", 0)
                    lt  = last_ev.get("type", "?")
                    rec = "TML" if lms >= 1300 else "MAR"
                    lines.append(f"⚡ 마지막 OHCL: type={lt} {lms}ms → {rec}")
                return "\n".join(lines)

            elif a == "power_list_ports":
                if k:
                    ports = k.power_list_ports()
                    return "🔌 COM 포트: " + (", ".join(ports) if ports else "없음 (장치를 연결해주세요)")
                return "⚠️ KernelEngine 미연결"

            # ── CAN/LIN ────────────────────────────────────────────────────
            elif a == "can_connect":
                if not CAN_AVAILABLE:
                    return ("⚠️ python-can 미설치\n"
                            "pip install python-can python-can[vector]")
                ok = self._can.connect(
                    channel=int(cmd.get("channel",0)),
                    bitrate=int(cmd.get("bitrate",500000)),
                    app_name=str(cmd.get("app_name","BltnRecorder")),
                    interface=str(cmd.get("interface","vector")))
                return self._can.status_msg

            elif a == "can_disconnect":
                self._can.disconnect(); return "✅ CAN 연결 해제"

            elif a == "can_send":
                if not (self._can.connected or self._can.canoe_mode):
                    return ("⚠️ CAN이 연결되지 않았습니다.\n"
                            "먼저 'CAN 채널 0번 연결해줘'를 실행해주세요.")
                raw_id = cmd.get("arb_id","0x000"); data = cmd.get("data",[])
                arb    = int(raw_id,16) if isinstance(raw_id,str) else int(raw_id)
                ok     = self._can.send(arb, data)
                return f"✅ CAN 송신 ID={raw_id} data={data}" if ok else f"❌ {self._can.status_msg}"

            elif a == "can_send_signal":
                if not (self._can.connected or self._can.canoe_mode):
                    return "⚠️ CAN이 연결되지 않았습니다."
                if not self._can._db:
                    return ("⚠️ DBC 파일이 로드되지 않았습니다.\n"
                            "먼저 'DBC 파일 로드해줘'를 실행해주세요.")
                ok = self._can.send_signal(str(cmd.get("msg_name","")),cmd.get("signals",{}))
                return "✅ 신호 송신" if ok else f"❌ {self._can.status_msg}"

            elif a == "can_recv":
                if not (self._can.connected or self._can.canoe_mode):
                    return "⚠️ CAN이 연결되지 않았습니다."
                msg = self._can.recv(float(cmd.get("timeout",1.0)))
                if msg is None: return "⚠️ CAN 수신 없음 (timeout)"
                dec = (f"\n  decoded:{msg['decoded']}") if msg["decoded"] else ""
                return f"📨 CAN  ID:{msg['id']}  data:{msg['data']}{dec}"

            elif a == "can_load_dbc":
                path = str(cmd.get("path",""))
                if not path:
                    return "⚠️ DBC 파일 경로를 지정해주세요.\n예: 'C:/path/to/BLTN.dbc 파일 로드해줘'"
                import os
                if not os.path.isfile(path):
                    return f"❌ 파일을 찾을 수 없습니다: {path}"
                self._can.load_dbc(path); return self._can.status_msg

            elif a == "can_status":
                return "📡 " + self._can.info()

            # ── 커널 스크립트 생성 ─────────────────────────────────────────
            elif a == "make_script":
                code_raw = str(cmd.get("code","")).replace("\\n","\n")
                desc = cmd.get("description", "")
                if not code_raw:
                    return "⚠️ 스크립트 코드가 비어 있습니다."

                # ★ 전처리: ω import time �Ǆ작 + α �˔팬 키워드 앞 강�ǘ �Ą바꿈 삽입
                import re as _re_ms
                import re as _re_ms
                # 1) alias mapping before import removal
                _ALIAS = {}
                for _am in _re_ms.finditer(r'import\s+(\w+)\s+as\s+(\w+)', code_raw):
                    _ALIAS[_am.group(2)] = _am.group(1)
                # 2) remove unnecessary imports (already injected in kernel globals)
                code_raw = _re_ms.sub(
                    r'import (time|os|sys|re|json|threading)(\s+as\s+\w+)?\s*[;]?',
                    '', code_raw, flags=_re_ms.MULTILINE).strip()
                # 3) replace aliases: _t2.time() -> time.time()
                for _al, _orig in _ALIAS.items():
                    code_raw = code_raw.replace(_al + '.', _orig + '.')
                # 4) force newline before block keywords joined without \n
                #    e.g. "t1=time.time() while not..." -> "t1=time.time()\nwhile not..."
                _KW_SPLIT = _re_ms.compile(
                    r'([)\w\'\"])([ \t]+)(while\b|if\b(?!\s+__name__)|for\b|else:|elif\b|try:|except|finally:)')
                for _ in range(10):
                    _new = _KW_SPLIT.sub(lambda m: m.group(1) + '\n' + m.group(3), code_raw)
                    if _new == code_raw: break
                    code_raw = _new
                # 5) split "BLOCK_HEADER: body" -> two lines (non-greedy colon match)
                _BH = _re_ms.compile(r'^(if |elif |while |for )')
                for _pass in range(6):
                    _lns = code_raw.split('\n'); _exp = []; _chg = False
                    for _ln in _lns:
                        _s = _ln.strip()
                        if _s and _BH.match(_s):
                            _mh = _re_ms.match(r'^((?:while|for|if|elif)\s+[^:]+?):\s*(\S.*)$', _s)
                            if _mh:
                                _exp.append(_mh.group(1) + ':')
                                _exp.append(_mh.group(2).strip())
                                _chg = True; continue
                        _exp.append(_s)
                    code_raw = '\n'.join(_exp)
                    if not _chg: break
                # 6) indent restoration
                code = _restore_indent(code_raw)
                code = _restore_indent(code_raw)
                # 커널 스크립트 창에 자동 삽입 시도
                inserted = False
                try:
                    mw = self._engine.signals  # 실제 MainWindow 접근
                    import gc
                    for obj in gc.get_objects():
                        if hasattr(obj, '_kernel_panel') and hasattr(obj._kernel_panel, 'insert_code'):
                            obj._kernel_panel.insert_code(code)
                            inserted = True
                            break
                except Exception:
                    pass
                result = f"📝 커널 스크립트 생성됨{' (' + desc + ')' if desc else ''}:\n```python\n{code}\n```"
                if inserted:
                    result += "\n✅ 커널 스크립트 패널에 자동 삽입됨"
                else:
                    result += "\n💡 위 코드를 커널 스크립트 패널에 붙여넣으세요."
                return result

            # ── 상태 조회 ──────────────────────────────────────────────────
            elif a == "status":
                st   = "녹화중🔴" if getattr(e,"recording",False) else "대기⏸"
                pth  = getattr(e,"output_dir","") or "❌ 미설정"
                mac  = "실행중" if getattr(e,"macro_running",False) else "대기"
                pv   = self._provider.upper()
                pwr  = "연결됨" if (k and getattr(k,"power_connected",False)) else "미연결"
                can  = self._can.info()
                timer_st = getattr(e, '_timer_state_cache', {})
                timer_str = (f"실행중({timer_st.get('elapsed_str','?')})"
                             if timer_st.get('running') else "정지")
                mem_tok = self._last_token_est
                return (f"📊 상태\n  녹화: {st}\n  경로: {pth}\n"
                        f"  매크로: {mac}\n  타이머: {timer_str}\n"
                        f"  AI: {pv}/{self._model}  추정토큰:{mem_tok}\n"
                        f"  장기기억: {self._long_term_tokens}토큰 누적\n"
                        f"  전원: {pwr}\n  CAN: {can}")

            # ── 타이머 액션 ─────────────────────────────────────────────────
            elif a == "timer_start":
                t = getattr(e, '_timer_panel_ref', None)
                if t and hasattr(t, '_manual_start'):
                    t._manual_start(); return "✅ 타이머 시작"
                return "⚠️ 타이머 패널 미연결 (프로그램 재시작 필요)"

            elif a == "timer_stop":
                t = getattr(e, '_timer_panel_ref', None)
                if t and hasattr(t, '_manual_stop'):
                    t._manual_stop(); return "✅ 타이머 종료"
                return "⚠️ 타이머 패널 미연결"

            elif a == "timer_reset":
                t = getattr(e, '_timer_panel_ref', None)
                if t and hasattr(t, '_reset'):
                    t._reset(); return "✅ 타이머 리셋"
                return "⚠️ 타이머 패널 미연결"

            elif a == "timer_lap":
                t = getattr(e, '_timer_panel_ref', None)
                if t and hasattr(t, '_record_lap'):
                    t._record_lap(); return "✅ 랩 기록"
                return "⚠️ 타이머 패널 미연결"

            elif a == "timer_status":
                ts = getattr(e, '_timer_state_cache', {})
                if not ts:
                    return "⚠️ 타이머 상태 없음"
                return (f"⏱ 타이머\n  실행중={ts.get('running',False)}\n"
                        f"  경과={ts.get('elapsed_str','00:00:00.000')}\n"
                        f"  랩수={ts.get('lap_count',0)}")

            # ── ROI NO→REC 제어 ─────────────────────────────────────────────
            elif a == "roi_set_no_rec":
                roi_idx = int(cmd.get("roi", 1)) - 1
                src     = cmd.get("source", "screen")
                enabled = bool(cmd.get("enabled", True))
                rois = (e.screen_rois if src == "screen" else e.camera_rois) or []
                if not 0 <= roi_idx < len(rois):
                    return f"⚠️ ROI {roi_idx+1}번이 없습니다 (현재 {len(rois)}개)"
                rois[roi_idx].roi_no_rec_enabled = enabled
                return (f"✅ ROI {roi_idx+1} NO→REC "
                        f"{'활성화' if enabled else '비활성화'}")

            elif a == "roi_set_cond":
                roi_idx = int(cmd.get("roi", 1)) - 1
                src     = cmd.get("source", "screen")
                cond    = str(cmd.get("cond_value", ""))
                rois = (e.screen_rois if src == "screen" else e.camera_rois) or []
                if not 0 <= roi_idx < len(rois):
                    return f"⚠️ ROI {roi_idx+1}번이 없습니다"
                rois[roi_idx].cond_value = cond
                return f"✅ ROI {roi_idx+1} 비교조건 설정: {cond!r}"

            elif a == "roi_no_count":
                src  = cmd.get("source", "screen")
                rois = (e.screen_rois if src == "screen" else e.camera_rois) or []
                lines_r = [f"  ROI {i+1} ({r.label()}): NO×{r.no_match_count}"
                           for i, r in enumerate(rois)]
                return "📊 NO 발생 횟수\n" + ("\n".join(lines_r) or "  등록된 ROI 없음")

            elif a == "roi_open_folder":
                try:
                    p = e._build_path("manual")
                    while p and not os.path.exists(p) and p != os.path.dirname(p):
                        p = os.path.dirname(p)
                    open_folder(p or e.base_dir)
                    return "✅ 저장 폴더 열기"
                except Exception as ex:
                    return f"❌ 폴더 열기 실패: {ex}"

            # ── 블랙아웃 제어 ────────────────────────────────────────────────
            elif a == "blackout_set_threshold":
                val = float(cmd.get("value", 30))
                e.brightness_threshold = val
                return f"✅ 블랙아웃 임계값 설정: {val}"

            elif a == "blackout_set_enabled":
                enabled = bool(cmd.get("enabled", True))
                e.blackout_rec_enabled = enabled
                return f"✅ 블랙아웃 녹화 {'활성화' if enabled else '비활성화'}"

            # ── OCR / 밝기 제어 ──────────────────────────────────────────────
            elif a == "ocr_set":
                src     = cmd.get("source", "screen")
                enabled = bool(cmd.get("enabled", True))
                if src == "screen":
                    e.ocr_screen_enabled = enabled
                    e.ocr_enabled = enabled
                else:
                    e.ocr_camera_enabled = enabled
                return f"✅ {src} OCR {'ON' if enabled else 'OFF'}"

            elif a == "brightness_set":
                src     = cmd.get("source", "screen")
                enabled = bool(cmd.get("enabled", True))
                if src == "screen":
                    e.brightness_screen_enabled = enabled
                else:
                    e.brightness_camera_enabled = enabled
                e.brightness_enabled = (
                    getattr(e,'brightness_screen_enabled',True) or
                    getattr(e,'brightness_camera_enabled',True))
                return f"✅ {src} 밝기 {'ON' if enabled else 'OFF'}"

            # ── [기능 추가] 누락됐던 설정값 제어 액션 ──────────────────────
            elif a == "blackout_set_cooldown":
                # blackout_cooldown 설정 (AI Chat에서 접근 불가했던 항목)
                val = float(cmd.get("value", 5.0))
                if val < 0:
                    return "⚠️ cooldown은 0 이상이어야 합니다"
                e.blackout_cooldown = val
                return f"✅ 블랙아웃 쿨다운 설정: {val}초"

            elif a == "set_buffer_seconds":
                # buffer_seconds 설정 (AI Chat에서 접근 불가했던 항목)
                val = int(cmd.get("value", 40))
                if val < 5:
                    return "⚠️ 버퍼는 최소 5초 이상이어야 합니다"
                e.buffer_seconds = val
                return f"✅ 버퍼 크기 설정: {val}초"

            elif a == "set_screen_fps":
                # actual_screen_fps 설정 (AI Chat에서 접근 불가했던 항목)
                val = float(cmd.get("value", 30.0))
                if not (1.0 <= val <= 120.0):
                    return "⚠️ FPS는 1~120 범위여야 합니다"
                e.actual_screen_fps = val
                return f"✅ 화면 FPS 설정: {val}fps"

            # ── 커널 제어 ────────────────────────────────────────────────────
            elif a == "kernel_run_all":
                if k is None:
                    return "⚠️ KernelEngine 미연결"
                if k.running:
                    return "⚠️ 커널이 이미 실행 중입니다"
                k.start(list(k.scripts))
                return f"✅ 커널 전체 실행 ({len(k.scripts)}개 스크립트)"

            elif a == "kernel_stop":
                if k is None:
                    return "⚠️ KernelEngine 미연결"
                k.stop(); return "✅ 커널 실행 중지"

            elif a == "kernel_status":
                if k is None:
                    return "⚠️ KernelEngine 미연결"
                return (f"🧠 커널\n  실행중={k.running}\n"
                        f"  현재스크립트={k.cur_title or '없음'}\n"
                        f"  스크립트수={len(k.scripts)}")

            # ── AI 메모리 제어 ───────────────────────────────────────────────
            elif a == "memory_status":
                return (f"🧠 AI 메모리\n"
                        f"  단기히스토리: {len(self._history)}개 항목\n"
                        f"  장기기억: {self._long_term_tokens}토큰\n"
                        f"  마지막요청 추정토큰: {self._last_token_est}\n"
                        f"  경고임계값: {self._TOKEN_WARN}토큰\n"
                        f"  압축임계값: {self._TOKEN_COMPRESS}토큰")

            elif a == "memory_compress":
                self._compress_history_to_long_term()
                return (f"✅ 히스토리 압축 완료\n"
                        f"  장기기억: {self._long_term_tokens}토큰")

            elif a == "memory_clear":
                with self._lock:
                    self._history.clear()
                return "✅ 단기 히스토리 초기화"

            elif a == "can_devices":
                devs = _can_manager.get_vector_devices()
                if not devs: return "⚠️ Vector 장비 없음"
                out = "\n".join(f"  {d['name']} [{d['type']}]" for d in devs)
                return f"🔌 Vector 장비 {len(devs)}개:\n" + out

            elif a == "chat":
                return str(cmd.get("message",""))

            # ── CAN 액션 ────────────────────────────────────────────────
            elif a == "can_read":
                sig_name = cmd.get("signal", cmd.get("sig_name", ""))
                if not sig_name:
                    return "⚠️ signal 파라미터 필요"
                # [버그 수정] python-can 직접 연결(connected)과 CANoe COM 모드(canoe_mode) 모두 허용
                is_can_active = _can_manager.connected or _can_manager.canoe_mode
                if not is_can_active:
                    return ("⚠️ CAN 미연결\n"
                            "→ CAN Monitor 창에서 DBC 로드 + CANoe 연결 후 신호를 그래프에 추가하세요.")
                # sig_cache에서 직접 읽기
                entry = _can_manager.sig_cache.get(sig_name)
                if entry:
                    ts, val = entry
                    from datetime import datetime as _dt
                    age = _dt.now().timestamp() - ts
                    return f"📡 {sig_name} = {val}  ({age:.1f}초 전 수신)"
                # 캐시에 없으면 폴링 등록 후 잠시 대기
                msg_name = ""
                if hasattr(_can_manager, '_all_sigs_ref'):
                    for s in (_can_manager._all_sigs_ref or []):
                        if s.get('name') == sig_name:
                            msg_name = s.get('msg', ''); break
                _can_manager.add_poll_signal(sig_name, msg_name)
                import time as _t2
                _t2.sleep(0.5)
                entry2 = _can_manager.sig_cache.get(sig_name)
                if entry2:
                    ts2, val2 = entry2
                    return f"📡 {sig_name} = {val2}  (폴링 등록 후 수신)"
                return (f"⚠️ {sig_name}: 수신 대기 중\n"
                        f"→ CAN Monitor의 신호 탐색 탭에서 이 신호를 그래프에 추가해주세요.\n"
                        f"→ 또는 커널 스크립트에서: can.add_poll_signal('{sig_name}', '메시지명')")

            elif a == "can_read_all":
                if not (_can_manager.connected or _can_manager.canoe_mode):
                    return "⚠️ CAN 미연결 (CAN Monitor 창에서 DBC 로드 + CANoe 연결해주세요)"
                # _EV_, _DIAG_, _POLL_ERR_, _PREV_ 키 제외한 실제 신호만
                real_sigs = {
                    k: v[1] for k, v in _can_manager.sig_cache.items()
                    if not k.startswith('_') and isinstance(v, tuple) and len(v) == 2
                }
                if not real_sigs:
                    return ("⚠️ 폴링 중인 신호 없음\n"
                            "→ CAN Monitor에서 신호를 그래프에 추가하면 자동 폴링됩니다.")
                lines_out = [f"  {k} = {v}" for k, v in list(real_sigs.items())[:30]]
                return f"📡 CAN 신호 현재값 ({len(real_sigs)}개):\n" + "\n".join(lines_out)

            elif a == "can_write" or a == "canoe_write":
                msg  = cmd.get("message", cmd.get("msg_name", ""))
                sig  = cmd.get("signal",  cmd.get("sig_name", ""))
                val  = cmd.get("value",   cmd.get("val", 0))
                if not msg or not sig:
                    return "⚠️ message, signal, value 파라미터 필요"
                ok = _can_manager.canoe_write(msg, {sig: float(val)})
                status = _can_manager.status_msg
                if ok:
                    return f"✅ CAN 출력: {msg}.{sig} = {val}"
                return f"❌ CAN 출력 실패: {status}"

            elif a == "can_wait":
                sig      = cmd.get("signal", "")
                expected = cmd.get("expected", cmd.get("value", 0))
                timeout  = float(cmd.get("timeout", 10.0))
                if not sig:
                    return "⚠️ signal, expected, timeout 파라미터 필요"
                import threading as _th
                result_box = [None]
                def _wait():
                    result_box[0] = _can_manager.can_wait_value(
                        sig, expected, timeout)
                t = _th.Thread(target=_wait, daemon=True)
                t.start(); t.join(timeout + 1.0)
                ok = result_box[0]
                if ok:
                    return f"✅ {sig} = {expected} 확인됨 ({timeout}초 내)"
                return f"❌ {sig} = {expected} 미확인 ({timeout}초 타임아웃)"

            elif a == "can_status":
                is_can_ok = _can_manager.connected or _can_manager.canoe_mode
                if not is_can_ok:
                    return "⚠️ CAN 미연결 (CAN Monitor 창에서 DBC 로드 + CANoe 연결해주세요)"
                return (f"📡 CANoe {_can_manager.status_msg}\n"
                        f"  연결: {'✅' if _can_manager.connected else '❌'}  "
                        f"  모드: {'CANoe COM' if _can_manager.canoe_mode else 'python-can'}\n"
                        f"  폴링 신호: {len(_can_manager.sig_cache)}개 캐시됨")

            else:
                return (f"⚠️ 알 수 없는 액션: '{a}'\n"
                        f"이 프로그램에서 지원하지 않는 기능입니다.\n"
                        f"녹화·캡처·ROI·전원제어·CAN 제어·커널 스크립트 작성만 가능합니다.")

        except Exception as ex:
            return f"❌ 실행 오류: {ex}"


