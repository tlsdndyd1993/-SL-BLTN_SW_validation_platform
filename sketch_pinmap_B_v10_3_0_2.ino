/*
 * Screen & Camera Recorder — 전원 제어 Arduino 펌웨어 v10.3 (핀맵 B 타입)
 * =========================================================
 * [v10.3 수정사항]
 *   · [PC 초기 상태 수신 안정화] 부팅 패킷 전송 횟수 증가 및 간격 조정.
 *       기존: uart_send_state() × 3 (즉시 / 300ms / 1000ms 후)
 *       수정: × 4 (즉시 / 200ms / 500ms / 1200ms 후)
 *       이유: PC 측에서 reset_input_buffer() 제거(v4.11) 후, _rx_loop 스레드
 *             기동 타이밍에 따라 첫 번째 패킷을 놓칠 수 있는 경우를 대비.
 *             총 전송 시간을 1200ms로 늘려 어떤 타이밍에도 최소 1개 이상 수신.
 *
 * [v10.1~v10.2 수정사항은 하단 원본 주석 참조]
 *   · [PC→Arduino OHCL 직접 트리거 추가] "A<ms>\n" 명령
 *       PC에서 릴레이 CLOSE 시간(ms)을 직접 지정해 A1 릴레이를 트리거.
 *       형식: ASCII 'A' + 십진수ms + '\n'  예) "A1250\n" / "A1350\n"
 *       범위: 100ms ~ 9999ms (범위 초과 시 무시)
 *       동작: 릴레이 busy 중이면 즉시 무시, 아니면 해당 ms CLOSE 후 "K<ms>\n" 완료 보고.
 *       ISR에서 'A' 수신 시 g_rx_ohcl_flag=1 세팅 → 메인루프에서 나머지 폴링 수신.
 *   · [버그 수정] ISR에서 'A' 이후 바이트("1250\n" 등)가 기존 전원 명령으로
 *       오처리되는 치명적 버그 수정.
 *       원인: "A1250\n" 전송 시 ISR이 '1'→B+ ON, '2'→CCU ON, '5'→PLBM ON,
 *             '0'→ALL OFF 순으로 처리해 전원이 꺼지는 현상.
 *       수정: g_rx_ohcl_flag + g_rx_ohcl_buf[] ISR 레벨 소형 버퍼 도입.
 *             'A' 수신 후 g_rx_ohcl_collecting=1 세팅 → 이후 바이트는
 *             switch/case 처리 없이 버퍼에만 쌓음 → '\n' 수신 시
 *             g_rx_ohcl_ready=1 세팅 → 메인루프에서 버퍼 파싱.
 *   · v10.0의 OHCL 스위치(D6), 릴레이(A1), LED PWM(A2) 기능 모두 유지.
 *
 * [v10.0 수정/추가사항]
 *   · [OHCL 스위치 추가] D6(PD6) — OHCL 택트스위치 입력 (INPUT_PULLUP)
 *       기존 "미사용 — 플로팅 방지" 핀을 OHCL 스위치로 전용.
 *       DDR/PORT 초기화는 v9.0 방식(= 완전 덮어쓰기) 유지, PD6 INPUT_PULLUP 그대로.
 *
 *   · [OHCL 릴레이 출력 추가] A1(PC1) — OHCL 전자식 릴레이 (OUTPUT, Active-Low)
 *       DDRC = 0x00 → DDRC = (1<<PC1) 으로 변경.
 *       PORTC 초기값: PC1 = HIGH (릴레이 OFF, Active-Low).
 *       나머지 PC0/PC2~PC5 는 INPUT_PULLUP 유지.
 *
 *   · [LED PWM 입력 추가] A2(PC2) — BLTN OHCL LED PWM 입력 (INPUT_PULLUP 유지)
 *       analogRead(PC2)로 10ms마다 샘플링 → 엣지(Hi→Lo, Lo→Hi) 검출.
 *       점멸 반주기(ms) 측정 → "P<ms>\n" 패킷으로 PC에 전송.
 *       LED OFF 판정 타임아웃: 200ms 이상 엣지 없으면 "L0\n" 전송.
 *       LED ON 판정: 첫 엣지 검출 시 "L1\n" 전송 (상태 변경 시에만).
 *
 *   · [OHCL 더블클릭/싱글클릭 판별] D6(PD6) 스위치
 *       더블클릭(350ms 이내 2회 누름) → A1 릴레이 1350ms CLOSE → "M\n" 전송
 *       싱글클릭(350ms 이내 2회 누름 없음) → A1 릴레이 1250ms CLOSE → "N\n" 전송
 *       릴레이 동작 중 중복 입력 무시 (방어 로직).
 *       릴레이 CLOSE 시간: 1300ms 기준 → MAR(<1300ms) / TML(>=1300ms) 구분은 PC에서 수행.
 *         실제 구현: 싱글=1250ms, 더블=1350ms.
 *
 *   · v9.0의 D8(PB0) SW6, PLBM 상호 배타, EEPROM, UART 프로토콜 유지.
 *
 * ── 핀맵 (v10.0) ─────────────────────────────────────────────────────────
 *  A0(PC0) | 택트스위치1  (BLTN B+)               — INPUT_PULLUP
 *  A1(PC1) | OHCL 전자식 릴레이                   — OUTPUT (Low=ON/CLOSE)  ★추가
 *  A2(PC2) | BLTN OHCL LED PWM 입력               — INPUT_PULLUP (analogRead) ★추가
 *  D2(PD2) | 택트스위치2  (BLTN CCU & HU B+)      — INPUT_PULLUP
 *  D3(PD3) | 택트스위치3  (ACC)                    — INPUT_PULLUP
 *  D4(PD4) | 택트스위치4  (IGN)                    — INPUT_PULLUP
 *  D5(PD5) | 택트스위치5  (PLBM 실물)              — INPUT_PULLUP
 *  D6(PD6) | OHCL 택트스위치7                      — INPUT_PULLUP          ★추가
 *  D7(PD7) | BLTN B+  전자식 릴레이                — OUTPUT (Low=ON)
 *  D8(PB0) | 택트스위치6  (PLBM 모사)              — INPUT_PULLUP
 *  D9(PB1) | CCU & HU B+ 전자식 릴레이             — OUTPUT (Low=ON)
 * D10(PB2) | ACC 전자식 릴레이                     — OUTPUT (Low=ON)
 * D11(PB3) | IGN 전자식 릴레이                     — OUTPUT (Low=ON)
 * D12(PB4) | PLBM 전자식 릴레이 (실물)             — OUTPUT (Low=ON)
 * D13(PB5) | PLBM 전자식 릴레이 (모사)             — OUTPUT (Low=ON)
 *
 * ⚠ D0(RX), D1(TX) — UART 점유, 배선 연결 금지
 *
 * ── UART 프로토콜 (9600 bps) ─────────────────────────────────────────────
 * PC → Arduino:
 *   '1'/'Q'   BLTN B+         ON/OFF
 *   '2'/'W'   CCU & HU B+    ON/OFF
 *   '3'/'E'   ACC             ON/OFF
 *   '4'/'R'   IGN             ON/OFF
 *   '5'/'T'   PLBM 실물      ON/OFF
 *   '6'/'Y'   PLBM 모사      ON/OFF
 *   '0'       ALL OFF
 *   'Z'       상태 쿼리
 *   "A<ms>\n" OHCL 릴레이 직접 트리거 — ms만큼 A1 CLOSE (100~9999ms)  ★v10.1추가
 *
 * Arduino → PC:
 *   "S<hex2>\n"   릴레이 상태 비트맵
 *     bit7:BLTN_B+  bit6:CCU_HU  bit5:ACC  bit4:IGN
 *     bit3:PLBM_REAL(D12)  bit2:PLBM_SIM(D13)  bit1:OHCL_RELAY(A1)  bit0:rsvd
 *   "SW<ch>\n"    택트스위치 이벤트 (ch=1~6)
 *   "M\n"         OHCL 더블클릭 → A1 릴레이 1350ms CLOSE 완료
 *   "N\n"         OHCL 싱글클릭 → A1 릴레이 1250ms CLOSE 완료
 *   "K<ms>\n"     PC 트리거("A<ms>\n") → A1 릴레이 CLOSE 완료 보고  ★v10.1추가
 *   "L1\n"        BLTN LED PWM 감지 시작 (A2 엣지 검출)
 *   "L0\n"        BLTN LED PWM 소멸 (200ms 이상 엣지 없음)
 *   "P<ms>\n"     LED 점멸 반주기 ms (엣지 간격, 10진수)
 */

