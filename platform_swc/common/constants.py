# -*- coding: utf-8 -*-
"""
common/constants.py
프로그램 전역 상수 — SWC 경계를 넘나드는 공용 값만 여기에 둘 것.
각 SWC 전용 상수는 해당 SWC 상단 상수 블록에 위치시킬 것 (§3.3).
"""
import os

# ── 경로 ──────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.join(os.path.expanduser("~/Desktop"), "bltn_rec")
DB_PATH    = os.path.join(BASE_DIR, "settings.db")
IO_DB_PATH = os.path.join(BASE_DIR, "io_channel.db")

# ── UI 테마 ───────────────────────────────────────────────────────────────────
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

# ── 섹션 정의 (MainWindow 패널 순서 및 색상) ─────────────────────────────────
_SECTION_DEFS = [
    # (section_key, 탭 텍스트, 색상)
    ("rec",      "⏺ 녹화",              "#27ae60"),
    ("manual",   "🎬 수동녹화",           "#e67e22"),
    ("blackout", "🔲 블랙아웃 판독",      "#e74c3c"),
    ("roi",      "📐 ROI/OCR 관리",       "#9b59b6"),
    ("timer",    "⏱ 조건부 타이머",       "#1abc9c"),
    ("ac",       "🖱 오토클릭",           "#2980b9"),
    ("power",    "⚡ 전원 제어 / BLTN",   "#e74c3c"),
    ("macro",    "⌨ 입/출력 장치 기록",   "#16a085"),
    ("schedule", "📅 예약",               "#8e44ad"),
    ("memo",     "📝 메모(화면 오버레이)", "#d35400"),
    ("path",     "📁 저장 경로",          "#7bc8e0"),
    ("log",      "📋 로그",               "#556"),
    ("reset",    "♻ 초기화",              "#e74c3c"),
]
