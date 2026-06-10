# Screen & Camera Recorder — SWC 분리 구조

## 디렉토리 구조

```
platform_swc/
├── common/                     Layer 0 — 의존성 없음, 모든 SWC가 임포트
│   ├── constants.py            전역 경로 상수, _DARK_QSS, _SECTION_DEFS
│   ├── signals.py              Signals (QObject pyqtSignal 허브)
│   ├── models.py               RoiItem, MacroStep, ScheduleEntry, MemoOverlayCfg
│   └── utils.py                open_folder, fmt_hms, draw_*, _get_font, resource_path
│
├── engine_swc/                 Layer 1 — common만 의존
│   ├── settings_db.py          SettingsDB, IoChannelDB
│   └── core_engine.py          CoreEngine (녹화·카메라·스크린·블랙아웃·ROI)
│
├── kernel_swc/                 Layer 2 — engine_swc + common 의존
│   ├── kernel_result_log.py    KernelResultLog
│   ├── kernel_script.py        KernelScript
│   ├── power_controller.py     PowerController (Arduino 시리얼)
│   └── kernel_engine.py        KernelEngine (exec 모드 A + 서브프로세스 모드 B)
│
├── can_swc/                    Layer 2 — engine_swc + common 의존
│   ├── make_script_helpers.py  make_script 전처리 파이프라인 (6단계)
│   ├── can_bus_manager.py      CanBusManager (CANoe COM + XL Driver)
│   ├── ai_chat_controller.py   AIChatController (Groq/Gemini/Claude)
│   ├── can_monitor_dialog.py   CanMonitorDialog
│   ├── llm_chat_dialog.py      LLMChatDialog
│   └── api_doc_dialog.py       ApiDocDialog
│
├── ui_swc/                     Layer 3 — 모든 하위 SWC 의존 가능
│   ├── widgets.py              ClickLabel, CollapsibleSection, _FeatDragList, _make_spinbox
│   ├── preview_window.py       PreviewLabel, CameraWindow, DisplayWindow
│   ├── roi_manager_panel.py    RoiEditDialog, RoiManagerPanel
│   ├── recording_panel.py      RecordingPanel, show_tc_dialog
│   ├── manual_clip_panel.py    ManualClipPanel
│   ├── blackout_panel.py       BlackoutPanel
│   ├── path_settings_panel.py  PathSettingsPanel
│   ├── autoclick_panel.py      AutoClickPanel
│   ├── macro_panel.py          MacroPanel
│   ├── schedule_panel.py       SchedulePanel
│   ├── memo_panel.py           TimestampMemoEdit, MemoPanel
│   ├── kernel_panel.py         KernelPanel, KernelDialog
│   ├── power_panel.py          PowerControlPanel, BLTNStatusPanel, _LedPwmChart
│   ├── timer_panel.py          TimerPanel
│   └── status_panel.py         ResetPanel, LogPanel
│
├── main.py                     Layer 4 — MainWindow + main() + DI 조립
└── main.spec                   PyInstaller 빌드 스펙 (§3.5)
```

## 의존성 그래프 (단방향 — §3.1)

```
common → engine_swc → (kernel_swc / can_swc) → ui_swc → main
```

## 수정이 필요한 todo 항목

### 1. §1.6 중복 타이머 초기화 (main.py)
`MainWindow.__init__`에서 `_gc_timer`가 두 번 초기화됨 (원본 L18929, L18935).
`main.py` 내 두 번째 `_gc_timer =` 블록을 제거할 것.

### 2. §2.5 _can_manager 전역 변수 → DI 완전 교체
`main.py` 상단의 `_can_manager = CanBusManager(None)` 은 임시 조치.
`MainWindow.__init__`에서 `CanBusManager(self._signals)` 생성 후
`closeEvent`에서 `self._can_manager.disconnect()` 호출하도록 교체.
`_can_manager`를 직접 참조하는 코드(`closeEvent` 내 `_can_manager.disconnect()`)도 업데이트.

### 3. §3.2 AIChatController 내부 속성 직접 접근
`can_swc/ai_chat_controller.py` 내에서 `engine._xxx` 형태로
`CoreEngine` 내부 속성을 직접 읽는 곳을 찾아 public getter 메서드로 교체.
예: `engine._scr_buf` → `engine.get_screen_buffer()`

### 4. §3.4 설정 키 Prefix 정리
현재 모든 설정 키가 `MainWindow._save_settings`에 혼재.
향후 각 SWC 전용 키는 SWC 내부에서 관리하도록 분리 권장:
- `rec_`, `cam_`, `bo_` → recording_swc
- `kernel_` → kernel_swc
- `can_`, `ai_` → can_swc
- `roi_`, `memo_`, `macro_`, `path_` → ui_swc

### 5. §10.1 SWC별 단위 테스트
각 SWC에 pytest 단위 테스트 최소 1개 이상 작성 필요:
```
tests/
  test_settings_db.py
  test_core_engine.py
  test_kernel_engine.py
  test_can_bus_manager.py
```

## 빌드

```batch
REM 1차 (인터넷 PC): 의존성 수집
pip install pyinstaller opencv-python numpy mss PyQt5 pyserial

REM 2차 (오프라인 PC): EXE 생성
pyinstaller main.spec
```