#ifndef F_CPU
#define F_CPU 16000000UL
#endif

#include <avr/io.h>
#include <avr/interrupt.h>
#include <avr/eeprom.h>
#include <util/delay.h>

// ══ 핀 정의 ══════════════════════════════════════════════════════════════════
#define PIN_RELAY_BLTN_B    PD7
#define PIN_RELAY_CCU_HU    PB1
#define PIN_RELAY_ACC       PB2
#define PIN_RELAY_IGN       PB3
#define PIN_RELAY_PLBM_REAL PB4   // PLBM 실물 릴레이 (D12)
#define PIN_RELAY_PLBM_SIM  PB5   // PLBM 모사 릴레이 (D13)
#define PIN_RELAY_OHCL      PC1   // OHCL 릴레이 (A1) ★추가

// OHCL 타이밍 상수 (ms)
#define OHCL_SINGLE_MS      1250  // 싱글클릭 릴레이 CLOSE 시간
#define OHCL_DOUBLE_MS      1350  // 더블클릭 릴레이 CLOSE 시간
#define OHCL_DCLICK_WIN_MS  350   // 더블클릭 판정 윈도우
#define OHCL_DEBOUNCE_MS    20    // 디바운스

// LED PWM 감지 상수
#define LED_EDGE_THRESHOLD  512   // analogRead 기준 (0~1023 중간)
#define LED_TIMEOUT_MS      200   // 이 시간 엣지 없으면 L0

