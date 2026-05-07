// NativMix MIDI Controller
//
// Board:    SparkFun Pro Micro (ATmega32u4) — native USB-MIDI
// Libraries: MIDIUSB, Adafruit NeoPixel
//
// Hardware:
//   A0–A3        4 Potentiometer / Fader
//   D2–D5        4 Mute-Buttons  (Pull-up, active LOW)
//   D6, D7       2 Profil-Buttons: Zurück / Vor (Pull-up, active LOW)
//   D8           1 Schalter: direktes Profil aktivieren (Pull-up, active LOW)
//   D9           WS2812B Daten (6 LEDs in Reihe: 0–3 Mute, 4–5 Profil)
//
// MIDI CC (Kanal 1):
//   Senden:   CC 1–4  Fader-Lautstärke
//             CC 5–8  Mute-Toggle (Wert 127)
//             CC 9    Profil zurück (Wert 127)
//             CC 10   Profil vor   (Wert 127)
//             CC 11   Profil direkt aktivieren (Wert 127)
//   Empfangen: CC 5–8  Mute-State von NativMix (>=64 = gemutet)
//
// USB-Gerätename: "NativMix Controller" / Hersteller "knoelliX"
// (boards.txt-Eintrag oder SparkFun-Core: usb_desc.h anpassen)

#include <MIDIUSB.h>
#include <Adafruit_NeoPixel.h>

// ── Pins ─────────────────────────────────────────────────────────
constexpr uint8_t POT_PINS[4]          = {A0, A1, A2, A3};
constexpr uint8_t MUTE_BTN_PINS[4]     = {2, 3, 4, 5};
constexpr uint8_t PROFILE_BTN_PINS[2]  = {6, 7};  // [0]=zurück, [1]=vor
constexpr uint8_t PROFILE_SW_PIN       = 8;
constexpr uint8_t LED_PIN              = 9;
constexpr uint8_t LED_COUNT            = 6;

// ── MIDI ─────────────────────────────────────────────────────────
constexpr uint8_t MIDI_CH       = 0;    // 0 = Kanal 1 (MIDIUSB-intern)
constexpr uint8_t CC_FADER      = 1;    // CC 1–4
constexpr uint8_t CC_MUTE       = 5;    // CC 5–8
constexpr uint8_t CC_PROF_PREV  = 9;
constexpr uint8_t CC_PROF_NEXT  = 10;
constexpr uint8_t CC_PROF_DIR   = 11;   // Schalter → direktes Profil

// ── NeoPixel ─────────────────────────────────────────────────────
Adafruit_NeoPixel leds(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);

constexpr uint32_t COL_ACTIVE  = 0x004000;  // grün  — nicht gemutet
constexpr uint32_t COL_MUTED   = 0x400000;  // rot   — gemutet
constexpr uint32_t COL_PROFILE = 0x000040;  // blau  — Profil-Button
constexpr uint32_t COL_FLASH   = 0x404040;  // weiß  — Tastendruck-Feedback
constexpr uint32_t COL_OFF     = 0x000000;

// ── Zustand ───────────────────────────────────────────────────────
int      potLast[4]        = {-1, -1, -1, -1};
bool     muteBtnLast[4]    = {};
bool     profileBtnLast[2] = {};
bool     switchLast        = false;
bool     muteState[4]      = {};       // per eingehendem MIDI aktualisiert
uint32_t flashUntil[6]     = {};       // Flash-Ablaufzeit pro LED

constexpr uint8_t  POT_DEAD    = 2;    // Deadband 10→7 bit
constexpr uint16_t DEBOUNCE_MS = 25;
constexpr uint16_t FLASH_MS    = 120;

// ── Hilfsfunktionen ───────────────────────────────────────────────

void sendCC(uint8_t cc, uint8_t val) {
    midiEventPacket_t msg = {0x0B, static_cast<uint8_t>(0xB0 | MIDI_CH), cc, val};
    MidiUSB.sendMIDI(msg);
    MidiUSB.flush();
}

void setLED(uint8_t idx, uint32_t color) {
    leds.setPixelColor(idx, color);
    leds.show();
}

