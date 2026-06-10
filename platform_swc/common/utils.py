# -*- coding: utf-8 -*-
"""
common/utils.py
공용 유틸리티 — 폰트·드로우 헬퍼, 경로 변환.
"""
import os, sys, platform, subprocess, threading
from typing import Optional

try:
    from PIL import Image as _PIL_Image, ImageDraw as _PIL_Draw, ImageFont as _PIL_Font
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import numpy as np
    import cv2
except ImportError:
    np = None; cv2 = None  # type: ignore

def _init_tesseract() -> str:
    """
    pytesseract의 Tesseract 실행파일 경로를 자동 탐색·설정.
    Windows 기본 설치 경로 및 PATH를 모두 탐색.
    반환값: "ok:<경로>" | "not_found" | "import_error"
    """
    try:
        import pytesseract as _tess
    except ImportError:
        return "import_error"

    # 이미 설정됐으면 검증만
    current = _tess.pytesseract.tesseract_cmd
    if current and os.path.isfile(current):
        return f"ok:{current}"

    # Windows 기본 설치 경로 후보
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                     "Tesseract-OCR", "tesseract.exe"),
        os.path.join(os.environ.get("APPDATA", ""), "Tesseract-OCR",
                     "tesseract.exe"),
        # Linux/Mac
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/opt/homebrew/bin/tesseract",
    ]

    # PATH에서 탐색
    import shutil as _sh
    path_result = _sh.which("tesseract")
    if path_result:
        candidates.insert(0, path_result)

    for path in candidates:
        if path and os.path.isfile(path):
            _tess.pytesseract.tesseract_cmd = path
            return f"ok:{path}"

    return "not_found"


# 프로그램 시작 시 Tesseract 경로 초기화
_TESS_STATUS = _init_tesseract()

