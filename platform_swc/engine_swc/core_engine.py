# -*- coding: utf-8 -*-
"""
engine_swc/core_engine.py
비-UI 핵심 엔진 — 녹화·카메라·스크린·블랙아웃·ROI 연산 캡슐화.

의존: common/* 만 참조. kernel_swc/can_swc/ui_swc 역방향 임포트 금지.
KernelEngine·CanBusManager는 MainWindow에서 DI로 주입 (§2.5).
"""
import os, sys, json, threading, time, queue, platform, subprocess, re
from datetime import datetime
from collections import deque
from typing import List, Optional, Dict, Any

import cv2
import numpy as np
import mss as _mss_lib

try:
    from PIL import Image as _PIL_Image, ImageDraw as _PIL_Draw, ImageFont as _PIL_Font
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from common.constants import BASE_DIR
from common.models import RoiItem, MacroStep, ScheduleEntry, MemoOverlayCfg
from common.signals import Signals
from common.utils import draw_time_bar, draw_memo_overlay, _get_font, fmt_hms
from engine_swc.settings_db import IoChannelDB

class CoreEngine:
    """
    모든 비-UI 로직 캡슐화.

    [통합Macro/AI 외부제어 설계]
    - io_channel: IoChannelDB 인스턴스. 외부 프로세스(LLM/MCP 등)는
      io_channel.db 의 commands 테이블에 명령을 INSERT 하면
      CoreEngine._io_poll_loop 가 폴링 후 실행하고 결과를 기록.
    - 지원 명령: start_recording, stop_recording, save_manual_clip,
                 start_ac, stop_ac, capture_frame, set_roi, clear_roi,
                 get_state

    [ROI 관리]
    - screen_rois, camera_rois 는 List[RoiItem] 으로 통일
    - 기존 (rx,ry,rw,rh) tuple 대신 RoiItem.rect() 로 접근
    """
    MANUAL_IDLE = 0; MANUAL_WAITING = 1

    # ── 지원하는 외부 명령 목록 (자동화/AI 참조용) ───────────────────────────
    SUPPORTED_COMMANDS = [
        "start_recording", "stop_recording",
        "save_manual_clip", "capture_frame",
        "start_ac", "stop_ac", "reset_ac",
        "set_roi",    # args: {source, x, y, w, h, name, description}
        "clear_roi",  # args: {source: "screen"|"camera"|"all"}
        "get_state",  # result: 현재 상태 dict 반환
        "macro_run", "macro_stop",
    ]

    def __init__(self, signals: Signals, base_dir: str = BASE_DIR,
                 io_channel: "IoChannelDB" = None):
        self.signals    = signals
        self.base_dir   = base_dir
        self.io_channel = io_channel  # 외부제어 채널 (선택)

        # ── 녹화 상태 ───────────────────────────────────────────────────────
        self.recording    = False
        self.start_time:  float = 0.0
        self.output_dir:  str   = ""

        # ── 스레드 제어 ─────────────────────────────────────────────────────
        self._scr_stop = threading.Event()
        self._cam_stop = threading.Event()
        self._scr_thread: threading.Thread = None
        self._cam_thread: threading.Thread = None

        # ── FPS 측정 ────────────────────────────────────────────────────────
        self.actual_screen_fps: float = 30.0
        self.actual_camera_fps: float = 30.0
        self._scr_fps_ts: deque = deque(maxlen=90)
        self._cam_fps_ts: deque = deque(maxlen=90)

        # ── 큐 (미리보기용) — maxsize로 병목 방지 ───────────────────────────
        self.screen_queue: queue.Queue = queue.Queue(maxsize=3)
        self.camera_queue: queue.Queue = queue.Queue(maxsize=3)

        # ── 라이터 ─────────────────────────────────────────────────────────
        self._scr_writer = None; self._cam_writer = None
        self._writer_lock = threading.Lock()
        self._seg_start_time: float = 0.0
        self._ov_ts_cache: str   = ''        # overlay 타임스탬프 캐시
        self._ov_ts_mono:  float = 0.0       # 캐시 마지막 갱신 시각
        self._scr_rects_cache: list = []     # screen roi_rects 캐시
        self._scr_roi_ver: int = -1
        self._cam_rects_cache: list = []     # camera roi_rects 캐시
        self._cam_roi_ver: int = -1
        self._scr_fidx = 0; self._cam_fidx = 0
        self.segment_duration: float = 30 * 60
        self._seg_switching: bool = False

        # ── 설정 ────────────────────────────────────────────────────────────
        self.screen_rec_enabled     = True
        self.camera_rec_enabled     = True
        self._blackout_rec_enabled  = True   # property 백킹 필드 (직접 접근 금지)
        # ★ 카운트만 하고 클립 녹화는 하지 않는 모드
        # True  → 블랙아웃 감지 시 카운트+로그만, 클립 저장 없음
        # False → 감지 시 카운트+로그+클립 저장 (기본값)
        self.blackout_count_only: bool = False
        self.playback_speed: float  = 1.0
        self.video_codec: str       = "mp4v"
        self.video_scale: float     = 1.0
        self.overlay_font_size: int = 18

        # ── T/C 검증 (기능별) ───────────────────────────────────────────────
        self.tc_rec_enabled:      bool = False
        self.tc_manual_enabled:   bool = False
        self.tc_blackout_enabled: bool = False
        self.tc_capture_enabled:  bool = False
        self.tc_verify_result: str = ""
        self.tc_tag_target_dir: str = ""
        self.tc_folder_idx:     int = 0   # 0=TC-ID, 1=차종, 2=Rec폴더, 3=커스텀

        # ── 저장 경로 (T/C 검증 ON 시 구성 경로 사용, OFF 시 기본 경로) ──────
        # use_custom_path_* 는 제거: tc_*_enabled 로 판단
        # tc ON → 구성 경로, tc OFF → base_dir 기본 경로

        # ── 버퍼 ────────────────────────────────────────────────────────────
        self.buffer_seconds: int = 40
        # ★ maxlen=1 placeholder — start_screen/start_camera에서 실제 크기로 교체
        self._scr_buf: deque = deque(maxlen=1)
        self._cam_buf: deque = deque(maxlen=1)
        self._buf_lock = threading.Lock()

        # ── 블랙아웃 post 수집 Queue 레지스트리 ──────────────────────────────
        # _save_bo_clip 스레드가 Queue를 등록하면 _screen_loop/_camera_loop가
        # 새 프레임을 직접 넣어준다. deque polling 방식의 "슬라이딩 창 혼입" 문제 근본 해결.
        self._scr_bo_queues: List[queue.Queue] = []   # screen post 수집기 목록
        self._cam_bo_queues: List[queue.Queue] = []   # camera post 수집기 목록
        self._bo_queue_lock = threading.Lock()         # 레지스트리 보호용 Lock

        # ── ROI (RoiItem 리스트) ─────────────────────────────────────────────
        self.screen_rois: List[RoiItem] = []
        self.camera_rois: List[RoiItem] = []
        self.screen_roi_avg     = []
        self.camera_roi_avg     = []
        self.screen_roi_prev    = []
        self.camera_roi_prev    = []
        self.screen_overall_avg = np.zeros(3)
        self.camera_overall_avg = np.zeros(3)

        # ── 블랙아웃 ─────────────────────────────────────────────────────────
        self.brightness_threshold: float = 30.0
        self.blackout_cooldown:    float = 5.0
        self._scr_last_bo: float = 0.0
        self._cam_last_bo: float = 0.0
        self.screen_bo_count = 0; self.camera_bo_count = 0
        # ★ maxlen=500 — 5일간 무제한 누적 방지 (500개 초과 시 오래된 것 자동 제거)
        self.screen_bo_events: deque = deque(maxlen=500)
        self.camera_bo_events: deque = deque(maxlen=500)
        self.blackout_dir = os.path.join(base_dir, "blackout")

        # ── 기능 ON/OFF 플래그 (자원 절약) ★ v2.9.3 ────────────────────────
        # ROI OCR 활성화 여부 — False면 _ocr_loop 가 ROI를 처리하지 않음
        self.ocr_enabled: bool = True         # 하위 호환 유지
        self.ocr_screen_enabled: bool = True  # ★ v4.5: Display OCR 독립 ON/OFF
        self.ocr_camera_enabled: bool = True  # ★ v4.5: Camera OCR 독립 ON/OFF
        # ROI 밝기 계산 활성화 여부 — False면 update_frame에서 밝기 연산 스킵
        self.brightness_enabled: bool = True

        # ── 메모 / 오버레이 ─────────────────────────────────────────────────
        self.memo_texts: list = [""]
        self.memo_overlays: list = [MemoOverlayCfg(
            tab_idx=0, position="bottom-right", target="both",
            enabled=False, overlay_font_size=18)]

        # ── [수정1] 커널 로그 오버레이 ──────────────────────────────────────
        # KernelPanel이 _on_log_safe()에서 이 리스트를 동기화.
        # _apply_overlays()가 source별로 참조해 프레임에 합성.
        self.kernel_log_overlay_lines: list = []   # 현재 사이클 로그 줄 목록
        self.kernel_log_overlay_on_cam: bool = False  # 웹캠 오버레이 활성
        self.kernel_log_overlay_on_scr: bool = False  # 디스플레이 오버레이 활성
        self._kernel_log_lock = threading.Lock()       # overlay_lines 보호

        # ── 수동녹화 ────────────────────────────────────────────────────────
        self.manual_pre_sec:  float = 10.0
        self.manual_post_sec: float = 10.0
        self.manual_source:   str   = "both"
        self.manual_dir = os.path.join(base_dir, "manual_clip")
        self.manual_state = self.MANUAL_IDLE
        self._manual_lock = threading.Lock()
        self._manual_trigger: float = 0.0

        # ── 오토클릭 ────────────────────────────────────────────────────────
        self.ac_enabled   = False
        self.ac_interval: float = 1.0
        self.ac_count     = 0
        self.ac_btn       = "left"    # "left" | "right" | "middle"
        self.ac_double    = False     # 더블클릭 여부
        self.ac_use_pos   = False     # True: 지정 좌표, False: 현재 커서
        self.ac_pos_x     = 0         # 클릭 X 좌표
        self.ac_pos_y     = 0         # 클릭 Y 좌표
        self._ac_stop   = threading.Event()
        self._ac_thread: threading.Thread = None

        # ── 매크로 ──────────────────────────────────────────────────────────
        self.macro_steps:    list = []
        self.macro_running   = False
        self.macro_recording = False
        self.macro_repeat    = 1
        self.macro_gap       = 1.0
        self._mac_stop    = threading.Event()
        # ★ 슬롯 캐시 — MacroPanel이 set_slots_cache()로 동기화
        self._macro_slots: list = []   # [{'title':str,'steps':[MacroStep,...]}]
        self._mac_thread: threading.Thread = None
        self._mac_listener = None
        self._mac_mouse_listener = None
        self._mac_key_listener   = None

        # ── 예약 ────────────────────────────────────────────────────────────
        self.schedules: list = []

        # ── 카메라 / 모니터 ─────────────────────────────────────────────────
        self.camera_list:  list = []
        self.monitor_list: list = []
        self.active_cam_idx:     int = 0
        self.active_monitor_idx: int = 1

        # ── 저장 경로 설정 ──────────────────────────────────────────────────
        self.vehicle_type:    str  = ""
        self.tc_id:           str  = ""
        self.extra_segments:  list = []

        # ── I/O 폴링 스레드 ─────────────────────────────────────────────────
        self._io_stop = threading.Event()
        self._io_thread: threading.Thread = None

    # ── 하위 호환 프로퍼티 ────────────────────────────────────────────────────
    # ── blackout_rec_enabled property — setter에서 UI 시그널 자동 emit ──────
    @property
    def blackout_rec_enabled(self) -> bool:
        return self._blackout_rec_enabled

    @blackout_rec_enabled.setter
    def blackout_rec_enabled(self, value: bool):
        value = bool(value)
        if self._blackout_rec_enabled == value:
            return
        self._blackout_rec_enabled = value
        # UI 체크박스 동기화 시그널 emit (Qt 메인스레드에서 수신)
        try:
            self.signals.blackout_rec_changed.emit(value)
        except Exception:
            pass

    @property
    def tc_verify_enabled(self) -> bool:
        return self.tc_rec_enabled

    @tc_verify_enabled.setter
    def tc_verify_enabled(self, v: bool):
        self.tc_rec_enabled = v

    # ── ROI 헬퍼: tuple 리스트 변환 (블랙아웃 감지 내부 호환) ────────────────
    def _roi_rects(self, source: str) -> list:
        """(x,y,w,h) tuple 리스트 반환 — 내부 감지 로직 호환."""
        rois = self.screen_rois if source == "screen" else self.camera_rois
        return [r.rect() for r in rois]

    # ── 경로 빌더 ─────────────────────────────────────────────────────────────
    def _make_output_dir(self) -> str:
        return self._build_path("rec")

    def _build_path(self, feature: str) -> str:
        """
        tc_*_enabled=True 면 구성 경로(vehicle_type/날짜/tc_id/extra/기능폴더),
        False 면 기본 경로(base_dir/날짜/기능폴더).
        """
        tc_on = {
            "rec":      self.tc_rec_enabled,
            "manual":   self.tc_manual_enabled,
            "blackout": self.tc_blackout_enabled,
            "capture":  self.tc_capture_enabled,
        }.get(feature, False)

        ts_date = datetime.now().strftime("%Y%m%d")
        ts_time = datetime.now().strftime("%H%M%S")

        if tc_on:
            parts = [self.base_dir]
            if self.vehicle_type: parts.append(self.vehicle_type)
            parts.append(ts_date)
            if self.tc_id: parts.append(self.tc_id)
            for seg in self.extra_segments:
                if seg and seg.strip(): parts.append(seg.strip())
        else:
            parts = [self.base_dir, ts_date]

        _suffix = {
            "rec":      f"Rec_{ts_time}",
            "manual":   "manual_clip",
            "blackout": "blackout",
            "capture":  "capture",
        }
        parts.append(_suffix.get(feature, feature))
        return os.path.join(*parts)

    def _apply_tc_result_to_folder(self, result: str, target_dir: str = "") -> str:
        """
        대상 폴더명에 (PASS)/(FAIL) 태그 삽입.

        ★ v2.9 수정: 동일 TC 중복 검증 처리
          - 기존 태그와 결과가 동일 → 폴더명 유지 (같은 폴더에 계속 저장)
          - 기존 태그와 결과가 다름  → 폴더 분리 (새 이름으로 복사/이동)
          - 태그 없음               → 태그 삽입 (기존 동작)
        """
        d = target_dir or self.tc_tag_target_dir or self.output_dir
        if not d:
            self.signals.status_message.emit("[T/C] 태그 대상 경로 없음 — 건너뜀")
            return d

        parent   = os.path.dirname(d)
        old_name = os.path.basename(d)
        if not parent:
            self.signals.status_message.emit("[T/C] 태그 대상 경로 오류 — 건너뜀")
            return d

        # ★ 수정: 폴더가 실제로 없어도 새 이름으로 바로 생성
        #   (capture/manual은 _apply_tc 시점에 폴더가 아직 없을 수 있음)
        d_exists = os.path.exists(d)

        # 기존 태그 파싱
        m_existing = re.match(r'^\((PASS|FAIL)\)\s*(.*)', old_name)
        if m_existing:
            existing_result = m_existing.group(1)
            clean           = m_existing.group(2)
            if existing_result == result:
                # 동일 결과 → 폴더명 그대로 유지
                if not d_exists:
                    os.makedirs(d, exist_ok=True)
                self.signals.status_message.emit(
                    f"[T/C] 동일 결과({result}) — 기존 폴더 재사용: {old_name}")
                return d
            else:
                # 다른 결과 → 새 폴더로 분리
                new_base = f"({result}) {clean}"
                new_path = os.path.join(parent, new_base)
                suffix   = 1
                while os.path.exists(new_path):
                    new_path = os.path.join(parent, f"{new_base} ({suffix})")
                    suffix  += 1
                os.makedirs(new_path, exist_ok=True)
                if d_exists:
                    try: os.rename(d, new_path)
                    except Exception: pass
                self.signals.status_message.emit(
                    f"[T/C] 결과 상이({existing_result}→{result}) — 새 폴더: {os.path.basename(new_path)}")
                return new_path
        else:
            # 태그 없음 → 태그 삽입
            clean    = re.sub(r'^\((PASS|FAIL)\)\s*', '', old_name)
            new_name = f"({result}) {clean}"
            new_path = os.path.join(parent, new_name)
            if d_exists:
                # 폴더가 이미 있으면 rename
                try:
                    os.rename(d, new_path)
                    self.signals.status_message.emit(f"[T/C] 폴더명 → {new_name}")
                    return new_path
                except Exception as ex:
                    self.signals.status_message.emit(f"[T/C] 폴더 이름 변경 실패: {ex}")
                    return d
            else:
                # ★ 폴더가 없으면 새 이름으로 바로 생성 (capture/manual 케이스)
                os.makedirs(new_path, exist_ok=True)
                self.signals.status_message.emit(f"[T/C] 새 태그 폴더 생성 → {new_name}")
                return new_path

    def _find_existing_tagged_folder(self, parent: str, base_name: str) -> str:
        """
        parent 디렉터리에서 '(PASS) base_name' 또는 '(FAIL) base_name' 형태의
        기존 태그 폴더를 찾아 반환. 없으면 빈 문자열 반환.
        여러 개 있을 경우 가장 최근 수정 시각 기준 반환.
        """
        if not parent or not os.path.isdir(parent):
            return ""
        pattern = re.compile(r'^\((PASS|FAIL)\)\s+' + re.escape(base_name) + r'(\s+\(\d+\))?$')
        matches = []
        try:
            for name in os.listdir(parent):
                if pattern.match(name):
                    full = os.path.join(parent, name)
                    if os.path.isdir(full):
                        matches.append(full)
        except Exception:
            return ""
        if not matches:
            return ""
        # 가장 최근 수정된 폴더 반환
        return max(matches, key=lambda p: os.path.getmtime(p))

    def _resolve_tc_save_dir(self, feature: str, result: str) -> str:
        """
        TC 태그가 적용된 실제 파일 저장 경로를 반환.

        설계 원칙:
          - TC 태그 대상(tag_dir)은 기능 폴더의 '상위' 폴더
            (TC-ID 폴더, 차종 폴더 등)
          - 파일은 tag_dir/(PASS or FAIL) 처리된 폴더/기능서브폴더 에 저장
          - 동일 결과의 태그 폴더가 이미 존재하면 그 안에 계속 증적 누적
          - 기능 서브폴더: rec→Rec_* 제외(output_dir 직접 사용),
                           manual→manual_clip, capture→capture, blackout→blackout

        idx:
          0 = TC-ID 폴더  : base/[vehicle]/날짜/tc_id
          1 = 차종_버전   : base/vehicle_type
          2 = 각 기능 폴더: 기능의 save_dir 자체를 tag_dir로 사용
          3 = 직접 입력

        예외 발생 시 빈 문자열 반환 → 호출자가 _build_path로 폴백.
        """
        try:
            return self._resolve_tc_save_dir_inner(feature, result)
        except Exception as ex:
            self.signals.status_message.emit(
                f"[T/C 경로] 계산 오류: {ex} — 기본 경로 사용")
            return ""

    def _resolve_tc_save_dir_inner(self, feature: str, result: str) -> str:
        """_resolve_tc_save_dir 실제 구현 — 예외를 호출자로 전파."""
        idx = getattr(self, 'tc_folder_idx', 0)
        ts_date = datetime.now().strftime("%Y%m%d")

        # ── tag_dir 계산 (TC 태그 처리할 상위 폴더) ─────────────────────────
        if idx == 0:
            # TC-ID 폴더: base/[vehicle]/날짜/tc_id
            tc = (self.tc_id or "").strip()
            if not tc:
                return ""
            parts = [self.base_dir]
            if self.vehicle_type: parts.append(self.vehicle_type)
            parts.append(ts_date)
            # 이미 태그된 폴더가 있는지 검색
            parent_of_tc = os.path.join(*parts)
            existing = self._find_existing_tagged_folder(parent_of_tc, tc)
            if existing:
                # 기존 태그 폴더 — 결과가 같으면 그 안, 다르면 새 폴더
                existing_result = re.match(r'^\((PASS|FAIL)\)', os.path.basename(existing))
                if existing_result and existing_result.group(1) == result:
                    tag_dir = existing   # 동일 결과 → 그대로 재사용
                else:
                    # 결과 다름 → 새 태그 폴더 생성
                    new_base = f"({result}) {tc}"
                    new_path = os.path.join(parent_of_tc, new_base)
                    suffix = 1
                    while os.path.exists(new_path):
                        new_path = os.path.join(parent_of_tc, f"{new_base} ({suffix})")
                        suffix += 1
                    os.makedirs(new_path, exist_ok=True)
                    tag_dir = new_path
            else:
                # 기존 태그 없음 → 새로 태그 폴더 생성
                tag_dir = os.path.join(parent_of_tc, f"({result}) {tc}")
                os.makedirs(tag_dir, exist_ok=True)

        elif idx == 1:
            # 차종_버전 폴더: base/vehicle_type
            vt = (self.vehicle_type or "").strip()
            if not vt:
                # 차종_버전 미설정 → 기본 경로 폴백 (빈 문자열 반환해서 호출자가 _build_path 사용)
                return ""
            else:
                parent_of_vt = self.base_dir
                existing = self._find_existing_tagged_folder(parent_of_vt, vt)
                if existing:
                    existing_result = re.match(r'^\((PASS|FAIL)\)', os.path.basename(existing))
                    if existing_result and existing_result.group(1) == result:
                        tag_dir = existing
                    else:
                        new_base = f"({result}) {vt}"
                        new_path = os.path.join(parent_of_vt, new_base)
                        suffix = 1
                        while os.path.exists(new_path):
                            new_path = os.path.join(parent_of_vt, f"{new_base} ({suffix})")
                            suffix += 1
                        os.makedirs(new_path, exist_ok=True)
                        tag_dir = new_path
                else:
                    # 기존 태그 없음 → (PASS/FAIL) 차종 폴더 새로 생성
                    orig   = os.path.join(parent_of_vt, vt)
                    tagged = os.path.join(parent_of_vt, f"({result}) {vt}")
                    if os.path.isdir(orig):
                        # 원본 폴더가 있으면 rename (태그 없는 폴더를 태그 폴더로 전환)
                        try:
                            os.rename(orig, tagged)
                        except Exception:
                            # rename 실패 시 (다른 프로세스 사용 중 등) tagged로 새 폴더 생성
                            os.makedirs(tagged, exist_ok=True)
                    else:
                        # 원본 폴더 없음 → 처음부터 태그 폴더로 생성
                        os.makedirs(tagged, exist_ok=True)
                    tag_dir = tagged

        elif idx == 2:
            # 각 기능 폴더 자체에 태그 — feature 별 save_dir 직접 반환
            # 이 경우 tag_dir = None 반환 → 각 기능이 자체 save_dir을 태깅
            return ""   # 호출자에서 idx==2 처리

        else:
            # 직접 입력 경로
            tag_dir = (self.tc_tag_target_dir or "").strip()
            if not tag_dir:
                return ""
            existing = self._find_existing_tagged_folder(
                os.path.dirname(tag_dir), os.path.basename(tag_dir))
            if existing:
                existing_result = re.match(r'^\((PASS|FAIL)\)', os.path.basename(existing))
                if existing_result and existing_result.group(1) == result:
                    tag_dir = existing
                else:
                    new_base = f"({result}) {os.path.basename(tag_dir)}"
                    new_path = os.path.join(os.path.dirname(tag_dir), new_base)
                    suffix = 1
                    while os.path.exists(new_path):
                        new_path = os.path.join(os.path.dirname(tag_dir),
                                                 f"{new_base} ({suffix})")
                        suffix += 1
                    os.makedirs(new_path, exist_ok=True)
                    tag_dir = new_path
            else:
                tagged = os.path.join(os.path.dirname(tag_dir),
                                      f"({result}) {os.path.basename(tag_dir)}")
                if os.path.isdir(tag_dir):
                    try: os.rename(tag_dir, tagged)
                    except Exception: tagged = tag_dir
                os.makedirs(tagged, exist_ok=True)
                tag_dir = tagged

        # ── 기능 서브폴더 결정 ───────────────────────────────────────────────
        _subfolder = {
            "manual":   "manual_clip",
            "capture":  "capture",
            "blackout": "blackout",
        }
        sub = _subfolder.get(feature, "")
        if sub:
            save_dir = os.path.join(tag_dir, sub)
        else:
            save_dir = tag_dir
        os.makedirs(save_dir, exist_ok=True)
        return save_dir

    # ── 헬퍼 ─────────────────────────────────────────────────────────────────
    def measured_fps(self, dq: deque) -> float:
        if len(dq) < 2: return 0.0
        sp = dq[-1] - dq[0]
        return (len(dq)-1)/sp if sp > 0 else 0.0

    def _fourcc(self):
        if self.video_codec == "avc1":
            import tempfile
            try:
                tmp = tempfile.mktemp(suffix='.mp4')
                test_wr = cv2.VideoWriter(
                    tmp, cv2.VideoWriter_fourcc(*'avc1'), 30.0, (2, 2))
                ok = test_wr.isOpened()
                test_wr.release()
                try: os.remove(tmp)
                except: pass
                if ok:
                    return cv2.VideoWriter_fourcc(*'avc1')
            except Exception:
                pass
            self.signals.status_message.emit(
                "⚠ avc1(H.264) 코덱을 사용할 수 없습니다. mp4v 로 자동 전환합니다.")
            self.video_codec = "mp4v"
            return cv2.VideoWriter_fourcc(*'mp4v')
        elif self.video_codec == "xvid":
            # XVID는 .avi 컨테이너에서만 동작 — .mp4 저장 시 mp4v로 폴백
            return cv2.VideoWriter_fourcc(*'mp4v')
        else:
            return cv2.VideoWriter_fourcc(*'mp4v')

    def _scale(self, frame: np.ndarray) -> np.ndarray:
        if self.video_scale >= 1.0: return frame
        h,w = frame.shape[:2]
        return cv2.resize(frame, (max(2,int(w*self.video_scale)),
                                  max(2,int(h*self.video_scale))),
                          interpolation=cv2.INTER_AREA)

    @property
    def _scr_buf_max(self): return max(1, int(self.actual_screen_fps*self.buffer_seconds))
    @property
    def _cam_buf_max(self): return max(1, int(self.actual_camera_fps*self.buffer_seconds))

    # ── 모니터 스캔 ───────────────────────────────────────────────────────────
    def scan_monitors(self):
        # ★ 중복 스캔 방지 (scan_cameras와 동일한 패턴)
        if getattr(self, '_mon_scanning', False):
            return
        self._mon_scanning = True
        found = []
        try:
            with _mss_lib.mss() as s:
                for i, m in enumerate(s.monitors):
                    name = ("전체 합성" if i==0 else f"Display {i}") + f"  ({m['width']}×{m['height']})"
                    found.append({"idx":i,"name":name,"w":m["width"],"h":m["height"]})
        except Exception as e:
            self.signals.status_message.emit(f"모니터 스캔 실패: {e}")
        self.monitor_list = found
        if not any(m["idx"]==self.active_monitor_idx for m in found):
            self.active_monitor_idx = found[0]["idx"] if found else 0
        self._mon_scanning = False
        self.signals.monitors_scanned.emit(found)

    # ── 카메라 스캔 ───────────────────────────────────────────────────────────
    def scan_cameras(self):
        """
        카메라 스캔.

        [문제1 수정] 스캔 시 프로그램 종료 (크래시)
          근본 원인: cv2.VideoCapture(idx, CAP_MSMF) 가 일부 환경에서
                     MSMF COM 초기화 실패 시 UnhandledException -> 프로세스 전체 강제 종료.
                     Thread 격리로는 막을 수 없음 (스레드 크래시 = 프로세스 종료).
          수정:
            VideoCapture 탐색을 별도 subprocess 로 격리.
            subprocess 크래시 -> 부모 프로세스 영향 없음.
            결과는 JSON stdout 으로 수신.
            subprocess 실패/타임아웃(20초) 시 빈 결과로 안전 처리.
            subprocess 불가 환경(frozen EXE 등) 시 기존 인프로세스 방식으로 폴백.
        """
        if getattr(self, '_cam_scanning', False):
            return
        self._cam_scanning = True
        try:
            self._do_scan_cameras()
        finally:
            self._cam_scanning = False

    def _do_scan_cameras(self):
        """
        카메라 스캔 실행부.

        [문제1 수정] subprocess 격리로 MSMF COM 크래시 방지.
          VideoCapture 초기화 코드를 별도 Python 프로세스에서 실행.
          해당 프로세스가 죽어도 이 프로세스는 영향 없음.
          결과를 JSON stdout 으로 수신 후 파싱.
          subprocess 실패(크래시/타임아웃) 시 -> 빈 결과로 안전 처리.
        """
        import tempfile, textwrap

        self.signals.status_message.emit("📷 카메라 스캔 시작...")

        # ── 서브프로세스로 실행할 스캔 스크립트 ─────────────────────────────
        _scan_code = textwrap.dedent('''
            import sys, json, os, time as _t
            os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
            os.environ.setdefault("OPENCV_VIDEOIO_DEBUG", "0")
            try:
                import cv2
                try: cv2.setLogLevel(0)
                except: pass
            except Exception as e:
                print(json.dumps([])); sys.exit(0)
            import platform as _pl

            _STANDARD_FPS = [15.0, 24.0, 25.0, 30.0, 50.0, 60.0]
            def snap_fps(fps):
                best = min(_STANDARD_FPS, key=lambda s: abs(s - fps))
                return best if abs(best - fps) <= 5.0 else fps

            _sys = _pl.system()
            if _sys == "Windows":
                backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
            elif _sys == "Linux":
                backends = [cv2.CAP_V4L2, cv2.CAP_ANY]
            else:
                backends = [cv2.CAP_ANY]

            found = []
            for idx in range(8):
                cap = None
                for backend in backends:
                    try:
                        c = cv2.VideoCapture(idx, backend)
                        if not c.isOpened():
                            c.release(); continue
                        _ok = False
                        for _ in range(3):
                            ret, fr = c.read()
                            if ret and fr is not None:
                                _ok = True; break
                            _t.sleep(0.05)
                        if not _ok:
                            c.release(); continue
                        cap = c; break
                    except Exception:
                        pass
                if cap is None:
                    continue
                _mjpg = cv2.VideoWriter_fourcc(*"MJPG")
                try:
                    cap.set(cv2.CAP_PROP_FOURCC, _mjpg)
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                except: pass
                _applied = int(cap.get(cv2.CAP_PROP_FOURCC))
                if _applied != _mjpg:
                    try:
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    except: pass
                fps = 15.0
                try:
                    r = cap.get(cv2.CAP_PROP_FPS)
                    if r and 5.0 < r < 300.0:
                        fps = snap_fps(float(r))
                    else:
                        t0 = _t.perf_counter()
                        ret2, _ = cap.read()
                        el = _t.perf_counter() - t0
                        fps = snap_fps(1.0/el) if (ret2 and 0 < el < 0.3) else 15.0
                except: fps = 15.0
                nm = "Camera"
                try: nm = cap.getBackendName()
                except: pass
                _fmt = "MJPG" if _applied == _mjpg else "YUY2"
                try: cap.release()
                except: pass
                found.append({"idx": idx,
                              "name": f"Camera {idx} [{nm}/{_fmt}] {int(fps)}fps",
                              "fps": fps})
            print(json.dumps(found))
        ''')

        found = []
        _ran_subprocess = False

        # ── subprocess 방식 시도 ──────────────────────────────────────────────
        # EXE 환경에서는 sys.executable 이 Python 인터프리터임.
        # frozen(PyInstaller onedir) 환경: sys.frozen=True, sys._MEIPASS 존재.
        # frozen 환경에서도 sys.executable 은 번들된 Python 인터프리터를 가리키므로
        # subprocess 실행 가능.
        _py_exe = sys.executable
        try:
            # 임시 스크립트 파일 작성 (frozen 환경에서 -c 인자 길이 한계 우회)
            _tf = tempfile.NamedTemporaryFile(
                mode='w', suffix='.py', delete=False,
                encoding='utf-8', prefix='cam_scan_')
            _tf.write(_scan_code)
            _tf_name = _tf.name
            _tf.close()

            _proc = subprocess.Popen(
                [_py_exe, _tf_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if hasattr(subprocess, "CREATE_NO_WINDOW") else 0),
            )
            try:
                _out, _ = _proc.communicate(timeout=20)
                _ran_subprocess = True
                _text = _out.decode('utf-8', errors='replace').strip()
                if _text:
                    # JSON 배열만 추출 (앞뒤 노이즈 제거)
                    _s = _text.find('[')
                    _e = _text.rfind(']')
                    if _s != -1 and _e != -1:
                        found = json.loads(_text[_s:_e+1])
            except subprocess.TimeoutExpired:
                _proc.kill()
                self.signals.status_message.emit("📷 카메라 스캔 타임아웃 — 카메라 연결 확인")
            except Exception:
                pass
            finally:
                try:
                    os.unlink(_tf_name)
                except Exception:
                    pass
        except Exception:
            # subprocess 자체 실행 불가 -> 인프로세스 폴백
            pass

        # ── 인프로세스 폴백 (subprocess 완전 실패 시) ────────────────────────
        if not _ran_subprocess:
            self.signals.status_message.emit("📷 카메라 스캔 (인프로세스 모드)...")
            try:
                found = self._do_scan_cameras_inproc()
            except Exception as e:
                self.signals.status_message.emit(f"📷 카메라 스캔 실패: {e}")
                found = []

        self.camera_list = found

        if found:
            self.signals.status_message.emit(
                f"📷 카메라 {len(found)}개 발견: "
                + ", ".join(c["name"] for c in found))
        else:
            self.signals.status_message.emit(
                "❌ 카메라 없음 — USB 카메라 연결 확인 후 재탐색 버튼을 누르세요")

        if found:
            ids = [c["idx"] for c in found]
            if self.active_cam_idx not in ids:
                self.active_cam_idx = found[0]["idx"]
                self.actual_camera_fps = found[0]["fps"]
            else:
                cam = next((c for c in found if c["idx"] == self.active_cam_idx), found[0])
                self.actual_camera_fps = cam["fps"]

        self.signals.cameras_scanned.emit(found)

    def _do_scan_cameras_inproc(self) -> list:
        """
        인프로세스 카메라 스캔 폴백 (subprocess 불가 환경용).
        subprocess 격리 없이 실행되므로 크래시 위험이 있으나,
        마지막 수단으로만 호출됨.
        """
        import time as _tscan2

        _STANDARD_FPS = [15.0, 24.0, 25.0, 30.0, 50.0, 60.0]
        def snap_fps(fps):
            best = min(_STANDARD_FPS, key=lambda s: abs(s - fps))
            return best if abs(best - fps) <= 5.0 else fps

        _sys = platform.system()
        if _sys == "Windows":
            backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
        elif _sys == "Linux":
            backends = [cv2.CAP_V4L2, cv2.CAP_ANY]
        else:
            backends = [cv2.CAP_ANY]

        found = []
        _prev_log = 0
        try:
            _prev_log = cv2.getLogLevel()
            cv2.setLogLevel(0)
        except Exception:
            pass
        try:
            for idx in range(8):
                cap = None
                for backend in backends:
                    try:
                        _o = sys.stderr
                        try: sys.stderr = open(os.devnull, 'w')
                        except: pass
                        try: c = cv2.VideoCapture(idx, backend)
                        finally:
                            try:
                                if sys.stderr is not _o: sys.stderr.close()
                            except: pass
                            sys.stderr = _o
                        if not c.isOpened():
                            c.release(); continue
                        _ok = False
                        for _ in range(3):
                            ret, fr = c.read()
                            if ret and fr is not None:
                                _ok = True; break
                            _tscan2.sleep(0.05)
                        if not _ok:
                            c.release(); continue
                        cap = c; break
                    except Exception:
                        pass
                if cap is None:
                    continue
                _mjpg = cv2.VideoWriter_fourcc(*"MJPG")
                try:
                    cap.set(cv2.CAP_PROP_FOURCC, _mjpg)
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                except: pass
                _applied = int(cap.get(cv2.CAP_PROP_FOURCC))
                if _applied != _mjpg:
                    try:
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    except: pass
                fps = 15.0
                try:
                    r = cap.get(cv2.CAP_PROP_FPS)
                    if r and 5.0 < r < 300.0:
                        fps = snap_fps(float(r))
                    else:
                        t0 = _tscan2.perf_counter()
                        ret2, _ = cap.read()
                        el = _tscan2.perf_counter() - t0
                        fps = snap_fps(1.0/el) if (ret2 and 0 < el < 0.3) else 15.0
                except: fps = 15.0
                nm = "Camera"
                try: nm = cap.getBackendName()
                except: pass
                _fmt = "MJPG" if _applied == _mjpg else "YUY2"
                try: cap.release()
                except: pass
                found.append({"idx": idx,
                              "name": f"Camera {idx} [{nm}/{_fmt}] {int(fps)}fps",
                              "fps": fps})
        finally:
            try: cv2.setLogLevel(_prev_log)
            except: pass
        return found



    # ── ROI 밝기 계산 ─────────────────────────────────────────────────────────
    @staticmethod
    def calc_roi_avg(frame, rects):
        """rects: list of (x,y,w,h)"""
        avgs = []
        for rx,ry,rw,rh in rects:
            r = frame[ry:ry+rh, rx:rx+rw]
            avgs.append(r.mean(axis=0).mean(axis=0) if r.size>0 else np.zeros(3))
        return avgs

    # ── 블랙아웃 감지 ─────────────────────────────────────────────────────────
    def _detect_blackout(self, curr, prev, source: str) -> bool:
        # ★ 수정(v4.12-fix1):
        #   blackout_rec_enabled=False 이면 tc_blackout_enabled 상태와 무관하게
        #   카운트·이벤트·클립 저장을 모두 차단.
        #   tc_blackout_enabled 단독 사용 시에도 클립 녹화 없이 T/C 판정만 수행하려면
        #   blackout_rec_enabled 를 켜야 한다는 점을 Doc에 명시할 것.
        if not self.blackout_rec_enabled:
            return False
        if not curr or not prev or len(curr)!=len(prev): return False
        changes = []
        for c,p in zip(curr,prev):
            if np.all(p==0): continue
            cb = 0.114*c[0]+0.587*c[1]+0.299*c[2]
            pb = 0.114*p[0]+0.587*p[1]+0.299*p[2]
            changes.append(pb-cb)
        if not changes: return False
        mc = float(np.mean(changes))
        if mc < self.brightness_threshold: return False
        now = time.time()
        last = (self._scr_last_bo if source=="screen" else self._cam_last_bo)
        if now-last < self.blackout_cooldown: return False
        # ── 카운트 & 이벤트 (blackout_rec_enabled=True 인 경우만 여기 도달) ──
        if source=="screen": self._scr_last_bo=now; self.screen_bo_count+=1
        else:                self._cam_last_bo=now; self.camera_bo_count+=1
        _count  = self.screen_bo_count if source=="screen" else self.camera_bo_count
        _src_kr = "화면" if source=="screen" else "카메라"
        _log_ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.signals.blackout_log.emit(
            f"[{_log_ts}] ⬛ 블랙아웃 감지 [{_src_kr}]  "
            f"카운트={_count}회  밝기변화={mc:.1f}")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        ev = {'time': _log_ts,
              'brightness_change': mc, 'timestamp': ts}
        (self.screen_bo_events if source=="screen" else self.camera_bo_events).append(ev)
        self.signals.blackout_detected.emit(source, ev)
        if self.io_channel:
            self.io_channel.emit_event("blackout_detected",
                {"source": source, **ev})
        # ★ count_only 모드: 카운트+로그만, 클립 저장 없음
        if self.blackout_count_only:
            return True
        # 클립 저장 스레드 시작
        threading.Thread(target=self._save_bo_clip,
                         args=(source, ts), daemon=True,
                         name=f"BoClip_{source}_{ts}").start()
        return True

    def _save_bo_clip(self, source: str, timestamp: str):
        """블랙아웃 클립 저장 — TC 검증 반영"""
        save_dir = os.path.join(
            self._build_path("blackout"), source.upper())
        os.makedirs(save_dir, exist_ok=True)

        fps = max((self.actual_screen_fps if source == "screen"
                   else self.actual_camera_fps), 1.0)
        n_pre  = int(fps * 20)
        n_post = int(fps * 20)
        bo_count = self.screen_bo_count if source == "screen" else self.camera_bo_count
        trigger_time = time.time()

        # ★ 수정(v4.12-fix3): post 수집 방식 근본 교체 — Queue 직접 주입
        #   [이전 방식의 근본 문제]
        #   deque는 슬라이딩 윈도우: 새 프레임 append → 오래된 프레임 popleft.
        #   "cur_len - snap_len" 또는 "fps × 경과시간" 어떤 방식이든
        #   cur_list[-elapsed_frames:] 를 쓰면 deque 끝 N개를 가져오는데,
        #   20초가 지나면 그 N개 안에 "감지 이전(pre) 구간 프레임"이 포함되어
        #   결국 블랙아웃 감지 시점으로 계속 되돌아가는 현상 발생.
        #
        #   [해결] _screen_loop/_camera_loop가 새 프레임을 append할 때마다
        #   등록된 Queue에 직접 넣어준다 → 감지 시점 이후 프레임만 정확히 수집.
        #   pre 스냅샷은 Queue 등록 직전 buf 스냅샷으로 취득 (기존과 동일).

        # ── pre 수집 (감지 직전 버퍼 스냅샷) ─────────────────────────────────
        bo_q: queue.Queue = queue.Queue(maxsize=n_post + 60)
        with self._buf_lock:
            buf     = self._scr_buf if source == "screen" else self._cam_buf
            pre_all = list(buf)

        pre_clip = pre_all[-n_pre:] if len(pre_all) >= n_pre else pre_all

        # ── post 수집 Queue 등록 → loop가 새 프레임을 직접 넣어줌 ───────────
        reg = self._scr_bo_queues if source == "screen" else self._cam_bo_queues
        with self._bo_queue_lock:
            reg.append(bo_q)

        post     = []
        deadline = time.time() + 22.0
        try:
            while len(post) < n_post and time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    frame_item = bo_q.get(timeout=min(remaining, 0.1))
                    post.append(frame_item)
                except queue.Empty:
                    continue
        finally:
            # Queue 등록 해제 (반드시 실행)
            with self._bo_queue_lock:
                try: reg.remove(bo_q)
                except ValueError: pass

        post = post[:n_post]   # 초과 방지

        all_f = pre_clip + post[:n_post]
        if not all_f: return

        bi = len(pre_clip)
        h, w = all_f[0].shape[:2]
        final_vp = os.path.join(save_dir, f"blackout_{timestamp}.mp4")

        # ★ 수정: 한글/괄호 경로에서 VideoWriter 실패 → 임시 ASCII 경로 우회
        import tempfile as _tf2, shutil as _shu2
        tmp_fd2, tmp_vp2 = _tf2.mkstemp(suffix=".mp4", prefix="blackout_tmp_")
        os.close(tmp_fd2)
        try:
            wr = cv2.VideoWriter(tmp_vp2, self._fourcc(), fps, (w, h))
            if not wr.isOpened():
                wr.release()
                wr = cv2.VideoWriter(
                    tmp_vp2, cv2.VideoWriter_fourcc(*'mp4v'), fps, (w, h))
            if not wr.isOpened():
                self.signals.status_message.emit("[Blackout] VideoWriter 열기 실패")
                try: os.remove(tmp_vp2)
                except Exception: pass
                return

            for i, f in enumerate(all_f):
                fc = self._scale(f.copy())
                t_off   = (i - bi) / fps
                abs_t   = trigger_time + t_off
                ts_str  = datetime.fromtimestamp(abs_t).strftime("%H:%M:%S.") + \
                          f"{int((abs_t % 1) * 1000):03d}"
                phase   = "PRE " if i < bi else "POST"
                elapsed = f"{t_off:+.2f}s"
                draw_time_bar(fc, ts_str, f"BLACKOUT#{bo_count}  {phase}  {elapsed}")
                if i == bi:
                    cv2.rectangle(fc, (4, 4),
                                  (fc.shape[1]-4, fc.shape[0]-4), (0, 0, 255), 6)
                    cv2.putText(fc, f"BLACKOUT DETECTED  #{bo_count}",
                                (10, fc.shape[0]-12),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2, cv2.LINE_AA)
                wr.write(fc)
            wr.release()
            _shu2.move(tmp_vp2, final_vp)
        except Exception as ex:
            self.signals.status_message.emit(f"[Blackout] 저장 실패: {ex}")
            try: os.remove(tmp_vp2)
            except Exception: pass
            return

        self.signals.status_message.emit(f"[Blackout/{source}] → {final_vp}")
        if self.io_channel:
            self.io_channel.emit_event("blackout_clip_saved",
                {"source": source, "path": final_vp})

        if self.tc_blackout_enabled and self.tc_verify_result in ("PASS", "FAIL"):
            idx = getattr(self, 'tc_folder_idx', 0)
            if idx == 2:
                tag_dir  = save_dir
                new_path = self._apply_tc_result_to_folder(self.tc_verify_result, tag_dir)
                # idx==2: 폴더 태그 방식 — save_dir과 다를 때만 메시지
                if new_path and new_path != tag_dir:
                    self.signals.status_message.emit(
                        f"[블랙아웃/T/C] 최종 저장 경로 → {new_path}")
            else:
                dest_dir = self._resolve_tc_save_dir("blackout", self.tc_verify_result)
                new_path = dest_dir if dest_dir else save_dir
                # idx!=2: 이동/복사 방식 — 경로가 있으면 항상 메시지
                if new_path:
                    self.signals.status_message.emit(
                        f"[블랙아웃/T/C] 최종 저장 경로 → {new_path}")

    # ── 오버레이 합성 ─────────────────────────────────────────────────────────
    def _apply_overlays(self, frame: np.ndarray, source: str) -> np.ndarray:
        h, w = frame.shape[:2]
        if self.recording and self.start_time:
            # ★ 병목 수정: datetime.now()를 매 프레임 호출하지 않고 캐싱
            # 타임스탬프 문자열은 33ms(30fps)마다 1ms씩만 변하므로
            # 100ms마다 갱신해도 시각적 차이 없음
            _mono = time.monotonic()
            if not hasattr(self, '_ov_ts_cache') or \
               _mono - self._ov_ts_mono > 0.1:
                now = datetime.now()
                self._ov_ts_cache = (
                    now.strftime("%Y-%m-%d  %H:%M:%S.")
                    + f"{now.microsecond//1000:03d}")
                self._ov_ts_mono  = _mono
            ns = self._ov_ts_cache
            e  = time.time() - self.start_time
            draw_time_bar(frame, ns, f"REC  {fmt_hms(e)}.{int((e%1)*1000):03d}")
        for cfg in self.memo_overlays:
            if not cfg.enabled: continue
            if cfg.target!="both" and cfg.target!=source: continue
            if cfg.tab_idx >= len(self.memo_texts): continue
            text = self.memo_texts[cfg.tab_idx].strip()
            if text:
                draw_memo_overlay(frame, text.splitlines(), cfg.position,
                                  w, h, cfg.overlay_font_size)

        # ── [수정1] 커널 로그 오버레이 ─────────────────────────────────────
        _show_kernel_ov = (
            (source == "camera" and self.kernel_log_overlay_on_cam) or
            (source == "screen" and self.kernel_log_overlay_on_scr)
        )
        if _show_kernel_ov:
            with self._kernel_log_lock:
                _klines = list(self.kernel_log_overlay_lines)
            if _klines:
                draw_memo_overlay(frame, _klines, "top-left", w, h, 14)

        return frame

    # ── PTS 동기화 쓰기 ───────────────────────────────────────────────────────
    def _write_sync(self, writer, frame, fps, fidx, elapsed):
        # ★ 프레임 크기를 VideoWriter 크기에 맞게 자동 보정
        # Display3 등 해상도 불일치 시 "Failed to write frame" 방지
        try:
            wr_w = int(writer.get(cv2.CAP_PROP_FRAME_WIDTH))
            wr_h = int(writer.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fr_h, fr_w = frame.shape[:2]
            if wr_w > 0 and wr_h > 0 and (fr_w != wr_w or fr_h != wr_h):
                frame = cv2.resize(frame, (wr_w, wr_h),
                                   interpolation=cv2.INTER_AREA)
        except Exception:
            pass
        expected = int(elapsed * fps)
        diff     = expected - fidx
        if diff <= 0:
            try: writer.write(frame)
            except Exception: pass
            return fidx + 1
        # ★ 병목 수정: fill 상한을 3프레임으로 제한
        # 이전: min(diff, fps*2) → 60fps면 최대 120프레임 동기 write → 2초 블로킹
        # 수정: 최대 3프레임. 세그먼트 드랍은 PTS가 맞추므로 영상 연속성 무관.
        fill = min(diff, 3)
        for _ in range(fill):
            try: writer.write(frame)
            except Exception: break
        return fidx + fill

    # ── 세그먼트 생성 ─────────────────────────────────────────────────────────
    def _create_segment(self):
        self._seg_switching = True
        time.sleep(0.05)

        seg_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        with self._writer_lock:
            if self._scr_writer: self._scr_writer.release()
            if self._cam_writer: self._cam_writer.release()
            self._scr_writer = self._cam_writer = None

        scr_fps = max(1.0, self.actual_screen_fps * self.playback_speed)
        cam_fps = max(1.0, self.actual_camera_fps * self.playback_speed)
        fc = self._fourcc()

        if self.screen_rec_enabled:
            try:
                # ★ 실제 캡처 프레임 크기로 VideoWriter 생성
                # monitor dict 크기와 실제 프레임 크기가 다를 수 있음 (DPI 스케일링)
                with _mss_lib.mss() as s:
                    midx = min(self.active_monitor_idx, len(s.monitors) - 1)
                    mon  = s.monitors[midx]
                    img  = s.grab(mon)
                    raw_frame = cv2.cvtColor(np.array(img), cv2.COLOR_BGRA2BGR)
                rh, rw = raw_frame.shape[:2]
                sw = max(2, int(rw * self.video_scale))
                sh = max(2, int(rh * self.video_scale))
                # 짝수 보정 (코덱 요구사항)
                sw = sw if sw % 2 == 0 else sw - 1
                sh = sh if sh % 2 == 0 else sh - 1
                sp = os.path.join(self.output_dir, f"screen_{seg_ts}.mp4")
                with self._writer_lock:
                    self._scr_writer = cv2.VideoWriter(sp, fc, scr_fps, (sw, sh))
                if not self._scr_writer.isOpened():
                    # fallback: XVID
                    self._scr_writer = cv2.VideoWriter(
                        sp, cv2.VideoWriter_fourcc(*'mp4v'), scr_fps, (sw, sh))
                self.signals.status_message.emit(
                    f"Screen seg: {sp} ({sw}x{sh})")
            except Exception as e:
                self.signals.status_message.emit(f"Screen writer 오류: {e}")

        with self._buf_lock:
            cf = self._cam_buf[-1] if self._cam_buf else None
        if cf is not None and self.camera_rec_enabled:
            h, w = cf.shape[:2]
            cw = max(2, int(w * self.video_scale))
            ch = max(2, int(h * self.video_scale))
            cp = os.path.join(self.output_dir, f"camera_{seg_ts}.mp4")
            with self._writer_lock:
                self._cam_writer = cv2.VideoWriter(cp, fc, cam_fps, (cw, ch))
            self.signals.status_message.emit(f"Camera seg: {cp}")

        self._seg_start_time = time.time()
        self._scr_fidx = 0
        self._cam_fidx = 0
        self._seg_switching = False

    # ── ROI 드로잉 헬퍼 ──────────────────────────────────────────────────────
    def _draw_rois_on_frame(self, frame: np.ndarray, rois: List[RoiItem],
                            brightness_enabled: bool = False,
                            ocr_enabled: bool = False) -> None:
        """ROI 사각형·이름·밝기·OCR 텍스트를 프레임에 그림.

        [수정2] brightness_enabled/ocr_enabled 인자 추가.
        활성 시 ROI 박스 하단에 'L:xxx' / 'T:xxx' 값을 함께 표시.
        """
        for i, roi in enumerate(rois):
            rx, ry, rw, rh = roi.rect()
            cv2.rectangle(frame, (rx, ry), (rx+rw, ry+rh), (255, 60, 60), 2)
            label = roi.name if roi.name else f"ROI{i+1}"
            cv2.putText(frame, label, (rx, max(ry - 4, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 60, 60), 1)
            # ── 밝기값 표시 ──────────────────────────────────────────
            if brightness_enabled:
                b_str = f"L:{roi.last_brightness:.0f}"
                by = min(ry + rh - 4, frame.shape[0] - 4)
                cv2.putText(frame, b_str, (rx + 2, by),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (80, 220, 255), 1)
            # ── OCR 텍스트 표시 ──────────────────────────────────────
            if ocr_enabled:
                ocr_txt = getattr(roi, 'last_text', '') or ''
                if ocr_txt:
                    disp = ocr_txt[:16]  # 너무 길면 잘라냄
                    ty = max(ry + 14, 14)
                    cv2.putText(frame, f"T:{disp}", (rx + 2, ty),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 220, 60), 1)

    # ── 스크린 루프 ───────────────────────────────────────────────────────────
    def _screen_loop(self):
        with _mss_lib.mss() as sct:
            midx = min(self.active_monitor_idx, len(sct.monitors)-1)
            mon  = sct.monitors[midx]
            interval = 1.0/max(self.actual_screen_fps,1.0)
            next_t   = time.perf_counter()
            while not self._scr_stop.is_set():
                now = time.perf_counter()
                if next_t-now > 0: time.sleep(next_t-now)
                next_t += interval
                img   = sct.grab(mon)
                frame = cv2.cvtColor(np.array(img), cv2.COLOR_BGRA2BGR)
                self._scr_fps_ts.append(time.time())

                # ── ROI 밝기 계산 — blackout ON/OFF 무관하게 항상 수행 ★ ─────
                # ★ 병목 수정: _roi_rects()를 매 프레임 호출하지 않고 ROI 변경 시만 갱신
                _scr_roi_ver = getattr(self, '_scr_roi_ver', -1)
                _cur_scr_ver = len(self.screen_rois)  # ROI 수 변경을 버전으로 사용
                if _cur_scr_ver != _scr_roi_ver:
                    self._scr_rects_cache = self._roi_rects("screen")
                    self._scr_roi_ver = _cur_scr_ver
                rects = self._scr_rects_cache
                if rects:
                    avgs = self.calc_roi_avg(frame, rects)
                    self.screen_roi_avg = avgs
                    self.screen_overall_avg = (np.mean(avgs, axis=0)
                                               if avgs else np.zeros(3))
                    # ── 블랙아웃 감지 — blackout_rec_enabled=True 일 때만 ★ ──
                    if self.blackout_rec_enabled:
                        if self.screen_roi_prev:
                            self._detect_blackout(avgs, self.screen_roi_prev, "screen")
                        self.screen_roi_prev = [a.copy() for a in avgs]
                    else:
                        # 비활성 상태에도 prev 갱신 → 활성화 직후 첫 프레임 오탐 방지
                        self.screen_roi_prev = [a.copy() for a in avgs]

                stamped = self._apply_overlays(frame.copy(), "screen")
                if self.screen_rois:
                    self._draw_rois_on_frame(
                        stamped, self.screen_rois,
                        brightness_enabled=self.brightness_enabled,
                        ocr_enabled=self.ocr_screen_enabled)
                with self._buf_lock:
                    self._scr_buf.append(stamped)  # maxlen이 자동으로 오래된 프레임 제거
                # ── 블랙아웃 post 수집기에 프레임 직접 전달 ──────────────
                if self._scr_bo_queues:
                    with self._bo_queue_lock:
                        qs = list(self._scr_bo_queues)
                    for _bq in qs:
                        try: _bq.put_nowait(stamped)
                        except queue.Full: pass
                if self.recording and self.screen_rec_enabled and not self._seg_switching:
                    with self._writer_lock: w = self._scr_writer
                    if w:
                        el = time.time()-self._seg_start_time
                        self._scr_fidx = self._write_sync(
                            w, self._scale(stamped), self.actual_screen_fps,
                            self._scr_fidx, el)
                try: self.screen_queue.put_nowait(frame)
                except queue.Full: pass

    # ── 카메라 루프 ───────────────────────────────────────────────────────────
    # ── 카메라 백엔드 우선순위 (Windows: DSHOW > MSMF > ANY) ─────────────────
    @staticmethod
    def _open_camera(idx: int, target_fps: float = 30.0):
        """
        백엔드 우선순위로 카메라 열기.

        [문제1] 카메라 FPS 낮음
          근본 원인 A: 내장 카메라(노트북)는 기본 픽셀 포맷이 YUY2 (압축 없음).
                       YUY2는 USB 대역폭 한계로 1280x720에서 최대 5~10fps로 제한됨.
          근본 원인 B: FPS set() 이후 드라이버가 즉시 반영하지 않고,
                       첫 read() 전까지 구 설정(저FPS)을 유지하는 카메라가 있음.
          수정:
            1) MJPG 포맷 먼저 설정 - 하드웨어 JPEG 압축으로 대역폭 대폭 감소
               -> 내장 카메라 30fps 달성 가능.
               MJPG 미지원 카메라는 YUY2 폴백(해상도를 640x480으로 낮춰 FPS 확보).
            2) FPS 설정 후 dummy read() 3회 - 드라이버 파이프라인 워밍업.
            3) CAP_PROP_BUFFERSIZE=1 유지.

        [문제2] 타 PC에서 노트북 내장 카메라 출력 안 됨
          근본 원인: Windows MSMF 백엔드는 일부 노트북 내장 카메라 드라이버와
                     호환성 문제가 있음. EXE 환경에서 MSMF 초기화 실패 시
                     isOpened()=True 를 반환하지만 read()는 항상 실패하는 경우 있음.
          수정:
            1) DSHOW 백엔드 우선 (내장/외장 모두 안정적).
            2) 백엔드별 open 후 즉시 dummy read() 1회로 실제 프레임 수신 가능 여부 검증.
               read() 실패 시 해당 백엔드 포기 -> 다음 백엔드 시도.
            3) MSMF는 DSHOW 실패 시 fallback으로만 사용.
        """
        import platform as _pl
        import time as _t
        _sys = _pl.system()
        if _sys == "Windows":
            backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
        elif _sys == "Linux":
            backends = [cv2.CAP_V4L2, cv2.CAP_ANY]
        else:
            backends = [cv2.CAP_ANY]

        def _silence_open(i, bk):
            """stderr 억제하며 VideoCapture 생성."""
            _orig = sys.stderr
            try:
                sys.stderr = open(os.devnull, 'w')
            except Exception:
                pass
            try:
                return cv2.VideoCapture(i, bk)
            finally:
                try:
                    if sys.stderr is not _orig:
                        sys.stderr.close()
                except Exception:
                    pass
                sys.stderr = _orig

        for backend in backends:
            cap = None
            try:
                cap = _silence_open(idx, backend)
                if not cap.isOpened():
                    cap.release()
                    continue

                # ── [문제2 수정] dummy read로 실제 동작 검증 ─────────────────
                _ok = False
                for _ in range(3):
                    ret, _fr = cap.read()
                    if ret and _fr is not None:
                        _ok = True
                        break
                    _t.sleep(0.05)
                if not _ok:
                    cap.release()
                    continue

                # ── [문제1 수정 A] MJPG 포맷 설정 -> USB 대역폭 절약 -> 고FPS 확보
                _mjpg = cv2.VideoWriter_fourcc(*'MJPG')
                try:
                    cap.set(cv2.CAP_PROP_FOURCC, _mjpg)
                except Exception:
                    pass

                # ── 해상도·FPS 설정 (DSHOW: 해상도 먼저, FPS 나중 순서 중요)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                cap.set(cv2.CAP_PROP_FPS, max(target_fps, 15.0))

                # ── MJPG 미지원 시 YUY2 폴백: 해상도를 낮춰 FPS 확보 ──────────
                _applied_fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
                if _applied_fourcc != _mjpg:
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    cap.set(cv2.CAP_PROP_FPS, max(target_fps, 15.0))

                # ── [문제1 수정 B] FPS 설정 후 워밍업 read -> 드라이버 파이프라인 반영
                for _ in range(3):
                    cap.read()

                # ── 내부 버퍼 최소화 -> 레이턴시 감소 ────────────────────────
                try:
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    pass

                return cap

            except Exception:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass

        return None

    def _camera_loop(self):
        # ★ CAP_DSHOW 우선 — MSMF grabFrame 오류(-1072873821) 방지
        # ★ 카메라 열기 실패 시 3회 재시도 (스캔 직후 장치 점유 해제 대기)
        # ★ [문제1 수정] target_fps를 전달해 카메라가 요청 FPS로 열리도록 함
        cap = None
        for _retry in range(3):
            cap = self._open_camera(self.active_cam_idx,
                                    target_fps=self.actual_camera_fps)
            if cap is not None and cap.isOpened():
                break
            if cap:
                cap.release()
            if _retry < 2:
                import time as _t2; _t2.sleep(0.5)
        if cap is None or not cap.isOpened():
            self.signals.status_message.emit(
                f"❌ Camera {self.active_cam_idx} 열기 실패 — "
                f"카메라 연결 확인 후 재탐색 버튼을 누르세요")
            return
        # ★ [문제1 수정] 실제 적용된 FPS를 재조회 — set() 후 드라이버가
        # 지원 가능한 가장 가까운 FPS로 조정하므로, 그 실제값을 사용.
        rep = cap.get(cv2.CAP_PROP_FPS)
        fps = float(rep) if (rep and 5.0 < rep < 300) else self.actual_camera_fps
        # actual_camera_fps가 스캔값보다 크게 어긋나면 스캔값을 신뢰
        if abs(fps - self.actual_camera_fps) > 10.0:
            fps = self.actual_camera_fps
        self.actual_camera_fps = fps
        # 현재 픽셀 포맷/해상도 확인 후 상태바 표시
        _mjpg_cc = cv2.VideoWriter_fourcc(*'MJPG')
        _cur_fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        _fmt_name = "MJPG" if _cur_fourcc == _mjpg_cc else "YUY2"
        _w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        _h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.signals.status_message.emit(
            f"📷 Camera {self.active_cam_idx} [{_fmt_name}] {_w}x{_h} @ {fps:.0f}fps")

        # ── [FPS 수정] grab/retrieve 분리로 블로킹+sleep 이중 대기 제거 ──────
        # 기존: time.sleep(interval) 후 cap.read() (블로킹) → 실제 주기 = sleep + read 대기
        # 수정: cap.grab()을 sleep 없이 최대한 빠르게 호출 (드라이버 내부 페이스 유지)
        #       retrieve()는 UI 표시 주기(interval)에 맞춰서만 호출 → CPU 낭비 없이 최고 FPS
        # ── [FPS 수정] 실측 FPS로 interval 자동 보정 (드라이버 반환값 불신) ──
        # CAP_PROP_FPS는 드라이버가 설정 요청값을 그대로 반환하는 경우가 있음.
        # 2초마다 실측값(_cam_fps_ts)으로 interval을 재계산해 실제 체감 FPS에 맞춤.
        interval = 1.0 / max(fps, 1.0)
        next_retrieve_t = time.perf_counter()
        _fps_update_t = time.perf_counter()   # 실측 FPS 보정 주기 타이머
        _fail_cnt = 0          # 연속 grab 실패 카운터
        _MAX_FAIL = 30         # 30회 연속 실패 시 재오픈
        while not self._cam_stop.is_set():
            # ── grab: 드라이버 내부 버퍼에서 다음 프레임을 최대한 빠르게 취득 ──
            if not cap.grab():
                _fail_cnt += 1
                if _fail_cnt >= _MAX_FAIL:
                    # ── 카메라 재오픈 시도 (장치 일시 점유 해제 후 복구) ─
                    cap.release()
                    time.sleep(0.5)
                    cap = self._open_camera(self.active_cam_idx,
                                            target_fps=self.actual_camera_fps)
                    if cap is None or not cap.isOpened():
                        self.signals.status_message.emit(
                            f"Camera {self.active_cam_idx} 재연결 실패 — 루프 종료")
                        break
                    _fail_cnt = 0
                    next_retrieve_t = time.perf_counter()
                    # FPS 재조회
                    rep2 = cap.get(cv2.CAP_PROP_FPS)
                    if rep2 and 0 < rep2 < 300:
                        fps = rep2; interval = 1.0 / fps
                else:
                    time.sleep(0.005)   # grab 실패 시 짧게 대기 후 재시도
                continue
            _fail_cnt = 0

            # ── retrieve: UI 표시 주기가 된 경우에만 디코딩 ─────────────────
            now = time.perf_counter()
            if now < next_retrieve_t:
                continue                # 아직 주기 전 — grab만 하고 건너뜀
            next_retrieve_t = now + interval

            ret, frame = cap.retrieve()
            if not ret or frame is None:
                continue

            # ── 실측 FPS로 interval 자동 보정 (2초마다) ──────────────────────
            self._cam_fps_ts.append(time.time())
            if now - _fps_update_t >= 2.0 and len(self._cam_fps_ts) >= 10:
                measured = self.measured_fps(self._cam_fps_ts)
                if 5.0 < measured < 120.0:
                    self.actual_camera_fps = measured
                    interval = 1.0 / measured
                _fps_update_t = now

            # ── ROI 밝기 계산 — blackout ON/OFF 무관하게 항상 수행 ★ ─────────
            # ★ 병목 수정: _roi_rects() 캐싱
            _cam_roi_ver = getattr(self, '_cam_roi_ver', -1)
            _cur_cam_ver = len(self.camera_rois)
            if _cur_cam_ver != _cam_roi_ver:
                self._cam_rects_cache = self._roi_rects("camera")
                self._cam_roi_ver = _cur_cam_ver
            rects = self._cam_rects_cache
            if rects:
                avgs = self.calc_roi_avg(frame, rects)
                self.camera_roi_avg = avgs
                self.camera_overall_avg = (np.mean(avgs, axis=0)
                                           if avgs else np.zeros(3))
                # ── 블랙아웃 감지 — blackout_rec_enabled=True 일 때만 ★ ──────
                if self.blackout_rec_enabled:
                    if self.camera_roi_prev:
                        self._detect_blackout(avgs, self.camera_roi_prev, "camera")
                    self.camera_roi_prev = [a.copy() for a in avgs]
                else:
                    # 비활성 상태에도 prev 갱신 → 활성화 직후 첫 프레임 오탐 방지
                    self.camera_roi_prev = [a.copy() for a in avgs]

            stamped = self._apply_overlays(frame.copy(), "camera")
            if self.camera_rois:
                self._draw_rois_on_frame(
                    stamped, self.camera_rois,
                    brightness_enabled=self.brightness_enabled,
                    ocr_enabled=self.ocr_camera_enabled)
            with self._buf_lock:
                self._cam_buf.append(stamped)  # maxlen이 자동으로 오래된 프레임 제거
            # ── 블랙아웃 post 수집기에 프레임 직접 전달 ──────────────────
            if self._cam_bo_queues:
                with self._bo_queue_lock:
                    qs = list(self._cam_bo_queues)
                for _bq in qs:
                    try: _bq.put_nowait(stamped)
                    except queue.Full: pass
            if self.recording and self.camera_rec_enabled and not self._seg_switching:
                with self._writer_lock: w = self._cam_writer
                if w:
                    el = time.time()-self._seg_start_time
                    self._cam_fidx = self._write_sync(
                        w, self._scale(stamped), fps, self._cam_fidx, el)
            try: self.camera_queue.put_nowait(frame)
            except queue.Full: pass
        cap.release()

    # ── 스레드 제어 ───────────────────────────────────────────────────────────
    def start_screen(self):
        if self._scr_thread and self._scr_thread.is_alive(): return
        # ★ 루프 시작 직전 maxlen을 현재 FPS 기준으로 설정
        with self._buf_lock:
            self._scr_buf = deque(maxlen=self._scr_buf_max)
        self._scr_stop.clear()
        self._scr_thread = threading.Thread(target=self._screen_loop, daemon=True)
        self._scr_thread.start()

    def stop_screen(self): self._scr_stop.set()

    def start_camera(self):
        if self._cam_thread and self._cam_thread.is_alive(): return
        # ★ 루프 시작 직전 maxlen을 현재 FPS 기준으로 설정
        with self._buf_lock:
            self._cam_buf = deque(maxlen=self._cam_buf_max)
        self._cam_stop.clear()
        self._cam_thread = threading.Thread(target=self._camera_loop, daemon=True)
        self._cam_thread.start()

    def stop_camera(self): self._cam_stop.set()

    def restart_screen(self):
        self.stop_screen()
        def _delayed():
            time.sleep(0.15)
            self.start_screen()
        threading.Thread(target=_delayed, daemon=True).start()

    def restart_camera(self):
        self.stop_camera()
        def _wait():
            if self._cam_thread: self._cam_thread.join(timeout=2.0)
            self.start_camera()
        threading.Thread(target=_wait, daemon=True).start()

    # ── 녹화 시작/종료 ────────────────────────────────────────────────────────
    def start_recording(self):
        if self.recording: return
        self.output_dir = self._make_output_dir()
        os.makedirs(self.output_dir, exist_ok=True)
        # tc_folder_idx==2(Rec 폴더) 이면 새로 생성된 output_dir을 태깅 대상으로 설정.
        # 그 외에도 tc_tag_target_dir이 비어있는 경우 output_dir 사용.
        if self.tc_rec_enabled:
            if getattr(self, 'tc_folder_idx', 0) == 2 or not self.tc_tag_target_dir:
                self.tc_tag_target_dir = self.output_dir
        self._scr_fidx = self._cam_fidx = 0
        self._create_segment()
        self.start_time = time.time(); self.recording = True
        self.signals.status_message.emit(f"녹화 시작 → {self.output_dir}")
        self.signals.rec_started.emit(self.output_dir)
        if self.io_channel:
            self.io_channel.emit_event("recording_started", {"path": self.output_dir})
            self.io_channel.set_state("recording", True)
            self.io_channel.set_state("output_dir", self.output_dir)

    def stop_recording(self):
        if not self.recording: return
        self.recording = False
        time.sleep(0.35)
        with self._writer_lock:
            if self._scr_writer: self._scr_writer.release(); self._scr_writer=None
            if self._cam_writer: self._cam_writer.release(); self._cam_writer=None
        if self.tc_rec_enabled and self.tc_verify_result in ("PASS", "FAIL"):
            idx    = getattr(self, 'tc_folder_idx', 0)
            result = self.tc_verify_result
            final_path = self.output_dir

            if idx == 2:
                # ③ 각 기능 폴더(Rec_ 폴더 자체)에 태그
                new_path = self._apply_tc_result_to_folder(result, self.output_dir)
                if new_path:
                    self.output_dir = new_path
                final_path = self.output_dir

            else:
                # ① TC-ID / ② 차종 / ④ 직접입력
                #
                # 근원 수정: 기존 shutil.move(Rec_, dest) 방식의 문제
                #   _build_path("rec") → "날짜/tc_id/Rec_HHMMSS"
                #   start_recording의 makedirs → "tc_id" 폴더 먼저 생성
                #   shutil.move(Rec_, (PASS)tc_id) → Rec_만 이동, tc_id 빈 폴더 잔류
                #
                # 수정: tc_id 폴더(output_dir의 부모) 자체를 rename
                #   이미 (PASS)tc_id 있으면 Rec_만 이동 + 빈 tc_id 삭제
                rec_folder = self.output_dir                  # .../tc_id/Rec_HHMMSS
                tag_parent = os.path.dirname(rec_folder)      # .../날짜/tc_id
                parent_dir = os.path.dirname(tag_parent)      # .../날짜
                base_name  = os.path.basename(tag_parent)     # "tc_id" 또는 "차종"

                # base_name이 이미 태그된 이름이면 부모를 한 단계 더 올림
                # (이전 실행에서 이미 "(PASS) tc_id" 안에 저장된 경우)
                _already = re.match(r'^\((PASS|FAIL)\)\s+', base_name)
                if _already:
                    # output_dir이 이미 태그된 폴더 안에 있음 → 그대로 유지
                    final_path = self.output_dir
                elif not base_name or not parent_dir or not os.path.isdir(tag_parent):
                    # 경로 파싱 실패 → 기존 방식으로 폴백
                    dest_base = self._resolve_tc_save_dir("rec", result)
                    if dest_base:
                        import shutil as _shu_fb
                        try:
                            _shu_fb.move(self.output_dir, dest_base)
                            moved = os.path.join(dest_base, os.path.basename(rec_folder))
                            self.output_dir = moved if os.path.isdir(moved) else dest_base
                        except Exception as _fbe:
                            self.signals.status_message.emit(f"[T/C] 폴더 이동 실패: {_fbe}")
                    final_path = self.output_dir
                else:
                    existing = self._find_existing_tagged_folder(parent_dir, base_name)
                    existing_res = None
                    if existing:
                        _m = re.match(r'^\((PASS|FAIL)\)', os.path.basename(existing))
                        existing_res = _m.group(1) if _m else None

                    import shutil as _shu_rec

                    if existing and existing_res == result:
                        # 동일 결과 태그 폴더 이미 존재
                        # → Rec_ 폴더만 그 안으로 이동 + 빈 tc_id 폴더 삭제
                        dest_rec = os.path.join(existing, os.path.basename(rec_folder))
                        try:
                            _shu_rec.move(rec_folder, dest_rec)
                            self.output_dir = dest_rec
                            # tag_parent(tc_id 폴더) 비어있으면 삭제
                            try:
                                if os.path.isdir(tag_parent) and not os.listdir(tag_parent):
                                    os.rmdir(tag_parent)
                            except Exception:
                                pass
                        except Exception as _e1:
                            self.signals.status_message.emit(f"[T/C] 이동 실패: {_e1}")

                    elif existing and existing_res != result:
                        # 다른 결과 태그 폴더 존재 → 새 태그 폴더 생성 후 Rec_ 이동
                        new_name = f"({result}) {base_name}"
                        new_tagged = os.path.join(parent_dir, new_name)
                        suffix = 1
                        while os.path.exists(new_tagged):
                            new_tagged = os.path.join(parent_dir, f"{new_name} ({suffix})")
                            suffix += 1
                        os.makedirs(new_tagged, exist_ok=True)
                        dest_rec = os.path.join(new_tagged, os.path.basename(rec_folder))
                        try:
                            _shu_rec.move(rec_folder, dest_rec)
                            self.output_dir = dest_rec
                            try:
                                if os.path.isdir(tag_parent) and not os.listdir(tag_parent):
                                    os.rmdir(tag_parent)
                            except Exception:
                                pass
                        except Exception as _e2:
                            self.signals.status_message.emit(f"[T/C] 이동 실패: {_e2}")

                    else:
                        # 태그 폴더 없음 → tag_parent 자체를 "(result) base_name"으로 rename
                        # rename이 원자적이므로 빈 폴더 잔류 없음
                        new_tagged = os.path.join(parent_dir, f"({result}) {base_name}")
                        try:
                            os.rename(tag_parent, new_tagged)
                            self.output_dir = os.path.join(
                                new_tagged, os.path.basename(rec_folder))
                        except Exception as _e3:
                            # rename 실패(파일 잠금 등) → Rec_만 이동 + 빈 폴더 삭제
                            os.makedirs(new_tagged, exist_ok=True)
                            dest_rec = os.path.join(new_tagged, os.path.basename(rec_folder))
                            try:
                                _shu_rec.move(rec_folder, dest_rec)
                                self.output_dir = dest_rec
                                try:
                                    if os.path.isdir(tag_parent) and not os.listdir(tag_parent):
                                        os.rmdir(tag_parent)
                                except Exception:
                                    pass
                            except Exception as _e4:
                                self.signals.status_message.emit(
                                    f"[T/C] rename/이동 실패: {_e4}")

                    final_path = self.output_dir

            self.tc_tag_target_dir = ""
            self.tc_verify_result  = ""
            self.signals.status_message.emit(f"[T/C] 최종 저장 경로 → {final_path}")
        self.start_time = 0.0
        self.signals.status_message.emit("녹화 종료")
        self.signals.rec_stopped.emit()
        if self.io_channel:
            self.io_channel.emit_event("recording_stopped",
                {"final_path": self.output_dir})
            self.io_channel.set_state("recording", False)
            self.io_channel.set_state("output_dir", self.output_dir)

    # ── 수동녹화 ─────────────────────────────────────────────────────────────
    def set_manual_source(self, source: str):
        """
        수동녹화 소스 설정 + UI 체크박스 동기화.
        source: "screen" | "camera" | "both"

        스크립트/커널에서 직접 engine.manual_source = ... 로 대입하면
        패널 체크박스가 따라가지 않음 → 이 메서드 사용.

        사용법:
          engine.set_manual_source("both")    # 화면+카메라 모두 저장
          engine.set_manual_source("camera")  # 카메라만 저장
          engine.set_manual_source("screen")  # 화면만 저장
        """
        if source not in ("screen", "camera", "both"):
            source = "both"
        self.manual_source = source
        self.signals.manual_source_changed.emit(source)
        self.signals.status_message.emit(f"[수동녹화] 소스 설정: {source}")

    def set_manual_time(self, pre_sec: float, post_sec: float):
        """
        수동녹화 시간 설정 + UI 동기화.
        ★ 커널/AI Chat에서 수동녹화 시간 변경 시 이 메서드 사용.
          engine.manual_pre_sec = 20 직접 대입은 UI가 업데이트되지 않음.

        사용법:
          engine.set_manual_time(20, 30)  # 앞 20초, 뒤 30초
        """
        self.manual_pre_sec  = float(pre_sec)
        self.manual_post_sec = float(post_sec)
        # ★ UI 스레드로 시그널 emit → ManualRecPanel 스핀박스 자동 업데이트
        self.signals.manual_settings_changed.emit(
            self.manual_pre_sec, self.manual_post_sec)
        self.signals.status_message.emit(
            f"[수동녹화] 시간 설정: -{pre_sec}초 ~ +{post_sec}초")

    def save_manual_clip(self) -> bool:
        with self._manual_lock:
            if self.manual_state != self.MANUAL_IDLE: return False
            self.manual_state = self.MANUAL_WAITING
            self._manual_trigger = time.time()
        sources = []
        if self.manual_source in ("screen","both"): sources.append("screen")
        if self.manual_source in ("camera","both"): sources.append("camera")
        for src in sources:
            threading.Thread(target=self._do_manual,args=(src,),daemon=True).start()
        return True

    def _do_manual(self, source: str):
        # ★ 수정: 실측 FPS 사용 — 설정값 FPS와 실제 캡처 속도가 다를 때 4배 오차 발생
        #   measured_fps()가 0이면 설정값 fallback
        measured = self.measured_fps(
            self._scr_fps_ts if source == "screen" else self._cam_fps_ts)
        fps = max(measured if measured > 0.5 else
                  (self.actual_screen_fps if source == "screen"
                   else self.actual_camera_fps), 1.0)

        trigger = self._manual_trigger
        n_pre   = int(fps * self.manual_pre_sec)
        n_post  = int(fps * self.manual_post_sec)

        # ── pre 프레임 수집 ───────────────────────────────────────────────────
        with self._buf_lock:
            buf = self._scr_buf if source == "screen" else self._cam_buf
            buf_snap = list(buf)          # 트리거 시점 버퍼 전체 스냅샷
        pre = buf_snap[-n_pre:] if n_pre > 0 else []
        snap_total = len(buf_snap)        # 트리거 시점의 총 프레임 수

        # ── post 프레임 수집 ──────────────────────────────────────────────────
        # 근원 원인: 슬라이딩 deque는 cur_len-snap_len == 0이 되어 elif 분기가
        # 매번 list(buf)[-n_post:]를 반환 → 트리거 이전 프레임 혼입.
        # 수정: 트리거 이후 버퍼에 추가된 누적 프레임 수를 elapsed_frames로 추적.
        #   슬라이딩 deque에서 새 프레임이 들어오면 popleft와 append가 동시에 발생
        #   → buf 전체 길이는 동일, 但 끝에서 elapsed_frames개가 새 프레임.
        post = []
        elapsed_frames = 0
        deadline = time.time() + self.manual_post_sec + 3.0
        frame_interval = 1.0 / fps

        while elapsed_frames < n_post and time.time() < deadline:
            time.sleep(min(frame_interval, 0.05))
            with self._buf_lock:
                buf = self._scr_buf if source == "screen" else self._cam_buf
                cur_buf = list(buf)
            cur_total = len(cur_buf)
            # 트리거 이후 추가된 프레임 수 = 현재 총 프레임 누적 - 트리거 시점 누적
            # 슬라이딩 deque는 전체 크기 고정이므로 절대 카운터 불가 →
            # fps × 경과시간으로 추정 (방어적 계산)
            elapsed_sec = time.time() - trigger
            elapsed_frames = min(int(fps * elapsed_sec), len(cur_buf))
            if elapsed_frames > 0:
                post = cur_buf[-elapsed_frames:]

        post = post[:n_post]   # n_post 초과 방지
        all_f = pre + post[:n_post]
        if not all_f:
            self.signals.status_message.emit(f"[수동녹화] {source}: 저장할 프레임 없음")
            with self._manual_lock: self.manual_state = self.MANUAL_IDLE
            return

        # ★ 근원 수정: 저장 경로를 TC 결과가 확정된 시점에서 미리 결정
        #   기존 방식: _build_path로 저장 → 이후 폴더 rename/이동 시 vp 경로 깨짐
        #   수정 방식: tc_manual_enabled + tc_verify_result 확정 시 _resolve_tc_save_dir로
        #             처음부터 올바른 경로에 직접 저장
        result = self.tc_verify_result
        idx    = getattr(self, 'tc_folder_idx', 0)

        if self.tc_manual_enabled and result in ("PASS", "FAIL"):
            if idx == 2:
                # ③ 각 기능 폴더 자체에 태그
                # _build_path로 경로 계산 후 해당 폴더명에 태그
                base_save = self._build_path("manual")
                tagged = self._apply_tc_result_to_folder(result, base_save)
                save_dir = tagged if tagged else base_save
            else:
                # ① TC-ID / ② 차종 / ④ 직접입력 → 상위 폴더 태그 + 하위 서브폴더
                resolved = self._resolve_tc_save_dir("manual", result)
                save_dir = resolved if resolved else self._build_path("manual")
        else:
            save_dir = self._build_path("manual")

        try:
            os.makedirs(save_dir, exist_ok=True)
        except Exception as ex:
            self.signals.status_message.emit(f"[수동녹화] 폴더 생성 실패: {ex}")
            with self._manual_lock: self.manual_state = self.MANUAL_IDLE
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"manual_{source}_{ts}.mp4"
        final_vp = os.path.join(save_dir, filename)

        # ★ 수정: cv2.VideoWriter 한글/괄호 경로 우회
        #   Windows에서 cv2.VideoWriter는 내부적으로 C fopen()을 사용하므로
        #   한글+괄호 조합 경로에서 isOpened=False로 조용히 실패함 (예외 없음).
        #   → 임시 ASCII 경로에 먼저 저장 후 shutil.move로 최종 경로 이동.
        import tempfile as _tf, shutil as _shu
        tmp_fd, tmp_vp = _tf.mkstemp(suffix=".mp4", prefix="manual_tmp_")
        os.close(tmp_fd)

        try:
            h, w = all_f[0].shape[:2]; bi = len(pre)
            wr = cv2.VideoWriter(tmp_vp, self._fourcc(), fps, (w, h))
            if not wr.isOpened():
                # fourcc 폴백: mp4v → XVID
                wr.release()
                wr = cv2.VideoWriter(
                    tmp_vp, cv2.VideoWriter_fourcc(*'mp4v'), fps, (w, h))
            if not wr.isOpened():
                self.signals.status_message.emit(
                    f"[수동녹화] VideoWriter 열기 실패 — 코덱을 확인하세요")
                try: os.remove(tmp_vp)
                except Exception: pass
                with self._manual_lock: self.manual_state = self.MANUAL_IDLE
                return

            for i, f in enumerate(all_f):
                fc    = self._scale(f.copy())
                t_off = (i - bi) / fps
                abs_t = trigger + t_off
                ts_str = datetime.fromtimestamp(abs_t).strftime("%H:%M:%S.") + \
                         f"{int((abs_t % 1) * 1000):03d}"
                draw_time_bar(fc, ts_str, f"{'PRE' if i < bi else 'POST'}  {t_off:+.2f}s")
                if i == bi:
                    cv2.rectangle(fc, (4, 4),
                                  (fc.shape[1]-4, fc.shape[0]-4), (0, 200, 255), 5)
                wr.write(fc)
            wr.release()

            # 임시 파일 → 최종 경로로 이동
            _shu.move(tmp_vp, final_vp)

        except Exception as ex:
            self.signals.status_message.emit(f"[수동녹화] 저장 실패: {ex}")
            try: os.remove(tmp_vp)
            except Exception: pass
            with self._manual_lock: self.manual_state = self.MANUAL_IDLE
            return

        try:
            rel = os.path.relpath(final_vp, self.base_dir)
        except ValueError:
            rel = final_vp   # 다른 드라이브 예외 방어
        self.signals.status_message.emit(f"[수동녹화] → {rel}")
        self.signals.manual_clip_saved.emit(final_vp)
        if self.io_channel:
            self.io_channel.emit_event("manual_clip_saved", {"path": final_vp})

        # tc_verify_result 사용 후 초기화
        if self.tc_manual_enabled:
            self.tc_verify_result = ""

        time.sleep(max(0.5, self.manual_post_sec / 4))
        with self._manual_lock: self.manual_state = self.MANUAL_IDLE

    # ── 캡처 ─────────────────────────────────────────────────────────────────
    def capture_frame(self, source: str, tc_tag: str = "") -> str:
        # ★ 근원 수정: 저장 경로를 TC 결과 확정 시점에서 미리 결정
        #   기존: 기본경로에 저장 → 이후 rename → 경로 깨짐
        #   수정: tc_capture_enabled + tc_verify_result 확정이면 처음부터 올바른 경로에 저장
        result = self.tc_verify_result
        idx    = getattr(self, 'tc_folder_idx', 0)

        if self.tc_capture_enabled and result in ("PASS", "FAIL"):
            if idx == 2:
                base_cap = self._build_path("capture")
                tagged = self._apply_tc_result_to_folder(result, base_cap)
                capture_dir = tagged if tagged else base_cap
            else:
                resolved = self._resolve_tc_save_dir("capture", result)
                capture_dir = resolved if resolved else self._build_path("capture")
        else:
            capture_dir = self._build_path("capture")

        os.makedirs(capture_dir, exist_ok=True)
        ts     = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        prefix = f"({tc_tag})_" if tc_tag else ""
        path   = os.path.join(capture_dir, f"{prefix}capture_{source}_{ts}.png")
        with self._buf_lock:
            buf   = self._scr_buf if source == "screen" else self._cam_buf
            frame = buf[-1].copy() if buf else None
        if frame is None:
            self.signals.status_message.emit(
                f"[캡처] {source}: 버퍼 없음 — 스트림 미시작 또는 카메라 미연결")
            return ""
        try:
            # Windows 한글 경로 대응 — imencode+open 방식
            ext = os.path.splitext(path)[1].lower() or ".png"
            ok, buf_enc = cv2.imencode(ext, frame)
            if not ok:
                self.signals.status_message.emit(f"[캡처] 인코딩 실패 — 경로: {path}")
                return ""
            with open(path, 'wb') as f:
                f.write(buf_enc.tobytes())

            try:
                rel_path = os.path.relpath(path, self.base_dir)
            except ValueError:
                rel_path = path   # 다른 드라이브 예외 방어
            self.signals.status_message.emit(f"[캡처] 저장 완료 → {rel_path}")
            self.signals.capture_saved.emit(source, path)
            if self.io_channel:
                self.io_channel.emit_event("capture_saved",
                    {"source": source, "path": path})
            # ★ 수정: tc_verify_result 사용 후 초기화
            if self.tc_capture_enabled:
                self.tc_verify_result = ""
            return path
        except Exception as ex:
            self.signals.status_message.emit(f"[캡처] 저장 실패: {ex}  경로: {path}")
            return ""

    # ── 오토클릭 ─────────────────────────────────────────────────────────────
    # ── 오토클릭 실행 엔진 ─────────────────────────────────────────────────────
    # ctypes/win32api 캐싱 — 클래스 변수로 선언, __init_subclass__ 대신 즉시 초기화
    _SEND_INPUT_READY = False
    _WIN32API_READY   = False
    _ct               = None
    _user32           = None
    _Input            = None
    _win32api_cached  = None

    @staticmethod
    def _init_click_backend():
        """최초 1회 클릭 백엔드 초기화 — 이후 캐싱된 객체 재사용."""
        if CoreEngine._SEND_INPUT_READY or CoreEngine._WIN32API_READY:
            return  # 이미 초기화됨
        # win32api 시도
        try:
            import win32api as _wa
            CoreEngine._win32api_cached = _wa
            CoreEngine._WIN32API_READY  = True
        except Exception:
            pass
        # ctypes SendInput 구조체 — 최초 1회만 정의
        try:
            import ctypes as _ct, ctypes.wintypes as _cwt
            class _MI(_ct.Structure):
                _fields_ = [("dx", _cwt.LONG), ("dy", _cwt.LONG),
                            ("mouseData", _cwt.DWORD), ("dwFlags", _cwt.DWORD),
                            ("time", _cwt.DWORD),
                            ("dwExtraInfo", _ct.POINTER(_ct.c_ulong))]
            class _IU(_ct.Union):
                _fields_ = [("mi", _MI)]
            class _IN(_ct.Structure):
                _fields_ = [("type", _cwt.DWORD), ("_u", _IU)]
            CoreEngine._ct               = _ct
            CoreEngine._Input            = _IN
            CoreEngine._user32           = _ct.windll.user32
            CoreEngine._SEND_INPUT_READY = True
        except Exception:
            pass

    @staticmethod
    def _do_mouse_click(btn: str = "left", x: int = None, y: int = None):
        """
        마우스 클릭 실행 — 3단계 폴백 (최초 1회 초기화 후 캐싱 재사용):
          1순위: win32api  2순위: ctypes SendInput  3순위: pynput
        """
        # 최초 1회 백엔드 초기화
        CoreEngine._init_click_backend()

        # 좌표 이동
        if x is not None and y is not None:
            try:
                if CoreEngine._ct:
                    CoreEngine._ct.windll.user32.SetCursorPos(int(x), int(y))
                elif PYNPUT_AVAILABLE:
                    pynput_mouse.Controller().position = (x, y)
            except Exception:
                pass

        # 버튼 플래그 매핑
        if btn == "right":
            dw_down, dw_up = 0x0008, 0x0010
        elif btn == "middle":
            dw_down, dw_up = 0x0020, 0x0040
        else:
            dw_down, dw_up = 0x0002, 0x0004

        # 1순위: win32api
        if CoreEngine._WIN32API_READY:
            try:
                CoreEngine._win32api_cached.mouse_event(dw_down, 0, 0, 0, 0)
                CoreEngine._win32api_cached.mouse_event(dw_up,   0, 0, 0, 0)
                return True
            except Exception:
                pass

        # 2순위: ctypes SendInput
        if CoreEngine._SEND_INPUT_READY:
            try:
                def _send(flags):
                    inp = CoreEngine._Input(type=0)
                    inp._u.mi.dwFlags = flags
                    CoreEngine._user32.SendInput(
                        1, CoreEngine._ct.byref(inp),
                        CoreEngine._ct.sizeof(CoreEngine._Input))
                _send(dw_down); _send(dw_up)
                return True
            except Exception:
                pass

        # 3순위: pynput
        if PYNPUT_AVAILABLE:
            try:
                mc = pynput_mouse.Controller()
                btn_obj = {"left": pynput_mouse.Button.left,
                           "right": pynput_mouse.Button.right,
                           "middle": pynput_mouse.Button.middle,
                           }.get(btn, pynput_mouse.Button.left)
                mc.click(btn_obj)
                return True
            except Exception:
                pass

        return False

    def _ac_loop(self):
        """오토클릭 루프 — 간격마다 _do_mouse_click() 호출."""
        # 첫 클릭 전 상태 로그
        x = getattr(self, 'ac_pos_x', None)
        y = getattr(self, 'ac_pos_y', None)
        btn = getattr(self, 'ac_btn', 'left')
        double = getattr(self, 'ac_double', False)

        ok_test = self._do_mouse_click(btn, x, y)
        if not ok_test:
            self.signals.status_message.emit(
                "⚠️ 오토클릭: 클릭 실패 — pywin32/pynput 설치 확인")
        # 첫 클릭은 위에서 이미 수행됨
        self.ac_count += 1
        self.signals.ac_count_changed.emit(self.ac_count)

        try:
            import ctypes as _ct_ac
            _ct_ac.windll.kernel32.SetThreadPriority(
                _ct_ac.windll.kernel32.GetCurrentThread(), -2)
        except Exception:
            pass
        _emit_every = max(1, int(1.0 / max(self.ac_interval, 0.05)))
        _emit_cnt   = 0
        while self.ac_enabled and not self._ac_stop.is_set():
            stopped = self._ac_stop.wait(timeout=self.ac_interval)
            if stopped or not self.ac_enabled:
                break
            # 더블클릭 시 연속 2회
            clicks = 2 if double else 1
            for _ in range(clicks):
                self._do_mouse_click(btn, x, y)
            self.ac_count += 1
            _emit_cnt  += 1
            # ★ 고속 클릭 시 Qt 이벤트 큐 폭격 방지 — _emit_every 클릭마다 1회 emit
            if _emit_cnt >= _emit_every:
                self.signals.ac_count_changed.emit(self.ac_count)
                _emit_cnt = 0

    def start_ac(self):
        if self.ac_enabled: return
        self.ac_enabled=True; self._ac_stop.clear()
        self._ac_thread = threading.Thread(target=self._ac_loop, daemon=True)
        self._ac_thread.start()
        self.signals.ac_state_changed.emit(True)   # ★ 외부 호출 시 UI 동기화

    def stop_ac(self):
        self.ac_enabled=False; self._ac_stop.set()
        self.signals.ac_state_changed.emit(False)  # ★ 외부 호출 시 UI 동기화

    def reset_ac_count(self):
        self.ac_count=0; self.signals.ac_count_changed.emit(0)

    # ── 매크로 (기록/실행) ────────────────────────────────────────────────────
    def macro_start_recording(self):
        if not PYNPUT_AVAILABLE or self.macro_recording: return
        self.macro_recording = True
        _q: queue.Queue = queue.Queue()

        def _delayed():
            time.sleep(0.5)
            if not self.macro_recording: return
            active_ts = time.time()
            _press_info = {}
            _DRAG_THRESH = 5

            def _btn_str(btn) -> str:
                if btn == pynput_mouse.Button.left:   return 'left'
                if btn == pynput_mouse.Button.right:  return 'right'
                if btn == pynput_mouse.Button.middle: return 'middle'
                return str(btn)

            def on_click(x, y, button, pressed):
                if not self.macro_recording: return
                t = time.time()
                if t < active_ts: return
                if pressed:
                    _press_info[button] = (int(x), int(y), t)
                else:
                    info = _press_info.pop(button, None)
                    if info is None: return
                    px, py, pt = info
                    dx, dy = abs(int(x)-px), abs(int(y)-py)
                    if dx > _DRAG_THRESH or dy > _DRAG_THRESH:
                        try: _q.put_nowait(('drag', px, py, int(x), int(y), _btn_str(button), t))
                        except: pass
                    else:
                        try: _q.put_nowait(('click', px, py, _btn_str(button), False, t))
                        except: pass

            _last_click = {}
            _DBL_MAX    = 0.4

            def on_click_dbl(x, y, button, pressed):
                if not pressed: return on_click(x, y, button, pressed)
                t = time.time()
                if t < active_ts: return
                key = (_btn_str(button),)
                last = _last_click.get(key, 0.0)
                is_dbl = (t - last) < _DBL_MAX
                _last_click[key] = t
                if is_dbl:
                    try: _q.put_nowait(('double_flag', t))
                    except: pass
                on_click(x, y, button, pressed)

            _MODIFIER_KEYS = {
                pynput_keyboard.Key.shift, pynput_keyboard.Key.shift_l,
                pynput_keyboard.Key.shift_r, pynput_keyboard.Key.ctrl,
                pynput_keyboard.Key.ctrl_l, pynput_keyboard.Key.ctrl_r,
                pynput_keyboard.Key.alt, pynput_keyboard.Key.alt_l,
                pynput_keyboard.Key.alt_r, pynput_keyboard.Key.cmd,
                pynput_keyboard.Key.caps_lock,
            }

            def on_key_press(key):
                if not self.macro_recording: return
                t = time.time()
                if t < active_ts: return
                if key in _MODIFIER_KEYS: return
                try:
                    ks = key.char if hasattr(key,'char') and key.char else str(key)
                    _q.put_nowait(('key', ks, t))
                except: pass

            def _emit_loop():
                last_ts = active_ts
                recent_steps: list = []

                while True:
                    try: item = _q.get(timeout=0.1)
                    except:
                        if not self.macro_recording and _q.empty(): break
                        continue
                    if item is None: break

                    ev_type = item[0]

                    if ev_type == 'double_flag':
                        for st in reversed(recent_steps):
                            if st.kind == 'click':
                                st.double = True; break
                        continue

                    if ev_type == 'click':
                        _, px, py, btn, dbl, t = item
                        delay = round(t - last_ts, 3); last_ts = t
                        step  = MacroStep('click', delay,
                                          x=px, y=py, button=btn, double=dbl)
                    elif ev_type == 'drag':
                        _, px, py, rx, ry, btn, t = item
                        delay = round(t - last_ts, 3); last_ts = t
                        step  = MacroStep('drag', delay,
                                          x=px, y=py, x2=rx, y2=ry, button=btn)
                    elif ev_type == 'key':
                        _, ks, t = item
                        delay = round(t - last_ts, 3); last_ts = t
                        step  = MacroStep('key', delay, key_str=ks)
                    else:
                        continue

                    self.macro_steps.append(step)
                    recent_steps.append(step)
                    if len(recent_steps) > 8: recent_steps.pop(0)
                    flt = getattr(self, '_rec_filter',
                                  {'click': True, 'drag': True, 'key': True})
                    if not flt.get(step.kind, True):
                        self.macro_steps.pop()
                        continue
                    self.signals.macro_step_rec.emit(step)

            threading.Thread(target=_emit_loop, daemon=True,
                             name="MacroEmitLoop").start()

            self._mac_mouse_listener = pynput_mouse.Listener(on_click=on_click_dbl)
            self._mac_key_listener   = pynput_keyboard.Listener(on_press=on_key_press)
            self._mac_mouse_listener.start()
            self._mac_key_listener.start()
            self._mac_listener = self._mac_mouse_listener
            self.signals.status_message.emit(
                "매크로 기록 중 — 마우스 클릭/드래그 및 키보드 입력이 기록됩니다")

        threading.Thread(target=_delayed, daemon=True,
                         name="MacroRecordDelayed").start()

    def macro_stop_recording(self):
        # ★ 수정: 기록 중지 버튼 클릭을 스텝에서 제거
        # 중지 버튼 클릭 시각을 기록해두고, _emit_loop 종료 후
        # 해당 시각 이후에 추가된 스텝(= 중지 클릭)을 제거
        self._macro_stop_ts = time.time()
        self.macro_recording = False

        def _stop():
            # listener 종료 전 짧게 대기 → 큐에 남은 이벤트 소진
            time.sleep(0.25)
            for attr in ('_mac_mouse_listener', '_mac_key_listener', '_mac_listener'):
                lst = getattr(self, attr, None)
                if lst:
                    try: lst.stop()
                    except: pass
                    setattr(self, attr, None)
            # ★ listener 종료 후: _macro_stop_ts 이후에 추가된 마지막 스텝 제거
            # (= 기록 중지 버튼 클릭 이벤트)
            # 단순 pop() 대신 타임스탬프 기반으로 처리하여 정상 스텝 삭제 방지
            # _emit_loop이 이미 종료된 상태이므로 thread-safe
            stop_ts = getattr(self, '_macro_stop_ts', None)
            if stop_ts and self.macro_steps:
                # 마지막 스텝이 중지 버튼 클릭일 가능성: 항상 마지막 1개 제거
                # (중지는 반드시 마지막 동작이므로)
                self.macro_steps.pop()
                # 혹시 double_click 처리로 2개 추가됐으면 추가 제거
                # (일반적으로는 1개만 제거하면 됨)
            self._macro_stop_ts = None
        threading.Thread(target=_stop, daemon=True, name="MacroStopClean").start()

    def macro_start_run(self, repeat=None, gap=None, slot: int = None,
                        wait: bool = False):
        """
        매크로 실행.
        ★ slot  : 1-based. 슬롯 1 = slot=1, 슬롯 2 = slot=2.
                  None이면 현재 macro_steps 그대로 사용.
        ★ wait  : True이면 실행 완료까지 블로킹 (커널 스크립트 순차 실행용).
                  kernel.is_stopped() 감지 포함.

        커널/AI Chat 사용법:
            # 단순 실행 (비동기)
            engine.macro_start_run(slot=1)

            # 슬롯 1 완료 후 슬롯 2 실행 (순차, wait 사용)
            engine.macro_start_run(slot=1, wait=True)
            engine.macro_start_run(slot=2, wait=True)

            # wait 없이 순차 실행하는 패턴 (while 루프)
            engine.macro_start_run(slot=1)
            while not kernel.is_stopped():
                if not engine.macro_running: break
                kernel.wait(0.2)
            engine.macro_start_run(slot=2)
        """
        if slot is not None:
            # ★ 1-based → 0-based 변환 (내부 처리)
            if not self.macro_load_slot(slot - 1):
                return
        if not PYNPUT_AVAILABLE:
            self.signals.status_message.emit("[Macro] ❌ pynput 미설치 — pip install pynput")
            return
        if self.macro_running:
            self.signals.status_message.emit("[Macro] ⚠ 이미 실행 중")
            return
        if not self.macro_steps:
            self.signals.status_message.emit("[Macro] ❌ 실행할 스텝 없음 — 슬롯을 먼저 로드하세요")
            return
        if repeat is not None: self.macro_repeat = repeat
        if gap    is not None: self.macro_gap    = gap
        self.macro_running = True; self._mac_stop.clear()
        self._mac_thread = threading.Thread(target=self._mac_loop, daemon=True)
        self._mac_thread.start()

        # ★ wait=True: 실행 완료까지 블로킹 (커널 스크립트 순차 실행용)
        if wait:
            while self.macro_running and not self._mac_stop.is_set():
                time.sleep(0.05)

    def _mac_loop(self):
        mc  = pynput_mouse.Controller()
        kc  = pynput_keyboard.Controller()
        rep = 0; infinite = (self.macro_repeat == 0)

        def _wait(secs):
            waited = 0.0
            while waited < secs and not self._mac_stop.is_set():
                chunk = min(0.05, secs - waited)
                time.sleep(chunk); waited += chunk

        def _btn(btn_str):
            m = {'left':   pynput_mouse.Button.left,
                 'right':  pynput_mouse.Button.right,
                 'middle': pynput_mouse.Button.middle}
            return m.get(btn_str, pynput_mouse.Button.left)

        def _play_step(step: MacroStep):
            _wait(step.delay)
            if self._mac_stop.is_set(): return
            if step.kind == 'click':
                mc.position = (step.x, step.y)
                btn = _btn(step.button)
                if step.double: mc.click(btn, 2)
                else: mc.click(btn)
            elif step.kind == 'drag':
                mc.position = (step.x, step.y)
                mc.press(_btn(step.button))
                dx = (step.x2 - step.x) / 5
                dy = (step.y2 - step.y) / 5
                for i in range(1, 6):
                    if self._mac_stop.is_set(): break
                    mc.position = (int(step.x + dx*i), int(step.y + dy*i))
                    time.sleep(0.02)
                mc.position = (step.x2, step.y2)
                mc.release(_btn(step.button))
            elif step.kind == 'key':
                ks = step.key_str
                try:
                    if len(ks) == 1:
                        kc.press(ks); kc.release(ks)
                    else:
                        key_name = ks.replace('Key.','')
                        special  = getattr(pynput_keyboard.Key, key_name, None)
                        if special: kc.press(special); kc.release(special)
                        else: kc.type(ks)
                except Exception:
                    try: kc.type(ks)
                    except: pass
            # ★ 매 스텝마다 emit하면 이벤트 큐 폭발 → 50ms 이상 간격일 때만 emit
            _now = time.time()
            if not hasattr(self, '_mac_last_emit') or _now - self._mac_last_emit >= 0.05:
                self.signals.status_message.emit(f"[Macro] {step.summary()}")
                self._mac_last_emit = _now

        while not self._mac_stop.is_set():
            for step in list(self.macro_steps):
                if self._mac_stop.is_set(): break
                _play_step(step)
            rep += 1
            if not infinite and rep >= self.macro_repeat: break
            _wait(self.macro_gap)

        self.macro_running = False
        self.signals.status_message.emit("[Macro] 실행 완료")

    def macro_stop_run(self): self._mac_stop.set(); self.macro_running=False
    def macro_clear(self): self.macro_steps.clear()

    def macro_load_slot(self, slot_idx: int) -> bool:
        """
        지정한 슬롯의 스텝을 macro_steps에 로드.
        ★ 내부 전용 (0-based). 커널 스크립트에서 직접 호출 비추천.
        커널/AI Chat에서는 engine.macro_start_run(slot=1)처럼 1-based를 사용하세요.
        반환값: 로드 성공 여부.
        """
        # ── _macro_slots가 비어있으면 패널 참조로 직접 동기화 ─────────
        if not self._macro_slots:
            # _macro_panel_ref를 우선 시도 (MainWindow에서 주입)
            panel = getattr(self, '_macro_panel_ref', None)
            if panel and hasattr(panel, '_sync_engine_slots'):
                panel._sync_engine_slots()
            else:
                # fallback: gc 탐색
                try:
                    import gc as _gc
                    for obj in _gc.get_objects():
                        if type(obj).__name__ == 'MacroPanel' and hasattr(obj, '_sync_engine_slots'):
                            obj._sync_engine_slots()
                            break
                except Exception:
                    pass

        slots = self._macro_slots
        if not slots:
            self.signals.status_message.emit(
                "[Macro] ❌ 슬롯 데이터 없음 — 매크로 패널에서 슬롯을 저장 후 실행하세요")
            return False
        if not (0 <= slot_idx < len(slots)):
            self.signals.status_message.emit(
                f"[Macro] ❌ 슬롯 {slot_idx+1}번 없음 "
                f"(사용 가능: 1~{len(slots)}번)")
            return False
        self.macro_steps.clear()
        self.macro_steps.extend(slots[slot_idx]['steps'])
        self.signals.status_message.emit(
            f"[Macro] ✅ 슬롯 {slot_idx+1} '{slots[slot_idx]['title']}' 로드 "
            f"({len(self.macro_steps)} 스텝)")
        return True

    # ── 예약 ─────────────────────────────────────────────────────────────────
    def schedule_tick(self) -> list:
        now=datetime.now(); actions=[]
        for s in list(self.schedules):
            if s.done: continue

            # ── 시작 조건 ─────────────────────────────────────────────────
            if s.start_dt and not s.started:
                if -2 <= (s.start_dt-now).total_seconds() <= 1:
                    s.started=True
                    if 'rec_start' in s.actions and not self.recording:
                        actions.append(('start',s))
                    if 'macro_run' in s.actions:
                        actions.append(('macro_run',s))
                    # 전원 ON
                    pwr_on = getattr(s, 'pwr_on_ch', None)
                    if pwr_on and hasattr(self, '_power_ctrl') and self._power_ctrl:
                        try:
                            if pwr_on != 'all':
                                self._power_ctrl.channel_on(pwr_on)
                        except Exception:
                            pass

            # ── 종료 조건 ─────────────────────────────────────────────────
            # stop_only(start_dt=None) 엔트리는 start_dt=now로 설정돼 즉시 started=True
            if s.stop_dt and s.started and not s.stopped:
                if -2 <= (s.stop_dt-now).total_seconds() <= 1:
                    s.stopped=s.done=True
                    if 'rec_stop' in s.actions and self.recording:
                        actions.append(('stop',s))
                    # 전원 OFF
                    pwr_off = getattr(s, 'pwr_off_ch', None)
                    if pwr_off and hasattr(self, '_power_ctrl') and self._power_ctrl:
                        try:
                            if pwr_off == 'all':
                                self._power_ctrl.send(self._power_ctrl.ALL_OFF_CMD)
                            else:
                                self._power_ctrl.channel_off(pwr_off)
                        except Exception:
                            pass

            # ── 완료 처리 ─────────────────────────────────────────────────
            # stop_dt 없고 시작됐으면 즉시 완료 (rec_start 단독 예약)
            if s.started and not s.stop_dt and not s.done:
                s.done=True
        return actions

    # ── I/O 채널 폴링 루프 (AI/외부제어) ─────────────────────────────────────
    def _io_poll_loop(self):
        """
        외부 프로세스(LLM/MCP 등)가 io_channel.db의 commands 테이블에
        삽입한 명령을 5초 간격으로 폴링하여 실행.
        """
        while not self._io_stop.is_set():
            try:
                cmds = self.io_channel.poll_commands()
                for cmd_info in cmds:
                    self._handle_io_command(cmd_info)
            except Exception as e:
                pass  # 폴링 오류는 무시하고 계속
            self._io_stop.wait(5.0)  # 5초 대기

    def _handle_io_command(self, cmd_info: dict):
        """외부 명령 처리 및 결과 기록."""
        cmd_id = cmd_info["id"]
        cmd    = cmd_info["cmd"]
        args   = cmd_info.get("args", {})
        result = {}
        ok = True
        try:
            if cmd == "start_recording":
                self.start_recording()
                result = {"output_dir": self.output_dir}
            elif cmd == "stop_recording":
                self.stop_recording()
            elif cmd == "save_manual_clip":
                saved = self.save_manual_clip()
                result = {"queued": saved}
            elif cmd == "capture_frame":
                source = args.get("source", "screen")
                path   = self.capture_frame(source)
                result = {"path": path}
            elif cmd == "start_ac":
                self.start_ac()
            elif cmd == "stop_ac":
                self.stop_ac()
            elif cmd == "reset_ac":
                self.reset_ac_count()
            elif cmd == "macro_run":
                self.macro_start_run(
                    repeat=args.get("repeat"),
                    gap=args.get("gap"),
                    slot=args.get("slot"))   # ★ slot 파라미터 추가
            elif cmd == "macro_stop":
                self.macro_stop_run()
            elif cmd == "set_roi":
                roi = RoiItem(
                    x=args.get("x",0), y=args.get("y",0),
                    w=args.get("w",100), h=args.get("h",100),
                    name=args.get("name",""), description=args.get("description",""),
                    source=args.get("source","screen"))
                if roi.source == "screen":
                    self.screen_rois.append(roi)
                else:
                    self.camera_rois.append(roi)
                self.signals.roi_list_changed.emit()
                result = {"roi_count": len(self.screen_rois)+len(self.camera_rois)}
            elif cmd == "clear_roi":
                source = args.get("source","all")
                if source in ("screen","all"): self.screen_rois.clear()
                if source in ("camera","all"): self.camera_rois.clear()
                self.signals.roi_list_changed.emit()
            elif cmd == "get_state":
                result = {
                    "recording":      self.recording,
                    "output_dir":     self.output_dir,
                    "ac_enabled":     self.ac_enabled,
                    "ac_count":       self.ac_count,
                    "macro_running":  self.macro_running,
                    "screen_bo":      self.screen_bo_count,
                    "camera_bo":      self.camera_bo_count,
                    "screen_fps":     round(self.measured_fps(self._scr_fps_ts),1),
                    "camera_fps":     round(self.measured_fps(self._cam_fps_ts),1),
                }
                # 최신 상태도 state 테이블에 반영
                self.io_channel.set_state("full_state", result)
            else:
                ok = False
                result = {"error": f"unknown command: {cmd}"}
        except Exception as ex:
            ok = False
            result = {"error": str(ex)}
        self.io_channel.complete_command(cmd_id, result, ok)

    # ── 전체 시작/종료 ────────────────────────────────────────────────────────
    def start(self):
        threading.Thread(target=self.scan_monitors, daemon=True).start()
        threading.Thread(target=self.scan_cameras,  daemon=True).start()

        # 10초 후 자동 시작 (과부화 방지)
        def _delayed_start():
            time.sleep(10.0)
            if not self._scr_stop.is_set():
                self.start_screen()
            if not self._cam_stop.is_set():
                self.start_camera()
        threading.Thread(target=_delayed_start, daemon=True,
                         name="EngineDelayedStart").start()

        # I/O 채널 폴링 시작 (선택적)
        if self.io_channel:
            self._io_stop.clear()
            self._io_thread = threading.Thread(
                target=self._io_poll_loop, daemon=True, name="IOPollLoop")
            self._io_thread.start()
            self.io_channel.set_state("recording", False)
            self.io_channel.set_state("supported_commands", self.SUPPORTED_COMMANDS)

    def stop(self):
        if self.recording: self.stop_recording()
        self.stop_ac()
        self.macro_stop_run()
        self.macro_stop_recording()
        self._scr_stop.set()
        self._cam_stop.set()
        self._io_stop.set()
        if self.io_channel:
            try: self.io_channel.cleanup()
            except: pass

    # ── 미리보기 스탬프 ───────────────────────────────────────────────────────
    @staticmethod
    def stamp_preview(frame: np.ndarray, engine: "CoreEngine",
                      source: str) -> np.ndarray:
        out = frame.copy()
        if engine.recording and engine.start_time:
            now = datetime.now()
            ns = now.strftime("%Y-%m-%d  %H:%M:%S.")+f"{now.microsecond//1000:03d}"
            e  = time.time()-engine.start_time
            draw_time_bar(out, ns, f"REC  {fmt_hms(e)}.{int((e%1)*1000):03d}")
        h,w = out.shape[:2]
        for cfg in engine.memo_overlays:
            if not cfg.enabled: continue
            if cfg.target!="both" and cfg.target!=source: continue
            if cfg.tab_idx >= len(engine.memo_texts): continue
            text = engine.memo_texts[cfg.tab_idx].strip()
            if text:
                draw_memo_overlay(out, text.splitlines(), cfg.position,
                                  w, h, cfg.overlay_font_size)
        return out
