# -*- coding: utf-8 -*-
"""kernel_swc/kernel_result_log.py — 커널 실행 결과 로그."""
import os, json
from datetime import datetime
from typing import List, Optional

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

    RESULT_FILE_SPLIT = 300   # 이 개수마다 결과 파일을 새로 생성

    def __init__(self):
        self._entries: list = []        # List[dict] — 현재 파일 분할 구간의 항목
        self._session_start: str = ""
        self._file_index: int = 1       # 현재 저장 중인 파일 번호 (001, 002...)
        self._result_count: int = 0     # 전체 누적 결과 수 (SESSION 제외)

    def begin_session(self):
        """
        새 세션 시작.
        ★ _entries / _file_index / _result_count 유지 — 재시작 시 이력 보존.
           세션 구분자만 추가해 재시작 시점 구분.
        """
        self._session_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._entries.append({
            "session_start": self._session_start,
            "script": "", "attempt": 0, "total": 0,
            "result": "SESSION", "ts": self._session_start,
        })

    def reset_all(self):
        """결과 이력 완전 초기화 (수동 리셋용 — 일반 실행에서는 호출하지 않음)."""
        self._entries.clear()
        self._file_index   = 1
        self._result_count = 0
        self._session_start = ""

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
        self._result_count += 1

        # ★ RESULT_FILE_SPLIT(300)개 도달 시 파일 인덱스 증가 + _entries 초기화
        # → 다음 _auto_export 호출 시 새 파일(002, 003...)에 저장됨
        if self._result_count > 0 and self._result_count % self.RESULT_FILE_SPLIT == 0:
            self._file_index += 1
            # 새 파일 구간 시작 — 이전 항목은 이미 파일에 저장됨
            self._entries.clear()
            self._entries.append({
                "session_start": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "script": "", "attempt": 0, "total": 0,
                "result": "SESSION",
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

    def get_summary(self) -> str:
        """
        현재까지 누적된 결과 요약 문자열 반환.
        형식:
            [스크립트명]  시도 N/M  PASS|FAIL  2026-04-13 10:00:00
        """
        if not self._entries:
            return "(결과 없음)"
        _real_cnt = sum(1 for e in self._entries if e.get('result') != 'SESSION')
        _range_start = (self._file_index - 1) * self.RESULT_FILE_SPLIT + 1
        _range_end   = _range_start + _real_cnt - 1
        lines = [
            f"=== 커널 실행 결과 요약 ===",
            f"세션 시작: {self._session_start}",
            f"파일 번호: {self._file_index:03d}  "
            f"(전체 {self._result_count}회 중 {_range_start}~{_range_end}회)",
            f"이 파일 기록 수: {_real_cnt}  / {self.RESULT_FILE_SPLIT}  "
            f"({self.RESULT_FILE_SPLIT - _real_cnt}개 남음 → 초과 시 다음 파일 생성)",
            "",
        ]
        # 스크립트별 PASS/FAIL 집계
        from collections import defaultdict
        agg: dict = defaultdict(lambda: {"pass": 0, "fail": 0, "skip": 0})
        for e in self._entries:
            if e.get('result') == 'SESSION': continue  # 세션 구분자 제외
            key = e["script"]
            r   = e["result"].upper()
            if r == "PASS":   agg[key]["pass"] += 1
            elif r == "FAIL": agg[key]["fail"] += 1
            else:             agg[key]["skip"] += 1

        # 집계 테이블
        lines.append("[ 스크립트별 집계 ]")
        for name, cnt in agg.items():
            total_cnt = cnt["pass"] + cnt["fail"] + cnt["skip"]
            lines.append(
                f"  {name:<30}  PASS:{cnt['pass']:>3}  FAIL:{cnt['fail']:>3}"
                f"  SKIP:{cnt['skip']:>3}  합계:{total_cnt:>3}")
        lines.append("")
        lines.append("[ 상세 기록 ]")
        lines.append(f"  {'스크립트':<30}  {'시도':>8}  {'결과':<6}  시각")
        lines.append("  " + "-" * 70)
        for e in self._entries:
            # 세션 구분자는 구분선으로 표시
            if e.get('result') == 'SESSION':
                lines.append(f"  {'─'*70}")
                lines.append(f"  ▶ 세션 시작: {e['ts']}")
                lines.append("  " + "-" * 70)
                continue
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

    # ── 파일명 결정 헬퍼 ────────────────────────────────────────────────
    @staticmethod
    def _safe_filename(title: str) -> str:
        """스크립트 제목 → 파일시스템 안전 문자열 (공백·특수문자 제거)."""
        import re as _re
        safe = _re.sub(r'[\\/:*?"<>|]', '_', title).strip()
        return safe[:60] if safe else 'kernel'

    def result_path(self, base_dir: str, script_title: str) -> str:
        """
        결과 파일 경로 반환.
        - 300개마다 새 파일: kernel_result_슬롯명_001.txt, _002.txt ...
        - _file_index 에 따라 현재 저장 대상 파일 결정
        """
        fdir = base_dir if base_dir else os.path.join(
            os.path.expanduser('~/Desktop'), 'bltn_rec', 'kernel_logs')
        os.makedirs(fdir, exist_ok=True)
        safe = self._safe_filename(script_title)
        return os.path.join(fdir,
            f'kernel_result_{safe}_{self._file_index:03d}.txt')

    def log_path(self, base_dir: str, script_title: str) -> str:
        """로그 파일 경로 반환 — 같은 스크립트 제목이면 항상 동일 경로."""
        fdir = base_dir if base_dir else os.path.join(
            os.path.expanduser('~/Desktop'), 'bltn_rec', 'kernel_logs')
        os.makedirs(fdir, exist_ok=True)
        safe = self._safe_filename(script_title)
        return os.path.join(fdir, f'kernel_log_{safe}.txt')

    def open_in_notepad(self, base_dir: str = "", script_title: str = "") -> str:
        """
        결과를 .txt 파일로 저장 후 메모장으로 열기.
        script_title 지정 시 고정 파일명(덮어쓰기), 미지정 시 타임스탬프 파일명.
        Returns 저장된 파일 경로 (실패 시 빈 문자열).
        """
        fdir = base_dir if base_dir else os.path.join(
            os.path.expanduser('~/Desktop'), 'bltn_rec', 'kernel_logs')
        os.makedirs(fdir, exist_ok=True)
        if script_title:
            # 스크립트 제목 기반 고정 파일명 → 반복 실행 시 덮어쓰기
            path = self.result_path(fdir, script_title)
        else:
            ts   = datetime.now().strftime('%Y%m%d_%H%M%S')
            path = os.path.join(fdir, f'kernel_result_{ts}.txt')
        if not self.export_txt(path):
            return ''
        try:
            if platform.system() == 'Windows':
                subprocess.Popen(['notepad.exe', path])
            elif platform.system() == 'Darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception:
            pass
        return path

