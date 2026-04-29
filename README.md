단품 수동 평가 환경 업무 활성화용 플랫폼 프로그램


[목적]

1. '녹화/수동녹화/캡쳐' 기능을 통한 증적 자료 생성
   -> 평가 시작과 동시에 Recording, 평가 완료시 녹화 종료하여 증적 자료 바로 생성.

   
2. Display / Web CAM 출력 화면을 동시에 Recording -> CAN Graph + 실물 동작성 동시 확인하여 증적 자료 생성
   <img width="1898" height="1023" alt="image" src="https://github.com/user-attachments/assets/2647d63e-1e9a-44f9-afd0-fde7892894df" />

   
3. PC환경에서, 제어기 전원 제어 및 CAN 시그널 READ 를 통해 특정 동작 천이 Aging 테스트
   -> 전원 제어       : PC <---(UART)---> Atmega328p <--(전자식 릴레이)--> BLTN 제어기
   -> CAN 신호 Read   : PC <--(Vector XL driver) --> CANoe VN1640A
   