void flashLED(uint8_t idx, uint32_t color) {
    flashUntil[idx] = millis() + FLASH_MS;
    setLED(idx, color);
}

uint32_t defaultLEDColor(uint8_t idx) {
    if (idx < 4) return muteState[idx] ? COL_MUTED : COL_ACTIVE;
    return COL_PROFILE;
}

void processIncomingMIDI() {
    midiEventPacket_t rx = MidiUSB.read();
    if (rx.header == 0x00) return;
    if ((rx.byte1 & 0xF0) != 0xB0) return;  // nur CC

    uint8_t cc  = rx.byte2;
    uint8_t val = rx.byte3;

    // Mute-State-Feedback von NativMix (CC 5–8)
    if (cc >= CC_MUTE && cc < CC_MUTE + 4) {
        uint8_t ch = cc - CC_MUTE;
        muteState[ch] = (val >= 64);
        if (!flashUntil[ch]) {
            setLED(ch, defaultLEDColor(ch));
        }
    }
}

// ── Setup ─────────────────────────────────────────────────────────
void setup() {
    for (uint8_t i = 0; i < 4; i++) pinMode(MUTE_BTN_PINS[i],    INPUT_PULLUP);
    for (uint8_t i = 0; i < 2; i++) pinMode(PROFILE_BTN_PINS[i], INPUT_PULLUP);
    pinMode(PROFILE_SW_PIN, INPUT_PULLUP);

    leds.begin();
    leds.setBrightness(50);
    for (uint8_t i = 0; i < 4; i++) leds.setPixelColor(i, COL_ACTIVE);
    leds.setPixelColor(4, COL_PROFILE);
    leds.setPixelColor(5, COL_PROFILE);
    leds.show();
}

// ── Loop ──────────────────────────────────────────────────────────
void loop() {
    uint32_t now = millis();

    // Flash-LEDs ablaufen lassen und Standardfarbe wiederherstellen
    for (uint8_t i = 0; i < LED_COUNT; i++) {
        if (flashUntil[i] && now >= flashUntil[i]) {
            flashUntil[i] = 0;
            setLED(i, defaultLEDColor(i));
        }
    }

    // ── Fader ─────────────────────────────────────────────────
    for (uint8_t i = 0; i < 4; i++) {
        int val7 = analogRead(POT_PINS[i]) >> 3;
        if (abs(val7 - potLast[i]) > POT_DEAD) {
            potLast[i] = val7;
            sendCC(CC_FADER + i, static_cast<uint8_t>(val7));
        }
    }

    // ── Mute-Buttons ──────────────────────────────────────────
    for (uint8_t i = 0; i < 4; i++) {
        bool pressed = !digitalRead(MUTE_BTN_PINS[i]);
        if (pressed && !muteBtnLast[i]) {
            delay(DEBOUNCE_MS);
            if (!digitalRead(MUTE_BTN_PINS[i])) {
                sendCC(CC_MUTE + i, 127);
                flashLED(i, COL_FLASH);
            }
        }
        muteBtnLast[i] = pressed;
    }

    // ── Profil-Buttons (zurück / vor) ─────────────────────────
    for (uint8_t i = 0; i < 2; i++) {
        bool pressed = !digitalRead(PROFILE_BTN_PINS[i]);
        if (pressed && !profileBtnLast[i]) {
            delay(DEBOUNCE_MS);
            if (!digitalRead(PROFILE_BTN_PINS[i])) {
                sendCC(i == 0 ? CC_PROF_PREV : CC_PROF_NEXT, 127);
                flashLED(4 + i, COL_FLASH);
            }
        }
        profileBtnLast[i] = pressed;
    }

    // ── Profil-Schalter (direktes Profil) ─────────────────────
    bool sw = !digitalRead(PROFILE_SW_PIN);
    if (sw && !switchLast) {
        delay(DEBOUNCE_MS);
        if (!digitalRead(PROFILE_SW_PIN)) {
            sendCC(CC_PROF_DIR, 127);
        }
    }
    switchLast = sw;

    // ── Eingehende MIDI-Nachrichten (LED-Feedback) ────────────
    processIncomingMIDI();
}
