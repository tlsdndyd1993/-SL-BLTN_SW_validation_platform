/*
 * Screen & Camera Recorder — 전원 제어 Arduino 펌웨어 v6.0 (핀맵 B 타입)
 * =========================================================
 * [v6.0 수정사항]
 *   · [근본 원인 수정] A2(PC2) 플로팅 핀 → 수동녹화 트리거 오감지 문제
 *             원인1: A2에 아무것도 연결 안 된 상태에서 내부 풀업 미활성화
 *                    → 플로팅 핀이 노이즈로 0/1 진동
 *                    → LED 변화 감지 → uart_send_led + uart_send_state 연속 전송
 *                    → Python 수신로그에 스위치/LED 이벤트 폭주 + RX LED 깜빡임
 *             원인2: measure_led_pwm()이 100샘플×50µs = 5ms 블로킹
 *                    → 루프 점유로 다른 처리 지연
 *     수정1: A2(PC2) 내부 풀업 활성화 (미연결 시 안정적 HIGH 고정)
 *     수정2: 블로킹 measure_led_pwm() 제거
 *            → 비블로킹 led_raw_sample() 교체
 *               매 루프(10ms)마다 1샘플, 5회(50ms) 연속 같은 값일 때만 확정
 *               → 플로팅 노이즈로 인한 오감지 완전 차단
 *   · v5.0의 A1(OHCL), PLBM 상호 배타, EEPROM, DTR 방어 유지
 *
 * ── 핀맵 (v5.0 핀맵 B) ──────────────────────────────────────────────────
 *  A0(PC0) | 택트스위치1  (BLTN B+)               — INPUT_PULLUP
 *  A1(PC1) | 택트스위치7  (OHCL)                  — INPUT_PULLUP ★추가
 *  A2(PC2) | BLTN OHCL LED PWM 신호 입력           — INPUT        ★추가
 *  D2(PD2) | 택트스위치2  (BLTN CCU & HU B+)      — INPUT_PULLUP
 *  D3(PD3) | 택트스위치3  (ACC)                    — INPUT_PULLUP
 *  D4(PD4) | 택트스위치4  (IGN)                    — INPUT_PULLUP
 *  D5(PD5) | 택트스위치5  (PLBM 실물)              — INPUT_PULLUP
 *  D6(PD6) | 택트스위치6  (PLBM 모사)              — INPUT_PULLUP
 *  D7(PD7) | BLTN B+  전자식 릴레이               — OUTPUT (Low=ON)
 *  D8(PB0) | BLTN OHCL LED OUT                    — OUTPUT (기존 유지)
 *  D9(PB1) | CCU & HU B+ 전자식 릴레이            — OUTPUT (Low=ON)
 * D10(PB2) | ACC 전자식 릴레이                    — OUTPUT (Low=ON)
 * D11(PB3) | IGN 전자식 릴레이                    — OUTPUT (Low=ON)
 * D12(PB4) | PLBM 전자식 릴레이 (실물)            — OUTPUT (Low=ON)
 * D13(PB5) | PLBM 전자식 릴레이 (모사)            — OUTPUT (Low=ON)
 *
 * ⚠ D0(RX), D1(TX) — UART 점유, 배선 연결 금지
 *
 * ── EEPROM 레이아웃 ──────────────────────────────────────────────────────
 *  addr 0x00 : 매직 바이트 (0xA5)
 *  addr 0x01 : 릴레이 상태 비트맵 (g_state, bit7~bit2)
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
 *
 * Arduino → PC:
 *   "S<hex2>\n"   릴레이 상태 비트맵
 *     bit7:BLTN_B+  bit6:CCU_HU  bit5:ACC  bit4:IGN
 *     bit3:PLBM_REAL(D12)  bit2:PLBM_SIM(D13)  bit1:LED  bit0:rsvd
 *   "L1\n"/"L0\n"   BLTN OHCL LED PWM ON/OFF (A2 입력 기반)
 *   "SW<ch>\n"       택트스위치 이벤트 (ch=1~6: 기존, ch=7: OHCL단타, ch=8: OHCL장누름)
 *   "M\n"            OHCL 단타 → 수동녹화 트리거 신호
 *   "N\n"            OHCL 장누름 → 타임랩스 트리거 신호
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
#define PIN_LED_OUT         PB0   // D8: BLTN OHCL LED OUT (출력 유지)

// ★ A1(PC1): OHCL 택트스위치 입력 (외부 Pull-up 반영 — INPUT, 눌리면 LOW)
#define PIN_OHCL_SW         PC1
// ★ A2(PC2): BLTN OHCL LED PWM 신호 입력 (아날로그 포트를 디지털로 읽음)
#define PIN_OHCL_LED_PWM    PC2

// OHCL 장/단누름 판별 임계 (ms)
#define OHCL_LONG_PRESS_MS  1300U

// ══ EEPROM 주소 ══════════════════════════════════════════════════════════════
#define EE_MAGIC    0x00
#define EE_STATE    0x01
#define MAGIC_VAL   0xA5

// ══ 전역 상태 ════════════════════════════════════════════════════════════════
// bit7:BLTN_B+ bit6:CCU_HU bit5:ACC bit4:IGN
// bit3:PLBM_REAL bit2:PLBM_SIM bit1:LED bit0:rsvd
volatile uint8_t  g_state      = 0;
volatile uint8_t  g_led_on     = 0;
volatile uint8_t  g_tx_request = 0;

// ══ EEPROM 저장/복원 ══════════════════════════════════════════════════════════
static void eeprom_save_state(void) {
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

static void uart_send_led(uint8_t on) {
    uart_putchar('L');
    uart_putchar(on ? '1' : '0');
    uart_putchar('\n');
}

static void uart_send_sw(char ch) {
    uart_putchar('S');
    uart_putchar('W');
    uart_putchar(ch);
    uart_putchar('\n');
}

// OHCL 트리거 패킷: 'M'=수동녹화, 'N'=타임랩스
static void uart_send_ohcl_trigger(char type) {
    uart_putchar(type);  // 'M' or 'N'
    uart_putchar('\n');
}

// ══ 하드웨어 동기화 ══════════════════════════════════════════════════════════
// g_state → 실제 릴레이 핀 출력 (Active-Low)
// [수정3] PLBM 상호 배타 하드웨어 강제:
//   PLBM_REAL(bit3)과 PLBM_SIM(bit2)은 동시에 1이 될 수 없음.
//   g_state에 두 비트가 동시에 세트되는 경우 PLBM_REAL 우선으로 처리.
static void update_hardware(void) {
    // PLBM 상호 배타 강제 (소프트웨어 2중 보호)
    if ((g_state & (1 << 3)) && (g_state & (1 << 2))) {
        g_state &= ~(1 << 2);   // 동시 ON → PLBM_SIM 강제 OFF
    }

    // D7: BLTN B+
    if (g_state & (1 << 7)) PORTD &= ~(1 << PIN_RELAY_BLTN_B);
    else                     PORTD |=  (1 << PIN_RELAY_BLTN_B);

    // PB1~PB5 (CCU_HU, ACC, IGN, PLBM_REAL, PLBM_SIM)
    // g_state bit6→PB1, bit5→PB2, bit4→PB3, bit3→PB4(PLBM_REAL), bit2→PB5(PLBM_SIM)
    static const uint8_t SB[5]  = {6, 5, 4, 3, 2};
    static const uint8_t PB_[5] = {1, 2, 3, 4, 5};
    for (uint8_t i = 0; i < 5; i++) {
        if (g_state & (1 << SB[i])) PORTB &= ~(1 << PB_[i]);
        else                         PORTB |=  (1 << PB_[i]);
    }
}

// ══ 실제 핀 → g_state 동기화 ════════════════════════════════════════════════
static void sync_state_from_hw(void) {
    uint8_t s = g_state & 0x03;   // bit1(LED), bit0(rsvd) 보존

    if (!(PORTD & (1 << PD7))) s |= (1 << 7); else s &= ~(1 << 7);
    if (!(PORTB & (1 << PB1))) s |= (1 << 6); else s &= ~(1 << 6);
    if (!(PORTB & (1 << PB2))) s |= (1 << 5); else s &= ~(1 << 5);
    if (!(PORTB & (1 << PB3))) s |= (1 << 4); else s &= ~(1 << 4);
    if (!(PORTB & (1 << PB4))) s |= (1 << 3); else s &= ~(1 << 3);  // PLBM_REAL
    if (!(PORTB & (1 << PB5))) s |= (1 << 2); else s &= ~(1 << 2);  // PLBM_SIM

    g_state = s;
}

// ══ UART RX 인터럽트 ═════════════════════════════════════════════════════════
ISR(USART_RX_vect) {
    char c = UDR0;

    switch (c) {
        case '1': g_state |=  (1 << 7); break;
        case 'Q': g_state &= ~(1 << 7); break;
        case '2': g_state |=  (1 << 6); break;
        case 'W': g_state &= ~(1 << 6); break;
        case '3': g_state |=  (1 << 5); break;
        case 'E': g_state &= ~(1 << 5); break;
        case '4': g_state |=  (1 << 4); break;
        case 'R': g_state &= ~(1 << 4); break;
        // ★ PLBM 실물 ON/OFF (D12 릴레이)
        case '5':
            g_state &= ~(1 << 2);   // PLBM_SIM 강제 OFF (배타)
            g_state |=  (1 << 3);
            break;
        case 'T':
            g_state &= ~(1 << 3);
            break;
        // ★ PLBM 모사 ON/OFF (D13 릴레이) — 기존 '6' 시간제한 OHCL 커맨드 대체
        case '6':
            g_state &= ~(1 << 3);   // PLBM_REAL 강제 OFF (배타)
            g_state |=  (1 << 2);
            break;
        case 'Y':
            g_state &= ~(1 << 2);
            break;
        case '0':
            g_state  = 0;
            break;
        case 'Z':
            sync_state_from_hw();
            g_tx_request = 1;
            return;
        default:
            break;
    }

    update_hardware();
    eeprom_save_state();
    g_tx_request = 1;
}

// ══ LED PWM 측정 (A2/PC2 입력 기반) ════════════════════════════════════════
// [v6.0 수정] 블로킹 방식(100샘플 × 50µs = 5ms 점유) 제거.
// 대신 단순 디지털 읽기 + 히스테리시스(연속 N회 확인) 방식으로 변경.
// → 루프 블로킹 없음, 플로팅 노이즈로 인한 오감지 방지.
//
// 호출 방법: 10ms 루프마다 led_raw_sample()로 샘플 1회 추가.
//            LED_CONFIRM_CNT 회 연속 같은 값이면 확정.
#define LED_CONFIRM_CNT  5   // 50ms 연속 같은 값 → 확정

static uint8_t led_sample_buf  = 0;    // 최근 샘플 비트맵 (LSB=최신)
static uint8_t led_confirmed   = 0;    // 현재 확정된 LED 상태

// 매 루프(10ms)마다 호출. 확정 변화 시 1 반환, 아니면 0.
static uint8_t led_raw_sample(uint8_t *confirmed_out) {
    uint8_t raw = (PINC & (1 << PIN_OHCL_LED_PWM)) ? 1 : 0;

    // 최근 LED_CONFIRM_CNT 샘플을 비트 시프트로 관리
    led_sample_buf = ((led_sample_buf << 1) | raw);

    // 하위 LED_CONFIRM_CNT 비트가 모두 1 → ON 확정
    uint8_t mask = (1 << LED_CONFIRM_CNT) - 1;
    uint8_t all_hi = ((led_sample_buf & mask) == mask);
    uint8_t all_lo = ((led_sample_buf & mask) == 0);

    uint8_t new_state = led_confirmed;
    if (all_hi) new_state = 1;
    if (all_lo) new_state = 0;

    if (new_state != led_confirmed) {
        led_confirmed  = new_state;
        *confirmed_out = new_state;
        return 1;   // 변화 감지
    }
    return 0;
}

// ══ 스위치 레벨 읽기 ═════════════════════════════════════════════════════════
static uint8_t read_sw(uint8_t idx) {
    switch (idx) {
        case 0: return (PINC & (1 << PC0)) ? 1 : 0;  // SW1: A0
        case 1: return (PIND & (1 << PD2)) ? 1 : 0;  // SW2: D2
        case 2: return (PIND & (1 << PD3)) ? 1 : 0;  // SW3: D3 (ACC)
        case 3: return (PIND & (1 << PD4)) ? 1 : 0;  // SW4: D4 (IGN)
        case 4: return (PIND & (1 << PD5)) ? 1 : 0;  // SW5: D5 (PLBM 실물) ★
        case 5: return (PIND & (1 << PD6)) ? 1 : 0;  // SW6: D6 (PLBM 모사) ★
        default: return 1;
    }
}

// ══ 메인 ════════════════════════════════════════════════════════════════════
int main(void) {

    // ── DDR 설정 ─────────────────────────────────────────────────────────────
    DDRD |= (1 << PD7);
    DDRB |= (1 << PB1) | (1 << PB2) | (1 << PB3) | (1 << PB4) | (1 << PB5);

    // D8: BLTN OHCL LED OUT — 출력 유지 (기존)
    DDRB  |=  (1 << PIN_LED_OUT);
    PORTB &= ~(1 << PIN_LED_OUT);   // 초기 LOW

    // SW1: A0(PC0) 내부 풀업
    DDRC  &= ~(1 << PC0);
    PORTC |=  (1 << PC0);

    // ★ A1(PC1): OHCL 택트스위치 — INPUT, 풀업 없음 (외부 Pull-up 반영)
    DDRC  &= ~(1 << PIN_OHCL_SW);
    PORTC &= ~(1 << PIN_OHCL_SW);

    // ★ A2(PC2): BLTN OHCL LED PWM 신호 입력
    // [v6.0 수정] 아무것도 연결 안 됐을 때 플로팅 → 내부 풀업 활성화
    // (실제 LED PWM 신호가 들어오면 풀업보다 강하므로 측정에 영향 없음)
    DDRC  &= ~(1 << PIN_OHCL_LED_PWM);
    PORTC |=  (1 << PIN_OHCL_LED_PWM);  // 내부 풀업 ON

    // SW2~SW6: D2~D6 내부 풀업 (D5=PLBM실물, D6=PLBM모사)
    DDRD  &= ~((1 << PD2) | (1 << PD3) | (1 << PD4) | (1 << PD5) | (1 << PD6));
    PORTD |=   (1 << PD2) | (1 << PD3) | (1 << PD4) | (1 << PD5) | (1 << PD6);

    // ★ EEPROM 복원 → 즉시 하드웨어 적용 (DTR 리셋 방어)
    if (eeprom_load_state()) {
        update_hardware();
    } else {
        // 최초 부팅: 전체 OFF (Active-Low → HIGH)
        PORTD |= (1 << PD7);
        PORTB |= (1 << PB1) | (1 << PB2) | (1 << PB3) | (1 << PB4) | (1 << PB5);
    }

    // ── UART 9600bps @ 16MHz ──────────────────────────────────────────────
    UBRR0H = 0; UBRR0L = 103;
    UCSR0B = (1 << RXEN0) | (1 << TXEN0) | (1 << RXCIE0);
    UCSR0C = (3 << UCSZ00);

    sei();
    sync_state_from_hw();
    uart_send_state();   // 부팅 시 PC에 현재 상태 자동 보고

    // SW 채널→비트 매핑 (SW1~SW6)
    static const uint8_t STATE_BIT[6] = {7, 6, 5, 4, 3, 2};

    uint8_t  sw_last[6]  = {1, 1, 1, 1, 1, 1};
    uint16_t loop_cnt    = 0;

    // ★ OHCL 스위치(A1) 상태 추적
    uint8_t  ohcl_last      = 1;    // 이전 레벨 (1=떼어짐, 0=눌림)
    uint32_t ohcl_press_ms  = 0;    // 누름 시작 시각 (ms, loop_cnt × 10ms)

    while (1) {
        _delay_ms(10);
        loop_cnt++;

        // ── 스위치 처리 (모두 토글, SW6은 이제 PLBM모사 토글) ───────────────
        for (uint8_t i = 0; i < 6; i++) {
            uint8_t sw_now = read_sw(i);

            if (sw_now == 0 && sw_last[i] == 1) {
                sw_last[i] = 0;
                _delay_ms(20);   // 디바운스
                if (read_sw(i) == 0) {
                    // [수정3] PLBM 상호 배타: SW5(PLBM_REAL) 누르면 PLBM_SIM OFF
                    //                        SW6(PLBM_SIM)  누르면 PLBM_REAL OFF
                    uint8_t bit = STATE_BIT[i];
                    if (bit == 3) {         // PLBM_REAL 토글
                        if (g_state & (1 << 3)) {
                            g_state &= ~(1 << 3);
                        } else {
                            g_state &= ~(1 << 2);   // PLBM_SIM 강제 OFF
                            g_state |=  (1 << 3);
                        }
                    } else if (bit == 2) {  // PLBM_SIM 토글
                        if (g_state & (1 << 2)) {
                            g_state &= ~(1 << 2);
                        } else {
                            g_state &= ~(1 << 3);   // PLBM_REAL 강제 OFF
                            g_state |=  (1 << 2);
                        }
                    } else {
                        g_state ^= (1 << bit);      // 일반 토글
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

        // ── ★ OHCL 스위치(A1/PC1) 처리 — 장/단 누름 판별 ──────────────────
        // 외부 Pull-up → 눌리면 LOW, 떼면 HIGH
        {
            uint8_t ohcl_now = (PINC & (1 << PIN_OHCL_SW)) ? 1 : 0;

            if (ohcl_now == 0 && ohcl_last == 1) {
                // 하강 엣지: 누름 시작
                _delay_ms(20);  // 디바운스
                if ((PINC & (1 << PIN_OHCL_SW)) == 0) {
                    ohcl_last     = 0;
                    ohcl_press_ms = (uint32_t)loop_cnt * 10UL;  // 누름 시작 ms
                }
            } else if (ohcl_now == 1 && ohcl_last == 0) {
                // 상승 엣지: 떼어짐 → 누름 지속 시간 계산
                uint32_t now_ms   = (uint32_t)loop_cnt * 10UL;
                uint32_t held_ms  = now_ms - ohcl_press_ms;
                ohcl_last = 1;

                if (held_ms < OHCL_LONG_PRESS_MS) {
                    // 단타(<1.3s) → 수동녹화 트리거
                    uart_send_sw('7');          // SW7: OHCL 단타 이벤트
                    uart_send_ohcl_trigger('M'); // 'M': 수동녹화
                } else {
                    // 장누름(≥1.3s) → 타임랩스 트리거
                    uart_send_sw('8');           // SW8: OHCL 장누름 이벤트
                    uart_send_ohcl_trigger('N'); // 'N': 타임랩스
                }
                g_tx_request = 1;
            }
        }

        // ── LED PWM 감지 (매 루프 10ms 단위 샘플링, 50ms 연속 확정) ──────────
        {
            uint8_t confirmed_val = 0;
            if (led_raw_sample(&confirmed_val)) {
                // 확정된 변화 발생
                g_led_on = confirmed_val;
                if (confirmed_val) g_state |=  (1 << 1);
                else               g_state &= ~(1 << 1);
                uart_send_led(confirmed_val);
                g_tx_request = 1;
            }
        }

        // ── 상태 전송 ────────────────────────────────────────────────────────
        if (g_tx_request) {
            g_tx_request = 0;
            uart_send_state();
        }

        // ── 주기 보고 (2초마다) ──────────────────────────────────────────────
        if (loop_cnt >= 200) {
            loop_cnt = 0;
            uart_send_state();
        }
    }
}