// ══ EEPROM 주소 ══════════════════════════════════════════════════════════════
#define EE_MAGIC    0x00
#define EE_STATE    0x01
#define MAGIC_VAL   0xA5

// ══ 전역 상태 ════════════════════════════════════════════════════════════════
// bit7:BLTN_B+ bit6:CCU_HU bit5:ACC bit4:IGN
// bit3:PLBM_REAL bit2:PLBM_SIM bit1:OHCL_RELAY bit0:rsvd
volatile uint8_t  g_state      = 0;
volatile uint8_t  g_tx_request = 0;

// ── OHCL 스위치 상태 머신 ──────────────────────────────────────────────────
// 메인 루프에서만 접근 (ISR 없음) → volatile 불필요
static uint8_t  ohcl_sw_last      = 1;    // 이전 스위치 레벨
static uint8_t  ohcl_click_count  = 0;    // 현재 클릭 횟수 (1 or 2)
static uint16_t ohcl_click_timer  = 0;    // 더블클릭 윈도우 카운터 (×10ms)
static uint8_t  ohcl_relay_busy   = 0;    // 릴레이 동작 중 플래그

// ── PC→Arduino OHCL 직접 트리거 수신 버퍼 (v10.2) ────────────────────────
// ISR 레벨 소형 버퍼 — 'A' 수신 후 후속 바이트를 switch/case 없이 버퍼에 쌓음.
// 기존 '1'~'0' 명령으로 오처리되는 버그를 근원적으로 차단.
#define OHCL_BUF_SIZE  8
volatile uint8_t  g_rx_ohcl_collecting = 0;           // 'A' 이후 수집 중
volatile char     g_rx_ohcl_buf[OHCL_BUF_SIZE];       // 수신 버퍼
volatile uint8_t  g_rx_ohcl_buf_len    = 0;           // 버퍼 길이
volatile uint8_t  g_rx_ohcl_ready      = 0;           // 파싱 준비 완료 플래그

// ── LED PWM 감지 상태 ─────────────────────────────────────────────────────
static uint8_t  led_last_level    = 0;    // 0=LOW, 1=HIGH
static uint16_t led_edge_timer    = 0;    // 마지막 엣지 이후 카운터 (×10ms)
static uint16_t led_half_period   = 0;    // 직전 반주기 (ms)
static uint8_t  led_on_state      = 0;    // PC에 보고된 LED 상태 (0=L0, 1=L1)

// ══ EEPROM 저장/복원 ══════════════════════════════════════════════════════════
static void eeprom_save_state(void) {
    // bit1(OHCL_RELAY)은 일시적 동작이므로 저장 제외 — 0xFC 마스크 유지
    uint8_t relay_bits = g_state & 0xFC;
    if (eeprom_read_byte((uint8_t*)EE_MAGIC) != MAGIC_VAL ||
        eeprom_read_byte((uint8_t*)EE_STATE) != relay_bits) {
        eeprom_update_byte((uint8_t*)EE_MAGIC, MAGIC_VAL);
        eeprom_update_byte((uint8_t*)EE_STATE, relay_bits);
    }
}

static uint8_t eeprom_load_state(void) {
    if (eeprom_read_byte((uint8_t*)EE_MAGIC) != MAGIC_VAL)
        return 0;
    uint8_t saved = eeprom_read_byte((uint8_t*)EE_STATE);
    g_state = (g_state & 0x03) | (saved & 0xFC);
    return 1;
}

// ══ UART 유틸 ════════════════════════════════════════════════════════════════
static void uart_putchar(char c) {
    while (!(UCSR0A & (1 << UDRE0)));
    UDR0 = c;
}

