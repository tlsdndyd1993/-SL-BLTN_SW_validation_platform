# Screen & Camera Recorder

**단품 평가 환경 자동화 플랫폼** — 차량용 제어기(BLTN) 검증 시 녹화·전원제어·CAN 신호 연동·AI 기반 T/C 스크립트 자동 생성을 통합한 Windows 데스크톱 도구.

> 개발 기간: 2026.03.02 ~ 현재

---

## 시연 영상

[![YouTube](https://img.shields.io/badge/YouTube-Demo-red?logo=youtube)](https://youtu.be/LCkzGNMzZxA)

---

## 환경 구성

| 항목 | 내용 |
|------|------|
| PC | Windows (오프라인 배포 EXE 지원) |
| 카메라 | USB Web CAM |
| CAN 인터페이스 | VECTOR CANoe VN1640A |
| 검증 대상 | BLTN 제어기 + 주변 환경 (CCU / HU / Display / PLBM …) |
| 계측 장비 | DMM / POWER |
| 전원 제어 HW | Arduino Uno + 택트스위치 7EA + 전자식 릴레이 + 저항 (10kΩ / 2kΩ / 4.7kΩ) + 브레드보드 + USB |

---

## 주요 기능

### 1. 녹화 / 수동 녹화 / 캡처

- Display 및 Web CAM 화면 동시 녹화 → CAN Graph + 실물 동작 동시 증적
- 평가 시작·종료 시 자동 녹화 시작·중단
- 수동 녹화: 원하는 시점 기준 최대 **−40초 ~ +40초** 범위 지정
- 코덱 종류·해상도·저장 배속·FPS 조절
- 캡처·녹화 완료 후 `PASS / FAIL` 선택 → 결과가 폴더 트리에 자동 반영

### 2. 전원 제어

PC ↔ (UART) ↔ ATmega328p ↔ (전자식 릴레이) ↔ BLTN 제어기

| 채널 | 설명 |
|------|------|
| BLTN B+ | 제어기 메인 전원 |
| HU & CCU B+ | 헤드유닛 / CCU 전원 |
| ACC / IGN | 차량 전원 라인 |
| PLBM (실물 / 모사) | PLBM 릴레이 |
| OHCL 스위치 | 수동녹화 / 타임랩스 트리거 |

택트스위치 및 플랫폼 UI 양쪽으로 제어 가능.

### 3. CAN 버스

- Vector XL Driver를 통해 CANoe VN1640A와 통신
- DBC 파일 업로드 → 신호 그래프 표시 및 커널·AI Chat에서 값 READ
- 사전 조건: CAPL 실행 필요

### 4. 커널 (스크립트 실행 엔진)

```python
# 커널 스크립트 예시
engine.set_manual_time(20, 30)
engine.start_recording()
kernel.wait(5)
engine.save_manual_clip()
kernel.wait_manual_clip(timeout=70)
engine.stop_recording()
```

- 프로그램 내 모든 기능(녹화·전원제어·블랙아웃·ROI 등)을 Python 스크립트로 제어
- 단일 스크립트 무한 반복 / 여러 스크립트 순차 실행
- 스크립트 Import / Export 지원

### 5. AI Chat

- Groq / Gemini / Claude API 연동
- 대화 형식으로 기능 설정값 반영 및 실행
- 요청에 따라 T/C 커널 스크립트 자동 생성
- 내·외부 LLM이 플랫폼 기능을 API처럼 직접 제어 가능

> AI Chat → 스크립트 자동 생성 → 커널 실행 흐름:
>
> `[AI Chat 요청]` → `[커널 스크립트 생성]` → `[커널에서 프로그램 제어]`

### 6. ROI / OCR 관리

- Display·Web CAM 각각 최대 **10개** ROI 영역 설정 (ROI별 네이밍)
- ROI 내 **Brightness** 실시간 모니터링
- ROI 내 **HEX 값** OCR 인식

### 7. 블랙아웃 감지

ROI 영역 내 Brightness가 급격히 하강하면 블랙아웃으로 판정 → 해당 시점 기준 **−20초 ~ +20초** 자동 녹화

### 8. 조건부 타이머

전원 채널 상태 또는 CAN 신호 값 조건을 기준으로 타이머 시작 / 종료 / 랩 기록

### 9. 저장 경로

설정한 폴더 트리(`차종 / 날짜 / TC-ID / 추가 세그먼트`)에 녹화·수동녹화·캡처 파일 자동 저장

### 10. 기타 기능

| 기능 | 설명 |
|------|------|
| 오토클릭 | PC 비활성화 방지용 자동 클릭 이벤트 |
| 입/출력 장치 히스토리 | 클릭·드래그·타이핑 이력 저장 및 재실행 (슬롯 다수) |
| 예약 | 녹화 시작·종료 시점(년/월/일/시/분/초) 예약 설정 |
| 메모 오버레이 | 메모 내용을 녹화 영상에 직접 오버레이 |

---

## 아키텍처 개요

```
common/ ──────────────────────────── 전역 상수·모델·시그널
engine_swc/ ─────────────────────── CoreEngine (녹화·캡처·ROI)
kernel_swc/ ─────────────────────── KernelEngine · PowerController
can_swc/ ────────────────────────── CanBusManager · AIChatController
ui_swc/ ─────────────────────────── 각 기능 패널 (16개)
main.py ─────────────────────────── MainWindow + DI 조립
```

---

## 실행 환경 / 빌드

```batch
:: 의존성 설치
pip install opencv-python numpy mss PyQt5 pyserial groq anthropic google-genai

:: EXE 빌드 (PyInstaller)
pyinstaller main.spec
```

오프라인 PC 배포 지원 — 별도 Python 설치 불필요.

---

## Kernel API Reference

Control Panel 최상단 `📖 API` 버튼에서 전체 함수 문서를 확인할 수 있습니다.

---

## 로드맵

- [ ] SSH/SFTP 연동
- [ ] 전압 제어 (0 ~ 20V)
- [ ] CAN 신호 인가 (TX)
- [ ] Ethernet (DoIP, SOME/IP)
- [ ] TP 인터럽트
- [ ] 시리얼 로깅 (UART)
- [ ] BLTN 제어기 마운트
- [ ] DC 모터 제어 (카메라 충격 인가)
- [ ] LED PWM / OHCL 인터럽트 스위치 UI 통합
