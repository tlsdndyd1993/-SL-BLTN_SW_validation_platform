# -*- coding: utf-8 -*-
"""
common/models.py
SWC 경계를 넘나드는 공용 데이터 모델.
"""
import json
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class RoiItem:
    """ROI 영역 하나를 표현하는 데이터클래스."""
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    name: str = ""
    description: str = ""
    source: str = "screen"   # "screen" | "camera"

    # ── 런타임 캐시 (저장 안 함) ──────────────────────────────────────────
    last_brightness: float = 0.0
    last_text: str = ""
    last_avg_bgr: tuple = (0, 0, 0)
    last_match: bool = False

    # ── 조건값 (저장됨) ──────────────────────────────────────────────────
    cond_value: str = ""
    roi_no_rec_enabled: bool = False
    no_match_count: int = 0

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


class MacroStep:
    __slots__ = ('kind', 'delay', 'x', 'y', 'x2', 'y2', 'button', 'double', 'key_str')

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
        kind  = d.get('kind', 'click')
        delay = d.get('delay', 0.5)
        kw    = {k: d[k] for k in ('x','y','x2','y2','button','double','key_str') if k in d}
        return cls(kind, delay, **kw)

    def summary(self) -> str:
        if self.kind == 'click':
            btn = self.button; dbl = " x2" if self.double else ""
            return f"[Click{dbl}] ({self.x},{self.y}) {btn}"
        elif self.kind == 'drag':
            return f"[Drag] ({self.x},{self.y})\u2192({self.x2},{self.y2})"
        elif self.kind == 'key':
            return f"[Key] {self.key_str}"
        return f"[{self.kind}]"


# 하위 호환
ClickStep = MacroStep


class ScheduleEntry:
    _cnt = 0

    def __init__(self, start_dt, stop_dt, actions=None,
                 macro_repeat=1, macro_gap=1.0):
        ScheduleEntry._cnt += 1
        self.id = ScheduleEntry._cnt
        self.start_dt     = start_dt
        self.stop_dt      = stop_dt
        self.actions      = actions or ['rec_start', 'rec_stop']
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