static void uart_puts(const char *s) {
    while (*s) uart_putchar(*s++);
}

static void uart_send_state(void) {
    char buf[5];
    buf[0] = 'S';
    buf[1] = "0123456789ABCDEF"[(g_state >> 4) & 0x0F];
    buf[2] = "0123456789ABCDEF"[ g_state       & 0x0F];
    buf[3] = '\n';
    buf[4] = '\0';
    uart_puts(buf);
}

static void uart_send_sw(char ch) {
    uart_putchar('S');
    uart_putchar('W');
    uart_putchar(ch);
    uart_putchar('\n');
}

// LED 반주기 전송: "P<십진수ms>\n"
static void uart_send_period(uint16_t ms) {
    // 최대 65535 → 최대 5자리
    char buf[10];
    buf[0] = 'P';
    uint8_t i = 1;
    if (ms == 0) {
        buf[i++] = '0';
    } else {
        // 십진 변환 (역순 후 뒤집기)
        char tmp[6]; uint8_t ti = 0;
        uint16_t v = ms;
        while (v > 0) { tmp[ti++] = '0' + (v % 10); v /= 10; }
        while (ti > 0) { buf[i++] = tmp[--ti]; }
    }
    buf[i++] = '\n';
    buf[i]   = '\0';
    uart_puts(buf);
}

// PC 트리거 완료 보고: "K<십진수ms>\n"
static void uart_send_k(uint16_t ms) {
    char buf[10];
    buf[0] = 'K';
    uint8_t i = 1;
    if (ms == 0) {
        buf[i++] = '0';
    } else {
        char tmp[6]; uint8_t ti = 0;
        uint16_t v = ms;
        while (v > 0) { tmp[ti++] = '0' + (v % 10); v /= 10; }
        while (ti > 0) { buf[i++] = tmp[--ti]; }
    }
    buf[i++] = '\n';
    buf[i]   = '\0';
    uart_puts(buf);
}

// ══ OHCL 릴레이 제어 ═════════════════════════════════════════════════════════
// 지정 시간(ms)만큼 A1(PC1) 릴레이 CLOSE 후 자동 OPEN.
// _delay_ms는 컴파일 타임 상수만 허용 → 10ms 단위 루프로 구현.
// ※ 릴레이 동작 중에는 메인 루프 전체가 블로킹됨 → 최대 1350ms.
//   이 시간 동안 LED PWM 샘플링은 중단되지만,
//   UART RX 인터럽트는 계속 동작하므로 PC 명령 수신은 유지.
static void ohcl_relay_pulse(uint16_t duration_ms, char report_char) {
    // 1) 릴레이 ON (Active-Low)
    PORTC &= ~(1 << PIN_RELAY_OHCL);
    g_state |= (1 << 1);   // bit1: OHCL_RELAY ON
    uart_send_state();

    // 2) duration_ms 동안 대기 (10ms 단위)
    uint16_t loops = duration_ms / 10;
    for (uint16_t j = 0; j < loops; j++) {
        _delay_ms(10);
    }
    // 나머지 ms 보정 (정밀도 향상)
    uint8_t rem = (uint8_t)(duration_ms % 10);
    if (rem >= 5) _delay_ms(5);   // 5ms 이상이면 5ms 추가

    // 3) 릴레이 OFF
    PORTC |=  (1 << PIN_RELAY_OHCL);
    g_state &= ~(1 << 1);
    uart_send_state();

    // 4) 완료 보고 ('M' or 'N', 0이면 생략 — PC 트리거 시 K로 별도 보고)
    if (report_char != 0) {
        uart_putchar(report_char);
        uart_putchar('\n');
    }
}

// ══ 하드웨어 동기화 ══════════════════════════════════════════════════════════
// PLBM 상호 배타: PLBM_REAL(bit3)과 PLBM_SIM(bit2) 동시 ON 불가.
static void update_hardware(void) {
    // PLBM 상호 배타 강제
    if ((g_state & (1 << 3)) && (g_state & (1 << 2))) {
        g_state &= ~(1 << 2);   // 동시 ON → PLBM_SIM 강제 OFF
    }

    // D7: BLTN B+
    if (g_state & (1 << 7)) PORTD &= ~(1 << PIN_RELAY_BLTN_B);
    else                     PORTD |=  (1 << PIN_RELAY_BLTN_B);

    // PB1~PB5
    static const uint8_t SB[5]  = {6, 5, 4, 3, 2};
    static const uint8_t PB_[5] = {1, 2, 3, 4, 5};
    for (uint8_t i = 0; i < 5; i++) {
        if (g_state & (1 << SB[i])) PORTB &= ~(1 << PB_[i]);
        else                         PORTB |=  (1 << PB_[i]);
    }

    // A1(PC1): OHCL 릴레이 — bit1 반영
    // (ohcl_relay_pulse가 직접 제어하므로 여기서는 동기만)
    if (g_state & (1 << 1)) PORTC &= ~(1 << PIN_RELAY_OHCL);
    else                     PORTC |=  (1 << PIN_RELAY_OHCL);
}

