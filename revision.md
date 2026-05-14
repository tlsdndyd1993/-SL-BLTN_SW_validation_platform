
**[수정 내용]**
- 26.05.03)
  1. AI Chat 기능에서 심어진 LLM 종류 관계 없이, 지속 학습이 가능한 SW 구조 반영
     
     -> (AI 메모리 구조 반영 : Layer 1~3 으로 구분 (Layer 1 : 장기 기억 / Layer 2 : 실시간 상황  /  Layer 3 : 단기 히스토리(최근 6턴만 기억)  -> 토근 체크(2500 토근 이상 : 응답느릴 수 있음 / 3000토근 이상 : layer 3 자동 초기화)
  2. AI Chat 기능 UI 개선
  3. 타이머 기능 추가
  4. 전원 제어 기능 UI개선
  5. CAN Graph 기능 프로세스 병목현상 완화 (Graph 기능 축소)
  6. Atmega328p 전원제어 기능 추가(PLBM)
      

<img width="1199" height="849" alt="image" src="https://github.com/user-attachments/assets/6984a4b4-8405-43f7-92f7-43ee1c4ed0fd" />

- 26.05.13)
1. OHCL 이벤트 택트스위치 및 플랫폼 UI 추가
2. 수동녹화/충격녹화/타임랩스 녹화에 따른 OHCL LED PWM 출력 및 점멸을 플랫폼 UI에서 확인하도록 반영
3. PLBM 전원 제어(실물)/(모사) TRADE OFF 로 반영
4. AI CHAT 및 API 기능 강건화 -> 모든 기능 설정 및 실행 접근 가능하도록 반영
5. 타이머 기능 개선 (조건식에 CAN 값에 따라 타이머 실행/종료/랩 기록 가능)


<img width="1837" height="828" alt="image" src="https://github.com/user-attachments/assets/eee1e0f0-b865-4fb6-aec5-9e97ccd75311" />

<img width="1533" height="1540" alt="image" src="https://github.com/user-attachments/assets/fb4f5cf6-7684-4ecd-85bc-679fa5f36c14" />



