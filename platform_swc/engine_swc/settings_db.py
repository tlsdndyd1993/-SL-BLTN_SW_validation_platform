# -*- coding: utf-8 -*-
"""
engine_swc/settings_db.py
SQLite 기반 설정 저장소 + 외부 I/O 채널 DB.

설계 원칙
 · §5.3 DB 접근 직렬화 — _lock으로 모든 읽기/쓰기 직렬화
 · §2.8 설정 단일 책임 — 모든 설정 읽기/쓰기는 이 모듈만 담당
 · 키 Prefix 소유권(§3.4)은 각 SWC가 자신의 prefix만 사용해야 함
"""
import os, json, sqlite3, threading
from typing import List, Optional, Any
from common.models import RoiItem, MacroStep
from common.constants import DB_PATH, IO_DB_PATH

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
                # 컬럼 추가 마이그레이션 (roi_no_rec_enabled)
                try:
                    c.execute("ALTER TABLE roi_items ADD COLUMN roi_no_rec_enabled INTEGER DEFAULT 0")
                except Exception:
                    pass
                c.execute("DELETE FROM roi_items")
                c.executemany(
                    "INSERT INTO roi_items(source,x,y,w,h,name,description,sort_order,roi_no_rec_enabled)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    [(r.source, r.x, r.y, r.w, r.h, r.name, r.description, i,
                      int(r.roi_no_rec_enabled))
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
                    sort_order INTEGER DEFAULT 0,
                    roi_no_rec_enabled INTEGER DEFAULT 0)""")
                # 마이그레이션: 컬럼 없으면 추가
                try:
                    c.execute("ALTER TABLE roi_items ADD COLUMN roi_no_rec_enabled INTEGER DEFAULT 0")
                except Exception:
                    pass
                rows = c.execute(
                    "SELECT source,x,y,w,h,name,description,roi_no_rec_enabled"
                    " FROM roi_items ORDER BY sort_order").fetchall()
        return [RoiItem(source=r[0], x=r[1], y=r[2], w=r[3], h=r[4],
                        name=r[5], description=r[6],
                        roi_no_rec_enabled=bool(r[7])) for r in rows]

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