// ══ 실제 핀 → g_state 동기화 ════════════════════════════════════════════════
static void sync_state_from_hw(void) {
    uint8_t s = 0;
    if (!(PORTD & (1 << PD7))) s |= (1 << 7);
    if (!(PORTB & (1 << PB1))) s |= (1 << 6);
    if (!(PORTB & (1 << PB2))) s |= (1 << 5);
    if (!(PORTB & (1 << PB3))) s |= (1 << 4);
    if (!(PORTB & (1 << PB4))) s |= (1 << 3);  // PLBM_REAL
    if (!(PORTB & (1 << PB5))) s |= (1 << 2);  // PLBM_SIM
    if (!(PORTC & (1 << PC1))) s |= (1 << 1);  // OHCL 릴레이 ★추가
    g_state = s;
}

// ══ UART RX 인터럽트 ═════════════════════════════════════════════════════════
ISR(USART_RX_vect) {
    char c = UDR0;

    // ── OHCL 버퍼 수집 중이면 switch/case 진입 없이 버퍼에만 쌓음 ──────
    if (g_rx_ohcl_collecting) {
        if (c == '\n' || c == '\r') {
            // 라인 끝 → 파싱 준비 완료
            g_rx_ohcl_collecting = 0;
            g_rx_ohcl_ready      = 1;
        } else if (g_rx_ohcl_buf_len < OHCL_BUF_SIZE - 1) {
            g_rx_ohcl_buf[g_rx_ohcl_buf_len++] = c;
            g_rx_ohcl_buf[g_rx_ohcl_buf_len]   = '\0';
        }
        return;   // ★ 전원 명령 switch/case 진입 완전 차단
    }

    switch (c) {
        case '1': g_state |=  (1 << 7); break;
        case 'Q': g_state &= ~(1 << 7); break;
        case '2': g_state |=  (1 << 6); break;
        case 'W': g_state &= ~(1 << 6); break;
        case '3': g_state |=  (1 << 5); break;
        case 'E': g_state &= ~(1 << 5); break;
        case '4': g_state |=  (1 << 4); break;
        case 'R': g_state &= ~(1 << 4); break;
        case '5':
            g_state &= ~(1 << 2);   // PLBM_SIM 강제 OFF (배타)
            g_state |=  (1 << 3);
            break;
        case 'T':
            g_state &= ~(1 << 3);
            break;
        case '6':
            g_state &= ~(1 << 3);   // PLBM_REAL 강제 OFF (배타)
            g_state |=  (1 << 2);
            break;
        case 'Y':
            g_state &= ~(1 << 2);
            break;
        case '0':
            g_state = 0;
            break;
        case 'Z':
            sync_state_from_hw();
            g_tx_request = 1;
            return;
        case 'A':
            // PC→Arduino OHCL 직접 트리거: 'A' 수신 → ISR 버퍼 수집 시작
            // 이후 바이트("1250\n" 등)는 switch/case 없이 버퍼에 쌓임.
            g_rx_ohcl_collecting = 1;
            g_rx_ohcl_buf_len    = 0;
            g_rx_ohcl_buf[0]     = '\0';
            g_rx_ohcl_ready      = 0;
            return;   // update_hardware/eeprom_save/g_tx_request 생략
        default:
            break;
    }
    update_hardware();
    eeprom_save_state();
    g_tx_request = 1;
}

// ══ ADC 초기화 및 단일 변환 ══════════════════════════════════════════════════
static void adc_init(void) {
    // AVCC 기준, ADC2(PC2) 선택
    ADMUX  = (1 << REFS0) | 0x02;   // REFS0=AVCC, MUX=ADC2
    // ADC 인에이블, 분주비 128 (16MHz/128=125kHz)
    ADCSRA = (1 << ADEN) | (1 << ADPS2) | (1 << ADPS1) | (1 << ADPS0);
}

static uint16_t adc_read(void) {
    ADCSRA |= (1 << ADSC);                   // 변환 시작
    while (ADCSRA & (1 << ADSC));            // 완료 대기 (~104µs)
    return ADC;                              // ADCL/ADCH 자동 결합
}

