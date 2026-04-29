단품 수동 평가 환경 업무 활성화용 플랫폼 프로그램 (26.03.02 ~ 현재)


[목적]

1. '녹화/수동녹화/캡쳐' 기능을 통한 증적 자료 생성
   
   -> 평가 시작과 동시에 Recording, 평가 완료시 녹화 종료하여 증적 자료 바로 생성.

   
2. Display / Web CAM 출력 화면을 동시에 Recording -> CAN Graph + 실물 동작성 동시 확인하여 증적 자료 생성
   
   <img width="1898" height="1023" alt="image" src="https://github.com/user-attachments/assets/2647d63e-1e9a-44f9-afd0-fde7892894df" />

   
3. PC환경에서, 제어기 전원 제어 및 CAN 시그널 READ 를 통해 특정 동작 천이 Aging 테스트
   
   -> 전원 제어       : PC <---(UART)---> Atmega328p <--(전자식 릴레이)--> BLTN 제어기

   -> CAN 신호 Read   : PC <--(Vector XL driver) --> CANoe VN1640A

   -> AI Chat        : 대화 형식으로 기능 API의 설정 및 실행을 할 수 있고, 스크립트를 자동 생성 하게끔 함.


 <img width="1440" height="1595" alt="image" src="https://github.com/user-attachments/assets/34f2f1e2-ec66-4d48-a0e4-09349d5947f8" />


   -> 커널            : 프로그램 내 기능(녹화/수동녹화/전원제어/블랙아웃/ROI 등)에 대한 API 설정 및 실행을 스크립트 형식으로 제어 가능

<img width="950" height="1190" alt="image" src="https://github.com/user-attachments/assets/b23f4e07-a16c-4ff9-a2f9-ac36040419fc" />

<img width="3823" height="2077" alt="image" src="https://github.com/user-attachments/assets/71d5e8c7-7087-4380-8557-9a25d5919868" />





