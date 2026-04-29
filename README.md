**단품 수동 평가 환경 업무 활성화용 플랫폼** (26.03.02 ~ 현재)
-
-> 내/외부 통신 가능하게 AI 및 기능들 API 설정O

(개선 및 추가 필요)

-> 전압 제어 (0~20V)

-> CAN 입력 (구현이 어려울 시, 입출력 장치(마우스/키보드) 히스토리 제어 API 를 통해 CAPL 프로그램으로 신호 입력)

-> BLTN 제어기 마운트

-> 카메라 충격 인가를 위한 DC 모터 제어 기능

**[목적]**
-
1. '녹화/수동녹화/캡쳐' 기능을 통한 증적 자료 생성
   
   -> 평가 시작과 동시에 Recording, 평가 완료시 녹화 종료하여 증적 자료 바로 생성.
-
2. Display / Web CAM 출력 화면을 동시에 Recording -> CAN Graph + 실물 동작성 동시 확인하여 증적 자료 생성
   
   <img width="1898" height="1023" alt="image" src="https://github.com/user-attachments/assets/2647d63e-1e9a-44f9-afd0-fde7892894df" />
-  
3. PC환경에서 동작 천이 Aging 테스트 (CAN 신호 Read & 전원제어 & PASS|FAIL 판별 & 녹화)
   
   -> 전원 제어       : **PC** <---(UART)---> Atmega328p <--(전자식 릴레이)--> **BLTN 제어기**

   -> CAN 신호 Read   : PC <--(Vector XL driver) --> CANoe VN1640A

   -> AI Chat        : 대화 형식으로 기능 API의 설정 및 실행을 할 수 있고, 스크립트를 자동 생성 하게끔 함.
   
   -> 커널            : 프로그램 내 기능(녹화/수동녹화/전원제어/블랙아웃/ROI 등)에 대한 API 설정 및 실행을 스크립트 형식으로 제어 가능-
-
4. 자동화 T/C (with HILS) 를 PC 환경에서 병렬로 진행

**[환경 세팅]**
-
1. PC
2. Web CAM
3. VECTOR CANoe 1640A
4. 아두이노 우노 & 택트스위치 4ea & 빵판 & 전자식 릴레이 & wire & 아두이노 USB
5. 검증 대상 제어기(BLTN)
-
-

**[기능]**
-

[녹화]
1. Display | Web CAM 선택하여 레코딩 가능
2. 저장 배속 및 FPS 조절 가능
3. 영상 용량 절감 목적 '코덱 종류', '해상도' 선택
-
[수동 녹화]
1. Display | Web CAM 선택하여 레코딩 가능
2. 원하는 시점으로부터 최대 -40초 ~ +40초 조절하여 녹화 가능
-
[저장 경로]
1. 설정한 폴더트리에 녹화/수동녹화/캡쳐 파일 저장
2. 캡쳐 후 | 녹화 종료 후, (PASS)|(FAIL) 결과 반영 가능 -> 지정한 폴더트리에 (PASS)|(FAIL) 자동 반영
-
[전원제어]
1. PC 에 연결된 아두이노 포트 활성화 시킨 후, B+/ACC/IGN 전원 제어
-
[CAN]
1. (사전 조건) CAPL 실행
2. CAPL에 사용되고 있는 DB를 이 플랫폼에 업로드 후, 그래프 및 커널, Ai chat을 통해 CAN 값 READ.
-
[커널]
1. 프로그램 내 기능들의 설정 및 실행을 스크립트 함수(Python 형식)로 실행 가능하게끔 함.
2. 하나의 스크립트를 최대 무한번 반복 가능
3. 여러 개의 스크립트를 순차적으로 실행 가능
4. 스크립트 import/export 가능
-
[ROI 관리]
1. Web CAM 및 Display 출력 화면에 ROI 영역 각각 최대 10EA 반영 가능
2. ROI 영역 내 Brightness 값 확인 가능
3. ROI 영역 내 HEX 값 OCR로 확인 가능
-
[BLACK OUT]
1. 출력 화면에 있는 ROI영역 내 Brightness 가 급격히 변하는 경우 blackout 으로 판정하여, 그 시점으로부터 -20초~+20초 녹화 시작.
-
[매크로]
1. 클릭, 드래그, 타이핑의 히스토리를 기억하여 추후 동일한 이벤트 실행 가능
2. 여러개의 히스토리를 저장가능하게끔 슬롯1,2,3... 반영
-
[예약]
1. 녹화 시작 및 종료 시점(년/월/일/시/분/초) 을 설정하여 녹화 예약
-
[오토클릭]
1. PC 비활성화 방지 목적으로 클릭 이벤트 자동 반영


-
**[참고 이미지 : Aging 테스트 관련 Ai chat 사용 -> 커널 스크립트 반영]**
-

[AI CHAT] : 요청 -> 응답
 <img width="1440" height="1595" alt="image" src="https://github.com/user-attachments/assets/34f2f1e2-ec66-4d48-a0e4-09349d5947f8" />


-
[커널] : BLTN 상태 파악 -> 프로그램 제어
<img width="950" height="1190" alt="image" src="https://github.com/user-attachments/assets/b23f4e07-a16c-4ff9-a2f9-ac36040419fc" />
(커널 명령에 따라 녹화중...)
<img width="3823" height="2077" alt="image" src="https://github.com/user-attachments/assets/71d5e8c7-7087-4380-8557-9a25d5919868" />

-
[CAN Graph] : CAN BUS 내 시그널 참고
<img width="1587" height="1189" alt="image" src="https://github.com/user-attachments/assets/70037f25-7cfb-4076-8bab-a53f79c70259" />




