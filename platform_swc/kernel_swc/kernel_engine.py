# -*- coding: utf-8 -*-
"""
kernel_swc/kernel_engine.py
Python exec 기반 커널 엔진.

모드 A: 내장 exec (동일 프로세스)
모드 B: 외부 Python 서브프로세스 + API 브릿지 소켓

의존: common/*, engine_swc/core_engine (DI 주입), kernel_swc/power_controller
"""
import os, sys, json, time, threading, socket, queue
from datetime import datetime
from typing import List, Optional, TYPE_CHECKING

from common.constants import BASE_DIR
from common.signals import Signals
from kernel_swc.kernel_result_log import KernelResultLog
from kernel_swc.kernel_script import KernelScript
from kernel_swc.power_controller import PowerController

if TYPE_CHECKING:
    from engine_swc.core_engine import CoreEngine

class KernelEngine:
    """
    Python 인터프리터 기반 커널 v2.9.2.

    ── 두 가지 실행 모드 ──
    [모드 A] 내장 exec — 이 프로세스 안에서 exec()로 스크립트 실행
             engine/kernel 객체가 직접 전달되어 녹화·ROI 등 모든 기능 접근 가능.

    [모드 B] 외부 인터프리터 — 사용자가 지정한 python.exe로 .py를 서브프로세스 실행
             API 브릿지 소켓 서버를 통해 engine 기능을 RPC 방식으로 노출.
             engine.start_recording() → 소켓 JSON → CoreEngine 실행

    외부 모드 사용 흐름:
      1. [인터프리터 경로 설정] 으로 python.exe 지정
      2. 스크립트에서 import recorder_bridge 후 engine 사용
      3. [▶ 외부 인터프리터로 실행] 버튼 클릭
    """
    # API 브릿지 소켓 포트
    BRIDGE_PORT    = 17291   # TCP JSON-RPC 브릿지
    CAN_WS_PORT    = 17292   # WebSocket CAN 신호 스트림

    def __init__(self, engine: 'CoreEngine', signals: 'Signals'):
        self._engine  = engine
        self._signals = signals
        self._stop_ev = threading.Event()
        self._thread: threading.Thread = None

        self.scripts: List[KernelScript] = []
        self.log_callback = None
        self.running   = False
        self.cur_title = ""

        # ── 외부 인터프리터 설정 ─────────────────────────────────────────
        self.python_exe: str = sys.executable   # 기본값: 현재 프로세스
        self.watch_dir: str  = ""               # .py 파일 감시 폴더

        # ── API 브릿지 서버 ──────────────────────────────────────────────
        self._bridge_server: Optional[threading.Thread] = None
        self._bridge_stop   = threading.Event()
        self._bridge_sock   = None

        # ── 전원 제어 ────────────────────────────────────────────────────
        # PowerController 인스턴스 — PowerControlPanel과 공유됨
        # MainWindow._make_panel("power")에서 주입됨
        self.power: 'PowerController' = None   # type: ignore

        # ── 타이머 프록시 (지연 바인딩) ──────────────────────────────────
        # timer.start()/stop()/lap()/reset() 커널 API
        # TimerPanel이 열린 뒤 engine._timer_panel_ref에 자동 저장됨
        # 커널 스크립트에서: timer.start(), elapsed = timer.lap() 형태로 사용

        # ── 결과 로그 ─────────────────────────────────────────────────────
        # 커널 실행 결과(PASS/FAIL/OK/SKIP)를 스크립트·시도별로 누적.
        # _run_all() 에서 자동 기록, KernelPanel 버튼으로 메모장 내보내기.
        self.result_log: 'KernelResultLog' = KernelResultLog()

        # ── 커널 전용 TC 결과 필드 ────────────────────────────────────────
        # kernel.set_tc_result() 가 여기에 기록.
        # engine.tc_verify_result 는 수동녹화/캡처 완료 후 초기화되므로
        # _run_all()의 최종 결과 판정에 사용하기 부적합 → 이 필드로 분리.
        # ★ _kernel_tc_result: list로 변경 — 여러 스텝 결과 누적
        # FAIL이 하나라도 있으면 최종 FAIL, 모두 PASS면 PASS
        # 예: ["FAIL_STEP5-B", "FAIL_STEP6"] → 파일명: (FAIL_STEP5-B_STEP6)
        self._kernel_tc_result: list = []
        self._panel_ref       = None  # KernelPanel 역참조 (_auto_export 로그 접근용)

    # ── 사용자 API 헬퍼 ──────────────────────────────────────────────────────
    @property
    def timer(self):
        """커널 API: timer.start() / timer.stop() / timer.lap() / timer.reset()
        커널 스크립트에서 타이머를 직접 제어:
          timer.start()                # 시작
          timer.stop()                 # 종료
          elapsed = timer.lap()        # 랩 기록 + 경과시간(초) 반환
          timer.reset()                # 리셋
          secs = timer.elapsed         # 현재 경과시간 읽기
          is_running = timer.running   # 실행 중 여부
        """
        ref = getattr(self._engine, '_timer_panel_ref', None)
        if ref is None:
            raise RuntimeError("타이머 패널 미초기화 — 조건부 타이머 패널을 열어주세요")
        return ref

    def wait(self, seconds: float):
        """슬립 + 중단 감지 (0.05초 단위)."""
        waited = 0.0
        while waited < seconds and not self._stop_ev.is_set():
            time.sleep(min(0.05, seconds - waited))
            waited += 0.05

    def is_stopped(self) -> bool:
        return self._stop_ev.is_set()

    def wait_manual_clip(self, timeout: float = 120.0):
        """
        수동녹화 완료까지 대기 (커널 스크립트용).
        save_manual_clip() 호출 후 이 함수로 완료를 대기해야 함.

        ★ 사용법 (올바른 수동녹화 → 녹화 종료 패턴):
            engine.set_manual_time(20, 30)   # pre=20초, post=30초 (UI도 자동 업데이트)
            engine.save_manual_clip()
            kernel.wait_manual_clip(timeout=70)  # post(30)+여유시간 이상으로 설정
            engine.stop_recording()

        ⚠️ engine.recording 체크로는 수동녹화 완료를 감지할 수 없음.
           recording=True는 메인 녹화가 진행 중임을 의미하며,
           수동녹화 완료와 무관하게 유지됨.
        """
        deadline = time.time() + timeout
        # save_manual_clip()이 상태를 MANUAL_WAITING으로 바꿀 때까지 최대 1초 대기
        time.sleep(0.1)
        while not self._stop_ev.is_set() and time.time() < deadline:
            state = getattr(self._engine, 'manual_state', 0)
            MANUAL_IDLE = getattr(self._engine.__class__, 'MANUAL_IDLE', 0)
            if state == MANUAL_IDLE:
                break
            time.sleep(0.2)
        else:
            if time.time() >= deadline:
                self._engine.signals.status_message.emit(
                    f"[커널] wait_manual_clip 타임아웃 ({timeout}초)")


    def power_connect(self, port: str, baudrate: int = 9600) -> bool:
        """
        전원 제어기 시리얼 포트 연결.
        Parameters
        ----------
        port     : 포트 이름 (예: "COM3", "/dev/ttyUSB0")
        baudrate : 보드레이트 (기본 9600)
        Returns
        -------
        True : 연결 성공, False : 실패
        """
        if self.power is None:
            self._log("[Power] ❌ PowerController 미초기화 — MainWindow 로드 필요")
            return False
        return self.power.connect(port, baudrate)

    def power_disconnect(self):
        """전원 제어기 연결 해제."""
        if self.power is None: return
        self.power.disconnect()

    def power_on(self, channel: str) -> bool:
        """
        지정 채널 ON.
        Parameters
        ----------
        channel : "bplus" | "tg_bplus" | "acc" | "ign"
                  "plbm_real" (PLBM 실물 릴레이, D12)
                  "plbm_sim"  (PLBM 모사 릴레이, D8스위치/D13릴레이 — 상호 배타)
        Returns
        -------
        True : 전송 성공
        """
        if self.power is None:
            self._log("[Power] ❌ PowerController 미초기화"); return False
        return self.power.channel_on(channel)

    def power_off(self, channel: str) -> bool:
        """
        지정 채널 OFF.
        Parameters
        ----------
        channel : "bplus" | "tg_bplus" | "acc" | "ign"
                  "plbm_real" | "plbm_sim"
        """
        if self.power is None:
            self._log("[Power] ❌ PowerController 미초기화"); return False
        return self.power.channel_off(channel)

    def power_all_on(self, delay: float = 0.3) -> bool:
        """전체 전원 ON — 채널 4개 순서대로 켜기 (bplus→tg_bplus→acc→ign).
        
        Args:
            delay: 채널 간 대기 시간(초), 기본 0.3초
        """
        if self.power is None:
            self._log("[Power] ❌ PowerController 미초기화"); return False
        import time as _t
        ok = True
        for ch in ["bplus", "tg_bplus", "acc", "ign"]:
            result = self.power.channel_on(ch)
            if not result:
                ok = False
            self._log(f"[Power] {'✅' if result else '❌'} ON: {ch}")
            _t.sleep(delay)
        return ok

    def power_all_off(self) -> bool:
        """전체 전원 OFF (비상 정지 — ALL OFF 명령 전송)."""
        if self.power is None:
            self._log("[Power] ❌ PowerController 미초기화"); return False
        return self.power.all_off()

    def power_send(self, cmd: str) -> bool:
        """
        임의 명령 문자열 직접 전송.
        Parameters
        ----------
        cmd : 전송 ASCII 명령 (예: "1", "Q", "0")
        """
        if self.power is None:
            self._log("[Power] ❌ PowerController 미초기화"); return False
        return self.power.send(cmd)

    def power_read(self, timeout: float = 0.2) -> str:
        """
        시리얼 수신 버퍼 읽기.
        Parameters
        ----------
        timeout : 최대 대기 시간(초, 기본 0.2)
        Returns
        -------
        str : 수신 문자열 (없으면 빈 문자열)
        """
        if self.power is None: return ""
        return self.power.read(timeout)

    def power_list_ports(self) -> list:
        """사용 가능한 시리얼 포트 목록 반환 (list[str])."""
        if self.power is None: return []
        return self.power.list_ports()

    @property
    def power_connected(self) -> bool:
        """현재 전원 제어기 연결 여부."""
        return bool(self.power and self.power.is_connected)

    def emit_status(self, msg: str):
        self._signals.status_message.emit(f"[Kernel] {msg}")

    def set_tc_result(self, result: str):
        """
        TC 결과 설정 — 누적 방식.
        - FAIL/FAIL_STEPX: 무조건 추가 (여러 스텝 FAIL 모두 기록)
        - PASS: 이미 FAIL이 있으면 무시, 없으면 추가
        - 최종 결과: FAIL이 하나라도 있으면 FAIL, 없으면 마지막 값
        """
        self._engine.tc_verify_result = result
        _r = result.strip().upper()
        _has_fail = any(e.startswith('FAIL') for e in self._kernel_tc_result)
        if _r.startswith('FAIL'):
            # FAIL 계열은 중복 없이 누적
            if _r not in self._kernel_tc_result:
                self._kernel_tc_result.append(_r)
        elif _r == 'PASS':
            # PASS는 FAIL이 없을 때만 추가
            if not _has_fail:
                self._kernel_tc_result = ['PASS']
        else:
            self._kernel_tc_result.append(_r)

    # ── ROI 0~9 일괄 조회 API ★ v4.4 ────────────────────────────────────────
    def read_all_roi(self, source: str = "screen") -> dict:
        """
        ROI 0~9 전체의 OCR 텍스트 / 밝기 / 평균 BGR을 딕셔너리로 반환.

        Returns
        -------
        {
          0: {"text": "A3", "brightness": 210.5, "bgr": (12, 200, 180)},
          1: {"text": "",   "brightness":  30.1, "bgr": ( 5,   5,   5)},
          ...
        }
        캐시 기반 — 추가 OCR 연산 없이 last_text / last_brightness / last_avg_bgr 반환.
        존재하지 않는 인덱스는 포함하지 않음.

        사용 예 (커널 스크립트):
            all_roi = kernel.read_all_roi("screen")
            for idx, info in all_roi.items():
                log(f"ROI[{idx}] text={info['text']}  bright={info['brightness']:.1f}")
            # ROI 2번 값이 "FF"이면 PASS
            if all_roi.get(2, {}).get("text") == "FF":
                kernel.set_tc_result("PASS")
        """
        rois = (self._engine.screen_rois if source == "screen"
                else self._engine.camera_rois)
        result = {}
        for idx, roi in enumerate(rois):
            result[idx] = {
                "text":       roi.last_text,
                "brightness": roi.last_brightness,
                "bgr":        roi.last_avg_bgr,
            }
        return result

    def read_roi_value(self, roi_idx: int, source: str = "screen"):
        roi_idx = roi_idx - 1  # UI는 1-based, 내부는 0-based
        rois = self._engine.screen_rois if source == "screen" else self._engine.camera_rois
        if not rois or roi_idx < 0 or roi_idx >= len(rois): return None
        with self._engine._buf_lock:
            buf   = self._engine._scr_buf if source == "screen" else self._engine._cam_buf
            frame = buf[-1].copy() if buf else None
        if frame is None: return None
        roi    = rois[roi_idx]
        region = frame[roi.y:roi.y+roi.h, roi.x:roi.x+roi.w]
        if region.size == 0: return None
        avg = region.mean(axis=0).mean(axis=0)
        b, g, r = float(avg[0]), float(avg[1]), float(avg[2])
        return 0.114*b + 0.587*g + 0.299*r

    def get_roi_avg(self, roi_idx: int, source: str = "screen"):
        roi_idx = roi_idx - 1  # UI는 1-based, 내부는 0-based
        rois = self._engine.screen_rois if source == "screen" else self._engine.camera_rois
        if not rois or roi_idx < 0 or roi_idx >= len(rois): return None
        with self._engine._buf_lock:
            buf   = self._engine._scr_buf if source == "screen" else self._engine._cam_buf
            frame = buf[-1].copy() if buf else None
        if frame is None: return None
        roi    = rois[roi_idx]
        region = frame[roi.y:roi.y+roi.h, roi.x:roi.x+roi.w]
        if region.size == 0: return None
        avg = region.mean(axis=0).mean(axis=0)
        return (float(avg[0]), float(avg[1]), float(avg[2]))

    def read_roi_text(self, roi_idx: int, source: str = "screen",
                      lang: str = "eng", numeric_only: bool = False) -> str:
        """
        ROI OCR 텍스트 반환.

        백그라운드 OCR 루프가 이미 캐시를 채우고 있으므로,
        캐시가 있으면 즉시 반환(빠름).
        캐시가 비어있으면 즉시 OCR 실행(느림, 최초 1회).

        Parameters
        ----------
        roi_idx      : ROI 인덱스 (0-based)
        source       : "screen" | "camera"
        lang         : pytesseract 언어 코드 (기본 "eng")
        numeric_only : True면 숫자·부호·콜론·점만 추출

        사용 예::
            txt = kernel.read_roi_text(0, "screen")
            if not kernel.roi_text_equals(0, "2"):
                engine.save_manual_clip()
        """
        roi_idx = roi_idx - 1  # UI는 1-based, 내부는 0-based
        roi_idx = roi_idx - 1  # UI는 1-based, 내부는 0-based
        rois = self._engine.screen_rois if source == "screen" \
               else self._engine.camera_rois
        if not rois or roi_idx >= len(rois):
            return ""
        roi = rois[roi_idx]

        # ── 캐시 우선 사용 ───────────────────────────────────────────
        if roi.last_text:
            text = roi.last_text
        else:
            # 캐시 없으면 즉시 실행 (백그라운드 루프가 시작 전일 때)
            text = self._ocr_now(roi_idx, source, lang)

        # ── numeric_only 후처리 ──────────────────────────────────────
        if numeric_only and text:
            import re as _re
            nums = _re.findall(r"[+-]?\d+[.:]?\d*", text)
            text = nums[0] if nums else ""

        return text

    def _ocr_now(self, roi_idx: int, source: str = "screen",
                 lang: str = "eng", zoom_hint: float = 1.0) -> str:
        """
        즉시 OCR 실행 — HEX 전용 고정밀 모드.
        ★ v4.4: 물리 픽셀 기준 최소 400px 강제 확대 + CLAHE + psm 4종
        zoom_hint 파라미터는 하위 호환성 유지를 위해 남겨두나 사용 안 함.
        """
        import re as _re
        rois = self._engine.screen_rois if source == "screen" \
               else self._engine.camera_rois
        if not rois or roi_idx >= len(rois):
            return ""
        with self._engine._buf_lock:
            buf   = self._engine._scr_buf if source == "screen" \
                    else self._engine._cam_buf
            frame = buf[-1].copy() if buf else None
        if frame is None:
            return ""
        roi    = rois[roi_idx]
        rx, ry, rw, rh = roi.rect()
        region = frame[ry:ry+rh, rx:rx+rw]
        if region.size == 0:
            return ""

        MIN_DIM = 400
        MAX_DIM = 1200

        def _force_upscale(img):
            h, w = img.shape[:2]
            if h <= 0 or w <= 0: return img
            if h > 0 and w / max(h, 1) > 8:
                pad = int(w / 8) - h
                val = img[0,0].tolist() if img.ndim==3 else int(img[0,0])
                img = cv2.copyMakeBorder(
                    img, pad//2, pad-pad//2, 0, 0,
                    cv2.BORDER_CONSTANT, value=val)
                h, w = img.shape[:2]
            short = min(h, w)
            if short < MIN_DIM:
                s  = MIN_DIM / short
                nh = min(int(h*s), MAX_DIM)
                nw = min(int(w*s), MAX_DIM)
                img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_CUBIC)
            return img

        _fix = {'o':'0','O':'0','Q':'0','l':'1','I':'1','i':'1',
                '|':'1','Z':'2','z':'2','S':'5','s':'5','G':'6',
                'T':'7','B':'8','g':'9','q':'9'}

        def _clean(text):
            result = ""
            for ch in _re.sub(r'[^0-9A-Fa-f]', '', text):
                fixed = _fix.get(ch, ch)
                if fixed.upper() in '0123456789ABCDEF':
                    result += fixed.upper()
            return result

        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        gray = _force_upscale(gray)

        # CLAHE + 샤프닝
        ts   = max(2, min(8, gray.shape[0] // 30))
        enh  = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(ts,ts)).apply(gray)
        blur = cv2.GaussianBlur(enh, (0,0), 2.0)
        enh  = cv2.addWeighted(enh, 1.8, blur, -0.8, 0)

        # 이진화 후보 6종
        thresh_cands = []
        for img_g, lbl in [(enh, "enh"), (gray, "raw")]:
            bg = cv2.GaussianBlur(img_g, (3,3), 0)
            _, ta = cv2.threshold(bg, 0, 255, cv2.THRESH_BINARY     + cv2.THRESH_OTSU)
            _, tb = cv2.threshold(bg, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            thresh_cands += [(ta, f"{lbl}_otsu"), (tb, f"{lbl}_otsu_inv")]
        tc = cv2.adaptiveThreshold(enh, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY, 21, 8)
        thresh_cands.append((tc, "adaptive"))

        best = ""
        WL   = "0123456789ABCDEFabcdef"
        CFGS = [
            f"--psm 7 --oem 3 -c tessedit_char_whitelist={WL}",
            f"--psm 8 --oem 3 -c tessedit_char_whitelist={WL}",
            f"--psm 6 --oem 3 -c tessedit_char_whitelist={WL}",
            f"--psm 13 --oem 3 -c tessedit_char_whitelist={WL}",
        ]
        try:
            import pytesseract as _tess
            from PIL import Image as _PIL
            if not _tess.pytesseract.tesseract_cmd or \
               not os.path.isfile(_tess.pytesseract.tesseract_cmd):
                _init_tesseract()

            for th, _ in thresh_cands:
                padded = cv2.copyMakeBorder(
                    th, 25, 25, 25, 25, cv2.BORDER_CONSTANT, value=255)
                pil = _PIL.fromarray(padded)
                for cfg in CFGS:
                    try:
                        data  = _tess.image_to_data(pil, config=cfg,
                                    output_type=_tess.Output.DICT)
                        parts = []
                        for t, c in zip(data.get('text',[]), data.get('conf',[])):
                            cl = _clean(t.strip())
                            cv = float(c) if str(c) != '-1' else 0.0
                            if cl and cv >= 0:
                                parts.append(cl)
                        joined = "".join(parts)
                        if len(joined) > len(best):
                            best = joined
                    except Exception:
                        pass
                if best:
                    break
        except ImportError:
            pass
        except Exception as ex:
            self._log(f"[OCR/now] {ex}")

        if not best:
            try:
                import easyocr as _eocr
                if not hasattr(self, "_easyocr_reader"):
                    self._easyocr_reader = _eocr.Reader(
                        ["en"], gpu=False, verbose=False)
                res   = self._easyocr_reader.readtext(
                    region, detail=1, allowlist='0123456789ABCDEFabcdef')
                parts = [_clean(str(r[1])) for r in res if float(r[2]) >= 0.2]
                best  = "".join(p for p in parts if p)
            except Exception:
                pass

        result = _clean(best)
        roi.last_text = result
        return result


    def read_roi_number(self, roi_idx: int, source: str = "screen") -> float:
        """
        ROI 영역의 숫자를 읽어 float으로 반환.
        인식 실패 또는 숫자 없으면 None 반환.

        사용 예 (커널 스크립트):
            n = kernel.read_roi_number(0, "screen")
            if n is not None and n != 2.0:
                log(f"값이 2가 아님: {n}")
                engine.save_manual_clip()
        """
        text = self.read_roi_text(roi_idx, source, numeric_only=True)
        try:
            return float(text.strip()) if text.strip() else None
        except ValueError:
            return None

    def get_roi_last_text(self, roi_idx: int, source: str = "screen") -> str:
        """
        캐시된 마지막 OCR 결과 반환 (재연산 없음 — 빠름).
        read_roi_text() 또는 PreviewLabel의 OCR 업데이트 후 유효.
        """
        roi_idx = roi_idx - 1  # UI는 1-based, 내부는 0-based
        roi_idx = roi_idx - 1  # UI는 1-based, 내부는 0-based
        rois = self._engine.screen_rois if source == "screen" \
               else self._engine.camera_rois
        if not rois or roi_idx >= len(rois):
            return ""
        return rois[roi_idx].last_text

    # ── ROI 조건 판단 함수들 ★ v2.9.3 ───────────────────────────────────────
    def roi_text_equals(self, roi_idx: int, value: str,
                        source: str = "screen",
                        ignore_case: bool = True) -> bool:
        """
        ROI OCR 텍스트가 value와 같으면 True.

        Parameters
        ----------
        roi_idx    : ROI 인덱스
        value      : 비교할 문자열 (예: "OK", "PASS", "2")
        source     : "screen" | "camera"
        ignore_case: 대소문자 무시 여부 (기본 True)

        Returns
        -------
        bool

        예시::
            if kernel.roi_text_equals(0, "OK"):
                engine.save_manual_clip()
                kernel.set_tc_result("PASS")
        """
        txt = self.get_roi_last_text(roi_idx, source).strip()
        if ignore_case:
            return txt.lower() == value.strip().lower()
        return txt == value.strip()

    def roi_text_contains(self, roi_idx: int, value: str,
                          source: str = "screen",
                          ignore_case: bool = True) -> bool:
        """
        ROI OCR 텍스트에 value가 포함되면 True.

        예시::
            if kernel.roi_text_contains(0, "ERR"):
                kernel.set_tc_result("FAIL")
        """
        txt = self.get_roi_last_text(roi_idx, source).strip()
        v   = value.strip()
        if ignore_case:
            return v.lower() in txt.lower()
        return v in txt

    def roi_number_equals(self, roi_idx: int, value: float,
                          source: str = "screen",
                          tolerance: float = 0.0) -> bool:
        """
        ROI OCR 숫자가 value와 같으면 True.

        Parameters
        ----------
        roi_idx   : ROI 인덱스
        value     : 비교할 숫자
        source    : "screen" | "camera"
        tolerance : 허용 오차 (기본 0 — 정확히 일치)

        예시::
            # ROI 값이 2가 아닐 경우
            if not kernel.roi_number_equals(0, 2):
                log("값이 2가 아님 — FAIL")
                kernel.set_tc_result("FAIL")

            # 허용 오차 ±0.5
            if kernel.roi_number_equals(0, 16.0, tolerance=0.5):
                log("약 16 감지")
        """
        n = self.read_roi_number(roi_idx, source)
        if n is None:
            return False
        return abs(n - value) <= tolerance

    def roi_number_compare(self, roi_idx: int, op: str, value: float,
                           source: str = "screen") -> bool:
        """
        ROI OCR 숫자를 연산자로 비교.

        Parameters
        ----------
        roi_idx : ROI 인덱스
        op      : "==" | "!=" | ">" | ">=" | "<" | "<="
        value   : 비교값

        예시::
            # 16:53.95 에서 시간(초) 부분 비교
            if kernel.roi_number_compare(0, ">=", 16.0):
                log("16 이상 감지")

            # 값이 2가 아닐 때
            if kernel.roi_number_compare(0, "!=", 2):
                kernel.set_tc_result("FAIL")
        """
        n = self.read_roi_number(roi_idx, source)
        if n is None:
            return False
        ops = {
            "==": n == value,
            "!=": n != value,
            ">":  n >  value,
            ">=": n >= value,
            "<":  n <  value,
            "<=": n <= value,
        }
        return ops.get(op, False)

    def roi_match(self, roi_idx: int, source: str = "screen") -> bool:
        """
        RoiManagerPanel에서 사용자가 설정한 cond_value 기준으로 매치 여부 반환.
        UI에서 직접 설정한 조건과 동일하게 동작.

        예시::
            # ROI 패널에서 조건값을 "2"로 설정했을 때
            if kernel.roi_match(0):
                log("조건 일치!")
        """
        rois = self._engine.screen_rois if source == "screen" \
               else self._engine.camera_rois
        if not rois or roi_idx >= len(rois):
            return False
        roi = rois[roi_idx]
        return getattr(roi, 'last_match', False)

    # ── API 브릿지 서버 ──────────────────────────────────────────────────────
    def start_bridge(self):
        """
        TCP 소켓 API 브릿지 서버 시작 (포트 {BRIDGE_PORT}).
        외부 .py 스크립트가 recorder_bridge.py 를 import 하면
        engine.start_recording() 등을 JSON RPC로 호출 가능.
        """
        if self._bridge_server and self._bridge_server.is_alive():
            return
        self._bridge_stop.clear()
        self._bridge_server = threading.Thread(
            target=self._bridge_loop, daemon=True, name="BridgeServer")
        self._bridge_server.start()
        self._log(f"[Bridge] API 서버 시작 — localhost:{self.BRIDGE_PORT}")
        # CAN WebSocket 서버도 함께 시작
        self.start_can_ws()

    def stop_bridge(self):
        self._bridge_stop.set()
        if self._bridge_sock:
            try: self._bridge_sock.close()
            except: pass

    # ── CAN WebSocket 브릿지 ──────────────────────────────────────────────
    def start_can_ws(self):
        """
        CAN 신호 실시간 WebSocket 서버 시작 (포트 CAN_WS_PORT).
        클라이언트가 접속하면:
          1. {"cmd":"subscribe","signals":["SigA","SigB"]} 전송
          2. 서버가 {"sig":"SigA","val":1.23,"ts":1234567890.0} 형태로 푸시
          3. {"cmd":"get_cache"} 로 현재 캐시 전체 조회 가능
        """
        if hasattr(self, "_can_ws_thread") and self._can_ws_thread \
                and self._can_ws_thread.is_alive():
            return
        self._can_ws_stop = threading.Event()
        self._can_ws_thread = threading.Thread(
            target=self._can_ws_loop, daemon=True, name="CAN_WS")
        self._can_ws_thread.start()
        self._log(f"[CAN-WS] WebSocket 서버 시작 — ws://localhost:{self.CAN_WS_PORT}")

    def stop_can_ws(self):
        if hasattr(self, "_can_ws_stop"): self._can_ws_stop.set()
        if hasattr(self, "_can_ws_sock") and self._can_ws_sock:
            try: self._can_ws_sock.close()
            except: pass

    def _can_ws_loop(self):
        """순수 소켓 기반 WebSocket 서버 (외부 라이브러리 불필요)."""
        import socket as _sk, hashlib as _hl, base64 as _b64
        import struct as _st, json as _js

        srv = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
        srv.setsockopt(_sk.SOL_SOCKET, _sk.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", self.CAN_WS_PORT))
            srv.listen(16)
            srv.settimeout(1.0)
            self._can_ws_sock = srv
        except Exception as ex:
            self._log(f"[CAN-WS] 서버 시작 실패: {ex}"); return

        def _ws_handshake(conn):
            """HTTP → WebSocket 업그레이드 핸드셰이크."""
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(1024)
            key = ""
            for line in data.decode("utf-8", errors="replace").splitlines():
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
                    break
            magic   = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            accept  = _b64.b64encode(
                _hl.sha1((key + magic).encode()).digest()).decode()
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n")
            conn.sendall(resp.encode())

        def _ws_send(conn, payload: str):
            """WebSocket 텍스트 프레임 전송."""
            b = payload.encode("utf-8")
            n = len(b)
            if n <= 125:
                header = _st.pack("BB", 0x81, n)
            elif n <= 65535:
                header = _st.pack("!BBH", 0x81, 126, n)
            else:
                header = _st.pack("!BBQ", 0x81, 127, n)
            try: conn.sendall(header + b)
            except Exception: pass

        def _ws_recv(conn):
            """WebSocket 프레임 수신 → 텍스트 반환."""
            try:
                h = conn.recv(2)
                if len(h) < 2: return None
                masked = bool(h[1] & 0x80)
                plen   = h[1] & 0x7F
                if plen == 126:
                    plen = _st.unpack("!H", conn.recv(2))[0]
                elif plen == 127:
                    plen = _st.unpack("!Q", conn.recv(8))[0]
                mask  = conn.recv(4) if masked else b"\x00" * 4
                data  = conn.recv(plen)
                if masked:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                opcode = h[0] & 0x0F
                if opcode == 8: return None  # close
                return data.decode("utf-8", errors="replace")
            except Exception: return None

        def _client_handler(conn):
            """WebSocket 클라이언트 처리 스레드."""
            try:
                _ws_handshake(conn)
                conn.settimeout(None)
                import queue as _q
                q = _q.Queue(maxsize=500)

                def _push(sig_name, val, ts):
                    msg = _js.dumps({
                        "sig": sig_name,
                        "val": round(val, 4) if val is not None else None,
                        "ts":  round(ts, 3)
                    }, ensure_ascii=False)
                    try: q.put_nowait(msg)
                    except _q.Full: pass

                subscribed = []

                # 송신 스레드
                def _sender():
                    while True:
                        try:
                            msg = q.get(timeout=1.0)
                            if msg is None: break
                            _ws_send(conn, msg)
                        except Exception: pass
                import threading as _thr
                st = _thr.Thread(target=_sender, daemon=True)
                st.start()

                # 수신 루프 (클라이언트 명령)
                while True:
                    raw_msg = _ws_recv(conn)
                    if raw_msg is None: break
                    try:
                        cmd = _js.loads(raw_msg)
                    except Exception: continue

                    if cmd.get("cmd") == "subscribe":
                        sigs = cmd.get("signals", [])
                        for sn in sigs:
                            _can_manager.subscribe(sn, _push)
                            subscribed.append(sn)
                        _ws_send(conn, _js.dumps({
                            "ok": True, "subscribed": subscribed}))

                    elif cmd.get("cmd") == "get_cache":
                        cache = {k: {"val": round(v[1], 4) if v[1] is not None else None,
                                     "ts": round(v[0], 3)}
                                 for k, v in _can_manager.sig_cache.items()}
                        _ws_send(conn, _js.dumps(
                            {"cmd": "cache", "data": cache}))

                    elif cmd.get("cmd") == "unsubscribe":
                        _can_manager.unsubscribe_all(_push)
                        subscribed.clear()

                    elif cmd.get("cmd") == "ping":
                        _ws_send(conn, _js.dumps({"pong": True}))

            except Exception: pass
            finally:
                _can_manager.unsubscribe_all(_push)
                try: q.put_nowait(None)
                except Exception: pass
                try: conn.close()
                except Exception: pass

        while not self._can_ws_stop.is_set():
            try:
                conn, _ = srv.accept()
                threading.Thread(target=_client_handler,
                                 args=(conn,), daemon=True).start()
            except Exception: continue
        try: srv.close()
        except: pass

    def _bridge_loop(self):
        import socket as _sock, struct as _struct
        srv = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        srv.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", self.BRIDGE_PORT))
            srv.listen(8)
            self._bridge_sock = srv
            srv.settimeout(1.0)
        except Exception as ex:
            self._log(f"[Bridge] 서버 시작 실패: {ex}"); return

        while not self._bridge_stop.is_set():
            try:
                conn, _ = srv.accept()
            except Exception:
                continue
            threading.Thread(
                target=self._bridge_handle, args=(conn,),
                daemon=True).start()
        try: srv.close()
        except: pass

    def _bridge_handle(self, conn):
        """클라이언트 연결 처리 — JSON 요청/응답."""
        import socket as _sock
        try:
            conn.settimeout(30.0)
            raw = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk: break
                raw += chunk
                if raw.endswith(b"\n"): break
            req  = json.loads(raw.decode("utf-8").strip())
            resp = self._dispatch_bridge(req)
            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode())
        except Exception as ex:
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(ex)}) + "\n").encode())
            except: pass
        finally:
            try: conn.close()
            except: pass

    def _dispatch_bridge(self, req: dict) -> dict:
        """브릿지 RPC 디스패처."""
        cmd    = req.get("cmd", "")
        params = req.get("params", {})
        e      = self._engine
        try:
            if   cmd == "start_recording":   e.start_recording();            return {"ok": True}
            elif cmd == "stop_recording":    e.stop_recording();             return {"ok": True}
            elif cmd == "save_manual_clip":  ok = e.save_manual_clip();      return {"ok": ok}
            elif cmd == "capture_frame":
                src = params.get("source", "screen")
                tag = params.get("tc_tag", "")
                p   = e.capture_frame(src, tag)
                return {"ok": bool(p), "path": p}
            elif cmd == "start_ac":          e.start_ac();                   return {"ok": True}
            elif cmd == "stop_ac":           e.stop_ac();                    return {"ok": True}
            elif cmd == "set_tc_result":
                e.tc_verify_result = params.get("result", "")
                return {"ok": True}
            elif cmd == "read_roi_value":
                val = self.read_roi_value(
                    params.get("roi_idx", 0), params.get("source", "screen"))
                return {"ok": True, "value": val}
            elif cmd == "get_roi_avg":
                val = self.get_roi_avg(
                    params.get("roi_idx", 0), params.get("source", "screen"))
                return {"ok": True, "value": val}
            elif cmd == "read_roi_text":
                text = self.read_roi_text(
                    params.get("roi_idx", 0),
                    params.get("source", "screen"),
                    params.get("lang", "eng"),
                    params.get("numeric_only", False))
                return {"ok": True, "text": text}
            elif cmd == "read_roi_number":
                num = self.read_roi_number(
                    params.get("roi_idx", 0),
                    params.get("source", "screen"))
                return {"ok": True, "value": num}
            elif cmd == "read_all_roi":
                source = params.get("source", "screen")
                rois = (e.screen_rois if source == "screen" else e.camera_rois)
                data = {str(i): {"text": r.last_text,
                                 "brightness": r.last_brightness,
                                 "bgr": list(r.last_avg_bgr)}
                        for i, r in enumerate(rois)}
                resp = {"all_roi": data}
            elif cmd == "get_roi_last_text":
                text = self.get_roi_last_text(
                    params.get("roi_idx", 0),
                    params.get("source", "screen"))
                return {"ok": True, "text": text}
            elif cmd == "roi_text_equals":
                r = self.roi_text_equals(
                    params.get("roi_idx", 0),
                    params.get("value", ""),
                    params.get("source", "screen"),
                    params.get("ignore_case", True))
                return {"ok": True, "result": r}
            elif cmd == "roi_text_contains":
                r = self.roi_text_contains(
                    params.get("roi_idx", 0),
                    params.get("value", ""),
                    params.get("source", "screen"),
                    params.get("ignore_case", True))
                return {"ok": True, "result": r}
            elif cmd == "roi_number_equals":
                r = self.roi_number_equals(
                    params.get("roi_idx", 0),
                    params.get("value", 0.0),
                    params.get("source", "screen"),
                    params.get("tolerance", 0.0))
                return {"ok": True, "result": r}
            elif cmd == "roi_number_compare":
                r = self.roi_number_compare(
                    params.get("roi_idx", 0),
                    params.get("op", "=="),
                    params.get("value", 0.0),
                    params.get("source", "screen"))
                return {"ok": True, "result": r}
            elif cmd == "roi_match":
                r = self.roi_match(
                    params.get("roi_idx", 0),
                    params.get("source", "screen"))
                return {"ok": True, "result": r}
            elif cmd == "get_state":
                return {"ok": True,
                        "recording": e.recording,
                        "ac_count":  e.ac_count,
                        "output_dir": e.output_dir}
            elif cmd == "log":
                self._log(f"[외부] {params.get('msg','')}")
                return {"ok": True}
            # ── CAN 명령 ──────────────────────────────────────────
            elif cmd == "can_read":
                val = _can_manager.can_read(
                    params.get("sig_name",""),
                    float(params.get("timeout", 0.0)))
                return {"value": val}
            elif cmd == "can_write":
                ok = _can_manager.can_write(
                    params.get("msg_name",""),
                    params.get("signals",{}))
                return {"ok": ok, "status": _can_manager.status_msg}
            elif cmd == "can_wait_value":
                r = _can_manager.can_wait_value(
                    sig_name=params.get("sig_name",""),
                    expected=params.get("expected",0),
                    timeout=float(params.get("timeout",10.0)),
                    tolerance=float(params.get("tolerance",0.0)))
                return {"result": r}
            elif cmd == "can_read_all":
                return {"signals": _can_manager.can_read_all()}
            elif cmd == "can_status":
                return {"info": _can_manager.info(),
                        "connected": _can_manager.connected,
                        "canoe_mode": _can_manager.canoe_mode}
            elif cmd == "can_connect_canoe":
                ok = _can_manager.connect_canoe()
                return {"ok": ok, "status": _can_manager.status_msg}
            elif cmd == "canoe_write":
                ok = _can_manager.canoe_write(
                    params.get("msg_name",""),
                    params.get("signals",{}))
                return {"ok": ok, "status": _can_manager.status_msg}
            elif cmd == "ping":              return {"ok": True, "pong": True}
            else:
                return {"ok": False, "error": f"Unknown cmd: {cmd}"}
        except Exception as ex:
            return {"ok": False, "error": str(ex)}

    def generate_bridge_client(self, save_path: str = "") -> str:
        """
        외부 .py 에서 import 할 수 있는 recorder_bridge.py 생성.
        외부 스크립트에서:
            from recorder_bridge import engine, log, kernel_wait
        """
        code = f'''\
# -*- coding: utf-8 -*-
"""
recorder_bridge.py — Screen & Camera Recorder API 브릿지 클라이언트
자동 생성됨 — 이 파일을 외부 스크립트와 같은 폴더에 두고 import 하세요.

사용 예:
    from recorder_bridge import engine, log, kernel_wait, env_check

    log("시작")
    engine.start_recording()
    kernel_wait(5)
    engine.stop_recording()
    log("완료")
"""
import socket, json, time, sys, os

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = {self.BRIDGE_PORT}

def _call(cmd: str, **params) -> dict:
    """브릿지 서버로 RPC 호출."""
    req = json.dumps({{"cmd": cmd, "params": params}}) + "\\n"
    try:
        with socket.create_connection((BRIDGE_HOST, BRIDGE_PORT), timeout=10) as s:
            s.sendall(req.encode("utf-8"))
            buf = b""
            while True:
                chunk = s.recv(4096)
                if not chunk: break
                buf += chunk
                if buf.endswith(b"\\n"): break
        resp = json.loads(buf.decode("utf-8").strip())
        if not resp.get("ok"):
            print(f"[Bridge] 오류: {{resp.get('error','?')}}")
        return resp
    except Exception as ex:
        print(f"[Bridge] 연결 실패: {{ex}}")
        return {{"ok": False, "error": str(ex)}}

class _Engine:
    """CoreEngine API 프록시 — engine.start_recording() 식으로 사용."""
    @property
    def recording(self) -> bool:
        r = _call("get_state"); return r.get("recording", False)
    @property
    def output_dir(self) -> str:
        r = _call("get_state"); return r.get("output_dir", "")
    @property
    def ac_count(self) -> int:
        r = _call("get_state"); return r.get("ac_count", 0)

    def start_recording(self):     _call("start_recording")
    def stop_recording(self):      _call("stop_recording")
    def save_manual_clip(self):    return _call("save_manual_clip").get("ok", False)
    def capture_frame(self, source="screen", tc_tag=""):
        return _call("capture_frame", source=source, tc_tag=tc_tag).get("path", "")
    def start_ac(self):            _call("start_ac")
    def stop_ac(self):             _call("stop_ac")
    def set_tc_result(self, r):    _call("set_tc_result", result=r)

class _Kernel:
    def read_roi_value(self, roi_idx=0, source="screen"):
        return _call("read_roi_value", roi_idx=roi_idx, source=source).get("value")
    def get_roi_avg(self, roi_idx=0, source="screen"):
        return _call("get_roi_avg", roi_idx=roi_idx, source=source).get("value")
    def read_roi_text(self, roi_idx=0, source="screen", lang="eng", numeric_only=False):
        return _call("read_roi_text", roi_idx=roi_idx, source=source,
                     lang=lang, numeric_only=numeric_only).get("text", "")
    def read_roi_number(self, roi_idx=0, source="screen"):
        return _call("read_roi_number", roi_idx=roi_idx, source=source).get("value")
    def get_roi_last_text(self, roi_idx=0, source="screen"):
        return _call("get_roi_last_text", roi_idx=roi_idx, source=source).get("text", "")
    def set_tc_result(self, r):
        _call("set_tc_result", result=r)
    def roi_text_equals(self, roi_idx=0, value="", source="screen", ignore_case=True):
        return _call("roi_text_equals", roi_idx=roi_idx, value=value,
                     source=source, ignore_case=ignore_case).get("result", False)
    def roi_text_contains(self, roi_idx=0, value="", source="screen", ignore_case=True):
        return _call("roi_text_contains", roi_idx=roi_idx, value=value,
                     source=source, ignore_case=ignore_case).get("result", False)
    def roi_number_equals(self, roi_idx=0, value=0.0, source="screen", tolerance=0.0):
        return _call("roi_number_equals", roi_idx=roi_idx, value=value,
                     source=source, tolerance=tolerance).get("result", False)
    def roi_number_compare(self, roi_idx=0, op="==", value=0.0, source="screen"):
        return _call("roi_number_compare", roi_idx=roi_idx, op=op,
                     value=value, source=source).get("result", False)
    def roi_match(self, roi_idx=0, source="screen"):
        return _call("roi_match", roi_idx=roi_idx, source=source).get("result", False)
    def is_stopped(self): return False
    def wait(self, s): time.sleep(s)
    # ── CAN API (브릿지 클라이언트용) ────────────────────────────────
    def can_read(self, sig_name: str, timeout: float = 0.0):
        return _call("can_read", sig_name=sig_name, timeout=timeout).get("value")
    def can_write(self, msg_name: str, signals: dict) -> bool:
        return _call("can_write", msg_name=msg_name, signals=signals).get("ok", False)
    def can_wait_value(self, sig_name: str, expected,
                       timeout: float = 10.0, tolerance: float = 0.0) -> bool:
        return _call("can_wait_value", sig_name=sig_name, expected=expected,
                     timeout=timeout, tolerance=tolerance).get("result", False)
    def can_read_all(self) -> dict:
        return _call("can_read_all").get("signals", {{}})

def log(msg: str):
    """브릿지 서버 로그 + 콘솔 출력."""
    print(f"[KernelLog] {{msg}}")
    _call("log", msg=msg)

def kernel_wait(seconds: float):
    """대기 (단순 time.sleep)."""
    time.sleep(seconds)

def env_check():
    """현재 Python 환경 + 브릿지 연결 확인."""
    print(f"Python: {{sys.version}}")
    print(f"실행파일: {{sys.executable}}")
    r = _call("ping")
    if r.get("pong"):
        print(f"[Bridge] ✅ 연결 OK (localhost:{{BRIDGE_PORT}})")
    else:
        print(f"[Bridge] ❌ 연결 실패 — 프로그램이 실행 중인지 확인하세요")

engine = _Engine()
kernel = _Kernel()
'''
        if not save_path:
            base = self._engine.base_dir if self._engine else \
                   os.path.join(os.path.expanduser("~"), "Desktop", "bltn_rec")
            os.makedirs(base, exist_ok=True)
            save_path = os.path.join(base, "recorder_bridge.py")
        with open(save_path, 'w', encoding='utf-8') as f:
            f.write(code)
        self._log(f"[Bridge] 클라이언트 생성: {save_path}")
        return save_path
    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        full = f"[{ts}] {msg}"
        self._signals.status_message.emit(full)
        if self.log_callback:
            try: self.log_callback(full)
            except: pass

    # ── 실행 제어 ─────────────────────────────────────────────────────────────
    def start(self, scripts: list = None):
        """스크립트 목록 실행 시작. 브릿지 서버도 자동 시작."""
        if self.running: return
        if scripts is not None:
            self.scripts = scripts
        self._stop_ev.clear()
        self.running = True
        # 브릿지 서버 자동 시작 (외부 .py에서 engine 접근 가능하게)
        self.start_bridge()
        self._thread = threading.Thread(
            target=self._run_all, daemon=True, name="KernelRun")
        self._thread.start()

    def stop(self):
        """실행 중단 요청."""
        self._stop_ev.set()
        self.running   = False
        self.cur_title = ""
        self.stop_bridge()

    def _run_all(self):
        """enabled 된 스크립트를 순서대로 실행 + 결과 로그 자동 기록."""
        enabled = [s for s in self.scripts if s.enabled]
        if not enabled:
            self._log("실행할 스크립트 없음")
            self.running = False
            return

        # ── 세션 시작 ─────────────────────────────────────────────────────
        self.result_log.begin_session()

        try:
          for script in enabled:
                if self._stop_ev.is_set(): break
                rep      = 0
                infinite = (script.repeat == 0)
                total    = script.repeat  # 0 = 무한
    
                while not self._stop_ev.is_set():
                    self.cur_title = script.title
                    self._log(
                        f"▶ [{script.title}] 실행 "
                        f"(반복 {rep+1}{'/' + str(script.repeat) if not infinite else '/∞'})")
    
                    # tc_verify_result 초기화 → 스크립트 실행 후 결과 캡처
                    self._engine.tc_verify_result = ""
                    self._kernel_tc_result = []   # 커널 전용 결과 리스트 초기화
                    self._exec_script(script)

                    # ── 결과 판정 ────────────────────────────────────────────
                    # _kernel_tc_result(list) 기준으로 최종 결과 조합
                    # FAIL이 하나라도 있으면 → FAIL_STEPX(_STEPY...) 형태
                    # 모두 PASS → PASS
                    # 비어있거나 중단 → SKIP
                    _fails = [r for r in self._kernel_tc_result
                              if r.startswith('FAIL')]
                    _passes = [r for r in self._kernel_tc_result
                               if r == 'PASS']
                    if _fails:
                        # 여러 FAIL 스텝을 '_'로 연결
                        # FAIL_STEP5-B + FAIL_STEP6 → FAIL_STEP5-B_STEP6
                        if len(_fails) == 1:
                            result_str = _fails[0]
                        else:
                            # 공통 'FAIL_' 접두어 제거 후 연결
                            _steps = []
                            for _f in _fails:
                                _steps.append(_f[5:] if _f.startswith('FAIL_') else _f)
                            result_str = 'FAIL_' + '_'.join(_steps)
                    elif _passes:
                        result_str = 'PASS'
                    elif self._stop_ev.is_set():
                        result_str = 'SKIP'
                    else:
                        # engine.tc_verify_result fallback
                        _fb = (self._engine.tc_verify_result or '').strip().upper()
                        result_str = _fb if _fb in ('PASS','FAIL') else 'SKIP'
    
                    self.result_log.record(
                        script_name=script.title,
                        attempt=rep + 1,
                        total=total,
                        result=result_str,
                    )
                    self._log(
                        f"  → 결과: {result_str}  "
                        f"({rep+1}{'/' + str(total) if total > 0 else '/∞'})")

                    # ── 1회 완료 직후 자동저장 (비동기, 중복 방지) ───────
                    # 이전 저장 스레드가 아직 실행 중이면 스킵
                    # (무한반복 스크립트에서 저장 스레드 누적 방지)
                    _title_snap  = script.title
                    _attempt_now = rep + 1
                    _ae_name     = f"AutoExport_{_title_snap[:20]}"
                    _ae_running  = any(
                        t.name == _ae_name
                        for t in threading.enumerate() if t.is_alive()
                    )
                    if not _ae_running:
                        threading.Thread(
                            target=self._auto_export,
                            args=(_title_snap, _attempt_now, result_str),
                            daemon=True,
                            name=_ae_name
                        ).start()

                    rep += 1
                    if not infinite and rep >= script.repeat: break
                    if self._stop_ev.is_set(): break
                    self.wait(0.1)
    
        except Exception as _run_ex:
            import traceback as _tb
            self._log(f"❌ [_run_all] 예외 발생:\n{_tb.format_exc()}")
        finally:
            self.running   = False
            self.cur_title = ""
            self._log("✅ 커널 실행 완료  (결과 로그: 패널 하단 [📄 결과 내보내기] 버튼)")
            # ── 최종 자동저장 (크래시 포함 항상 실행) ──────────────────
            # 루프 내 저장과 중복될 수 있으나 크래시 시 마지막 상태 보장
            try:
                self._auto_export()  # finally는 동기 실행 (종료 전 완료 보장)
            except Exception:
                pass

    def _auto_export(self, title: str = "", attempt: int = 0,
                     result_str: str = ""):
        """
        1회 스크립트 완료 직후 결과·로그 자동 저장.
        [로그] 파일명: (PASS|FAIL|SKIP)kernel_log_YYYYMMDD_HHMMSS_슬롯명_NNN회.txt
               result_str 지정 시 파일명 앞에 (결과) 접두어 추가
        [결과] 스크립트 제목 기반 분할 파일, 누적 업데이트
        """
        # ── log_dir 결정 ────────────────────────────────────────────────────
        # ★ [수정1] 저장 경로 패널의 폴더 트리를 kernel_logs 아래에도 동일 반영.
        # engine.vehicle_type / tc_id / extra_segments 를 읽어
        # kernel_logs/<vehicle_type>/<YYYYMMDD>/<tc_id>/<seg1>/<seg2>/… 구조 생성.
        try:
            _bd = (getattr(self._engine, 'base_dir', None) or "").strip()
            if not _bd:
                _bd = os.path.join(os.path.expanduser("~"), "Desktop", "bltn_rec")
            _now_date = datetime.now().strftime("%Y%m%d")
            _parts = [_bd, "kernel_logs"]
            _vt = (getattr(self._engine, 'vehicle_type', None) or "").strip()
            if _vt:
                _parts.append(_vt)
            _parts.append(_now_date)
            _tc = (getattr(self._engine, 'tc_id', None) or "").strip()
            if _tc:
                _parts.append(_tc)
            for _seg in (getattr(self._engine, 'extra_segments', None) or []):
                if _seg and _seg.strip():
                    _parts.append(_seg.strip())
            log_dir = os.path.join(*_parts)
            os.makedirs(log_dir, exist_ok=True)
        except Exception as _e:
            self._log(f"[AutoExport] ⚠ log_dir 생성 실패: {_e}")
            return

        # ── 저장 대상 제목 결정 ─────────────────────────────────────────────
        if title:
            # 1회 완료 직후 호출 — 해당 스크립트만
            titles = [title]
        else:
            # 크래시/최종 fallback — result_log에 기록된 전체
            try:
                titles = list(dict.fromkeys(
                    e["script"] for e in self.result_log._entries
                    if e.get("script")
                ))
            except Exception:
                titles = []
            if not titles:
                return

        # ── 결과 파일 저장 ───────────────────────────────────────────────────
        for t in titles:
            try:
                rpath = self.result_log.result_path(log_dir, t)
                ok    = self.result_log.export_txt(rpath)
                if ok:
                    self._log(f"📄 자동 저장: {os.path.basename(rpath)}")
                else:
                    self._log(f"[AutoExport] ⚠ 결과 저장 실패: {rpath}")
            except Exception as _e:
                self._log(f"[AutoExport] ⚠ 결과 저장 예외({t}): {_e}")

        # ── 로그 파일 저장 ───────────────────────────────────────────────────
        panel = self._panel_ref
        if panel is None:
            return
        try:
            # deque → list 스냅샷 (스레드 안전, 이후 clear와 경쟁 없음)
            lines = list(getattr(panel, "_log_lines", []))
            if not lines:
                return
            _now_str   = datetime.now().strftime("%Y%m%d_%H%M%S")
            _saved_any = False
            for t in titles:
                try:
                    _safe_t  = KernelResultLog._safe_filename(t)
                    _cnt_str = f"{attempt:03d}회" if attempt > 0 else "000회"
                    # 결과에 따라 파일명 앞에 (PASS)/(FAIL_STEPX)/(SKIP) 접두어
                    # FAIL_STEP5-B 등 스텝 정보 포함 문자열도 허용
                    _res_up  = result_str.strip().upper()
                    if _res_up == "PASS":
                        _prefix = "(PASS)"
                    elif _res_up == "SKIP":
                        _prefix = "(SKIP)"
                    elif _res_up.startswith("FAIL"):
                        # FAIL, FAIL_STEP5-B, FAIL_STEP6 등 모두 허용
                        # 파일명 불가 문자(/ \ : * ? " < > |) 제거
                        import re as _re2
                        _safe_res = _re2.sub(r'[\\/:*?"<>|]', '_', _res_up)
                        _prefix = f"({_safe_res})"
                    else:
                        _prefix = ""
                    lname    = f"{_prefix}kernel_log_{_now_str}_{_safe_t}_{_cnt_str}.txt"
                    lpath    = os.path.join(log_dir, lname)
                    _ts      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    with open(lpath, "w", encoding="utf-8") as f:
                        f.write(
                            f"=== 커널 실행 로그  {_ts}  [{t}]  {_cnt_str} ===\n")
                        f.write("\n".join(lines))
                        f.write("\n")
                    self._log(f"📋 로그 저장: {lname}")
                    _saved_any = True
                except Exception as _e:
                    self._log(f"[AutoExport] ⚠ 로그 저장 예외({t}): {_e}")
            # ★ 저장 완료 후 메모리 즉시 휘발
            if _saved_any:
                try:
                    panel._log_lines.clear()
                except Exception:
                    pass
                try:
                    self._signals.status_message.emit("__CLEAR_KERNEL_LOG__")
                except Exception:
                    pass
        except Exception as _e:
            self._log(f"[AutoExport] ⚠ 로그 저장 중 예외: {_e}")

    def get_env_info(self) -> str:
        """현재 Python 환경 정보 + OCR 엔진 설치 상태 확인."""
        import sys as _sys, platform as _plat
        lines = [
            f"Python {_sys.version}",
            f"실행파일: {_sys.executable}",
            f"OS: {_plat.system()} {_plat.release()}",
            "─── 설치된 주요 패키지 ───",
        ]
        pkgs = [
            "PyQt5", "cv2", "numpy", "mss", "PIL",
            "pynput", "fastapi", "uvicorn", "pytesseract", "easyocr",
        ]
        for pkg in pkgs:
            try:
                mod = __import__(pkg)
                ver = getattr(mod, '__version__', '?')
                lines.append(f"  ✅ {pkg}: {ver}")
            except ImportError:
                lines.append(f"  ❌ {pkg}: 미설치")

        # OCR 엔진 상태 요약
        lines.append("─── OCR 엔진 상태 ───")
        try:
            import pytesseract as _tess
            cmd = _tess.pytesseract.tesseract_cmd
            lines.append(f"  pytesseract 설치됨 (tesseract_cmd={cmd})")
            # Tesseract 실행파일 확인
            if cmd and os.path.isfile(cmd):
                try:
                    ver = _tess.get_tesseract_version()
                    lines.append(f"  ✅ Tesseract 실행파일: {cmd}")
                    lines.append(f"     버전: {ver}")
                except Exception as ex:
                    lines.append(f"  ⚠ 실행파일 있지만 실행 실패: {ex}")
            else:
                # 자동 재탐색
                status = _init_tesseract()
                if status.startswith("ok:"):
                    lines.append(f"  ✅ Tesseract 자동 탐색 성공: {status[3:]}")
                    try:
                        ver = _tess.get_tesseract_version()
                        lines.append(f"     버전: {ver}")
                    except Exception:
                        pass
                else:
                    lines.append("  ❌ Tesseract 실행파일을 찾을 수 없습니다!")
                    lines.append("     현재 탐색된 경로: " + str(cmd))
                    lines.append("     해결 방법:")
                    lines.append("       1) https://github.com/UB-Mannheim/tesseract/wiki")
                    lines.append("          에서 Windows 설치파일 다운로드 후 설치")
                    lines.append("       2) 설치 후 프로그램 재시작 (자동 탐색)")
                    lines.append("       3) 또는 PATH에 tesseract.exe 폴더 추가")
        except ImportError:
            lines.append("  ❌ pytesseract 미설치")
            lines.append("    → pip install pytesseract  (+ Tesseract 실행파일 별도 설치 필요)")
        try:
            import easyocr as _eocr
            lines.append(f"  ✅ easyocr: 사용 가능 (pytesseract 없을 때 자동 폴백)")
        except ImportError:
            lines.append("  ❌ easyocr 미설치")
            lines.append("    → pip install easyocr")

        lines.append("")
        lines.append("※ OCR 없이는 ROI 텍스트 인식 불가.")
        lines.append(f"  프로그램 시작 시 Tesseract 탐색 결과: {_TESS_STATUS}")
        lines.append("  pytesseract (정확) 또는 easyocr (설치 간편) 중 하나 필요.")

        # pip list 간이 출력
        try:
            import subprocess as _sp
            r = _sp.run(
                [sys.executable, "-m", "pip", "list", "--format=columns"],
                capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                lines.append("─── pip list ───")
                for ln in r.stdout.strip().split('\n')[:30]:
                    lines.append(f"  {ln}")
        except Exception as ex:
            lines.append(f"  (pip list 실패: {ex})")
        return "\n".join(lines)

    def _exec_script(self, script: KernelScript):
        """
        단일 스크립트 실행 (exec).
        ─ 전역 네임스페이스에 sys, subprocess, pip_install 등 노출
        ─ traceback 전체 출력
        """
        import traceback as _tb, subprocess as _sp

        def _pip_install(*pkgs):
            """커널 스크립트 내에서 패키지 설치: pip_install('numpy', 'requests')"""
            for pkg in pkgs:
                self._log(f"[pip] 설치 중: {pkg}")
                r = _sp.run(
                    [sys.executable, "-m", "pip", "install", pkg],
                    capture_output=True, text=True, timeout=120)
                if r.returncode == 0:
                    self._log(f"[pip] ✅ {pkg} 설치 완료")
                else:
                    self._log(f"[pip] ❌ {pkg} 실패: {r.stderr.strip()[-200:]}")

        def _pip_list():
            """설치된 패키지 목록 출력."""
            r = _sp.run(
                [sys.executable, "-m", "pip", "list", "--format=columns"],
                capture_output=True, text=True, timeout=10)
            self._log("[pip list]\n" + r.stdout)

        def _env_info():
            """Python 환경 정보 출력."""
            self._log(self.get_env_info())

        _globals = {
            "__builtins__": __builtins__,
            # ── 핵심 객체 ──
            "engine":     self._engine,
            "kernel":     self,
            "log":        self._log,
            # ── CAN 객체 ──
            # can.read(sig)          신호값 읽기 (캐시)
            # can.wait_value(sig, v) 값이 v될 때까지 대기
            # can.write(msg, sigs)   메시지 송신
            # can.read_all()         전체 캐시 딕셔너리
            "can":        _can_manager,
            # can.xl_connect(channel=0)           XL Driver 연결 (CANoe 동시 사용 가능)
            # can.xl_write(msg_name, {sig: val})  물리 버스에 직접 CAN 프레임 송신
            # can.xl_disconnect()                 XL Driver 연결 해제
            # can.xl_ready : bool                 XL Driver 준비 여부
            # ── 전원 제어 객체 ──
            # power.ohcl_trigger(ms)       OHCL 릴레이 직접 트리거
            # power.channel_on/off(ch)     채널 ON/OFF
            # power.is_connected           연결 여부
            "power":      getattr(self, 'power', None),
            # ── 표준 라이브러리 ──
            "sys":        sys,
            "os":         os,
            "time":       time,
            "datetime":   datetime,
            "re":         re,
            "json":       json,
            "threading":  threading,
            "subprocess": _sp,
            "platform":   platform,
            # ── 서드파티 ──
            "np":         np,
            "cv2":        cv2,
            # ── 커널 유틸 ──
            "pip_install": _pip_install,
            "pip_list":    _pip_list,
            "env_info":    _env_info,
        }
        # 선택적 패키지
        try:
            import mss as _mss
            _globals["mss"] = _mss
        except ImportError:
            pass
        try:
            from PIL import Image as _PILImg
            _globals["PIL_Image"] = _PILImg
        except ImportError:
            pass

        try:
            # ── 코드 전처리 ──────────────────────────────────────────────
            # 멀티라인 코드를 한 줄로 붙여넣으면 IndentationError 발생.
            # textwrap.dedent + 줄바꿈 정규화로 방지.
            import textwrap as _tw
            raw_code = script.code

            # 1) \r\n → \n 정규화
            raw_code = raw_code.replace('\r\n', '\n').replace('\r', '\n')

            # 2) 공통 들여쓰기 제거 (dedent)
            raw_code = _tw.dedent(raw_code)

            # 3) 앞뒤 공백 제거 후 마지막 줄바꿈 보장
            raw_code = raw_code.strip() + '\n'

            code = compile(raw_code, f"<kernel:{script.title}>", "exec")
            exec(code, _globals)
        except SystemExit:
            self._log(f"[{script.title}] sys.exit() 호출됨 — 스크립트 종료")
        except Exception:
            full_tb = _tb.format_exc()
            self._log(f"❌ [{script.title}] 오류:\n{full_tb}")

    # ── DB 직렬화 ─────────────────────────────────────────────────────────────
    def get_scripts_data(self) -> list:
        return [s.to_dict() for s in self.scripts]

    def set_scripts_data(self, data: list):
        self.scripts = [KernelScript.from_dict(d) for d in data]


