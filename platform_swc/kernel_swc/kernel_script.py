# -*- coding: utf-8 -*-
"""kernel_swc/kernel_script.py — 커널 스크립트 데이터클래스."""

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