// ══ 스위치 레벨 읽기 ═════════════════════════════════════════════════════════
static uint8_t read_sw(uint8_t idx) {
    switch (idx) {
        case 0: return (PINC & (1 << PC0)) ? 1 : 0;  // SW1: A0  (BLTN B+)
        case 1: return (PIND & (1 << PD2)) ? 1 : 0;  // SW2: D2  (CCU & HU B+)
        case 2: return (PIND & (1 << PD3)) ? 1 : 0;  // SW3: D3  (ACC)
        case 3: return (PIND & (1 << PD4)) ? 1 : 0;  // SW4: D4  (IGN)
        case 4: return (PIND & (1 << PD5)) ? 1 : 0;  // SW5: D5  (PLBM 실물)
        case 5: return (PINB & (1 << PB0)) ? 1 : 0;  // SW6: D8(PB0) (PLBM 모사)
        default: return 1;
    }
}

// ── OHCL 스위치(D6/PD6) 읽기 ─────────────────────────────────────────────
static uint8_t read_ohcl_sw(void) {
    return (PIND & (1 << PD6)) ? 1 : 0;
}

// ══ PC→Arduino OHCL 직접 트리거 처리 (ISR 버퍼 방식, v10.2) ════════════════
// ISR이 'A' 수신 후 후속 바이트를 g_rx_ohcl_buf[]에 쌓고 g_rx_ohcl_ready=1.
// 메인루프에서 ready 플래그 확인 → 버퍼에서 ms 파싱 → 릴레이 구동.
// 기존 폴링 방식의 전원 명령 오처리 버그를 완전히 제거.
static void process_pc_ohcl_trigger(void) {
    if (!g_rx_ohcl_ready) return;

    // 원자적으로 플래그 클리어 + 버퍼 복사
    cli();   // 인터럽트 잠시 비활성화
    g_rx_ohcl_ready = 0;
    char local_buf[OHCL_BUF_SIZE];
    uint8_t local_len = g_rx_ohcl_buf_len;
    for (uint8_t i = 0; i <= local_len && i < OHCL_BUF_SIZE; i++) {
        local_buf[i] = (char)g_rx_ohcl_buf[i];
    }
    sei();   // 인터럽트 재활성화

    if (ohcl_relay_busy) return;   // 릴레이 동작 중 무시

    if (local_len == 0) return;

    // 십진수 파싱
    uint16_t ms = 0;
    for (uint8_t i = 0; i < local_len; i++) {
        char c = local_buf[i];
        if (c < '0' || c > '9') return;   // 숫자 이외 → 잘못된 명령
        ms = ms * 10 + (c - '0');
    }

    // 범위 검사 (100ms ~ 9999ms)
    if (ms < 100 || ms > 9999) return;

    // 릴레이 펄스 실행
    ohcl_relay_busy = 1;
    ohcl_relay_pulse(ms, 0);   // report_char=0 → M/N 대신 K로 별도 보고
    uart_send_k(ms);            // "K<ms>\n" 완료 보고
    ohcl_relay_busy = 0;
}

// ══ LED PWM 감지 처리 (10ms마다 호출) ═══════════════════════════════════════
// A2(PC2)를 analogRead → LED_EDGE_THRESHOLD 기준으로 Hi/Lo 판단 → 엣지 검출
static void process_led_pwm(void) {
    uint16_t adc_val   = adc_read();
    uint8_t  cur_level = (adc_val >= LED_EDGE_THRESHOLD) ? 1 : 0;

    if (cur_level != led_last_level) {
        // ── 엣지 발생 ────────────────────────────────────────────────────
        // 반주기 = 마지막 엣지 이후 경과 시간 (×10ms)
        uint16_t period_ms = led_edge_timer * 10;
        led_half_period = period_ms;
        led_edge_timer  = 0;
        led_last_level  = cur_level;

        // LED ON 보고 (아직 OFF 상태였을 때만)
        if (!led_on_state) {
            led_on_state = 1;
            uart_puts("L1\n");
        }
        // 반주기 전송 (최소 10ms 이상일 때만 — 노이즈 필터)
        if (period_ms >= 10) {
            uart_send_period(period_ms);
        }
    } else {
        // ── 엣지 없음 ────────────────────────────────────────────────────
        // overflow 방지: 최대 카운트 고정
        if (led_edge_timer < 0xFFFF) {
            led_edge_timer++;
        }
        // 타임아웃 → LED OFF 판정
        if (led_on_state && (led_edge_timer * 10 >= LED_TIMEOUT_MS)) {
            led_on_state   = 0;
            led_half_period = 0;
            uart_puts("L0\n");
        }
    }
}