# =============================================================================
@dataclass
class RoiItem:
    """ROI 영역 하나를 표현하는 데이터클래스."""
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    name: str = ""          # 사용자 지정 제목
    description: str = ""   # 사용자 지정 설명
    source: str = "screen"  # "screen" | "camera"

    # ── 런타임 캐시 (저장 안 함) ────────────────────────────────────────
    last_brightness: float = 0.0   # 마지막 측정 밝기 (0~255)
    last_text: str = ""            # 마지막 OCR 결과
    last_avg_bgr: tuple = (0,0,0)  # 마지막 평균 BGR
    last_match: bool = False       # 마지막 cond_value 매치 결과

    # ── 조건값 (저장됨) ──────────────────────────────────────────────────
    cond_value: str = ""           # 사용자가 설정한 비교값 (예: "2", "OK")
    roi_no_rec_enabled: bool = False   # [문제3] NO 판정 시 자동녹화 ON/OFF
    # 런타임 카운트 (저장 안 함 — 세션마다 초기화)
    no_match_count: int = 0            # MATCH→NO 전환 횟수 카운트

    def rect(self):
        return (self.x, self.y, self.w, self.h)

    def to_dict(self) -> dict:
        return {
            "x": self.x, "y": self.y, "w": self.w, "h": self.h,
            "name": self.name, "description": self.description,
            "source": self.source,
            "cond_value": self.cond_value,
            "roi_no_rec_enabled": self.roi_no_rec_enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RoiItem":
        return cls(
            x=d.get("x", 0), y=d.get("y", 0),
            w=d.get("w", 0), h=d.get("h", 0),
            name=d.get("name", ""), description=d.get("description", ""),
            source=d.get("source", "screen"),
            cond_value=d.get("cond_value", ""),
            roi_no_rec_enabled=d.get("roi_no_rec_enabled", False),
        )

    def label(self) -> str:
        return self.name if self.name else f"ROI ({self.x},{self.y})"


# =============================================================================
#  유니코드 / 한글 폰트  (백그라운드 로딩)
# =============================================================================
_FONT_CACHE: dict = {}
_FONT_LOCK = threading.Lock()


def _find_unicode_font(size: int = 18):
    if not PIL_AVAILABLE:
        return None
    import glob as _glob
    candidates = []
    _sys = platform.system()
    if _sys == "Windows":
        wf = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
        for f in ["malgun.ttf","malgunbd.ttf","gulim.ttc","batang.ttc",
                  "NanumGothic.ttf","NanumGothicBold.ttf"]:
            candidates.append(os.path.join(wf, f))
        try:
            import winreg
            try:
                rk = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
                i = 0
                while True:
                    try:
                        nm, data, _ = winreg.EnumValue(rk, i); i += 1
                        if any(k in nm for k in ("맑은","Malgun","나눔","Nanum","굴림","바탕")):
                            p = data if os.path.isabs(data) else os.path.join(wf, data)
                            candidates.insert(0, p)
                    except OSError:
                        break
                winreg.CloseKey(rk)
            except Exception:
                pass
        except ImportError:
            pass
    elif _sys == "Darwin":
        for d in ["/System/Library/Fonts","/Library/Fonts",
                  os.path.expanduser("~/Library/Fonts")]:
            for f in ["AppleSDGothicNeo.ttc","NanumGothic.ttf","Arial Unicode.ttf"]:
                candidates.append(os.path.join(d, f))
    else:
        try:
            r = subprocess.run(["fc-list",":lang=ko","--format=%{file}\n"],
                               capture_output=True, text=True, timeout=5)
            candidates += [l.strip() for l in r.stdout.splitlines() if l.strip()]
        except Exception:
            pass
        candidates += list(_glob.glob("/usr/share/fonts/**/Noto*CJK*.ttc", recursive=True))
        candidates += list(_glob.glob("/usr/share/fonts/**/Nanum*.ttf", recursive=True))

    seen = set()
    for p in candidates:
        if not p or not os.path.exists(p) or p in seen:
            continue
        seen.add(p)
        try:
            fnt = _PIL_Font.truetype(p, size)
            w, h = size * 4, size * 2
            def rnd(txt, _fnt=fnt, _w=w, _h=h):
                img = _PIL_Image.new("L", (_w, _h), 0)
                _PIL_Draw.Draw(img).text((2, 2), txt, font=_fnt, fill=255)
                return bytes(img.tobytes())
            r1, r2, r3 = rnd("가"), rnd("나"), rnd("다")
            if sum(r1) < 20 or r1 == r2 == r3:
                continue
            return fnt
        except Exception:
            continue
    return None


def _get_font(size: int = 18):
    """
    지정 크기의 한글 폰트 반환.
    캐시에 없으면 가장 가까운 크기의 폰트로 fallback하거나
    해당 크기를 즉시 로드.
    """
    with _FONT_LOCK:
        fnt = _FONT_CACHE.get(size, None)
        if fnt is not None:
            return fnt
        # 캐시에 없는 크기 → 가장 가까운 캐시 크기 사용
        if _FONT_CACHE:
            best_sz = min(_FONT_CACHE.keys(), key=lambda k: abs(k - size))
            return _FONT_CACHE.get(best_sz, None)
    # 캐시 자체가 비어있으면 즉시 로드 시도 (드문 경우)
    fnt = _find_unicode_font(size)
    with _FONT_LOCK:
        _FONT_CACHE[size] = fnt
    return fnt


def _preload_fonts():
    # [문제1 수정] 한글이 깨지는 문제: 모든 자주 쓰이는 크기를 미리 로드
    for sz in (10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 26, 28, 30, 32, 36):
        fnt = _find_unicode_font(sz)
        with _FONT_LOCK:
            _FONT_CACHE[sz] = fnt


threading.Thread(target=_preload_fonts, daemon=True, name="FontPreload").start()
def open_folder(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    if platform.system() == "Windows": os.startfile(path)
    elif platform.system() == "Darwin": subprocess.Popen(["open", path])
    else: subprocess.Popen(["xdg-open", path])

def fmt_hms(secs: float) -> str:
    s = int(secs); return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

def draw_time_bar(frame: np.ndarray, now_str: str, elapsed_str: str) -> None:
    ov = frame.copy()
    cv2.rectangle(ov, (4,4),(440,78),(0,0,0),-1)
    cv2.addWeighted(ov, 0.45, frame, 0.55, 0, frame)
    cv2.putText(frame, now_str,     (10,32), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (0,255,80),  2, cv2.LINE_AA)
    cv2.putText(frame, elapsed_str, (10,68), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (80,220,255),2, cv2.LINE_AA)

def draw_memo_overlay(frame: np.ndarray, lines: list, position: str,
                      fw: int, fh: int, font_size: int = 18) -> None:
    if not lines: return
    fnt = _get_font(font_size)
    pad = 8
    try:
        sb = fnt.getbbox("A가") if fnt else None
        line_h = (sb[3]-sb[1]+6) if sb else font_size+6
    except: line_h = font_size+6
    max_tw = 0
    for ln in lines:
        try:
            bb = fnt.getbbox(ln) if fnt else None
            tw = (bb[2]-bb[0]) if bb else len(ln)*(font_size//2+2)
        except: tw = len(ln)*(font_size//2+2)
        max_tw = max(max_tw, tw)
    inner = 12; bg_pad = 4
    box_w = min(max_tw+inner*2, fw-bg_pad*2-pad*2)
    box_h = len(lines)*line_h+14
    if position == "top-left":    x0,y0 = pad, pad+30
    elif position == "top-right": x0,y0 = fw-box_w-pad-bg_pad, pad+30
    elif position == "bottom-left": x0,y0 = pad, fh-box_h-pad-bg_pad
    elif position == "center":    x0,y0 = (fw-box_w)//2, (fh-box_h)//2
    else:                         x0,y0 = fw-box_w-pad-bg_pad, fh-box_h-pad-bg_pad
    x0 = max(bg_pad, min(x0, fw-box_w-bg_pad))
    y0 = max(bg_pad, min(y0, fh-box_h-bg_pad))
    ov = frame.copy()
    cv2.rectangle(ov, (max(0,x0-bg_pad),max(0,y0-bg_pad)),
                  (min(fw,x0+box_w+bg_pad),min(fh,y0+box_h+bg_pad)), (0,0,0), -1)
    cv2.addWeighted(ov, 0.55, frame, 0.45, 0, frame)
    if PIL_AVAILABLE and fnt:
        pil_img = _PIL_Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        draw = _PIL_Draw.Draw(pil_img)
        for j, ln in enumerate(lines):
            if not ln: continue
            ty = y0 + j*line_h
            if ty > fh-bg_pad: break
            draw.text((x0+inner-6, ty), ln, font=fnt, fill=(100,240,255))
        frame[:] = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    else:
        for j, ln in enumerate(lines):
            cy = y0+j*line_h+line_h
            if cy > fh-bg_pad: break
            cv2.putText(frame, ln, (x0+inner, cy),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (100,240,255), 1, cv2.LINE_AA)




def resource_path(relative: str) -> str:
    """PyInstaller EXE 환경에서 번들 리소스 경로 변환 (§4.2)."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)
