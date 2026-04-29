#ifndef F_CPU
#define F_CPU 16000000UL
#endif

#include <avr/io.h>
#include <avr/interrupt.h>
#include <util/delay.h>

// 전원 상태 관리 (Bit3:B+, Bit2:TG, Bit1:ACC, Bit0:IGN)
volatile uint8_t power_status = 0; 

// 하드웨어 업데이트 (릴레이: PD7~4, LED: PB0~3)
void update_hardware() {
    // 릴레이 제어 (D7, D6, D5, D4 순서)
    for(int i=0; i<4; i++) {
        if(power_status & (1 << (3-i))) 
            PORTD &= ~(1 << (7-i)); // ON (Low)
        else 
            PORTD |= (1 << (7-i));  // OFF (High)
    }
    // LED 제어 (D8, D9, D10, D11 순서)
    for(int i=0; i<4; i++) {
        if(power_status & (1 << (3-i))) 
            PORTB |= (1 << i);      // ON (High)
        else 
            PORTB &= ~(1 << i);     // OFF (Low)
    }
}

// PC 명령 수신 (PyQt 전송 문자 대응)
ISR(USART_RX_vect) {
    char cmd = UDR0;
    switch(cmd) {
        case '1': power_status |= (1 << 3); break; // B+ ON
        case 'Q': power_status &= ~(1 << 3); break; // B+ OFF
        case '2': power_status |= (1 << 2); break; // TG ON
        case 'W': power_status &= ~(1 << 2); break; // TG OFF
        case '3': power_status |= (1 << 1); break; // ACC ON
        case 'E': power_status &= ~(1 << 1); break; // ACC OFF
        case '4': power_status |= (1 << 0); break; // IGN ON
        case 'R': power_status &= ~(1 << 0); break; // IGN OFF
        case '0': power_status = 0; break;          // ALL OFF
    }
    update_hardware();
}

int main(void) {
    DDRD |= 0xF0; // PD4~7 출력 (릴레이)
    DDRB |= 0x0F; // PB0~3 출력 (LED)
    DDRC &= ~0x0F; // PC0~3 입력 (택트)
    PORTC |= 0x0F; // 풀업 저항

    // UART 설정 (9600 bps)
    UBRR0H = 0; UBRR0L = 103;
    UCSR0B = (1 << RXEN0) | (1 << TXEN0) | (1 << RXCIE0);
    UCSR0C = (3 << UCSZ00);

    sei(); 
    update_hardware();

    uint8_t last_sw = 0x0F;

    while(1) {
        uint8_t current_sw = PINC & 0x0F;
        for(int i=0; i<4; i++) {
            // 버튼 눌림 감지 (Falling Edge)
            if(!(current_sw & (1 << i)) && (last_sw & (1 << i))) {
                _delay_ms(20); // 디바운싱
                if(!(PINC & (1 << i))) {
                    power_status ^= (1 << (3-i)); // 해당 채널 토글
                    update_hardware();
                }
            }
        }
        last_sw = current_sw;
        _delay_ms(10);
    }
}