// ══ OHCL 스위치 처리 (10ms마다 호출) ════════════════════════════════════════
// 더블클릭 윈도우(OHCL_DCLICK_WIN_MS) 내 2회 클릭 → 더블클릭
// 윈도우 내 1회만 → 싱글클릭
// 릴레이 busy 중에는 입력 무시 (방어)
static void process_ohcl_sw(void) {
    uint8_t sw_now = read_ohcl_sw();

    // ── 더블클릭 윈도우 카운터 증가 ─────────────────────────────────────
    if (ohcl_click_count > 0) {
        if (ohcl_click_timer < 0xFFFF) ohcl_click_timer++;

        uint16_t elapsed_ms = ohcl_click_timer * 10;

        if (elapsed_ms >= OHCL_DCLICK_WIN_MS) {
            // 윈도우 만료 → 클릭 횟수 확정
            if (!ohcl_relay_busy) {
                if (ohcl_click_count >= 2) {
                    // 더블클릭 → 1350ms CLOSE
                    ohcl_relay_busy = 1;
                    ohcl_relay_pulse(OHCL_DOUBLE_MS, 'M');
                    ohcl_relay_busy = 0;
                } else {
                    // 싱글클릭 → 1250ms CLOSE
                    ohcl_relay_busy = 1;
                    ohcl_relay_pulse(OHCL_SINGLE_MS, 'N');
                    ohcl_relay_busy = 0;
                }
            }
            // 상태 초기화
            ohcl_click_count = 0;
            ohcl_click_timer = 0;
        }
    }

    // ── 하강 엣지 감지 (1→0 누름) ───────────────────────────────────────
    if (sw_now == 0 && ohcl_sw_last == 1) {
        ohcl_sw_last = 0;
        _delay_ms(OHCL_DEBOUNCE_MS);   // 디바운스
        if (read_ohcl_sw() == 0) {
            // 릴레이 동작 중이면 무시
            if (!ohcl_relay_busy) {
                if (ohcl_click_count == 0) {
                    // 첫 번째 클릭 — 윈도우 시작
                    ohcl_click_count = 1;
                    ohcl_click_timer = 0;
                } else {
                    // 두 번째 클릭 — 더블클릭 확정 (윈도우 내 카운터 2)
                    ohcl_click_count = 2;
                }
            }
        }
    } else if (sw_now == 1 && ohcl_sw_last == 0) {
        ohcl_sw_last = 1;
    }
}

