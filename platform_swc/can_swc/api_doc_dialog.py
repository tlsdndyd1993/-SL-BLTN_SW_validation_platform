# -*- coding: utf-8 -*-
"""can_swc/api_doc_dialog.py — API 레퍼런스 NonModal 다이얼로그."""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt

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
