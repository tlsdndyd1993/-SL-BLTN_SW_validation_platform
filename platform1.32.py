# -*- coding: utf-8 -*-
"""
Screen & Camera Recorder  v4.4
────────────────────────────────────────────────────────────────────────────
v2.8 → v2.9 수정/추가사항

  [추가1] KernelEngine — 내장 Python 인터프리터 기반 조건부 자동화 커널
    · KernelScript : 슬롯별 파이썬 스크립트 (반복/활성화 설정)
    · KernelEngine : 스크립트 순서 실행, ROI값 읽기, TC결과 설정, OCR
    · KernelPanel  : 드래그앤드랍 슬롯 관리, 에디터, import/export .py
    · 전역 API : engine(CoreEngine), kernel(KernelEngine), log()
    · ROI 밝기 감시, 조건부 Recording/수동녹화/캡처 자동화 가능

  [추가2] ApiDocDialog — 커널 API 레퍼런스 모달
    · Control Panel 우측 상단 📖 API Doc 버튼으로 접근
    · 전체 함수 서명, 파라미터, 예시 코드 포함

  [수정1] TC 중복 검증 처리 (_apply_tc_result_to_folder)
    · 동일 결과(PASS→PASS / FAIL→FAIL) : 기존 폴더 재사용 (같은 폴더에 계속 저장)
    · 다른 결과(PASS→FAIL / FAIL→PASS) : 새 폴더 분리 (suffix 자동 증가)
    · 태그 없는 폴더 : 기존처럼 태그 삽입

  [v2.8 변경사항 유지]
    · RoiItem 데이터클래스 + RoiManagerPanel (드래그앤드랍, 팝업 편집)
    · show_tc_dialog() 공용 함수 — Recording/블랙아웃/캡처 모두 표시
    · use_custom_path_* 완전 삭제 → TC ON/OFF로 경로 자동 결정
    · TC-ID 번호형식: QButtonGroup+QRadioButton (개별|범위)
    · 범위 끝번호 ≥ 시작번호+1 강제
    · (PASS)|(FAIL) 반영 할 폴더 텍스트
    · IoChannelDB + MCP/REST/IPC 외부제어 서버
────────────────────────────────────────────────────────────────────────────
요구사항:
  pip install PyQt5 opencv-python numpy mss Pillow pynput
  (선택) pip install fastapi uvicorn   ← AI/외부제어 REST API
  (선택) pip install pytesseract       ← 커널 OCR 기능
"""
import sys, os, threading, time, queue, platform, subprocess, sqlite3, json, re
from datetime import datetime
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

# ── 서드파티 ─────────────────────────────────────────────────────────────────
import cv2
try: cv2.setLogLevel(0)  # OpenCV 로그 레벨 0=SILENT
except Exception: pass
import numpy as np
import mss

try:
    from PIL import Image as _PIL_Image, ImageDraw as _PIL_Draw, ImageFont as _PIL_Font
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QGroupBox, QCheckBox, QDoubleSpinBox,
    QScrollArea, QScrollBar, QFrame, QGridLayout, QTextEdit, QSizePolicy,
    QDialog, QDateTimeEdit, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QPlainTextEdit, QAbstractItemView, QSpinBox, QSlider,
    QTabWidget, QComboBox, QLineEdit, QSplitter, QInputDialog,
    QButtonGroup, QRadioButton, QTextBrowser,
)
from PyQt5.QtCore import (Qt, QTimer, pyqtSignal, QObject, QPoint,
                           QRect, QDateTime)
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor

try:
    from pynput import keyboard as pynput_keyboard, mouse as pynput_mouse
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False

# ── FastAPI (선택적 — AI/외부제어용) ─────────────────────────────────────────
try:
    import fastapi, uvicorn
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

# ── AI API 라이브러리 (선택적) ──────────────────────────────────────────
# Groq  : pip install groq
# Gemini: pip install google-genai
# Claude: pip install anthropic
try:
    import groq as _groq_lib
    GROQ_AVAILABLE = True
except ImportError:
    _groq_lib = None
    GROQ_AVAILABLE = False
try:
    import google.genai as _genai
    GEMINI_AVAILABLE = True
except ImportError:
    _genai = None
    GEMINI_AVAILABLE = False
try:
    import anthropic as _anthropic_lib
    CLAUDE_AVAILABLE = True
except ImportError:
    _anthropic_lib = None
    CLAUDE_AVAILABLE = False

# ── 경로 상수 ─────────────────────────────────────────────────────────────────
BASE_DIR = os.path.join(os.path.expanduser("~/Desktop"), "bltn_rec")
DB_PATH  = os.path.join(BASE_DIR, "settings.db")
# AI/외부제어 I/O 채널 DB (CoreEngine이 쓰고, 외부 LLM/MCP가 읽음)
IO_DB_PATH = os.path.join(BASE_DIR, "io_channel.db")


# =============================================================================
#  OCR 엔진 초기화 (프로그램 시작 시 1회 실행)
# =============================================================================
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

    def rect(self):
        return (self.x, self.y, self.w, self.h)

    def to_dict(self) -> dict:
        return {
            "x": self.x, "y": self.y, "w": self.w, "h": self.h,
            "name": self.name, "description": self.description,
            "source": self.source,
            "cond_value": self.cond_value,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RoiItem":
        return cls(
            x=d.get("x", 0), y=d.get("y", 0),
            w=d.get("w", 0), h=d.get("h", 0),
            name=d.get("name", ""), description=d.get("description", ""),
            source=d.get("source", "screen"),
            cond_value=d.get("cond_value", ""),
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
    with _FONT_LOCK:
        return _FONT_CACHE.get(size, None)


def _preload_fonts():
    for sz in (18, 14, 20, 24):
        fnt = _find_unicode_font(sz)
        with _FONT_LOCK:
            _FONT_CACHE[sz] = fnt


threading.Thread(target=_preload_fonts, daemon=True, name="FontPreload").start()


# =============================================================================
#  유틸리티
# =============================================================================
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


# =============================================================================
#  IO Channel DB  (AI/외부제어 소통 창구)
# =============================================================================
class IoChannelDB:
    """
    외부 LLM/MCP/자동화 툴이 이 프로그램을 제어하고 결과를 읽을 수 있는
    SQLite 기반 I/O 채널.

    테이블 구조:
      commands  : 외부 → 프로그램 (id, cmd, args_json, status, created_at)
      state     : 프로그램 → 외부  (key, value, updated_at)
      events    : 프로그램 → 외부  (id, event, data_json, ts)

    외부 제어 예시 (Python):
      import sqlite3, json
      con = sqlite3.connect("~/Desktop/bltn_rec/io_channel.db")
      con.execute("INSERT INTO commands(cmd,args_json,status) VALUES(?,?,?)",
                  ("start_recording", "{}", "pending"))
      con.commit()
    """
    def __init__(self, path: str = IO_DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        return sqlite3.connect(self._path, check_same_thread=False)

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cmd TEXT NOT NULL,
                    args_json TEXT DEFAULT '{}',
                    status TEXT DEFAULT 'pending',
                    result_json TEXT DEFAULT '{}',
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    updated_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    data_json TEXT DEFAULT '{}',
                    ts TEXT DEFAULT (datetime('now','localtime'))
                );
            """)

    # ── 상태 업데이트 (프로그램 → 외부) ─────────────────────────────────────
    def set_state(self, key: str, value: Any):
        with self._lock:
            with self._conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO state(key,value,updated_at)"
                    " VALUES(?,?,datetime('now','localtime'))",
                    (key, json.dumps(value, ensure_ascii=False)))

    def get_state(self, key: str, default=None) -> Any:
        with self._lock:
            with self._conn() as c:
                row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if row:
            try: return json.loads(row[0])
            except: return row[0]
        return default

    # ── 이벤트 발행 (프로그램 → 외부) ────────────────────────────────────────
    def emit_event(self, event: str, data: dict = None):
        with self._lock:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO events(event,data_json) VALUES(?,?)",
                    (event, json.dumps(data or {}, ensure_ascii=False)))

    # ── 명령 폴링 (외부 → 프로그램) ─────────────────────────────────────────
    def poll_commands(self) -> list:
        """pending 상태의 명령을 가져와 running 으로 변경."""
        with self._lock:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT id,cmd,args_json FROM commands WHERE status='pending'"
                    " ORDER BY id LIMIT 10").fetchall()
                if rows:
                    ids = [str(r[0]) for r in rows]
                    c.execute(
                        f"UPDATE commands SET status='running',"
                        f"updated_at=datetime('now','localtime')"
                        f" WHERE id IN ({','.join(ids)})")
        return [{"id": r[0], "cmd": r[1],
                 "args": json.loads(r[2]) if r[2] else {}} for r in rows]

    def complete_command(self, cmd_id: int, result: dict = None, ok: bool = True):
        status = "done" if ok else "error"
        with self._lock:
            with self._conn() as c:
                c.execute(
                    "UPDATE commands SET status=?,result_json=?,"
                    "updated_at=datetime('now','localtime') WHERE id=?",
                    (status, json.dumps(result or {}, ensure_ascii=False), cmd_id))

    # ── 오래된 이벤트 정리 (1000건 초과 시) ──────────────────────────────────
    def cleanup(self):
        with self._lock:
            with self._conn() as c:
                c.execute(
                    "DELETE FROM events WHERE id NOT IN"
                    " (SELECT id FROM events ORDER BY id DESC LIMIT 1000)")
                c.execute(
                    "DELETE FROM commands WHERE status IN ('done','error')"
                    " AND updated_at < datetime('now','-1 day','localtime')")


# =============================================================================
#  SettingsDB
# =============================================================================
class SettingsDB:
    def __init__(self, path: str = DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        return sqlite3.connect(self._path, check_same_thread=False)

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS memo_tabs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT, content TEXT, sort_order INTEGER DEFAULT 0,
                    font_size INTEGER DEFAULT 11, ts_enabled INTEGER DEFAULT 1);
                CREATE TABLE IF NOT EXISTS macro_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT, steps_json TEXT, sort_order INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS schedule_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_iso TEXT, stop_iso TEXT,
                    actions_json TEXT, repeat INTEGER DEFAULT 1,
                    gap REAL DEFAULT 1.0);
                CREATE TABLE IF NOT EXISTS manual_clip_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT, clip_time TEXT,
                    pre_sec REAL, post_sec REAL, clip_path TEXT);
                CREATE TABLE IF NOT EXISTS path_settings (
                    id INTEGER PRIMARY KEY,
                    vehicle_type TEXT DEFAULT '',
                    tc_id TEXT DEFAULT '',
                    extra_segments TEXT DEFAULT '[]',
                    tc_rec     INTEGER DEFAULT 0,
                    tc_manual  INTEGER DEFAULT 0,
                    tc_blackout INTEGER DEFAULT 0,
                    tc_capture INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS roi_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT DEFAULT 'screen',
                    x INTEGER DEFAULT 0, y INTEGER DEFAULT 0,
                    w INTEGER DEFAULT 0, h INTEGER DEFAULT 0,
                    name TEXT DEFAULT '', description TEXT DEFAULT '',
                    sort_order INTEGER DEFAULT 0);
            """)
            c.execute("INSERT OR IGNORE INTO path_settings(id) VALUES(1)")
            # 구버전 마이그레이션: use_custom_* 컬럼 제거는 SQLite에서 불가능하므로 무시

    # ── Key-Value 설정 ────────────────────────────────────────────────────────
    def get(self, key, default=None):
        with self._lock:
            with self._conn() as c:
                row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        with self._lock:
            with self._conn() as c:
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                          (key, str(value)))

    def get_float(self, key, default=0.0):
        v = self.get(key)
        try: return float(v) if v is not None else default
        except: return default

    def get_int(self, key, default=0):
        v = self.get(key)
        try: return int(float(v)) if v is not None else default
        except: return default

    def get_bool(self, key, default=True):
        v = self.get(key)
        if v is None: return default
        return v.lower() in ('1','true','yes')

    # ── 메모 탭 ──────────────────────────────────────────────────────────────
    def save_memo_tabs(self, tabs: list):
        with self._lock:
            with self._conn() as c:
                c.execute("DELETE FROM memo_tabs")
                c.executemany(
                    "INSERT INTO memo_tabs(title,content,sort_order,font_size,ts_enabled)"
                    " VALUES(?,?,?,?,?)",
                    [(t.get('title','메모'), t.get('content',''), i,
                      int(t.get('font_size',11)), int(bool(t.get('ts_enabled',True))))
                     for i,t in enumerate(tabs)])

    def load_memo_tabs(self) -> list:
        with self._lock:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT title,content,font_size,ts_enabled"
                    " FROM memo_tabs ORDER BY sort_order").fetchall()
        return [{'title':r[0],'content':r[1],
                 'font_size':int(r[2]) if r[2] else 11,
                 'ts_enabled':bool(r[3]) if r[3] is not None else True}
                for r in rows]

    # ── 매크로 슬롯 ──────────────────────────────────────────────────────────
    def save_macro_slots(self, slots: list):
        with self._lock:
            with self._conn() as c:
                c.execute("DELETE FROM macro_slots")
                c.executemany(
                    "INSERT INTO macro_slots(title,steps_json,sort_order) VALUES(?,?,?)",
                    [(s.get('title','슬롯'),
                      json.dumps(s.get('steps',[]), ensure_ascii=False), i)
                     for i,s in enumerate(slots)])

    def load_macro_slots(self) -> list:
        with self._lock:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT title,steps_json FROM macro_slots ORDER BY sort_order").fetchall()
        result = []
        for title, sj in rows:
            try:
                steps_raw = json.loads(sj) if sj else []
            except:
                steps_raw = []
            steps = []
            for sd in steps_raw:
                if 'kind' not in sd:
                    sd = {'kind':'click','delay':sd.get('delay',0.5),
                          'x':sd.get('x',0),'y':sd.get('y',0),
                          'button':'left','double':False,'key_str':'',
                          'x2':0,'y2':0}
                steps.append(sd)
            result.append({'title': title, 'steps': steps})
        return result

    # ── ROI 저장/복원 ─────────────────────────────────────────────────────────
    def save_roi_items(self, items: List[RoiItem]):
        with self._lock:
            with self._conn() as c:
                c.execute("DELETE FROM roi_items")
                c.executemany(
                    "INSERT INTO roi_items(source,x,y,w,h,name,description,sort_order)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    [(r.source, r.x, r.y, r.w, r.h, r.name, r.description, i)
                     for i, r in enumerate(items)])

    def load_roi_items(self) -> List[RoiItem]:
        with self._lock:
            with self._conn() as c:
                # 테이블 없으면 생성
                c.execute("""CREATE TABLE IF NOT EXISTS roi_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT DEFAULT 'screen',
                    x INTEGER DEFAULT 0, y INTEGER DEFAULT 0,
                    w INTEGER DEFAULT 0, h INTEGER DEFAULT 0,
                    name TEXT DEFAULT '', description TEXT DEFAULT '',
                    sort_order INTEGER DEFAULT 0)""")
                rows = c.execute(
                    "SELECT source,x,y,w,h,name,description"
                    " FROM roi_items ORDER BY sort_order").fetchall()
        return [RoiItem(source=r[0], x=r[1], y=r[2], w=r[3], h=r[4],
                        name=r[5], description=r[6]) for r in rows]

    # ── 경로 설정 ─────────────────────────────────────────────────────────────
    def get_path_settings(self) -> dict:
        with self._lock:
            with self._conn() as c:
                # 구버전 컬럼 추가 마이그레이션
                for col, typedef in [
                    ("extra_segments",  "TEXT DEFAULT '[]'"),
                    ("tc_rec",          "INTEGER DEFAULT 0"),
                    ("tc_manual",       "INTEGER DEFAULT 0"),
                    ("tc_blackout",     "INTEGER DEFAULT 0"),
                    ("tc_capture",      "INTEGER DEFAULT 0"),
                    ("tc_folder_idx",   "INTEGER DEFAULT 0"),
                ]:
                    try:
                        c.execute(f"ALTER TABLE path_settings ADD COLUMN {col} {typedef}")
                    except Exception:
                        pass
                row = c.execute(
                    "SELECT vehicle_type, tc_id, extra_segments,"
                    " tc_rec, tc_manual, tc_blackout, tc_capture, tc_folder_idx"
                    " FROM path_settings WHERE id=1"
                ).fetchone()
        if not row:
            return {'vehicle_type':'','tc_id':'','extra_segments':[],
                    'tc_rec':False,'tc_manual':False,'tc_blackout':False,
                    'tc_capture':False,'tc_folder_idx':0}
        try:
            extras = json.loads(row[2]) if row[2] else []
        except Exception:
            extras = []
        return {
            'vehicle_type':   row[0] or '',
            'tc_id':          row[1] or '',
            'extra_segments': extras,
            'tc_rec':         bool(row[3]),
            'tc_manual':      bool(row[4]),
            'tc_blackout':    bool(row[5]),
            'tc_capture':     bool(row[6]),
            'tc_folder_idx':  int(row[7]) if row[7] is not None else 0,
        }

    def set_path_settings(self, vehicle_type: str, tc_id: str,
                          extra_segments: list = None,
                          tc_rec: bool = False, tc_manual: bool = False,
                          tc_blackout: bool = False, tc_capture: bool = False,
                          tc_folder_idx: int = 0, root_dir: str = ''):
        if extra_segments is None: extra_segments = []
        with self._lock:
            with self._conn() as c:
                try:
                    c.execute("ALTER TABLE path_settings ADD COLUMN root_dir TEXT DEFAULT ''")
                except Exception:
                    pass
                c.execute(
                    "INSERT OR REPLACE INTO path_settings"
                    "(id, vehicle_type, tc_id, extra_segments,"
                    " tc_rec, tc_manual, tc_blackout, tc_capture, tc_folder_idx, root_dir)"
                    " VALUES(1,?,?,?, ?,?,?,?,?,?)",
                    (vehicle_type, tc_id,
                     json.dumps(extra_segments, ensure_ascii=False),
                     int(tc_rec), int(tc_manual), int(tc_blackout), int(tc_capture),
                     int(tc_folder_idx), root_dir or ''))

    # ── 수동녹화 로그 ─────────────────────────────────────────────────────────
    def log_manual_clip(self, source, pre, post, path):
        with self._lock:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO manual_clip_log(source,clip_time,pre_sec,post_sec,clip_path)"
                    " VALUES(?,?,?,?,?)",
                    (source, datetime.now().isoformat(), pre, post, path))

    # ── DB 초기화 ─────────────────────────────────────────────────────────────
    def wipe(self):
        with self._lock:
            with self._conn() as c:
                c.execute("DELETE FROM settings")
                c.execute("DELETE FROM memo_tabs")
                c.execute("DELETE FROM macro_slots")
                c.execute("DELETE FROM schedule_entries")
                c.execute("DELETE FROM roi_items")


# =============================================================================
#  데이터 모델
# =============================================================================
class MacroStep:
    __slots__ = ('kind','delay','x','y','x2','y2','button','double','key_str')

    def __init__(self, kind='click', delay=0.5, **kw):
        self.kind    = kind
        self.delay   = delay
        self.x       = kw.get('x', 0)
        self.y       = kw.get('y', 0)
        self.x2      = kw.get('x2', 0)
        self.y2      = kw.get('y2', 0)
        self.button  = kw.get('button', 'left')
        self.double  = kw.get('double', False)
        self.key_str = kw.get('key_str', '')

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}

    @classmethod
    def from_dict(cls, d: dict) -> 'MacroStep':
        kind  = d.get('kind','click')
        delay = d.get('delay', 0.5)
        kw    = {k: d[k] for k in ('x','y','x2','y2','button','double','key_str') if k in d}
        return cls(kind, delay, **kw)

    def summary(self) -> str:
        if self.kind == 'click':
            btn = self.button; dbl = " x2" if self.double else ""
            return f"[Click{dbl}] ({self.x},{self.y}) {btn}"
        elif self.kind == 'drag':
            return f"[Drag] ({self.x},{self.y})→({self.x2},{self.y2})"
        elif self.kind == 'key':
            return f"[Key] {self.key_str}"
        return f"[{self.kind}]"

ClickStep = MacroStep  # 하위 호환

class ScheduleEntry:
    _cnt = 0
    def __init__(self, start_dt, stop_dt, actions=None,
                 macro_repeat=1, macro_gap=1.0):
        ScheduleEntry._cnt += 1
        self.id = ScheduleEntry._cnt
        self.start_dt = start_dt; self.stop_dt = stop_dt
        self.actions  = actions or ['rec_start','rec_stop']
        self.macro_repeat = macro_repeat
        self.macro_gap    = macro_gap
        self.started = self.stopped = self.done = False
        self.macro_run_done = False

class MemoOverlayCfg:
    def __init__(self, tab_idx=0, position="bottom-right",
                 target="both", enabled=False,
                 overlay_font_size=18):
        self.tab_idx           = tab_idx
        self.position          = position
        self.target            = target
        self.enabled           = enabled
        self.overlay_font_size = overlay_font_size


# =============================================================================
#  Signals
# =============================================================================
class Signals(QObject):
    blackout_detected  = pyqtSignal(str, dict)
    status_message     = pyqtSignal(str)
    ac_count_changed   = pyqtSignal(int)
    rec_started        = pyqtSignal(str)
    rec_stopped        = pyqtSignal()
    macro_step_rec     = pyqtSignal(object)
    manual_clip_saved  = pyqtSignal(str)
    cameras_scanned    = pyqtSignal(list)
    monitors_scanned   = pyqtSignal(list)
    capture_saved      = pyqtSignal(str, str)
    tc_verify_request  = pyqtSignal()
    roi_list_changed   = pyqtSignal()   # ROI 목록 변경 시


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
        self._scr_fidx = 0; self._cam_fidx = 0
        self.segment_duration: float = 30 * 60
        self._seg_switching: bool = False

        # ── 설정 ────────────────────────────────────────────────────────────
        self.screen_rec_enabled     = True
        self.camera_rec_enabled     = True
        self.blackout_rec_enabled   = True
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
        self._scr_buf: deque = deque()
        self._cam_buf: deque = deque()
        self._buf_lock = threading.Lock()

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
        self.screen_bo_events: list = []; self.camera_bo_events: list = []
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
        found = []
        try:
            with mss.mss() as s:
                for i, m in enumerate(s.monitors):
                    name = ("전체 합성" if i==0 else f"Display {i}") + f"  ({m['width']}×{m['height']})"
                    found.append({"idx":i,"name":name,"w":m["width"],"h":m["height"]})
        except Exception as e:
            self.signals.status_message.emit(f"모니터 스캔 실패: {e}")
        self.monitor_list = found
        if not any(m["idx"]==self.active_monitor_idx for m in found):
            self.active_monitor_idx = found[0]["idx"] if found else 0
        self.signals.monitors_scanned.emit(found)

    # ── 카메라 스캔 ───────────────────────────────────────────────────────────
    def scan_cameras(self):
        _STANDARD_FPS = [15.0, 24.0, 25.0, 30.0, 50.0, 60.0]

        def snap_fps(fps: float) -> float:
            best = min(_STANDARD_FPS, key=lambda s: abs(s - fps))
            return best if abs(best - fps) <= 5.0 else fps

        def measure_fps_once(cap) -> float:
            try:
                frames = 0; t0 = time.perf_counter()
                while frames < 30:
                    ret, _ = cap.read()
                    if ret: frames += 1
                    if time.perf_counter() - t0 > 3.0: break
                elapsed = time.perf_counter() - t0
                return frames / elapsed if (elapsed > 0 and frames > 0) else 0.0
            except Exception:
                return 0.0

        found = []
        _sys = platform.system()
        if _sys == "Windows":
            backends = [cv2.CAP_DSHOW, cv2.CAP_ANY]
        elif _sys == "Linux":
            backends = [cv2.CAP_V4L2, cv2.CAP_ANY]
        else:
            backends = [cv2.CAP_ANY]

        # ★ OS fd 레벨 stderr 억제 (OpenCV C++ 오류는 sys.stderr 교체로 안됨)
        import os as _os, sys as _sys
        import ctypes as _ct

        def _suppress_stderr():
            """OS fd 2를 devnull로 리다이렉트 — C++ OpenCV 오류 억제."""
            try:
                _sys.stderr.flush()
                _devnull_fd = _os.open(_os.devnull, _os.O_WRONLY)
                _old_fd = _os.dup(2)
                _os.dup2(_devnull_fd, 2)
                _os.close(_devnull_fd)
                return _old_fd
            except Exception:
                return None

        def _restore_stderr(old_fd):
            """fd 2 복원."""
            try:
                if old_fd is not None:
                    _sys.stderr.flush()
                    _os.dup2(old_fd, 2)
                    _os.close(old_fd)
            except Exception:
                pass

        # ★ 전체 스캔 동안 stderr 억제 (CAP 초기화 시 DSHOW 경고 차단)
        _scan_saved = _suppress_stderr()
        try:
          for idx in range(8):
            cap = None
            for backend in backends:
                try:
                    c = cv2.VideoCapture(idx, backend)
                    if c.isOpened():
                        cap = c; break
                    c.release()
                except Exception:
                    pass
            if cap is None:
                continue

            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            except Exception:
                pass

            fps = 15.0
            try:
                reported_fps = cap.get(cv2.CAP_PROP_FPS)
                if reported_fps and 5.0 < reported_fps < 300.0:
                    fps = snap_fps(float(reported_fps))
                else:
                    samples = []
                    for _ in range(3):
                        m = measure_fps_once(cap)
                        if m > 0: samples.append(m)
                    if samples:
                        samples.sort()
                        median = samples[len(samples) // 2]
                        fps = snap_fps(median)
            except Exception:
                fps = 15.0

            nm = "Camera"
            try:
                nm = cap.getBackendName()
            except Exception:
                pass

            try:
                cap.release()
            except Exception:
                pass

            found.append({
                "idx":  idx,
                "name": f"Camera {idx} [{nm}] {fps:.0f}fps",
                "fps":  fps,
            })

        finally:
            _restore_stderr(_scan_saved)
        self.camera_list = found
        if found:
            ids = [c["idx"] for c in found]
            if self.active_cam_idx not in ids:
                self.active_cam_idx = found[0]["idx"]
                self.actual_camera_fps = found[0]["fps"]
            else:
                cam = next(c for c in found if c["idx"] == self.active_cam_idx)
                self.actual_camera_fps = cam["fps"]
        self.signals.cameras_scanned.emit(found)

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
        # ★ 수정: blackout_rec_enabled=False 이고 tc_blackout_enabled=False 이면
        #   연산 자체를 수행하지 않음 — 감지 횟수/이벤트/클립 모두 차단
        if not self.blackout_rec_enabled and not self.tc_blackout_enabled:
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
        if source=="screen": self._scr_last_bo=now; self.screen_bo_count+=1
        else:                self._cam_last_bo=now; self.camera_bo_count+=1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        ev = {'time': datetime.now().strftime("%H:%M:%S.%f")[:-3],
              'brightness_change': mc, 'timestamp': ts}
        (self.screen_bo_events if source=="screen" else self.camera_bo_events).append(ev)
        self.signals.blackout_detected.emit(source, ev)
        if self.io_channel:
            self.io_channel.emit_event("blackout_detected",
                {"source": source, **ev})
        # ★ 수정: blackout_rec_enabled=True 일 때만 클립 저장 스레드 시작
        if self.blackout_rec_enabled:
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

        # ★ 수정: 수동녹화와 동일한 snap_len 방식으로 post 수집
        #   이전 코드: pre=list(buf) 전체, post=list(buf)[len(pre):]
        #   → 버퍼 꽉 찬 경우 len(pre)=buf_max → post=[] → 저장 실패
        with self._buf_lock:
            buf = self._scr_buf if source == "screen" else self._cam_buf
            pre_all = list(buf)
            snap_len = len(buf)

        pre_clip = pre_all[-n_pre:] if len(pre_all) >= n_pre else pre_all

        post = []; deadline = time.time() + 22.0
        while len(post) < n_post and time.time() < deadline:
            time.sleep(0.04)
            with self._buf_lock:
                buf = self._scr_buf if source == "screen" else self._cam_buf
                cur_len = len(buf)
            new_frames = cur_len - snap_len
            if new_frames > 0:
                with self._buf_lock:
                    buf = self._scr_buf if source == "screen" else self._cam_buf
                    post = list(buf)[-new_frames:]
            elif cur_len >= snap_len and cur_len > 0:
                # 버퍼 꽉 찬 경우: 끝 n_post 프레임이 트리거 이후 새 프레임
                with self._buf_lock:
                    buf = self._scr_buf if source == "screen" else self._cam_buf
                    post = list(buf)[-n_post:]

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
                tag_dir = save_dir
                new_path = self._apply_tc_result_to_folder(self.tc_verify_result, tag_dir)
            else:
                dest_dir = self._resolve_tc_save_dir("blackout", self.tc_verify_result)
                new_path = dest_dir if dest_dir else save_dir
            # ★ 수정: 최종 경로 상태 메시지
            if new_path and new_path != tag_dir:
                self.signals.status_message.emit(
                    f"[블랙아웃/T/C] 최종 저장 경로 → {new_path}")

    # ── 오버레이 합성 ─────────────────────────────────────────────────────────
    def _apply_overlays(self, frame: np.ndarray, source: str) -> np.ndarray:
        h, w = frame.shape[:2]
        if self.recording and self.start_time:
            now = datetime.now()
            ns = now.strftime("%Y-%m-%d  %H:%M:%S.")+f"{now.microsecond//1000:03d}"
            e = time.time()-self.start_time
            draw_time_bar(frame, ns, f"REC  {fmt_hms(e)}.{int((e%1)*1000):03d}")
        for cfg in self.memo_overlays:
            if not cfg.enabled: continue
            if cfg.target!="both" and cfg.target!=source: continue
            if cfg.tab_idx >= len(self.memo_texts): continue
            text = self.memo_texts[cfg.tab_idx].strip()
            if text:
                draw_memo_overlay(frame, text.splitlines(), cfg.position,
                                  w, h, cfg.overlay_font_size)
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
            try:
                writer.write(frame)
            except Exception:
                pass
            return fidx + 1
        fill = min(diff, max(1, int(fps * 2)))
        for _ in range(fill):
            try:
                writer.write(frame)
            except Exception:
                break
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
                with mss.mss() as s:
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
    def _draw_rois_on_frame(self, frame: np.ndarray, rois: List[RoiItem]) -> None:
        """ROI 사각형과 이름을 프레임에 그림."""
        for i, roi in enumerate(rois):
            rx, ry, rw, rh = roi.rect()
            cv2.rectangle(frame, (rx,ry), (rx+rw,ry+rh), (255,60,60), 2)
            label = roi.name if roi.name else f"ROI{i+1}"
            cv2.putText(frame, label, (rx, max(ry-4,12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255,60,60), 1)

    # ── 스크린 루프 ───────────────────────────────────────────────────────────
    def _screen_loop(self):
        with mss.mss() as sct:
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

                # ── 블랙아웃 감지 — 활성화 시에만 밝기 계산 ★ ─────────
                if self.blackout_rec_enabled or self.tc_blackout_enabled:
                    rects = self._roi_rects("screen")
                    if rects:
                        avgs = self.calc_roi_avg(frame, rects)
                        self.screen_roi_avg = avgs
                        self.screen_overall_avg = (np.mean(avgs,axis=0)
                                                   if avgs else np.zeros(3))
                        if self.screen_roi_prev:
                            self._detect_blackout(avgs,self.screen_roi_prev,"screen")
                        self.screen_roi_prev = [a.copy() for a in avgs]

                stamped = self._apply_overlays(frame.copy(), "screen")
                if self.screen_rois:
                    self._draw_rois_on_frame(stamped, self.screen_rois)
                with self._buf_lock:
                    self._scr_buf.append(stamped)
                    while len(self._scr_buf) > self._scr_buf_max:
                        self._scr_buf.popleft()
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
    # ── 카메라 백엔드 우선순위 (Windows: DSHOW > ANY) ────────────────────────
    @staticmethod
    def _open_camera(idx: int):
        """
        백엔드 우선순위로 카메라 열기.
        MSMF(기본값)는 grab 오류(-1072873821) 유발 → DSHOW 우선 사용.
        """
        import platform as _pl
        _sys = _pl.system()
        if _sys == "Windows":
            backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
        elif _sys == "Linux":
            backends = [cv2.CAP_V4L2, cv2.CAP_ANY]
        else:
            backends = [cv2.CAP_ANY]
        for backend in backends:
            try:
                cap = cv2.VideoCapture(idx, backend)
                if cap.isOpened():
                    return cap
                cap.release()
            except Exception:
                pass
        return None

    def _camera_loop(self):
        # ★ CAP_DSHOW 우선 — MSMF grabFrame 오류(-1072873821) 방지
        cap = self._open_camera(self.active_cam_idx)
        if cap is None or not cap.isOpened():
            self.signals.status_message.emit(
                f"Camera {self.active_cam_idx} 열기 실패"); return
        rep = cap.get(cv2.CAP_PROP_FPS)
        fps = float(rep) if (rep and 0 < rep < 300) else self.actual_camera_fps
        self.actual_camera_fps = fps
        interval = 1.0 / max(fps, 1.0)
        next_t = time.perf_counter()
        _fail_cnt = 0          # 연속 grab 실패 카운터
        _MAX_FAIL = 30         # 30회 연속 실패 시 재오픈
        while not self._cam_stop.is_set():
            now = time.perf_counter()
            if next_t - now > 0:
                time.sleep(next_t - now)
            next_t += interval
            ret, frame = cap.read()
            if not ret:
                _fail_cnt += 1
                if _fail_cnt >= _MAX_FAIL:
                    # ── 카메라 재오픈 시도 (장치 일시 점유 해제 후 복구) ─
                    cap.release()
                    time.sleep(0.5)
                    cap = self._open_camera(self.active_cam_idx)
                    if cap is None or not cap.isOpened():
                        self.signals.status_message.emit(
                            f"Camera {self.active_cam_idx} 재연결 실패 — 루프 종료")
                        break
                    _fail_cnt = 0
                    # FPS 재조회
                    rep2 = cap.get(cv2.CAP_PROP_FPS)
                    if rep2 and 0 < rep2 < 300:
                        fps = rep2; interval = 1.0 / fps
                continue
            _fail_cnt = 0   # 성공 시 초기화
            self._cam_fps_ts.append(time.time())

            # ── 블랙아웃 감지 — 활성화 시에만 밝기 계산 ★ ─────────
            if self.blackout_rec_enabled or self.tc_blackout_enabled:
                rects = self._roi_rects("camera")
                if rects:
                    avgs = self.calc_roi_avg(frame, rects)
                    self.camera_roi_avg = avgs
                    self.camera_overall_avg = (np.mean(avgs,axis=0)
                                               if avgs else np.zeros(3))
                    if self.camera_roi_prev:
                        self._detect_blackout(avgs,self.camera_roi_prev,"camera")
                    self.camera_roi_prev = [a.copy() for a in avgs]

            stamped = self._apply_overlays(frame.copy(), "camera")
            if self.camera_rois:
                self._draw_rois_on_frame(stamped, self.camera_rois)
            with self._buf_lock:
                self._cam_buf.append(stamped)
                while len(self._cam_buf) > self._cam_buf_max:
                    self._cam_buf.popleft()
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
        self._scr_stop.clear()
        self._scr_thread = threading.Thread(target=self._screen_loop, daemon=True)
        self._scr_thread.start()

    def stop_screen(self): self._scr_stop.set()

    def start_camera(self):
        if self._cam_thread and self._cam_thread.is_alive(): return
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
    @staticmethod
    def _do_mouse_click(btn: str = "left", x: int = None, y: int = None):
        """
        마우스 클릭 실행 — 3단계 폴백:
          1순위: win32api (pywin32) — 관리자 권한 앱에도 전달 가능
          2순위: ctypes SendInput  — pywin32 없어도 Windows에서 동작
          3순위: pynput            — 크로스플랫폼 (일부 앱 전달 안 됨)
        """
        import platform as _pl
        # 좌표 이동
        if x is not None and y is not None:
            try:
                import ctypes
                ctypes.windll.user32.SetCursorPos(int(x), int(y))
            except Exception:
                if PYNPUT_AVAILABLE:
                    pynput_mouse.Controller().position = (x, y)

        # 버튼 매핑
        if btn == "right":
            dw_down, dw_up = 0x0008, 0x0010   # MOUSEEVENTF_RIGHTDOWN/UP
        elif btn == "middle":
            dw_down, dw_up = 0x0020, 0x0040
        else:
            dw_down, dw_up = 0x0002, 0x0004   # MOUSEEVENTF_LEFTDOWN/UP

        # 1순위: win32api
        try:
            import win32api as _wa
            _wa.mouse_event(dw_down, 0, 0, 0, 0)
            _wa.mouse_event(dw_up,   0, 0, 0, 0)
            return True
        except Exception:
            pass

        # 2순위: ctypes SendInput
        try:
            import ctypes, ctypes.wintypes as _wt
            class _MI(ctypes.Structure):
                _fields_ = [("dx",_wt.LONG),("dy",_wt.LONG),
                             ("mouseData",_wt.DWORD),("dwFlags",_wt.DWORD),
                             ("time",_wt.DWORD),("dwExtraInfo",ctypes.POINTER(ctypes.c_ulong))]
            class _INPUT(ctypes.Structure):
                class _U(ctypes.Union):
                    _fields_ = [("mi", _MI)]
                _fields_ = [("type", _wt.DWORD), ("_u", _U)]
            def _send(flags):
                inp = _INPUT(type=0)
                inp._u.mi.dwFlags = flags
                ctypes.windll.user32.SendInput(1, ctypes.byref(inp),
                                               ctypes.sizeof(_INPUT))
            _send(dw_down); _send(dw_up)
            return True
        except Exception:
            pass

        # 3순위: pynput
        if PYNPUT_AVAILABLE:
            try:
                mc = pynput_mouse.Controller()
                btn_obj = {
                    "left":   pynput_mouse.Button.left,
                    "right":  pynput_mouse.Button.right,
                    "middle": pynput_mouse.Button.middle,
                }.get(btn, pynput_mouse.Button.left)
                mc.click(btn_obj)
                return True
            except Exception:
                pass

        return False   # 모든 방법 실패

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

        while self.ac_enabled and not self._ac_stop.is_set():
            self._ac_stop.wait(self.ac_interval)
            if not self.ac_enabled or self._ac_stop.is_set():
                break
            # 더블클릭 시 연속 2회
            clicks = 2 if double else 1
            for _ in range(clicks):
                self._do_mouse_click(btn, x, y)
            self.ac_count += 1
            self.signals.ac_count_changed.emit(self.ac_count)

    def start_ac(self):
        if self.ac_enabled: return
        self.ac_enabled=True; self._ac_stop.clear()
        self._ac_thread = threading.Thread(target=self._ac_loop, daemon=True)
        self._ac_thread.start()

    def stop_ac(self):
        self.ac_enabled=False; self._ac_stop.set()

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

    def macro_start_run(self, repeat=None, gap=None, slot: int = None):
        """
        매크로 실행.
        slot: 슬롯 번호(0-based). 지정 시 해당 슬롯 로드 후 실행.
              None이면 현재 macro_steps 그대로 사용.
        """
        # ★ slot 파라미터: 커널/외부에서 슬롯 지정 실행 지원
        if slot is not None:
            if not self.macro_load_slot(slot):
                return   # 슬롯 로드 실패 시 중단
        if not PYNPUT_AVAILABLE or self.macro_running or not self.macro_steps: return
        if repeat is not None: self.macro_repeat=repeat
        if gap    is not None: self.macro_gap=gap
        self.macro_running=True; self._mac_stop.clear()
        self._mac_thread = threading.Thread(target=self._mac_loop, daemon=True)
        self._mac_thread.start()

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
            self.signals.status_message.emit(f"[Macro] {step.summary()}")

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
        지정한 슬롯 번호(0-based)의 스텝을 macro_steps에 로드.
        커널 스크립트에서 engine.macro_load_slot(1) 처럼 사용.
        반환값: 로드 성공 여부.
        """
        slots = self._macro_slots
        if not slots:
            self.signals.status_message.emit(
                "[Macro] 슬롯 데이터 없음 — 매크로 패널을 먼저 열거나 저장하세요")
            return False
        if not (0 <= slot_idx < len(slots)):
            self.signals.status_message.emit(
                f"[Macro] 슬롯 {slot_idx+1} 없음 (전체: {len(slots)}개)")
            return False
        self.macro_steps.clear()
        self.macro_steps.extend(slots[slot_idx]['steps'])
        self.signals.status_message.emit(
            f"[Macro] 슬롯 {slot_idx+1} '{slots[slot_idx]['title']}' 로드 "
            f"({len(self.macro_steps)} 스텝)")
        return True

    # ── 예약 ─────────────────────────────────────────────────────────────────
    def schedule_tick(self) -> list:
        now=datetime.now(); actions=[]
        for s in list(self.schedules):
            if s.done: continue
            if s.start_dt and not s.started:
                if -2 <= (s.start_dt-now).total_seconds() <= 1:
                    s.started=True
                    if 'rec_start' in s.actions and not self.recording:
                        actions.append(('start',s))
                    if 'macro_run' in s.actions:
                        actions.append(('macro_run',s))
            if s.stop_dt and s.started and not s.stopped:
                if -2 <= (s.stop_dt-now).total_seconds() <= 1:
                    s.stopped=s.done=True
                    if 'rec_stop' in s.actions and self.recording:
                        actions.append(('stop',s))
            if s.started and not s.stop_dt and not s.done: s.done=True
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

def _make_spinbox(min_v, max_v, val, step=1, decimals=0,
                  special=None, width=None) -> QDoubleSpinBox:
    sb = QDoubleSpinBox()
    sb.setRange(min_v, max_v); sb.setValue(val)
    sb.setSingleStep(step); sb.setDecimals(int(decimals))
    if special: sb.setSpecialValueText(special)
    if width:   sb.setFixedWidth(width)
    sb.setStyleSheet("QDoubleSpinBox{background:#1a1a3a;color:#ddd;"
                     "border:1px solid #3a4a6a;padding:2px 4px;border-radius:3px;}")
    return sb


# =============================================================================
#  ROI 이름/설명 입력 다이얼로그
# =============================================================================
class RoiEditDialog(QDialog):
    """ROI 추가/편집 시 이름과 설명을 입력하는 다이얼로그."""
    def __init__(self, name="", description="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("ROI 정보 입력")
        self.setFixedSize(380, 200)
        self.setStyleSheet("QDialog{background:#0d0d1e;}QLabel{color:#dde;}")
        lay = QVBoxLayout(self); lay.setSpacing(12); lay.setContentsMargins(20,16,20,16)

        lay.addWidget(QLabel("ROI 제목 (선택)"))
        self._name_ed = QLineEdit(name)
        self._name_ed.setPlaceholderText("예: 클러스터 경고등 영역")
        self._name_ed.setStyleSheet(
            "QLineEdit{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:3px;padding:4px 8px;font-size:12px;}")
        lay.addWidget(self._name_ed)

        lay.addWidget(QLabel("설명 (선택)"))
        self._desc_ed = QLineEdit(description)
        self._desc_ed.setPlaceholderText("예: 전방 카메라 블랙아웃 감지용")
        self._desc_ed.setStyleSheet(
            "QLineEdit{background:#1a1a3a;color:#ccd;border:1px solid #334;"
            "border-radius:3px;padding:4px 8px;font-size:11px;}")
        lay.addWidget(self._desc_ed)

        btn_row = QHBoxLayout(); btn_row.setSpacing(10)
        ok_btn = QPushButton("✅ 확인")
        ok_btn.setMinimumHeight(34)
        ok_btn.setStyleSheet(
            "QPushButton{background:#1a6a3a;color:#afffcf;font-size:12px;"
            "font-weight:bold;border-radius:5px;border:none;}"
            "QPushButton:hover{background:#27ae60;}")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("취소")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setStyleSheet(
            "QPushButton{background:#1a1a2a;color:#888;border:1px solid #334;"
            "border-radius:4px;font-size:11px;}")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn); btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

    @property
    def name(self) -> str:
        return self._name_ed.text().strip()

    @property
    def description(self) -> str:
        return self._desc_ed.text().strip()


# =============================================================================
#  PreviewLabel  (ROI를 RoiItem 기반으로 관리, 우클릭=마지막 ROI 제거)
# =============================================================================
class PreviewLabel(QLabel):
    """
    미리보기 위젯 — 휠 줌 + 패닝 + ROI 드래그 지원.

    ── 조작법 ──
    마우스 휠 위/아래  : 확대/축소 (최소 1x, 최대 10x)
    중클릭 드래그      : 화면 이동(패닝)
    좌클릭 드래그      : ROI 영역 지정
    우클릭             : 마지막 ROI 삭제
    더블클릭           : 줌 리셋 (1x, 원점)
    """
    roi_changed = pyqtSignal()

    # 줌 설정
    ZOOM_MIN  = 1.0
    ZOOM_MAX  = 10.0
    ZOOM_STEP = 0.15   # 휠 1칸당 배율 변화

    def __init__(self, source: str, engine: "CoreEngine", parent=None):
        super().__init__(parent)
        self.source = source
        self.engine = engine
        self._drawing = False
        self._pt1 = self._pt2 = QPoint()
        self._raw_size = (1, 1)
        self._active   = True

        # ── 줌/패닝 상태 ──────────────────────────────────────────────
        self._zoom        = 1.0         # 현재 배율
        self._pan_x       = 0.0         # 패닝 오프셋 (raw 픽셀 단위)
        self._pan_y       = 0.0
        self._pan_drag    = False        # 중클릭 드래그 중
        self._pan_start   = QPoint()     # 드래그 시작점 (위젯 좌표)
        self._pan_ox      = 0.0          # 드래그 시작 시 오프셋 백업
        self._pan_oy      = 0.0
        self._last_frame: np.ndarray = None   # 최신 프레임 캐시

        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAlignment(Qt.AlignCenter)
        self.setMouseTracking(True)
        self._idle()

    def _idle(self):
        self.setStyleSheet("background:#0d0d1e;border:1px solid #334;")

    def set_active(self, v: bool):
        self._active = v
        if not v:
            self.clear(); self.setText("⏸ Thread Paused")
            self.setStyleSheet("background:#0d0d1e;border:1px solid #334;"
                               "color:#555;font-size:18px;font-weight:bold;")
        else:
            self.clear(); self._idle()

    def _rois(self) -> List[RoiItem]:
        return (self.engine.screen_rois if self.source == "screen"
                else self.engine.camera_rois)

    # ── 좌표 변환 ─────────────────────────────────────────────────────
    def _widget_to_raw(self, qp: QPoint) -> tuple:
        """위젯 좌표 → 원본 프레임 좌표 (줌/패닝 반영)."""
        pw, ph = self.width(), self.height()
        rw, rh = self._raw_size
        # 줌 적용 후 가시 영역 크기
        vw = rw / self._zoom
        vh = rh / self._zoom
        # 가시 영역의 원본 프레임 내 시작점
        sx = max(0, min(self._pan_x, rw - vw))
        sy = max(0, min(self._pan_y, rh - vh))
        # 위젯에서 가시 영역 배치 (aspect-ratio 유지)
        disp_sc = min(pw / vw, ph / vh)
        ox = (pw - vw * disp_sc) / 2
        oy = (ph - vh * disp_sc) / 2
        raw_x = sx + (qp.x() - ox) / disp_sc
        raw_y = sy + (qp.y() - oy) / disp_sc
        return int(max(0, min(raw_x, rw-1))), int(max(0, min(raw_y, rh-1)))

    def _clamp_pan(self):
        rw, rh = self._raw_size
        vw = rw / self._zoom
        vh = rh / self._zoom
        self._pan_x = max(0, min(self._pan_x, rw - vw))
        self._pan_y = max(0, min(self._pan_y, rh - vh))

    def _reset_zoom(self):
        self._zoom  = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0

    # ── 마우스 이벤트 ─────────────────────────────────────────────────
    def wheelEvent(self, e):
        """휠 줌 — 커서 위치를 중심으로 확대/축소."""
        if not self._active:
            return
        delta = e.angleDelta().y()
        if delta == 0:
            return

        # 줌 전 커서의 raw 좌표
        raw_before_x, raw_before_y = self._widget_to_raw(e.pos())

        # 배율 변경
        factor = 1 + self.ZOOM_STEP * (1 if delta > 0 else -1)
        new_zoom = max(self.ZOOM_MIN, min(self.ZOOM_MAX, self._zoom * factor))
        if new_zoom == self._zoom:
            return
        self._zoom = new_zoom

        # 줌 후에도 커서 위치가 같은 raw 좌표를 가리키도록 패닝 조정
        rw, rh = self._raw_size
        vw = rw / self._zoom
        vh = rh / self._zoom
        pw, ph = self.width(), self.height()
        disp_sc = min(pw / vw, ph / vh)
        ox = (pw - vw * disp_sc) / 2
        oy = (ph - vh * disp_sc) / 2
        self._pan_x = raw_before_x - (e.pos().x() - ox) / disp_sc
        self._pan_y = raw_before_y - (e.pos().y() - oy) / disp_sc
        self._clamp_pan()

        # 줌 표시 오버레이 (잠깐)
        self._zoom_hint_frames = 10
        if self._last_frame is not None:
            self._render_frame(self._last_frame)
        e.accept()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drawing = True
            self._pt1 = self._pt2 = e.pos()
        elif e.button() == Qt.MidButton:
            # 중클릭: 패닝 시작
            self._pan_drag  = True
            self._pan_start = e.pos()
            self._pan_ox    = self._pan_x
            self._pan_oy    = self._pan_y
            self.setCursor(Qt.ClosedHandCursor)
        elif e.button() == Qt.RightButton:
            rois = self._rois()
            if rois:
                rois.pop()
                self.roi_changed.emit()

    def mouseMoveEvent(self, e):
        if self._drawing:
            self._pt2 = e.pos()
            self.update()
        elif self._pan_drag:
            rw, rh = self._raw_size
            vw = rw / self._zoom
            vh = rh / self._zoom
            pw, ph = self.width(), self.height()
            disp_sc = min(pw / vw, ph / vh)
            dx = (e.pos().x() - self._pan_start.x()) / disp_sc
            dy = (e.pos().y() - self._pan_start.y()) / disp_sc
            self._pan_x = self._pan_ox - dx
            self._pan_y = self._pan_oy - dy
            self._clamp_pan()
            if self._last_frame is not None:
                self._render_frame(self._last_frame)

    def mouseReleaseEvent(self, e):
        if self._drawing and e.button() == Qt.LeftButton:
            self._drawing = False
            x1, y1 = self._widget_to_raw(self._pt1)
            x2, y2 = self._widget_to_raw(self._pt2)
            rx, ry = min(x1,x2), min(y1,y2)
            rw, rh = abs(x1-x2), abs(y1-y2)
            if rw > 5 and rh > 5 and len(self._rois()) < 10:
                dlg = RoiEditDialog(parent=self)
                if dlg.exec_() == QDialog.Accepted:
                    roi = RoiItem(x=rx, y=ry, w=rw, h=rh,
                                  name=dlg.name, description=dlg.description,
                                  source=self.source)
                else:
                    roi = RoiItem(x=rx, y=ry, w=rw, h=rh, source=self.source)
                self._rois().append(roi)
                self.roi_changed.emit()
            self.update()
        elif e.button() == Qt.MidButton:
            self._pan_drag = False
            self.setCursor(Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, e):
        """더블클릭 → 줌 리셋."""
        if e.button() == Qt.LeftButton:
            self._reset_zoom()
            if self._last_frame is not None:
                self._render_frame(self._last_frame)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._drawing:
            p = QPainter(self)
            p.setPen(QPen(QColor(255, 80, 80), 2, Qt.DashLine))
            p.drawRect(QRect(self._pt1, self._pt2).normalized())
            p.end()

    # ── 프레임 렌더링 ─────────────────────────────────────────────────
    def _render_frame(self, frame: np.ndarray):
        """줌/패닝 적용 후 미리보기에 표시. 오버레이(ROI, 정보바) 포함."""
        rw, rh = frame.shape[1], frame.shape[0]
        self._raw_size = (rw, rh)
        self._last_frame = frame

        # ── 가시 영역 크롭 (줌 적용) ──────────────────────────────────
        vw = rw / self._zoom
        vh = rh / self._zoom
        sx = int(max(0, min(self._pan_x, rw - vw)))
        sy = int(max(0, min(self._pan_y, rh - vh)))
        ex = int(min(rw, sx + vw))
        ey = int(min(rh, sy + vh))
        cropped = frame[sy:ey, sx:ex]
        if cropped.size == 0:
            return

        rgb  = cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB)
        disp = rgb.copy()
        ch, cw = disp.shape[:2]

        # ── ROI 오버레이 (크롭 좌표로 변환) ───────────────────────────
        def _draw_label(img, text, x, y, color=(255,255,255),
                        scale=0.40, thickness=1, bg_alpha=0.45):
            font = cv2.FONT_HERSHEY_SIMPLEX
            (tw, th), bl = cv2.getTextSize(text, font, scale, thickness)
            px, py = 3, 2
            x1 = max(x, 0); y1 = max(y - th - py, 0)
            x2 = min(x + tw + px*2, img.shape[1]-1)
            y2 = min(y + bl + py,   img.shape[0]-1)
            sub = img[y1:y2, x1:x2]
            if sub.size > 0:
                img[y1:y2, x1:x2] = (sub * (1-bg_alpha)).astype(sub.dtype)
            cv2.putText(img, text, (x, y), font,
                        scale, color, thickness, cv2.LINE_AA)
            return tw

        INFO_SCALE = 0.40
        LABEL_H    = 14

        for i, roi in enumerate(self._rois()):
            # raw 좌표 → 크롭 좌표
            rx  = roi.x - sx;  ry  = roi.y - sy
            rx2 = rx + roi.w;  ry2 = ry + roi.h
            # 완전히 벗어난 ROI 스킵
            if rx2 < 0 or ry2 < 0 or rx > cw or ry > ch:
                continue
            # 클리핑
            drx  = max(rx, 0);  dry  = max(ry, 0)
            drx2 = min(rx2, cw); dry2 = min(ry2, ch)
            cv2.rectangle(disp, (drx, dry), (drx2, dry2), (255, 60, 60), 2)

            above_y = ry - 4
            if above_y < LABEL_H:
                above_y = ry2 + LABEL_H

            name_lbl = roi.name if roi.name else f"ROI{i+1}"
            cur_x = drx
            tw = _draw_label(disp, name_lbl, cur_x, above_y,
                             color=(255, 80, 80), scale=INFO_SCALE)
            cur_x += tw + 8

            if getattr(self.engine, 'brightness_enabled', True):
                bright_str = f"L:{roi.last_brightness:.0f}"
                tw = _draw_label(disp, bright_str, cur_x, above_y,
                                 color=(255, 200, 60), scale=INFO_SCALE)
                cur_x += tw + 8

            if getattr(self.engine, 'ocr_enabled', True):
                ocr_txt = (roi.last_text or "").strip()
                if ocr_txt.startswith("[ERR:"):
                    ocr_show, ocr_color = "OCR:ERR", (255, 80, 80)
                elif ocr_txt and not ocr_txt.startswith("~"):
                    ocr_show, ocr_color = f"OCR:{ocr_txt[:14]}", (60, 230, 255)
                else:
                    ocr_show, ocr_color = "OCR:—", (100, 100, 120)
                _draw_label(disp, ocr_show, cur_x, above_y,
                            color=ocr_color, scale=INFO_SCALE)

        # ── 줌 표시 (우상단) ──────────────────────────────────────────
        if self._zoom > 1.01:
            zoom_txt = f"x{self._zoom:.1f}"
            font  = cv2.FONT_HERSHEY_SIMPLEX
            (ztw, zth), _ = cv2.getTextSize(zoom_txt, font, 0.55, 1)
            zx = disp.shape[1] - ztw - 8
            zy = zth + 6
            sub = disp[max(0,zy-zth-3):zy+4, max(0,zx-3):zx+ztw+5]
            if sub.size > 0:
                disp[max(0,zy-zth-3):zy+4, max(0,zx-3):zx+ztw+5] = \
                    (sub * 0.35).astype(sub.dtype)
            cv2.putText(disp, zoom_txt, (zx, zy),
                        font, 0.55, (255, 220, 60), 1, cv2.LINE_AA)

        # ── QLabel에 표시 ─────────────────────────────────────────────
        h, w, _ = disp.shape
        qi  = QImage(disp.data, w, h, 3*w, QImage.Format_RGB888)
        pix = QPixmap.fromImage(qi).scaled(
            self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.setPixmap(pix)

    def update_frame(self, frame: np.ndarray):
        if not self._active:
            return
        # ── 밝기 캐시 업데이트 (brightness_enabled 시에만) ───────────
        if getattr(self.engine, 'brightness_enabled', True):
            for roi in self._rois():
                rx, ry, rw, rh = roi.rect()
                region = frame[ry:ry+rh, rx:rx+rw]
                if region.size > 0:
                    avg = region.mean(axis=0).mean(axis=0)
                    b, g, r_ = float(avg[0]), float(avg[1]), float(avg[2])
                    roi.last_brightness = round(0.114*b + 0.587*g + 0.299*r_, 2)
                    roi.last_avg_bgr    = (round(b,1), round(g,1), round(r_,1))
        self._render_frame(frame)

# =============================================================================
#  RoiManagerPanel  — ROI 목록 관리 (이름/설명/드래그앤드랍 순서 변경)
# =============================================================================
class RoiManagerPanel(QWidget):
    """
    ROI 항목을 목록으로 표시하고 드래그앤드랍으로 순서를 변경.
    순서 변경 시 engine.screen_rois / camera_rois 도 동일하게 반영.
    """
    changed = pyqtSignal()   # ROI 추가/삭제/순서변경 시 emit

    _ROW_H  = 46
    _BG     = "#0a0a18"
    _BG_HV  = "#12122a"
    _BG_DG  = "#1a2a4a"

    def __init__(self, engine: "CoreEngine", signals: "Signals", parent=None):
        super().__init__(parent)
        self.engine  = engine
        self.signals = signals
        self._drag_idx   = -1
        self._drag_start = QPoint()
        self._dragging   = False
        self._source_tab = "screen"   # 현재 표시 소스
        self._build()
        signals.roi_list_changed.connect(self._refresh)

    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,4); v.setSpacing(6)

        # ── OCR / 밝기 ON/OFF 제어 바 ★ v2.9.3 ─────────────────────────
        ctrl_row = QHBoxLayout(); ctrl_row.setSpacing(6)

        _btn_on_ss = (
            "QPushButton{{background:{bg};color:{fg};"
            "border:1px solid {bd};border-radius:4px;"
            "font-size:10px;font-weight:bold;padding:2px 8px;}}"
            "QPushButton:hover{{filter:brightness(1.2);}}")

        # ★ v4.5: Display OCR / Camera OCR 독립 버튼
        _ocr_ss_on  = ("QPushButton{background:#0a3a4a;color:#3de;"
                        "border:1px solid #1a6a7a;border-radius:4px;"
                        "font-size:10px;font-weight:bold;}"
                        "QPushButton:!checked{background:#0a0a18;color:#334;"
                        "border-color:#1a1a2a;}"
                        "QPushButton:hover{background:#0d4a5a;}")

        self._ocr_scr_btn = QPushButton("🖥 OCR ▶ ON")
        self._ocr_scr_btn.setCheckable(True)
        self._ocr_scr_btn.setChecked(True)
        self._ocr_scr_btn.setFixedHeight(26)
        self._ocr_scr_btn.setToolTip("Display(화면) ROI OCR ON/OFF")
        self._ocr_scr_btn.setStyleSheet(_ocr_ss_on)
        self._ocr_scr_btn.toggled.connect(self._on_ocr_screen_toggle)

        self._ocr_cam_btn = QPushButton("📷 OCR ▶ ON")
        self._ocr_cam_btn.setCheckable(True)
        self._ocr_cam_btn.setChecked(True)
        self._ocr_cam_btn.setFixedHeight(26)
        self._ocr_cam_btn.setToolTip("Camera ROI OCR ON/OFF")
        self._ocr_cam_btn.setStyleSheet(_ocr_ss_on)
        self._ocr_cam_btn.toggled.connect(self._on_ocr_camera_toggle)

        # 하위 호환: 단일 버튼 참조 (저장/복원 코드 호환)
        self._ocr_btn = self._ocr_scr_btn

        self._bright_btn = QPushButton("💡 밝기 ▶ ON")
        self._bright_btn.setCheckable(True)
        self._bright_btn.setChecked(True)
        self._bright_btn.setFixedHeight(26)
        self._bright_btn.setStyleSheet(
            "QPushButton{background:#3a3a0a;color:#ff0;"
            "border:1px solid #6a6a1a;border-radius:4px;"
            "font-size:10px;font-weight:bold;}"
            "QPushButton:!checked{background:#0a0a18;color:#334;"
            "border-color:#1a1a2a;}"
            "QPushButton:hover{background:#4a4a0d;}")
        self._bright_btn.toggled.connect(self._on_bright_toggle)

        ctrl_row.addWidget(self._ocr_scr_btn, 1)
        ctrl_row.addWidget(self._ocr_cam_btn, 1)
        ctrl_row.addWidget(self._bright_btn, 1)
        v.addLayout(ctrl_row)

        # 소스 탭 선택
        tab_row = QHBoxLayout(); tab_row.setSpacing(6)
        self._scr_tab = QPushButton("🖥 Display ROI")
        self._scr_tab.setCheckable(True); self._scr_tab.setChecked(True)
        self._cam_tab = QPushButton("📷 Camera ROI")
        self._cam_tab.setCheckable(True)
        _tab_style = (
            "QPushButton{background:#1a1a3a;color:#888;border:1px solid #334;"
            "border-radius:4px;font-size:11px;padding:4px 10px;}"
            "QPushButton:checked{background:#1a2a4a;color:#7bc8e0;"
            "border-color:#2a6aaa;font-weight:bold;}"
            "QPushButton:hover{background:#22224a;}")
        self._scr_tab.setStyleSheet(_tab_style)
        self._cam_tab.setStyleSheet(_tab_style)
        self._scr_tab.clicked.connect(lambda: self._set_source("screen"))
        self._cam_tab.clicked.connect(lambda: self._set_source("camera"))
        tab_row.addWidget(self._scr_tab); tab_row.addWidget(self._cam_tab)
        tab_row.addStretch()
        v.addLayout(tab_row)

        # 힌트
        hint = QLabel("⠿ 드래그: 순서변경  |  ✕: 삭제  |  ✏: 편집")
        hint.setStyleSheet("color:#446;font-size:9px;")
        v.addWidget(hint)

        # 목록 스크롤 영역
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea{border:1px solid #1a2a3a;background:#08081a;}"
            "QScrollBar:vertical{background:#0d0d1e;width:6px;border-radius:3px;}"
            "QScrollBar::handle:vertical{background:#336;border-radius:3px;min-height:16px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        self._list_w = QWidget()
        self._list_w.setStyleSheet(f"background:{self._BG};")
        self._list_lay = QVBoxLayout(self._list_w)
        self._list_lay.setContentsMargins(4,4,4,4); self._list_lay.setSpacing(3)
        scroll.setWidget(self._list_w)
        scroll.setFixedHeight(160)
        v.addWidget(scroll)

        # 통계 레이블
        self._stat_lbl = QLabel("ROI 없음")
        self._stat_lbl.setStyleSheet("color:#556;font-size:10px;")
        v.addWidget(self._stat_lbl)

        # 전체 삭제
        clr_btn = QPushButton("🗑 현재 소스 ROI 전체 삭제")
        clr_btn.setFixedHeight(26)
        clr_btn.setStyleSheet(
            "QPushButton{background:#2a1a1a;color:#f88;border:1px solid #5a2a2a;"
            "border-radius:4px;font-size:10px;}"
            "QPushButton:hover{background:#3a1a1a;}")
        clr_btn.clicked.connect(self._clear_all)
        v.addWidget(clr_btn)

        self._refresh()

    def _set_source(self, source: str):
        self._source_tab = source
        self._scr_tab.setChecked(source == "screen")
        self._cam_tab.setChecked(source == "camera")
        self._refresh()

    def _current_rois(self) -> List[RoiItem]:
        return (self.engine.screen_rois if self._source_tab == "screen"
                else self.engine.camera_rois)

    def _refresh(self):
        # 목록 재빌드
        while self._list_lay.count():
            it = self._list_lay.takeAt(0)
            if it.widget(): it.widget().setParent(None)

        rois = self._current_rois()
        for i, roi in enumerate(rois):
            row = self._make_row(i, roi)
            self._list_lay.addWidget(row)
        self._list_lay.addStretch()

        total_scr = len(self.engine.screen_rois)
        total_cam = len(self.engine.camera_rois)
        self._stat_lbl.setText(
            f"Display: {total_scr}개  |  Camera: {total_cam}개")

    _ROW_H  = 70   # 조건 입력 줄 추가로 높이 확장

    def _make_row(self, idx: int, roi: RoiItem) -> QFrame:
        row = QFrame()
        row.setFixedHeight(self._ROW_H)
        row.setObjectName(f"roi_row_{idx}")
        # ★ f-string 제거: Qt가 동적 selector를 파싱 못해 WARN 발생
        row.setStyleSheet(
            "QFrame{background:#0d0d1e;border:1px solid #1a1a3a;"
            "border-radius:4px;}"
            "QFrame:hover{background:#12122e;}")
        row.setCursor(Qt.OpenHandCursor)

        outer = QVBoxLayout(row)
        outer.setContentsMargins(6,3,6,3); outer.setSpacing(2)

        # ── 상단 행: 핸들 + 번호 + 이름/위치 + 실시간값 + 버튼 ──────────
        top = QHBoxLayout(); top.setSpacing(5); top.setContentsMargins(0,0,0,0)

        grip = QLabel("⠿"); grip.setFixedWidth(14)
        grip.setStyleSheet("color:#4a5a8a;font-size:17px;background:transparent;")

        num = QLabel(f"{idx+1}"); num.setFixedWidth(16)
        num.setStyleSheet("color:#556;font-size:10px;background:transparent;")
        num.setAlignment(Qt.AlignCenter)

        name_lbl = QLabel(roi.label())
        name_lbl.setStyleSheet(
            "color:#7bc8e0;font-size:11px;font-weight:bold;background:transparent;")
        pos_lbl = QLabel(f"({roi.x},{roi.y}) {roi.w}×{roi.h}")
        pos_lbl.setStyleSheet("color:#556;font-size:9px;background:transparent;")
        info_col = QVBoxLayout(); info_col.setSpacing(0)
        info_col.addWidget(name_lbl); info_col.addWidget(pos_lbl)

        # ── 실시간 OCR 값 표시 레이블 ★ ────────────────────────────────
        row._ocr_lbl = QLabel("OCR: —")
        row._ocr_lbl.setFixedWidth(110)
        row._ocr_lbl.setStyleSheet(
            "color:#3de;font-size:10px;font-weight:bold;"
            "background:#080818;border:1px solid #1a3a4a;"
            "border-radius:3px;padding:1px 4px;")
        row._ocr_lbl.setAlignment(Qt.AlignCenter)
        row._roi_idx = idx   # 나중에 타이머 업데이트용

        edit_btn = QPushButton("✏"); edit_btn.setFixedSize(22,22)
        edit_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;border:none;"
            "border-radius:3px;font-size:11px;}"
            "QPushButton:hover{background:#223344;}")
        edit_btn.clicked.connect(lambda _, i=idx: self._edit_roi(i))

        del_btn = QPushButton("✕"); del_btn.setFixedSize(22,22)
        del_btn.setStyleSheet(
            "QPushButton{background:#3a1a1a;color:#f88;border:none;"
            "border-radius:3px;font-size:12px;}"
            "QPushButton:hover{background:#7f2020;}")
        del_btn.clicked.connect(lambda _, i=idx: self._del_roi(i))

        top.addWidget(grip); top.addWidget(num)
        top.addLayout(info_col, 1)
        top.addWidget(row._ocr_lbl)
        top.addWidget(edit_btn); top.addWidget(del_btn)
        outer.addLayout(top)

        # ── 하단 행: 조건 입력 UI ★ ─────────────────────────────────────
        # "OCR값이 [  입력  ] 일 때 → 변수: roi_{idx}_match = True"
        bot = QHBoxLayout(); bot.setSpacing(4); bot.setContentsMargins(30,0,4,0)

        cond_lbl = QLabel("OCR =")
        cond_lbl.setStyleSheet("color:#667;font-size:9px;background:transparent;")
        bot.addWidget(cond_lbl)

        row._cond_ed = QLineEdit(roi.cond_value)
        row._cond_ed.setPlaceholderText("비교값 (예: 2  또는  OK)")
        row._cond_ed.setFixedHeight(18)
        row._cond_ed.setStyleSheet(
            "QLineEdit{background:#08080e;color:#f0c040;"
            "border:1px solid #3a3a20;border-radius:2px;"
            "font-size:10px;padding:0 4px;}")
        row._cond_ed.textChanged.connect(
            lambda t, i=idx: self._on_cond_changed(i, t))
        bot.addWidget(row._cond_ed, 1)

        # 매치 결과 표시
        row._match_lbl = QLabel("—")
        row._match_lbl.setFixedWidth(50)
        row._match_lbl.setStyleSheet(
            "color:#556;font-size:9px;background:transparent;")
        row._match_lbl.setAlignment(Qt.AlignCenter)
        bot.addWidget(row._match_lbl)

        outer.addLayout(bot)

        # 마우스 이벤트 (드래그앤드랍)
        row.mousePressEvent   = lambda e, i=idx: self._on_press(e, i)
        row.mouseMoveEvent    = lambda e: self._on_move(e)
        row.mouseReleaseEvent = lambda e: self._on_release(e)

        # 행 위젯 참조 저장 (타이머 업데이트용)
        if not hasattr(self, '_row_widgets'):
            self._row_widgets = {}
        self._row_widgets[idx] = row

        return row

    # ── 드래그앤드랍 ─────────────────────────────────────────────────────────
    def _on_press(self, e, idx):
        if e.button() == Qt.LeftButton:
            self._drag_idx   = idx
            self._drag_start = e.globalPos()

    def _on_move(self, e):
        if self._drag_idx >= 0 and not self._dragging:
            if (e.globalPos()-self._drag_start).manhattanLength() > 6:
                self._dragging = True

    def _on_release(self, e):
        if self._dragging and self._drag_idx >= 0:
            y_rel  = self._list_w.mapFromGlobal(e.globalPos()).y()
            tgt    = max(0, min(y_rel // self._ROW_H,
                                len(self._current_rois())-1))
            rois   = self._current_rois()
            if tgt != self._drag_idx and 0 <= tgt < len(rois):
                rois.insert(tgt, rois.pop(self._drag_idx))
                self.signals.roi_list_changed.emit()
                self.changed.emit()
        self._drag_idx  = -1
        self._dragging  = False

    # ── OCR 조건값 변경 ───────────────────────────────────────────────────
    def _on_ocr_toggle(self, on: bool):
        """하위 호환용 — Display OCR 토글과 동일."""
        self._on_ocr_screen_toggle(on)

    def _on_ocr_screen_toggle(self, on: bool):
        """★ v4.5: Display(화면) ROI OCR 독립 ON/OFF."""
        self.engine.ocr_screen_enabled = on
        self.engine.ocr_enabled = on  # 하위 호환 유지
        self._ocr_scr_btn.setText("🖥 OCR ▶ ON" if on else "🖥 OCR ⏸ OFF")
        self._ocr_scr_btn.setToolTip(
            "Display OCR 활성화 중" if on
            else "Display OCR 비활성화 — 연산 없음 (자원 절약)")

    def _on_ocr_camera_toggle(self, on: bool):
        """★ v4.5: Camera ROI OCR 독립 ON/OFF."""
        self.engine.ocr_camera_enabled = on
        self._ocr_cam_btn.setText("📷 OCR ▶ ON" if on else "📷 OCR ⏸ OFF")
        self._ocr_cam_btn.setToolTip(
            "Camera OCR 활성화 중" if on
            else "Camera OCR 비활성화 — 연산 없음 (자원 절약)")

    def _on_bright_toggle(self, on: bool):
        """밝기 ON/OFF — engine.brightness_enabled 플래그 + 버튼 텍스트 갱신."""
        self.engine.brightness_enabled = on
        self._bright_btn.setText("💡 밝기 ▶ ON" if on else "💡 밝기 ⏸ OFF")
        self._bright_btn.setToolTip(
            "밝기 계산 활성화 — ROI 평균 밝기 측정 중" if on
            else "밝기 계산 비활성화 — 밝기 연산 없음 (자원 절약)\n"
                 "※ 블랙아웃 기능은 별도 ON/OFF로 동작합니다")
        # 밝기 OFF 시 last_brightness 초기화 (오래된 값 표시 방지)
        if not on:
            for roi in self.engine.screen_rois + self.engine.camera_rois:
                roi.last_brightness = 0.0
                roi.last_avg_bgr    = (0, 0, 0)

    def _on_cond_changed(self, idx: int, value: str):
        """사용자가 조건 입력란을 수정하면 RoiItem.cond_value 업데이트."""
        rois = self._current_rois()
        if 0 <= idx < len(rois):
            rois[idx].cond_value = value.strip()

    # ── OCR 레이블 실시간 갱신 (QTimer 30fps) ────────────────────────────
    def start_ocr_refresh(self):
        """미리보기 업데이트와 연동해서 OCR 레이블을 주기적으로 갱신."""
        if hasattr(self, '_ocr_timer') and self._ocr_timer.isActive():
            return
        self._ocr_timer = QTimer()
        self._ocr_timer.setInterval(200)   # 200ms(5fps) — OCR 갱신은 느려도 충분
        self._ocr_timer.timeout.connect(self.refresh_ocr_labels)
        self._ocr_timer.start()

    def refresh_ocr_labels(self):
        """
        각 ROI 행의 OCR 레이블과 매치 결과를 최신 캐시로 업데이트.
        부하 없음 — RoiItem.last_text 캐시만 읽음.
        """
        rois = self._current_rois()
        rows = getattr(self, '_row_widgets', {})
        for idx, roi in enumerate(rois):
            row = rows.get(idx)
            if row is None:
                continue

            # OCR 값 레이블 갱신
            ocr_lbl = getattr(row, '_ocr_lbl', None)
            if ocr_lbl:
                txt = roi.last_text.strip() if roi.last_text else ""
                if txt and not txt.startswith("~"):
                    ocr_lbl.setText(f"OCR: {txt[:14]}")
                    ocr_lbl.setStyleSheet(
                        "color:#3de;font-size:10px;font-weight:bold;"
                        "background:#080818;border:1px solid #1a5a6a;"
                        "border-radius:3px;padding:1px 4px;")
                else:
                    ocr_lbl.setText("OCR: —")
                    ocr_lbl.setStyleSheet(
                        "color:#446;font-size:10px;font-weight:bold;"
                        "background:#080818;border:1px solid #1a2a3a;"
                        "border-radius:3px;padding:1px 4px;")

            # 조건 매치 결과 표시
            match_lbl = getattr(row, '_match_lbl', None)
            cond_ed   = getattr(row, '_cond_ed',   None)
            if match_lbl and cond_ed:
                cond = roi.cond_value.strip()
                txt  = roi.last_text.strip() if roi.last_text else ""
                if not cond:
                    match_lbl.setText("—")
                    match_lbl.setStyleSheet("color:#446;font-size:9px;")
                else:
                    matched = self._eval_cond(txt, cond)
                    if matched:
                        match_lbl.setText("✅ MATCH")
                        match_lbl.setStyleSheet(
                            "color:#2ecc71;font-size:9px;font-weight:bold;")
                    else:
                        match_lbl.setText("✗ NO")
                        match_lbl.setStyleSheet(
                            "color:#e74c3c;font-size:9px;font-weight:bold;")
                    # RoiItem에 결과 캐시
                    roi.last_match = matched

    @staticmethod
    def _eval_cond(ocr_text: str, cond: str) -> bool:
        """
        OCR 텍스트와 조건값을 비교.
        - 숫자면 수치 비교 (==, !=, >, <, >=, <= 지원)
        - 문자면 대소문자 무시 포함 비교
        """
        import re as _re
        ocr_clean = ocr_text.strip()
        cond_clean = cond.strip()
        if not cond_clean:
            return False

        # 연산자 포함 조건 (예: ">=2", "<100", "!=OK")
        m = _re.match(r'^(==|!=|>=|<=|>|<)\s*(.+)$', cond_clean)
        if m:
            op, val = m.group(1), m.group(2).strip()
            try:
                lhs = float(ocr_clean); rhs = float(val)
                return eval(f"{lhs}{op}{rhs}")
            except ValueError:
                # 문자 비교
                lhs, rhs = ocr_clean.lower(), val.lower()
                if   op == "==": return lhs == rhs
                elif op == "!=": return lhs != rhs
                else:            return False

        # 단순 비교: 숫자 또는 문자열
        try:
            return float(ocr_clean) == float(cond_clean)
        except ValueError:
            return ocr_clean.lower() == cond_clean.lower()

    # ── ROI 편집 ─────────────────────────────────────────────────────────────
    def _edit_roi(self, idx: int):
        rois = self._current_rois()
        if not 0 <= idx < len(rois): return
        roi = rois[idx]
        dlg = RoiEditDialog(name=roi.name, description=roi.description, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            roi.name        = dlg.name
            roi.description = dlg.description
            self.signals.roi_list_changed.emit()
            self.changed.emit()

    def _del_roi(self, idx: int):
        rois = self._current_rois()
        if 0 <= idx < len(rois):
            rois.pop(idx)
            self.signals.roi_list_changed.emit()
            self.changed.emit()

    def _clear_all(self):
        self._current_rois().clear()
        self.signals.roi_list_changed.emit()
        self.changed.emit()


# =============================================================================
#  TimestampMemoEdit
# =============================================================================
class TimestampMemoEdit(QPlainTextEdit):
    _TS_RE = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.timestamp_enabled = True

    @classmethod
    def _pat(cls):
        if cls._TS_RE is None:
            cls._TS_RE = re.compile(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ')
        return cls._TS_RE

    def contextMenuEvent(self, e): e.accept()

    def mousePressEvent(self, e):
        super().mousePressEvent(e)
        if e.button() == Qt.RightButton and self.timestamp_enabled:
            cur = self.textCursor()
            cur.movePosition(QTextCursor.StartOfBlock)
            cur.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            line = cur.selectedText()
            m = self._pat().match(line)
            if m:
                sc = self.textCursor()
                sc.movePosition(QTextCursor.StartOfBlock)
                sc.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, m.end())
                sc.removeSelectedText()
                self.setTextCursor(sc)

    def mouseDoubleClickEvent(self, e):
        super().mouseDoubleClickEvent(e)
        if not self.timestamp_enabled: return
        if e.button() == Qt.LeftButton:
            cur = self.textCursor()
            cur.movePosition(QTextCursor.StartOfBlock)
            cur.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            line = cur.selectedText()
            ts_now = datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
            m = self._pat().match(line)
            sc = self.textCursor()
            sc.movePosition(QTextCursor.StartOfBlock)
            if m:
                sc.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, m.end())
            sc.insertText(ts_now)
            self.setTextCursor(sc)


# =============================================================================
#  CollapsibleSection
# =============================================================================
class CollapsibleSection(QWidget):
    def __init__(self, title: str, color: str="#3a7bd5", parent=None):
        super().__init__(parent)
        self._collapsed=False
        outer=QVBoxLayout(self); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)
        self._btn=QPushButton(); self._btn.setCheckable(True); self._btn.setChecked(False)
        self._btn.setFixedHeight(34); self._btn.clicked.connect(self._toggle)
        self._title=title; self._color=color; self._style(False)
        outer.addWidget(self._btn)
        self._content=QWidget()
        self._content.setStyleSheet(
            "QWidget{background:#10102a;border:1px solid #2a2a4a;"
            "border-top:none;border-radius:0 0 6px 6px;}")
        cl=QVBoxLayout(self._content); cl.setContentsMargins(8,8,8,10); cl.setSpacing(6)
        self._cl=cl; outer.addWidget(self._content)

    def _style(self, collapsed):
        arrow="▶" if collapsed else "▼"
        self._btn.setText(f"  {arrow}  {self._title}")
        bot="1px solid #2a2a4a" if collapsed else "none"
        rad="6px" if collapsed else "6px 6px 0 0"
        self._btn.setStyleSheet(f"""
            QPushButton{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #1e2240,stop:1 #12122a);
                color:#7ab4d4;font-size:12px;font-weight:bold;text-align:left;
                padding:0 12px;border-left:3px solid {self._color};
                border-top:1px solid #2a2a4a;border-right:1px solid #2a2a4a;
                border-bottom:{bot};border-radius:{rad};}}
            QPushButton:hover{{background:#22224a;color:#9ad4f4;}}""")

    def _toggle(self):
        self._collapsed=not self._collapsed
        self._content.setVisible(not self._collapsed); self._style(self._collapsed)

    def add_widget(self, w): self._cl.addWidget(w)
    def set_collapsed(self, v):
        if v!=self._collapsed: self._toggle()
    def is_collapsed(self): return self._collapsed


# =============================================================================
#  MemoOverlayRow
# =============================================================================
class MemoOverlayRow(QWidget):
    changed = pyqtSignal()
    removed = pyqtSignal(object)
    _POSITIONS = ["top-left","top-right","bottom-left","bottom-right","center"]
    _TARGETS   = ["both","screen","camera"]
    _POS_KR    = ["좌상","우상","좌하","우하","중앙"]
    _TGT_KR    = ["Both","Display","Camera"]

    def __init__(self, cfg: MemoOverlayCfg, tab_count: int, parent=None):
        super().__init__(parent); self.cfg=cfg
        lay=QHBoxLayout(self); lay.setContentsMargins(2,2,2,2); lay.setSpacing(4)

        self._en=QCheckBox("ON"); self._en.setChecked(cfg.enabled)
        self._en.setStyleSheet("QCheckBox{font-size:10px;font-weight:bold;color:#2ecc71;}")
        self._en.toggled.connect(self._upd); lay.addWidget(self._en)

        lay.addWidget(QLabel("탭:"))
        self._tab=QSpinBox(); self._tab.setRange(1,max(tab_count,1))
        self._tab.setValue(cfg.tab_idx+1); self._tab.setFixedWidth(44)
        self._tab.valueChanged.connect(self._upd); lay.addWidget(self._tab)

        lay.addWidget(QLabel("위치:"))
        self._pos=QComboBox()
        for p in self._POS_KR: self._pos.addItem(p)
        if cfg.position in self._POSITIONS:
            self._pos.setCurrentIndex(self._POSITIONS.index(cfg.position))
        self._pos.currentIndexChanged.connect(self._upd)
        self._pos.setFixedWidth(58); lay.addWidget(self._pos)

        lay.addWidget(QLabel("대상:"))
        self._tgt=QComboBox()
        for t in self._TGT_KR: self._tgt.addItem(t)
        if cfg.target in self._TARGETS:
            self._tgt.setCurrentIndex(self._TARGETS.index(cfg.target))
        self._tgt.currentIndexChanged.connect(self._upd)
        self._tgt.setFixedWidth(66); lay.addWidget(self._tgt)

        lay.addWidget(QLabel("크기:"))
        self._fsz=QSpinBox(); self._fsz.setRange(8,72)
        self._fsz.setValue(getattr(cfg,'overlay_font_size',18))
        self._fsz.setFixedWidth(50)
        self._fsz.setStyleSheet(
            "QSpinBox{background:#1a1a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:3px;font-size:11px;}")
        self._fsz.valueChanged.connect(self._upd); lay.addWidget(self._fsz)

        rm=QPushButton("✕"); rm.setFixedSize(22,22)
        rm.setStyleSheet("QPushButton{background:#3a1a1a;color:#f88;border:none;"
                         "border-radius:3px;}QPushButton:hover{background:#7f2020;}")
        rm.clicked.connect(lambda: self.removed.emit(self)); lay.addWidget(rm)

    def _upd(self):
        self.cfg.enabled           = self._en.isChecked()
        self.cfg.tab_idx           = self._tab.value()-1
        self.cfg.position          = self._POSITIONS[self._pos.currentIndex()]
        self.cfg.target            = self._TARGETS[self._tgt.currentIndex()]
        self.cfg.overlay_font_size = self._fsz.value()
        self.changed.emit()

    def update_tab_max(self, n): self._tab.setMaximum(max(n,1))

def show_tc_dialog(parent, prompt: str = "T/C 검증 결과를 선택하세요.",
                   cancel_label: str = "취소") -> Optional[str]:
    """
    PASS/FAIL 선택 모달 다이얼로그.
    반환: "PASS" | "FAIL" | None(취소)
    모든 패널에서 공통으로 사용.
    """
    result_box = [None]
    dlg = QDialog(parent)
    dlg.setWindowTitle("T/C 검증 결과 입력")
    dlg.setWindowModality(Qt.ApplicationModal)
    dlg.setWindowFlags(
        Qt.Dialog | Qt.WindowTitleHint |
        Qt.WindowCloseButtonHint | Qt.MSWindowsFixedSizeDialogHint)
    dlg.setFixedSize(420, 220)
    dlg.setStyleSheet(
        "QDialog{background:#0d0d1e;}"
        "QLabel{color:#dde;}"
        "QPushButton{border-radius:6px;border:none;}")
    lay = QVBoxLayout(dlg)
    lay.setSpacing(20); lay.setContentsMargins(28, 24, 28, 20)
    lbl = QLabel(prompt)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet("font-size:14px;font-weight:bold;color:#f0c040;")
    lay.addWidget(lbl)
    btn_row = QHBoxLayout(); btn_row.setSpacing(20)
    btn_pass = QPushButton("✅  PASS")
    btn_pass.setMinimumHeight(48); btn_pass.setMinimumWidth(140)
    btn_pass.setStyleSheet(
        "QPushButton{background:#1a6a3a;color:#afffcf;font-size:17px;"
        "font-weight:bold;border-radius:8px;border:2px solid #2a9a5a;}"
        "QPushButton:hover{background:#27ae60;color:#fff;}"
        "QPushButton:pressed{background:#1a5a30;}")
    btn_fail = QPushButton("❌  FAIL")
    btn_fail.setMinimumHeight(48); btn_fail.setMinimumWidth(140)
    btn_fail.setStyleSheet(
        "QPushButton{background:#6a1a1a;color:#ffaaaa;font-size:17px;"
        "font-weight:bold;border-radius:8px;border:2px solid #aa3a3a;}"
        "QPushButton:hover{background:#c0392b;color:#fff;}"
        "QPushButton:pressed{background:#5a1010;}")
    def _pick(r):
        result_box[0] = r; dlg.accept()
    btn_pass.clicked.connect(lambda: _pick("PASS"))
    btn_fail.clicked.connect(lambda: _pick("FAIL"))
    btn_row.addWidget(btn_pass); btn_row.addWidget(btn_fail)
    lay.addLayout(btn_row)
    btn_cancel = QPushButton(cancel_label)
    btn_cancel.setFixedHeight(30)
    btn_cancel.setStyleSheet(
        "QPushButton{background:#1a1a2a;color:#888;border:1px solid #3a3a5a;"
        "font-size:11px;padding:4px 16px;border-radius:4px;}"
        "QPushButton:hover{background:#252535;color:#aab;}")
    btn_cancel.clicked.connect(dlg.reject)
    lay.addWidget(btn_cancel, alignment=Qt.AlignCenter)
    dlg.exec_()
    return result_box[0]


# =============================================================================
#  RecordingPanel
# =============================================================================
class RecordingPanel(QWidget):
    def __init__(self, engine: "CoreEngine", signals: "Signals", parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        self._build()
        signals.rec_started.connect(lambda _: self._on_rec_started())
        signals.rec_stopped.connect(self._on_rec_stopped)
        signals.capture_saved.connect(self._on_capture_saved)

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        # ── 상태 ─────────────────────────────────────────────────────────────
        st=QGroupBox("상태"); g=QGridLayout(st); g.setSpacing(6)
        self.status_lbl=QLabel("● 대기")
        self.status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;font-size:14px;")
        self.timer_lbl=QLabel("00:00:00")
        self.timer_lbl.setStyleSheet("font-size:28px;font-weight:bold;color:#2ecc71;font-family:monospace;")
        self.datetime_lbl=QLabel("—")
        self.datetime_lbl.setStyleSheet("color:#7bc8e0;font-size:11px;font-family:monospace;")
        g.addWidget(self.status_lbl,0,0,1,2)
        g.addWidget(self.timer_lbl,1,0,1,2,Qt.AlignCenter)
        g.addWidget(self.datetime_lbl,2,0,1,2,Qt.AlignCenter)
        g.addWidget(QLabel("Screen FPS:"),3,0)
        self.scr_fps_lbl=QLabel("—"); g.addWidget(self.scr_fps_lbl,3,1)
        g.addWidget(QLabel("Camera FPS:"),4,0)
        self.cam_fps_lbl=QLabel("—"); g.addWidget(self.cam_fps_lbl,4,1)
        self._folder_btn=QPushButton("📂 녹화 폴더"); self._folder_btn.setFixedHeight(26)
        self._folder_btn.clicked.connect(
            lambda: open_folder(self.engine.output_dir or self.engine.base_dir))
        g.addWidget(self._folder_btn,5,0,1,2)
        v.addWidget(st)

        # ── 녹화 소스 ─────────────────────────────────────────────────────────
        src_g=QGroupBox("📹 녹화 소스  (미리보기 출력 ON/OFF 와 독립)")
        src_l=QHBoxLayout(src_g); src_l.setSpacing(16)
        self.scr_chk=QCheckBox("🖥 Display 저장")
        self.scr_chk.setChecked(True)
        self.scr_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#7bc8e0;")
        self.scr_chk.toggled.connect(lambda c: setattr(self.engine,'screen_rec_enabled',c))
        self.cam_chk=QCheckBox("📷 Camera 저장")
        self.cam_chk.setChecked(True)
        self.cam_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#f0c040;")
        self.cam_chk.toggled.connect(lambda c: setattr(self.engine,'camera_rec_enabled',c))
        src_l.addWidget(self.scr_chk); src_l.addWidget(self.cam_chk); src_l.addStretch()
        v.addWidget(src_g)

        # ── 현재 프레임 캡처 ─────────────────────────────────────────────────
        cap_g=QGroupBox("📸 현재 프레임 캡처 (PNG 저장)")
        cap_v=QVBoxLayout(cap_g); cap_v.setSpacing(6)
        cap_btn_row=QHBoxLayout(); cap_btn_row.setSpacing(8)
        self.cap_scr_btn=QPushButton("🖥 Display 캡처"); self.cap_scr_btn.setFixedHeight(30)
        self.cap_scr_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:4px;font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#223344;}")
        self.cap_scr_btn.clicked.connect(self._on_cap_scr)
        self.cap_cam_btn=QPushButton("📷 Camera 캡처"); self.cap_cam_btn.setFixedHeight(30)
        self.cap_cam_btn.setStyleSheet(
            "QPushButton{background:#1a2a1a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:4px;font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#222a10;}")
        self.cap_cam_btn.clicked.connect(self._on_cap_cam)
        cap_btn_row.addWidget(self.cap_scr_btn); cap_btn_row.addWidget(self.cap_cam_btn)
        cap_v.addLayout(cap_btn_row)
        self.cap_lbl=QLabel(""); self.cap_lbl.setStyleSheet("color:#2ecc71;font-size:10px;")
        cap_v.addWidget(self.cap_lbl)
        v.addWidget(cap_g)

        # ── 컨트롤 버튼 ──────────────────────────────────────────────────────
        ctrl=QGroupBox("컨트롤"); cv=QVBoxLayout(ctrl); cv.setSpacing(8)
        self.btn_start=QPushButton("⏺  녹화 시작  [Ctrl+Alt+W]")
        self.btn_start.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;font-size:12px;padding:8px;"
            "border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#2ecc71;}"
            "QPushButton:disabled{background:#1a3a28;color:#4a7a5a;}")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop=QPushButton("⏹  녹화 종료  [Ctrl+Alt+E]")
        self.btn_stop.setStyleSheet(
            "QPushButton{background:#c0392b;color:white;font-size:12px;padding:8px;"
            "border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#e74c3c;}"
            "QPushButton:disabled{background:#3a1a1a;color:#7a4a4a;}")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)
        cv.addWidget(self.btn_start); cv.addWidget(self.btn_stop); v.addWidget(ctrl)

        # ── FPS / 배속 ────────────────────────────────────────────────────────
        fg=QGroupBox("FPS & 배속"); fl=QGridLayout(fg); fl.setSpacing(8)
        fl.addWidget(QLabel("화면 Target FPS:"),0,0)
        self.fps_spin=_make_spinbox(1,120,30,1,1); self.fps_spin.setFixedWidth(80)
        self.fps_spin.valueChanged.connect(lambda val: setattr(self.engine,'actual_screen_fps',val))
        fl.addWidget(self.fps_spin,0,1)
        fl.addWidget(QLabel("저장 배속:"),1,0)
        self.speed_spin=_make_spinbox(0.1,10,1,0.25,2); self.speed_spin.setFixedWidth(80)
        self.speed_spin.setStyleSheet(
            "QDoubleSpinBox{background:#1a1a2a;color:#f0c040;border:1px solid #5a5a20;"
            "border-radius:4px;padding:3px;font-size:13px;font-weight:bold;}")
        self.speed_spin.valueChanged.connect(self._on_speed)
        fl.addWidget(self.speed_spin,1,1)
        pr=QHBoxLayout(); pr.setSpacing(4)
        for lbl,val in [("0.5×",.5),("1×",1.),("2×",2.),("4×",4.)]:
            b=QPushButton(lbl); b.setFixedSize(40,24)
            b.setStyleSheet("QPushButton{background:#2a2a1a;color:#f0c040;"
                            "border:1px solid #4a4a20;border-radius:3px;font-size:10px;}")
            b.clicked.connect(lambda _,val=val: self.speed_spin.setValue(val))
            pr.addWidget(b)
        fl.addLayout(pr,2,0,1,2)
        self.speed_info=QLabel("정배속"); self.speed_info.setStyleSheet("color:#888;font-size:10px;")
        fl.addWidget(self.speed_info,3,0,1,2)
        self.speed_lock=QLabel("🔒 녹화 중 배속 변경 불가")
        self.speed_lock.setStyleSheet("color:#e74c3c;font-size:10px;font-weight:bold;")
        self.speed_lock.setVisible(False); fl.addWidget(self.speed_lock,4,0,1,2)
        v.addWidget(fg)

        # ── 용량 절감 ─────────────────────────────────────────────────────────
        cg=QGroupBox("🗜 영상 용량 절감"); cl=QGridLayout(cg); cl.setSpacing(6)
        cl.addWidget(QLabel("코덱:"),0,0)
        self.codec_cb=QComboBox()
        self.codec_cb.addItems(["mp4v (기본/안전)","avc1 / H.264 (소형, 지원 시)","xvid (호환성)"])
        self.codec_cb.setStyleSheet(
            "QComboBox{background:#1a1a3a;color:#ddd;border:1px solid #3a4a6a;border-radius:3px;padding:2px;}")
        self.codec_cb.currentIndexChanged.connect(self._on_codec)
        cl.addWidget(self.codec_cb,0,1)
        self.codec_warn=QLabel("※ avc1 지원 불가 시 자동으로 mp4v로 전환")
        self.codec_warn.setStyleSheet("color:#666;font-size:9px;"); self.codec_warn.setVisible(False)
        cl.addWidget(self.codec_warn,1,0,1,2)
        cl.addWidget(QLabel("해상도:"),2,0)
        self.scale_cb=QComboBox()
        self.scale_cb.addItems(["100% 원본","75%","50%"])
        self.scale_cb.setStyleSheet(
            "QComboBox{background:#1a1a3a;color:#ddd;border:1px solid #3a4a6a;border-radius:3px;padding:2px;}")
        self.scale_cb.currentIndexChanged.connect(self._on_scale)
        cl.addWidget(self.scale_cb,2,1)
        v.addWidget(cg)

        self._timer=QTimer(self); self._timer.timeout.connect(self._tick)

    # ── 슬롯 ─────────────────────────────────────────────────────────────────
    def _on_start(self):
        self.engine.start_recording()

    def _on_stop(self):
        if self.engine.tc_rec_enabled and self.engine.recording:
            result = show_tc_dialog(
                self, "녹화를 종료합니다.\nT/C 검증 결과를 선택하세요.",
                cancel_label="취소  (녹화 계속)")
            if result is None:
                return
            self.engine.tc_verify_result = result
        self.engine.stop_recording()

    def _on_cap_scr(self):
        if self.engine.tc_capture_enabled:
            result = show_tc_dialog(self, "캡처 전 T/C 검증 결과를 선택하세요.")
            if result is None: return
            # ★ 수정: tc_verify_result 설정 누락 → capture_frame에서 PASS/FAIL 경로 미적용
            self.engine.tc_verify_result = result
            self.engine.capture_frame("screen", tc_tag=result)
        else:
            self.engine.capture_frame("screen")

    def _on_cap_cam(self):
        if self.engine.tc_capture_enabled:
            result = show_tc_dialog(self, "캡처 전 T/C 검증 결과를 선택하세요.")
            if result is None: return
            # ★ 수정: tc_verify_result 설정 누락
            self.engine.tc_verify_result = result
            self.engine.capture_frame("camera", tc_tag=result)
        else:
            self.engine.capture_frame("camera")

    def _on_rec_started(self):
        self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True)
        self.status_lbl.setText("● 녹화 중")
        self.status_lbl.setStyleSheet("color:#2ecc71;font-weight:bold;font-size:14px;")
        self.speed_spin.setEnabled(False); self.speed_lock.setVisible(True)
        self._timer.start(500)

    def _on_rec_stopped(self):
        self.btn_start.setEnabled(True); self.btn_stop.setEnabled(False)
        self.status_lbl.setText("● 대기")
        self.status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;font-size:14px;")
        self.timer_lbl.setText("00:00:00"); self.datetime_lbl.setText("—")
        self.speed_spin.setEnabled(True); self.speed_lock.setVisible(False)
        self._timer.stop()
        # ★ 수정: TC 결과로 폴더명이 바뀌었을 수 있으므로 _folder_btn 경로 갱신
        try:
            self._folder_btn.clicked.disconnect()
        except Exception:
            pass
        final_dir = self.engine.output_dir or self.engine.base_dir
        self._folder_btn.clicked.connect(lambda: open_folder(final_dir))

    def _tick(self):
        if self.engine.recording and self.engine.start_time:
            e=time.time()-self.engine.start_time
            self.timer_lbl.setText(fmt_hms(e))
            self.datetime_lbl.setText(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))

    def _on_capture_saved(self, source, path):
        self.cap_lbl.setText(f"✅ {source}: {os.path.basename(path)}")
        QTimer.singleShot(4000, lambda: self.cap_lbl.setText(""))

    def _on_speed(self, val):
        self.engine.playback_speed=val
        if val==1.0: t="정배속"
        elif val<1.0: t=f"{val:.2f}× 슬로우모션"
        else:          t=f"{val:.2f}× 타임랩스"
        self.speed_info.setText(t)

    def _on_codec(self, i):
        codecs=["mp4v","avc1","xvid"]
        self.engine.video_codec=codecs[i] if i<len(codecs) else "mp4v"
        self.codec_warn.setVisible(i==1)

    def _on_scale(self, i):
        self.engine.video_scale=[1.0,0.75,0.5][i]

    def update_fps(self, scr: float, cam: float):
        self.scr_fps_lbl.setText(f"{scr:.1f} fps")
        self.cam_fps_lbl.setText(f"{cam:.1f} fps")


# =============================================================================
#  ManualClipPanel  (T/C 검증 다이얼로그 공용 함수 사용)
# =============================================================================
class ManualClipPanel(QWidget):
    def __init__(self, engine: "CoreEngine", signals: "Signals", parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        self._led=False; self._led_timer=QTimer(self)
        self._led_timer.timeout.connect(self._blink)
        signals.manual_clip_saved.connect(self._on_saved)
        self._build()

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        info=QLabel("버튼을 누르면 해당 시점 기준 전/후 N초를 버퍼에서 추출해 저장합니다.\n최대 ±40초 저장 가능 (버퍼 크기에 따라 달라질 수 있습니다)")
        info.setWordWrap(True); info.setStyleSheet("color:#778;font-size:10px;"); v.addWidget(info)

        # T/C 상태 표시
        tc_row=QHBoxLayout(); tc_row.setSpacing(8)
        self.tc_lbl=QLabel("🔬 T/C 검증:"); self.tc_lbl.setStyleSheet("color:#f0a040;font-size:10px;font-weight:bold;")
        self.tc_status_lbl=QLabel("비활성  (저장 경로 패널에서 설정)")
        self.tc_status_lbl.setStyleSheet("color:#556;font-size:9px;")
        tc_row.addWidget(self.tc_lbl); tc_row.addWidget(self.tc_status_lbl); tc_row.addStretch()
        v.addLayout(tc_row)

        # 소스
        sg=QGroupBox("저장 소스"); sl=QHBoxLayout(sg); sl.setSpacing(12)
        self.scr_chk=QCheckBox("🖥 Display"); self.scr_chk.setChecked(True)
        self.cam_chk=QCheckBox("📷 Camera");  self.cam_chk.setChecked(True)
        self.scr_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#7bc8e0;")
        self.cam_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#f0c040;")
        self.scr_chk.toggled.connect(self._upd_src); self.cam_chk.toggled.connect(self._upd_src)
        sl.addWidget(self.scr_chk); sl.addWidget(self.cam_chk); sl.addStretch(); v.addWidget(sg)

        # 전/후 시간
        tg=QGroupBox("전/후 시간  (최대 40초)"); tgl=QGridLayout(tg); tgl.setSpacing(6)
        # ★ 초기값을 engine에서 읽어 DB-UI 동기화 (default: 10초)
        _pre_val  = float(getattr(self.engine, 'manual_pre_sec',  10.0))
        _post_val = float(getattr(self.engine, 'manual_post_sec', 10.0))
        self.pre_spin  = self._make_row(tgl,0,"🔵 전 (초)","#7bc8e0","manual_pre_sec", 0,40,_pre_val)
        self.post_spin = self._make_row(tgl,1,"🟠 후 (초)","#f0a040","manual_post_sec",0,40,_post_val)
        v.addWidget(tg)

        # 버튼
        br=QHBoxLayout(); br.setSpacing(8)
        self.led_lbl=QLabel("●"); self.led_lbl.setFixedWidth(22)
        self.led_lbl.setAlignment(Qt.AlignCenter)
        self.led_lbl.setStyleSheet("font-size:22px;color:#333;"); br.addWidget(self.led_lbl)
        self.clip_btn=QPushButton("🎬  지금 클립 저장  [Ctrl+Alt+M]")
        self.clip_btn.setMinimumHeight(42)
        self.clip_btn.setStyleSheet(
            "QPushButton{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #7d3c98,stop:1 #e67e22);color:white;font-size:13px;"
            "font-weight:bold;border:none;border-radius:6px;padding:6px;}"
            "QPushButton:hover{background:#9b59b6;}"
            "QPushButton:disabled{background:#2a2a3a;color:#666;border:1px solid #444;}")
        self.clip_btn.clicked.connect(self._on_clip); br.addWidget(self.clip_btn,1); v.addLayout(br)
        self.status_lbl=QLabel("대기 중"); self.status_lbl.setStyleSheet("color:#888;font-size:11px;font-family:monospace;"); v.addWidget(self.status_lbl)
        open_btn=QPushButton("📂 수동클립 폴더"); open_btn.setFixedHeight(26)
        def _open_manual_folder():
            # ★ 수정: _build_path로 실제 저장 경로 계산 후 열기
            p = self.engine._build_path("manual")
            # 해당 경로가 없으면 존재하는 상위 폴더를 찾아 올라감
            while p and not os.path.exists(p) and p != os.path.dirname(p):
                p = os.path.dirname(p)
            open_folder(p or self.engine.base_dir)
        open_btn.clicked.connect(_open_manual_folder); v.addWidget(open_btn)

        self._poll=QTimer(self); self._poll.timeout.connect(self._check_state); self._poll.start(200)
        self._tc_poll=QTimer(self); self._tc_poll.timeout.connect(self._upd_tc_status); self._tc_poll.start(500)

    def _upd_tc_status(self):
        if self.engine.tc_manual_enabled:
            self.tc_status_lbl.setText("✅ 활성화  (클립 저장 시 PASS/FAIL 선택)")
            self.tc_status_lbl.setStyleSheet("color:#f0a040;font-size:9px;font-weight:bold;")
        else:
            self.tc_status_lbl.setText("비활성  (저장 경로 패널에서 설정)")
            self.tc_status_lbl.setStyleSheet("color:#556;font-size:9px;")

    def _make_row(self, grid, row, label, color, attr, min_v, max_v, val):
        lbl=QLabel(label); lbl.setStyleSheet(f"font-weight:bold;color:{color};")
        sp=QDoubleSpinBox(); sp.setRange(min_v,max_v); sp.setValue(val)
        sp.setSingleStep(1.0); sp.setDecimals(1); sp.setMinimumHeight(28)
        sp.setStyleSheet(f"QDoubleSpinBox{{background:#1a1a3a;color:{color};"
                         "border:1px solid #3a3a5a;border-radius:4px;padding:3px;"
                         "font-size:13px;font-weight:bold;}")
        sl=QSlider(Qt.Horizontal); sl.setRange(0,int(max_v*10)); sl.setValue(int(val*10))
        sp.valueChanged.connect(lambda v,sl=sl,a=attr: (sl.blockSignals(True),sl.setValue(int(v*10)),sl.blockSignals(False),setattr(self.engine,a,v)))
        sl.valueChanged.connect(lambda v,sp=sp,a=attr: (sp.blockSignals(True),sp.setValue(v/10),sp.blockSignals(False),setattr(self.engine,a,v/10)))
        grid.addWidget(lbl,row,0); grid.addWidget(sp,row,1); grid.addWidget(sl,row,2)
        grid.setColumnStretch(2,1)
        return sp

    def _upd_src(self):
        s=self.scr_chk.isChecked(); c=self.cam_chk.isChecked()
        if not s and not c:
            self.sender().blockSignals(True); self.sender().setChecked(True)
            self.sender().blockSignals(False); return
        self.engine.manual_source=("both" if s and c else "screen" if s else "camera")

    def _on_clip(self):
        if self.engine.tc_manual_enabled:
            result = show_tc_dialog(self, "수동녹화 저장 전\nT/C 검증 결과를 선택하세요.")
            if result is None:
                return
            self.engine.tc_verify_result = result

        ok = self.engine.save_manual_clip()
        if not ok:
            self.status_lbl.setText("⏳ 쿨다운 중 — 잠시 후 재시도")
            self.status_lbl.setStyleSheet("color:#f0c040;font-size:11px;")

    def _on_saved(self, path):
        self.status_lbl.setText(f"✅ 저장: {os.path.basename(path)}")
        self.status_lbl.setStyleSheet("color:#2ecc71;font-size:11px;")

    def _check_state(self):
        idle = (self.engine.manual_state == CoreEngine.MANUAL_IDLE)
        if idle:
            self._led_timer.stop(); self.led_lbl.setStyleSheet("font-size:22px;color:#333;")
            self.clip_btn.setEnabled(True)
        else:
            if not self._led_timer.isActive(): self._led_timer.start(400)
            self.clip_btn.setEnabled(False)

    def _blink(self):
        self._led=not self._led
        self.led_lbl.setStyleSheet(f"font-size:22px;color:{'#e74c3c' if self._led else '#7f2a2a'};")


# =============================================================================
#  BlackoutPanel
# =============================================================================
class BlackoutPanel(QWidget):
    def __init__(self, engine: "CoreEngine", signals: "Signals", parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        signals.blackout_detected.connect(self._on_bo)
        self._build()

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        self.rec_chk=QCheckBox("블랙아웃 클립 녹화 활성화")
        self.rec_chk.setChecked(True)
        self.rec_chk.toggled.connect(lambda c: setattr(self.engine,'blackout_rec_enabled',c))
        v.addWidget(self.rec_chk)

        tc_row=QHBoxLayout()
        self.tc_status_lbl=QLabel("🔬 T/C 검증: 비활성")
        self.tc_status_lbl.setStyleSheet("color:#556;font-size:9px;")
        tc_row.addWidget(self.tc_status_lbl); tc_row.addStretch()
        v.addLayout(tc_row)
        self._tc_poll=QTimer(self); self._tc_poll.timeout.connect(self._upd_tc_status); self._tc_poll.start(500)

        tg=QGroupBox("감지 설정"); tl=QGridLayout(tg)
        tl.addWidget(QLabel("밝기 변화 임계값:"),0,0)
        self.thr_spin=_make_spinbox(5,200,30,1,0,"",80)
        self.thr_spin.setSuffix(" (0~255)")
        self.thr_spin.valueChanged.connect(lambda v: setattr(self.engine,'brightness_threshold',v))
        tl.addWidget(self.thr_spin,0,1)
        tl.addWidget(QLabel("쿨다운 (초):"),1,0)
        self.cd_spin=_make_spinbox(0.5,60,5,0.5,1,"",80)
        self.cd_spin.valueChanged.connect(lambda v: setattr(self.engine,'blackout_cooldown',v))
        tl.addWidget(self.cd_spin,1,1)
        v.addWidget(tg)

        cnt=QGroupBox("감지 횟수"); cl=QGridLayout(cnt)
        cl.addWidget(QLabel("화면:"),0,0); self.scr_cnt=QLabel("0")
        self.scr_cnt.setStyleSheet("font-weight:bold;color:#e74c3c;"); cl.addWidget(self.scr_cnt,0,1)
        cl.addWidget(QLabel("카메라:"),1,0); self.cam_cnt=QLabel("0")
        self.cam_cnt.setStyleSheet("font-weight:bold;color:#e74c3c;"); cl.addWidget(self.cam_cnt,1,1)
        open_btn=QPushButton("📂 블랙아웃 폴더"); open_btn.setFixedHeight(26)
        def _open_bo_folder():
            # ★ 수정: _build_path로 실제 저장 경로 계산 후 열기
            p = self.engine._build_path("blackout")
            while p and not os.path.exists(p) and p != os.path.dirname(p):
                p = os.path.dirname(p)
            open_folder(p or self.engine.base_dir)
        open_btn.clicked.connect(_open_bo_folder)
        cl.addWidget(open_btn,2,0,1,2)
        v.addWidget(cnt)

        roi_g=QGroupBox("ROI 밝기 현황"); rl=QVBoxLayout(roi_g)
        self.roi_txt=QTextEdit(); self.roi_txt.setReadOnly(True); self.roi_txt.setFixedHeight(100)
        self.roi_txt.setStyleSheet("font-size:10px;font-family:monospace;background:#0d0d1e;")
        rl.addWidget(self.roi_txt); v.addWidget(roi_g)

        ev_g=QGroupBox("최근 이벤트"); el=QVBoxLayout(ev_g)
        self.ev_txt=QTextEdit(); self.ev_txt.setReadOnly(True); self.ev_txt.setFixedHeight(80)
        self.ev_txt.setStyleSheet("font-size:10px;font-family:monospace;background:#0d0d1e;")
        el.addWidget(self.ev_txt); v.addWidget(ev_g)

    def _upd_tc_status(self):
        if self.engine.tc_blackout_enabled:
            self.tc_status_lbl.setText("🔬 T/C 검증: ✅ 활성화  (블랙아웃 클립 저장 후 PASS/FAIL 적용)")
            self.tc_status_lbl.setStyleSheet("color:#f0a040;font-size:9px;font-weight:bold;")
        else:
            self.tc_status_lbl.setText("🔬 T/C 검증: 비활성  (저장 경로 패널에서 설정)")
            self.tc_status_lbl.setStyleSheet("color:#556;font-size:9px;")

    def _on_bo(self, source, event):
        self.scr_cnt.setText(str(self.engine.screen_bo_count))
        self.cam_cnt.setText(str(self.engine.camera_bo_count))

    def refresh(self):
        self.scr_cnt.setText(str(self.engine.screen_bo_count))
        self.cam_cnt.setText(str(self.engine.camera_bo_count))

        # ── 블랙아웃/T/C 중 하나라도 켜져 있을 때만 밝기 현황 표시 ★
        if not (self.engine.blackout_rec_enabled or
                self.engine.tc_blackout_enabled):
            self.roi_txt.setPlainText(
                "블랙아웃 기능 꺼짐 — 밝기 계산 중단 중\n"
                "(절약 중: screen/camera 루프에서 ROI 밝기 연산 없음)")
            return

        lines=[]
        for src,avgs,ov in [("Scr",self.engine.screen_roi_avg,self.engine.screen_overall_avg),
                             ("Cam",self.engine.camera_roi_avg,self.engine.camera_overall_avg)]:
            if avgs:
                b,g,r=ov; br=0.114*b+0.587*g+0.299*r
                lines.append(f"[{src}] R{int(r)} G{int(g)} B{int(b)} Br:{int(br)}")
                for i,a in enumerate(avgs[:5]):
                    b2,g2,r2=a; br2=0.114*b2+0.587*g2+0.299*r2
                    lines.append(f"  ROI{i+1}: Br:{int(br2)}")
        self.roi_txt.setPlainText("\n".join(lines))
        ev=[]
        for src,evs in [("화면",self.engine.screen_bo_events),("카메라",self.engine.camera_bo_events)]:
            for e in reversed(evs[-4:]):
                ev.append(f"[{src}] {e['time']}  변화:{int(e['brightness_change'])}")
        self.ev_txt.setPlainText("\n".join(ev))


# =============================================================================
#  PathSettingsPanel  (use_custom_path 제거, 라디오버튼, 텍스트 수정)
# =============================================================================
class PathSettingsPanel(QWidget):
    """
    저장 경로 설정 v2.8
    ─ T/C 검증 목적 (기능별) — 수정:
        · Recording/블랙아웃/캡처에도 수동녹화처럼 TC 검증 UI 반영
        · '(PASS)|(FAIL) 반영 할 폴더' 텍스트
    ─ 기능별 저장경로 소기능(use_custom_path_*) 삭제
        · TC ON → 구성 경로 사용, TC OFF → 기본 경로
    ─ TC-ID 번호형식: RadioButton (개별|범위 단일 선택)
    ─ 범위 선택 시 끝번호 항상 시작번호+1 이상 강제
    """

    class _PathDragList(QWidget):
        """경로 세그먼트 드래그앤드랍 순서변경."""
        order_changed = pyqtSignal()
        _ROW_H = 32; _BG = "#0d0d1e"; _BG_HV = "#1a1a3a"; _BG_DG = "#1a2a4a"

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setStyleSheet(f"background:{self._BG};")
            self._items: list = []
            self._lay = QVBoxLayout(self)
            self._lay.setContentsMargins(2,2,2,2); self._lay.setSpacing(3)
            self._drag_idx = -1; self._drag_start = QPoint(); self._dragging = False

        def set_items(self, items: list):
            while self._lay.count():
                it=self._lay.takeAt(0)
                if it.widget(): it.widget().setParent(None)
            self._items.clear()
            for item in items:
                row = self._make_row(item); item['widget'] = row
                self._items.append(item); self._lay.addWidget(row)
            self._lay.addStretch()

        def _make_row(self, item: dict) -> QWidget:
            row = QWidget(); row.setFixedHeight(self._ROW_H)
            row.setStyleSheet(f"QWidget{{background:{self._BG};border:1px solid #1a1a3a;border-radius:4px;}}")
            row.setCursor(Qt.OpenHandCursor)
            hl = QHBoxLayout(row); hl.setContentsMargins(6,2,6,2); hl.setSpacing(6)
            grip = QLabel("⠿"); grip.setFixedWidth(16)
            grip.setStyleSheet("color:#4a5a8a;font-size:18px;")
            lbl = QLabel(item['label'])
            lbl.setStyleSheet("color:#ccd;font-size:11px;background:transparent;")
            hl.addWidget(grip); hl.addWidget(lbl, 1)
            return row

        def get_order(self) -> list:
            return [item['key'] for item in self._items]

        def _row_at_y(self, y):
            for i, item in enumerate(self._items):
                w = item.get('widget')
                if w and w.y() <= y < w.y() + w.height(): return i
            return -1

        def mousePressEvent(self, e):
            if e.button() == Qt.LeftButton:
                idx = self._row_at_y(e.pos().y())
                if idx >= 0: self._drag_idx = idx; self._drag_start = e.pos()
            super().mousePressEvent(e)

        def mouseMoveEvent(self, e):
            if self._drag_idx >= 0 and not self._dragging:
                if (e.pos()-self._drag_start).manhattanLength() > 6:
                    self._dragging = True
                    w = self._items[self._drag_idx].get('widget')
                    if w: w.setStyleSheet(f"QWidget{{background:{self._BG_DG};border:1px solid #5a7aaa;border-radius:4px;}}"); w.setCursor(Qt.ClosedHandCursor)
            super().mouseMoveEvent(e)

        def mouseReleaseEvent(self, e):
            if self._dragging and self._drag_idx >= 0:
                tgt = self._row_at_y(e.pos().y())
                if 0 <= tgt < len(self._items) and tgt != self._drag_idx:
                    self._items.insert(tgt, self._items.pop(self._drag_idx))
                    while self._lay.count():
                        it = self._lay.takeAt(0)
                        if it.widget(): it.widget().setParent(None)
                    for item in self._items:
                        row = self._make_row(item); item['widget'] = row; self._lay.addWidget(row)
                    self._lay.addStretch(); self.order_changed.emit()
                else:
                    w = self._items[self._drag_idx].get('widget')
                    if w: w.setStyleSheet(f"QWidget{{background:{self._BG};border:1px solid #1a1a3a;border-radius:4px;}}"); w.setCursor(Qt.OpenHandCursor)
            self._drag_idx = -1; self._dragging = False
            super().mouseReleaseEvent(e)

    # ── PathSettingsPanel 본체 ────────────────────────────────────────────────
    def __init__(self, engine: "CoreEngine", db: "SettingsDB", parent=None):
        super().__init__(parent)
        self.engine = engine; self.db = db
        self._seg_rows: list = []
        self._build()

    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,6); v.setSpacing(0)

        def _make_collapsible(title: str, color: str):
            btn = QPushButton(f"▼  {title}")
            btn.setCheckable(True); btn.setChecked(False); btn.setFixedHeight(30)
            btn.setStyleSheet(
                f"QPushButton{{background:#1a1a2e;color:{color};border:1px solid #2a2a4a;"
                "border-radius:4px;font-size:11px;font-weight:bold;text-align:left;"
                "padding:0 10px;margin-top:6px;}}"
                f"QPushButton:hover{{background:#22223a;}}"
                f"QPushButton:checked{{background:#0d0d1a;color:{color};}}")
            body = QWidget()
            body.setStyleSheet("QWidget{background:#0a0a1a;border:1px solid #1a1a3a;border-top:none;border-radius:0 0 4px 4px;}")
            bl = QVBoxLayout(body); bl.setContentsMargins(10,8,10,10); bl.setSpacing(6)
            body.setVisible(True)
            def _toggle(collapsed, b=body, bt=btn, t=title, col=color):
                body.setVisible(not collapsed)
                bt.setText(f"{'▶' if collapsed else '▼'}  {t}")
            btn.toggled.connect(_toggle)
            return btn, body, bl

        # ══ 1. T/C 검증 목적 ══════════════════════════════════════════════════
        tc_btn, tc_body, tc_l = _make_collapsible("🔬 T/C 검증 목적  (기능별 개별 활성화)", "#f0c040")
        v.addWidget(tc_btn); v.addWidget(tc_body)

        tc_hint = QLabel(
            "활성화된 기능 완료 후 PASS/FAIL 선택 → 아래 지정 폴더명 앞에 태그 삽입\n"
            "③ 선택 시: 녹화→Rec_폴더, 수동녹화→manual_clip폴더, 캡처→capture폴더, 블랙아웃→blackout폴더\n"
            "⚠ TC ON 시 아래 경로 구성 사용  |  TC OFF 시 기본 경로(날짜/기능폴더) 사용\n"
            "⚠ TC-ID를 먼저 입력하고 저장해야 T/C 검증 활성화 가능합니다.")
        tc_hint.setWordWrap(True); tc_hint.setStyleSheet("color:#888;font-size:9px;")
        tc_l.addWidget(tc_hint)

        _TC_ITEMS = [
            ("tc_rec_chk",      "tc_rec_enabled",      "⏺ 녹화",      "#27ae60"),
            ("tc_manual_chk",   "tc_manual_enabled",   "🎬 수동녹화",  "#e67e22"),
            ("tc_blackout_chk", "tc_blackout_enabled", "⚡ 블랙아웃",  "#e74c3c"),
            ("tc_capture_chk",  "tc_capture_enabled",  "📸 캡처",      "#3498db"),
        ]
        chk_grid = QGridLayout(); chk_grid.setSpacing(8)
        for i, (attr_name, engine_attr, label, color) in enumerate(_TC_ITEMS):
            chk = QCheckBox(label); chk.setChecked(False); chk.setMinimumHeight(28)
            chk.setStyleSheet(
                f"QCheckBox{{font-size:11px;font-weight:bold;color:{color};"
                "spacing:6px;padding:4px 8px;border:1px solid #2a2a3a;border-radius:5px;background:#0d0d1e;}}"
                f"QCheckBox:checked{{background:#0d1525;border-color:{color};}}"
                "QCheckBox:hover{background:#14142a;}")
            chk.toggled.connect(self._make_tc_toggle(engine_attr))
            setattr(self, attr_name, chk)
            chk_grid.addWidget(chk, i//2, i%2)
        tc_l.addLayout(chk_grid)

        # (PASS)|(FAIL) 반영 할 폴더 — 텍스트 수정
        folder_row = QHBoxLayout(); folder_row.setSpacing(8)
        folder_row.addWidget(QLabel("(PASS)|(FAIL) 반영 할 폴더:"))
        self.tc_folder_combo = QComboBox()
        self.tc_folder_combo.addItems([
            "① TC-ID 폴더  (기본 — 모든 기능 공통)",
            "② 차종_버전 폴더",
            "③ 각 기능 폴더  (Rec_ / manual_clip / capture / blackout)",
            "④ 직접 입력",
        ])
        self.tc_folder_combo.setStyleSheet(
            "QComboBox{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:3px;padding:2px 6px;font-size:11px;}")
        self.tc_folder_combo.currentIndexChanged.connect(self._on_tc_folder_changed)
        folder_row.addWidget(self.tc_folder_combo, 1)
        tc_l.addLayout(folder_row)
        self.tc_custom_folder_ed = QLineEdit()
        self.tc_custom_folder_ed.setPlaceholderText("폴더 절대경로 직접 입력")
        self.tc_custom_folder_ed.setVisible(False)
        self.tc_custom_folder_ed.setStyleSheet(
            "QLineEdit{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:3px;padding:3px 8px;font-size:11px;}")
        self.tc_custom_folder_ed.textChanged.connect(self._sync_tc_tag_target)
        tc_l.addWidget(self.tc_custom_folder_ed)

        # ══ 2. TC-ID 설정 ══════════════════════════════════════════════════════
        tc_id_btn, tc_id_body, tc_id_l = _make_collapsible("🔢 TC-ID 설정", "#7bc8e0")
        v.addWidget(tc_id_btn); v.addWidget(tc_id_body)

        prow = QHBoxLayout(); prow.setSpacing(8)
        prow.addWidget(QLabel("prefix"))
        self.tc_prefix_ed = QLineEdit()
        self.tc_prefix_ed.setPlaceholderText("예: BLTN_CAM_TC_3-")
        self.tc_prefix_ed.setMinimumHeight(28)
        self.tc_prefix_ed.setStyleSheet(
            "QLineEdit{background:#1a1a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:3px;padding:3px 8px;font-size:12px;}")
        self.tc_prefix_ed.textChanged.connect(self._upd_tc_id)
        prow.addWidget(self.tc_prefix_ed, 1)
        tc_id_l.addLayout(prow)

        # ★ 번호형식 — QButtonGroup RadioButton (단일 선택)
        mode_grp = QGroupBox("번호 형식")
        mode_grp.setStyleSheet("QGroupBox{font-size:10px;color:#9bc;border:1px solid #2a2a4a;"
                               "border-radius:4px;margin-top:12px;padding-top:8px;}")
        mode_l = QHBoxLayout(mode_grp); mode_l.setSpacing(16)
        self._mode_group = QButtonGroup(self)
        self.tc_mode_single = QRadioButton("개별  (예: -0041)")
        self.tc_mode_range  = QRadioButton("범위  (예: -0041~51)")
        self.tc_mode_single.setChecked(True)
        for rb in (self.tc_mode_single, self.tc_mode_range):
            rb.setStyleSheet("QRadioButton{font-size:11px;color:#ccd;spacing:5px;}")
            self._mode_group.addButton(rb)
            mode_l.addWidget(rb)
        mode_l.addStretch()
        self.tc_mode_single.toggled.connect(self._on_tc_mode_changed)
        tc_id_l.addWidget(mode_grp)

        # 번호 스핀박스
        num_grid = QGridLayout(); num_grid.setSpacing(8)
        self._tc_start_lbl = QLabel("번호"); num_grid.addWidget(self._tc_start_lbl, 0, 0)
        self.tc_start_spin = QSpinBox()
        self.tc_start_spin.setRange(0, 99999); self.tc_start_spin.setValue(1)
        self.tc_start_spin.setFixedWidth(88)
        self.tc_start_spin.setToolTip("개별 모드: 이 번호 사용  /  범위 모드: 시작 번호")
        self.tc_start_spin.setStyleSheet(
            "QSpinBox{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:3px;padding:3px;font-size:13px;font-weight:bold;}")
        self.tc_start_spin.valueChanged.connect(self._on_start_changed)
        num_grid.addWidget(self.tc_start_spin, 0, 1)
        num_grid.addWidget(QLabel("자릿수"), 0, 2)
        self.tc_digits_spin = QSpinBox()
        self.tc_digits_spin.setRange(1, 6); self.tc_digits_spin.setValue(4)
        self.tc_digits_spin.setFixedWidth(44); self.tc_digits_spin.setPrefix("0")
        self.tc_digits_spin.setStyleSheet(
            "QSpinBox{background:#1a1a3a;color:#888;border:1px solid #334;"
            "border-radius:3px;padding:2px;font-size:10px;}")
        self.tc_digits_spin.valueChanged.connect(self._upd_tc_id)
        num_grid.addWidget(self.tc_digits_spin, 0, 3)
        tc_id_l.addLayout(num_grid)

        # 끝 번호 행 (범위 모드에서만 표시)
        self._tc_end_row = QWidget(); self._tc_end_row.setStyleSheet("background:transparent;")
        end_row_l = QHBoxLayout(self._tc_end_row)
        end_row_l.setContentsMargins(0,0,0,0); end_row_l.setSpacing(8)
        end_row_l.addWidget(QLabel("끝 번호"))
        self.tc_end_spin = QSpinBox()
        self.tc_end_spin.setRange(0, 99999)
        # ★ 초기값 = 시작번호+1
        self.tc_end_spin.setValue(2)
        self.tc_end_spin.setFixedWidth(88)
        self.tc_end_spin.setToolTip("범위 모드 끝 번호 (항상 시작번호+1 이상)")
        self.tc_end_spin.setStyleSheet(
            "QSpinBox{background:#1a1a3a;color:#2ecc71;border:1px solid #2a6a2a;"
            "border-radius:3px;padding:3px;font-size:13px;font-weight:bold;}")
        self.tc_end_spin.valueChanged.connect(self._on_end_changed)
        end_hint = QLabel("  ← 시작번호보다 큰 값")
        end_hint.setStyleSheet("color:#556;font-size:9px;")
        end_row_l.addWidget(self.tc_end_spin); end_row_l.addWidget(end_hint); end_row_l.addStretch()
        self._tc_end_row.setVisible(False)
        tc_id_l.addWidget(self._tc_end_row)

        self.tc_preview_lbl = QLabel("TC-ID: —")
        self.tc_preview_lbl.setStyleSheet(
            "color:#f0c040;font-size:12px;font-weight:bold;"
            "background:#0d0d1e;border:1px solid #2a2a1a;border-radius:3px;padding:4px 8px;")
        tc_id_l.addWidget(self.tc_preview_lbl)

        # ══ 3. 저장 경로 구성 ══════════════════════════════════════════════════
        path_btn, path_body, path_l = _make_collapsible(
            "📁 저장 경로 구성  (드래그로 순서 변경)", "#7bc8e0")
        v.addWidget(path_btn); v.addWidget(path_body)

        path_hint = QLabel("⚠ T/C 검증 ON 시 아래 구성 경로 사용  /  OFF 시 기본 경로(bltn_rec/날짜/기능폴더)")
        path_hint.setStyleSheet("color:#888;font-size:9px;"); path_hint.setWordWrap(True)
        path_l.addWidget(path_hint)

        path_l.addWidget(QLabel("차종_버전:"))
        self.vehicle_ed = QLineEdit()
        self.vehicle_ed.setPlaceholderText("예: TK1_2541  또는  NX5_2633")
        self.vehicle_ed.setMinimumHeight(30)
        self.vehicle_ed.setStyleSheet(
            "QLineEdit{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:4px;padding:4px 10px;font-size:12px;font-weight:bold;}")
        self.vehicle_ed.textChanged.connect(
            lambda t: setattr(self.engine, 'vehicle_type', t.strip()))
        self.vehicle_ed.textChanged.connect(self._upd_preview)
        path_l.addWidget(self.vehicle_ed)

        self._drag_list = PathSettingsPanel._PathDragList(self)
        self._drag_list.order_changed.connect(self._upd_preview)
        path_l.addWidget(self._drag_list)

        self._extra_container = QWidget(); self._extra_container.setStyleSheet("background:transparent;")
        self._extra_lay = QVBoxLayout(self._extra_container)
        self._extra_lay.setContentsMargins(0,0,0,0); self._extra_lay.setSpacing(3)
        path_l.addWidget(self._extra_container)

        add_btn = QPushButton("＋  경로 항목 추가"); add_btn.setFixedHeight(28)
        add_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:5px;font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#1e3548;}")
        add_btn.clicked.connect(lambda: self._add_segment())
        path_l.addWidget(add_btn)

        # ══ 4. 경로 미리보기 ═══════════════════════════════════════════════════
        prev_btn, prev_body, prev_l = _make_collapsible("🔍 경로 미리보기", "#556")
        v.addWidget(prev_btn); v.addWidget(prev_body)
        self.preview_lbl = QLabel("")
        self.preview_lbl.setStyleSheet(
            "color:#f0c040;font-size:10px;font-family:monospace;"
            "background:#06060e;border:1px solid #3a3a1a;border-radius:4px;padding:8px 10px;")
        self.preview_lbl.setWordWrap(True)
        prev_l.addWidget(self.preview_lbl)

        # ══ 5. 저장/폴더 버튼 ═════════════════════════════════════════════════
        btn_row = QHBoxLayout(); btn_row.setSpacing(8); btn_row.setContentsMargins(0,8,0,0)
        save_btn = QPushButton("💾  경로 설정 저장")
        save_btn.setMinimumHeight(36)
        save_btn.setStyleSheet(
            "QPushButton{background:#1a3a2a;color:#afffcf;border:1px solid #2a8a5a;"
            "border-radius:5px;font-size:12px;font-weight:bold;}"
            "QPushButton:hover{background:#225a3a;}")
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn, 3)
        open_btn2 = QPushButton("📂 기본 폴더")
        open_btn2.setMinimumHeight(36)
        open_btn2.setStyleSheet(
            "QPushButton{background:#1a1a3a;color:#9ab;border:1px solid #2a2a5a;"
            "border-radius:5px;font-size:11px;}"
            "QPushButton:hover{background:#22224a;}")
        open_btn2.clicked.connect(lambda: open_folder(self.engine.base_dir))
        btn_row.addWidget(open_btn2, 1)
        v.addLayout(btn_row)

        self.result_lbl = QLabel("")
        self.result_lbl.setStyleSheet("font-size:11px;padding:3px 6px;border-radius:4px;")
        v.addWidget(self.result_lbl)
        v.addStretch()

        self._seg_rows = []
        self._rebuild_drag_list()
        self._upd_tc_id()
        self._upd_preview()

    # ── TC toggle 팩토리 ──────────────────────────────────────────────────────
    def _make_tc_toggle(self, engine_attr: str):
        def _toggle(checked: bool):
            if not checked:
                setattr(self.engine, engine_attr, False); return
            if not self.engine.tc_id or not self.engine.tc_id.strip():
                QMessageBox.warning(
                    self, "저장 경로 설정 필요",
                    "T/C 검증 목적을 활성화하려면\nTC-ID를 먼저 입력하고 저장하세요.",
                    QMessageBox.Ok)
                _attr_to_chk = {
                    'tc_rec_enabled': 'tc_rec_chk', 'tc_manual_enabled': 'tc_manual_chk',
                    'tc_blackout_enabled': 'tc_blackout_chk', 'tc_capture_enabled': 'tc_capture_chk',
                }
                chk_name = _attr_to_chk.get(engine_attr)
                if chk_name:
                    chk = getattr(self, chk_name, None)
                    if chk: chk.blockSignals(True); chk.setChecked(False); chk.blockSignals(False)
                return
            setattr(self.engine, engine_attr, True)
        return _toggle

    def _on_tc_folder_changed(self, idx):
        self.tc_custom_folder_ed.setVisible(idx == 3)
        # idx==2: 각 기능이 자신의 저장 폴더(save_dir)를 tag_dir로 직접 사용
        #         → tc_tag_target_dir을 비워서 각 기능이 자체 처리하도록 함
        self._sync_tc_tag_target()

    def get_tc_target_folder(self) -> str:
        """
        TC 태그 대상 폴더 경로 반환.
        이미 태그된 폴더가 존재하면 그 경로를 우선 반환
        (동일 TC에 계속 증적 누적을 위해).
        """
        idx = self.tc_folder_combo.currentIndex()
        eng = self.engine
        ts_date = datetime.now().strftime("%Y%m%d")

        if idx == 0:
            # TC-ID 폴더: base_dir/[vehicle_type]/날짜/tc_id
            tc = (eng.tc_id or "").strip()
            if not tc:
                return ""
            parts = [eng.base_dir]
            if eng.vehicle_type: parts.append(eng.vehicle_type)
            parts.append(ts_date)
            parent = os.path.join(*parts)
            # 기존 태그 폴더 우선 반환
            existing = eng._find_existing_tagged_folder(parent, tc)
            if existing:
                return existing
            return os.path.join(parent, tc)

        elif idx == 1:
            # 차종_버전 폴더: base_dir/vehicle_type
            vt = (eng.vehicle_type or "").strip()
            if not vt:
                return eng.base_dir
            parent = eng.base_dir
            existing = eng._find_existing_tagged_folder(parent, vt)
            if existing:
                return existing
            return os.path.join(parent, vt)

        elif idx == 2:
            # 각 기능 폴더: 빈 문자열 반환 → 각 기능이 자체 save_dir 사용
            return eng.output_dir or ""

        else:
            return self.tc_custom_folder_ed.text().strip()

    def _sync_tc_tag_target(self):
        idx = self.tc_folder_combo.currentIndex()
        # idx==2(Rec 폴더) : stale output_dir을 저장하지 않고 빈 문자열로 표시.
        # start_recording()에서 새 output_dir 확정 후 채워짐.
        self.engine.tc_folder_idx = idx
        if idx == 2:
            self.engine.tc_tag_target_dir = ''
        else:
            path = self.get_tc_target_folder()
            self.engine.tc_tag_target_dir = path

    def _on_tc_mode_changed(self, single: bool):
        """★ RadioButton 방식 — single=True: 개별, False: 범위."""
        self._tc_end_row.setVisible(not single)
        if not single:
            # 범위 모드: 끝 번호 = 시작번호+1 보장
            self._ensure_end_gt_start()
        self._upd_tc_id()

    def _on_start_changed(self, val: int):
        """시작번호 변경 시 끝번호가 시작번호 이하면 시작+1로 강제."""
        if not self.tc_mode_single.isChecked():
            self._ensure_end_gt_start()
        self._upd_tc_id()

    def _on_end_changed(self, val: int):
        """끝번호가 시작번호 이하면 시작+1로 강제."""
        self._ensure_end_gt_start()
        self._upd_tc_id()

    def _ensure_end_gt_start(self):
        start = self.tc_start_spin.value()
        end   = self.tc_end_spin.value()
        if end <= start:
            self.tc_end_spin.blockSignals(True)
            self.tc_end_spin.setValue(start + 1)
            self.tc_end_spin.blockSignals(False)

    def _upd_tc_id(self):
        prefix = self.tc_prefix_ed.text()
        start  = self.tc_start_spin.value()
        digits = self.tc_digits_spin.value()
        is_range = self.tc_mode_range.isChecked()
        if is_range:
            end = self.tc_end_spin.value()
            tc_val = f"{prefix}{start:0{digits}d}~{end}"
        else:
            tc_val = f"{prefix}{start:0{digits}d}"
        self.tc_preview_lbl.setText(f"TC-ID: {tc_val}")
        # ★ 수정: TC-ID 변경 시 이전 녹화의 tc_tag_target_dir 잔재 초기화
        if self.engine.tc_id != tc_val:
            self.engine.tc_tag_target_dir = ""
        self.engine.tc_id = tc_val
        self._upd_preview()
        self._sync_tc_tag_target()
        # ★ 번호 변경 시 0.5초 디바운스 자동 저장
        #   (휠 스크롤로 급격히 변경 시 매번 저장하면 과부하 방지)
        if not hasattr(self, "_autosave_timer"):
            from PyQt5.QtCore import QTimer as _QT
            self._autosave_timer = _QT(self)
            self._autosave_timer.setSingleShot(True)
            self._autosave_timer.timeout.connect(self._autosave_path)
        self._autosave_timer.start(500)   # 500ms 디바운스

    def _autosave_path(self):
        """TC 번호 변경 후 0.5초 디바운스 자동 저장."""
        self._save()
        self.result_lbl.setText("💾 자동 저장")
        self.result_lbl.setStyleSheet("color:#7bc8e0;font-size:11px;")
        from PyQt5.QtCore import QTimer as _QT
        _QT.singleShot(1500, lambda: self.result_lbl.setText(""))

    # ── 동적 경로 항목 ────────────────────────────────────────────────────────
    def _rebuild_drag_list(self):
        items = []
        vt = self.engine.vehicle_type or '미입력'
        tc = self.engine.tc_id        or '미설정'
        items.append({'key': '__vehicle__', 'label': f"🚗 차종_버전  [{vt}]",   'fixed': False})
        items.append({'key': '__date__',    'label': '📅 YYYYMMDD  (자동)',     'fixed': False})
        items.append({'key': '__tcid__',    'label': f"🔢 TC-ID  [{tc}]",       'fixed': False})
        for i, seg in enumerate(self._seg_rows):
            items.append({'key': f'seg_{i}', 'label': f"➕ {seg.get('val','') or '(비어있음)'}", 'fixed': False})
        items.append({'key': '__suffix__', 'label': '📁 Rec_HHMMSS / blackout / … (자동)', 'fixed': False})
        self._drag_list.set_items(items)

    def _add_segment(self, value: str = ""):
        idx   = len(self._seg_rows)
        row_w = QWidget(); row_w.setStyleSheet("background:transparent;")
        row_l = QHBoxLayout(row_w); row_l.setContentsMargins(0,0,0,0); row_l.setSpacing(5)
        num_lbl = QLabel(f"항목 {idx+1}:"); num_lbl.setStyleSheet("color:#aaa;font-size:10px;min-width:46px;")
        ed = QLineEdit(str(value) if value else "")
        ed.setPlaceholderText("추가 폴더명 (예: Feature_A)")
        ed.setStyleSheet("QLineEdit{background:#1a1a3a;color:#ccd;border:1px solid #334;border-radius:3px;padding:3px 6px;font-size:11px;}")
        del_btn = QPushButton("✕"); del_btn.setFixedSize(24, 24)
        del_btn.setStyleSheet("QPushButton{background:#3a1a1a;color:#f88;border:none;border-radius:3px;}QPushButton:hover{background:#7f2020;}")
        row_l.addWidget(num_lbl); row_l.addWidget(ed, 1); row_l.addWidget(del_btn)
        seg_entry = {'row_w': row_w, 'ed': ed, 'lbl': num_lbl, 'val': value}
        self._seg_rows.append(seg_entry)
        self._extra_lay.addWidget(row_w)
        ed.textChanged.connect(lambda t, s=seg_entry: self._on_seg_text(s, t))
        del_btn.clicked.connect(lambda _=False, s=seg_entry: self._del_segment(s))
        self._rebuild_drag_list(); self._upd_preview()

    def _on_seg_text(self, seg_entry, text):
        seg_entry['val'] = text; self._upd_extra_engine(); self._rebuild_drag_list(); self._upd_preview()

    def _del_segment(self, seg_entry):
        if seg_entry not in self._seg_rows: return
        self._seg_rows.remove(seg_entry)
        w = seg_entry['row_w']
        self._extra_lay.removeWidget(w); w.setParent(None); w.deleteLater()
        for i, s in enumerate(self._seg_rows): s['lbl'].setText(f"항목 {i+1}:")
        self._upd_extra_engine(); self._rebuild_drag_list(); self._upd_preview()

    def _upd_extra_engine(self):
        self.engine.extra_segments = [s['val'] for s in self._seg_rows if s['val'].strip()]

    def _upd_preview(self):
        vv  = self.vehicle_ed.text().strip() if hasattr(self, 'vehicle_ed') else ""
        tc  = self.engine.tc_id
        segs = [s['val'].strip() for s in self._seg_rows if s.get('val','').strip()]
        lines = []
        for label, tc_flag in [
            ("⏺ Recording (TC ON)",   True),
            ("⏺ Recording (TC OFF)",  False),
            ("🎬 수동녹화 (TC ON)",   True),
            ("⚡ 블랙아웃 (TC ON)",   True),
            ("📸 캡처 (TC ON)",       True),
        ]:
            if tc_flag:
                parts = ["bltn_rec"]
                if vv: parts.append(vv)
                parts.append("YYYYMMDD")
                if tc: parts.append(tc)
                parts.extend(segs)
            else:
                parts = ["bltn_rec", "YYYYMMDD"]
            lines.append(f"{label}: ~/{'/'.join(parts)}/<기능폴더>")
        if hasattr(self, 'preview_lbl'):
            self.preview_lbl.setText("경로 미리보기:\n" + "\n".join(lines))

    # ── 저장 / 복원 ───────────────────────────────────────────────────────────
    def _save(self):
        segs = [s['val'].strip() for s in self._seg_rows if s['val'].strip()]
        # root_dir: root_dir_ed 위젯 있으면 그 값, 없으면 engine.base_dir
        _root = ""
        if hasattr(self, 'root_dir_ed'):
            _root = self.root_dir_ed.text().strip()
        if not _root:
            _root = getattr(self.engine, 'base_dir', '')
        self.db.set_path_settings(
            vehicle_type  = self.vehicle_ed.text().strip(),
            tc_id         = self.engine.tc_id,
            extra_segments = segs,
            tc_rec        = self.tc_rec_chk.isChecked(),
            tc_manual     = self.tc_manual_chk.isChecked(),
            tc_blackout   = self.tc_blackout_chk.isChecked(),
            tc_capture    = self.tc_capture_chk.isChecked(),
            tc_folder_idx = self.tc_folder_combo.currentIndex(),
            root_dir      = _root,
        )
        # ★ 핵심 수정: DB 저장 직후 engine 플래그 즉시 동기화
        #   누락 시 체크박스 ON 후 저장해도 engine.tc_*_enabled=False 유지
        #   → _build_path()가 기본 경로 사용 → 사용자 지정 경로에 저장 안 됨
        self.engine.vehicle_type        = self.vehicle_ed.text().strip()
        self.engine.extra_segments      = segs
        if _root: self.engine.base_dir  = _root  # ★ base_dir 즉시 동기화
        self.engine.tc_rec_enabled      = self.tc_rec_chk.isChecked()
        self.engine.tc_manual_enabled   = self.tc_manual_chk.isChecked()
        self.engine.tc_blackout_enabled = self.tc_blackout_chk.isChecked()
        self.engine.tc_capture_enabled  = self.tc_capture_chk.isChecked()
        self._sync_tc_tag_target()
        self.result_lbl.setText("✅ 저장 완료")
        self.result_lbl.setStyleSheet("color:#2ecc71;font-size:11px;")
        QTimer.singleShot(2500, lambda: self.result_lbl.setText(""))

    def load_from_db(self):
        ps = self.db.get_path_settings()
        self.vehicle_ed.setText(ps.get('vehicle_type', ''))
        self.engine.vehicle_type = ps.get('vehicle_type', '')
        tc_raw = ps.get('tc_id', '')
        m_range = re.match(r'^(.*?)(\d+)~(\d+)$', tc_raw)
        m_single = re.match(r'^(.*?)(\d+)$', tc_raw)
        if m_range:
            self.tc_prefix_ed.setText(m_range.group(1))
            self.tc_start_spin.setValue(int(m_range.group(2)))
            self.tc_end_spin.setValue(int(m_range.group(3)))
            self.tc_digits_spin.setValue(max(1, len(m_range.group(2))))
            self.tc_mode_range.setChecked(True)
        elif m_single:
            self.tc_prefix_ed.setText(m_single.group(1))
            self.tc_start_spin.setValue(int(m_single.group(2)))
            self.tc_digits_spin.setValue(max(1, len(m_single.group(2))))
            self.tc_mode_single.setChecked(True)
        else:
            self.tc_prefix_ed.setText(tc_raw)
        for s in list(self._seg_rows): self._del_segment(s)
        for seg in ps.get('extra_segments', []):
            self._add_segment(seg)
        self.tc_rec_chk.setChecked(     ps.get('tc_rec',      False))
        self.tc_manual_chk.setChecked(  ps.get('tc_manual',   False))
        self.tc_blackout_chk.setChecked(ps.get('tc_blackout', False))
        self.tc_capture_chk.setChecked( ps.get('tc_capture',  False))
        self.engine.tc_rec_enabled      = ps.get('tc_rec',      False)
        self.engine.tc_manual_enabled   = ps.get('tc_manual',   False)
        self.engine.tc_blackout_enabled = ps.get('tc_blackout', False)
        self.engine.tc_capture_enabled  = ps.get('tc_capture',  False)
        # tc_folder_combo 복원
        fi = ps.get('tc_folder_idx', 0)
        self.tc_folder_combo.blockSignals(True)
        self.tc_folder_combo.setCurrentIndex(fi)
        self.tc_folder_combo.blockSignals(False)
        self.tc_custom_folder_ed.setVisible(fi == 3)
        self._upd_preview(); self._sync_tc_tag_target()
        # ★ engine.base_dir 반영
        root = ps.get('root_dir', '') or ''
        if root: self.engine.base_dir = root
        elif hasattr(self, 'root_dir_ed'):
            ui_root = self.root_dir_ed.text().strip()
            if ui_root: self.engine.base_dir = ui_root

class AutoClickPanel(QWidget):
    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine = engine; self.signals = signals
        signals.ac_count_changed.connect(lambda n: self.lcd.display(n))
        self._build()

    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        # 간격 설정
        ig = QGroupBox("클릭 간격"); il = QGridLayout(ig); il.setSpacing(6)
        il.addWidget(QLabel("간격 (초):"), 0, 0)
        self.interval_spin = _make_spinbox(0.1, 3600, 1, 0.1, 1, "", 90)
        self.interval_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'ac_interval', v))
        il.addWidget(self.interval_spin, 0, 1)

        pr = QHBoxLayout(); pr.setSpacing(4)
        for lbl, val in [("0.1s",.1),("0.5s",.5),("1s",1.),("5s",5.),("10s",10.)]:
            b = QPushButton(lbl); b.setFixedSize(44, 22)
            b.setStyleSheet(
                "QPushButton{background:#1e2a3a;border:1px solid #3a4a6a;"
                "color:#ccd;font-size:10px;border-radius:3px;}")
            b.clicked.connect(lambda _, v=val: self.interval_spin.setValue(v))
            pr.addWidget(b)
        il.addLayout(pr, 1, 0, 1, 2)

        adj_g = QGroupBox("간격 조절 (빠른 증감)")
        adj_v = QVBoxLayout(adj_g); adj_v.setSpacing(5)
        plus_row = QHBoxLayout(); plus_row.setSpacing(6)
        minus_row = QHBoxLayout(); minus_row.setSpacing(6)
        for delta, label in [(10,"+10초"),(60,"+1분"),(600,"+10분")]:
            b = QPushButton(label); b.setFixedHeight(28)
            b.setStyleSheet(
                "QPushButton{background:#1a3a1a;color:#8fa;border:1px solid #2a6a2a;"
                "border-radius:4px;font-size:10px;font-weight:bold;}"
                "QPushButton:hover{background:#225a22;}")
            b.clicked.connect(
                lambda _, d=delta: self.interval_spin.setValue(
                    min(3600, self.interval_spin.value()+d)))
            plus_row.addWidget(b)
        for delta, label in [(10,"-10초"),(60,"-1분"),(600,"-10분")]:
            b = QPushButton(label); b.setFixedHeight(28)
            b.setStyleSheet(
                "QPushButton{background:#3a1a1a;color:#f88;border:1px solid #6a2a2a;"
                "border-radius:4px;font-size:10px;font-weight:bold;}"
                "QPushButton:hover{background:#5a1a1a;}")
            b.clicked.connect(
                lambda _, d=delta: self.interval_spin.setValue(
                    max(0.1, self.interval_spin.value()-d)))
            minus_row.addWidget(b)
        adj_v.addLayout(plus_row); adj_v.addLayout(minus_row)
        il.addWidget(adj_g, 2, 0, 1, 2)
        v.addWidget(ig)

        # 카운터
        cg = QGroupBox("클릭 카운터"); cl = QGridLayout(cg)
        from PyQt5.QtWidgets import QLCDNumber
        self.lcd = QLCDNumber(8)
        self.lcd.setSegmentStyle(QLCDNumber.Flat)
        self.lcd.setFixedHeight(44)
        self.lcd.setStyleSheet(
            "background:#0d1520;border:1px solid #336;color:#2ecc71;")
        cl.addWidget(self.lcd, 0, 0, 1, 2)
        rst = QPushButton("카운터 초기화"); rst.setFixedHeight(26)
        rst.clicked.connect(self.engine.reset_ac_count)
        cl.addWidget(rst, 1, 0, 1, 2)
        v.addWidget(cg)

        # 제어
        ctrl = QGroupBox("제어"); ctl = QVBoxLayout(ctrl); ctl.setSpacing(6)
        self.btn_start = QPushButton("▶  시작  [Ctrl+Alt+A]")
        self.btn_start.setStyleSheet(
            "QPushButton{background:#2980b9;color:white;font-size:12px;"
            "padding:7px;border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#3498db;}"
            "QPushButton:disabled{background:#1a2a3a;color:#4a6a8a;}")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop = QPushButton("■  정지  [Ctrl+Alt+S]")
        self.btn_stop.setStyleSheet(
            "QPushButton{background:#5a6a7a;color:white;font-size:12px;"
            "padding:7px;border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#7f8c8d;}"
            "QPushButton:disabled{background:#2a2a2a;color:#555;}")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)
        self.status_lbl = QLabel("● 정지")
        self.status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;")
        ctl.addWidget(self.btn_start); ctl.addWidget(self.btn_stop)
        ctl.addWidget(self.status_lbl)
        v.addWidget(ctrl)
        self._build_click_options(v)

    def _build_click_options(self, v):
        """클릭 타입 + 좌표 지정 옵션 그룹."""
        og = QGroupBox("클릭 옵션")
        ol = QVBoxLayout(og); ol.setSpacing(6)

        # 클릭 버튼 종류
        btn_row = QHBoxLayout(); btn_row.setSpacing(4)
        btn_row.addWidget(QLabel("버튼:"))
        self._btn_grp_btns = {}
        for label, key in [("좌클릭", "left"), ("우클릭", "right"), ("더블", "double")]:
            b = QPushButton(label); b.setCheckable(True)
            b.setFixedHeight(26)
            b.setStyleSheet(
                "QPushButton{background:#1e2a3a;border:1px solid #3a4a6a;"
                "color:#ccd;font-size:10px;border-radius:3px;}"
                "QPushButton:checked{background:#2980b9;color:white;"
                "border-color:#2980b9;}")
            self._btn_grp_btns[key] = b
            btn_row.addWidget(b)
        self._btn_grp_btns["left"].setChecked(True)
        for key, b in self._btn_grp_btns.items():
            b.clicked.connect(lambda _, k=key: self._on_btn_type(k))
        btn_row.addStretch()
        ol.addLayout(btn_row)

        # 좌표 지정
        pos_row = QHBoxLayout(); pos_row.setSpacing(4)
        self._use_pos_chk = QPushButton("📍 좌표 지정")
        self._use_pos_chk.setCheckable(True)
        self._use_pos_chk.setFixedHeight(26)
        self._use_pos_chk.setStyleSheet(
            "QPushButton{background:#1e2a3a;border:1px solid #3a4a6a;"
            "color:#ccd;font-size:10px;border-radius:3px;}"
            "QPushButton:checked{background:#1a5a2a;color:#7fdb9e;"
            "border-color:#2a7a3a;}")
        self._use_pos_chk.toggled.connect(self._on_use_pos_toggle)
        pos_row.addWidget(self._use_pos_chk)
        pos_row.addWidget(QLabel("X:"))
        self._x_spin = _make_spinbox(0, 9999, 0, 1, 1, "", 70)
        self._x_spin.setEnabled(False)
        pos_row.addWidget(self._x_spin)
        pos_row.addWidget(QLabel("Y:"))
        self._y_spin = _make_spinbox(0, 9999, 0, 1, 1, "", 70)
        self._y_spin.setEnabled(False)
        pos_row.addWidget(self._y_spin)
        cap_btn = QPushButton("🎯 현재 좌표 캡처")
        cap_btn.setFixedHeight(26)
        cap_btn.setStyleSheet(
            "QPushButton{background:#1a3a4a;color:#7bc8e0;"
            "border:1px solid #2a5a7a;border-radius:3px;font-size:10px;}"
            "QPushButton:hover{background:#1e4a5a;}")
        cap_btn.clicked.connect(self._capture_cursor_pos)
        pos_row.addWidget(cap_btn)
        pos_row.addStretch()
        ol.addLayout(pos_row)

        # 상태 안내
        self._click_hint = QLabel("ℹ️  클릭 방식: win32api → ctypes → pynput 순 자동 선택")
        self._click_hint.setStyleSheet("color:#667;font-size:9px;")
        self._click_hint.setWordWrap(True)
        ol.addWidget(self._click_hint)

        v.addWidget(og)

    def _on_btn_type(self, key):
        """클릭 타입 선택 (좌/우/더블 — 단일 선택)."""
        for k, b in self._btn_grp_btns.items():
            b.setChecked(k == key)
        if key == "double":
            self.engine.ac_double = True
            self.engine.ac_btn    = "left"
        else:
            self.engine.ac_double = False
            self.engine.ac_btn    = key

    def _on_use_pos_toggle(self, checked):
        self._x_spin.setEnabled(checked)
        self._y_spin.setEnabled(checked)
        self.engine.ac_use_pos = checked
        if checked:
            self.engine.ac_pos_x = int(self._x_spin.value())
            self.engine.ac_pos_y = int(self._y_spin.value())
        else:
            self.engine.ac_pos_x = None
            self.engine.ac_pos_y = None
        self._x_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'ac_pos_x', int(v)))
        self._y_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'ac_pos_y', int(v)))

    def _capture_cursor_pos(self):
        """현재 마우스 커서 위치를 X/Y 스핀박스에 채우기."""
        try:
            import ctypes
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            x, y = pt.x, pt.y
        except Exception:
            if PYNPUT_AVAILABLE:
                x, y = pynput_mouse.Controller().position
            else:
                x, y = 0, 0
        self._x_spin.setValue(x)
        self._y_spin.setValue(y)
        self.engine.ac_pos_x = x
        self.engine.ac_pos_y = y
        self._click_hint.setText(f"📍 캡처됨: X={x}, Y={y}")

    def _on_start(self):
        # 좌표 업데이트
        if self._use_pos_chk.isChecked():
            self.engine.ac_pos_x = int(self._x_spin.value())
            self.engine.ac_pos_y = int(self._y_spin.value())
        else:
            self.engine.ac_pos_x = None
            self.engine.ac_pos_y = None
        self.engine.start_ac()
        self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True)
        self.status_lbl.setText("● 실행 중")
        self.status_lbl.setStyleSheet("color:#2ecc71;font-weight:bold;")

    def _on_stop(self):
        self.engine.stop_ac()
        self.btn_start.setEnabled(True); self.btn_stop.setEnabled(False)
        self.status_lbl.setText("● 정지")
        self.status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;")


# =============================================================================
#  MacroPanel
# =============================================================================
class MacroPanel(QWidget):
    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine = engine; self.signals = signals
        self._slots: list = []
        self._active_slot = 0
        signals.macro_step_rec.connect(self._on_step)
        self._build()
        QTimer.singleShot(0, self._init_slots)

    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        # ── 슬롯 관리 ─────────────────────────────────────────────────────
        sg = QGroupBox("🗂 매크로 슬롯"); sl = QVBoxLayout(sg); sl.setSpacing(5)
        sc = QHBoxLayout(); sc.setSpacing(6)
        sc.addWidget(QLabel("슬롯:"))
        self.slot_cb = QComboBox()
        self.slot_cb.setStyleSheet(
            "QComboBox{background:#0f2a1a;color:#afffcf;border:1px solid #2a8a5a;"
            "border-radius:3px;font-weight:bold;font-size:11px;min-width:100px;}")
        self.slot_cb.currentIndexChanged.connect(self._on_slot_changed)
        sc.addWidget(self.slot_cb, 1)
        for lbl, fn, c in [("＋","_add_slot","#1a4a2a"),
                            ("－","_del_slot","#3a1a1a"),
                            ("✏","_rename_slot","#1a2a3a")]:
            b = QPushButton(lbl); b.setFixedSize(28, 26)
            b.setStyleSheet(
                f"QPushButton{{background:{c};color:#ddd;"
                "border:1px solid #4a6a4a;border-radius:3px;}}")
            b.clicked.connect(getattr(self, fn)); sc.addWidget(b)
        sl.addLayout(sc)
        self.slot_info = QLabel("슬롯 1/1  |  0 스텝")
        self.slot_info.setStyleSheet(
            "color:#556;font-size:10px;font-family:monospace;")
        sl.addWidget(self.slot_info); v.addWidget(sg)

        # ── 기록 ──────────────────────────────────────────────────────────
        rg = QGroupBox("📍 이벤트 기록"); rl = QVBoxLayout(rg); rl.setSpacing(6)
        evt_row = QHBoxLayout(); evt_row.setSpacing(10)
        evt_row.addWidget(QLabel("기록 대상:"))
        self.rec_click_chk = QCheckBox("🖱 클릭")
        self.rec_drag_chk  = QCheckBox("↔ 드래그")
        self.rec_key_chk   = QCheckBox("⌨ 키보드")
        for chk in (self.rec_click_chk, self.rec_drag_chk, self.rec_key_chk):
            chk.setChecked(True)
            chk.setStyleSheet("QCheckBox{font-size:11px;color:#dde;spacing:4px;}")
            evt_row.addWidget(chk)
        evt_row.addStretch(); rl.addLayout(evt_row)

        rb = QHBoxLayout()
        self.rec_btn = QPushButton("⏺  기록 시작")
        self.rec_btn.setCheckable(True); self.rec_btn.setFixedHeight(32)
        self.rec_btn.setStyleSheet(
            "QPushButton{background:#1a4a2a;color:#afffcf;border:1px solid #2a8a5a;"
            "border-radius:5px;font-size:12px;font-weight:bold;}"
            "QPushButton:checked{background:#c0392b;color:#fff;border:2px solid #e74c3c;}")
        self.rec_btn.toggled.connect(self._on_rec_toggle)
        self.rec_st = QLabel("● 대기")
        self.rec_st.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")
        rb.addWidget(self.rec_btn, 1); rb.addWidget(self.rec_st)
        rl.addLayout(rb)
        self.last_evt_lbl = QLabel("마지막 이벤트: —")
        self.last_evt_lbl.setStyleSheet(
            "color:#f0c040;font-size:11px;font-family:monospace;")
        rl.addWidget(self.last_evt_lbl); v.addWidget(rg)

        # ── 스텝 테이블 ───────────────────────────────────────────────────
        tg = QGroupBox("📋 이벤트 스텝"); tv = QVBoxLayout(tg); tv.setSpacing(4)
        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(["#","타입","좌표/키","딜레이(s)","옵션"])
        hdr = self.tbl.horizontalHeader()
        for i, m in enumerate([QHeaderView.Fixed, QHeaderView.Fixed,
                                QHeaderView.Stretch, QHeaderView.Fixed, QHeaderView.Fixed]):
            hdr.setSectionResizeMode(i, m)
        self.tbl.setColumnWidth(0, 30); self.tbl.setColumnWidth(1, 58)
        self.tbl.setColumnWidth(3, 70); self.tbl.setColumnWidth(4, 60)
        self.tbl.setFixedHeight(180)
        self.tbl.setStyleSheet(
            "QTableWidget{background:#0a0a18;color:#ccc;font-size:10px;"
            "border:1px solid #1a2a3a;gridline-color:#1a2030;}"
            "QHeaderView::section{background:#0f1a2a;color:#7ab4d4;"
            "font-size:10px;border:none;padding:3px;}")
        self.tbl.itemChanged.connect(self._on_item_changed)
        tv.addWidget(self.tbl)

        tb = QHBoxLayout(); tb.setSpacing(4)
        for lbl, fn in [("↑","_step_up"),("↓","_step_dn")]:
            b = QPushButton(lbl); b.setFixedSize(28, 24)
            b.clicked.connect(getattr(self, fn)); tb.addWidget(b)
        tb.addStretch()
        for lbl, fn in [("선택 삭제","_del_step"),("전체 삭제","_clear_steps")]:
            b = QPushButton(lbl); b.setFixedHeight(24)
            b.clicked.connect(getattr(self, fn)); tb.addWidget(b)
        tv.addLayout(tb)

        br = QHBoxLayout(); br.addWidget(QLabel("일괄 딜레이:"))
        self.bulk_spin = _make_spinbox(0.0, 60, 0.5, 0.1, 2, "", 70)
        bb = QPushButton("적용"); bb.setFixedHeight(24)
        bb.clicked.connect(self._bulk_delay)
        br.addWidget(self.bulk_spin); br.addWidget(QLabel("초"))
        br.addWidget(bb); br.addStretch()
        tv.addLayout(br); v.addWidget(tg)

        # ── 실행 ──────────────────────────────────────────────────────────
        run = QGroupBox("▶ 실행"); rn = QGridLayout(run); rn.setSpacing(6)
        rn.addWidget(QLabel("반복:"), 0, 0)
        self.rep_spin = _make_spinbox(0, 9999, 1, 1, 0, "∞", 70)
        self.rep_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'macro_repeat', int(v)))
        rn.addWidget(self.rep_spin, 0, 1)
        rn.addWidget(QLabel("루프 간격(초):"), 1, 0)
        self.gap_spin = _make_spinbox(0, 60, 1, 0.5, 1, "", 70)
        self.gap_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'macro_gap', v))
        rn.addWidget(self.gap_spin, 1, 1)

        rb2 = QHBoxLayout(); rb2.setSpacing(6)
        self.run_btn = QPushButton("▶  실행"); self.run_btn.setFixedHeight(32)
        self.run_btn.setStyleSheet(
            "QPushButton{background:#2980b9;color:#fff;border:none;border-radius:5px;"
            "font-size:12px;font-weight:bold;}QPushButton:hover{background:#3498db;}"
            "QPushButton:disabled{background:#1a3a5a;color:#555;}")
        self.run_btn.clicked.connect(self._on_run)
        self.stop_btn = QPushButton("■  중단  [Ctrl+Alt+X]"); self.stop_btn.setFixedHeight(32)
        self.stop_btn.setStyleSheet(
            "QPushButton{background:#5a6a7a;color:#fff;border:none;border-radius:5px;"
            "font-size:12px;}QPushButton:hover{background:#7f8c8d;}"
            "QPushButton:disabled{background:#2a2a2a;color:#555;}")
        self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self._on_stop)
        self.run_st = QLabel("● 대기")
        self.run_st.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")
        rb2.addWidget(self.run_btn, 1); rb2.addWidget(self.stop_btn, 1)
        rn.addLayout(rb2, 2, 0, 1, 2); rn.addWidget(self.run_st, 3, 0, 1, 2)
        v.addWidget(run)

    # ── 슬롯 ─────────────────────────────────────────────────────────────
    def _init_slots(self):
        if not self._slots:
            self._slots.append({'title':'슬롯 1','steps':[]})
            self.slot_cb.blockSignals(True)
            self.slot_cb.addItem('슬롯 1')
            self.slot_cb.blockSignals(False)
        self._active_slot = 0; self.slot_cb.setCurrentIndex(0)
        self._sync(); self._info_upd()
        self._sync_engine_slots()   # ★ 엔진 캐시 초기 동기화

    def _sync(self):
        if 0 <= self._active_slot < len(self._slots):
            self.engine.macro_steps.clear()
            self.engine.macro_steps.extend(self._slots[self._active_slot]['steps'])

    def _save_cur(self):
        if 0 <= self._active_slot < len(self._slots):
            self._slots[self._active_slot]['steps'] = list(self.engine.macro_steps)
            # ★ engine 슬롯 캐시 동기화 (커널에서 slot 번호로 접근 가능하도록)
            self._sync_engine_slots()

    def _sync_engine_slots(self):
        """engine._macro_slots 캐시를 현재 _slots 데이터로 갱신."""
        self.engine._macro_slots = [
            {'title': s['title'],
             'steps': list(s['steps'])}
            for s in self._slots
        ]

    def _info_upd(self):
        n = len(self._slots); idx = self._active_slot
        nm = self._slots[idx]['title'] if 0 <= idx < n else "—"
        self.slot_info.setText(
            f"{nm}  |  슬롯 {idx+1}/{n}  |  {len(self.engine.macro_steps)} 스텝")

    def _on_slot_changed(self, idx):
        self._save_cur(); self._active_slot = idx
        self._sync(); self._rebuild_tbl(); self._info_upd()
        # _save_cur 내부에서 이미 _sync_engine_slots() 호출됨

    def _add_slot(self):
        t = f"슬롯 {len(self._slots)+1}"
        self._slots.append({'title':t,'steps':[]})
        self.slot_cb.blockSignals(True); self.slot_cb.addItem(t)
        self.slot_cb.blockSignals(False)
        self.slot_cb.setCurrentIndex(len(self._slots)-1)
        self._sync_engine_slots()   # ★ 슬롯 추가 후 동기화

    def _del_slot(self):
        if len(self._slots) <= 1: return
        idx = self._active_slot; self._slots.pop(idx)
        self.slot_cb.blockSignals(True); self.slot_cb.removeItem(idx)
        self.slot_cb.blockSignals(False)
        new = max(0, idx-1); self._active_slot = new
        self.slot_cb.setCurrentIndex(new)
        self._sync(); self._rebuild_tbl(); self._info_upd()
        self._sync_engine_slots()   # ★ 슬롯 삭제 후 동기화

    def _rename_slot(self):
        idx = self._active_slot
        if not 0 <= idx < len(self._slots): return
        new, ok = QInputDialog.getText(
            self, "슬롯 이름", "새 이름:", text=self._slots[idx]['title'])
        if ok and new.strip():
            self._slots[idx]['title'] = new.strip()
            self.slot_cb.blockSignals(True)
            self.slot_cb.setItemText(idx, new.strip())
            self.slot_cb.blockSignals(False)
            self._info_upd()

    # ── 기록 ─────────────────────────────────────────────────────────────
    def _on_rec_toggle(self, recording):
        if recording:
            self.rec_btn.setText("⏹  기록 중단")
            self.rec_st.setText("● 기록 중")
            self.rec_st.setStyleSheet("color:#e74c3c;font-size:11px;font-weight:bold;")
            self.engine._rec_filter = {
                'click': self.rec_click_chk.isChecked(),
                'drag':  self.rec_drag_chk.isChecked(),
                'key':   self.rec_key_chk.isChecked(),
            }
            self.engine.macro_start_recording()
        else:
            self.rec_btn.setText("⏺  기록 시작")
            self.rec_st.setText("● 완료")
            self.rec_st.setStyleSheet("color:#2ecc71;font-size:11px;font-weight:bold;")
            self.engine.macro_stop_recording()
            QTimer.singleShot(200, self._make_editable)

    def _on_step(self, step):
        self._append_row(step, editable=False)
        self.last_evt_lbl.setText(step.summary())
        self._info_upd()

    # ── 테이블 ───────────────────────────────────────────────────────────
    @staticmethod
    def _type_color(kind):
        return {'click':'#7bc8e0','drag':'#f0c040','key':'#afffcf'}.get(kind,'#ccc')

    def _append_row(self, step, editable=True):
        ef = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable
        rf = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        self.tbl.blockSignals(True)
        r = self.tbl.rowCount(); self.tbl.insertRow(r)
        items = [
            (str(r+1), rf, Qt.AlignCenter, QColor("#556")),
            (step.kind.upper(), rf, Qt.AlignCenter, QColor(self._type_color(step.kind))),
            (self._step_coord_str(step), ef if editable else rf,
             Qt.AlignLeft|Qt.AlignVCenter,
             QColor("#ddd") if editable else QColor("#999")),
            (f"{step.delay:.3f}", ef if editable else rf,
             Qt.AlignCenter,
             QColor("#ddd") if editable else QColor("#999")),
        ]
        for c, (text, flags, align, color) in enumerate(items):
            it = QTableWidgetItem(text)
            it.setFlags(flags); it.setTextAlignment(align); it.setForeground(color)
            self.tbl.setItem(r, c, it)
        opt = ""
        if step.kind == 'click':
            opt = step.button[:1].upper() + (" x2" if step.double else "")
        elif step.kind == 'drag':
            opt = step.button[:1].upper()
        it4 = QTableWidgetItem(opt)
        it4.setFlags(rf); it4.setTextAlignment(Qt.AlignCenter)
        it4.setForeground(QColor("#888"))
        self.tbl.setItem(r, 4, it4)
        self.tbl.scrollToBottom(); self.tbl.blockSignals(False)

    @staticmethod
    def _step_coord_str(step) -> str:
        if step.kind == 'click': return f"({step.x},{step.y})"
        if step.kind == 'drag':  return f"({step.x},{step.y})→({step.x2},{step.y2})"
        if step.kind == 'key':   return step.key_str
        return ""

    def _make_editable(self):
        ef = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable
        self.tbl.blockSignals(True)
        for r in range(self.tbl.rowCount()):
            for c in (2, 3):
                it = self.tbl.item(r, c)
                if it: it.setFlags(ef); it.setForeground(QColor("#ddd"))
        self.tbl.blockSignals(False)

    def _rebuild_tbl(self):
        self.tbl.blockSignals(True); self.tbl.setRowCount(0)
        self.tbl.blockSignals(False)
        for step in self.engine.macro_steps:
            self._append_row(step, editable=True)

    def _on_item_changed(self, item):
        r = item.row(); c = item.column()
        if r >= len(self.engine.macro_steps): return
        step = self.engine.macro_steps[r]
        try:
            txt = item.text().strip()
            if c == 3:
                step.delay = max(0.0, float(txt))
            elif c == 2:
                if step.kind == 'click':
                    m = re.fullmatch(r'\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)', txt)
                    if m: step.x, step.y = int(m.group(1)), int(m.group(2))
                elif step.kind == 'drag':
                    m = re.fullmatch(
                        r'\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)→\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)', txt)
                    if m:
                        step.x, step.y   = int(m.group(1)), int(m.group(2))
                        step.x2, step.y2 = int(m.group(3)), int(m.group(4))
                elif step.kind == 'key':
                    step.key_str = txt
        except: pass

    def _del_step(self):
        rows = sorted({i.row() for i in self.tbl.selectedItems()}, reverse=True)
        self.tbl.blockSignals(True)
        for r in rows:
            self.tbl.removeRow(r)
            if r < len(self.engine.macro_steps): self.engine.macro_steps.pop(r)
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 0)
            if it: it.setText(str(r+1))
        self.tbl.blockSignals(False); self._info_upd()

    def _clear_steps(self):
        self.tbl.blockSignals(True); self.tbl.setRowCount(0)
        self.tbl.blockSignals(False); self.engine.macro_clear(); self._info_upd()

    def _step_up(self):
        r = self.tbl.currentRow(); s = self.engine.macro_steps
        if r <= 0 or r >= len(s): return
        s[r-1], s[r] = s[r], s[r-1]
        self._rebuild_tbl(); self.tbl.setCurrentCell(r-1, 0)

    def _step_dn(self):
        r = self.tbl.currentRow(); s = self.engine.macro_steps
        if r < 0 or r >= len(s)-1: return
        s[r], s[r+1] = s[r+1], s[r]
        self._rebuild_tbl(); self.tbl.setCurrentCell(r+1, 0)

    def _bulk_delay(self):
        d = self.bulk_spin.value(); self.tbl.blockSignals(True)
        for i, step in enumerate(self.engine.macro_steps):
            step.delay = d
            it = self.tbl.item(i, 3)
            if it: it.setText(f"{d:.3f}")
        self.tbl.blockSignals(False)

    def _on_run(self):
        if not self.engine.macro_steps:
            self.signals.status_message.emit("[Macro] 스텝이 없습니다"); return
        self.engine.macro_start_run()
        self.run_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        self.run_st.setText("● 실행 중")
        self.run_st.setStyleSheet("color:#2ecc71;font-size:11px;font-weight:bold;")
        self._watch = QTimer(self)
        self._watch.timeout.connect(self._check_done)
        self._watch.start(300)

    def _check_done(self):
        if not self.engine.macro_running:
            self._watch.stop()
            self.run_btn.setEnabled(True); self.stop_btn.setEnabled(False)
            self.run_st.setText("● 완료")
            self.run_st.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")

    def _on_stop(self):
        self.engine.macro_stop_run()
        self.run_btn.setEnabled(True); self.stop_btn.setEnabled(False)
        self.run_st.setText("● 중단")
        self.run_st.setStyleSheet("color:#e74c3c;font-size:11px;font-weight:bold;")

    def get_slots_data(self) -> list:
        self._save_cur()
        return [{'title': s['title'],
                 'steps': [st.to_dict() for st in s['steps']]}
                for s in self._slots]

    def set_slots_data(self, data: list):
        self._slots.clear()
        self.slot_cb.blockSignals(True); self.slot_cb.clear()
        for s in data:
            steps = [MacroStep.from_dict(st) for st in s.get('steps', [])]
            self._slots.append({'title': s['title'], 'steps': steps})
            self.slot_cb.addItem(s['title'])
        self.slot_cb.blockSignals(False)
        self._active_slot = 0; self.slot_cb.setCurrentIndex(0)
        self._sync(); self._rebuild_tbl(); self._info_upd()
        self._sync_engine_slots()   # ★ DB 복원 후 엔진 캐시 동기화


# =============================================================================
#  SchedulePanel
# =============================================================================
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
        self.mac_chk = QCheckBox("매크로도 실행"); self.mac_chk.setChecked(False)
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
        e = ScheduleEntry(start_dt, stop_dt, actions,
                          self.mac_rep.value(), self.mac_gap.value())
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
class MemoPanel(QWidget):
    overlay_changed = pyqtSignal()

    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine = engine; self.signals = signals
        self._editors: list = []; self._overlay_rows: list = []
        self._build()
        signals.rec_started.connect(lambda _: QTimer.singleShot(
            500, lambda: self._export_txt_to_rec_dir()))

    def _build(self):
        root = QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)

        # ★ QSplitter: 상단(설정+버튼) / 하단(에디터+오버레이) 크기 조절
        splitter = QSplitter(Qt.Vertical)
        splitter.setStyleSheet(
            "QSplitter::handle{background:#1a2a3a;height:5px;}"
            "QSplitter::handle:hover{background:#2a5aaa;cursor:ns-resize;}")
        root.addWidget(splitter)

        top_w = QWidget()
        v = QVBoxLayout(top_w); v.setContentsMargins(0,4,0,4); v.setSpacing(6)
        splitter.addWidget(top_w)

        # 폰트/타임스탬프 설정
        cg = QGroupBox("📝 현재 탭 설정")
        cl = QVBoxLayout(cg); cl.setSpacing(6)
        font_row = QHBoxLayout(); font_row.setSpacing(6)
        font_row.addWidget(QLabel("에디터 글꼴:"))
        self.font_spin = QSpinBox(); self.font_spin.setRange(8,36); self.font_spin.setValue(11)
        self.font_spin.setFixedWidth(52)
        self.font_spin.setStyleSheet(
            "QSpinBox{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:3px;font-weight:bold;font-size:13px;}")
        self.font_sl = QSlider(Qt.Horizontal); self.font_sl.setRange(8,36); self.font_sl.setValue(11)
        self.font_sl.setStyleSheet(
            "QSlider::groove:horizontal{background:#1a2030;height:6px;border-radius:3px;}"
            "QSlider::handle:horizontal{background:#f0c040;width:16px;height:16px;"
            "margin-top:-5px;margin-bottom:-5px;border-radius:8px;}"
            "QSlider::sub-page:horizontal{background:#5a5a20;border-radius:3px;}")
        self.font_spin.valueChanged.connect(
            lambda v: (self.font_sl.blockSignals(True), self.font_sl.setValue(v),
                       self.font_sl.blockSignals(False), self._apply_font_cur(v)))
        self.font_sl.valueChanged.connect(
            lambda v: (self.font_spin.blockSignals(True), self.font_spin.setValue(v),
                       self.font_spin.blockSignals(False), self._apply_font_cur(v)))
        font_row.addWidget(self.font_sl, 1); font_row.addWidget(self.font_spin)
        for lbl, sz in [("S",10),("M",13),("L",16),("XL",20)]:
            b = QPushButton(lbl); b.setFixedSize(28, 22)
            b.setStyleSheet(
                "QPushButton{background:#2a2a1a;color:#f0c040;"
                "border:1px solid #4a4a20;border-radius:3px;font-size:10px;}")
            b.clicked.connect(lambda _, s=sz: self.font_spin.setValue(s))
            font_row.addWidget(b)
        cl.addLayout(font_row)
        ts_row = QHBoxLayout()
        self.ts_chk = QCheckBox("더블클릭 타임스탬프 삽입"); self.ts_chk.setChecked(True)
        self.ts_chk.setStyleSheet("font-size:11px;font-weight:bold;color:#7bc8e0;")
        self.ts_chk.toggled.connect(self._on_ts_toggled)
        ts_row.addWidget(self.ts_chk); ts_row.addWidget(QLabel("(우클릭: 제거)"))
        ts_row.addStretch(); cl.addLayout(ts_row)
        v.addWidget(cg)

        # 탭 컨트롤 버튼
        tc = QHBoxLayout(); tc.setSpacing(6)
        btns = [
            ("＋ 탭","_add_tab_new","#1a3a1a","#8fa","#2a6a2a"),
            ("－ 탭","_del_tab","#3a1a1a","#f88","#6a2a2a"),
            ("현재 탭 지우기","_clear_cur","#1a1a3a","#aaa","#334"),
            ("📄 .txt 내보내기","_export_txt","#1a2a3a","#7bc8e0","#2a4a6a"),
            ("📁 녹화폴더 저장","_export_to_rec","#1a2a1a","#8fa","#2a6a2a"),
        ]
        for text, fn, bg, fg, border in btns:
            b = QPushButton(text); b.setFixedHeight(26)
            b.setStyleSheet(
                f"QPushButton{{background:{bg};color:{fg};"
                f"border:1px solid {border};border-radius:4px;font-size:11px;}}")
            b.clicked.connect(getattr(self, fn)); tc.addWidget(b)
        tc.addStretch(); v.addLayout(tc)

        # ★ 하단 위젯 (에디터 탭 + 오버레이 설정)
        bottom_w = QWidget()
        vb = QVBoxLayout(bottom_w); vb.setContentsMargins(0,4,0,0); vb.setSpacing(4)
        splitter.addWidget(bottom_w)
        splitter.setSizes([160, 400])
        v = vb   # 이하 v는 bottom_w 레이아웃으로 사용

        # 탭 위젯
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            "QTabWidget::pane{border:1px solid #2a2a4a;background:#0d0d1e;}"
            "QTabBar::tab{background:#1a1a3a;color:#888;border:1px solid #334;"
            "border-bottom:none;border-radius:4px 4px 0 0;padding:4px 10px;"
            "font-size:11px;min-width:60px;}"
            "QTabBar::tab:selected{background:#2a2a5a;color:#dde;border-color:#446;}"
            "QTabBar::tab:hover{background:#22224a;color:#bbd;}")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._add_tab("메모 1"); v.addWidget(self.tabs)

        # 오버레이 설정
        ovg = QGroupBox("🖼 오버레이 설정"); ovl = QVBoxLayout(ovg); ovl.setSpacing(4)
        self._ov_cont = QWidget()
        self._ov_lay  = QVBoxLayout(self._ov_cont)
        self._ov_lay.setContentsMargins(0,0,0,0); self._ov_lay.setSpacing(3)
        ovl.addWidget(self._ov_cont)
        add_ov = QPushButton("＋ 오버레이 추가"); add_ov.setFixedHeight(26)
        add_ov.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:4px;font-size:11px;}")
        add_ov.clicked.connect(self._add_overlay); ovl.addWidget(add_ov)
        v.addWidget(ovg)
        self._rebuild_ov_rows()

    def _add_tab(self, title, content="", font_size=11, ts_enabled=True):
        ed = TimestampMemoEdit()
        ed.setPlaceholderText("메모 입력… (더블클릭: 타임스탬프 | 우클릭: 제거)")
        ed.setStyleSheet("background:#0d0d1e;color:#ffe;border:none;font-family:monospace;")
        ed.timestamp_enabled = ts_enabled
        f = ed.font(); f.setPointSize(max(8, font_size)); ed.setFont(f)
        if content: ed.setPlainText(content)
        ed.textChanged.connect(self._sync_texts)
        self._editors.append(ed); self.tabs.addTab(ed, title)
        while len(self.engine.memo_texts) < len(self._editors):
            self.engine.memo_texts.append("")
        self._upd_ov_max()

    def _sync_texts(self):
        for i, ed in enumerate(self._editors):
            if i < len(self.engine.memo_texts):
                self.engine.memo_texts[i] = ed.toPlainText()
            else:
                self.engine.memo_texts.append(ed.toPlainText())
        self.overlay_changed.emit()

    def _on_tab_changed(self, idx):
        if not 0 <= idx < len(self._editors): return
        ed = self._editors[idx]
        fs = ed.font().pointSize(); fs = fs if fs > 0 else 11
        for w in (self.font_spin, self.font_sl):
            w.blockSignals(True); w.setValue(fs); w.blockSignals(False)
        self.ts_chk.blockSignals(True)
        self.ts_chk.setChecked(ed.timestamp_enabled)
        self.ts_chk.blockSignals(False)

    def _apply_font_cur(self, size):
        idx = self.tabs.currentIndex()
        if 0 <= idx < len(self._editors):
            f = self._editors[idx].font(); f.setPointSize(max(8,size))
            self._editors[idx].setFont(f)

    def _on_ts_toggled(self, v):
        idx = self.tabs.currentIndex()
        if 0 <= idx < len(self._editors):
            self._editors[idx].timestamp_enabled = v

    def _add_tab_new(self):
        n = self.tabs.count() + 1
        self._add_tab(f"메모 {n}")
        self.tabs.setCurrentIndex(self.tabs.count()-1)

    def _del_tab(self):
        if self.tabs.count() <= 1: return
        idx = self.tabs.currentIndex()
        self.tabs.removeTab(idx)
        if idx < len(self._editors): self._editors.pop(idx)
        if idx < len(self.engine.memo_texts): self.engine.memo_texts.pop(idx)
        self._upd_ov_max(); self._on_tab_changed(self.tabs.currentIndex())

    def _clear_cur(self):
        idx = self.tabs.currentIndex()
        if idx < len(self._editors): self._editors[idx].clear()

    def _export_txt(self):
        idx = self.tabs.currentIndex()
        if not 0 <= idx < len(self._editors): return
        content   = self._editors[idx].toPlainText()
        tab_name  = self.tabs.tabText(idx)
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', tab_name)
        ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_dir  = os.path.join(self.engine.base_dir, "memo_export")
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, f"{safe_name}_{ts}.txt")
        try:
            with open(path, 'w', encoding='utf-8') as f: f.write(content)
            self.signals.status_message.emit(f"[메모] → {path}")
            open_folder(save_dir)
        except Exception as ex:
            self.signals.status_message.emit(f"[메모] 실패: {ex}")

    def _export_to_rec(self):
        self._export_txt_to_rec_dir()

    def _export_txt_to_rec_dir(self):
        save_dir = (self.engine.output_dir
                    if (self.engine.output_dir and os.path.isdir(self.engine.output_dir))
                    else os.path.join(self.engine.base_dir, "memo_export"))
        os.makedirs(save_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        lines_all = []
        for i in range(self.tabs.count()):
            if i >= len(self._editors): break
            title   = self.tabs.tabText(i)
            content = self._editors[i].toPlainText().strip()
            if content: lines_all.append(f"=== {title} ===\n{content}\n")
        path = os.path.join(save_dir, f"memo_all_{ts}.txt")
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write("\n".join(lines_all))
            self.signals.status_message.emit(f"[메모→녹화폴더] → {path}")
        except Exception as ex:
            self.signals.status_message.emit(f"[메모→녹화폴더] 실패: {ex}")

    def _rebuild_ov_rows(self):
        while self._ov_lay.count():
            it = self._ov_lay.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        self._overlay_rows.clear()
        for cfg in self.engine.memo_overlays: self._add_ov_row(cfg)

    def _add_ov_row(self, cfg):
        n = max(self.tabs.count(), 1)
        row = MemoOverlayRow(cfg, n)
        row.removed.connect(self._rm_ov_row)
        row.changed.connect(self.overlay_changed)
        self._overlay_rows.append(row); self._ov_lay.addWidget(row)

    def _rm_ov_row(self, row):
        if len(self.engine.memo_overlays) <= 1: return
        if row.cfg in self.engine.memo_overlays:
            self.engine.memo_overlays.remove(row.cfg)
        self._ov_lay.removeWidget(row); row.deleteLater()
        if row in self._overlay_rows: self._overlay_rows.remove(row)

    def _add_overlay(self):
        cfg = MemoOverlayCfg(0, "bottom-right", "both", True)
        self.engine.memo_overlays.append(cfg)
        self._add_ov_row(cfg); self.overlay_changed.emit()

    def _upd_ov_max(self):
        n = max(self.tabs.count(), 1)
        for row in self._overlay_rows: row.update_tab_max(n)

    def get_tab_data(self) -> list:
        tabs = []
        for i in range(self.tabs.count()):
            if i >= len(self._editors): break
            ed = self._editors[i]
            fs = ed.font().pointSize(); fs = fs if fs > 0 else 11
            tabs.append({'title':self.tabs.tabText(i), 'content':ed.toPlainText(),
                         'font_size':fs, 'ts_enabled':ed.timestamp_enabled})
        return tabs

    def set_tab_data(self, tabs: list):
        while self.tabs.count() > 0: self.tabs.removeTab(0)
        self._editors.clear(); self.engine.memo_texts.clear()
        for t in tabs:
            self._add_tab(t['title'], t['content'],
                          t.get('font_size',11), t.get('ts_enabled',True))
        self._on_tab_changed(self.tabs.currentIndex())


# =============================================================================
#  ResetPanel
# =============================================================================

# ═══ v2.9 추가 모듈 ═════════════════════════════════════════════════
_API_DOC = """
╔══════════════════════════════════════════════════════════════════╗
║   Screen & Camera Recorder  v2.9.x  —  Kernel API Reference     ║
╚══════════════════════════════════════════════════════════════════╝

커널 스크립트에서 사용 가능한 전역 객체 / 함수:
  engine          : CoreEngine 인스턴스  (녹화·캡처·수동녹화·ROI 등)
  kernel          : KernelEngine 인스턴스 (대기·조건 판단·TC결과 등)
  log(msg)        : 커널 로그 출력 (시간 자동 prefix)
  sys, os, time, datetime, re, json, threading, subprocess, platform
  np (numpy), cv2 (opencv)
  pip_install(*pkgs)  : 패키지 설치
  pip_list()          : 설치된 패키지 목록 출력
  env_info()          : Python 환경 정보 출력

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [대기 — kernel.wait()]   ★ 필수 함수
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  kernel.wait(seconds: float)
      지정 시간(초) 동안 슬립. 0.05초 단위로 중단 신호를 감지하므로
      ■ 중단 버튼을 누르면 즉시 반응합니다.
      ※ time.sleep() 대신 항상 kernel.wait()을 사용하세요.

  예시:
    kernel.wait(3)        # 3초 대기
    kernel.wait(0.5)      # 500ms 대기
    kernel.wait(60 * 5)   # 5분 대기

  타임아웃 루프 패턴:
    import time
    t0 = time.time()
    while not kernel.is_stopped():
        if time.time() - t0 > 30:   # 30초 타임아웃
            log("타임아웃")
            break
        # 작업 수행 ...
        kernel.wait(0.5)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [녹화 제어 — engine.start/stop_recording()]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  engine.start_recording()  → 녹화 시작 (이미 녹화 중이면 무시)
  engine.stop_recording()   → 녹화 종료
  engine.recording : bool   → 현재 녹화 중 여부

  예시: 10초 녹화
    engine.start_recording()
    kernel.wait(10)
    engine.stop_recording()
    log("10초 녹화 완료")

  예시: 조건 충족 시 녹화 시작/종료
    engine.start_recording()
    import time
    t0 = time.time()
    while not kernel.is_stopped():
        val = kernel.read_roi_number(0, "screen")
        if val is not None and val > 100:
            kernel.set_tc_result("PASS")
            break
        if time.time() - t0 > 60:
            kernel.set_tc_result("FAIL")
            break
        kernel.wait(0.5)
    engine.stop_recording()

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [수동녹화 — engine.save_manual_clip()]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  engine.save_manual_clip() → bool
      현재 시점 기준 pre/post 클립 저장. 쿨다운 중이면 False.
  engine.manual_pre_sec  : float  (기본 10.0초)
  engine.manual_post_sec : float  (기본 10.0초)
  engine.manual_source   : str    "screen" | "camera" | "both"

  예시: 특정 이벤트 발생 시 클립 저장
    engine.manual_pre_sec  = 5.0   # 이벤트 5초 전부터
    engine.manual_post_sec = 5.0   # 이벤트 5초 후까지
    engine.manual_source   = "both"
    while not kernel.is_stopped():
        val = kernel.read_roi_number(0, "screen")
        if val is not None and val < 10:
            log(f"이벤트 발생! val={val}")
            engine.save_manual_clip()
            break
        kernel.wait(0.3)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [캡처 — engine.capture_frame()]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  engine.capture_frame(source, tc_tag="") → str
      source : "screen" | "camera"
      tc_tag : "PASS" | "FAIL" | ""  (파일명 앞에 태그 삽입)
      return : 저장된 PNG 절대경로 (실패 시 빈 문자열)

  예시: PASS/FAIL 태그 붙여 캡처
    path = engine.capture_frame("screen", tc_tag="PASS")
    log(f"PASS 캡처 저장: {path}")

  예시: 조건 판단 후 캡처
    if kernel.roi_number_equals(0, 5, tolerance=0.1):
        engine.capture_frame("screen", tc_tag="PASS")
        kernel.set_tc_result("PASS")
    else:
        engine.capture_frame("screen", tc_tag="FAIL")
        kernel.set_tc_result("FAIL")

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [ROI 0~9 일괄 조회 — kernel.read_all_roi()]   ★ v4.4 신규
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  kernel.read_all_roi(source="screen") → dict
      ROI 0~9까지 전체의 {text, brightness, bgr}를 한 번에 반환.
      캐시 기반이므로 추가 OCR 연산 없음 (빠름).

  반환 구조:
    {
      0: {"text": "A3", "brightness": 210.5, "bgr": (12, 200, 180)},
      1: {"text": "FF", "brightness": 245.0, "bgr": (255, 255, 255)},
      ...
    }

  예시: 전체 ROI 순회
    all_roi = kernel.read_all_roi("screen")
    for idx, info in all_roi.items():
        log(f"ROI[{idx}] text={info['text']}  bright={info['brightness']:.1f}")

  예시: ROI 2번 값이 "FF"이면 PASS
    if kernel.read_all_roi("screen").get(2, {}).get("text") == "FF":
        kernel.set_tc_result("PASS")

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [ROI 밝기·색상 개별 읽기]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  kernel.read_roi_value(roi_idx, source="screen") → float | None
      ROI 평균 밝기 (0~255 휘도값)  — roi_idx: 0~9

  kernel.get_roi_avg(roi_idx, source="screen") → (B, G, R) | None
      ROI 평균 BGR 튜플  — roi_idx: 0~9

  예시: 밝기 임계값 감시
    while not kernel.is_stopped():
        brightness = kernel.read_roi_value(0, "screen")
        if brightness is not None:
            log(f"밝기: {brightness:.1f}")
            if brightness < 30:
                log("어두워짐 감지!")
                engine.save_manual_clip()
                break
        kernel.wait(0.5)

  예시: 특정 색상(BGR) 감지
    while not kernel.is_stopped():
        bgr = kernel.get_roi_avg(0, "camera")
        if bgr:
            b, g, r = bgr
            if r > 200 and g < 50:   # 빨간색 감지
                log(f"빨간색 감지 R={r:.0f}")
                engine.capture_frame("camera", tc_tag="FAIL")
                break
        kernel.wait(0.3)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [ROI OCR 텍스트 읽기]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  kernel.read_roi_text(roi_idx, source="screen",
                       lang="eng", numeric_only=False) → str
      ROI 영역 OCR 텍스트. 백그라운드 캐시 우선 사용 (빠름).

  kernel.read_roi_number(roi_idx, source="screen") → float | None
      ROI 내 숫자를 float으로 반환. 인식 실패 시 None.

  kernel.get_roi_last_text(roi_idx, source="screen") → str
      캐시된 마지막 OCR 결과 (재연산 없이 즉시 반환).

  예시: 특정 텍스트가 나타날 때까지 대기
    import time
    t0 = time.time()
    while not kernel.is_stopped():
        txt = kernel.read_roi_text(0, "screen")
        log(f"OCR: '{txt}'")
        if "OK" in txt.upper():
            log("OK 확인 → PASS")
            kernel.set_tc_result("PASS")
            break
        if time.time() - t0 > 15:
            log("타임아웃 → FAIL")
            kernel.set_tc_result("FAIL")
            break
        kernel.wait(0.5)

  예시: 숫자값 확인
    n = kernel.read_roi_number(0, "camera")
    if n is not None:
        log(f"측정값: {n}")
        if 4.5 <= n <= 5.5:
            kernel.set_tc_result("PASS")
        else:
            log(f"범위 초과: {n} (기대: 4.5~5.5)")
            kernel.set_tc_result("FAIL")

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [ROI 조건 판단 함수]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  kernel.roi_text_equals(roi_idx, value,
                         source="screen", ignore_case=True) → bool
  kernel.roi_text_contains(roi_idx, value,
                           source="screen", ignore_case=True) → bool
  kernel.roi_number_equals(roi_idx, value,
                           source="screen", tolerance=0.0) → bool
  kernel.roi_number_compare(roi_idx, op, value,
                            source="screen") → bool
      op: "==" | "!=" | ">" | ">=" | "<" | "<="
  kernel.roi_match(roi_idx, source="screen") → bool
      ROI 패널 [OCR=] 입력란의 조건값과 일치 여부

  예시: 조건 함수로 간결하게 판단
    if kernel.roi_text_equals(0, "READY", source="screen"):
        log("READY 확인 → 녹화 시작")
        engine.start_recording()
    
    if kernel.roi_number_compare(1, ">=", 80.0, source="camera"):
        log("온도 과열!")
        engine.save_manual_clip()
        kernel.set_tc_result("FAIL")
    
    if kernel.roi_match(0, "screen"):   # UI 조건값과 일치
        kernel.set_tc_result("PASS")

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [TC 결과 설정 — kernel.set_tc_result()]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  kernel.set_tc_result("PASS" | "FAIL")
      녹화 종료 시 폴더명 앞에 (PASS) 또는 (FAIL) 태그가 붙습니다.
      ※ 저장 경로 패널에서 TC-ID와 T/C 검증 목적이 활성화되어 있어야 함.

  예시: 자동 PASS/FAIL 판정 후 녹화 종료
    engine.start_recording()
    kernel.wait(5)   # 5초 대기
    
    n = kernel.read_roi_number(0, "screen")
    if n is not None and abs(n - 100.0) <= 2.0:
        kernel.set_tc_result("PASS")
        log(f"PASS: {n}")
    else:
        kernel.set_tc_result("FAIL")
        log(f"FAIL: {n}")
    
    engine.stop_recording()

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [전원 제어 — kernel.power_*()]   ★ Arduino 시리얼 연동
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  전원 제어 패널(Arduino)을 커널 스크립트에서 직접 제어합니다.
  사전 조건: 전원 제어 패널에서 시리얼 포트를 연결해두거나,
             스크립트 내에서 kernel.power_connect()로 직접 연결.

  채널 이름 (channel 파라미터):
    "bplus"    → B+  (Battery)
    "tg_bplus" → TG B+ (Target)
    "acc"      → ACC (Accessory)
    "ign"      → IGN (Ignition)

  kernel.power_connected : bool
      현재 시리얼 연결 여부

  kernel.power_list_ports() → list[str]
      사용 가능한 시리얼 포트 목록 반환

  kernel.power_connect(port, baudrate=9600) → bool
      지정 포트로 연결. 성공 시 True.

  kernel.power_disconnect()
      연결 해제.

  kernel.power_on(channel) → bool
      지정 채널 ON.

  kernel.power_off(channel) → bool
      지정 채널 OFF.

  kernel.power_all_off() → bool
      전체 전원 OFF (비상 정지).

  kernel.power_send(cmd) → bool
      임의 ASCII 명령 직접 전송 (예: "1", "Q", "0").

  kernel.power_read(timeout=0.2) → str
      수신 버퍼에서 데이터 읽기.

  ── 예시: 포트 확인 후 연결 ───────────────────────────────────
    ports = kernel.power_list_ports()
    log(f"사용 가능 포트: {ports}")
    if not ports:
        log("❌ 포트 없음 — Arduino 연결 확인")
    else:
        ok = kernel.power_connect(ports[0])
        log(f"연결 결과: {ok}")

  ── 예시: B+ ON → 3초 후 OFF ──────────────────────────────────
    if not kernel.power_connected:
        kernel.power_connect("COM3")

    kernel.power_on("bplus")
    log("B+ ON")
    kernel.wait(3)
    kernel.power_off("bplus")
    log("B+ OFF")

  ── 예시: 점화 시퀀스 (B+ → ACC → IGN 순차 ON) ───────────────
    if not kernel.power_connected:
        log("❌ 전원 제어기 미연결"); kernel.is_stopped()

    log("=== 점화 시퀀스 시작 ===")
    kernel.power_on("bplus");    kernel.wait(0.5)
    kernel.power_on("acc");      kernel.wait(0.5)
    kernel.power_on("ign");      kernel.wait(1.0)
    log("점화 완료 — 녹화 시작")
    engine.start_recording()

  ── 예시: T/C 검증 후 전원 OFF + 녹화 종료 ────────────────────
    import time

    # 점화
    kernel.power_on("bplus")
    kernel.power_on("acc")
    kernel.power_on("ign")
    kernel.wait(1.0)
    engine.start_recording()

    # 조건 감시 (최대 30초)
    t0 = time.time()
    result = "FAIL"
    while not kernel.is_stopped():
        n = kernel.read_roi_number(0, "screen")
        if n is not None and abs(n - 5.0) <= 0.2:
            result = "PASS"
            break
        if time.time() - t0 > 30:
            break
        kernel.wait(0.5)

    kernel.set_tc_result(result)
    engine.capture_frame("screen", tc_tag=result)
    engine.stop_recording()

    # 전원 역순 OFF
    kernel.power_off("ign")
    kernel.wait(0.3)
    kernel.power_off("acc")
    kernel.wait(0.3)
    kernel.power_off("bplus")
    log(f"=== 시퀀스 완료: {result} ===")

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [커널 제어 유틸]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  kernel.is_stopped() → bool   ■ 중단 버튼 눌렸는지 확인
  kernel.wait(seconds)         슬립 + 중단 감지 (time.sleep 대용)
  kernel.emit_status(msg)      하단 상태바에 메시지 출력

  루프에서 반드시 is_stopped() 확인:
    while not kernel.is_stopped():
        # 반복 작업
        kernel.wait(1.0)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [오토클릭]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  engine.start_ac()        오토클릭 시작
  engine.stop_ac()         오토클릭 중단
  engine.reset_ac_count()  카운터 초기화
  engine.ac_interval : float  클릭 간격(초, 기본 1.0)
  engine.ac_count    : int    누적 클릭 횟수

  예시: 50번 클릭 후 자동 중단
    engine.ac_interval = 0.5
    engine.reset_ac_count()
    engine.start_ac()
    while not kernel.is_stopped():
        if engine.ac_count >= 50:
            engine.stop_ac()
            log("50회 클릭 완료")
            break
        kernel.wait(0.1)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [매크로]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  engine.macro_start_run(repeat=1, gap=1.0, slot=None)
    slot : 슬롯 번호(0-based). 지정 시 해당 슬롯 로드 후 실행.
           None이면 현재 활성 슬롯 그대로 사용.
  engine.macro_load_slot(slot_idx)  → bool
    슬롯 번호의 스텝을 macro_steps에 로드만 하고 실행하지 않음.
  engine.macro_stop_run()
  engine.macro_running : bool

  예시: 슬롯 2번 매크로 3회 반복 실행
    engine.macro_start_run(repeat=3, gap=2.0, slot=1)  # slot은 0-based
    while not kernel.is_stopped():
        if not engine.macro_running:
            log("매크로 완료")
            break
        kernel.wait(0.5)

  예시: 슬롯 목록 확인 후 이름으로 선택
    for i, s in enumerate(engine._macro_slots):
        log(f"슬롯 {i}: {s['title']} ({len(s['steps'])} 스텝)")
    engine.macro_start_run(slot=0)  # 첫 번째 슬롯 실행

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [환경 확인 / 패키지 관리]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  env_info()                  Python 버전, 경로, 패키지 설치 여부 확인
  pip_list()                  설치된 패키지 목록 출력
  pip_install("numpy", "requests")  패키지 설치 (EXE 환경 포함)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [전체 시나리오 예시 — 자동 T/C 검증]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  # 전제: 저장 경로 패널에서 TC-ID 설정 + T/C 검증 목적 활성화

  import time

  PASS_VALUE = 100.0   # 기대값
  TOLERANCE  = 2.0     # 허용 오차
  TIMEOUT    = 30.0    # 최대 대기 시간(초)

  log("=== T/C 검증 시작 ===")
  engine.start_recording()

  t0 = time.time()
  result = "FAIL"
  while not kernel.is_stopped():
      n = kernel.read_roi_number(0, "screen")
      if n is not None:
          log(f"측정값: {n:.2f}")
          if abs(n - PASS_VALUE) <= TOLERANCE:
              result = "PASS"
              break
      if time.time() - t0 > TIMEOUT:
          log("타임아웃")
          break
      kernel.wait(0.5)

  engine.capture_frame("screen", tc_tag=result)
  kernel.set_tc_result(result)
  engine.stop_recording()
  log(f"=== 결과: {result} ===")

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [커널 결과 메모장 내보내기]   ★ v4.4 신규
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  커널 패널 하단 [📄 결과 내보내기] 버튼을 누르면:
    1. 실행된 모든 스크립트의 결과를 텍스트 파일로 저장
    2. 메모장(Notepad)으로 자동 열기
    3. 저장 위치: bltn_rec/kernel_logs/kernel_result_YYYYMMDD_HHMMSS.txt

  텍스트 파일 형식:
    [스크립트명]        시도 N/M   PASS|FAIL   2026-04-13 10:00:00
    ─────────────────────────────────────────────────────────────
    TC_001               1/3      PASS        2026-04-13 10:00:05
    TC_001               2/3      FAIL        2026-04-13 10:00:35
    TC_001               3/3      PASS        2026-04-13 10:01:05

  스크립트에서 직접 결과 파일 경로 얻기:
    path = kernel.result_log.open_in_notepad()
    log(f"결과 저장: {path}")

  결과 요약 문자열만 가져오기 (파일 저장 없이):
    summary = kernel.result_log.get_summary()
    log(summary)

  ※ PASS/FAIL 기록은 kernel.set_tc_result("PASS"|"FAIL") 호출 기반.
     set_tc_result() 없이 완료된 스크립트는 "OK"로 기록됨.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 [주의사항]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  · 커널 스크립트는 백그라운드 스레드에서 실행됩니다.
  · UI 위젯 직접 접근 금지 (thread-safe 하지 않음).
  · 무한 루프에 반드시 kernel.is_stopped() 조건 포함.
  · time.sleep() 대신 kernel.wait()을 사용하면 ■ 중단 즉시 반응.
  · EXE 배포 시 [🖥 CMD에서 실행] 버튼으로 스크립트 독립 테스트 가능.
"""

# =============================================================================
#  CanBusManager
# =============================================================================
# =============================================================================
#  CanBusManager — CAN/LIN 버스 관리자 (python-can 기반)
# =============================================================================
class CanBusManager:
    """
    python-can 기반 CAN 버스 관리자.
    connect() 후 자동으로 수신 루프 시작 → DBC 디코딩 → 구독자 콜백 호출.
    WebSocket 브릿지 서버가 subscribe()로 등록 → 실시간 신호값 푸시.
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
        self.sig_cache: dict = {}
        # CANoe COM 모드 (python-can 대신 CANoe COM API 사용)
        self._canoe_app  = None   # win32com CANoe.Application
        self.canoe_mode  = False  # True = CANoe COM 폴링 사용
        self._poll_sigs: dict = {}  # {sig_name: msg_name} 폴링 대상
        self._poll_lock  = threading.Lock()

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

                _bus = None  # 초기화

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
                        # GetBus('CAN') 은 연결 시 확인됨
                        try:
                            _bus = app.GetBus(_bus_name_found)
                        except Exception as _be:
                            _bus = None
                        if _bus is None:
                            _t.sleep(0.1); continue

                        for sig_name, msg_name in all_sigs.items():
                            val = _read_signal_value(_bus, sig_name, msg_name)
                            if val is None:
                                continue

                            # ★ 값 변화 감지 — 이전값과 다를 때만 이벤트 기록
                            # CANoe sig.LastUpdateTime 시도 → 실패 시 현재 시각
                            ts = ts_now
                            try:
                                # CANoe COM API: ISignal.LastUpdateTime (초 단위)
                                sig_obj = _sig_method_cache.get(sig_name)
                                if sig_obj:
                                    raw_sig = sig_obj()
                                    last_t = getattr(raw_sig, 'LastUpdateTime', None)
                                    if last_t is not None and float(last_t) > 0:
                                        # CANoe 측정 시작 기준 상대시간 → 절대시간 근사
                                        ts = ts_now  # 절대변환 어려우므로 현재시각 사용
                            except Exception:
                                pass

                            # ★ 0.1초마다 무조건 기록 (값 변화 여부 무관)
                            self.sig_cache[sig_name] = (ts, val)
                            ev_key  = f"_EV_{sig_name}"
                            ev_list = self.sig_cache.get(ev_key, [])
                            if not isinstance(ev_list, list):
                                ev_list = []
                            ev_list.append((ts, val))
                            if len(ev_list) > 6000:   # 최대 10분 @ 0.1s
                                ev_list.pop(0)
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
                    self._rx_stop.wait(0.1)

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
        if not self.connected or not self._db:
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


# =============================================================================
#  GeminiChatController — Google Gemini API 기반 AI 컨트롤러
# =============================================================================
_SYS = """\
당신은 Screen & Camera Recorder 프로그램의 AI 비서입니다.
사용자 요청을 분석해 반드시 JSON 형식 하나만 출력하세요. 다른 텍스트 절대 금지.

【절대 규칙】
1. 아래 액션 목록 외 새 액션 금지.
2. 프로그램 무관 일반 대화/질문 → chat 액션으로 답변.
3. CAN 신호값 질문 → can_read 액션 사용.
4. 커널 스크립트 작성 요청 → make_script 액션에 완성된 Python 코드 포함.

【engine API】
engine.start_recording() / stop_recording() / recording: bool
engine.capture_frame(source, tc_tag) / save_manual_clip()
engine.start_ac() / stop_ac()
engine.macro_start_run(repeat, slot) / macro_stop_run()
engine.tc_verify_result (PASS/FAIL) / output_dir / base_dir

【kernel API (커널 스크립트 전용)】
kernel.wait(seconds) / kernel.is_stopped() -> bool
kernel.set_tc_result(PASS/FAIL) / kernel.emit_status(msg)
kernel.read_roi_text(N, source) / read_roi_number(N, source)
kernel.power_on(ch) / power_off(ch)  ch: bplus/tg_bplus/acc/ign
kernel.power_all_on(delay=0.3)  # 전체 채널 순서대로 ON
kernel.power_all_off()  # 전체 채널 즉시 OFF

【can API (커널 스크립트 전용)】
can.can_read(sig_name) -> float|None  # 현재 캐시값 즉시 반환
can.can_wait_value(sig_name, expected, timeout) -> bool  # 값 도달 대기
can.canoe_write(msg_name, {sig_name: val}) -> bool  # 신호 출력
can.add_poll_signal(sig_name, msg_name)  # 폴링 등록

【커널 스크립트 예시 (CAN 기반 자동화)】
# BLTN_CAM_RecSta_OWD 가 1이 되면 녹화 종료:
can.add_poll_signal('BLTN_CAM_RecSta_OWD', 'BLTN_CAM_FD_STAT_01_200ms')
engine.start_recording()
ok = can.can_wait_value('BLTN_CAM_RecSta_OWD', expected=1, timeout=30)
engine.stop_recording()
kernel.set_tc_result('PASS' if ok else 'FAIL')

# 신호값 읽고 조건 분기:
can.add_poll_signal('BLTN_CAM_RecSta_OWD', 'BLTN_CAM_FD_STAT_01_200ms')
kernel.wait(1)
v = can.can_read('BLTN_CAM_RecSta_OWD')
if v is None or v != 1.0:
    engine.save_manual_clip()
    kernel.set_tc_result('FAIL')

【AI 직접 실행 가능한 액션】
{"action":"start_recording"}
{"action":"stop_recording","result":"PASS"}
{"action":"capture","source":"screen","tc_tag":""}
{"action":"manual_clip"}
{"action":"start_ac"}
{"action":"stop_ac"}
{"action":"macro_run","slot":1,"repeat":1}
{"action":"macro_stop"}
{"action":"roi_read_text","roi":1,"source":"screen"}
{"action":"roi_read_number","roi":1,"source":"screen"}
{"action":"roi_read_all","source":"screen"}
{"action":"power_on","channel":"bplus"}
{"action":"power_off","channel":"bplus"}
{"action":"power_all_off"}
{"action":"power_status"}
{"action":"can_read","signal":"BLTN_CAM_RecSta_OWD"}
{"action":"can_read_all"}
{"action":"can_write","message":"HU_BLTN_CAM_02_200ms","signal":"HU_BLTN_CAM_UI_Mode","value":1}
{"action":"can_status"}
{"action":"make_script","code":"can.add_poll_signal('BLTN_CAM_RecSta_OWD','BLTN_CAM_FD_STAT_01_200ms')\\nkernel.wait(1)\\nv = can.can_read('BLTN_CAM_RecSta_OWD')\\nlog(f'값: {v}')"}
{"action":"status"}
{"action":"chat","message":"답변"}

【스크립트 작성 규칙】
- 루프: while not kernel.is_stopped():
- 대기: kernel.wait(초)  (time.sleep 금지)
- ROI 인덱스: 1-based (UI 번호 그대로)
- 코드 줄바꿈: \\n 사용

【예시】
Q:"BLTN_CAM_RecSta_OWD 값 알려줘" -> {"action":"can_read","signal":"BLTN_CAM_RecSta_OWD"}
Q:"현재 CAN 신호 전부 알려줘" -> {"action":"can_read_all"}
Q:"HU UI Mode 1로 설정" -> {"action":"can_write","message":"HU_BLTN_CAM_02_200ms","signal":"HU_BLTN_CAM_UI_Mode","value":1}
Q:"BLTN_CAM_RecSta_OWD 읽는 스크립트 짜줘" -> {"action":"make_script","code":"can.add_poll_signal('BLTN_CAM_RecSta_OWD','BLTN_CAM_FD_STAT_01_200ms')\\nkernel.wait(1)\\nv=can.can_read('BLTN_CAM_RecSta_OWD')\\nlog(f'값:{v}')"}
Q:"녹화 시작" -> {"action":"start_recording"}
Q:"안녕" -> {"action":"chat","message":"안녕하세요! 녹화·CAN 제어·커널 스크립트 작성을 도와드립니다."}
"""


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
        self._history  = []
        self._lock     = threading.Lock()
        self._provider = "groq"           # 기본값: Groq
        self._model    = "llama-3.1-8b-instant"
        self._api_key  = ""
        self._client   = None             # 초기화된 클라이언트

    def set_power(self, power_ctrl):
        self._power = power_ctrl

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
        with self._lock: self._history.clear()

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
        AI 응답 처리 — 다중 JSON 액션 시퀀스 지원.
        LLM이 {"action":"kernel.wait","seconds":4} {"action":"start_recording"}
        처럼 여러 JSON을 반환할 때 순서대로 실행.
        kernel.wait 액션은 백그라운드 스레드에서 실제 대기.
        """
        import json as _j, re as _re
        with self._lock:
            self._history.append({"role": "user", "parts": [user_text]})
            history_copy = list(self._history[-20:])
        try:
            if self._provider == "groq":
                raw = self._call_groq(history_copy, user_text)
            elif self._provider == "gemini":
                raw = self._call_gemini(history_copy, user_text)
            elif self._provider == "claude":
                raw = self._call_claude(history_copy, user_text)
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
            with self._lock:
                self._history.append({"role": "model", "parts": [raw]})
            self.reply_ready.emit(raw, False)
            return

        # ── 시퀀스 실행 (백그라운드 스레드에서 대기 포함) ─────────────────
        import threading as _th, time as _tm

        def _run_sequence():
            results = []
            for cmd in cmds:
                action = cmd.get("action", "")
                if action == "kernel.wait":
                    # 실제 대기
                    secs = float(cmd.get("seconds", cmd.get("sec", 1)))
                    self.reply_ready.emit(
                        f"⏳ {secs}초 대기 중…", False)
                    _tm.sleep(secs)
                    results.append(f"⏱ {secs}초 대기 완료")
                elif action == "engine.start_recording":
                    # engine.start_recording() 직접 호출 별칭
                    r = self._execute({"action": "start_recording"})
                    results.append(r)
                elif action == "engine.stop_recording":
                    r = self._execute({"action": "stop_recording"})
                    results.append(r)
                else:
                    r = self._execute(cmd)
                    results.append(r)

            reply = "\n".join(str(r) for r in results if r)
            with self._lock:
                self._history.append({"role": "model", "parts": [reply]})
            self.reply_ready.emit(reply, False)

        if len(cmds) == 1 and cmds[0].get("action") not in ("kernel.wait",
                                                              "engine.start_recording",
                                                              "engine.stop_recording"):
            # 단일 즉시 액션 — 스레드 불필요
            reply = self._execute(cmds[0])
            with self._lock:
                self._history.append({"role": "model", "parts": [reply]})
            self.reply_ready.emit(reply, False)
        else:
            # 다중 액션 또는 wait 포함 — 백그라운드 스레드
            _th.Thread(target=_run_sequence, daemon=True,
                       name="AI_SEQ").start()

    def _call_groq(self, history, user_text):
        msgs = [{"role": "system", "content": _SYS}]
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
            max_tokens=512,
        )
        return resp.choices[0].message.content.strip()

    def _call_gemini(self, history, user_text):
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
                system_instruction=_SYS,
                temperature=0.1,
                max_output_tokens=512,
            ),
        )
        return resp.text.strip()

    def _call_claude(self, history, user_text):
        msgs = []
        for h in history[:-1]:
            role  = "user" if h.get("role") == "user" else "assistant"
            parts = h.get("parts", [])
            txt   = parts[0] if parts else ""
            msgs.append({"role": role, "content": txt})
        msgs.append({"role": "user", "content": user_text})
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=512,
            system=_SYS,
            messages=msgs,
        )
        return resp.content[0].text.strip()

    def _execute(self, cmd: dict) -> str:
        a = cmd.get("action", "")
        e = self._engine
        k = self._kernel
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

            elif a == "start_ac":
                if not getattr(e, "output_dir", ""):
                    import os as _osa
                    try: e.output_dir = e._build_path("capture"); _osa.makedirs(e.output_dir, exist_ok=True)
                    except Exception: e.output_dir = e.base_dir; _osa.makedirs(e.output_dir, exist_ok=True)
                e.start_ac(); return "✅ 자동캡처 시작"

            elif a == "stop_ac":
                e.stop_ac(); return "✅ 자동캡처 중지"

            # ── 매크로 ─────────────────────────────────────────────────────
            elif a == "macro_run":
                slot   = cmd.get("slot", None)
                rep    = int(cmd.get("repeat", 1))
                slots  = getattr(e, "_macro_slots", None) or []
                n_slots = len(slots) if slots else 0
                if slot is not None and n_slots > 0 and slot > n_slots:
                    return (f"⚠️ 슬롯 {slot}번이 없습니다.\n"
                            f"현재 저장된 슬롯: {n_slots}개 (1~{n_slots})")
                if getattr(e, "macro_running", False):
                    return "⚠️ 이미 매크로가 실행 중입니다. 먼저 중지해주세요."
                e.macro_start_run(repeat=rep, slot=slot)
                s2 = f" 슬롯{slot}" if slot is not None else ""
                return f"✅ 매크로{s2} {rep}회 실행"

            elif a == "macro_stop":
                if not getattr(e, "macro_running", False):
                    return "⚠️ 실행 중인 매크로가 없습니다."
                e.macro_stop_run(); return "✅ 매크로 중지"

            elif a == "macro_load":
                slot = int(cmd.get("slot", 1))
                ok   = e.macro_load_slot(slot - 1)
                return f"✅ 슬롯{slot} 로드" if ok else (
                    f"❌ 슬롯{slot} 로드 실패\n"
                    f"해당 슬롯에 저장된 매크로가 없을 수 있습니다.")

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
                ch = str(cmd.get("channel","")).lower()
                if not ch:
                    return ("⚠️ 채널을 지정해주세요.\n"
                            "사용 가능: bplus(B+), tg_bplus(TG B+), acc(ACC), ign(IGN)")
                if k:
                    conn = getattr(k, "power_connected", False)
                    if not conn:
                        return ("⚠️ 전원 제어기가 연결되지 않았습니다.\n"
                                "먼저 'COM포트로 전원 연결해줘'를 실행해주세요.")
                    ok = k.power_on(ch)
                elif self._power:
                    ok = self._power.channel_on(ch)
                else:
                    return "⚠️ 전원 제어기 미초기화"
                return f"✅ 전원 ON: {ch}" if ok else f"❌ ON 실패: {ch}"

            elif a == "power_off":
                ch = str(cmd.get("channel","")).lower()
                if not ch:
                    return ("⚠️ 채널을 지정해주세요.\n"
                            "사용 가능: bplus(B+), tg_bplus(TG B+), acc(ACC), ign(IGN)")
                if k:
                    conn = getattr(k, "power_connected", False)
                    if not conn:
                        return ("⚠️ 전원 제어기가 연결되지 않았습니다.\n"
                                "먼저 'COM포트로 전원 연결해줘'를 실행해주세요.")
                    ok = k.power_off(ch)
                elif self._power:
                    ok = self._power.channel_off(ch)
                else:
                    return "⚠️ 전원 제어기 미초기화"
                return f"✅ 전원 OFF: {ch}" if ok else f"❌ OFF 실패: {ch}"

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
                if k:
                    conn = getattr(k, "power_connected", False)
                    if not conn:
                        return ("⚠️ 전원 제어기가 연결되지 않았습니다.\n"
                                "먼저 COM 포트로 연결해주세요.")
                    ok = k.power_all_off()
                elif self._power:
                    ok = self._power.all_off()
                else:
                    return "⚠️ 전원 제어기 미초기화"
                return "✅ 전체 전원 OFF" if ok else "❌ ALL OFF 실패"

            elif a == "power_status":
                if k:
                    conn = getattr(k, "power_connected", False)
                    return "✅ 전원 연결됨" if conn else "전원 미연결 (COM 포트 연결 필요)"
                if not self._power: return "⚠️ 전원 제어기 미초기화"
                return f"✅ 전원: {self._power.port}" if self._power.is_connected else "전원 미연결"

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
                if not self._can.connected:
                    return ("⚠️ CAN이 연결되지 않았습니다.\n"
                            "먼저 'CAN 채널 0번 연결해줘'를 실행해주세요.")
                raw_id = cmd.get("arb_id","0x000"); data = cmd.get("data",[])
                arb    = int(raw_id,16) if isinstance(raw_id,str) else int(raw_id)
                ok     = self._can.send(arb, data)
                return f"✅ CAN 송신 ID={raw_id} data={data}" if ok else f"❌ {self._can.status_msg}"

            elif a == "can_send_signal":
                if not self._can.connected:
                    return "⚠️ CAN이 연결되지 않았습니다."
                if not self._can._db:
                    return ("⚠️ DBC 파일이 로드되지 않았습니다.\n"
                            "먼저 'DBC 파일 로드해줘'를 실행해주세요.")
                ok = self._can.send_signal(str(cmd.get("msg_name","")),cmd.get("signals",{}))
                return "✅ 신호 송신" if ok else f"❌ {self._can.status_msg}"

            elif a == "can_recv":
                if not self._can.connected:
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
                code = str(cmd.get("code","")).replace("\\n","\n")
                desc = cmd.get("description", "")
                if not code:
                    return "⚠️ 스크립트 코드가 비어 있습니다."
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
                return (f"📊 상태\n  녹화: {st}\n  경로: {pth}\n"
                        f"  매크로: {mac}\n  AI: {pv}/{self._model}\n"
                        f"  전원: {pwr}\n  CAN: {can}")

            elif a == "chat":
                return str(cmd.get("message",""))

            # ── CAN 액션 ────────────────────────────────────────────────
            elif a == "can_read":
                sig_name = cmd.get("signal", cmd.get("sig_name", ""))
                if not sig_name:
                    return "⚠️ signal 파라미터 필요"
                if not _can_manager.connected:
                    return ("⚠️ CANoe 미연결\n"
                            "→ CAN Monitor 창에서 DBC 로드 + CANoe 연결 후 신호를 그래프에 추가하세요.")
                # sig_cache에서 직접 읽기
                entry = _can_manager.sig_cache.get(sig_name)
                if entry:
                    ts, val = entry
                    from datetime import datetime as _dt
                    age = _dt.now().timestamp() - ts
                    return f"📡 {sig_name} = {val}  ({age:.1f}초 전 수신)"
                # 캐시에 없으면 폴링 등록 후 잠시 대기
                # DBC에서 메시지명 찾기
                msg_name = ""
                if hasattr(_can_manager, '_all_sigs_ref'):
                    for s in (_can_manager._all_sigs_ref or []):
                        if s.get('name') == sig_name:
                            msg_name = s.get('msg', ''); break
                _can_manager.add_poll_signal(sig_name, msg_name)
                # 0.5초 대기 후 재확인
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
                if not _can_manager.connected:
                    return "⚠️ CANoe 미연결"
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
                if not _can_manager.connected:
                    return "⚠️ CANoe 미연결 (CAN Monitor 창에서 연결해주세요)"
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


# =============================================================================
#  LLMChatDialog — AI 챗봇 창 (Groq / Gemini / Claude)
# =============================================================================
# =============================================================================
#  CanMonitorDialog — CAN Bus 신호 모니터 (AI챗봇과 동일한 독립 창)
# =============================================================================
class CanMonitorDialog(QDialog):
    """
    CAN Bus 실시간 신호 모니터 독립 창 (AI챗봇 UI 방식).

    목적:
      - BLTN 제어기 CAN 송수신 정보 실시간 확인
      - HU/CCU 등 외부 환경 모사용 신호 송신 (canoe_write)
      - 신호값 그래프로 시각화
      - 커널 스크립트에서 can.* API 사용 가이드 제공

    탭 구성:
      1. 📊 신호 탐색 / 그래프 — DBC 로드 → 신호 검색 → 실시간 그래프
      2. 📖 커널 스크립트 가이드 — can.* API 코드 예시
      3. 📋 실시간 로그 — 연결/오류 상태 로그

    스크립트 사용 예:
      can.connect_canoe()
      ok = can.can_wait_value("BLTN_CAM_RecStat_OWD", expected=1, timeout=6)
      if not ok:
          engine.start_recording()   # OWD 미확인 → 증적 녹화
      can.canoe_write("HU_BLTN_CAM_02_200ms", {"BLTN_CAM_Set_EV_BfrTime": 30})
    """
    _instance = None

    # thread-safe 시그널 (백그라운드 → 메인 스레드 UI 업데이트)
    _log_signal        = pyqtSignal(str)          # 로그 텍스트
    _lbl_signal        = pyqtSignal(str, bool)    # (텍스트, ok색상)
    _dbc_apply_signal  = pyqtSignal()             # DBC 파싱 완료 → UI 반영
    _can_prog_signal   = pyqtSignal(int, str)     # CANoe 연결 진행 (step, msg)
    _can_done_signal   = pyqtSignal(bool, str)    # CANoe 연결 완료 (ok, msg)

    @classmethod
    def show_or_raise(cls, parent, engine, kernel=None):
        if cls._instance is None or not cls._instance.isVisible():
            cls._instance = cls(parent, engine, kernel)
            cls._instance.show()
        else:
            cls._instance.raise_()
            cls._instance.activateWindow()

    def __init__(self, parent=None, engine=None, kernel=None):
        super().__init__(parent)
        self._engine   = engine
        self._kernel   = kernel
        self._can      = _can_manager
        self._dbc_path = ""
        self._watched: list = []   # [{name, msg, unit, color, data, labels}]
        self._chart_lbl   = None
        self._all_sigs:   list = []
        self._filtered:   list = []
        self._graph_zoom_pts: int = 200   # X축 기본 20초 표시 (휠로 조절, 1pt=0.1s)
        self._graph_y_center: float = 0.0  # Y축 중심값 (드래그로 이동)
        self._graph_y_range:  float = 10.0 # Y축 표시 범위 (기본 10칸)
        self._graph_pan_off:  int = 0     # 과거 스크롤 오프셋 (0=최신)
        self._graph_paused:   bool = False  # 일시정지 플래그
        self._graph_y_zoom:   float = 1.0   # Y축 확대 배율 (Ctrl+휠)
        self._graph_drag_x:   int = 0     # 드래그 시작 X 좌표

        self.setWindowTitle("📡  CAN Monitor — 신호 모니터 / CAN 입출력")
        self.setWindowModality(Qt.NonModal)
        self.setWindowFlags(
            Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint |
            Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint)
        self.resize(1060, 760)
        # thread-safe 시그널 연결 (백그라운드 스레드 → 메인 스레드)
        self._log_signal.connect(self._log)
        self._lbl_signal.connect(self._set_dbc_lbl)
        self._dbc_apply_signal.connect(self._apply_dbc_result)
        self._can_prog_signal.connect(self._on_can_progress)
        self._can_done_signal.connect(self._on_can_done)
        self._dbc_done_data = {}

        self.setStyleSheet(
            "QDialog{background:#08081a;color:#ccd;}"
            "QGroupBox{color:#556;font-size:10px;border:1px solid #1a2a3a;"
            "border-radius:4px;margin-top:10px;padding-top:8px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:8px;}"
            "QLineEdit,QComboBox{background:#0d0d1e;color:#dde;"
            "border:1px solid #2a3a5a;border-radius:4px;padding:3px 7px;font-size:11px;}"
            "QLineEdit:focus,QComboBox:focus{border-color:#4a8aaa;}"
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:4px;padding:4px 10px;font-size:11px;}"
            "QPushButton:hover{background:#22334a;}"
            "QTableWidget{background:#06060f;color:#ccd;"
            "border:1px solid #1a2a3a;gridline-color:#0f1828;font-size:11px;}"
            "QHeaderView::section{background:#0d0d1e;color:#7bc8e0;"
            "border:none;padding:3px;font-size:10px;}"
            "QLabel{font-size:11px;}"
            "QPlainTextEdit{background:#04040c;color:#9fc;"
            "border:1px solid #1a3a1a;font-family:Consolas,monospace;font-size:11px;}")
        self._build()

    # ── UI 빌드 ──────────────────────────────────────────────────────────────
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # ── 상단: DBC 로드 + CANoe 연결 바 ──────────────────────────────────
        conn_bar = QHBoxLayout(); conn_bar.setSpacing(6)

        dbc_btn = QPushButton("📂 DBC 로드")
        dbc_btn.setFixedHeight(28)
        dbc_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:4px;"
            "font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#22334a;}")
        dbc_btn.clicked.connect(self._on_load_dbc)

        self._dbc_lbl = QLabel("DBC: 미로드")
        self._dbc_lbl.setStyleSheet(
            "color:#556;font-size:10px;background:#0d0d1e;"
            "border:1px solid #1a2a3a;border-radius:3px;padding:2px 8px;")
        self._dbc_lbl.setMinimumWidth(260)

        self._conn_btn = QPushButton("🔌 CANoe 연결")
        self._conn_btn.setFixedHeight(28)
        self._conn_btn.setStyleSheet(
            "QPushButton{background:#1a3a2a;color:#7fdb9e;"
            "border:1px solid #2a6a4a;border-radius:4px;"
            "font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#1e4a32;}"
            "QPushButton:disabled{background:#0a1a0a;color:#3a5a3a;}")
        self._conn_btn.clicked.connect(self._on_connect)

        self._conn_status = QLabel("● 미연결")
        self._conn_status.setStyleSheet(
            "color:#556;font-size:10px;font-weight:bold;"
            "background:#0d0d1e;border:1px solid #1a2a3a;"
            "border-radius:3px;padding:2px 8px;min-width:200px;")

        conn_bar.addWidget(dbc_btn)
        conn_bar.addWidget(self._dbc_lbl, 1)
        conn_bar.addWidget(self._conn_btn)
        conn_bar.addWidget(self._conn_status)
        root.addLayout(conn_bar)

        # ── 탭 ───────────────────────────────────────────────────────────────
        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabWidget::pane{background:#08081a;border:1px solid #1a2a3a;}"
            "QTabBar::tab{background:#0d0d1e;color:#556;padding:6px 14px;"
            "border:1px solid #1a2a3a;border-bottom:none;font-size:11px;}"
            "QTabBar::tab:selected{background:#0f1a2a;color:#7bc8e0;"
            "border-color:#2a4a6a;font-weight:bold;}")
        root.addWidget(tabs, 1)

        self._build_tab_signal(tabs)   # 탭1: 신호 탐색 / 그래프 + Write 통합
        self._build_tab_guide(tabs)    # 탭2: 커널 스크립트 가이드
        self._build_tab_log(tabs)      # 탭3: 실시간 로그

        # 그래프 업데이트 타이머 (250ms)
        self._graph_timer = QTimer(self)
        self._graph_timer.setInterval(100)
        self._graph_timer.timeout.connect(self._update_graph)
        self._graph_timer.start()

    # ── 탭1: 신호 탐색 / 그래프 ──────────────────────────────────────────────
    def _build_tab_signal(self, tabs):
        tab = QWidget()
        h = QHBoxLayout(tab)
        h.setContentsMargins(4, 4, 4, 4); h.setSpacing(0)

        # ★ QSplitter — 신호 목록 / 그래프 영역 마우스로 크기 조절
        splitter = QSplitter(Qt.Horizontal)
        splitter.setStyleSheet(
            "QSplitter::handle{background:#1a2a3a;width:5px;}"
            "QSplitter::handle:hover{background:#2a5aaa;cursor:ew-resize;}")
        h.addWidget(splitter)

        # 좌측: 신호 검색 패널
        lw = QWidget(); lw.setMinimumWidth(220)
        lv = QVBoxLayout(lw); lv.setContentsMargins(4,0,4,0); lv.setSpacing(4)

        self._search_ed = QLineEdit()
        self._search_ed.setPlaceholderText("신호명 / 메시지명 검색…")
        self._search_ed.textChanged.connect(self._do_search)
        lv.addWidget(self._search_ed)

        fr = QHBoxLayout(); fr.setSpacing(4)
        self._flt_sender = QComboBox(); self._flt_sender.addItem("송신 노드 전체")
        self._flt_sender.currentIndexChanged.connect(self._do_search)
        self._flt_recv   = QComboBox(); self._flt_recv.addItem("수신 노드 전체")
        self._flt_recv.currentIndexChanged.connect(self._do_search)
        fr.addWidget(self._flt_sender); fr.addWidget(self._flt_recv)
        lv.addLayout(fr)

        self._sig_table = QTableWidget(0, 2)
        self._sig_table.setHorizontalHeaderLabels(["신호명", "메시지"])
        # ★ Interactive: 사용자가 컬럼 너비 직접 조절 가능
        self._sig_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Interactive)
        self._sig_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Interactive)
        self._sig_table.horizontalHeader().setStretchLastSection(False)
        self._sig_table.setColumnWidth(0, 180)
        self._sig_table.setColumnWidth(1, 130)
        self._sig_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._sig_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._sig_table.setFixedHeight(256)
        self._sig_table.itemSelectionChanged.connect(self._on_sig_selected)
        lv.addWidget(self._sig_table)

        # 신호 흐름 표시 (송신→메시지→수신)
        self._flow_lbl = QLabel("")
        self._flow_lbl.setWordWrap(True)
        self._flow_lbl.setStyleSheet(
            "color:#aaa;font-size:10px;background:#050510;"
            "border:1px solid #1a2a3a;border-radius:3px;padding:6px 8px;")
        self._flow_lbl.setMinimumHeight(76)
        lv.addWidget(self._flow_lbl)

        add_btn = QPushButton("➕  선택 신호를 그래프에 추가")
        add_btn.setFixedHeight(30)
        add_btn.setStyleSheet(
            "QPushButton{background:#1a3a2a;color:#7fdb9e;"
            "border:1px solid #2a6a4a;border-radius:4px;"
            "font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#1e4a32;}")
        add_btn.clicked.connect(self._add_to_graph)
        lv.addWidget(add_btn)

        # ── CAN 신호 출력 (Write) — 신호 탐색과 동일 선택 활용 ───────────
        write_sep = QLabel("─── 📤 신호 출력 (CANoe Write) ───────────")
        write_sep.setStyleSheet(
            "color:#3a5a7a;font-size:9px;padding:4px 0 2px 0;")
        lv.addWidget(write_sep)

        # 값 입력 행 (스핀박스 + 슬라이더)
        val_row = QHBoxLayout(); val_row.setSpacing(4)
        val_row.addWidget(QLabel("값:"))
        self._write_val_spin = QDoubleSpinBox()
        self._write_val_spin.setRange(-1e9, 1e9)
        self._write_val_spin.setDecimals(3)
        self._write_val_spin.setSingleStep(1.0)
        self._write_val_spin.setFixedWidth(90)
        self._write_val_spin.setStyleSheet(
            "QDoubleSpinBox{background:#0d0d1e;color:#f0c040;"
            "border:1px solid #3a3a1a;border-radius:4px;padding:2px 4px;"
            "font-size:12px;font-weight:bold;}")
        self._write_slider = QSlider(Qt.Horizontal)
        self._write_slider.setRange(0, 255)
        self._write_slider.setValue(0)
        self._write_slider.setStyleSheet(
            "QSlider::groove:horizontal{background:#1a2030;height:5px;border-radius:3px;}"
            "QSlider::handle:horizontal{background:#f0c040;width:14px;height:14px;"
            "margin-top:-5px;margin-bottom:-5px;border-radius:7px;}"
            "QSlider::sub-page:horizontal{background:#4a4a20;border-radius:3px;}")
        self._write_slider.valueChanged.connect(
            lambda v: self._write_val_spin.setValue(float(v)))
        self._write_val_spin.valueChanged.connect(
            lambda v: self._write_slider.setValue(max(0, min(255, int(v)))))
        val_row.addWidget(self._write_val_spin)
        val_row.addWidget(self._write_slider, 1)
        lv.addLayout(val_row)

        # 송신 버튼 행
        send_row = QHBoxLayout(); send_row.setSpacing(4)
        self._write_send_btn = QPushButton("📤 선택 신호 출력")
        self._write_send_btn.setFixedHeight(30)
        self._write_send_btn.setStyleSheet(
            "QPushButton{background:#1a3a2a;color:#7fdb9e;"
            "border:1px solid #2a6a4a;border-radius:4px;"
            "font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#1e4a32;}"
            "QPushButton:disabled{background:#0a0a18;color:#334;}")
        self._write_send_btn.clicked.connect(self._do_can_write)
        self._write_send_btn.setEnabled(False)

        self._write_repeat_chk = QPushButton("🔁")
        self._write_repeat_chk.setCheckable(True)
        self._write_repeat_chk.setFixedSize(30, 30)
        self._write_repeat_chk.setToolTip("주기 반복 송신 ON/OFF")
        self._write_repeat_chk.setStyleSheet(
            "QPushButton{background:#1a1a3a;color:#888;"
            "border:1px solid #2a2a5a;border-radius:4px;font-size:13px;}"
            "QPushButton:checked{background:#2a1a1a;color:#f0c040;"
            "border-color:#5a3a1a;}"
            "QPushButton:hover{background:#22224a;}")
        self._write_repeat_chk.toggled.connect(self._on_write_repeat)

        self._write_period_spin = QSpinBox()
        self._write_period_spin.setRange(10, 10000)
        self._write_period_spin.setValue(200)
        self._write_period_spin.setSuffix("ms")
        self._write_period_spin.setFixedWidth(72)
        self._write_period_spin.setStyleSheet(
            "QSpinBox{background:#0d0d1e;color:#f0c040;"
            "border:1px solid #3a3a1a;border-radius:3px;padding:2px;font-size:10px;}")
        send_row.addWidget(self._write_send_btn, 1)
        send_row.addWidget(self._write_repeat_chk)
        send_row.addWidget(self._write_period_spin)
        lv.addLayout(send_row)

        # 송신 이력 (compact)
        self._write_hist = QPlainTextEdit()
        self._write_hist.setReadOnly(True)
        self._write_hist.setMaximumHeight(60)
        self._write_hist.setStyleSheet(
            "QPlainTextEdit{background:#030308;color:#7bc8e0;"
            "border:1px solid #0a1a2a;"
            "font-family:Consolas,monospace;font-size:9px;}")
        lv.addWidget(self._write_hist)

        # 반복 타이머
        self._write_timer = QTimer(self)
        self._write_timer.timeout.connect(self._do_can_write_silent)

        lv.addStretch()
        splitter.addWidget(lw)

        # 우측: 그래프 + 컨트롤
        rw = QWidget()
        rv = QVBoxLayout(rw); rv.setContentsMargins(0,0,0,0); rv.setSpacing(4)

        self._graph_w = QWidget()
        self._graph_w.setMinimumHeight(280)
        self._graph_w.setStyleSheet(
            "background:#03030a;border:1px solid #1a2a3a;border-radius:4px;")
        self._graph_w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # ★ 휠 이벤트 활성화
        self._graph_w.setFocusPolicy(Qt.WheelFocus)
        self._graph_w.wheelEvent       = self._on_graph_wheel
        self._graph_w.mousePressEvent  = self._on_graph_mouse_press
        self._graph_w.mouseMoveEvent   = self._on_graph_mouse_move
        self._graph_w.mouseReleaseEvent= self._on_graph_mouse_release
        self._graph_w.setCursor(Qt.OpenHandCursor)
        gi = QVBoxLayout(self._graph_w); gi.setContentsMargins(0,0,0,0)
        self._graph_hint = QLabel("신호를 추가하면 실시간 그래프가 표시됩니다\n(휠: 시간 범위 확대/축소)")
        self._graph_hint.setAlignment(Qt.AlignCenter)
        self._graph_hint.setStyleSheet("color:#223;font-size:12px;")
        gi.addWidget(self._graph_hint)
        self._chart_lbl = QLabel(self._graph_w)
        self._chart_lbl.setVisible(False)
        rv.addWidget(self._graph_w, 1)

        # 현재값 칩 행
        cw = QWidget(); cw.setFixedHeight(40)
        cw.setStyleSheet("background:#0a0a18;border:1px solid #1a2a3a;border-radius:3px;")
        self._chips_lay = QHBoxLayout(cw)
        self._chips_lay.setContentsMargins(8,4,8,4); self._chips_lay.setSpacing(6)
        self._chips_lay.addStretch()
        rv.addWidget(cw)

        gc = QHBoxLayout(); gc.setSpacing(6)
        clr_btn = QPushButton("🗑 그래프 초기화")
        clr_btn.setFixedHeight(26)
        clr_btn.setStyleSheet(
            "QPushButton{background:#2a1a1a;color:#f88;"
            "border:1px solid #5a2a2a;border-radius:4px;font-size:10px;}")
        clr_btn.clicked.connect(self._clear_graph)
        # 그래프 컨트롤 버튼
        self._pause_btn = QPushButton("⏸ 일시정지")
        self._pause_btn.setFixedHeight(26)
        self._pause_btn.setCheckable(True)
        self._pause_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#f0c040;"
            "border:1px solid #3a3a1a;border-radius:4px;font-size:10px;}"
            "QPushButton:checked{background:#2a1a1a;color:#f88;"
            "border-color:#5a2a2a;}"
            "QPushButton:hover{background:#22334a;}")
        self._pause_btn.clicked.connect(self._on_graph_pause)
        gc.addWidget(self._pause_btn)
        gc.addStretch(); gc.addWidget(clr_btn)
        rv.addLayout(gc)

        # ★ 일시정지 시 과거 이력 스크롤바
        self._graph_scroll = QScrollBar(Qt.Horizontal)
        self._graph_scroll.setRange(0, 0)
        self._graph_scroll.setValue(0)
        self._graph_scroll.setFixedHeight(16)
        self._graph_scroll.setStyleSheet(
            "QScrollBar:horizontal{background:#0a0a18;height:16px;border-radius:4px;}"
            "QScrollBar::handle:horizontal{background:#2a5aaa;border-radius:4px;min-width:30px;}"
            "QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{width:0px;}")
        self._graph_scroll.valueChanged.connect(self._on_graph_scroll)
        self._graph_scroll.setVisible(False)
        rv.addWidget(self._graph_scroll)
        splitter.addWidget(rw)
        splitter.setSizes([300, 700])   # 초기 비율 30:70

        tabs.addTab(tab, "📊 신호 탐색 / 그래프")

    # ── 탭3: 커널 스크립트 가이드 ────────────────────────────────────────────
    def _build_tab_guide(self, tabs):
        tab = QWidget()
        v = QVBoxLayout(tab); v.setContentsMargins(4,4,4,4)

        hint = QLabel(
            "💡 커널 패널 스크립트에서 can.* API로 CAN 버스를 제어하세요.  "
            "CAN Monitor는 신호 확인 / CANoe 연결 전용입니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet(
            "color:#778;font-size:10px;background:#0a0a18;"
            "border:1px solid #1a2a3a;border-radius:4px;padding:8px;")
        v.addWidget(hint)

        doc = QPlainTextEdit()
        doc.setReadOnly(True)
        doc.setStyleSheet(
            "QPlainTextEdit{background:#030308;color:#9fc;"
            "border:1px solid #1a3a1a;font-family:Consolas,monospace;font-size:11px;}")
        doc.setPlainText(
            "# =====================================================\n"
            "# CAN 커널 스크립트 API  (커널 패널에서 사용)\n"
            "# =====================================================\n\n"
            "# 1) CANoe 연결\n"
            "if can.connect_canoe():\n"
            "    log(\'CANoe 연결 완료\')\n\n"
            "# 2) 신호 읽기 (실시간 캐시 — 100ms 주기 폴링)\n"
            "v = can.can_read(\'BLTN_CAM_RecStat_OWD\')\n"
            "log(f\'OWD RecStat = {v}\')\n\n"
            "# 3) 신호값 대기 (BLTN Key on 후 6초 안에 OWD=1 확인)\n"
            "ok = can.can_wait_value(\'BLTN_CAM_RecStat_OWD\',\n"
            "                        expected=1, timeout=6)\n"
            "if not ok:\n"
            "    log(\'OWD 1 미확인 -> 증적 녹화 시작\')\n"
            "    engine.start_recording()\n\n"
            "# 4) 폴링 신호 등록 (등록 후 sig_cache 자동 갱신)\n"
            "can.add_poll_signal(\'BLTN_CAM_RecStat_OWD\',\n"
            "                    \'BLTN_CAM_FD_STAT_01_200ms\')\n\n"
            "# 5) 주변 제어기 모사 (CANoe write)\n"
            "can.canoe_write(\'HU_BLTN_CAM_02_200ms\', {\n"
            "    \'BLTN_CAM_Set_EV_BfrTime\': 30,\n"
            "    \'BLTN_CAM_Set_EV_AftTime\': 10,\n"
            "})\n\n"
            "# =====================================================\n"
            "# 전체 예시: BLTN Key on -> 6초 안에 OWD=1 확인\n"
            "# =====================================================\n"
            "import time\n"
            "can.connect_canoe()\n"
            "can.add_poll_signal(\'BLTN_CAM_RecStat_OWD\',\n"
            "                    \'BLTN_CAM_FD_STAT_01_200ms\')\n"
            "kernel.power_on(\'ign\')\n"
            "kernel.wait(1.0)\n"
            "ok = can.can_wait_value(\'BLTN_CAM_RecStat_OWD\', 1, timeout=6)\n"
            "if ok:\n"
            "    log(\'OWD 1 확인 -> PASS\')\n"
            "    kernel.set_tc_result(\'PASS\')\n"
            "else:\n"
            "    log(\'OWD 1 미확인 -> 증적 녹화 + FAIL\')\n"
            "    engine.start_recording()\n"
            "    kernel.wait(10)\n"
            "    engine.stop_recording()\n"
            "    engine.capture_frame(\'screen\', tc_tag=\'FAIL\')\n"
            "    kernel.set_tc_result(\'FAIL\')\n"
            "kernel.power_off(\'ign\')\n"
        )
        v.addWidget(doc, 1)
        tabs.addTab(tab, "📖 커널 스크립트 가이드")

    # ── 탭3: 실시간 로그 ─────────────────────────────────────────────────────
    def _build_tab_log(self, tabs):
        tab = QWidget()
        v = QVBoxLayout(tab); v.setContentsMargins(4,4,4,4)
        self._can_log = QPlainTextEdit()
        self._can_log.setReadOnly(True)
        self._can_log.setStyleSheet(
            "QPlainTextEdit{background:#030308;color:#7bc8e0;"
            "border:1px solid #0a1a2a;font-family:Consolas,monospace;font-size:10px;}")
        lc = QHBoxLayout()
        clb = QPushButton("🗑 로그 지우기"); clb.setFixedHeight(24)
        clb.setStyleSheet(
            "QPushButton{background:#0a0a1a;color:#556;"
            "border:1px solid #1a1a2a;border-radius:3px;font-size:10px;}")
        clb.clicked.connect(self._can_log.clear)
        lc.addStretch(); lc.addWidget(clb)
        v.addWidget(self._can_log, 1)
        v.addLayout(lc)
        tabs.addTab(tab, "📋 실시간 로그")

    # ── DBC 로드 (백그라운드 파싱 — COM 미사용) ──────────────────────────────
    def _on_load_dbc(self):
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "DBC 파일 선택", "", "DBC Files (*.dbc);;All Files (*)")
        if not path: return
        self._dbc_path = path
        self._log(f"[DBC] 파싱 중: {path}")
        threading.Thread(
            target=self._parse_dbc_bg, args=(path,),
            daemon=True, name="DBC_PARSE").start()

    def _set_dbc_lbl(self, txt: str, ok: bool = True):
        """DBC 상태 레이블 업데이트 (메인 스레드에서 실행)."""
        self._dbc_lbl.setText(txt)
        self._dbc_lbl.setStyleSheet(
            ("color:#7fdb9e;font-size:10px;background:#051a0a;"
             "border:1px solid #2a6a4a;border-radius:3px;padding:2px 8px;")
            if ok else
            ("color:#f0c040;font-size:10px;background:#0d0d05;"
             "border:1px solid #3a3a1a;border-radius:3px;padding:2px 8px;"))

    def _apply_dbc_result(self):
        """DBC 파싱 완료 후 UI 반영 — 반드시 메인 스레드에서 실행."""
        d = getattr(self, '_dbc_done_data', {})
        if not d: return
        all_sigs = d.get('all_sigs', [])
        self._all_sigs = all_sigs
        # ★ CanBusManager에 신호 목록 참조 연결 (can_read 자동 폴링 등록용)
        self._can._all_sigs_ref = all_sigs
        senders = sorted(set(s['sender'] for s in all_sigs))
        recvs   = sorted(set(r for s in all_sigs for r in s['receivers']))
        for cb, items, lbl in [
                (self._flt_sender, senders, "송신 노드 전체"),
                (self._flt_recv,   recvs,   "수신 노드 전체")]:
            cb.blockSignals(True); cb.clear(); cb.addItem(lbl)
            for n in items: cb.addItem(n)
            cb.blockSignals(False)
        n_s = len(all_sigs)
        self._search_ed.setPlaceholderText(f"신호명 / 메시지명 검색 ({n_s}개)…")
        self._do_search()
        self._dbc_done_data = {}

    def _parse_dbc_bg(self, path):
        """
        DBC 파싱 — 순수 Python regex, COM 미사용.
        ★ thread-safe: QTimer.singleShot 대신 pyqtSignal 사용
           (QTimer.singleShot은 non-thread-safe → UI 미반영 버그 원인)
        """
        import re as _re, os as _os, traceback as _tb

        # ★ thread-safe emit (백그라운드 스레드에서 안전)
        def _ui(msg):  self._log_signal.emit(msg)
        def _lbl(txt, ok=True): self._lbl_signal.emit(txt, ok)

        try:
            # ── STEP 1: 파일 열기 ────────────────────────────────────────────
            fname = _os.path.basename(path)
            fsize = _os.path.getsize(path)
            _lbl(f"⏳ 파싱 중: {fname}", ok=False)
            _ui(f"[DBC] ① 파일 열기: {fname}  ({fsize/1024:.1f} KB)")

            try:
                txt = open(path, encoding='utf-8', errors='replace').read()
            except Exception as e:
                _ui(f"[DBC] ❌ 파일 열기 실패: {e}")
                _lbl("DBC: 열기 실패", ok=False); return

            # ── STEP 2: 블록 분할 ────────────────────────────────────────────
            blocks = _re.split(r'\n(?=BO_ )', txt)

            msg_blocks = [b for b in blocks if _re.match(r'BO_ \d+', b)]
            _ui(f"[DBC] ② 메시지 블록 분할: {len(msg_blocks)}개 메시지 발견")
            _lbl(f"⏳ 신호 파싱 중… (0 / {len(msg_blocks)} 메시지)", ok=False)

            # ── STEP 3: 신호 파싱 ────────────────────────────────────────────
            _SIG_RE = (_re.compile(
                r'SG_ (\S+) : \d+\|\d+@\d+[+-]'
                r' \([^,]+,[^)]+\) \[[^|]*\|[^\]]*\]'
                r' "([^"]*)"  (\S+)'))
            all_sigs = []; msgs_cnt = 0
            report_step = max(1, len(msg_blocks) // 10)  # 10% 단위 진행 보고

            for i, block in enumerate(msg_blocks):
                m = _re.match(r'BO_ (\d+) (\S+): \d+ (\S+)', block)
                if not m: continue
                mid, mname, sender = m.groups(); msgs_cnt += 1
                for sm in _SIG_RE.finditer(block):
                    sname, unit, recvs = sm.groups()
                    rv = [r for r in recvs.split(',')
                          if r and r != 'Vector__XXX']
                    all_sigs.append({
                        'name': sname, 'msg': mname, 'id': hex(int(mid)),
                        'sender': sender, 'receivers': rv, 'unit': unit or ''})
                # 10% 단위 진행 보고
                if (i + 1) % report_step == 0 or i == len(msg_blocks) - 1:
                    pct = int((i + 1) / len(msg_blocks) * 100)
                    _lbl(f"⏳ 신호 파싱 중… {pct}%  ({msgs_cnt} 메시지 / {len(all_sigs)} 신호)", ok=False)
                    _ui(f"[DBC] ③ 파싱 진행: {pct}%  {msgs_cnt}개 메시지, {len(all_sigs)}개 신호")

            # ── STEP 4: cantools 로드 시도 ───────────────────────────────────
            _ui(f"[DBC] ④ cantools 로드 시도…")
            ct_ok  = self._can.load_dbc(path)
            ct_str = 'cantools OK' if ct_ok else 'cantools 미설치 (regex 파싱만 사용)'
            _ui(f"[DBC] ④ cantools: {ct_str}")

            # ── STEP 5: UI 반영 ──────────────────────────────────────────────
            n_s = len(all_sigs); n_m = msgs_cnt
            _ui(f"[DBC] ⑤ UI 반영 중…")

            # ★ _upd_signal로 메인 스레드에 완료 데이터 전달
            #   lambda 캡처로 all_sigs, fname 등 전달
            self._dbc_done_data = {
                'all_sigs': all_sigs, 'fname': fname,
                'n_s': n_s, 'n_m': n_m, 'ct_str': ct_str}
            self._log_signal.emit(
                f"[DBC] ✅ 로드 완료: {n_s}개 신호, {n_m}개 메시지  ({ct_str})")
            self._lbl_signal.emit(
                f"DBC: {fname}  ({n_s}개 신호, {n_m}개 메시지)", True)
            # UI 반영 — signal로 메인 스레드에서 실행
            self._dbc_apply_signal.emit()

        except Exception as ex:
            tb = _tb.format_exc()
            self._log_signal.emit(f"[DBC] ❌ 파싱 예외:\n{tb}")
            self._lbl_signal.emit("DBC: 파싱 실패", False)

    # ── CANoe 연결/해제 ───────────────────────────────────────────────────────
    def _on_connect(self):
        """
        CANoe COM 연결 버튼 핸들러.
        ★ thread-safe: QTimer.singleShot 대신 pyqtSignal 사용.
           _can_prog_signal / _can_done_signal → 메인 스레드에서 UI 업데이트.
        """
        if self._can.connected:
            self._can.disconnect()
            self._set_conn_state(connected=False, msg="미연결")
            self._conn_btn.setText("🔌 CANoe 연결")
            self._log("[CAN] 연결 해제")
            return

        self._conn_btn.setEnabled(False)
        self._set_conn_state(connecting=True)
        self._log("[CAN] CANoe COM 연결 시작…")

        def _progress(step, msg):
            """백그라운드에서 pyqtSignal.emit — thread-safe."""
            self._can_prog_signal.emit(step, msg)

        def _worker():
            """백그라운드 연결 스레드."""
            ok  = self._can.connect_canoe(progress_cb=_progress)
            msg = self._can.status_msg
            self._can_done_signal.emit(ok, msg)

        threading.Thread(target=_worker, daemon=True,
                         name="CANoe_CONNECT").start()

    def _on_can_progress(self, step: int, msg: str):
        """CANoe 연결 진행 콜백 — 메인 스레드에서 실행 (pyqtSignal)."""
        _OSI_LAYERS = ["L1 물리","L2 데이터링크","L3 네트워크",
                       "L4 전송","L5 세션","L6 표현","L7 응용"]
        layer = _OSI_LAYERS[step] if 0 <= step < 7 else "연결"
        _OSI_BAR = ["▓░░░░░░","▓▓░░░░░","▓▓▓░░░░",
                    "▓▓▓▓░░░","▓▓▓▓▓░░","▓▓▓▓▓▓░","▓▓▓▓▓▓▓"]
        bar = _OSI_BAR[min(step, 6)] if 0 <= step <= 6 else "░░░░░░░"
        # 상태 레이블: OSI 진행바
        clean = msg
        for tag in ["[L1 물리] ","[L2 데이터링크] ","[L3 네트워크] ",
                    "[L4 전송] ","[L5 세션] ","[L6 표현] ","[L7 응용] "]:
            clean = clean.replace(tag, "")
        self._conn_status.setText(f"⏳ [{bar}] {layer}  {clean[:30]}")
        self._conn_status.setStyleSheet(
            "color:#f0c040;font-size:10px;font-weight:bold;"
            "background:#0d0d05;border:1px solid #3a3a1a;"
            "border-radius:3px;padding:2px 8px;min-width:200px;")
        # 실시간 로그
        self._log(f"[CAN] {msg}")

    def _on_can_done(self, ok: bool, msg: str):
        """CANoe 연결 완료 콜백 — 메인 스레드에서 실행 (pyqtSignal)."""
        self._conn_btn.setEnabled(True)
        if ok:
            self._conn_btn.setText("🔴 연결 해제")
            self._set_conn_state(connected=True, msg=msg[:35])
            self._log(f"[CAN] ✅ 연결 완료: {msg}")
            # ★ 커널/AI Chat에 CAN 연결 완료 알림
            try:
                from datetime import datetime as _dt
                _can_status = (f"CANoe 연결됨 | DBC: "
                               f"{'로드됨 ' + str(len(self._all_sigs)) + '개 신호' if self._all_sigs else '미로드'}")
                self._log(f"[CAN] 커널/AI Chat에서 can_read(), can_write() 사용 가능")
                self._log(f"[CAN] 예: can.can_read('BLTN_CAM_RecSta_OWD')")
            except Exception:
                pass
        else:
            self._conn_btn.setText("🔌 CANoe 연결")
            self._set_conn_state(connected=False, msg="연결 실패")
            self._log(f"[CAN] ❌ 연결 실패: {msg}")
            m = msg.lower()
            if "pywin32" in m or "l1" in m:
                self._log("[CAN]  → pip install pywin32")
            elif "start" in m or "l5" in m or "measurement" in m:
                self._log("[CAN]  → CANoe ▶ Start Measurement 버튼 클릭")
            elif "타임아웃" in m or "l3" in m or "dispatch" in m:
                self._log("[CAN]  → CANoe.exe 실행 중인지 확인")
                self._log("[CAN]  → canoe_test.py 스크립트로 직접 진단해보세요")

    def _set_conn_state(self, connected=False, connecting=False, msg=""):
        """연결 상태 레이블 업데이트."""
        if connecting:
            disp = f"⏳ {msg[:30]}" if msg else "⏳ 연결 중…"
            self._conn_status.setText(disp)
            self._conn_status.setStyleSheet(
                "color:#f0c040;font-size:10px;font-weight:bold;"
                "background:#0d0d05;border:1px solid #3a3a1a;"
                "border-radius:3px;padding:2px 8px;min-width:200px;")
        elif connected:
            disp = f"● 연결됨  {msg}" if msg else "● 연결됨"
            self._conn_status.setText(disp)
            self._conn_status.setStyleSheet(
                "color:#7fdb9e;font-size:10px;font-weight:bold;"
                "background:#051a0a;border:1px solid #2a6a4a;"
                "border-radius:3px;padding:2px 8px;min-width:200px;")
        else:
            disp = f"● {msg}" if msg else "● 미연결"
            self._conn_status.setText(disp)
            self._conn_status.setStyleSheet(
                "color:#e74c3c;font-size:10px;font-weight:bold;"
                "background:#1a0505;border:1px solid #5a2a2a;"
                "border-radius:3px;padding:2px 8px;min-width:200px;")

    # ── 신호 검색 ─────────────────────────────────────────────────────────────
    def _do_search(self):
        q      = self._search_ed.text().lower()
        sender = self._flt_sender.currentText()
        recv   = self._flt_recv.currentText()
        self._filtered = [
            s for s in self._all_sigs
            if (not q or q in s['name'].lower() or q in s['msg'].lower())
            and (sender == "송신 노드 전체" or s['sender'] == sender)
            and (recv   == "수신 노드 전체" or recv in s['receivers'])
        ][:300]
        self._sig_table.setRowCount(0)
        self._sig_table.setRowCount(len(self._filtered))
        for i, sig in enumerate(self._filtered):
            self._sig_table.setItem(i, 0, QTableWidgetItem(sig['name']))
            self._sig_table.setItem(i, 1, QTableWidgetItem(sig['msg']))

    def _on_sig_selected(self):
        row = self._sig_table.currentRow()
        if row < 0 or row >= len(self._filtered): return
        sig = self._filtered[row]
        rv  = ' / '.join(sig['receivers']) if sig['receivers'] else '수신 없음'
        us  = ("  단위: " + sig['unit']) if sig['unit'] else ""
        lines = [
            "\U0001f7e6 송신:    " + sig['sender'],
            "        \u2193",
            "\U0001f4e6 메시지:  " + sig['msg'] + "  (" + sig['id'] + ")",
            "        \u2193",
            "\U0001f7e9 수신:    " + rv,
            "\U0001f537 신호:    " + sig['name'] + us,
        ]
        self._flow_lbl.setText("\n".join(lines))
        # ★ 신호 선택 시 Write 버튼 활성화
        if hasattr(self, '_write_send_btn'):
            self._write_send_btn.setEnabled(True)
            self._write_send_btn.setText(
                f"📤 {sig['name'][:20]} 출력")
            self._write_send_btn.setToolTip(
                f"메시지: {sig['msg']}\n신호: {sig['name']}\n"
                f"값: (슬라이더/스핀박스 설정)")

    # ── 그래프 ────────────────────────────────────────────────────────────────
    _COLORS  = ['#378ADD', '#1D9E75', '#D85A30', '#BA7517',
                '#D4537E', '#8B5CF6', '#639922', '#E24B4A']
    _MAX_PTS = 6000  # 10분 @ 0.1초 단위 (휠로 1/100배 축소 시 최대 표시)

    def _add_to_graph(self):
        row = self._sig_table.currentRow()
        if row < 0 or row >= len(self._filtered): return
        sig = self._filtered[row]
        if any(w['name'] == sig['name'] for w in self._watched):
            self._log(f"[Graph] {sig['name']} 이미 추가됨"); return
        if len(self._watched) >= 8:
            removed = self._watched.pop(0)
            self._log(f"[Graph] 최대 8개 초과 — {removed['name']} 제거")
        color = self._COLORS[len(self._watched) % len(self._COLORS)]
        # ★ _ev_ptr을 현재 이벤트 리스트 끝으로 설정 → 추가 시점 이후 데이터만 표시
        ev_key = f"_EV_{sig['name']}"
        ev_now = self._can.sig_cache.get(ev_key)
        ev_ptr_init = len(ev_now) if isinstance(ev_now, list) else 0
        self._watched.append({
            'name': sig['name'], 'msg': sig['msg'],
            'unit': sig['unit'], 'color': color,
            'data': [], 'labels': [],
            '_ev_ptr': ev_ptr_init})  # 현재 시점부터 기록 시작
        # CANoe 폴링 등록 (연결된 경우)
        if self._can.connected:
            self._can.add_poll_signal(sig['name'], sig['msg'])
            self._log(f"[Graph] + {sig['name']} 폴링 등록 (msg={sig['msg']})")
            self._log("[Graph]  → 신호값이 sig_cache에 들어오면 그래프 자동 업데이트")
        else:
            self._log(f"[Graph] + {sig['name']} 추가 (CANoe 미연결 — 연결 후 자동 폴링)")
        self._rebuild_chips()

    def _rebuild_chips(self):
        """현재값 칩 행 재구성."""
        while self._chips_lay.count():
            it = self._chips_lay.takeAt(0)
            if it.widget(): it.widget().setParent(None)
        for i, w in enumerate(self._watched):
            chip = QPushButton(f"● {w['name'][:20]}  ✕")
            chip.setFixedHeight(28)
            chip.setStyleSheet(
                f"QPushButton{{background:#0d0d1e;color:{w['color']};"
                "border:1px solid #1a2a3a;border-radius:10px;"
                "font-size:10px;padding:0 8px;}}"
                "QPushButton:hover{background:#1a1a2a;}")
            chip.setObjectName(f"chip_{i}")
            chip.clicked.connect((lambda _, idx=i: self._remove_watch(idx)))
            self._chips_lay.addWidget(chip)
        self._chips_lay.addStretch()
        if not self._watched:
            self._graph_hint.setVisible(True)
            if self._chart_lbl: self._chart_lbl.setVisible(False)

    def _update_chip_text(self):
        """칩에 최신값 표시."""
        for i in range(self._chips_lay.count()):
            it = self._chips_lay.itemAt(i)
            if not it or not it.widget(): continue
            chip = it.widget()
            oname = chip.objectName()
            if not oname.startswith("chip_"): continue
            try: idx = int(oname[5:])
            except: continue
            if idx >= len(self._watched): continue
            w = self._watched[idx]
            val = w['data'][-1] if w['data'] else None
            vs  = f"{val:.3f}" if val is not None else "--"
            us  = f" {w['unit']}" if w['unit'] else ""
            chip.setText(f"● {w['name'][:16]}  {vs}{us}  ✕")

    def _remove_watch(self, idx):
        if 0 <= idx < len(self._watched):
            self._watched.pop(idx); self._rebuild_chips()

    def _clear_graph(self):
        self._watched.clear(); self._rebuild_chips()

    def _get_selected_sig(self):
        """현재 신호 테이블에서 선택된 신호 dict 반환."""
        row = getattr(self, '_sig_table', None)
        if row is None: return None
        row = self._sig_table.currentRow()
        if row < 0 or row >= len(self._filtered): return None
        return self._filtered[row]

    def _do_can_write(self):
        """선택된 신호를 현재 값으로 CANoe 출력."""
        sig = self._get_selected_sig()
        if not sig:
            self._log("[Write] ❌ 신호를 먼저 선택하세요"); return
        if not self._can.connected:
            self._log("[Write] ❌ CANoe 미연결"); return
        val = self._write_val_spin.value()
        ok  = self._can.canoe_write(sig['msg'], {sig['name']: val})
        ts  = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}] {'✅' if ok else '❌'} {sig['msg']}.{sig['name']} = {val}"
        self._write_hist.appendPlainText(line)
        self._write_hist.verticalScrollBar().setValue(
            self._write_hist.verticalScrollBar().maximum())
        self._log(f"[Write] {line}")
        if not ok:
            err = getattr(self._can, 'status_msg', '') or ''
            # \n 이스케이프 처리 후 줄별 출력
            err_lines = err.replace('\\n', '\n').split('\n')
            for el in err_lines:
                if el.strip(): self._log(f"[Write]    {el.strip()}")
            db = getattr(self._can, '_db', None)
            self._log(f"[Write]  DB: {'✅ cantools 로드됨' if db else '❌ DBC 없음'}")
            self._log(f"[Write]  메시지: {sig['msg']}  신호: {sig['name']}")

    def _do_can_write_silent(self):
        """반복 타이머용 조용한 write."""
        sig = self._get_selected_sig()
        if sig and self._can.connected:
            self._can.canoe_write(
                sig['msg'], {sig['name']: self._write_val_spin.value()})

    def _on_write_repeat(self, on: bool):
        if on:
            period = self._write_period_spin.value()
            self._write_timer.start(period)
            self._log(f"[Write] 반복 송신 시작 ({period}ms)")
        else:
            self._write_timer.stop()
            self._log("[Write] 반복 송신 중지")

    def _on_graph_pause(self, checked: bool):
        """일시정지 버튼 핸들러."""
        self._graph_paused = checked
        self._pause_btn.setText("▶ 재개" if checked else "⏸ 일시정지")
        if checked:
            self._update_graph_scrollbar()
            self._graph_scroll.setVisible(True)
        else:
            self._graph_scroll.setVisible(False)
            self._graph_pan_off = 0
            # Y 중심도 데이터 중심으로 자동 복귀
            all_v = [v for w in self._watched for v in w['data'] if v is not None]
            if all_v:
                self._graph_y_center = (min(all_v) + max(all_v)) / 2.0
                self._graph_y_range  = max(max(all_v) - min(all_v) + 2, 10.0)
            if self._watched: self._draw_graph()

    def _update_graph_scrollbar(self):
        """스크롤바 범위 업데이트 (일시정지 중)."""
        if not self._watched: return
        max_pts = max((len(w['data']) for w in self._watched if w['data']),
                      default=0)
        max_pan = max(0, max_pts - self._graph_zoom_pts)
        self._graph_scroll.blockSignals(True)
        self._graph_scroll.setRange(0, max_pan)
        # 스크롤바 값: 0=최신(오른쪽), max=최오래된(왼쪽) — 반전 표시
        self._graph_scroll.setValue(max_pan - self._graph_pan_off)
        self._graph_scroll.blockSignals(False)

    def _on_graph_scroll(self, val: int):
        """스크롤바 이동 → pan_off 갱신."""
        max_pan = self._graph_scroll.maximum()
        self._graph_pan_off = max(0, max_pan - val)
        if self._watched: self._draw_graph()

    def _update_graph(self):
        """
        100ms 타이머 — _EV_ 이벤트 로그 기반 그래프 업데이트.
        ★ 값이 변화한 순간만 점으로 기록 (폴링 주기 무관).
        """
        if not self._watched: return
        changed = False
        for w in self._watched:
            sig_name = w['name']

            # 진단 로그 1회
            diag_entry = self._can.sig_cache.get(f"_DIAG_{sig_name}")
            if diag_entry and not w.get('_diag_logged'):
                self._log(f"[Graph] ✅ {sig_name}: {diag_entry[1]}")
                w['_diag_logged'] = True
            err_entry = self._can.sig_cache.get(f"_POLL_ERR_{sig_name}")
            if err_entry and not w.get('_err_logged'):
                self._log(f"[Graph] ❌ {sig_name}: {err_entry[1]}")
                w['_err_logged'] = True

            # ★ 이벤트 로그에서 새로운 값 변화만 그래프에 추가
            ev_list = self._can.sig_cache.get(f"_EV_{sig_name}")
            if isinstance(ev_list, list) and ev_list:
                ptr = w.get('_ev_ptr', 0)
                new_events = ev_list[ptr:]
                if new_events:
                    w.pop('_err_logged', None)
                    for ts, val in new_events:
                        if val is not None:
                            w['data'].append(val)
                            w['labels'].append(ts)
                    w['_ev_ptr'] = len(ev_list)
                    # MAX_PTS 초과 정리
                    if len(w['data']) > self._MAX_PTS:
                        excess = len(w['data']) - self._MAX_PTS
                        w['data']   = w['data'][excess:]
                        w['labels'] = w['labels'][excess:]
                        w['_ev_ptr'] = max(0, w['_ev_ptr'] - excess)
                    changed = True
            else:
                # 이벤트 없으면 폴링 폴백 (처음 연결 시 등)
                entry = self._can.sig_cache.get(sig_name)
                if entry:
                    ts, val = entry
                    if val is not None:
                        w.pop('_err_logged', None)
                        if not w['labels'] or abs(ts - w['labels'][-1]) > 0.08:
                            w['data'].append(val)
                            w['labels'].append(ts)
                            if len(w['data']) > self._MAX_PTS:
                                w['data'].pop(0); w['labels'].pop(0)
                            changed = True

        if changed:
            self._update_chip_text()
            if not self._graph_paused:
                self._draw_graph()


    def _draw_graph(self):
        """
        CAN 신호 스캐터 그래프 (점 방식).
        - 각 폴링 샘플을 점으로 표시 (실선 없음)
        - Y축: 1단위 눈금, 기본 10칸, Ctrl+휠로 1/100배까지 확대
        - X축: 0.1초 단위, 휠로 1/100배까지 축소
        - 점 간 시간 표시 (최근 2점)
        """
        from PyQt5.QtGui import QPainter, QPen, QColor, QFont, QPixmap, QBrush
        gw = self._graph_w.width(); gh = self._graph_w.height()
        if gw <= 20 or gh <= 20: return

        pix = QPixmap(gw, gh); pix.fill(QColor("#03030a"))
        p = QPainter(pix); p.setRenderHint(QPainter.Antialiasing)
        PL, PR, PT, PB = 60, 12, 24, 36
        gw2 = gw - PL - PR; gh2 = gh - PT - PB

        # ── 표시 범위 ────────────────────────────────────────────────────────
        zoom_pts = max(1, min(getattr(self, '_graph_zoom_pts', 600),
                              self._MAX_PTS))
        pan_off  = max(0, getattr(self, '_graph_pan_off', 0))
        y_range  = max(0.1, getattr(self, '_graph_y_range', 10.0))
        y_center = getattr(self, '_graph_y_center', 0.0)
        paused   = getattr(self, '_graph_paused', False)

        def _slice(w):
            d = w['data']; lbl = w['labels']
            n = len(d)
            if n == 0: return [], []
            end   = n - pan_off
            start = max(0, end - zoom_pts)
            return d[start:end], lbl[start:end]

        # ── Y축 범위 (center ± range/2, 1단위 눈금) ─────────────────────────
        import math
        ymin = y_center - y_range / 2.0
        ymax = y_center + y_range / 2.0

        # nice tick — 1단위 기준
        raw_step = y_range / 10.0
        if raw_step < 0.1:
            tick = 0.1
        elif raw_step < 0.5:
            tick = 0.5
        else:
            mag = 10 ** math.floor(math.log10(max(raw_step, 1e-9)))
            for s in [1, 2, 5, 10, 20, 50, 100]:
                if mag * s >= raw_step:
                    tick = mag * s; break
            else:
                tick = mag * 10

        tick_start = math.ceil(ymin / tick) * tick
        ticks = []
        v = tick_start
        while v <= ymax + tick * 0.01:
            ticks.append(v)
            v = round(v + tick, 10)

        # ── 그리드 ───────────────────────────────────────────────────────────
        p.setPen(QPen(QColor("#0d1828"), 1))
        for tv in ticks:
            if ymin <= tv <= ymax:
                yp = PT + int(gh2 * (1 - (tv - ymin) / max(ymax - ymin, 1e-9)))
                p.drawLine(PL, yp, PL + gw2, yp)
        # 수직 그리드 (X축)
        n_vgrid = min(10, max(2, zoom_pts // 50))
        for i in range(n_vgrid + 1):
            xp = PL + int(gw2 * i / n_vgrid)
            p.drawLine(xp, PT, xp, PT + gh2)

        # ── Y축 레이블 ───────────────────────────────────────────────────────
        p.setFont(QFont("Consolas", 8))
        for tv in ticks:
            if ymin - tick * 0.1 <= tv <= ymax + tick * 0.1:
                yp = PT + int(gh2 * (1 - (tv - ymin) / max(ymax - ymin, 1e-9)))
                p.setPen(QPen(QColor("#7bc8e0"), 1))
                label = str(int(round(tv))) if abs(tv - round(tv)) < 1e-6                         else f"{tv:.2g}"
                p.drawText(0, yp - 8, PL - 4, 16,
                           0x0002 | 0x0080, label)  # AlignRight|AlignVCenter

        # ── 0 기준선 강조 ────────────────────────────────────────────────────
        if ymin <= 0 <= ymax:
            y0p = PT + int(gh2 * (1 - (0 - ymin) / max(ymax - ymin, 1e-9)))
            p.setPen(QPen(QColor("#2a4a6a"), 1))
            p.drawLine(PL, y0p, PL + gw2, y0p)

        # ── 신호 점 그리기 ───────────────────────────────────────────────────
        _DOT_R = 4  # 점 반지름
        for w in self._watched:
            data_s, lbl_s = _slice(w)
            if not data_s: continue
            color = QColor(w['color'])
            p.setBrush(QBrush(color))
            p.setPen(QPen(color, 1))
            n = len(data_s)
            prev_pt = None
            for i, v in enumerate(data_s):
                if v is None: continue
                # Y 범위 밖이면 스킵 (클리핑)
                if v < ymin - (ymax - ymin) * 0.05: continue
                if v > ymax + (ymax - ymin) * 0.05: continue
                xp = PL + int(gw2 * i / max(n - 1, 1))
                yp = PT + int(gh2 * (1 - (v - ymin) / max(ymax - ymin, 1e-9)))
                yp = max(PT, min(PT + gh2, yp))
                # 점 그리기
                p.drawEllipse(xp - _DOT_R, yp - _DOT_R,
                               _DOT_R * 2, _DOT_R * 2)
                # ★ 최근 2점 간 시간 차이 표시
                if prev_pt is not None and i == n - 1:
                    pi, px, pv, pt_prev = prev_pt
                    pt_curr = lbl_s[i] if i < len(lbl_s) else 0
                    if pt_prev and pt_curr:
                        dt_ms = (pt_curr - pt_prev) * 1000
                        p.setPen(QPen(QColor("#556"), 1))
                        p.setFont(QFont("Consolas", 7))
                        p.drawText(xp - 25, yp - 14, 60, 12,
                                   0x0004, f"Δ{dt_ms:.0f}ms")
                        p.setFont(QFont("Consolas", 8))
                        p.setPen(QPen(color, 1))
                prev_pt = (i, xp, v,
                           lbl_s[i] if i < len(lbl_s) else None)

        # ── 범례 ─────────────────────────────────────────────────────────────
        fy = PT + 4
        for w in self._watched:
            data_s, _ = _slice(w)
            p.setPen(QPen(QColor(w['color']), 2))
            p.setBrush(QBrush(QColor(w['color'])))
            p.drawEllipse(PL + 8, fy + 3, 8, 8)
            p.setPen(QPen(QColor("#ccd"), 1))
            us = f" ({w['unit']})" if w['unit'] else ""
            last = data_s[-1] if data_s else None
            if last is not None:
                vs = str(int(round(last))) if abs(last - round(last)) < 1e-6                      else f"{last:.2g}"
            else:
                vs = "--"
            p.drawText(PL + 22, fy, 260, 14,
                       0x0001, f"{w['name'][:22]}{us} = {vs}")
            fy += 15

        # ── X축 시간 레이블 (0.1초 단위) ────────────────────────────────────
        all_ts = []
        for w in self._watched:
            _, lbl_s = _slice(w)
            all_ts.extend(lbl_s)
        if len(all_ts) >= 2:
            t0, t1 = min(all_ts), max(all_ts)
            dur = t1 - t0
            t_now = max(all_ts)  # 가장 최신 시각
            for i in range(n_vgrid + 1):
                frac = i / n_vgrid
                tv = t0 + dur * frac
                xp = PL + int(gw2 * frac)
                from datetime import datetime as _dt
                # ★ 오른쪽 끝 = 현재, 왼쪽 = 과거
                # X축 레이블: 현재로부터 몇 초 전인지 표시
                offset = t_now - tv   # 현재로부터 몇 초 전
                if offset < 1.0:
                    ts_s = _dt.fromtimestamp(tv).strftime("%H:%M:%S.") + f"{_dt.fromtimestamp(tv).microsecond // 100000}"
                else:
                    ts_s = f"-{offset:.1f}s"
                p.setPen(QPen(QColor("#556"), 1))
                p.setFont(QFont("Consolas", 7))
                p.drawText(xp - 28, PT + gh2 + 4, 56, 16,
                           0x0004, ts_s)

        # ── 상태 표시 ────────────────────────────────────────────────────────
        zoom_sec = zoom_pts * 0.1
        y_r = y_range
        info = f"X:{zoom_sec:.1f}s  Y:±{y_r/2:.1f}"
        if pan_off > 0: info += f"  ←{pan_off*0.1:.1f}s"
        if paused: info += "  ⏸"
        p.setPen(QPen(QColor("#445"), 1))
        p.setFont(QFont("Consolas", 7))
        p.drawText(PL, PT - 2, gw2, 12, 0x0002, info)

        p.end()
        self._show_pix(pix)


    def _show_pix(self, pix):
        self._graph_hint.setVisible(False)
        self._chart_lbl.setPixmap(pix)
        self._chart_lbl.setGeometry(
            0, 0, self._graph_w.width(), self._graph_w.height())
        self._chart_lbl.setVisible(True)

    def _on_graph_wheel(self, event):
        """
        휠 이벤트:
          - 기본 휠         → X축 시간 범위 확대/축소
          - Ctrl + 휠       → Y축 배율 확대/축소 (0~255 신호 대응)
        """
        from PyQt5.QtCore import Qt as _Qt
        delta = event.angleDelta().y()
        ctrl  = event.modifiers() & _Qt.ControlModifier

        if ctrl:
            # ★ Ctrl+휠 → Y축 범위 (1/100배까지 축소)
            factor = 0.8 if delta > 0 else 1.25
            self._graph_y_range = max(0.1, min(10000.0,
                getattr(self, '_graph_y_range', 10.0) * factor))
            # 하위 호환
            self._graph_y_zoom = max(0.1, min(100.0,
                                    self._graph_y_zoom * factor))
        else:
            # 기본 휠 → X축 시간 범위
            # X축: 1포인트(0.1초)~MAX_PTS(10분) 범위
            step = max(1, int(self._graph_zoom_pts * 0.2))
            if delta > 0:
                self._graph_zoom_pts = max(1, self._graph_zoom_pts - step)
            else:
                self._graph_zoom_pts = min(self._MAX_PTS,
                                           self._graph_zoom_pts + step)
            # 휠로 X축 바꾸면 pan 오프셋도 clamp
            max_pan = max(0, len(max(
                (w['data'] for w in self._watched if w['data']),
                key=len, default=[])) - self._graph_zoom_pts)
            self._graph_pan_off = min(self._graph_pan_off, max_pan)

        if self._watched:
            self._draw_graph()
        event.accept()

    def _on_graph_mouse_press(self, event):
        """좌클릭 드래그 시작 — X/Y pan."""
        from PyQt5.QtCore import Qt as _Qt
        if event.button() == _Qt.LeftButton:
            self._graph_drag_x = event.x()
            self._graph_drag_y = event.y()
            self._graph_w.setCursor(_Qt.ClosedHandCursor)

    def _on_graph_mouse_move(self, event):
        """좌클릭 드래그 — X(과거스크롤) + Y(상하이동) pan."""
        from PyQt5.QtCore import Qt as _Qt
        if not (event.buttons() & _Qt.LeftButton): return
        dx = event.x() - self._graph_drag_x
        dy = event.y() - getattr(self, '_graph_drag_y', event.y())
        self._graph_drag_x = event.x()
        self._graph_drag_y = event.y()

        # X pan (시간축)
        gw2 = max(1, self._graph_w.width() - 60 - 12)
        pts_per_px = self._graph_zoom_pts / gw2
        self._graph_pan_off = max(0, self._graph_pan_off - int(dx * pts_per_px))
        max_pts = max((len(w['data']) for w in self._watched if w['data']),
                      default=0)
        self._graph_pan_off = min(self._graph_pan_off,
                                  max(0, max_pts - self._graph_zoom_pts))

        # Y pan (값 중심 이동) — -100~+100 범위
        gh2 = max(1, self._graph_w.height() - 24 - 36)
        y_range = getattr(self, '_graph_y_range', 10.0)
        val_per_px = y_range / gh2
        self._graph_y_center = max(-100.0, min(100.0,
            getattr(self, '_graph_y_center', 0.0) + dy * val_per_px))

        if self._watched: self._draw_graph()

    def _on_graph_mouse_release(self, event):
        """드래그 종료."""
        from PyQt5.QtCore import Qt as _Qt
        self._graph_w.setCursor(_Qt.OpenHandCursor)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._watched: self._draw_graph()

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._can_log.appendPlainText(f"[{ts}]  {msg}")
        self._can_log.verticalScrollBar().setValue(
            self._can_log.verticalScrollBar().maximum())

    def closeEvent(self, e):
        self._graph_timer.stop()
        CanMonitorDialog._instance = None
        e.accept()


class LLMChatDialog(QDialog):
    _instance = None

    @classmethod
    def show_or_raise(cls, parent, engine):
        if cls._instance is None or not cls._instance.isVisible():
            cls._instance = cls(parent, engine)
            cls._instance.show()
        else:
            cls._instance.raise_()
            cls._instance.activateWindow()

    def __init__(self, parent=None, engine=None):
        super().__init__(parent)
        self.setWindowTitle("🤖  AI Chat — Groq / Gemini / Claude")
        self.setWindowModality(Qt.NonModal)
        self.setWindowFlags(
            Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint |
            Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint)
        self.resize(680, 720)
        self.setStyleSheet(
            "QDialog{background:#08081a;color:#dde;}"
            "QTabWidget::pane{background:#08081a;border:1px solid #1a2a3a;}"
            "QTabBar::tab{background:#0d0d1e;color:#667;padding:6px 14px;"
            "border:1px solid #1a2a3a;border-bottom:none;font-size:11px;}"
            "QTabBar::tab:selected{background:#0f1a2a;color:#7bc8e0;"
            "border-color:#2a4a6a;font-weight:bold;}"
            "QGroupBox{color:#556;font-size:10px;border:1px solid #1a2a3a;"
            "border-radius:4px;margin-top:10px;padding-top:8px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:8px;}"
            "QLineEdit,QComboBox{background:#0d0d1e;color:#dde;"
            "border:1px solid #2a3a5a;border-radius:4px;"
            "padding:4px 8px;font-size:11px;}"
            "QLineEdit:focus,QComboBox:focus{border-color:#4a8aaa;}"
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:4px;"
            "padding:4px 12px;font-size:11px;}"
            "QPushButton:hover{background:#22334a;}"
            "QLabel{font-size:11px;}")

        self._ctrl = AIChatController(engine, parent=self)
        self._ctrl.reply_ready.connect(self._on_reply)
        self._ctrl.status_update.connect(self._on_status)
        self._build()
        QTimer.singleShot(100, self._load_settings)

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(10,10,10,10); v.setSpacing(8)

        # 상태 표시줄
        self._status_lbl = QLabel("⏳ AI 제공자를 선택하고 API 키를 입력해주세요.")
        self._status_lbl.setStyleSheet(
            "color:#f0c040;font-size:10px;background:#0d0d18;"
            "border:1px solid #2a2a1a;border-radius:3px;padding:3px 8px;")
        self._status_lbl.setWordWrap(True)
        v.addWidget(self._status_lbl)

        tabs = QTabWidget(); v.addWidget(tabs, 1)

        # ── 탭1: 채팅 ─────────────────────────────────────────────────────
        cw = QWidget(); ct = QVBoxLayout(cw)
        ct.setContentsMargins(6,6,6,6); ct.setSpacing(6)

        # AI 제공자 설정 그룹
        api_grp = QGroupBox("🔧 AI 제공자 설정")
        ag = QGridLayout(api_grp); ag.setSpacing(6)

        # 제공자 선택
        ag.addWidget(QLabel("제공자:"), 0, 0)
        self._prov_cb = QComboBox()
        for k, v2 in _AI_PROVIDERS.items():
            self._prov_cb.addItem(v2["label"], k)
        self._prov_cb.currentIndexChanged.connect(self._on_provider_changed)
        ag.addWidget(self._prov_cb, 0, 1)

        # 발급 링크
        self._link_btn = QPushButton("🔗 무료 키 발급")
        self._link_btn.setStyleSheet(
            "QPushButton{background:#0d0d1e;color:#7bc8e0;"
            "border:1px solid #1a3a5a;border-radius:4px;font-size:10px;}"
            "QPushButton:hover{background:#12182a;}")
        self._link_btn.clicked.connect(self._open_key_link)
        ag.addWidget(self._link_btn, 0, 2)

        # 모델 선택
        ag.addWidget(QLabel("모델:"), 1, 0)
        self._model_cb = QComboBox()
        ag.addWidget(self._model_cb, 1, 1, 1, 2)

        # API 키 입력
        ag.addWidget(QLabel("API 키:"), 2, 0)
        key_row = QHBoxLayout()
        self._key_ed = QLineEdit()
        self._key_ed.setPlaceholderText("API 키 입력...")
        self._key_ed.setEchoMode(QLineEdit.Password)
        self._key_ed.returnPressed.connect(self._on_connect)
        key_row.addWidget(self._key_ed, 1)
        show_btn = QPushButton("👁"); show_btn.setFixedSize(28,26)
        show_btn.setCheckable(True)
        show_btn.toggled.connect(lambda on: self._key_ed.setEchoMode(
            QLineEdit.Normal if on else QLineEdit.Password))
        key_row.addWidget(show_btn)
        ag.addLayout(key_row, 2, 1, 1, 2)

        # 연결 버튼
        conn_btn = QPushButton("🔌 연결")
        conn_btn.setStyleSheet(
            "QPushButton{background:#1a3a2a;color:#afffcf;"
            "border:1px solid #2a8a5a;border-radius:4px;font-weight:bold;}"
            "QPushButton:hover{background:#22503a;}")
        conn_btn.clicked.connect(self._on_connect)
        ag.addWidget(conn_btn, 3, 0, 1, 3)
        ct.addWidget(api_grp)

        # 대화 로그
        self._log = QTextEdit(); self._log.setReadOnly(True)
        self._log.setStyleSheet(
            "QTextEdit{background:#05050f;color:#ccd;border:1px solid #1a2a3a;"
            "font-size:11px;font-family:Consolas,'Malgun Gothic',monospace;}")
        ct.addWidget(self._log, 1)

        # 입력 행
        ir = QHBoxLayout(); ir.setSpacing(6)
        self._input = QLineEdit()
        self._input.setPlaceholderText(
            "명령 입력… 예: 녹화 시작해줘 / B+ 꺼줘 / 3초 후 녹화 스크립트 짜줘")
        self._input.setFixedHeight(34)
        self._input.returnPressed.connect(self._on_send)
        ir.addWidget(self._input, 1)
        send_btn = QPushButton("전송"); send_btn.setFixedSize(60,34)
        send_btn.setStyleSheet(
            "QPushButton{background:#1a3a5a;color:#7bc8e0;"
            "border:1px solid #2a6a8a;border-radius:4px;font-weight:bold;}"
            "QPushButton:hover{background:#1e4a6a;}")
        send_btn.clicked.connect(self._on_send)
        ir.addWidget(send_btn); ct.addLayout(ir)

        # 빠른 명령
        qg = QGroupBox("빠른 명령"); ql = QHBoxLayout(qg); ql.setSpacing(4)
        for lbl, cmd in [("⏺ 녹화","녹화 시작해줘"),("⏹ 종료","녹화 종료 PASS"),
                          ("📸 캡처","화면 캡처해줘"),("📊 상태","현재 상태 알려줘"),
                          ("🗑 초기화",None)]:
            b = QPushButton(lbl); b.setFixedHeight(26)
            b.setStyleSheet(
                "QPushButton{background:#0f1a2a;color:#aaa;"
                "border:1px solid #1a3a5a;border-radius:3px;font-size:10px;}"
                "QPushButton:hover{background:#1a2a3a;color:#dde;}")
            b.clicked.connect((lambda _,c=cmd: self._quick(c)) if cmd
                               else (lambda _: self._clear_chat()))
            ql.addWidget(b)
        # 📂 저장 폴더 열기 버튼
        fb = QPushButton("📂 저장폴더"); fb.setFixedHeight(26)
        fb.setStyleSheet(
            "QPushButton{background:#0f2a1a;color:#7fdb9e;"
            "border:1px solid #1a5a3a;border-radius:3px;font-size:10px;}"
            "QPushButton:hover{background:#1a3a2a;color:#affcbf;}")
        fb.setToolTip("최근 저장된 영상/이미지 폴더 열기")
        fb.clicked.connect(self._open_output_folder)
        ql.addWidget(fb)
        ct.addWidget(qg)
        tabs.addTab(cw, "💬 채팅")

        # ── 탭2: 가이드 ───────────────────────────────────────────────────
        gw = QWidget(); gv = QVBoxLayout(gw); gv.setContentsMargins(6,6,6,6)
        gt = QTextEdit(); gt.setReadOnly(True)
        gt.setStyleSheet(
            "QTextEdit{background:#05050f;color:#9fc;"
            "border:none;font-family:Consolas,monospace;font-size:11px;}")
        gt.setPlainText(
            "AI 챗봇 가이드 — Groq / Gemini / Claude\n"
            "=" * 46 + "\n\n"
            "[Groq — 무료 권장]\n"
            "  1. https://console.groq.com 접속\n"
            "  2. 구글/GitHub 계정으로 가입\n"
            "  3. API Keys → Create API Key\n"
            "  4. 무료 한도: 분당 30회, 하루 14,400회\n"
            "  5. 추천 모델: llama-3.1-8b-instant\n"
            "  설치: pip install groq\n\n"
            "[Google Gemini]\n"
            "  1. https://aistudio.google.com 접속\n"
            "  2. API key → Create API key\n"
            "  3. 한국: 무료 할당량 없음 (유료 전환 필요)\n"
            "  설치: pip install google-genai\n\n"
            "[Anthropic Claude]\n"
            "  1. https://console.anthropic.com 접속\n"
            "  2. 카드 등록 후 $5 크레딧 제공\n"
            "  3. 추천 모델: claude-haiku-4-5\n"
            "  4. 대화 1회 ≈ 약 1~5원\n"
            "  설치: pip install anthropic\n\n"
            "[명령 예시]\n"
            '  "녹화 시작해줘"\n'
            '  "B+ 전원 꺼줘"\n'
            '  "3초 후 녹화 시작하는 스크립트 짜줘"\n'
            '  "현재 상태 알려줘"\n\n'
            "[설정 저장]\n"
            "  제공자/모델/키는 자동 저장됩니다.\n"
            "  재시작해도 유지됩니다.\n"
        )
        gv.addWidget(gt)
        tabs.addTab(gw, "📘 가이드")

        # 초기 제공자 설정 (Groq 기본)
        self._on_provider_changed(0)

    # ── 핸들러 ──────────────────────────────────────────────────────────
    _LINKS = {
        "groq":   "https://console.groq.com",
        "gemini": "https://aistudio.google.com/app/apikey",
        "claude": "https://console.anthropic.com",
    }

    def _on_provider_changed(self, idx):
        prov = self._prov_cb.currentData() or "groq"
        models = _AI_PROVIDERS.get(prov, {}).get("models", [])
        self._model_cb.clear()
        self._model_cb.addItems(models)
        # 링크 버튼 툴팁
        self._link_btn.setToolTip(self._LINKS.get(prov, ""))

    def _open_key_link(self):
        prov = self._prov_cb.currentData() or "groq"
        url  = self._LINKS.get(prov, "https://console.groq.com")
        __import__('webbrowser').open(url)

    def _load_settings(self):
        """DB에서 저장된 설정 복원."""
        try:
            mw = self.parent()
            if not (mw and hasattr(mw, '_db')): return
            db   = mw._db
            prov = db.get("ai_provider") or "groq"
            mdl  = db.get("ai_model")    or "llama-3.1-8b-instant"
            key  = db.get(f"ai_key_{prov}") or ""

            # 제공자 콤보 설정
            for i in range(self._prov_cb.count()):
                if self._prov_cb.itemData(i) == prov:
                    self._prov_cb.setCurrentIndex(i)
                    break
            # 모델 콤보 설정
            idx = self._model_cb.findText(mdl)
            if idx >= 0: self._model_cb.setCurrentIndex(idx)
            # 키 설정
            if key:
                self._key_ed.setText(key)
                self._ctrl.set_provider(prov, mdl, key)
        except Exception:
            pass

    def _on_connect(self):
        prov = self._prov_cb.currentData() or "groq"
        mdl  = self._model_cb.currentText()
        key  = self._key_ed.text().strip()
        if not key:
            self._set_status("⚠️ API 키를 입력해주세요.", error=True)
            return
        self._set_status("연결 중...")
        self._ctrl.set_provider(prov, mdl, key)
        # DB 저장
        try:
            mw = self.parent()
            if mw and hasattr(mw, '_db'):
                mw._db.set("ai_provider",    prov)
                mw._db.set("ai_model",       mdl)
                mw._db.set(f"ai_key_{prov}", key)
        except Exception:
            pass

    def _on_status(self, text):
        error = text.startswith("❌") or "미설치" in text or "⚠️" in text
        self._set_status(text, error=error)

    def _set_status(self, text, error=False):
        ok = text.startswith("✅")
        c  = "#e74c3c" if error else "#2ecc71" if ok else "#f0c040"
        bg = "#1a0505" if error else "#051a05" if ok else "#0d0d18"
        bd = "#4a1a1a" if error else "#1a4a1a" if ok else "#2a2a1a"
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(
            f"color:{c};font-size:10px;background:{bg};"
            f"border:1px solid {bd};border-radius:3px;padding:3px 8px;")

    def _on_send(self):
        text = self._input.text().strip()
        if not text: return
        self._append("👤  " + text, "#7bc8e0")
        self._input.clear(); self._input.setEnabled(False)
        self._ctrl.send(text)

    def _quick(self, text):
        self._input.setText(text); self._on_send()

    def _on_reply(self, text, is_error):
        self._append("🤖  " + text, "#e74c3c" if is_error else "#afffcf")
        self._input.setEnabled(True); self._input.setFocus()

    def _append(self, text, color):
        safe = (text.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
                    .replace("\n","<br>"))
        self._log.append(
            f'<span style="color:{color};">{safe}</span>'
            '<hr style="border:none;border-top:1px solid #1a1a2a;margin:4px 0;">')
        sb = self._log.verticalScrollBar(); sb.setValue(sb.maximum())

    def _clear_chat(self):
        self._ctrl.clear_history(); self._log.clear()

    def _open_output_folder(self):
        """최근 저장된 영상/이미지 폴더를 탐색기로 열기."""
        import os as _os, subprocess as _sp, sys as _sys
        e = getattr(self._ctrl, '_engine', None)
        folder = getattr(e, 'output_dir', '') if e else ''
        # output_dir 없으면 부모 경로 탐색
        if not folder or not _os.path.isdir(folder):
            try:
                mw = self.parent()
                if mw and hasattr(mw, '_db'):
                    ps = mw._db.get_path_settings()
                    root = ps.get('root_dir', '') or ''
                    if root and _os.path.isdir(root):
                        # 최신 날짜 폴더 탐색
                        subdirs = sorted(
                            [d for d in _os.scandir(root) if d.is_dir()],
                            key=lambda d: d.stat().st_mtime, reverse=True)
                        folder = subdirs[0].path if subdirs else root
            except Exception:
                pass
        if not folder or not _os.path.isdir(folder):
            self._append("⚠️ 저장 폴더를 찾을 수 없습니다. 녹화를 먼저 실행해주세요.", "#f0c040")
            return
        try:
            if _sys.platform == 'win32':
                _os.startfile(folder)
            elif _sys.platform == 'darwin':
                _sp.Popen(['open', folder])
            else:
                _sp.Popen(['xdg-open', folder])
            self._append(f"📂 폴더 열기: {folder}", "#7fdb9e")
        except Exception as ex:
            self._append(f"❌ 폴더 열기 실패: {ex}", "#f88")

    def closeEvent(self, e):
        LLMChatDialog._instance = None; e.accept()


class ApiDocDialog(QDialog):
    """
    API 레퍼런스 다이얼로그.
    ★ v4.4: Ctrl+F 인라인 검색 바 추가 — 단어 강조·이동 지원.
    NonModal — 열려있어도 다른 창을 자유롭게 조작 가능.
    """
    _instance = None   # 싱글턴 — 중복 창 방지

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📖  Kernel API Reference")
        self.setWindowModality(Qt.NonModal)
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint |
            Qt.WindowMinimizeButtonHint |
            Qt.WindowMaximizeButtonHint)
        self.resize(800, 720)
        self.setStyleSheet(
            "QDialog{background:#0d0d1e;color:#dde;}"
            "QPlainTextEdit{background:#06060e;color:#9fc;border:1px solid #1a3a1a;"
            "font-family:Consolas,Courier New,monospace;font-size:11px;}"
            "QLineEdit{background:#0a0a18;color:#dde;border:1px solid #2a3a5a;"
            "border-radius:3px;padding:3px 8px;font-size:11px;}"
            "QLineEdit:focus{border:1px solid #4a8aaa;}"
            "QPushButton{background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:4px;padding:4px 14px;font-size:11px;}"
            "QPushButton:hover{background:#22334a;}"
            "QLabel{font-size:10px;}")

        self._matches: list = []    # [(start, end), ...]
        self._match_idx: int  = -1  # 현재 강조 위치

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 10)
        v.setSpacing(6)

        # ── 헤더 ─────────────────────────────────────────────────────────
        hdr = QLabel("🧠  커널 스크립트에서 사용 가능한 API 레퍼런스")
        hdr.setStyleSheet(
            "color:#f0c040;font-size:13px;font-weight:bold;"
            "padding:6px 10px;background:#0d1018;"
            "border:1px solid #2a3a1a;border-radius:4px;")
        v.addWidget(hdr)

        # ── 검색 바 (Ctrl+F 토글) ────────────────────────────────────────
        self._search_bar = QWidget()
        sb_lay = QHBoxLayout(self._search_bar)
        sb_lay.setContentsMargins(0, 0, 0, 0)
        sb_lay.setSpacing(6)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("검색어 입력… (Enter: 다음, Shift+Enter: 이전)")
        self._search_input.setFixedHeight(28)
        self._search_input.textChanged.connect(self._on_search_changed)
        self._search_input.returnPressed.connect(self._find_next)

        self._match_label = QLabel("0 / 0")
        self._match_label.setFixedWidth(52)
        self._match_label.setStyleSheet("color:#7bc8e0;font-size:10px;")
        self._match_label.setAlignment(Qt.AlignCenter)

        btn_prev = QPushButton("▲")
        btn_prev.setFixedSize(26, 26)
        btn_prev.setToolTip("이전 결과  (Shift+Enter)")
        btn_prev.clicked.connect(self._find_prev)

        btn_next = QPushButton("▼")
        btn_next.setFixedSize(26, 26)
        btn_next.setToolTip("다음 결과  (Enter)")
        btn_next.clicked.connect(self._find_next)

        btn_clear = QPushButton("✕")
        btn_clear.setFixedSize(26, 26)
        btn_clear.setToolTip("검색 닫기  (Esc)")
        btn_clear.setStyleSheet(
            "QPushButton{background:#1a1a2a;color:#888;"
            "border:1px solid #2a2a3a;border-radius:4px;}"
            "QPushButton:hover{background:#2a1a1a;color:#e74c3c;}")
        btn_clear.clicked.connect(self._close_search)

        sb_lay.addWidget(QLabel("🔍"))
        sb_lay.addWidget(self._search_input, 1)
        sb_lay.addWidget(self._match_label)
        sb_lay.addWidget(btn_prev)
        sb_lay.addWidget(btn_next)
        sb_lay.addWidget(btn_clear)

        self._search_bar.setVisible(False)   # 기본 숨김
        v.addWidget(self._search_bar)

        # ── 본문 텍스트 ──────────────────────────────────────────────────
        self._txt = QPlainTextEdit()
        self._txt.setReadOnly(True)
        self._txt.setPlainText(_API_DOC)
        v.addWidget(self._txt, 1)

        # ── 하단 버튼 ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        copy_btn = QPushButton("📋  예시 코드 복사")
        copy_btn.clicked.connect(self._copy_example)

        search_btn = QPushButton("🔍  Ctrl+F")
        search_btn.setToolTip("텍스트 검색 (Ctrl+F)")
        search_btn.clicked.connect(self._toggle_search)

        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(self.close)

        btn_row.addWidget(copy_btn)
        btn_row.addWidget(search_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        v.addLayout(btn_row)

        # ── 단축키 ──────────────────────────────────────────────────────
        from PyQt5.QtWidgets import QShortcut
        from PyQt5.QtGui import QKeySequence
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(self._toggle_search)
        QShortcut(QKeySequence("Escape"), self).activated.connect(self._close_search)

    # ── 검색 로직 ────────────────────────────────────────────────────────────

    def _toggle_search(self):
        """Ctrl+F — 검색 바 토글."""
        visible = not self._search_bar.isVisible()
        self._search_bar.setVisible(visible)
        if visible:
            self._search_input.setFocus()
            self._search_input.selectAll()
        else:
            self._clear_highlights()
            self._txt.setFocus()

    def _close_search(self):
        """Esc — 검색 바 닫기 + 강조 제거."""
        self._search_bar.setVisible(False)
        self._clear_highlights()
        self._txt.setFocus()

    def _on_search_changed(self, text: str):
        """검색어 변경 시 전체 재검색 후 첫 번째 결과로 이동."""
        self._clear_highlights()
        self._matches = []
        self._match_idx = -1
        if not text:
            self._match_label.setText("0 / 0")
            return
        self._build_matches(text)
        if self._matches:
            self._match_idx = 0
            self._highlight_all()
            self._scroll_to_match(0)
        self._update_label()

    def _build_matches(self, keyword: str):
        """대소문자 무시 전체 검색 — (start, end) 목록 구축."""
        doc  = self._txt.document()
        full = doc.toPlainText()
        kw   = keyword.lower()
        txt  = full.lower()
        pos  = 0
        while True:
            idx = txt.find(kw, pos)
            if idx == -1:
                break
            self._matches.append((idx, idx + len(keyword)))
            pos = idx + 1

    def _highlight_all(self):
        """모든 매치를 형광펜으로 표시 — 현재 선택은 별도 색상."""
        from PyQt5.QtGui import QTextCursor, QTextCharFormat, QColor
        # 전체 초기화
        cur_all = self._txt.textCursor()
        cur_all.select(QTextCursor.Document)
        fmt_clear = QTextCharFormat()
        fmt_clear.setBackground(QColor("transparent"))
        cur_all.setCharFormat(fmt_clear)

        # 일반 매치: 연한 노랑
        fmt_normal = QTextCharFormat()
        fmt_normal.setBackground(QColor("#3a3a00"))
        fmt_normal.setForeground(QColor("#f0f0a0"))

        # 현재 매치: 밝은 초록
        fmt_current = QTextCharFormat()
        fmt_current.setBackground(QColor("#006633"))
        fmt_current.setForeground(QColor("#ffffff"))

        doc = self._txt.document()
        for i, (start, end) in enumerate(self._matches):
            cur = QTextCursor(doc)
            cur.setPosition(start)
            cur.setPosition(end, QTextCursor.KeepAnchor)
            cur.setCharFormat(fmt_current if i == self._match_idx else fmt_normal)

    def _scroll_to_match(self, idx: int):
        """지정 매치로 스크롤 + 커서 이동."""
        if not self._matches or idx < 0:
            return
        start, end = self._matches[idx]
        from PyQt5.QtGui import QTextCursor
        cur = self._txt.textCursor()
        cur.setPosition(start)
        cur.setPosition(end, QTextCursor.KeepAnchor)
        self._txt.setTextCursor(cur)
        self._txt.ensureCursorVisible()

    def _find_next(self):
        """다음 매치로 이동."""
        if not self._matches:
            return
        self._match_idx = (self._match_idx + 1) % len(self._matches)
        self._highlight_all()
        self._scroll_to_match(self._match_idx)
        self._update_label()

    def _find_prev(self):
        """이전 매치로 이동."""
        if not self._matches:
            return
        self._match_idx = (self._match_idx - 1) % len(self._matches)
        self._highlight_all()
        self._scroll_to_match(self._match_idx)
        self._update_label()

    def _update_label(self):
        """N / M 레이블 갱신."""
        total = len(self._matches)
        cur   = self._match_idx + 1 if self._matches else 0
        self._match_label.setText(f"{cur} / {total}")
        # 결과 없음 강조
        color = "#e74c3c" if total == 0 and self._search_input.text() else "#7bc8e0"
        self._match_label.setStyleSheet(f"color:{color};font-size:10px;")

    def _clear_highlights(self):
        """모든 형광펜 제거."""
        from PyQt5.QtGui import QTextCursor, QTextCharFormat, QColor
        cur = self._txt.textCursor()
        cur.select(QTextCursor.Document)
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("transparent"))
        fmt.setForeground(QColor("#9fc"))   # 원래 텍스트 색상 복원
        cur.setCharFormat(fmt)

    # ── 기타 ────────────────────────────────────────────────────────────────

    def keyPressEvent(self, e):
        """Shift+Enter → 이전 결과."""
        from PyQt5.QtCore import Qt as _Qt
        if (e.key() == _Qt.Key_Return and
                e.modifiers() & _Qt.ShiftModifier and
                self._search_bar.isVisible()):
            self._find_prev()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        ApiDocDialog._instance = None
        super().closeEvent(e)

    @classmethod
    def show_or_raise(cls, parent=None):
        """싱글턴: 이미 열려있으면 앞으로 가져오고, 없으면 새로 생성."""
        if cls._instance is None or not cls._instance.isVisible():
            cls._instance = cls(parent)
        cls._instance.show()
        cls._instance.raise_()
        cls._instance.activateWindow()

    def _copy_example(self):
        lines = _API_DOC.split("\n")
        in_ex = False; ex_lines = []
        for ln in lines:
            if "스크립트 예시" in ln: in_ex = True; continue
            if in_ex and "━" in ln and ex_lines: break
            if in_ex: ex_lines.append(ln.lstrip("  "))
        QApplication.clipboard().setText("\n".join(ex_lines).strip())


# =============================================================================
#  KernelEngine  — 내장 Python 인터프리터 + 조건부 스케줄링
# =============================================================================

# =============================================================================
#  KernelResultLog  — 커널 스크립트 실행 결과 누적 + 텍스트 내보내기
# =============================================================================
class KernelResultLog:
    """
    커널 스크립트 실행 결과를 누적하고 텍스트 파일로 내보내는 전용 클래스.

    기록 항목:
        · 스크립트 이름
        · 시도 횟수 (총 N번 중 M번째)
        · 실행 시각
        · PASS / FAIL / ERROR / SKIP 결과

    사용 흐름:
        log = KernelResultLog()
        log.begin_session()               # 세션 시작 (전체 실행 전 1회)
        log.record("스크립트A", 1, 3, "PASS", "2026-04-13 10:00:00")
        log.record("스크립트A", 2, 3, "FAIL")
        log.export_txt(path)              # 텍스트 파일로 내보내기
    """

    def __init__(self):
        self._entries: list = []        # List[dict]
        self._session_start: str = ""

    def begin_session(self):
        """새 세션 시작 — 기존 기록 초기화."""
        self._entries.clear()
        self._session_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def record(self, script_name: str, attempt: int, total: int,
               result: str, timestamp: str = ""):
        """
        단일 실행 결과 기록.

        Parameters
        ----------
        script_name : 스크립트 제목
        attempt     : 현재 시도 번호 (1부터)
        total       : 총 시도 횟수 (0 = 무한)
        result      : "PASS" | "FAIL" | "ERROR" | "SKIP"
        timestamp   : 실행 시각 문자열 (생략 시 현재 시각)
        """
        self._entries.append({
            "script": script_name,
            "attempt": attempt,
            "total": total,
            "result": result,
            "ts": timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    def get_summary(self) -> str:
        """
        현재까지 누적된 결과 요약 문자열 반환.
        형식:
            [스크립트명]  시도 N/M  PASS|FAIL  2026-04-13 10:00:00
        """
        if not self._entries:
            return "(결과 없음)"
        lines = [
            f"=== 커널 실행 결과 요약 ===",
            f"세션 시작: {self._session_start}",
            f"총 기록 수: {len(self._entries)}",
            "",
        ]
        # 스크립트별 PASS/FAIL 집계
        from collections import defaultdict
        agg: dict = defaultdict(lambda: {"pass": 0, "fail": 0, "other": 0})
        for e in self._entries:
            key = e["script"]
            r   = e["result"].upper()
            if r == "PASS":   agg[key]["pass"]  += 1
            elif r == "FAIL": agg[key]["fail"]  += 1
            else:             agg[key]["other"] += 1

        # 집계 테이블
        lines.append("[ 스크립트별 집계 ]")
        for name, cnt in agg.items():
            total_cnt = cnt["pass"] + cnt["fail"] + cnt["other"]
            lines.append(
                f"  {name:<30}  PASS:{cnt['pass']:>3}  FAIL:{cnt['fail']:>3}"
                f"  기타:{cnt['other']:>3}  합계:{total_cnt:>3}")
        lines.append("")
        lines.append("[ 상세 기록 ]")
        lines.append(f"  {'스크립트':<30}  {'시도':>8}  {'결과':<6}  시각")
        lines.append("  " + "-" * 70)
        for e in self._entries:
            tot_str = f"/{e['total']}" if e['total'] > 0 else "/∞"
            lines.append(
                f"  {e['script']:<30}  {e['attempt']:>3}{tot_str:<5}  "
                f"{e['result']:<6}  {e['ts']}")
        return "\n".join(lines)

    def export_txt(self, path: str) -> bool:
        """
        결과를 텍스트 파일로 내보내기.
        Returns True 성공 / False 실패.
        """
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(self.get_summary())
                f.write("\n")
            return True
        except Exception as ex:
            return False

    def open_in_notepad(self, base_dir: str = "") -> str:
        """
        결과를 임시 .txt 파일로 저장 후 메모장(Notepad / xdg-open)으로 열기.
        Returns 저장된 파일 경로 (실패 시 빈 문자열).
        """
        ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"kernel_result_{ts}.txt"
        fdir  = base_dir if base_dir else os.path.join(
            os.path.expanduser("~/Desktop"), "bltn_rec", "kernel_logs")
        path  = os.path.join(fdir, fname)
        if not self.export_txt(path):
            return ""
        try:
            if platform.system() == "Windows":
                subprocess.Popen(["notepad.exe", path])
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass
        return path

class KernelScript:
    """단일 커널 스크립트 슬롯."""
    _cnt = 0
    def __init__(self, title="스크립트 1", code="", repeat=1, enabled=True):
        KernelScript._cnt += 1
        self.id      = KernelScript._cnt
        self.title   = title
        self.code    = code
        self.repeat  = repeat   # 0 = 무한
        self.enabled = enabled

    def to_dict(self) -> dict:
        return dict(title=self.title, code=self.code,
                    repeat=self.repeat, enabled=self.enabled)

    @classmethod
    def from_dict(cls, d: dict) -> 'KernelScript':
        obj = cls(d.get('title','스크립트'), d.get('code',''),
                  d.get('repeat',1), d.get('enabled',True))
        return obj


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

        # ── 결과 로그 ─────────────────────────────────────────────────────
        # 커널 실행 결과(PASS/FAIL/OK/SKIP)를 스크립트·시도별로 누적.
        # _run_all() 에서 자동 기록, KernelPanel 버튼으로 메모장 내보내기.
        self.result_log: 'KernelResultLog' = KernelResultLog()

    # ── 사용자 API 헬퍼 ──────────────────────────────────────────────────────
    def wait(self, seconds: float):
        """슬립 + 중단 감지 (0.05초 단위)."""
        waited = 0.0
        while waited < seconds and not self._stop_ev.is_set():
            time.sleep(min(0.05, seconds - waited))
            waited += 0.05

    def is_stopped(self) -> bool:
        return self._stop_ev.is_set()

    # ── 전원 제어 API ─────────────────────────────────────────────────────────
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
        self._engine.tc_verify_result = result

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
                self._exec_script(script)

                # ── 결과 판정 ────────────────────────────────────────────
                tc_res = (self._engine.tc_verify_result or "").strip().upper()
                if tc_res in ("PASS", "FAIL"):
                    result_str = tc_res
                elif self._stop_ev.is_set():
                    result_str = "SKIP"
                else:
                    result_str = "OK"   # TC 결과 미설정 = 단순 완료

                self.result_log.record(
                    script_name=script.title,
                    attempt=rep + 1,
                    total=total,
                    result=result_str,
                )
                self._log(
                    f"  → 결과: {result_str}  "
                    f"({rep+1}{'/' + str(total) if total > 0 else '/∞'})")

                rep += 1
                if not infinite and rep >= script.repeat: break
                if self._stop_ev.is_set(): break
                self.wait(0.1)

        self.running = False
        self.cur_title = ""
        self._log("✅ 커널 실행 완료  (결과 로그: 패널 하단 [📄 결과 내보내기] 버튼)")

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
            # can.connect_canoe()          CANoe COM 연결
            # can.canoe_write(msg, sigs)   CANoe로 신호 송신 (주변기기 모사)
            # can.add_poll_signal(sig,msg)  폴링 신호 등록
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


# =============================================================================
#  KernelPanel  — 커널 스크립트 편집/실행 UI
# =============================================================================
class KernelPanel(QWidget):
    """
    커널 스크립트 관리 패널.

    구성:
      ┌─ 슬롯 목록 (드래그앤드랍 순서변경) ─────────────────┐
      │  + 추가  /  - 삭제  /  ✏ 이름변경                   │
      └──────────────────────────────────────────────────────┘
      ┌─ 스크립트 에디터 ────────────────────────────────────┐
      │  반복 설정  /  활성화 체크박스                        │
      │  Python 에디터 (QPlainTextEdit)                      │
      │  [가져오기 .py]  [내보내기 .py]  [▶실행]  [■중단]   │
      └──────────────────────────────────────────────────────┘
      ┌─ 실행 로그 ─────────────────────────────────────────┐
      └──────────────────────────────────────────────────────┘
    """
    _SLOT_H = 34
    _BG     = "#0a0a18"
    _BG_HV  = "#141428"
    _BG_DG  = "#1a2a4a"

    def __init__(self, kernel: KernelEngine, signals: 'Signals', parent=None):
        super().__init__(parent)
        self._kernel  = kernel
        self._signals = signals
        self._kernel.log_callback = self._on_log
        self._active_idx = -1
        self._slot_widgets: list = []
        self._dragging   = False
        self._drag_idx   = -1
        self._drag_start = QPoint()
        self._build()
        self._add_script()   # 기본 슬롯 1개

    # ── UI 빌드 ───────────────────────────────────────────────────────────────
    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(6)

        # ── Python 인터프리터 설정 ────────────────────────────────────────
        py_grp = QGroupBox("🐍 Python 인터프리터 설정")
        py_grp.setStyleSheet(
            "QGroupBox{font-size:10px;color:#7bc8e0;"
            "border:1px solid #1a3a3a;border-radius:4px;margin-top:8px;"
            "padding-top:6px;background:#060612;}"
            "QGroupBox::title{left:8px;top:-6px;background:#060612;"
            "padding:0 4px;}")
        py_l = QVBoxLayout(py_grp); py_l.setSpacing(4); py_l.setContentsMargins(6,4,6,6)

        # 인터프리터 경로
        py_row = QHBoxLayout(); py_row.setSpacing(4)
        py_row.addWidget(QLabel("python:"))
        self._py_ed = QLineEdit(self._kernel.python_exe)
        self._py_ed.setPlaceholderText("python.exe 경로 (비워두면 현재 프로세스 사용)")
        self._py_ed.setStyleSheet(
            "QLineEdit{background:#0a0a18;color:#7bc8e0;"
            "border:1px solid #1a3a4a;border-radius:3px;"
            "padding:2px 6px;font-size:10px;font-family:monospace;}")
        self._py_ed.textChanged.connect(
            lambda t: setattr(self._kernel, 'python_exe',
                              t.strip() or sys.executable))
        py_row.addWidget(self._py_ed, 1)
        py_browse = QPushButton("📂")
        py_browse.setFixedSize(26, 24); py_browse.setToolTip("python.exe 찾기")
        py_browse.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:3px;font-size:12px;}")
        py_browse.clicked.connect(self._on_browse_python)
        py_row.addWidget(py_browse)
        py_l.addLayout(py_row)

        # 브릿지 + 클라이언트 생성 버튼
        bridge_row = QHBoxLayout(); bridge_row.setSpacing(4)
        self._bridge_btn = QPushButton("🔌 브릿지 서버 시작")
        self._bridge_btn.setFixedHeight(24)
        self._bridge_btn.setStyleSheet(
            "QPushButton{background:#1a1a3a;color:#f0c040;"
            "border:1px solid #4a4a20;border-radius:3px;font-size:10px;}"
            "QPushButton:hover{background:#22221a;}")
        self._bridge_btn.clicked.connect(self._on_bridge_toggle)
        gen_btn = QPushButton("📄 recorder_bridge.py 생성")
        gen_btn.setFixedHeight(24)
        gen_btn.setStyleSheet(
            "QPushButton{background:#1a2a1a;color:#8fa;"
            "border:1px solid #2a6a2a;border-radius:3px;font-size:10px;}"
            "QPushButton:hover{background:#1e3a1e;}")
        gen_btn.clicked.connect(self._on_gen_bridge)
        self._bridge_status = QLabel("● 미시작")
        self._bridge_status.setStyleSheet("color:#556;font-size:9px;")
        bridge_row.addWidget(self._bridge_btn)
        bridge_row.addWidget(gen_btn)
        bridge_row.addStretch()
        bridge_row.addWidget(self._bridge_status)
        py_l.addLayout(bridge_row)

        # 폴더 일괄 가져오기 ── 수정3
        folder_row = QHBoxLayout(); folder_row.setSpacing(4)
        folder_row.addWidget(QLabel("📁 폴더:"))
        self._watch_ed = QLineEdit(self._kernel.watch_dir)
        self._watch_ed.setPlaceholderText(".py 파일이 있는 폴더 경로")
        self._watch_ed.setStyleSheet(
            "QLineEdit{background:#0a0a18;color:#f0c040;"
            "border:1px solid #3a3a20;border-radius:3px;"
            "padding:2px 6px;font-size:10px;font-family:monospace;}")
        self._watch_ed.textChanged.connect(
            lambda t: setattr(self._kernel, 'watch_dir', t.strip()))
        folder_row.addWidget(self._watch_ed, 1)
        folder_browse = QPushButton("📂")
        folder_browse.setFixedSize(26, 24)
        folder_browse.setStyleSheet(py_browse.styleSheet())
        folder_browse.clicked.connect(self._on_browse_folder)
        folder_row.addWidget(folder_browse)
        load_btn = QPushButton("⬇ 전체 가져오기")
        load_btn.setFixedHeight(24)
        load_btn.setToolTip("폴더 내 .py 파일을 슬롯으로 일괄 추가")
        load_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:3px;font-size:10px;}"
            "QPushButton:hover{background:#22334a;}")
        load_btn.clicked.connect(self._on_load_folder)
        folder_row.addWidget(load_btn)
        py_l.addLayout(folder_row)
        v.addWidget(py_grp)

        # ── 슬롯 관리 바 ──────────────────────────────────────────────────────
        slot_hdr = QHBoxLayout(); slot_hdr.setSpacing(6)
        slot_hdr.addWidget(QLabel("📜 스크립트 슬롯"))
        for lbl, fn, tip, c in [
            ("＋","_add_script",   "슬롯 추가","#1a4a2a"),
            ("－","_del_script",   "선택 슬롯 삭제","#3a1a1a"),
            ("✏","_rename_script","이름 변경","#1a2a3a"),
        ]:
            b = QPushButton(lbl); b.setFixedSize(26, 26)
            b.setToolTip(tip)
            b.setStyleSheet(
                f"QPushButton{{background:{c};color:#ddd;"
                "border:1px solid #3a4a3a;border-radius:3px;}}")
            b.clicked.connect(getattr(self, fn))
            slot_hdr.addWidget(b)
        slot_hdr.addStretch()

        # 전체 RUN 버튼
        self._run_all_btn = QPushButton("▶▶  전체 실행")
        self._run_all_btn.setFixedHeight(26)
        self._run_all_btn.setStyleSheet(
            "QPushButton{background:#1a4a2a;color:#afffcf;"
            "border:1px solid #2a8a5a;border-radius:4px;"
            "font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#225a3a;}"
            "QPushButton:disabled{background:#0a1a0a;color:#3a5a3a;}")
        self._run_all_btn.clicked.connect(self._on_run_all)
        slot_hdr.addWidget(self._run_all_btn)
        self._stop_btn = QPushButton("■  중단")
        self._stop_btn.setFixedHeight(26)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            "QPushButton{background:#3a1a1a;color:#f88;"
            "border:1px solid #6a2a2a;border-radius:4px;font-size:11px;}"
            "QPushButton:hover{background:#5a1a1a;}"
            "QPushButton:disabled{background:#1a1a1a;color:#556;}")
        self._stop_btn.clicked.connect(self._on_stop)
        slot_hdr.addWidget(self._stop_btn)
        v.addLayout(slot_hdr)

        # 슬롯 스크롤 리스트
        self._slot_scroll = QScrollArea()
        self._slot_scroll.setWidgetResizable(True)
        self._slot_scroll.setFixedHeight(130)
        self._slot_scroll.setStyleSheet(
            f"QScrollArea{{border:1px solid #1a2a3a;background:{self._BG};}}"
            "QScrollBar:vertical{background:#0a0a18;width:6px;}"
            "QScrollBar::handle:vertical{background:#336;border-radius:3px;}")
        self._slot_w = QWidget(); self._slot_w.setStyleSheet(f"background:{self._BG};")
        self._slot_lay = QVBoxLayout(self._slot_w)
        self._slot_lay.setContentsMargins(2,2,2,2); self._slot_lay.setSpacing(2)
        self._slot_scroll.setWidget(self._slot_w)
        v.addWidget(self._slot_scroll)

        # ── 에디터 영역 ───────────────────────────────────────────────────────
        ed_grp = QGroupBox("📝 스크립트 편집")
        ed_l = QVBoxLayout(ed_grp); ed_l.setSpacing(6)

        # 옵션 행
        opt_row = QHBoxLayout(); opt_row.setSpacing(10)
        self._enabled_chk = QCheckBox("활성화")
        self._enabled_chk.setChecked(True)
        self._enabled_chk.setStyleSheet(
            "QCheckBox{font-size:11px;font-weight:bold;color:#2ecc71;spacing:5px;}")
        self._enabled_chk.toggled.connect(self._on_enabled_toggled)
        opt_row.addWidget(self._enabled_chk)
        opt_row.addWidget(QLabel("반복:"))
        self._rep_spin = QSpinBox(); self._rep_spin.setRange(0, 9999)
        self._rep_spin.setValue(1); self._rep_spin.setFixedWidth(64)
        self._rep_spin.setSpecialValueText("∞")
        self._rep_spin.setStyleSheet(
            "QSpinBox{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:3px;font-size:12px;font-weight:bold;}")
        self._rep_spin.valueChanged.connect(self._on_rep_changed)
        opt_row.addWidget(self._rep_spin)
        opt_row.addStretch()

        # 파일 IO 버튼
        imp_btn = QPushButton("📂 .py 가져오기")
        imp_btn.setFixedHeight(26)
        imp_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:4px;font-size:10px;}")
        imp_btn.clicked.connect(self._on_import)
        exp_btn = QPushButton("💾 .py 내보내기")
        exp_btn.setFixedHeight(26)
        exp_btn.setStyleSheet(
            "QPushButton{background:#1a2a1a;color:#8fa;"
            "border:1px solid #2a6a2a;border-radius:4px;font-size:10px;}")
        exp_btn.clicked.connect(self._on_export)
        opt_row.addWidget(imp_btn); opt_row.addWidget(exp_btn)

        # ── 환경정보 / CMD 실행 버튼 ──────────────────────────────────────
        env_row = QHBoxLayout(); env_row.setSpacing(6)
        env_btn = QPushButton("🐍 Python 환경 정보")
        env_btn.setFixedHeight(26)
        env_btn.setStyleSheet(
            "QPushButton{background:#1a1a3a;color:#f0c040;"
            "border:1px solid #4a4a20;border-radius:4px;font-size:10px;}"
            "QPushButton:hover{background:#22221a;}")
        env_btn.clicked.connect(self._on_env_info)

        cmd_btn = QPushButton("🖥 CMD에서 실행")
        cmd_btn.setFixedHeight(26)
        cmd_btn.setToolTip(
            "현재 스크립트를 임시 .py로 저장 후\n"
            "cmd 창에서 직접 실행합니다.")
        cmd_btn.setStyleSheet(
            "QPushButton{background:#1a2a1a;color:#8fa;"
            "border:1px solid #2a6a2a;border-radius:4px;font-size:10px;}"
            "QPushButton:hover{background:#1e3a1e;}")
        cmd_btn.clicked.connect(self._on_run_cmd)

        env_row.addWidget(env_btn); env_row.addWidget(cmd_btn)
        env_row.addStretch()
        ed_l.addLayout(opt_row)
        ed_l.addLayout(env_row)

        # 에디터
        self._editor = QPlainTextEdit()
        self._editor.setPlaceholderText(
            "# Python 스크립트를 입력하세요.\n"
            "# 사용 가능한 전역: engine, kernel, log, sys, os, np, cv2\n"
            "# 유틸: pip_install('패키지'), pip_list(), env_info()\n"
            "# 예: engine.start_recording()\n"
            "#     kernel.wait(5)\n"
            "#     engine.stop_recording()")
        self._editor.setStyleSheet(
            "QPlainTextEdit{background:#06060e;color:#9fc;"
            "border:1px solid #1a3a1a;font-family:Consolas,Courier New,monospace;"
            "font-size:11px;}")
        self._editor.setMinimumHeight(160)
        self._editor.textChanged.connect(self._on_code_changed)
        ed_l.addWidget(self._editor, 1)

        # 단일 실행 버튼
        run_row = QHBoxLayout(); run_row.setSpacing(6)
        self._run_one_btn = QPushButton("▶  이 스크립트만 실행")
        self._run_one_btn.setFixedHeight(30)
        self._run_one_btn.setStyleSheet(
            "QPushButton{background:#1a3a4a;color:#7be0e0;"
            "border:1px solid #2a6a7a;border-radius:4px;"
            "font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#22505a;}"
            "QPushButton:disabled{background:#0a1a1a;color:#3a5a5a;}")
        self._run_one_btn.clicked.connect(self._on_run_one)
        run_row.addWidget(self._run_one_btn)
        run_row.addStretch()
        self._run_status = QLabel("● 대기")
        self._run_status.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")
        run_row.addWidget(self._run_status)
        ed_l.addLayout(run_row)
        v.addWidget(ed_grp, 1)

        # ── 실행 로그 ─────────────────────────────────────────────────────────
        log_grp = QGroupBox("📋 커널 실행 로그")
        log_l = QVBoxLayout(log_grp); log_l.setSpacing(4)
        self._log_txt = QPlainTextEdit()
        self._log_txt.setReadOnly(True)
        self._log_txt.setFixedHeight(100)
        self._log_txt.setStyleSheet(
            "QPlainTextEdit{background:#030308;color:#7bc8e0;"
            "font-family:Consolas,Courier New,monospace;font-size:10px;"
            "border:1px solid #0a1a2a;}")
        log_btn_row = QHBoxLayout(); log_btn_row.setSpacing(6)

        clr_btn = QPushButton("🗑 로그 지우기"); clr_btn.setFixedHeight(22)
        clr_btn.setStyleSheet(
            "QPushButton{background:#0a0a1a;color:#556;"
            "border:1px solid #1a1a2a;border-radius:3px;font-size:10px;}")
        clr_btn.clicked.connect(self._log_txt.clear)

        # ★ 추가: 결과 메모장 내보내기 버튼
        self._export_result_btn = QPushButton("📄 결과 내보내기")
        self._export_result_btn.setFixedHeight(22)
        self._export_result_btn.setToolTip(
            "커널 실행 결과(PASS/FAIL)를 텍스트 파일로 저장 후 메모장으로 열기")
        self._export_result_btn.setStyleSheet(
            "QPushButton{background:#0a1a2a;color:#4a9ad4;"
            "border:1px solid #1a3a5a;border-radius:3px;font-size:10px;}"
            "QPushButton:hover{background:#0d2035;}")
        self._export_result_btn.clicked.connect(self._on_export_result)

        log_btn_row.addStretch()
        log_btn_row.addWidget(self._export_result_btn)
        log_btn_row.addWidget(clr_btn)

        log_l.addWidget(self._log_txt)
        log_l.addLayout(log_btn_row)
        v.addWidget(log_grp)

        # 상태 폴링 타이머
        self._poll = QTimer(self); self._poll.timeout.connect(self._poll_status)
        self._poll.start(400)

    # ── 슬롯 위젯 ────────────────────────────────────────────────────────────
    def _rebuild_slots(self):
        # ★ 수정: 기존 row 위젯 완전 제거 — deleteLater()까지 호출
        while self._slot_lay.count():
            it = self._slot_lay.takeAt(0)
            w = it.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self._slot_widgets.clear()
        for i, sc in enumerate(self._kernel.scripts):
            row = self._make_slot_row(i, sc)
            self._slot_widgets.append(row)
            self._slot_lay.addWidget(row)
        self._slot_lay.addStretch()

    def _make_slot_row(self, idx: int, sc: KernelScript) -> QFrame:
        row = QFrame(); row.setFixedHeight(self._SLOT_H)
        row._idx = idx
        is_active = (idx == self._active_idx)
        self._style_slot(row, is_active, sc.enabled)
        row.setCursor(Qt.OpenHandCursor)
        hl = QHBoxLayout(row); hl.setContentsMargins(6,2,6,2); hl.setSpacing(6)
        grip = QLabel("⠿"); grip.setFixedWidth(16)
        grip.setStyleSheet("color:#4a5a8a;font-size:16px;background:transparent;")
        en_lbl = QLabel("●")
        en_lbl.setStyleSheet(
            f"color:{'#2ecc71' if sc.enabled else '#3a3a5a'};"
            "font-size:12px;background:transparent;")
        en_lbl.setFixedWidth(14)
        nm = QLabel(sc.title); nm.setStyleSheet("color:#ccd;font-size:11px;background:transparent;")
        rep_lbl = QLabel(f"×{sc.repeat}" if sc.repeat > 0 else "×∞")
        rep_lbl.setStyleSheet("color:#556;font-size:10px;background:transparent;min-width:30px;")
        hl.addWidget(grip); hl.addWidget(en_lbl); hl.addWidget(nm, 1); hl.addWidget(rep_lbl)
        row.mousePressEvent   = lambda e, i=idx, r=row: self._slot_press(e, i, r)
        row.mouseMoveEvent    = lambda e, r=row: self._slot_move(e, r)
        row.mouseReleaseEvent = lambda e, r=row: self._slot_release(e, r)
        row.mouseDoubleClickEvent = lambda e, i=idx: self._select_slot(i)
        return row

    def _style_slot(self, row, active: bool, enabled: bool):
        c = "#0d1a2a" if active else self._BG
        border = "#3a7aaa" if active else ("#1a1a3a" if enabled else "#0d0d1a")
        row.setStyleSheet(
            f"QFrame{{background:{c};border:1px solid {border};border-radius:4px;}}")

    def _select_slot(self, idx: int):
        self._active_idx = idx
        self._rebuild_slots()
        if 0 <= idx < len(self._kernel.scripts):
            sc = self._kernel.scripts[idx]
            self._editor.blockSignals(True)
            self._editor.setPlainText(sc.code)
            self._editor.blockSignals(False)
            self._enabled_chk.blockSignals(True)
            self._enabled_chk.setChecked(sc.enabled)
            self._enabled_chk.blockSignals(False)
            self._rep_spin.blockSignals(True)
            self._rep_spin.setValue(sc.repeat)
            self._rep_spin.blockSignals(False)

    # ── 드래그앤드랍 순서변경 ────────────────────────────────────────────────
    def _slot_press(self, e, idx, row):
        if e.button() == Qt.LeftButton:
            self._drag_idx   = idx
            self._drag_start = e.pos()
            self._dragging   = False
            self._select_slot(idx)

    def _slot_move(self, e, row):
        if self._drag_idx >= 0 and not self._dragging:
            if (e.pos() - self._drag_start).manhattanLength() > 6:
                self._dragging = True
                row.setStyleSheet(
                    f"QFrame{{background:{self._BG_DG};"
                    "border:1px solid #5a7aaa;border-radius:4px;}}")
                row.setCursor(Qt.ClosedHandCursor)

    def _slot_release(self, e, row):
        if self._dragging and self._drag_idx >= 0:
            gpos = row.mapToGlobal(e.pos())
            tgt  = self._find_slot_at_global(gpos)
            sc   = self._kernel.scripts
            if 0 <= tgt < len(sc) and tgt != self._drag_idx:
                sc.insert(tgt, sc.pop(self._drag_idx))
                self._active_idx = tgt
                self._rebuild_slots()
        self._drag_idx = -1; self._dragging = False

    def _find_slot_at_global(self, gpos: QPoint) -> int:
        for i, row in enumerate(self._slot_widgets):
            local = row.mapFromGlobal(gpos)
            if row.rect().contains(local): return i
        return -1

    # ── 슬롯 CRUD ────────────────────────────────────────────────────────────
    def _add_script(self):
        """슬롯 추가 — 모달창 없이 즉시 생성, 이름은 더블클릭으로 편집."""
        n  = len(self._kernel.scripts) + 1
        sc = KernelScript(f"스크립트 {n}")
        self._kernel.scripts.append(sc)
        self._rebuild_slots()
        new_idx = len(self._kernel.scripts) - 1
        self._select_slot(new_idx)
        # 이름 편집 필요 시 ✏ 버튼 또는 더블클릭 사용 안내
        self._on_log(f"슬롯 추가: '{sc.title}'  — ✏ 버튼으로 이름 변경 가능")

    def _del_script(self):
        idx = self._active_idx
        if not 0 <= idx < len(self._kernel.scripts): return
        self._kernel.scripts.pop(idx)
        new_idx = max(0, idx - 1)
        self._active_idx = new_idx if self._kernel.scripts else -1
        self._rebuild_slots()
        if self._active_idx >= 0:
            self._select_slot(self._active_idx)
        else:
            self._editor.clear()

    def _rename_script(self):
        # ★ 중복 실행 방지 — 이미 다이얼로그가 열려있으면 무시
        if getattr(self, '_renaming', False):
            return
        idx = self._active_idx
        if not 0 <= idx < len(self._kernel.scripts): return
        self._renaming = True
        try:
            sc = self._kernel.scripts[idx]
            new_name, ok = QInputDialog.getText(
                self, "슬롯 이름 변경", "새 이름:", text=sc.title)
            if ok and new_name.strip():
                sc.title = new_name.strip()
                self._rebuild_slots()
        finally:
            self._renaming = False

    # ── 편집 동기화 ───────────────────────────────────────────────────────────
    def _on_code_changed(self):
        idx = self._active_idx
        if 0 <= idx < len(self._kernel.scripts):
            self._kernel.scripts[idx].code = self._editor.toPlainText()

    def _on_enabled_toggled(self, v: bool):
        idx = self._active_idx
        if 0 <= idx < len(self._kernel.scripts):
            self._kernel.scripts[idx].enabled = v
            self._rebuild_slots()

    def _on_rep_changed(self, v: int):
        idx = self._active_idx
        if 0 <= idx < len(self._kernel.scripts):
            self._kernel.scripts[idx].repeat = v

    # ── 파일 IO ───────────────────────────────────────────────────────────────
    # ── 인터프리터·브릿지·폴더 메서드 ──────────────────────────────────────
    def _on_browse_python(self):
        """python.exe 파일 선택 다이얼로그."""
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "python.exe 선택", "",
            "Python Executable (python.exe python3 python3.*);;All Files (*)")
        if path:
            self._py_ed.setText(path)
            self._kernel.python_exe = path
            self._on_log(f"[인터프리터] {path}")

    def _on_browse_folder(self):
        """폴더 선택 다이얼로그."""
        from PyQt5.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(
            self, ".py 파일 폴더 선택", "")
        if folder:
            self._watch_ed.setText(folder)
            self._kernel.watch_dir = folder

    def _on_load_folder(self):
        """
        지정 폴더 내 .py 파일을 모두 슬롯으로 가져오기.
        기존 슬롯과 이름이 겹치면 건너뜀.
        """
        folder = self._watch_ed.text().strip()
        if not folder or not os.path.isdir(folder):
            self._on_log("❌ 유효한 폴더 경로를 입력하세요."); return
        py_files = sorted(
            f for f in os.listdir(folder)
            if f.endswith('.py') and not f.startswith('_'))
        if not py_files:
            self._on_log(f"📁 .py 파일 없음: {folder}"); return

        existing = {sc.title for sc in self._kernel.scripts}
        added = 0
        for fname in py_files:
            title = os.path.splitext(fname)[0]
            if title in existing:
                self._on_log(f"  건너뜀 (이미 있음): {fname}"); continue
            fpath = os.path.join(folder, fname)
            try:
                with open(fpath, encoding='utf-8', errors='replace') as f:
                    code = f.read()
            except Exception as ex:
                self._on_log(f"  ❌ 읽기 실패 {fname}: {ex}"); continue
            sc = KernelScript(title=title, code=code)
            self._kernel.scripts.append(sc)
            existing.add(title)
            added += 1
        self._rebuild_slots()
        self._on_log(f"✅ {added}개 슬롯 추가 (폴더: {folder})")
        if self._kernel.scripts:
            self._select_slot(len(self._kernel.scripts) - 1)

    def _on_bridge_toggle(self):
        """브릿지 서버 시작/중단 토글."""
        if self._kernel._bridge_server and self._kernel._bridge_server.is_alive():
            self._kernel.stop_bridge()
            self._bridge_btn.setText("🔌 브릿지 서버 시작")
            self._bridge_status.setText("● 중단됨")
            self._bridge_status.setStyleSheet("color:#e74c3c;font-size:9px;")
            self._on_log("[Bridge] 서버 중단")
        else:
            self._kernel.start_bridge()
            self._bridge_btn.setText("🔴 브릿지 서버 중단")
            self._bridge_status.setText(f"● 실행 중 :{self._kernel.BRIDGE_PORT}")
            self._bridge_status.setStyleSheet("color:#2ecc71;font-size:9px;")
            self._on_log(f"[Bridge] 서버 시작 — localhost:{self._kernel.BRIDGE_PORT}")

    def _on_gen_bridge(self):
        """recorder_bridge.py 생성 후 폴더 열기."""
        path = self._kernel.generate_bridge_client()
        if path:
            self._on_log(f"✅ recorder_bridge.py 생성: {path}")
            open_folder(os.path.dirname(path))

    def _on_import(self):
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, ".py 파일 가져오기", "", "Python Files (*.py);;All Files (*)")
        if not path: return
        try:
            with open(path, encoding='utf-8') as f:
                code = f.read()
            if self._active_idx >= 0:
                self._kernel.scripts[self._active_idx].code = code
                sc_name = os.path.splitext(os.path.basename(path))[0]
                self._kernel.scripts[self._active_idx].title = sc_name
            self._editor.blockSignals(True)
            self._editor.setPlainText(code)
            self._editor.blockSignals(False)
            self._rebuild_slots()
            self._signals.status_message.emit(f"[Kernel] 가져오기: {path}")
        except Exception as ex:
            self._signals.status_message.emit(f"[Kernel] 가져오기 실패: {ex}")

    def _on_env_info(self):
        """Python 환경 정보를 로그 패널에 출력."""
        info = self._kernel.get_env_info()
        self._on_log("[환경 정보]\n" + info)

    def _on_run_cmd(self):
        """
        현재 스크립트를 임시 .py로 저장 후 CMD 창에서 직접 실행.
        EXE 배포 환경에서도 동일하게 동작.
        """
        self._save_current()
        idx = self._active_idx
        if not 0 <= idx < len(self._kernel.scripts):
            self._on_log("❌ 실행할 스크립트를 선택하세요.")
            return
        sc   = self._kernel.scripts[idx]
        base = self._kernel._engine.base_dir
        os.makedirs(base, exist_ok=True)
        safe = re.sub(r'[\\/:*?"<>|]', '_', sc.title)
        tmp_path = os.path.join(base, f"_kernel_{safe}.py")

        # 헤더 주입: engine/kernel 없이 독립 실행 가능한 래퍼
        header = f"""\
# -*- coding: utf-8 -*-
# 자동 생성된 커널 스크립트 실행 파일 — {sc.title}
# 실행: python "{tmp_path}"
import sys, os, time, datetime, json, re, threading, platform, subprocess

# ── 더미 kernel/engine (CMD 독립 실행 시) ────────────────────────────
class _DummyKernel:
    def is_stopped(self): return False
    def wait(self, s): time.sleep(s)
    def set_tc_result(self, r): print(f"[TC] {{r}}")
    def read_roi_value(self, *a, **kw): return None
    def get_roi_avg(self, *a, **kw): return None
    def read_roi_text(self, *a, **kw): return ""
    def read_all_roi(self, source="screen"): return {{}}
    def read_roi_number(self, *a, **kw): return None
    def get_roi_last_text(self, *a, **kw): return ""
    def roi_text_equals(self, *a, **kw): return False
    def roi_text_contains(self, *a, **kw): return False
    def roi_number_equals(self, *a, **kw): return False
    def roi_number_compare(self, *a, **kw): return False
    def roi_match(self, *a, **kw): return False
    def emit_status(self, m): print(f"[STATUS] {{m}}")
    def power_connect(self, port, baudrate=9600): print(f"[PWR] connect {{port}}@{{baudrate}}"); return False
    def power_disconnect(self): print("[PWR] disconnect")
    def power_on(self, ch): print(f"[PWR] ON {{ch}}"); return False
    def power_off(self, ch): print(f"[PWR] OFF {{ch}}"); return False
    def power_all_on(self, delay=0.3): print("[PWR] ALL ON"); return False
    def power_all_off(self): print("[PWR] ALL OFF"); return False
    def power_send(self, cmd): print(f"[PWR] send {{cmd!r}}"); return False
    def power_read(self, timeout=0.2): return ""
    def power_list_ports(self): return []
    @property
    def power_connected(self): return False

class _DummyEngine:
    recording = False
    def start_recording(self): print("[ENG] start_recording")
    def stop_recording(self): print("[ENG] stop_recording")
    def save_manual_clip(self): print("[ENG] save_manual_clip"); return True
    def capture_frame(self, src="screen", tc_tag=""): print(f"[ENG] capture {{src}} {{tc_tag}}"); return ""
    def start_ac(self): print("[ENG] start_ac")
    def stop_ac(self): print("[ENG] stop_ac")
    def macro_start_run(self, *a, **kw): print("[ENG] macro_run")
    def macro_stop_run(self): print("[ENG] macro_stop")

def pip_install(*pkgs):
    for pkg in pkgs:
        subprocess.run([sys.executable, "-m", "pip", "install", pkg])

def pip_list():
    subprocess.run([sys.executable, "-m", "pip", "list"])

def env_info():
    print(f"Python {{sys.version}}")
    print(f"실행파일: {{sys.executable}}")

kernel = _DummyKernel()
engine = _DummyEngine()
log    = print

try: import numpy as np
except ImportError: np = None
try: import cv2
except ImportError: cv2 = None

# ── 사용자 스크립트 ────────────────────────────────────────────────────
if __name__ == "__main__":
"""
        # 사용자 코드를 들여쓰기하여 if __name__ 블록 안에 삽입
        indented = "\n".join(
            "    " + ln for ln in sc.code.split("\n"))
        full_code = header + indented + "\n"

        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write(full_code)
            self._on_log(f"✅ 스크립트 저장: {tmp_path}")

            # 플랫폼별 CMD 실행
            if platform.system() == "Windows":
                subprocess.Popen(
                    f'start cmd /K "{sys.executable}" "{tmp_path}"',
                    shell=True)
            elif platform.system() == "Darwin":
                subprocess.Popen(
                    ["open", "-a", "Terminal",
                     sys.executable, tmp_path])
            else:
                subprocess.Popen(
                    ["x-terminal-emulator", "-e",
                     sys.executable, tmp_path])
            self._on_log("✅ CMD 창에서 스크립트 실행 시작")
        except Exception as ex:
            self._on_log(f"❌ CMD 실행 실패: {ex}")

    def _on_export(self):
        from PyQt5.QtWidgets import QFileDialog
        idx = self._active_idx
        if not 0 <= idx < len(self._kernel.scripts): return
        sc = self._kernel.scripts[idx]
        safe = re.sub(r'[\\/:*?"<>|]', '_', sc.title)
        path, _ = QFileDialog.getSaveFileName(
            self, ".py 파일 내보내기",
            os.path.join(self._kernel._engine.base_dir, f"{safe}.py"),
            "Python Files (*.py);;All Files (*)")
        if not path: return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(sc.code)
            self._signals.status_message.emit(f"[Kernel] 내보내기: {path}")
            open_folder(os.path.dirname(path))
        except Exception as ex:
            self._signals.status_message.emit(f"[Kernel] 내보내기 실패: {ex}")

    def _on_export_result(self):
        """
        커널 실행 결과를 텍스트 파일로 저장 후 메모장으로 열기.
        결과가 없으면 안내 메시지 표시.
        """
        log = self._kernel.result_log
        if not log._entries:
            QMessageBox.information(
                self, "결과 없음",
                "아직 커널 실행 결과가 없습니다.\n"
                "스크립트를 실행한 후 사용하세요.")
            return

        base_dir = getattr(self._kernel._engine, 'base_dir',
                           os.path.join(os.path.expanduser("~/Desktop"), "bltn_rec"))
        log_dir  = os.path.join(base_dir, "kernel_logs")
        path     = log.open_in_notepad(log_dir)

        if path:
            self._signals.status_message.emit(
                f"[Kernel] 결과 저장: {path}")
            self._on_log(f"📄 결과 내보내기: {path}")
        else:
            QMessageBox.warning(self, "저장 실패",
                                "결과 파일 저장에 실패했습니다.")

    # ── 실행 제어 ─────────────────────────────────────────────────────────────
    def _on_run_all(self):
        if self._kernel.running: return
        self._save_current()
        self._kernel.start(list(self._kernel.scripts))

    def _on_run_one(self):
        idx = self._active_idx
        if not 0 <= idx < len(self._kernel.scripts): return
        self._save_current()
        sc = self._kernel.scripts[idx]
        self._kernel.start([sc])

    def _on_stop(self):
        self._kernel.stop()

    def _save_current(self):
        """편집 중인 내용 저장."""
        idx = self._active_idx
        if 0 <= idx < len(self._kernel.scripts):
            self._kernel.scripts[idx].code = self._editor.toPlainText()

    def _poll_status(self):
        running = self._kernel.running
        self._run_all_btn.setEnabled(not running)
        self._run_one_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        if running:
            self._run_status.setText(f"● 실행 중: {self._kernel.cur_title}")
            self._run_status.setStyleSheet("color:#2ecc71;font-size:11px;font-weight:bold;")
        else:
            self._run_status.setText("● 대기")
            self._run_status.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")

    def _on_log(self, msg: str):
        """KernelEngine 로그 콜백 → 로그 패널 출력."""
        self._log_txt.appendPlainText(msg)
        self._log_txt.verticalScrollBar().setValue(
            self._log_txt.verticalScrollBar().maximum())

    # ── DB 저장/복원 ─────────────────────────────────────────────────────────
    def get_scripts_data(self) -> list:
        self._save_current()
        return self._kernel.get_scripts_data()

    def set_scripts_data(self, data: list):
        # ★ 수정: 에디터 textChanged 시그널 차단 후 슬롯 교체
        #   _select_slot 내부의 setPlainText가 textChanged를 일으켜
        #   _save_current가 엉뚱한 슬롯에 덮어쓰는 것을 방지
        self._editor.blockSignals(True)
        try:
            self._kernel.set_scripts_data(data)
            self._active_idx = -1   # 초기화 후 재선택
            self._rebuild_slots()
            if self._kernel.scripts:
                self._select_slot(0)
        finally:
            self._editor.blockSignals(False)


# =============================================================================
#  PowerController  — 시리얼 전원 제어 캡슐화 (KernelEngine / UI 공용)
# =============================================================================
class PowerController:
    """
    Arduino 시리얼 전원 제어기.
    KernelEngine과 PowerControlPanel이 같은 인스턴스를 공유한다.

    채널 명령 표:
      채널          ON    OFF
      B+ (Battery)  "1"   "Q"
      TG B+         "2"   "W"
      ACC           "3"   "E"
      IGN           "4"   "R"
      ALL OFF             "0"
    """

    # 채널 이름 → (ON 명령, OFF 명령)
    CHANNELS: dict = {
        "bplus":    ("1", "Q"),
        "tg_bplus": ("2", "W"),
        "acc":      ("3", "E"),
        "ign":      ("4", "R"),
    }
    ALL_OFF_CMD = "0"

    def __init__(self):
        self._ser     = None          # serial.Serial 인스턴스
        self._port    = ""
        self._lock    = threading.Lock()
        self._log_cb  = None          # 로그 콜백 (str → None)

    # ── 공개 API ──────────────────────────────────────────────────────────────
    def connect(self, port: str, baudrate: int = 9600) -> bool:
        """
        시리얼 포트 연결.
        Parameters
        ----------
        port     : 포트 이름 (예: "COM3", "/dev/ttyUSB0")
        baudrate : 보드레이트 (기본 9600)
        Returns
        -------
        True : 연결 성공, False : 실패
        """
        try:
            import serial as _serial
        except ImportError:
            self._log("❌ pyserial 미설치 — pip install pyserial")
            return False
        with self._lock:
            if self._ser and self._ser.is_open:
                self._ser.close()
            try:
                self._ser = _serial.Serial(port, baudrate, timeout=1)
                self._port = port
                self._log(f"[Power] 연결: {port} @ {baudrate}bps")
                return True
            except Exception as ex:
                self._log(f"[Power] 연결 실패: {ex}")
                return False

    def disconnect(self):
        """시리얼 포트 연결 해제."""
        with self._lock:
            if self._ser and self._ser.is_open:
                self._ser.close()
            self._ser  = None
            self._port = ""
        self._log("[Power] 연결 해제")

    @property
    def is_connected(self) -> bool:
        """현재 시리얼 연결 상태."""
        return bool(self._ser and self._ser.is_open)

    @property
    def port(self) -> str:
        """현재 연결된 포트 이름."""
        return self._port

    def send(self, cmd: str) -> bool:
        """
        임의 명령 문자열 전송.
        Parameters
        ----------
        cmd : 전송할 ASCII 명령 (예: "1", "Q", "0")
        Returns
        -------
        True : 전송 성공, False : 연결 없음 / 실패
        """
        with self._lock:
            if not (self._ser and self._ser.is_open):
                self._log("[Power] ❌ 미연결 — send 실패")
                return False
            try:
                self._ser.write(cmd.encode())
                self._log(f"[Power TX] {cmd!r}")
                return True
            except Exception as ex:
                self._log(f"[Power TX ERR] {ex}")
                return False

    def channel_on(self, channel: str) -> bool:
        """
        지정 채널 ON.
        Parameters
        ----------
        channel : "bplus" | "tg_bplus" | "acc" | "ign"
        """
        ch = channel.lower().strip()
        if ch not in self.CHANNELS:
            self._log(f"[Power] ❌ 알 수 없는 채널: {channel!r}  (가능: {list(self.CHANNELS)})")
            return False
        return self.send(self.CHANNELS[ch][0])

    def channel_off(self, channel: str) -> bool:
        """
        지정 채널 OFF.
        Parameters
        ----------
        channel : "bplus" | "tg_bplus" | "acc" | "ign"
        """
        ch = channel.lower().strip()
        if ch not in self.CHANNELS:
            self._log(f"[Power] ❌ 알 수 없는 채널: {channel!r}  (가능: {list(self.CHANNELS)})")
            return False
        return self.send(self.CHANNELS[ch][1])

    def all_off(self) -> bool:
        """전체 전원 OFF (비상 정지)."""
        self._log("[Power] ⚠ ALL OFF")
        return self.send(self.ALL_OFF_CMD)

    def list_ports(self) -> list:
        """
        사용 가능한 시리얼 포트 목록 반환.
        Returns
        -------
        list[str] : 포트 이름 목록 (예: ["COM3", "COM5"])
        """
        try:
            import serial.tools.list_ports as _lp
            ports = [p.device for p in _lp.comports()]
            self._log(f"[Power] 포트 목록: {ports}")
            return ports
        except ImportError:
            self._log("[Power] ❌ pyserial 미설치")
            return []

    def read(self, timeout: float = 0.2) -> str:
        """
        수신 버퍼에서 데이터 읽기.
        Parameters
        ----------
        timeout : 최대 대기 시간(초)
        Returns
        -------
        str : 수신 데이터 (없으면 빈 문자열)
        """
        import time as _time
        deadline = _time.time() + timeout
        buf = ""
        with self._lock:
            if not (self._ser and self._ser.is_open):
                return ""
            while _time.time() < deadline:
                if self._ser.in_waiting:
                    buf += self._ser.read(self._ser.in_waiting).decode(errors='replace')
                else:
                    _time.sleep(0.02)
        return buf.strip()

    # ── 내부 ─────────────────────────────────────────────────────────────────
    def _log(self, msg: str):
        if self._log_cb:
            try: self._log_cb(msg)
            except: pass

    def set_log_callback(self, cb):
        """로그 콜백 설정 (str → None)."""
        self._log_cb = cb

# ═══ v2.9 추가 모듈 끝 ═══════════════════════════════════════════════
class PowerControlPanel(QWidget):
    _CONTROLS = [
        ("B+ (Battery)",    "bplus"),
        ("TG B+ (Target)",  "tg_bplus"),
        ("ACC (Accessory)", "acc"),
        ("IGN (Ignition)",  "ign"),
    ]

    def __init__(self, power_ctrl: 'PowerController', parent=None):
        super().__init__(parent)
        # ★ 수정: 공유 PowerController 인스턴스 사용
        self._ctrl = power_ctrl
        self._ctrl.set_log_callback(self._append_log)
        self._build()

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # ── 포트 선택 ──
        pg = QGroupBox("🔌 시리얼 포트")
        pl = QHBoxLayout(pg); pl.setSpacing(6)
        self._port_cb = QComboBox()
        self._port_cb.setStyleSheet(
            "QComboBox{background:#1a1a3a;color:#ddd;"
            "border:1px solid #3a4a6a;border-radius:3px;padding:2px 6px;}")
        self._refresh_ports()

        ref_btn = QPushButton("🔄")
        ref_btn.setFixedSize(28, 28)
        ref_btn.setToolTip("포트 목록 새로고침")
        ref_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#9ab;"
            "border:1px solid #2a3a5a;border-radius:3px;}")
        ref_btn.clicked.connect(self._refresh_ports)

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
        pl.addWidget(self._conn_btn)
        pl.addWidget(self._conn_status)
        v.addWidget(pg)

        # ── 채널별 ON/OFF ──
        cg = QGroupBox("🔋 전원 채널 제어")
        cl = QVBoxLayout(cg); cl.setSpacing(6)

        _COLORS = ["#2ecc71", "#3498db", "#f0c040", "#e67e22"]
        for i, (name, ch_key) in enumerate(self._CONTROLS):
            row = QHBoxLayout(); row.setSpacing(6)
            lbl = QLabel(name)
            lbl.setFixedWidth(130)
            lbl.setStyleSheet(
                f"color:{_COLORS[i % len(_COLORS)]};font-size:11px;font-weight:bold;")

            btn_on = QPushButton("▶ ON")
            btn_on.setMinimumHeight(32)
            btn_on.setStyleSheet(
                "QPushButton{background:#1a4a2a;color:#afffcf;"
                "border:1px solid #2a8a5a;border-radius:4px;font-weight:bold;}"
                "QPushButton:hover{background:#225a3a;}"
                "QPushButton:disabled{background:#0a1a0a;color:#3a5a3a;}")
            btn_on.clicked.connect(
                lambda _=False, ch=ch_key: self._ctrl.channel_on(ch))

            btn_off = QPushButton("■ OFF")
            btn_off.setMinimumHeight(32)
            btn_off.setStyleSheet(
                "QPushButton{background:#3a1a1a;color:#ffaaaa;"
                "border:1px solid #6a2a2a;border-radius:4px;font-weight:bold;}"
                "QPushButton:hover{background:#5a1a1a;}"
                "QPushButton:disabled{background:#1a0a0a;color:#5a3a3a;}")
            btn_off.clicked.connect(
                lambda _=False, ch=ch_key: self._ctrl.channel_off(ch))

            row.addWidget(lbl)
            row.addWidget(btn_on, 1)
            row.addWidget(btn_off, 1)
            cl.addLayout(row)

        v.addWidget(cg)

        # ── 전체 OFF ──
        all_off = QPushButton("⚠  ALL POWER OFF  (비상)")
        all_off.setMinimumHeight(44)
        all_off.setStyleSheet(
            "QPushButton{background:#7f0000;color:white;"
            "font-size:13px;font-weight:bold;"
            "border:2px solid #e74c3c;border-radius:6px;}"
            "QPushButton:hover{background:#a00000;}"
            "QPushButton:disabled{background:#2a0a0a;color:#5a3a3a;}")
        all_off.clicked.connect(lambda: self._ctrl.all_off())
        v.addWidget(all_off)

        # ── 수신 로그 ──
        lg = QGroupBox("📟 수신 로그")
        ll = QVBoxLayout(lg)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFixedHeight(70)
        self._log.setStyleSheet(
            "QPlainTextEdit{background:#030308;color:#7bc8e0;"
            "font-family:Consolas,monospace;font-size:10px;"
            "border:1px solid #0a1a2a;}")
        ll.addWidget(self._log)
        v.addWidget(lg)

        # 수신 폴링 타이머
        self._rx_timer = QTimer(self)
        self._rx_timer.timeout.connect(self._poll_rx)
        self._rx_timer.start(100)

    def _refresh_ports(self):
        self._port_cb.clear()
        ports = self._ctrl.list_ports()
        if ports:
            for p in ports:
                self._port_cb.addItem(p)
        else:
            self._port_cb.addItem("(포트 없음)")

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
        """PowerController 로그 콜백 → 수신 로그 패널."""
        self._log.appendPlainText(msg)
        self._log.verticalScrollBar().setValue(
            self._log.verticalScrollBar().maximum())

    def _poll_rx(self):
        """수신 폴링 — PowerController를 통해 읽기."""
        if not self._ctrl.is_connected:
            return
        data = self._ctrl.read(timeout=0.0)
        if data:
            self._log.appendPlainText(f"[RX] {data}")
            self._log.verticalScrollBar().setValue(
                self._log.verticalScrollBar().maximum())

    def closeEvent(self, e):
        # PowerController 연결은 MainWindow.closeEvent에서 일괄 해제
        super().closeEvent(e)


class ResetPanel(QWidget):
    def __init__(self, engine: CoreEngine, db: SettingsDB,
                 signals: Signals, parent=None):
        super().__init__(parent)
        self.engine = engine; self.db = db; self.signals = signals
        self._build()

    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(12)

        warn = QLabel(
            "⚠  주의: 아래 버튼들은 설정을 영구적으로 초기화합니다.\n"
            "녹화 중에는 사용을 피하세요.")
        warn.setWordWrap(True)
        warn.setStyleSheet(
            "color:#e74c3c;font-size:11px;font-weight:bold;"
            "background:#1a0a0a;border:1px solid #4a1a1a;"
            "border-radius:4px;padding:8px;")
        v.addWidget(warn)

        items = [
            ("🔴 전체 설정 초기화",
             "모든 설정(DB)을 초기화합니다. ROI, 메모, 매크로, 경로 설정 포함.",
             self._reset_all, "#3a1a1a", "#e74c3c"),
            ("🟠 ROI 목록만 초기화",
             "Screen/Camera ROI 목록을 모두 삭제합니다.",
             self._reset_roi, "#2a1a0a", "#e67e22"),
            ("🟡 블랙아웃 카운터 초기화",
             "Screen/Camera 블랙아웃 감지 횟수와 이벤트 로그를 초기화합니다.",
             self._reset_bo,  "#2a2a0a", "#f0c040"),
            ("🔵 오토클릭 카운터 초기화",
             "오토클릭 횟수 카운터를 0으로 리셋합니다.",
             self._reset_ac,  "#0a1a2a", "#3498db"),
            ("⚪ IO채널 DB 초기화",
             "AI/MCP 외부제어 I/O 채널 DB (commands/state/events)를 초기화합니다.",
             self._reset_io,  "#1a1a2a", "#888"),
        ]

        for title, desc, fn, bg, fg in items:
            grp = QGroupBox(); gl = QVBoxLayout(grp); gl.setSpacing(6)
            grp.setStyleSheet(
                f"QGroupBox{{background:{bg};border:1px solid #2a2a3a;"
                "border-radius:4px;margin-top:0px;}")
            d = QLabel(desc); d.setWordWrap(True)
            d.setStyleSheet("color:#889;font-size:10px;")
            btn = QPushButton(title); btn.setMinimumHeight(34)
            btn.setStyleSheet(
                f"QPushButton{{background:#0d0d1e;color:{fg};"
                "border:1px solid #2a2a3a;border-radius:4px;"
                "font-size:11px;font-weight:bold;}}"
                f"QPushButton:hover{{background:{bg};border-color:{fg};}}")
            btn.clicked.connect(fn)
            gl.addWidget(d); gl.addWidget(btn)
            v.addWidget(grp)

        v.addStretch()

    def _confirm(self, msg: str) -> bool:
        r = QMessageBox.question(
            self, "확인", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return r == QMessageBox.Yes

    def _reset_all(self):
        if not self._confirm("전체 설정을 초기화합니까?\n이 작업은 되돌릴 수 없습니다."): return
        self.db.wipe()
        self.engine.screen_rois.clear(); self.engine.camera_rois.clear()
        self.engine.screen_bo_count = self.engine.camera_bo_count = 0
        self.engine.screen_bo_events.clear(); self.engine.camera_bo_events.clear()
        self.engine.ac_count = 0; self.engine.macro_steps.clear()
        self.signals.status_message.emit("✅ 전체 설정 초기화 완료")
        self.signals.roi_list_changed.emit()
        self.signals.ac_count_changed.emit(0)

    def _reset_roi(self):
        if not self._confirm("ROI 목록을 모두 삭제합니까?"): return
        self.engine.screen_rois.clear(); self.engine.camera_rois.clear()
        self.db.save_roi_items([])
        self.signals.roi_list_changed.emit()
        self.signals.status_message.emit("✅ ROI 초기화 완료")

    def _reset_bo(self):
        if not self._confirm("블랙아웃 카운터와 이벤트 로그를 초기화합니까?"): return
        self.engine.screen_bo_count = self.engine.camera_bo_count = 0
        self.engine.screen_bo_events.clear(); self.engine.camera_bo_events.clear()
        self.signals.status_message.emit("✅ 블랙아웃 카운터 초기화 완료")

    def _reset_ac(self):
        self.engine.reset_ac_count()
        self.signals.status_message.emit("✅ 오토클릭 카운터 초기화 완료")

    def _reset_io(self):
        if not self._confirm("AI/MCP I/O채널 DB를 초기화합니까?"): return
        io_ch = getattr(self.engine, 'io_channel', None)
        if io_ch:
            try:
                with io_ch._conn() as c:
                    c.executescript(
                        "DELETE FROM commands;"
                        "DELETE FROM state;"
                        "DELETE FROM events;")
                self.signals.status_message.emit("✅ IO채널 DB 초기화 완료")
            except Exception as ex:
                self.signals.status_message.emit(f"IO채널 초기화 실패: {ex}")
        else:
            self.signals.status_message.emit("IO채널 미초기화 (io_channel=None)")


# =============================================================================
#  LogPanel
# =============================================================================
class LogPanel(QWidget):
    MAX_LINES = 500

    def __init__(self, signals: Signals, parent=None):
        super().__init__(parent)
        self.signals = signals
        self._lines: deque = deque(maxlen=self.MAX_LINES)
        self._build()
        signals.status_message.connect(self._append)

    def _build(self):
        v = QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(6)

        ctrl = QHBoxLayout(); ctrl.setSpacing(6)
        self.auto_scroll = QCheckBox("자동 스크롤"); self.auto_scroll.setChecked(True)
        self.auto_scroll.setStyleSheet("font-size:11px;color:#9ab;")
        ctrl.addWidget(self.auto_scroll)
        clr = QPushButton("로그 지우기"); clr.setFixedHeight(26)
        clr.setStyleSheet(
            "QPushButton{background:#1a1a2a;color:#888;border:1px solid #334;"
            "border-radius:3px;font-size:10px;}")
        clr.clicked.connect(self._clear)
        ctrl.addWidget(clr); ctrl.addStretch()
        v.addLayout(ctrl)

        self.log_txt = QPlainTextEdit()
        self.log_txt.setReadOnly(True)
        self.log_txt.setStyleSheet(
            "QPlainTextEdit{background:#060612;color:#9ab;font-family:Consolas,"
            "Courier New,monospace;font-size:11px;border:1px solid #1a2a3a;"
            "border-radius:4px;}")
        v.addWidget(self.log_txt)

    def _append(self, msg: str):
        ts  = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}]  {msg}"
        self._lines.append(line)
        self.log_txt.appendPlainText(line)
        if self.auto_scroll.isChecked():
            self.log_txt.verticalScrollBar().setValue(
                self.log_txt.verticalScrollBar().maximum())

    def _clear(self):
        self._lines.clear(); self.log_txt.clear()


# =============================================================================
#  CameraWindow / DisplayWindow  (플로팅 미리보기)
# =============================================================================
class _FloatingPreview(QWidget):
    """카메라 또는 스크린 플로팅 미리보기 창."""
    def __init__(self, title: str, source: str,
                 engine: CoreEngine, parent=None):
        super().__init__(parent,
            Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint |
            Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint)
        self.setWindowTitle(title)
        self.source  = source
        self.engine  = engine
        self._visible = False
        v = QVBoxLayout(self); v.setContentsMargins(4,4,4,4); v.setSpacing(4)

        # ── 내부 ON/OFF 버튼 (창 닫지 않고 영상처리만 중지) ── ★ v2.9.3
        ctrl = QHBoxLayout(); ctrl.setSpacing(6)
        _is_disp = (source == "screen")
        _color   = "#7bc8e0" if _is_disp else "#f0c040"
        self._preview_on_btn = QPushButton("▶ 출력 ON")
        self._preview_on_btn.setCheckable(True)
        self._preview_on_btn.setChecked(True)
        self._preview_on_btn.setFixedHeight(24)
        self._preview_on_btn.setStyleSheet(
            f"QPushButton{{background:#1a2a3a;color:{_color};"
            "border:1px solid #2a4a6a;border-radius:3px;font-size:10px;font-weight:bold;}"
            f"QPushButton:!checked{{background:#0a0a18;color:#334;border-color:#1a1a2a;}}"
            "QPushButton:hover{background:#22334a;}")
        self._preview_on_btn.toggled.connect(self._on_preview_toggle)

        _src_lbl = "🖥 Display" if _is_disp else "📷 Camera"
        src_info = QLabel(_src_lbl)
        src_info.setStyleSheet(f"color:{_color};font-size:10px;font-weight:bold;")
        # ★ Always-on-top 토글 버튼
        self._top_btn = QPushButton("📌 최상위")
        self._top_btn.setCheckable(True)
        self._top_btn.setFixedHeight(24)
        self._top_btn.setStyleSheet(
            "QPushButton{background:#1a1a3a;color:#aaa;"
            "border:1px solid #2a2a5a;border-radius:3px;font-size:10px;}"
            "QPushButton:checked{background:#2a1a4a;color:#c0a0ff;"
            "border-color:#5a2a8a;}"
            "QPushButton:hover{background:#22224a;}")
        self._top_btn.setToolTip("이 창을 항상 최상위에 표시")
        self._top_btn.toggled.connect(self._on_always_on_top)

        ctrl.addWidget(src_info)
        ctrl.addStretch()
        ctrl.addWidget(self._top_btn)
        ctrl.addWidget(self._preview_on_btn)
        v.addLayout(ctrl)

        self.prev = PreviewLabel(source, engine)
        v.addWidget(self.prev, 1)
        info = QLabel("좌클릭+드래그: ROI 추가  |  우클릭: 마지막 ROI 제거")
        info.setStyleSheet("color:#445;font-size:9px;")
        info.setAlignment(Qt.AlignCenter); v.addWidget(info)
        self.resize(640, 420)

    def _on_preview_toggle(self, on: bool):
        """창 내부 ON/OFF — PreviewLabel 렌더만 중지 (스레드는 유지)."""
        self.prev.set_active(on)
        self._preview_on_btn.setText("▶ 출력 ON" if on else "⏸ 출력 OFF")

    def _on_always_on_top(self, on: bool):
        """항상 최상위 토글 — 윈도우 플래그 재설정."""
        self._top_btn.setText("📌 최상위 ON" if on else "📌 최상위")
        flags = (Qt.Window | Qt.WindowTitleHint | Qt.WindowCloseButtonHint |
                 Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint)
        if on:
            flags |= Qt.WindowStaysOnTopHint
        pos  = self.pos()
        size = self.size()
        self.setWindowFlags(flags)
        self.move(pos); self.resize(size)
        if self._visible:
            self.show()

    def set_always_on_top(self, on: bool):
        """외부(커널/AI)에서 최상위 설정."""
        self._top_btn.setChecked(on)

    def show_win(self):
        self._visible = True; self.show(); self.activateWindow()

    def hide_win(self):
        self._visible = False; self.hide()

    def toggle(self):
        if self._visible: self.hide_win()
        else: self.show_win()

    def closeEvent(self, e):
        self._visible = False; e.accept()

    def update_frame(self, frame):
        if self._visible and frame is not None:
            self.prev.update_frame(frame)


class CameraWindow(_FloatingPreview):
    def __init__(self, engine: CoreEngine, parent=None):
        super().__init__("📷 Camera Preview", "camera", engine, parent)

class DisplayWindow(_FloatingPreview):
    def __init__(self, engine: CoreEngine, parent=None):
        super().__init__("🖥 Display Preview", "screen", engine, parent)


# =============================================================================
#  MainWindow
# =============================================================================
# =============================================================================
#  _FeatDragList — 기능 순서 드래그앤드랍 리스트  (수정1)
# =============================================================================
class _FeatDragList(QWidget):
    """
    기능 패널 순서를 드래그앤드랍으로 변경하는 컴팩트 리스트.
    각 행: [⠿ 핸들] [☑ ON/OFF] [색상 레이블]
    - 드래그: 순서 변경
    - 더블클릭: 해당 섹션으로 스크롤
    - 체크박스: 섹션 표시/숨기기
    """
    order_changed      = pyqtSignal(list)   # 새 순서 [key,...]
    visibility_changed = pyqtSignal(str, bool)  # (key, visible)
    scroll_to          = pyqtSignal(str)    # 더블클릭 key

    _ROW_H = 26
    _BG    = "#080818"
    _BG_HV = "#101028"
    _BG_DG = "#1a2a4a"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background:{self._BG};")
        self._order:   List[str] = []
        self._labels:  dict = {}
        self._colors:  dict = {}
        self._visible: dict = {}
        self._rows:    list = []   # QFrame 위젯
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(2,2,2,2); self._lay.setSpacing(1)
        self._drag_idx   = -1
        self._drag_start = QPoint()
        self._dragging   = False

    def populate(self, order: list, labels: dict,
                 colors: dict, visible: dict):
        self._order   = list(order)
        self._labels  = labels
        self._colors  = colors
        self._visible = dict(visible)
        self._rebuild()

    def _rebuild(self):
        while self._lay.count():
            it = self._lay.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        self._rows.clear()
        for key in self._order:
            row = self._make_row(key)
            self._rows.append(row)
            self._lay.addWidget(row)
        self._lay.addStretch()

    def _make_row(self, key: str) -> QFrame:
        row = QFrame(); row.setFixedHeight(self._ROW_H)
        row._key = key
        row.setStyleSheet(
            f"QFrame{{background:{self._BG};border-radius:3px;}}"
            f"QFrame:hover{{background:{self._BG_HV};}}")
        row.setCursor(Qt.OpenHandCursor)
        hl = QHBoxLayout(row); hl.setContentsMargins(4,1,4,1); hl.setSpacing(4)

        grip = QLabel("⠿"); grip.setFixedWidth(14)
        grip.setStyleSheet("color:#3a4a6a;font-size:16px;background:transparent;")

        chk = QCheckBox(); chk.setChecked(self._visible.get(key, True))
        chk.setStyleSheet(
            "QCheckBox::indicator{width:13px;height:13px;"
            "border:1px solid #3a4a6a;border-radius:2px;background:#0d0d1e;}"
            "QCheckBox::indicator:checked{background:#2980b9;border-color:#3a9ad9;}")
        chk.toggled.connect(lambda v, k=key: self._on_chk(k, v))

        color = self._colors.get(key, "#7bc8e0")
        lbl = QLabel(self._labels.get(key, key))
        lbl.setStyleSheet(
            f"color:{color};font-size:10px;font-weight:bold;background:transparent;")

        hl.addWidget(grip); hl.addWidget(chk); hl.addWidget(lbl, 1)

        # 이벤트 바인딩
        row.mousePressEvent   = lambda e, r=row: self._press(e, r)
        row.mouseMoveEvent    = lambda e, r=row: self._move(e, r)
        row.mouseReleaseEvent = lambda e, r=row: self._release(e, r)
        row.mouseDoubleClickEvent = lambda e, k=key: self.scroll_to.emit(k)
        return row

    def _on_chk(self, key: str, visible: bool):
        self._visible[key] = visible
        self.visibility_changed.emit(key, visible)

    def _press(self, e, row):
        if e.button() == Qt.LeftButton:
            idx = self._rows.index(row) if row in self._rows else -1
            self._drag_idx   = idx
            self._drag_start = e.globalPos()
            self._dragging   = False

    def _move(self, e, row):
        if self._drag_idx >= 0 and not self._dragging:
            if (e.globalPos()-self._drag_start).manhattanLength() > 6:
                self._dragging = True
                row.setStyleSheet(
                    f"QFrame{{background:{self._BG_DG};"
                    "border:1px solid #5a7aaa;border-radius:3px;}}")
                row.setCursor(Qt.ClosedHandCursor)

    def _release(self, e, row):
        if self._dragging and self._drag_idx >= 0:
            # 마우스 위치에서 대상 행 찾기
            gpos = e.globalPos()
            tgt  = -1
            for i, r in enumerate(self._rows):
                local = r.mapFromGlobal(gpos)
                if r.rect().contains(local):
                    tgt = i; break
            if tgt >= 0 and tgt != self._drag_idx:
                self._order.insert(tgt, self._order.pop(self._drag_idx))
                self._rebuild()
                self.order_changed.emit(list(self._order))
            else:
                self._rebuild()   # 스타일 복원
        self._drag_idx = -1; self._dragging = False

    def set_order(self, order: list):
        valid = [k for k in order if k in self._labels]
        for k in self._order:
            if k not in valid: valid.append(k)
        self._order = valid
        self._rebuild()

    def set_visible(self, key: str, v: bool):
        self._visible[key] = v
        self._rebuild()


_DARK_QSS = """
QMainWindow,QWidget{background:#0d0d1e;color:#ccd;}
QGroupBox{border:1px solid #2a3a5a;border-radius:6px;margin-top:8px;
    font-size:11px;font-weight:bold;color:#7ab4d4;padding:4px;}
QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px;}
QLabel{color:#ccd;font-size:11px;}
QCheckBox{color:#ccd;font-size:11px;}
QCheckBox::indicator{width:14px;height:14px;border:1px solid #446;
    border-radius:3px;background:#0d0d1e;}
QCheckBox::indicator:checked{background:#2980b9;border-color:#3498db;}
QSpinBox,QDoubleSpinBox{background:#1a1a3a;color:#ddd;border:1px solid #3a4a6a;
    padding:2px 4px;border-radius:3px;}
QComboBox{background:#1a1a3a;color:#ddd;border:1px solid #3a4a6a;
    padding:2px 6px;border-radius:3px;}
QComboBox::drop-down{border:none;}
QComboBox QAbstractItemView{background:#1a1a3a;color:#ddd;selection-background-color:#2a3a5a;}
QScrollBar:vertical{background:#0a0a18;width:8px;}
QScrollBar::handle:vertical{background:#2a3a5a;border-radius:4px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0px;}
QScrollBar:horizontal{background:#0a0a18;height:8px;}
QScrollBar::handle:horizontal{background:#2a3a5a;border-radius:4px;}
QPushButton{background:#1a2a3a;color:#ccd;border:1px solid #2a3a5a;
    border-radius:4px;padding:4px 10px;font-size:11px;}
QPushButton:hover{background:#22334a;color:#eef;}
QPushButton:pressed{background:#1a2030;}
QTabWidget::pane{border:1px solid #2a3a5a;}
QTabBar::tab{background:#141428;color:#667;border:1px solid #2a2a4a;
    border-bottom:none;padding:5px 12px;border-radius:4px 4px 0 0;font-size:11px;}
QTabBar::tab:selected{background:#1a1a38;color:#dde;border-color:#3a3a6a;}
QTabBar::tab:hover{background:#1a1a30;color:#aab;}
QPlainTextEdit,QTextEdit{background:#08080f;color:#9ab;border:1px solid #1a2a3a;}
"""

_SECTION_DEFS = [
    # (section_key, 탭 텍스트, 색상)
    ("rec",      "⏺ 녹화",        "#27ae60"),
    ("manual",   "🎬 수동녹화",    "#e67e22"),
    ("blackout", "⚡ 블랙아웃",    "#e74c3c"),
    ("roi",      "📐 ROI 관리",    "#9b59b6"),
    ("ac",       "🖱 오토클릭",    "#2980b9"),
    ("power",    "⚡ 전원 제어",   "#e74c3c"),
    ("macro",    "⚙ 매크로",       "#16a085"),
    ("schedule", "📅 예약",         "#8e44ad"),
    ("memo",     "📝 메모",         "#d35400"),
    ("kernel",   "🧠 커널",         "#e74c3c"),   # ★ v2.9 추가
    ("path",     "📁 저장 경로",    "#7bc8e0"),
    ("log",      "📋 로그",         "#556"),
    ("reset",    "♻ 초기화",        "#e74c3c"),
]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Screen & Camera Recorder  v4.4")
        self.resize(1400, 900)
        self.setStyleSheet(_DARK_QSS)

        # ── 코어 ──────────────────────────────────────────────────────────
        self._signals = Signals()
        self._engine  = CoreEngine(self._signals)
        self._db      = SettingsDB()
        self._kernel  = KernelEngine(self._engine, self._signals)  # ★ v2.9
        # ★ 전원 제어기 — KernelEngine과 PowerControlPanel이 공유
        self._power_ctrl = PowerController()
        self._kernel.power = self._power_ctrl

        # ── 미리보기 창 (ROI 관리 연동은 ROI 패널 생성 후 연결) ──────────
        self._cam_win  = CameraWindow(self._engine)
        self._disp_win = DisplayWindow(self._engine)

        # ── UI 빌드 ───────────────────────────────────────────────────────
        self._build_ui()
        self._connect_signals()
        self._start_timers()
        self._load_settings()

        # ★ 전역 1초 디바운스 자동저장 타이머
        self._global_autosave_timer = QTimer(self)
        self._global_autosave_timer.setSingleShot(True)
        self._global_autosave_timer.setInterval(1000)
        self._global_autosave_timer.timeout.connect(self._save_settings)

        # ★ 3초마다 설정 변경 감지 + 자동저장 (패널 훅 없이도 동작)
        self._settings_snapshot = {}   # 마지막 저장 상태 스냅샷
        self._settings_check_timer = QTimer(self)
        self._settings_check_timer.setInterval(3000)   # 3초
        self._settings_check_timer.timeout.connect(self._check_and_save_settings)
        self._settings_check_timer.start()

        # ── 엔진 시작 ─────────────────────────────────────────────────────
        self._engine.start()

    # ── UI 빌드 ──────────────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget(); self.setCentralWidget(central)
        root_h  = QHBoxLayout(central)
        root_h.setContentsMargins(6,6,6,6); root_h.setSpacing(6)

        # ════════════════════════════════════════════════════════════════
        #  좌측: 기능 패널 (세로 스크롤 + 섹션 토글 + 순서 이동)
        # ════════════════════════════════════════════════════════════════
        left_w = QWidget()
        left_v = QVBoxLayout(left_w)
        left_v.setContentsMargins(0,0,0,0); left_v.setSpacing(4)

        # ── 기능 제어 툴바 ───────────────────────────────────────────────
        tool_bar = QFrame()
        tool_bar.setStyleSheet(
            "QFrame{background:#0a0a1e;border-bottom:1px solid #1a2a3a;}")
        tool_bar.setFixedHeight(38)
        tb_lay = QHBoxLayout(tool_bar)
        tb_lay.setContentsMargins(6,4,6,4); tb_lay.setSpacing(4)

        tb_lbl = QLabel("⚙ 기능 패널")
        tb_lbl.setStyleSheet(
            "color:#7ab4d4;font-size:11px;font-weight:bold;background:transparent;")
        tb_lay.addWidget(tb_lbl)

        tb_hint = QLabel("⠿ 드래그로 순서변경  |  ☑ 체크로 ON/OFF")
        tb_hint.setStyleSheet("color:#334;font-size:9px;background:transparent;")
        tb_lay.addWidget(tb_hint)

        # 전체 펼치기/접기
        expand_btn = QPushButton("⊞")
        expand_btn.setFixedSize(26, 26)
        expand_btn.setToolTip("전체 펼치기")
        expand_btn.setStyleSheet(
            "QPushButton{background:#1a1a3a;color:#9ab;"
            "border:1px solid #2a2a4a;border-radius:3px;font-size:13px;}"
            "QPushButton:hover{background:#22224a;}")
        expand_btn.clicked.connect(lambda: self._set_all_collapsed(False))

        collapse_btn = QPushButton("⊟")
        collapse_btn.setFixedSize(26, 26)
        collapse_btn.setToolTip("전체 접기")
        collapse_btn.setStyleSheet(expand_btn.styleSheet())
        collapse_btn.clicked.connect(lambda: self._set_all_collapsed(True))

        api_btn2 = QPushButton("📖 API")
        api_btn2.setFixedHeight(26)
        api_btn2.setStyleSheet(
            "QPushButton{background:#1a1a2a;color:#f0c040;"
            "border:1px solid #4a4a20;border-radius:3px;font-size:10px;padding:0 6px;}"
            "QPushButton:hover{background:#22221a;}")
        api_btn2.clicked.connect(self._show_api_doc)

        tb_lay.addStretch()
        tb_lay.addWidget(expand_btn)
        tb_lay.addWidget(collapse_btn)
        tb_lay.addWidget(api_btn2)
        left_v.addWidget(tool_bar)

        # ── 기능 순서 리스트 (드래그앤드랍 + 체크박스 ON/OFF) ────────────
        self._feat_list = _FeatDragList(self)
        self._feat_list.order_changed.connect(self._on_feat_order_changed)
        self._feat_list.visibility_changed.connect(self._on_feat_visibility_changed)
        self._feat_list.scroll_to.connect(self._select_and_scroll)

        feat_scroll = QScrollArea()
        feat_scroll.setWidgetResizable(True)
        feat_scroll.setFixedHeight(140)
        feat_scroll.setStyleSheet(
            "QScrollArea{border:1px solid #1a2a3a;background:#080818;}"
            "QScrollBar:vertical{background:#080818;width:5px;}"
            "QScrollBar::handle:vertical{background:#2a3a5a;border-radius:2px;}")
        feat_scroll.setWidget(self._feat_list)
        left_v.addWidget(feat_scroll)

        self._section_order: List[str] = [d[0] for d in _SECTION_DEFS]
        self._section_visible: dict = {d[0]: True for d in _SECTION_DEFS}
        self._feat_rows: dict = {}
        self._selected_feat: str = ""

        # ── 기능 섹션 스크롤 영역 ────────────────────────────────────────
        self._sect_scroll = QScrollArea()
        self._sect_scroll.setWidgetResizable(True)
        self._sect_scroll.setStyleSheet(
            "QScrollArea{border:none;background:#0d0d1e;}"
            "QScrollBar:vertical{background:#080812;width:7px;}"
            "QScrollBar::handle:vertical{background:#2a3a5a;border-radius:3px;min-height:20px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        self._sect_w = QWidget()
        self._sect_w.setStyleSheet("background:#0d0d1e;")
        self._sect_lay = QVBoxLayout(self._sect_w)
        self._sect_lay.setContentsMargins(6,6,6,12); self._sect_lay.setSpacing(6)
        self._sect_scroll.setWidget(self._sect_w)
        left_v.addWidget(self._sect_scroll, 1)

        # ── 패널 + 섹션 생성 ─────────────────────────────────────────────
        self._panels    = {}
        self._sections  = {}
        self._roi_mgr   = None

        for key, label, color in _SECTION_DEFS:
            panel = self._make_panel(key)
            self._panels[key] = panel

            sec = CollapsibleSection(label, color)
            sec.add_widget(panel)
            self._sections[key] = sec
            self._sect_lay.addWidget(sec)

        self._sect_lay.addStretch()

        # 기능 리스트 초기 빌드
        self._rebuild_feat_list()

        # ════════════════════════════════════════════════════════════════
        #  우측: 미리보기 + 상태 (QWidget으로 감싸서 Splitter에 추가)
        # ════════════════════════════════════════════════════════════════
        right_w = QWidget()
        right_v = QVBoxLayout(right_w); right_v.setSpacing(4)
        right_v.setContentsMargins(0,0,0,0)

        # ── 버튼 행 ──────────────────────────────────────────────────────
        # ★ v2.9.3: 미리보기 관련 모든 ON/OFF 독립 제어
        #
        #  행1: [🖥 인라인 Display] [📷 인라인 Camera] │ [🔄 스캔] [📖 API]
        #  행2: [🖥 플로팅 창 Display] [📷 플로팅 창 Camera]
        #  행3: [🖥 영상처리 Display ▶ ON] [📷 영상처리 Camera ▶ ON]

        _btn_ss_disp = (
            "QPushButton{background:#1a2a3a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:4px;font-size:10px;font-weight:bold;}"
            "QPushButton:checked{background:#0d3a5a;border-color:#3a7aaa;}"
            "QPushButton:!checked{background:#0a0a18;color:#334;border-color:#1a1a2a;}"
            "QPushButton:hover{background:#22334a;}")
        _btn_ss_cam = (
            "QPushButton{background:#1a2a1a;color:#f0c040;"
            "border:1px solid #4a4a20;border-radius:4px;font-size:10px;font-weight:bold;}"
            "QPushButton:checked{background:#0d2a0d;border-color:#7a7a30;}"
            "QPushButton:!checked{background:#0a0a0a;color:#334;border-color:#1a1a1a;}"
            "QPushButton:hover{background:#22331a;}")
        _btn_ss_util = (
            "QPushButton{background:#1a1a2a;color:#9ab;"
            "border:1px solid #2a2a4a;border-radius:4px;font-size:10px;}"
            "QPushButton:hover{background:#22224a;}")

        btn_row1 = QHBoxLayout(); btn_row1.setSpacing(4)
        btn_row2 = QHBoxLayout(); btn_row2.setSpacing(4)
        btn_row3 = QHBoxLayout(); btn_row3.setSpacing(4)

        # ── 행1: 인라인 미리보기 ON/OFF ─────────────────────────────────
        self._scr_inline_btn = QPushButton("🖥 인라인 Display ▶ ON")
        self._scr_inline_btn.setCheckable(True)
        self._scr_inline_btn.setChecked(True)
        self._scr_inline_btn.setFixedHeight(26)
        self._scr_inline_btn.setStyleSheet(_btn_ss_disp)
        self._scr_inline_btn.toggled.connect(self._on_scr_inline_toggle)

        self._cam_inline_btn = QPushButton("📷 인라인 Camera ▶ ON")
        self._cam_inline_btn.setCheckable(True)
        self._cam_inline_btn.setChecked(True)
        self._cam_inline_btn.setFixedHeight(26)
        self._cam_inline_btn.setStyleSheet(_btn_ss_cam)
        self._cam_inline_btn.toggled.connect(self._on_cam_inline_toggle)

        scan_btn = QPushButton("🔄 스캔")
        scan_btn.setFixedHeight(26)
        scan_btn.setStyleSheet(_btn_ss_util)
        scan_btn.clicked.connect(self._scan_devices)

        api_btn = QPushButton("📖 API")
        api_btn.setFixedHeight(26)
        api_btn.setStyleSheet(
            "QPushButton{background:#1a1a2a;color:#f0c040;"
            "border:1px solid #4a4a20;border-radius:4px;font-size:10px;}"
            "QPushButton:hover{background:#22221a;}")
        api_btn.clicked.connect(self._show_api_doc)

        llm_btn = QPushButton("AI")
        llm_btn.setFixedHeight(26)
        llm_btn.setStyleSheet(
            "QPushButton{background:#1a0a2e;color:#c080ff;"
            "border:1px solid #5a2a8a;border-radius:4px;"
            "font-size:10px;font-weight:bold;}"
            "QPushButton:hover{background:#2a1a4a;}")
        llm_btn.setToolTip("AI Chat — Groq(무료)/Gemini/Claude")
        llm_btn.clicked.connect(self._show_llm_chat)
        # 📡 CAN Monitor 버튼
        can_mon_btn = QPushButton("📡 CAN")
        can_mon_btn.setFixedHeight(26)
        can_mon_btn.setStyleSheet(
            "QPushButton{background:#0a1a2a;color:#7bc8e0;"
            "border:1px solid #2a4a6a;border-radius:4px;"
            "font-size:10px;font-weight:bold;}"
            "QPushButton:hover{background:#12223a;}")
        can_mon_btn.setToolTip(
            "CAN Monitor — DBC 신호 확인 / CANoe COM 연결\n"
            "커널 스크립트: can.can_read() / canoe_write()")
        can_mon_btn.clicked.connect(self._show_can_monitor)
        btn_row1.addWidget(self._scr_inline_btn, 1)
        btn_row1.addWidget(self._cam_inline_btn, 1)
        btn_row1.addWidget(scan_btn)
        btn_row1.addWidget(api_btn)
        btn_row1.addWidget(llm_btn)
        btn_row1.addWidget(can_mon_btn)

        # ★ CAN 그래프 ON/OFF 버튼
        self._can_graph_btn = QPushButton("📊 CAN그래프 ON")
        self._can_graph_btn.setCheckable(True)
        self._can_graph_btn.setChecked(True)
        self._can_graph_btn.setFixedHeight(26)
        self._can_graph_btn.setStyleSheet(
            "QPushButton{background:#0a1a0a;color:#7fdb9e;"
            "border:1px solid #1a5a2a;border-radius:3px;font-size:10px;}"
            "QPushButton:checked{background:#0f2a0f;color:#affcbf;}"
            "QPushButton:!checked{background:#1a0a0a;color:#f88;"
            "border-color:#5a1a1a;}")
        self._can_graph_btn.toggled.connect(self._on_can_graph_toggle)
        btn_row1.addWidget(self._can_graph_btn)

        # ── 행2: 플로팅 창 ON/OFF ────────────────────────────────────────
        self._disp_btn = QPushButton("🖥 플로팅 Display 창")
        self._disp_btn.setCheckable(True)
        self._disp_btn.setFixedHeight(26)
        self._disp_btn.setStyleSheet(_btn_ss_disp)
        self._disp_btn.toggled.connect(
            lambda c: self._disp_win.show_win() if c else self._disp_win.hide_win())

        self._cam_btn = QPushButton("📷 플로팅 Camera 창")
        self._cam_btn.setCheckable(True)
        self._cam_btn.setFixedHeight(26)
        self._cam_btn.setStyleSheet(_btn_ss_cam)
        self._cam_btn.toggled.connect(
            lambda c: self._cam_win.show_win() if c else self._cam_win.hide_win())

        btn_row2.addWidget(self._disp_btn, 1)
        btn_row2.addWidget(self._cam_btn, 1)

        # ── 행3: 영상처리 스레드 ON/OFF (자원 절약) ──────────────────────
        self._scr_preview_btn = QPushButton("🖥 Display 영상처리 ▶ ON")
        self._scr_preview_btn.setCheckable(True)
        self._scr_preview_btn.setChecked(True)
        self._scr_preview_btn.setFixedHeight(26)
        self._scr_preview_btn.setStyleSheet(_btn_ss_disp)
        self._scr_preview_btn.toggled.connect(self._on_scr_preview_toggle)

        self._cam_preview_btn = QPushButton("📷 Camera 영상처리 ▶ ON")
        self._cam_preview_btn.setCheckable(True)
        self._cam_preview_btn.setChecked(True)
        self._cam_preview_btn.setFixedHeight(26)
        self._cam_preview_btn.setStyleSheet(_btn_ss_cam)
        self._cam_preview_btn.toggled.connect(self._on_cam_preview_toggle)

        btn_row3.addWidget(self._scr_preview_btn, 1)
        btn_row3.addWidget(self._cam_preview_btn, 1)

        right_v.addLayout(btn_row1)
        right_v.addLayout(btn_row2)
        right_v.addLayout(btn_row3)

        # ── 인라인 미리보기 — 각각 독립 ON/OFF 가능 ★ v2.9.3 ────────────
        self._scr_inline = PreviewLabel("screen", self._engine)
        self._scr_inline.setMinimumHeight(140)

        self._cam_inline = PreviewLabel("camera", self._engine)
        self._cam_inline.setMinimumHeight(140)

        # 각각 QWidget으로 감싸서 Splitter에 추가 (hide/show 독립 제어)
        scr_wrap = QWidget()
        scr_wrap_v = QVBoxLayout(scr_wrap)
        scr_wrap_v.setContentsMargins(0,0,0,0); scr_wrap_v.setSpacing(0)
        scr_wrap_v.addWidget(self._scr_inline)
        self._scr_inline_wrap = scr_wrap

        cam_wrap = QWidget()
        cam_wrap_v = QVBoxLayout(cam_wrap)
        cam_wrap_v.setContentsMargins(0,0,0,0); cam_wrap_v.setSpacing(0)
        cam_wrap_v.addWidget(self._cam_inline)
        self._cam_inline_wrap = cam_wrap

        inline_splitter = QSplitter(Qt.Vertical)
        inline_splitter.addWidget(scr_wrap)
        inline_splitter.addWidget(cam_wrap)
        inline_splitter.setSizes([280, 280])
        inline_splitter.setChildrenCollapsible(True)
        right_v.addWidget(inline_splitter, 1)

        # ── 상태바 ───────────────────────────────────────────────────────
        stat_grp = QGroupBox("상태"); sl = QGridLayout(stat_grp); sl.setSpacing(4)
        self._status_lbl = QLabel("대기 중")
        self._status_lbl.setStyleSheet(
            "color:#7bc8e0;font-family:monospace;font-size:10px;")
        self._status_lbl.setWordWrap(True)
        sl.addWidget(self._status_lbl, 0, 0, 1, 4)

        sl.addWidget(QLabel("모니터:"), 1, 0)
        self._mon_cb = QComboBox(); self._mon_cb.setMinimumWidth(150)
        self._mon_cb.currentIndexChanged.connect(self._on_mon_changed)
        sl.addWidget(self._mon_cb, 1, 1)
        sl.addWidget(QLabel("카메라:"), 1, 2)
        self._cam_cb = QComboBox(); self._cam_cb.setMinimumWidth(150)
        self._cam_cb.currentIndexChanged.connect(self._on_cam_changed)
        sl.addWidget(self._cam_cb, 1, 3)

        self._fps_lbl = QLabel("Screen: — fps  |  Camera: — fps")
        self._fps_lbl.setStyleSheet("color:#556;font-size:9px;font-family:monospace;")
        sl.addWidget(self._fps_lbl, 2, 0, 1, 4)

        self._io_lbl = QLabel("IO채널: —")
        self._io_lbl.setStyleSheet("color:#334;font-size:9px;font-family:monospace;")
        sl.addWidget(self._io_lbl, 3, 0, 1, 4)

        right_v.addWidget(stat_grp)

        # ── 좌/우 QSplitter로 연결 (마우스로 비율 조절) ── 수정2
        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.setStyleSheet(
            "QSplitter::handle{background:#1a2a3a;width:5px;}"
            "QSplitter::handle:hover{background:#2a5a9a;}")
        main_splitter.addWidget(left_w)
        main_splitter.addWidget(right_w)
        main_splitter.setSizes([500, 900])   # 초기 비율 (픽셀)
        main_splitter.setChildrenCollapsible(False)
        root_h.addWidget(main_splitter)

    def _rebuild_feat_list(self):
        """_FeatDragList 위젯에 현재 순서/가시성 반영."""
        _labels  = {d[0]: d[1] for d in _SECTION_DEFS}
        _colors  = {d[0]: d[2] for d in _SECTION_DEFS}
        if hasattr(self, '_feat_list') and isinstance(self._feat_list, _FeatDragList):
            self._feat_list.populate(
                self._section_order, _labels, _colors, self._section_visible)

    def _on_feat_order_changed(self, new_order: list):
        """_FeatDragList 드래그앤드랍 → 섹션 위젯 순서 재배치."""
        self._section_order = new_order
        while self._sect_lay.count():
            it = self._sect_lay.takeAt(0)
            if it.widget(): it.widget().setParent(None)
        for k in new_order:
            sec = self._sections.get(k)
            if sec:
                sec.setParent(self._sect_w)
                self._sect_lay.addWidget(sec)
        self._sect_lay.addStretch()

    def _on_feat_visibility_changed(self, key: str, visible: bool):
        """체크박스 ON/OFF → 섹션 표시/숨기기."""
        self._section_visible[key] = visible
        sec = self._sections.get(key)
        if sec: sec.setVisible(visible)

    def _select_and_scroll(self, key: str):
        """기능명 더블클릭 → 해당 섹션으로 스크롤."""
        self._selected_feat = key
        sec = self._sections.get(key)
        if sec:
            if sec.is_collapsed(): sec.set_collapsed(False)
            QTimer.singleShot(50, lambda: self._scroll_to(sec))

    def _scroll_to(self, widget: QWidget):
        pos = widget.mapTo(self._sect_w, QPoint(0,0))
        vsb = self._sect_scroll.verticalScrollBar()
        vsb.setValue(max(0, pos.y() - 10))

    def _set_all_collapsed(self, collapsed: bool):
        for sec in self._sections.values():
            sec.set_collapsed(collapsed)

    # ── 미리보기 ON/OFF (자원 절약) ★ 수정1 복구 ────────────────────────
    # ── 미리보기 ON/OFF 핸들러 6개 독립 제어 ★ v2.9.3 ──────────────────
    def _on_scr_inline_toggle(self, on: bool):
        """인라인 Display 미리보기 표시/숨기기."""
        if hasattr(self, '_scr_inline_wrap'):
            self._scr_inline_wrap.setVisible(on)
        self._scr_inline.set_active(on)
        self._scr_inline_btn.setText(
            "🖥 인라인 Display ▶ ON" if on else "🖥 인라인 Display ⏸ OFF")

    def _on_cam_inline_toggle(self, on: bool):
        """인라인 Camera 미리보기 표시/숨기기."""
        if hasattr(self, '_cam_inline_wrap'):
            self._cam_inline_wrap.setVisible(on)
        self._cam_inline.set_active(on)
        self._cam_inline_btn.setText(
            "📷 인라인 Camera ▶ ON" if on else "📷 인라인 Camera ⏸ OFF")

    def _on_scr_preview_toggle(self, on: bool):
        """Display 영상처리 스레드 ON/OFF (자원 절약)."""
        self._scr_inline.set_active(on)
        self._scr_preview_btn.setText(
            "🖥 Display 영상처리 ▶ ON" if on else "🖥 Display 영상처리 ⏸ OFF")
        if on:
            self._engine.start_screen()
        else:
            self._engine.stop_screen()
        if hasattr(self._disp_win, 'prev'):
            self._disp_win.prev.set_active(on)

    def _on_cam_preview_toggle(self, on: bool):
        """Camera 영상처리 스레드 ON/OFF (자원 절약)."""
        self._cam_inline.set_active(on)
        self._cam_preview_btn.setText(
            "📷 Camera 영상처리 ▶ ON" if on else "📷 Camera 영상처리 ⏸ OFF")
        if on:
            self._engine.start_camera()
        else:
            self._engine.stop_camera()
        if hasattr(self._cam_win, 'prev'):
            self._cam_win.prev.set_active(on)

    def _make_panel(self, key: str) -> QWidget:
        e = self._engine; s = self._signals; db = self._db

        if key == "rec":
            p = RecordingPanel(e, s); return p
        elif key == "manual":
            return ManualClipPanel(e, s)
        elif key == "blackout":
            return BlackoutPanel(e, s)
        elif key == "roi":
            # ★ 추가1: ROI 관리 탭 — RoiManagerPanel(engine, signals) 올바른 호출
            w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(8)

            # RoiManagerPanel은 내부 탭(Display/Camera)으로 둘 다 관리
            self._roi_mgr = RoiManagerPanel(e, s)
            self._roi_mgr.changed.connect(lambda: db.save_roi_items(
                e.screen_rois + e.camera_rois))
            self._roi_mgr.start_ocr_refresh()   # ★ OCR 레이블 자동 갱신 시작
            v.addWidget(self._roi_mgr)

            hint = QLabel(
                "💡 ROI 추가: Display/Camera 미리보기 창에서 좌클릭 드래그\n"
                "   드래그 완료 → 팝업 다이얼로그에서 이름/설명 입력\n"
                "   우클릭 → 마지막 ROI 제거  |  ✏ → 이름/설명 편집\n"
                "   드래그앤드랍 → 감지 순서 변경")
            hint.setWordWrap(True)
            hint.setStyleSheet(
                "color:#567;font-size:10px;background:#090912;"
                "border:1px solid #1a1a2a;border-radius:4px;padding:8px;")
            v.addWidget(hint)
            v.addStretch()
            return w

        elif key == "ac":
            return AutoClickPanel(e, s)
        elif key == "macro":
            self._macro_panel = MacroPanel(e, s); return self._macro_panel
        elif key == "schedule":
            self._sched_panel = SchedulePanel(e, s); return self._sched_panel
        elif key == "memo":
            self._memo_panel = MemoPanel(e, s); return self._memo_panel
        elif key == "path":
            p = PathSettingsPanel(e, db)
            self._path_panel = p; return p
        elif key == "log":
            return LogPanel(s)
        elif key == "power":
            return PowerControlPanel(self._power_ctrl)
        elif key == "reset":
            return ResetPanel(e, db, s)
        elif key == "kernel":
            # ★ v2.9: 커널 패널
            self._kernel_panel = KernelPanel(self._kernel, s)
            return self._kernel_panel
        return QLabel(f"(미구현: {key})")

    # ── 시그널 연결 ──────────────────────────────────────────────────────
    def _connect_signals(self):
        self._signals.status_message.connect(self._on_status)
        self._signals.monitors_scanned.connect(self._on_monitors)
        self._signals.cameras_scanned.connect(self._on_cameras)
        self._signals.roi_list_changed.connect(self._on_roi_changed)
        # ROI 변경 시 ROI 매니저 UI 동기화
        self._signals.roi_list_changed.connect(self._refresh_roi_panels)
        # 인라인 미리보기 PreviewLabel에서 ROI 드래그/우클릭 시 ROI 매니저 갱신
        self._scr_inline.roi_changed.connect(self._signals.roi_list_changed.emit)
        self._cam_inline.roi_changed.connect(self._signals.roi_list_changed.emit)

    def _on_status(self, msg: str):
        self._status_lbl.setText(msg)

    def _on_monitors(self, monitors: list):
        self._mon_cb.blockSignals(True); self._mon_cb.clear()
        for m in monitors:
            self._mon_cb.addItem(m["name"], m["idx"])
        idx = next((i for i, m in enumerate(monitors)
                    if m["idx"] == self._engine.active_monitor_idx), 0)
        self._mon_cb.setCurrentIndex(idx)
        self._mon_cb.blockSignals(False)

    def _on_cameras(self, cameras: list):
        self._cam_cb.blockSignals(True); self._cam_cb.clear()
        for c in cameras:
            self._cam_cb.addItem(c["name"], c["idx"])
        idx = next((i for i, c in enumerate(cameras)
                    if c["idx"] == self._engine.active_cam_idx), 0)
        self._cam_cb.setCurrentIndex(idx)
        self._cam_cb.blockSignals(False)

    def _on_mon_changed(self, i):
        if i < 0: return
        self._engine.active_monitor_idx = self._mon_cb.itemData(i)
        self._engine.restart_screen()

    def _on_cam_changed(self, i):
        if i < 0: return
        self._engine.active_cam_idx = self._cam_cb.itemData(i)
        cams = self._engine.camera_list
        cam  = next((c for c in cams if c["idx"]==self._engine.active_cam_idx), None)
        if cam: self._engine.actual_camera_fps = cam["fps"]
        self._engine.restart_camera()

    def _on_roi_changed(self):
        pass  # roi_list_changed 시그널 수신 → refresh는 _refresh_roi_panels에서

    def _refresh_roi_panels(self):
        mgr = getattr(self, '_roi_mgr', None)
        if mgr and hasattr(mgr, '_refresh'):
            mgr._refresh()

    def _scan_devices(self):
        threading.Thread(target=self._engine.scan_monitors, daemon=True).start()
        threading.Thread(target=self._engine.scan_cameras,  daemon=True).start()

    def _show_api_doc(self):
        """★ v2.9.3: API 레퍼런스 — NonModal 싱글턴 창."""
        ApiDocDialog.show_or_raise(self)

    def _show_llm_chat(self):
        """AI 챗봇 창 — Groq/Gemini/Claude, NonModal 싱글턴."""
        LLMChatDialog.show_or_raise(self, self._engine)
        dlg = LLMChatDialog._instance
        if dlg and hasattr(dlg, "_ctrl"):
            dlg._ctrl.set_power(getattr(self, "_power_ctrl", None))
            # KernelEngine 주입 (ROI 읽기 등 전체 API 접근)
            dlg._ctrl._kernel = getattr(self, "_kernel", None)

    def _on_can_graph_toggle(self, on: bool):
        """CAN Monitor 그래프 ON/OFF — 타이머 제어."""
        self._can_graph_btn.setText("📊 CAN그래프 ON" if on else "📊 CAN그래프 OFF")
        # CanMonitorDialog 인스턴스에 직접 접근
        try:
            inst = CanMonitorDialog._instance
            if inst and hasattr(inst, '_graph_timer'):
                inst._graph_timer.start() if on else inst._graph_timer.stop()
            if inst:
                inst.show() if on else inst.hide()
        except Exception:
            pass

    def _show_can_monitor(self):
        """CAN Monitor 창 — DBC 신호 탐색 / CANoe COM 연결, NonModal."""
        CanMonitorDialog.show_or_raise(
            self, self._engine,
            kernel=getattr(self, "_kernel", None))

    # ── 타이머 ───────────────────────────────────────────────────────────
    def _start_timers(self):
        # 미리보기 업데이트 (30fps)
        self._prev_timer = QTimer(self)
        self._prev_timer.timeout.connect(self._update_previews)
        self._prev_timer.start(33)

        # OCR 백그라운드 루프 — 별도 스레드 (UI 차단 없음)
        self._ocr_stop = threading.Event()
        self._ocr_thread = threading.Thread(
            target=self._ocr_loop, daemon=True, name="OCRLoop")
        self._ocr_thread.start()

        # 블랙아웃/예약/FPS/IO 갱신 (1초)
        self._slow_timer = QTimer(self)
        self._slow_timer.timeout.connect(self._slow_tick)
        self._slow_timer.start(1000)

        # 예약 체크 (1초)
        self._sched_timer = QTimer(self)
        self._sched_timer.timeout.connect(self._sched_tick)
        self._sched_timer.start(1000)

    def _update_previews(self):
        try:
            frame = self._engine.screen_queue.get_nowait()
            stamped = CoreEngine.stamp_preview(frame, self._engine, "screen")
            self._scr_inline.update_frame(stamped)
            self._disp_win.update_frame(stamped)
        except queue.Empty: pass
        try:
            frame = self._engine.camera_queue.get_nowait()
            stamped = CoreEngine.stamp_preview(frame, self._engine, "camera")
            self._cam_inline.update_frame(stamped)
            self._cam_win.update_frame(stamped)
        except queue.Empty: pass

    def _ocr_loop(self):
        """
        ★ v2.9.3 — OCR 백그라운드 루프.
        ROI가 있는 소스에 대해 주기적으로 OCR 실행 후 RoiItem.last_text 캐시.
        UI 스레드를 차단하지 않음 (별도 daemon 스레드).

        주기: 500ms (빠른 OCR 필요 시 줄일 수 있으나 CPU 주의)
        """
        import textwrap as _tw

        OCR_INTERVAL = 0.5   # 초 — 필요 시 줄이기 (최소 0.2 권장)
        ocr_self = self      # 클로저에서 self 참조용

        def _extract_region(source: str):
            """엔진 버퍼에서 최신 프레임 추출."""
            with self._engine._buf_lock:
                buf = (self._engine._scr_buf if source == "screen"
                       else self._engine._cam_buf)
                return buf[-1].copy() if buf else None

        def _run_ocr_on_roi(roi, frame, zoom_hint: float = 1.0):
            """
            단일 ROI OCR 실행 — HEX 전용 고정밀 모드.

            ★ v4.4 OCR 개선:
            · zoom_hint 의존 제거 → 물리 픽셀 기준 최소 400px 강제 확대
              (화면 줌은 표시 배율일 뿐 실픽셀 증가 없음)
            · CLAHE 대비 강화 + 언샤프 마스킹(샤프닝) 추가
            · 이진화 후보: Otsu 정/역 + 적응형(Gaussian) + 고정(127) + dilate
            · psm: 7(단행) / 8(단어) / 6(블록) / 13(원시행) 4종
            · 다자리 수 보존: image_to_data parts를 공백 없이 join
            · confidence 임계값 40으로 완화 (소형 숫자 대응)
            """
            rx, ry, rw, rh = roi.rect()
            region = frame[ry:ry+rh, rx:rx+rw]
            if region.size == 0:
                return

            # ── 물리 픽셀 기준 강제 업스케일 ─────────────────────────
            # zoom_hint는 표시 배율일 뿐 실픽셀과 무관.
            # 실픽셀 h 가 작으면 OCR이 글자를 인식 못함.
            # → h·w 모두 독립적으로 최소 400px 보장.
            MIN_DIM = 300   # ★ v4.5: 과확대 억제 (400→300)
            MAX_DIM = 900   # 메모리 방어선

            def _force_upscale(img):
                """
                물리 픽셀 기준 강제 확대 — 극소 ROI 대응 개선판.

                ★ v4.5 변경:
                  · 짧은 변 <30px (초극소)이면 먼저 NEAREST×4로 선확대
                    → 획의 형태를 보존한 채 확대 (bicubic 단독 사용 시
                       극소 획이 번져서 오인식되는 문제 방지)
                  · 목표 크기: MIN_DIM=300 (400→300 하향)
                    → 과확대로 인한 획 왜곡 억제
                  · 최대 크기: MAX_DIM=900
                """
                h, w = img.shape[:2]
                if h <= 0 or w <= 0:
                    return img
                # 종횡비 극단 보정 (가로/세로 > 8)
                if h > 0 and w / h > 8:
                    pad = int(w / 8) - h
                    val = img[0,0].tolist() if img.ndim == 3 else int(img[0,0])
                    img = cv2.copyMakeBorder(
                        img, pad//2, pad - pad//2, 0, 0,
                        cv2.BORDER_CONSTANT, value=val)
                    h, w = img.shape[:2]
                short = min(h, w)
                # ★ 극소(30px 미만) → NEAREST×4 선확대로 획 형태 보존
                if short < 30:
                    img = cv2.resize(img, (w*4, h*4),
                                     interpolation=cv2.INTER_NEAREST)
                    h, w = img.shape[:2]
                    short = min(h, w)
                # 목표 픽셀로 bicubic 확대
                if short < MIN_DIM:
                    s   = MIN_DIM / short
                    nh  = min(int(h * s), MAX_DIM)
                    nw  = min(int(w * s), MAX_DIM)
                    img = cv2.resize(img, (nw, nh),
                                     interpolation=cv2.INTER_CUBIC)
                return img

            def _sharpen(gray):
                """언샤프 마스킹 — 획 경계 강화."""
                blur = cv2.GaussianBlur(gray, (0, 0), 2.0)
                return cv2.addWeighted(gray, 1.8, blur, -0.8, 0)

            def _clahe(gray):
                """CLAHE 대비 강화 — 저조도·낮은 대비 숫자 대응."""
                h  = gray.shape[0]
                ts = max(2, min(8, h // 30))
                return cv2.createCLAHE(
                    clipLimit=4.0, tileGridSize=(ts, ts)).apply(gray)

            def _dilate(th, ksize=2):
                k = cv2.getStructuringElement(cv2.MORPH_RECT, (ksize, ksize))
                return cv2.dilate(th, k, iterations=1)

            def _make_candidates(bgr):
                """
                전처리 후보 생성 — 극소 ROI 최적화판.

                ★ v4.5 변경:
                  · medianBlur 추가 → 소금후추 노이즈 제거
                    (극소 ROI 확대 후 나타나는 점 노이즈 억제)
                  · 적응형 이진화 제거 → 극소 ROI에서 노이즈 증폭 문제
                  · 고정 임계값(120/160) 후보 추가
                    → 배경/전경 밝기가 일정한 Display에 유리
                  · dilate 후보 유지 (가는 획 보강)
                """
                cands = []
                gray  = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                gray  = _force_upscale(gray)

                # ★ 노이즈 제거: medianBlur (극소 ROI 확대 시 발생하는 점 노이즈)
                med = cv2.medianBlur(gray, 3)

                # ── 경로 A: CLAHE + 샤프닝 (주력) ──────────────────────
                enhanced = _sharpen(_clahe(med))

                # ① Otsu 정방향 (밝은 배경 + 어두운 글자)
                _, th_otsu = cv2.threshold(
                    enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                cands.append((th_otsu, "clahe_otsu"))

                # ② Otsu 반전 (어두운 배경 + 밝은 글자 — LCD)
                _, th_inv = cv2.threshold(
                    enhanced, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
                cands.append((th_inv, "clahe_otsu_inv"))

                # ③ dilate (가는 획 보강)
                cands.append((_dilate(th_otsu), "clahe_otsu_dilate"))

                # ── 경로 B: 원본 gray (보험용) ──────────────────────────
                blur_g = cv2.GaussianBlur(med, (3, 3), 0)

                # ④ Otsu 정방향 원본
                _, th_raw = cv2.threshold(
                    blur_g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                cands.append((th_raw, "raw_otsu"))

                # ⑤ Otsu 반전 원본
                _, th_raw_inv = cv2.threshold(
                    blur_g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
                cands.append((th_raw_inv, "raw_otsu_inv"))

                # ⑥⑦ 고정 임계값 (Display 배경이 균일한 경우 강점)
                _, th_fix1 = cv2.threshold(blur_g, 120, 255, cv2.THRESH_BINARY)
                _, th_fix2 = cv2.threshold(blur_g, 160, 255, cv2.THRESH_BINARY_INV)
                cands.append((th_fix1, "fixed_120"))
                cands.append((th_fix2, "fixed_160_inv"))

                return cands

            def _pad(img, px=25):
                """여백 추가 — Tesseract는 여백이 충분할 때 잘 인식함."""
                return cv2.copyMakeBorder(
                    img, px, px, px, px,
                    cv2.BORDER_CONSTANT, value=255)

            def _clean_number(text: str) -> str:
                """
                숫자 + HEX 문자만 남기고 정규화.
                · 0~9, A~F (대소문자) 허용
                · 흔한 오인식 교정 후 대문자 정규화
                """
                import re as _re
                # 오인식 교정 테이블 (OCR이 혼동하는 문자 → HEX 의미상 올바른 값)
                _fix = {
                    'o': '0', 'O': '0', 'Q': '0',        # 0 오인식
                    'l': '1', 'I': '1', 'i': '1', '|': '1',  # 1 오인식
                    'Z': '2', 'z': '2',                   # 2 오인식
                    'S': '5', 's': '5',                   # 5 오인식
                    'G': '6',                              # 6 오인식
                    'T': '7',                              # 7 오인식
                    'B': '8',                              # 8 오인식
                    'g': '9', 'q': '9',                   # 9 오인식
                    # HEX 문자 소문자 → 대문자
                    'a': 'A', 'b': 'B', 'c': 'C',
                    'd': 'D', 'e': 'E', 'f': 'F',
                }
                t = text.strip()
                result = ""
                for ch in t:
                    fixed = _fix.get(ch, ch)
                    if fixed in '0123456789ABCDEFabcdef':
                        result += fixed.upper()
                    # 그 외 문자 제거 (공백, 특수문자 등)
                return result

            def _score(text: str, conf: float = 0.0) -> float:
                """
                HEX 또는 순수 숫자 결과에 점수 부여.

                ★ v4.5 개선:
                  · 단일 순수 숫자(0~9): 보너스 +50 (오탐 억제 핵심)
                    예) "1" conf=30 → (12+50)*(1+0.3) = 80.6
                        "FF" conf=10 → (20)*(1+0.1) = 22  → "1" 채택
                  · confidence 가중치 유지
                  · 다자리(2~3자)는 여전히 digit_only 우대
                  · 4자 이상 hex: 오탐 가능성으로 점수 하향
                """
                t = text.strip()
                if not t:
                    return 0.0
                leng = len(t)
                digit_only = all(c.isdigit() for c in t)
                hex_only   = all(c in '0123456789ABCDEF' for c in t.upper())
                conf_mult  = 1.0 + conf / 100.0

                if digit_only:
                    base = leng * 12
                    # ★ 단일 숫자: 보너스 +50 → 'FF' 같은 오탐보다 확실히 높게
                    if leng == 1:
                        base += 50
                    return base * conf_mult
                if hex_only:
                    base = leng * 10
                    # 4자 이상 hex는 오탐 가능성 → 점수 하향
                    if leng >= 4:
                        base = leng * 6
                    return base * conf_mult
                return float(leng) * conf_mult

            # ── OCR 1: pytesseract — 2단계 고속 전략 ────────────────
            #
            # ★ v4.5 고속화:
            #   기존: 이진화 7종 × psm 4종 = 최대 28 Tesseract 호출
            #         → ROI 3개면 ~3초 소요
            #
            #   신규: 1차 빠른 패스(max 4회) + 2차 보완 패스(max 10회)
            #         → 1차에서 conf ≥ 60이면 즉시 종료 (~0.1초)
            #         → 전체 최악 케이스에도 ~0.3초 이내 완료
            #
            best_text       = ""
            best_score      = -1.0
            best_confidence = -1.0
            tess_ran        = False

            try:
                import pytesseract as _tess
                from PIL import Image as _PIL

                exe = _tess.pytesseract.tesseract_cmd
                if not exe or not os.path.isfile(exe):
                    status = _init_tesseract()
                    if status.startswith("not_found"):
                        roi.last_text = "[ERR:Tesseract없음→UB-Mannheim설치]"
                        return

                WL = "0123456789ABCDEFabcdef"
                cands = _make_candidates(region)

                def _tess_once(pil_img, psm_n):
                    """단일 psm 호출 → (joined, avg_conf, score)."""
                    cfg = (f"--psm {psm_n} --oem 3"
                           f" -c tessedit_char_whitelist={WL}")
                    try:
                        data = _tess.image_to_data(
                            pil_img, config=cfg,
                            output_type=_tess.Output.DICT)
                        parts, csum, ccnt = [], 0.0, 0
                        for t, c in zip(data.get('text', []),
                                        data.get('conf', [])):
                            ts   = t.strip()
                            cv   = float(c) if str(c) != '-1' else 0.0
                            if ts and cv >= 0:
                                cl = _clean_number(ts)
                                if cl:
                                    parts.append(cl)
                                    if cv > 0:
                                        csum += cv; ccnt += 1
                        if not parts:
                            return "", 0.0, 0.0
                        joined = "".join(parts[:4])
                        aconf  = csum / ccnt if ccnt else 0.0
                        return joined, aconf, _score(joined, aconf)
                    except Exception:
                        return "", 0.0, 0.0

                def _upd(txt, conf, sc):
                    nonlocal best_text, best_score, best_confidence
                    if sc > best_score:
                        best_text, best_score, best_confidence = txt, sc, conf

                # ── 1차 빠른 패스: 상위 2종 이진화 × psm 10·7 (4회) ──
                for img_bin, _ in cands[:2]:
                    pil = _PIL.fromarray(_pad(img_bin))
                    for psm in (10, 7):
                        _upd(*_tess_once(pil, psm))
                    if best_confidence >= 60:
                        break   # 높은 신뢰도 → 즉시 채택

                # ── 2차 보완 패스: 1차 미확보 시만 실행 ─────────────
                if best_confidence < 60:
                    for img_bin, _ in cands[2:]:   # 나머지 이진화 후보
                        pil = _PIL.fromarray(_pad(img_bin))
                        for psm in (10, 8, 7):
                            _upd(*_tess_once(pil, psm))
                        if best_confidence >= 40:
                            break
                    # 여전히 미확보면 psm 6(블록)으로 상위 2종 재시도
                    if best_confidence < 40:
                        for img_bin, _ in cands[:2]:
                            pil = _PIL.fromarray(_pad(img_bin))
                            _upd(*_tess_once(pil, 6))
                            if best_confidence >= 40:
                                break

                tess_ran = True

            except ImportError:
                pass
            except Exception as ex:
                roi.last_text = f"[ERR:{str(ex)[:40]}]"
                return

            # ── OCR 2: easyocr fallback ──────────────────────────────
            if not best_text:
                try:
                    import easyocr as _eocr
                    if not hasattr(ocr_self, '_easyocr_reader'):
                        ocr_self._easyocr_reader = _eocr.Reader(
                            ['en'], gpu=False, verbose=False)
                    results = ocr_self._easyocr_reader.readtext(
                        region, detail=1, allowlist='0123456789ABCDEFabcdef')
                    if results:
                        parts = [_clean_number(str(r[1]))
                                 for r in results if float(r[2]) >= 0.3]
                        best_text = "".join(p for p in parts if p)
                    if not best_text and results:
                        best_text = "".join(
                            _clean_number(str(r[1])) for r in results)
                except ImportError:
                    if not tess_ran:
                        roi.last_text = "[ERR:OCR없음-pip install easyocr]"
                        return
                except Exception as ex:
                    roi.last_text = f"[ERR:easy-{str(ex)[:30]}]"
                    return

            # ── 최종 저장 ─────────────────────────────────────────────
            # 숫자만 남기기 (최후 방어선)
            roi.last_text = _clean_number(best_text) if best_text else ""


        # ── 메인 루프 ─────────────────────────────────────────────────
        # ★ v4.5 고속화:
        #   · OCR_INTERVAL 0.3s → 0.05s (루프 대기 단축)
        #     실제 처리 시간이 줄었으므로 CPU 부담 증가 없음
        #   · ROI 병렬 처리: ThreadPoolExecutor
        #     ROI 3개 → 각 ROI를 별도 스레드에서 동시 실행
        #     → 가장 오래 걸리는 ROI 1개의 시간만 소요
        #   · 프레임 캡처 1회 → 모든 ROI가 공유 (중복 캡처 제거)
        OCR_INTERVAL = 0.05  # ★ 0.3→0.05: 루프 대기 단축

        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Tesseract는 프로세스당 1개 인스턴스가 적합.
        # ROI별 병렬화는 Python 스레드 수준으로 충분
        # (GIL이 있어도 Tesseract 내부 C 코드는 GIL 해제)
        _ocr_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="ocr_worker")

        while not self._ocr_stop.is_set():
            try:
                has_roi = bool(self._engine.screen_rois or
                               self._engine.camera_rois)
                scr_ocr_on = getattr(self._engine, 'ocr_screen_enabled', True)
                cam_ocr_on = getattr(self._engine, 'ocr_camera_enabled', True)
                any_ocr_on = (
                    (bool(self._engine.screen_rois) and scr_ocr_on) or
                    (bool(self._engine.camera_rois) and cam_ocr_on)
                )
                if not has_roi or not any_ocr_on:
                    self._ocr_stop.wait(OCR_INTERVAL)
                    continue

                # zoom 배율 읽기
                scr_zoom = cam_zoom = 1.0
                mw = ocr_self
                try:
                    si = getattr(mw, '_scr_inline', None)
                    sf = getattr(getattr(mw, '_disp_win', None), 'prev', None)
                    if si: scr_zoom = max(scr_zoom, getattr(si, '_zoom', 1.0))
                    if sf: scr_zoom = max(scr_zoom, getattr(sf, '_zoom', 1.0))
                    ci = getattr(mw, '_cam_inline', None)
                    cf = getattr(getattr(mw, '_cam_win', None), 'prev', None)
                    if ci: cam_zoom = max(cam_zoom, getattr(ci, '_zoom', 1.0))
                    if cf: cam_zoom = max(cam_zoom, getattr(cf, '_zoom', 1.0))
                except Exception:
                    pass

                zoom_map = {"screen": scr_zoom, "camera": cam_zoom}

                # ── ROI 작업 목록 구성 ────────────────────────────────
                tasks = []
                for source, rois in (
                    ("screen", self._engine.screen_rois),
                    ("camera", self._engine.camera_rois),
                ):
                    if not rois or self._ocr_stop.is_set():
                        continue
                    if source == "screen" and not scr_ocr_on:
                        continue
                    if source == "camera" and not cam_ocr_on:
                        continue
                    # ★ 프레임 1회만 캡처 → 모든 ROI 공유
                    frame = _extract_region(source)
                    if frame is None:
                        continue
                    zh = zoom_map.get(source, 1.0)
                    for roi in rois:
                        tasks.append((roi, frame, zh))

                if not tasks:
                    self._ocr_stop.wait(OCR_INTERVAL)
                    continue

                if len(tasks) == 1:
                    # ROI 1개: 오버헤드 없이 직접 실행
                    _run_ocr_on_roi(*tasks[0])
                else:
                    # ROI 여러 개: 병렬 실행
                    futs = [
                        _ocr_executor.submit(_run_ocr_on_roi, roi, fr, zh)
                        for roi, fr, zh in tasks
                        if not self._ocr_stop.is_set()
                    ]
                    for fut in as_completed(futs):
                        try:
                            fut.result()
                        except Exception:
                            pass

            except Exception:
                pass
            self._ocr_stop.wait(OCR_INTERVAL)

    def _slow_tick(self):
        # FPS 표시
        sf = self._engine.measured_fps(self._engine._scr_fps_ts)
        cf = self._engine.measured_fps(self._engine._cam_fps_ts)
        self._fps_lbl.setText(
            f"Screen: {sf:.1f} fps  |  Camera: {cf:.1f} fps")

        # 녹화 패널 FPS
        rec_panel = self._panels.get("rec")
        if rec_panel and hasattr(rec_panel, 'update_fps'):
            rec_panel.update_fps(sf, cf)

        # 블랙아웃 패널 갱신
        bo_panel = self._panels.get("blackout")
        if bo_panel and hasattr(bo_panel, 'refresh'): bo_panel.refresh()

        # IO채널 상태
        io_ch = getattr(self._engine, 'io_channel', None)
        if io_ch:
            try:
                recording = io_ch.get_state("recording", False)
                self._io_lbl.setText(
                    f"IO채널: {IO_DB_PATH}  |  recording={recording}")
                self._io_lbl.setStyleSheet("color:#446;font-size:9px;font-family:monospace;")
            except: pass

    def _sched_tick(self):
        actions = self._engine.schedule_tick()
        for act, entry in actions:
            if act == 'start':   self._engine.start_recording()
            elif act == 'stop':  self._engine.stop_recording()
            elif act == 'macro_run':
                self._engine.macro_start_run(
                    entry.macro_repeat, entry.macro_gap)
        sched_panel = self._panels.get("schedule")
        if sched_panel and hasattr(sched_panel, 'refresh_tbl'):
            sched_panel.refresh_tbl()

    # ── 설정 저장/복원 ───────────────────────────────────────────────────
    def _load_settings(self):
        db = self._db; e = self._engine

        # 일반 설정
        e.buffer_seconds       = db.get_int("buffer_seconds", 40)
        e.brightness_threshold = db.get_float("brightness_threshold", 30.0)
        e.blackout_cooldown    = db.get_float("blackout_cooldown", 5.0)
        e.manual_pre_sec       = db.get_float("manual_pre_sec", 10.0)
        e.manual_post_sec      = db.get_float("manual_post_sec", 10.0)
        e.manual_source        = db.get("manual_source", "both")
        e.actual_screen_fps    = db.get_float("screen_fps", 30.0)

        # ROI OCR / 밝기 ON/OFF + 블랙아웃 복원
        e.ocr_enabled           = db.get_bool("ocr_enabled", True)
        e.ocr_screen_enabled    = db.get_bool("ocr_screen_enabled", True)
        e.ocr_camera_enabled    = db.get_bool("ocr_camera_enabled", True)
        e.brightness_enabled    = db.get_bool("brightness_enabled", True)
        e.blackout_rec_enabled  = db.get_bool("blackout_rec_enabled", True)

        # ROI 복원
        all_rois = db.load_roi_items()
        e.screen_rois = [r for r in all_rois if r.source == "screen"]
        e.camera_rois = [r for r in all_rois if r.source == "camera"]

        # 경로 설정
        if hasattr(self, '_path_panel'):
            self._path_panel.load_from_db()

        # ★ 패널 UI 값을 DB/engine 값으로 동기화 (처음 실행 시 default 적용)
        self._sync_panels_from_engine()

        # 메모 탭
        if hasattr(self, '_memo_panel'):
            tabs = db.load_memo_tabs()
            if tabs: self._memo_panel.set_tab_data(tabs)

        # 매크로 슬롯
        if hasattr(self, '_macro_panel'):
            slots = db.load_macro_slots()
            if slots: self._macro_panel.set_slots_data(slots)

        # 커널 스크립트
        if hasattr(self, '_kernel_panel'):
            raw = db.get("kernel_scripts", "")
            if raw:
                try:
                    self._kernel_panel.set_scripts_data(json.loads(raw))
                except Exception:
                    pass

        # ── 섹션 순서·가시성 복원 ──────────────────────────────────────
        order_raw = db.get("section_order", "")
        if order_raw:
            try:
                saved = json.loads(order_raw)
                all_keys = [d[0] for d in _SECTION_DEFS]
                valid = [k for k in saved if k in all_keys]
                for k in all_keys:
                    if k not in valid: valid.append(k)
                self._section_order = valid
            except Exception:
                pass

        vis_raw = db.get("section_visible", "")
        if vis_raw:
            try:
                saved_vis = json.loads(vis_raw)
                for k, v in saved_vis.items():
                    self._section_visible[k] = bool(v)
            except Exception:
                pass

        # 순서·가시성 적용
        while self._sect_lay.count():
            it = self._sect_lay.takeAt(0)
            if it.widget(): it.widget().setParent(None)
        for k in self._section_order:
            sec = self._sections.get(k)
            if sec:
                sec.setParent(self._sect_w)
                sec.setVisible(self._section_visible.get(k, True))
                self._sect_lay.addWidget(sec)
        self._sect_lay.addStretch()
        self._rebuild_feat_list()

        # 섹션 접힘 상태 복원
        for key in self._section_order:
            collapsed = db.get_bool(f"sec_col_{key}", key not in ("rec",))
            sec = self._sections.get(key)
            if sec: sec.set_collapsed(collapsed)

        # ROI 패널 갱신
        self._refresh_roi_panels()

        # ROI 패널 버튼 상태 동기화 (복원된 플래그 반영)
        mgr = getattr(self, '_roi_mgr', None)
        if mgr:
            ocr_on    = getattr(e, 'ocr_enabled', True)
            bright_on = getattr(e, 'brightness_enabled', True)
            # ★ v4.5: 독립 버튼 복원
            scr_ocr = getattr(e, 'ocr_screen_enabled', True)
            cam_ocr = getattr(e, 'ocr_camera_enabled', True)
            if hasattr(mgr, '_ocr_scr_btn'):
                mgr._ocr_scr_btn.blockSignals(True)
                mgr._ocr_scr_btn.setChecked(scr_ocr)
                mgr._ocr_scr_btn.setText("🖥 OCR ▶ ON" if scr_ocr else "🖥 OCR ⏸ OFF")
                mgr._ocr_scr_btn.blockSignals(False)
            if hasattr(mgr, '_ocr_cam_btn'):
                mgr._ocr_cam_btn.blockSignals(True)
                mgr._ocr_cam_btn.setChecked(cam_ocr)
                mgr._ocr_cam_btn.setText("📷 OCR ▶ ON" if cam_ocr else "📷 OCR ⏸ OFF")
                mgr._ocr_cam_btn.blockSignals(False)
            if hasattr(mgr, '_ocr_btn'):
                mgr._ocr_btn.blockSignals(True)
                mgr._ocr_btn.setChecked(ocr_on)
                mgr._ocr_btn.setText("🖥 OCR ▶ ON" if ocr_on else "🖥 OCR ⏸ OFF")
                mgr._ocr_btn.blockSignals(False)
            if hasattr(mgr, '_bright_btn'):
                mgr._bright_btn.blockSignals(True)
                mgr._bright_btn.setChecked(bright_on)
                mgr._bright_btn.setText("💡 밝기 ▶ ON" if bright_on else "💡 밝기 ⏸ OFF")
                mgr._bright_btn.blockSignals(False)

        # ★ 수정: BlackoutPanel rec_chk 체크박스를 복원된 엔진 상태와 동기화
        bo_sec = self._sections.get('blackout') if hasattr(self, '_sections') else None
        bo_panel = bo_sec._content.findChild(BlackoutPanel) if bo_sec else None
        # _content의 직접 자식 위젯 탐색
        if bo_sec:
            for child in bo_sec._content.findChildren(BlackoutPanel):
                if hasattr(child, 'rec_chk'):
                    child.rec_chk.blockSignals(True)
                    child.rec_chk.setChecked(e.blackout_rec_enabled)
                    child.rec_chk.blockSignals(False)
                    break

        self._signals.status_message.emit("설정 복원 완료")

    def _save_settings(self):
        """엔진 설정을 DB에 저장. 종료 시 + 1초 디바운스 자동저장 시 호출."""
        db = self._db; e = self._engine

        db.set("buffer_seconds",       str(e.buffer_seconds))
        db.set("brightness_threshold", str(e.brightness_threshold))
        db.set("blackout_cooldown",    str(e.blackout_cooldown))
        db.set("manual_pre_sec",       str(e.manual_pre_sec))
        db.set("manual_post_sec",      str(e.manual_post_sec))
        db.set("manual_source",        e.manual_source)
        db.set("screen_fps",           str(e.actual_screen_fps))

        # ROI OCR / 밝기 ON/OFF + 블랙아웃 저장
        db.set("ocr_enabled",           e.ocr_enabled)
        db.set("ocr_screen_enabled",    getattr(e, "ocr_screen_enabled", True))
        db.set("ocr_camera_enabled",    getattr(e, "ocr_camera_enabled", True))
        db.set("brightness_enabled",    e.brightness_enabled)
        db.set("blackout_rec_enabled",  e.blackout_rec_enabled)

        # ROI 저장
        db.save_roi_items(e.screen_rois + e.camera_rois)

        # 경로 설정
        if hasattr(self, '_path_panel'):
            self._path_panel._save()

        # 메모 탭
        if hasattr(self, '_memo_panel'):
            db.save_memo_tabs(self._memo_panel.get_tab_data())

        # 매크로 슬롯
        if hasattr(self, '_macro_panel'):
            db.save_macro_slots(self._macro_panel.get_slots_data())

        # 커널 스크립트
        if hasattr(self, '_kernel_panel'):
            try:
                db.set("kernel_scripts",
                       json.dumps(self._kernel_panel.get_scripts_data(),
                                  ensure_ascii=False))
            except Exception:
                pass

        # ── 섹션 순서·가시성·접힘 저장 ────────────────────────────────
        if hasattr(self, '_section_order'):
            db.set("section_order",
                   json.dumps(self._section_order, ensure_ascii=False))
        if hasattr(self, '_section_visible'):
            db.set("section_visible",
                   json.dumps(self._section_visible, ensure_ascii=False))
        if hasattr(self, '_sections'):
            for key, sec in self._sections.items():
                db.set(f"sec_col_{key}", sec.is_collapsed())

    # ── 키보드 단축키 ────────────────────────────────────────────────────
    def keyPressEvent(self, e):
        mod = e.modifiers()
        key = e.key()
        CTRL_ALT = Qt.ControlModifier | Qt.AltModifier
        if mod == CTRL_ALT:
            if key == Qt.Key_W:
                if not self._engine.recording: self._engine.start_recording()
            elif key == Qt.Key_E:
                if self._engine.recording:
                    if self._engine.tc_rec_enabled:
                        result = show_tc_dialog(self, "녹화 종료 — T/C 검증 결과 선택")
                        # ★ 수정: 취소(None) 시 녹화 중단하지 않음
                        if result is None:
                            pass  # 취소 → 녹화 계속
                        else:
                            self._engine.tc_verify_result = result
                            self._engine.stop_recording()
                    else:
                        self._engine.stop_recording()
            elif key == Qt.Key_M:
                self._engine.save_manual_clip()
            elif key == Qt.Key_A:
                if not self._engine.ac_enabled: self._engine.start_ac()
            elif key == Qt.Key_S:
                if self._engine.ac_enabled: self._engine.stop_ac()
            elif key == Qt.Key_X:
                self._engine.macro_stop_run()
        super().keyPressEvent(e)

    def _sync_panels_from_engine(self):
        """엔진 설정값으로 각 패널 UI를 동기화. 처음 실행 시 default 적용."""
        e = self._engine
        try:
            # 수동녹화 패널
            if hasattr(self, '_manual_panel'):
                mp = self._manual_panel
                if hasattr(mp, 'pre_spin'):
                    mp.pre_spin.blockSignals(True)
                    mp.pre_spin.setValue(float(getattr(e, 'manual_pre_sec', 10.0)))
                    mp.pre_spin.blockSignals(False)
                if hasattr(mp, 'post_spin'):
                    mp.post_spin.blockSignals(True)
                    mp.post_spin.setValue(float(getattr(e, 'manual_post_sec', 10.0)))
                    mp.post_spin.blockSignals(False)
        except Exception:
            pass
        try:
            # 버퍼 설정 패널
            if hasattr(self, '_buffer_panel'):
                bp = self._buffer_panel
                if hasattr(bp, 'buf_spin'):
                    bp.buf_spin.blockSignals(True)
                    bp.buf_spin.setValue(int(getattr(e, 'buffer_seconds', 40)))
                    bp.buf_spin.blockSignals(False)
        except Exception:
            pass

    def _get_settings_snapshot(self) -> dict:
        """현재 엔진 설정값 스냅샷 (변경 감지용)."""
        e = self._engine
        return {
            'buffer_seconds':       getattr(e, 'buffer_seconds', 40),
            'brightness_threshold': getattr(e, 'brightness_threshold', 30.0),
            'blackout_cooldown':    getattr(e, 'blackout_cooldown', 5.0),
            'manual_pre_sec':       getattr(e, 'manual_pre_sec', 10.0),
            'manual_post_sec':      getattr(e, 'manual_post_sec', 10.0),
            'manual_source':        getattr(e, 'manual_source', 'both'),
            'screen_fps':           getattr(e, 'actual_screen_fps', 30.0),
            'ocr_enabled':          getattr(e, 'ocr_enabled', True),
            'brightness_enabled':   getattr(e, 'brightness_enabled', True),
            'blackout_rec_enabled': getattr(e, 'blackout_rec_enabled', True),
        }

    def _check_and_save_settings(self):
        """3초마다 설정 변경 감지 → 변경 시 DB 저장 (CPU 최소화)."""
        current = self._get_settings_snapshot()
        if current != self._settings_snapshot:
            self._settings_snapshot = current
            self._save_settings()

    def trigger_autosave(self):
        """패널에서 설정 변경 시 호출 — 1초 후 DB 자동저장."""
        if hasattr(self, '_global_autosave_timer'):
            self._global_autosave_timer.start()

    def closeEvent(self, e):
        # 종료 시 즉시 저장 (타이머 대기 없이)
        if hasattr(self, '_global_autosave_timer'):
            self._global_autosave_timer.stop()
        if hasattr(self, '_settings_check_timer'):
            self._settings_check_timer.stop()
        self._save_settings()
        # OCR 루프 중단
        if hasattr(self, '_ocr_stop'):
            self._ocr_stop.set()
        self._kernel.stop()
        self._engine.stop()
        # 전원 제어기 연결 해제
        if hasattr(self, '_power_ctrl') and self._power_ctrl:
            self._power_ctrl.disconnect()
        try: _can_manager.disconnect()
        except Exception: pass
        self._cam_win.close(); self._disp_win.close()
        e.accept()


# =============================================================================
#  main()
# =============================================================================
def main():
    # PyInstaller DPI awareness (Windows)
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    import os as _os_qt; _os_qt.environ.setdefault("QT_LOGGING_RULES", "*.warning=false")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 다크 팔레트
    from PyQt5.QtGui import QPalette
    pal = QPalette()
    pal.setColor(QPalette.Window,          QColor(13,13,30))
    pal.setColor(QPalette.WindowText,      QColor(200,200,220))
    pal.setColor(QPalette.Base,            QColor(8,8,18))
    pal.setColor(QPalette.AlternateBase,   QColor(20,20,40))
    pal.setColor(QPalette.ToolTipBase,     QColor(13,13,30))
    pal.setColor(QPalette.ToolTipText,     QColor(200,200,220))
    pal.setColor(QPalette.Text,            QColor(200,200,220))
    pal.setColor(QPalette.Button,          QColor(26,26,58))
    pal.setColor(QPalette.ButtonText,      QColor(200,200,220))
    pal.setColor(QPalette.BrightText,      QColor(255,80,80))
    pal.setColor(QPalette.Link,            QColor(42,130,218))
    pal.setColor(QPalette.Highlight,       QColor(42,130,218))
    pal.setColor(QPalette.HighlightedText, QColor(0,0,0))
    app.setPalette(pal)

    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()