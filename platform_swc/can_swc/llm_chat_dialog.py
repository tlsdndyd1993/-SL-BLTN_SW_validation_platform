# -*- coding: utf-8 -*-
"""can_swc/llm_chat_dialog.py — LLM 채팅 NonModal 다이얼로그."""
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QTextCursor

from common.signals import Signals
from can_swc.ai_chat_controller import AIChatController

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

        # 토큰/메모리 상태 표시 바
        self._token_lbl = QLabel("🧠 메모리: 단기 0턴 | 추정 0토큰 | 장기기억 0토큰")
        self._token_lbl.setStyleSheet(
            "color:#446;font-size:9px;background:#03030a;"
            "border-bottom:1px solid #1a1a2a;padding:2px 6px;font-family:monospace;")
        self._token_lbl.setFixedHeight(18)
        ct.addWidget(self._token_lbl)

        # 메모리 상태 갱신 타이머 (3초마다)
        self._mem_timer = QTimer(self)
        self._mem_timer.setInterval(3000)
        self._mem_timer.timeout.connect(self._refresh_token_lbl)
        self._mem_timer.start()

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
                          ("🎬 수동녹화","수동녹화 저장해줘"),
                          ("📸 캡처","화면 캡처해줘"),("📊 상태","현재 상태 알려줘")]:
            b = QPushButton(lbl); b.setFixedHeight(26)
            b.setStyleSheet(
                "QPushButton{background:#0f1a2a;color:#aaa;"
                "border:1px solid #1a3a5a;border-radius:3px;font-size:10px;}"
                "QPushButton:hover{background:#1a2a3a;color:#dde;}")
            b.clicked.connect(lambda _,c=cmd: self._quick(c))
            ql.addWidget(b)

        # 단기 히스토리만 초기화 (장기기억 유지)
        clr_btn = QPushButton("🗑 대화초기화"); clr_btn.setFixedHeight(26)
        clr_btn.setToolTip("단기 대화 기록 삭제 (장기기억은 유지됩니다)")
        clr_btn.setStyleSheet(
            "QPushButton{background:#0f1a2a;color:#aaa;"
            "border:1px solid #1a3a5a;border-radius:3px;font-size:10px;}"
            "QPushButton:hover{background:#1a2a3a;color:#dde;}")
        clr_btn.clicked.connect(self._clear_chat)
        ql.addWidget(clr_btn)

        # 장기기억까지 완전 초기화
        rst_btn = QPushButton("🧹 기억전체삭제"); rst_btn.setFixedHeight(26)
        rst_btn.setToolTip("단기 대화 + 장기기억 모두 초기화")
        rst_btn.setStyleSheet(
            "QPushButton{background:#2a0a0a;color:#f87;"
            "border:1px solid #5a2a2a;border-radius:3px;font-size:10px;}"
            "QPushButton:hover{background:#3a1010;}")
        rst_btn.clicked.connect(self._clear_all_memory)
        ql.addWidget(rst_btn)

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
        # [수정1] 사용자 메시지(👤)는 오른쪽, AI 메시지(🤖)는 왼쪽 정렬
        is_user = text.startswith("👤")
        if is_user:
            # 오른쪽 정렬: margin-left auto, 연한 배경
            self._log.append(
                f'<div style="text-align:right;margin-left:15%;margin-bottom:4px;">'
                f'<span style="color:{color};background:#0d1a2a;border-radius:8px 8px 2px 8px;'
                f'padding:4px 10px;display:inline-block;font-size:11px;">{safe}</span></div>'
                '<hr style="border:none;border-top:1px solid #1a1a2a;margin:2px 0;">')
        else:
            # 왼쪽 정렬 (AI)
            self._log.append(
                f'<div style="text-align:left;margin-right:15%;margin-bottom:4px;">'
                f'<span style="color:{color};background:#0a1a0a;border-radius:8px 8px 8px 2px;'
                f'padding:4px 10px;display:inline-block;font-size:11px;">{safe}</span></div>'
                '<hr style="border:none;border-top:1px solid #1a1a2a;margin:2px 0;">')
        sb = self._log.verticalScrollBar(); sb.setValue(sb.maximum())

    def _refresh_token_lbl(self):
        """3초마다 메모리 상태 레이블 갱신."""
        ctrl = self._ctrl
        hist_n   = len(ctrl._history)
        last_tok = ctrl._last_token_est
        lt_tok   = ctrl._long_term_tokens
        warn = ctrl._TOKEN_WARN
        color = "#f0c040" if last_tok >= warn else "#446"
        self._token_lbl.setText(
            f"🧠 단기 {hist_n}항목 | 추정 {last_tok}토큰 "
            f"(경고>{warn}) | 장기기억 {lt_tok}토큰")
        self._token_lbl.setStyleSheet(
            f"color:{color};font-size:9px;background:#03030a;"
            "border-bottom:1px solid #1a1a2a;padding:2px 6px;font-family:monospace;")

    def _clear_chat(self):
        """단기 대화 기록만 삭제. 장기기억(long_term_summary)은 유지."""
        self._ctrl.clear_history()
        self._log.clear()
        self._append("🗑 단기 대화 기록이 초기화됐습니다. (장기기억은 유지됩니다)", "#446")

    def _clear_all_memory(self):
        """단기 히스토리 + 장기기억 모두 초기화."""
        from PyQt5.QtWidgets import QMessageBox
        r = QMessageBox.question(
            self, "기억 전체 삭제",
            "단기 대화 + 장기기억(이전 대화 압축본)을 모두 삭제합니다.\n계속하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        self._ctrl.clear_all_memory()
        self._log.clear()
        self._append("🧹 AI 기억이 완전히 초기화됐습니다.", "#f87")

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