// ══ 메인 ════════════════════════════════════════════════════════════════════
int main(void) {

    // ── DDR / 풀업 설정 ──────────────────────────────────────────────────────
    // [v9.0 방식 유지] = 완전 덮어쓰기로 부트로더 잔재 무효화

    // DDRB: PB1~PB5 = OUTPUT(릴레이), PB0 = INPUT(SW6)
    DDRB = (1 << PB5) | (1 << PB4) | (1 << PB3) | (1 << PB2) | (1 << PB1);
    // PORTB 초기값: 릴레이 OFF(Active-Low → HIGH), PB0 풀업
    PORTB = (1 << PB5) | (1 << PB4) | (1 << PB3) | (1 << PB2) | (1 << PB1)
          | (1 << PB0);

    // DDRC: PC1 = OUTPUT(OHCL 릴레이), 나머지 = INPUT ★v10.0 변경
    //   PC0 = INPUT (SW1/A0)
    //   PC1 = OUTPUT (OHCL 릴레이/A1)   ← 변경점
    //   PC2 = INPUT (LED PWM/A2)
    //   PC3~PC5 = INPUT (미사용)
    DDRC  = (1 << PC1);
    // PORTC 초기값: PC1 릴레이 OFF (HIGH), 나머지 INPUT_PULLUP
    PORTC = (1 << PC5) | (1 << PC4) | (1 << PC3) | (1 << PC2)
          | (1 << PC1)   // PC1 HIGH = 릴레이 OFF (Active-Low)
          | (1 << PC0);

    // DDRD: PD7 = OUTPUT(릴레이), PD0~PD6 = INPUT
    DDRD  = (1 << PD7);
    // PORTD: PD7 릴레이 OFF(HIGH), PD2~PD6 풀업(SW2~SW5, OHCL SW), PD0/PD1 풀업 없음(UART)
    PORTD = (1 << PD7)
          | (1 << PD6) | (1 << PD5) | (1 << PD4) | (1 << PD3) | (1 << PD2);

    // ── ADC 초기화 (LED PWM 입력 A2/PC2) ────────────────────────────────
    adc_init();

    // ── EEPROM 복원 → 즉시 하드웨어 적용 ────────────────────────────────
    if (eeprom_load_state()) {
        update_hardware();
    }

    // ── UART 9600bps @ 16MHz ─────────────────────────────────────────────
    UBRR0H = 0; UBRR0L = 103;
    UCSR0B = (1 << RXEN0) | (1 << TXEN0) | (1 << RXCIE0);
    UCSR0C = (3 << UCSZ00);

    sei();
    sync_state_from_hw();
    // ★ [v10.3] 부팅 시 상태 자동 보고 — 4회 반복 (PC 수신 준비 전 소실 방어 강화)
    // PC에서 시리얼 포트 오픈 후 _rx_loop 스레드가 올라오는 데 시간이 걸림.
    // v4.11부터 PC 측 reset_input_buffer() 제거로 버퍼 보존되지만,
    // 간격을 넓혀 어떤 타이밍에도 최소 1개 이상 수신되도록 보강.
    uart_send_state();          // 즉시 전송
    _delay_ms(200);
    uart_send_state();          // 200ms 후
    _delay_ms(300);
    uart_send_state();          // 500ms 후
    _delay_ms(700);
    uart_send_state();          // 1200ms 후 (DTR 리셋 후 부팅 완료 시점 커버)

    // SW 채널→비트 매핑 (SW1~SW6, OHCL 제외)
    static const uint8_t STATE_BIT[6] = {7, 6, 5, 4, 3, 2};

    uint8_t  sw_last[6] = {1, 1, 1, 1, 1, 1};
    uint16_t loop_cnt   = 0;

    while (1) {
        _delay_ms(10);
        loop_cnt++;

        // ── 기존 스위치 처리 (SW1~SW6) ──────────────────────────────────
        for (uint8_t i = 0; i < 6; i++) {
            uint8_t sw_now = read_sw(i);

            if (sw_now == 0 && sw_last[i] == 1) {
                sw_last[i] = 0;
                _delay_ms(20);   // 디바운스
                if (read_sw(i) == 0) {
                    uint8_t bit = STATE_BIT[i];
                    if (bit == 3) {             // PLBM_REAL 토글
                        if (g_state & (1 << 3)) {
                            g_state &= ~(1 << 3);
                        } else {
                            g_state &= ~(1 << 2);   // PLBM_SIM 강제 OFF
                            g_state |=  (1 << 3);
                        }
                    } else if (bit == 2) {      // PLBM_SIM 토글
                        if (g_state & (1 << 2)) {
                            g_state &= ~(1 << 2);
                        } else {
                            g_state &= ~(1 << 3);   // PLBM_REAL 강제 OFF
                            g_state |=  (1 << 2);
                        }
                    } else {
                        g_state ^= (1 << bit);  // 일반 토글
                    }
                    update_hardware();
                    eeprom_save_state();
                    uart_send_sw((char)('1' + i));
                    g_tx_request = 1;
                }
            } else if (sw_now == 1 && sw_last[i] == 0) {
                sw_last[i] = 1;
            }
        }

        // ── PC→Arduino OHCL 직접 트리거 처리 (v10.1) ───────────────────
        // ISR에서 'A' 플래그가 세팅되면 나머지 ms 폴링 수신 후 릴레이 구동
        process_pc_ohcl_trigger();

        // ── OHCL 스위치 처리 (D6/PD6) ───────────────────────────────────
        // ※ ohcl_relay_pulse() 내부에서 최대 1350ms 블로킹 발생.
        //   블로킹 중에는 SW1~SW6 처리와 LED PWM 샘플링이 중단됨.
        //   단, UART RX ISR은 계속 동작하므로 PC 명령 수실은 유지.
        process_ohcl_sw();

        // ── LED PWM 감지 처리 (A2/PC2) ──────────────────────────────────
        // ohcl_relay_pulse 블로킹 중에는 아래 코드에 도달하지 못함.
        // 릴레이 동작 완료 후 재개 — LED 타임아웃 카운터는 그 사이 멈춤.
        // (릴레이 동작 시간 최대 1350ms → 타임아웃 200ms 초과 가능)
        // → 릴레이 동작 직후 led_edge_timer 리셋하여 오검출 방지
        if (ohcl_relay_busy == 0) {
            process_led_pwm();
        }

        // ── 상태 전송 ────────────────────────────────────────────────────
        if (g_tx_request) {
            g_tx_request = 0;
            uart_send_state();
        }

        // ── 주기 보고 (2초마다) ──────────────────────────────────────────
        if (loop_cnt >= 200) {
            loop_cnt = 0;
            uart_send_state();
        }
    }
}
