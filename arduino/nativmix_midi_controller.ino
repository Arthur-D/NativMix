// NativMix MIDI Controller
//
// Board:    SparkFun Pro Micro (ATmega32u4) — native USB-MIDI
// Libraries: MIDIUSB, Adafruit NeoPixel
//
// Hardware:
//   A0–A3        4 potentiometers / faders
//   D2–D5        4 mute buttons   (pull-up, active LOW)
//   D6, D7       2 profile buttons: prev / next (pull-up, active LOW)
//   D8           1 switch: activate a specific profile (pull-up, active LOW)
//   D9           WS2812B data (6 LEDs in series: 0–3 mute, 4–5 profile)
//
// MIDI CC (channel 1):
//   Send:    CC 1–4   fader volume (0–127)
//            CC 5–8   mute toggle (value 127)
//            CC 9     profile prev (value 127)
//            CC 10    profile next (value 127)
//            CC 11    profile direct activate (value 127)
//   Receive: CC 32–37 LED color — value = hue (0–127, see below)
//            CC 38    global LED brightness (0–127, optional)
//
// LED hue encoding (CC value → color):
//   0   = red    (muted / error)
//   21  = orange
//   42  = green  (active / ok)
//   63  = yellow (takeover active)
//   85  = blue   (profile / idle)
//   106 = purple
//   127 = back to red (full hue circle)
//   Special: value 255 (or any >127 if sent) = off
//
// USB device name: "NativMix Controller" / manufacturer "knoelliX"
// Set in SparkFun core boards.txt:
//   promicro.build.usb_product="NativMix Controller"
//   promicro.build.usb_manufacturer="knoelliX"

#include <MIDIUSB.h>
#include <Adafruit_NeoPixel.h>

// ── Pins ─────────────────────────────────────────────────────────
constexpr uint8_t POT_PINS[4]          = {A0, A1, A2, A3};
constexpr uint8_t MUTE_BTN_PINS[4]     = {2, 3, 4, 5};
constexpr uint8_t PROFILE_BTN_PINS[2]  = {6, 7};  // [0]=prev, [1]=next
constexpr uint8_t PROFILE_SW_PIN       = 8;
constexpr uint8_t LED_PIN              = 9;
constexpr uint8_t LED_COUNT            = 6;

// ── MIDI ─────────────────────────────────────────────────────────
constexpr uint8_t MIDI_CH        = 0;   // 0 = channel 1 (MIDIUSB internal)
constexpr uint8_t CC_FADER       = 1;   // CC 1–4: fader volume
constexpr uint8_t CC_MUTE        = 5;   // CC 5–8: mute toggle
constexpr uint8_t CC_PROF_PREV   = 9;
constexpr uint8_t CC_PROF_NEXT   = 10;
constexpr uint8_t CC_PROF_DIR    = 11;  // switch → direct profile activate
constexpr uint8_t CC_LED_BASE    = 32;  // CC 32–37: LED hue (one per LED)
constexpr uint8_t CC_LED_BRIGHT  = 38;  // CC 38: global brightness (optional)

// ── NeoPixel ─────────────────────────────────────────────────────
Adafruit_NeoPixel leds(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);

// Default hues (0–127, maps to 0°–360° hue)
constexpr uint8_t HUE_GREEN   = 42;   // active / unmuted
constexpr uint8_t HUE_RED     = 0;    // muted
constexpr uint8_t HUE_BLUE    = 85;   // profile button idle
constexpr uint8_t HUE_YELLOW  = 63;   // takeover / pending

constexpr uint8_t  DEFAULT_BRIGHTNESS = 50;
constexpr uint8_t  SATURATION         = 255;

// ── State ─────────────────────────────────────────────────────────
int      potLast[4]        = {-1, -1, -1, -1};
bool     muteBtnLast[4]    = {};
bool     profileBtnLast[2] = {};
bool     switchLast        = false;
uint8_t  ledHue[6]         = {HUE_GREEN, HUE_GREEN, HUE_GREEN, HUE_GREEN,
                               HUE_BLUE, HUE_BLUE};
uint8_t  brightness        = DEFAULT_BRIGHTNESS;
uint32_t flashUntil[6]     = {};

constexpr uint8_t  POT_DEAD    = 2;
constexpr uint16_t DEBOUNCE_MS = 25;
constexpr uint16_t FLASH_MS    = 120;

// ── Helpers ───────────────────────────────────────────────────────

// hue 0–127 → NeoPixel color (full saturation, fixed brightness)
uint32_t hueToColor(uint8_t hue) {
    // NeoPixel ColorHSV expects hue 0–65535
    return leds.gamma32(leds.ColorHSV(
        static_cast<uint16_t>(hue) * 512,
        SATURATION,
        brightness
    ));
}

void setLED(uint8_t idx, uint8_t hue) {
    leds.setPixelColor(idx, hueToColor(hue));
    leds.show();
}

void flashLED(uint8_t idx) {
    flashUntil[idx] = millis() + FLASH_MS;
    leds.setPixelColor(idx, leds.Color(brightness, brightness, brightness));
    leds.show();
}

void sendCC(uint8_t cc, uint8_t val) {
    midiEventPacket_t msg = {0x0B, static_cast<uint8_t>(0xB0 | MIDI_CH), cc, val};
    MidiUSB.sendMIDI(msg);
    MidiUSB.flush();
}

void processIncomingMIDI() {
    midiEventPacket_t rx = MidiUSB.read();
    if (rx.header == 0x00) return;
    if ((rx.byte1 & 0xF0) != 0xB0) return;  // CC only

    uint8_t cc  = rx.byte2;
    uint8_t val = rx.byte3;

    // LED color: CC 32–37 → hue for LED 0–5
    if (cc >= CC_LED_BASE && cc < CC_LED_BASE + LED_COUNT) {
        uint8_t idx = cc - CC_LED_BASE;
        ledHue[idx] = val;
        if (!flashUntil[idx]) {
            setLED(idx, ledHue[idx]);
        }
    }

    // Global brightness: CC 38
    if (cc == CC_LED_BRIGHT) {
        brightness = map(val, 0, 127, 10, 255);
        for (uint8_t i = 0; i < LED_COUNT; i++) {
            if (!flashUntil[i]) setLED(i, ledHue[i]);
        }
    }
}

// ── Setup ─────────────────────────────────────────────────────────
void setup() {
    for (uint8_t i = 0; i < 4; i++) pinMode(MUTE_BTN_PINS[i],    INPUT_PULLUP);
    for (uint8_t i = 0; i < 2; i++) pinMode(PROFILE_BTN_PINS[i], INPUT_PULLUP);
    pinMode(PROFILE_SW_PIN, INPUT_PULLUP);

    leds.begin();
    leds.setBrightness(brightness);
    for (uint8_t i = 0; i < LED_COUNT; i++) setLED(i, ledHue[i]);
}

// ── Loop ──────────────────────────────────────────────────────────
void loop() {
    uint32_t now = millis();

    // Restore LED colors after flash
    for (uint8_t i = 0; i < LED_COUNT; i++) {
        if (flashUntil[i] && now >= flashUntil[i]) {
            flashUntil[i] = 0;
            setLED(i, ledHue[i]);
        }
    }

    // ── Faders ───────────────────────────────────────────────
    for (uint8_t i = 0; i < 4; i++) {
        int val7 = analogRead(POT_PINS[i]) >> 3;
        if (abs(val7 - potLast[i]) > POT_DEAD) {
            potLast[i] = val7;
            sendCC(CC_FADER + i, static_cast<uint8_t>(val7));
        }
    }

    // ── Mute buttons ─────────────────────────────────────────
    for (uint8_t i = 0; i < 4; i++) {
        bool pressed = !digitalRead(MUTE_BTN_PINS[i]);
        if (pressed && !muteBtnLast[i]) {
            delay(DEBOUNCE_MS);
            if (!digitalRead(MUTE_BTN_PINS[i])) {
                sendCC(CC_MUTE + i, 127);
                flashLED(i);
            }
        }
        muteBtnLast[i] = pressed;
    }

    // ── Profile buttons (prev / next) ────────────────────────
    for (uint8_t i = 0; i < 2; i++) {
        bool pressed = !digitalRead(PROFILE_BTN_PINS[i]);
        if (pressed && !profileBtnLast[i]) {
            delay(DEBOUNCE_MS);
            if (!digitalRead(PROFILE_BTN_PINS[i])) {
                sendCC(i == 0 ? CC_PROF_PREV : CC_PROF_NEXT, 127);
                flashLED(4 + i);
            }
        }
        profileBtnLast[i] = pressed;
    }

    // ── Profile switch (direct activate) ─────────────────────
    bool sw = !digitalRead(PROFILE_SW_PIN);
    if (sw && !switchLast) {
        delay(DEBOUNCE_MS);
        if (!digitalRead(PROFILE_SW_PIN)) {
            sendCC(CC_PROF_DIR, 127);
        }
    }
    switchLast = sw;

    // ── Incoming MIDI (LED feedback from NativMix) ────────────
    processIncomingMIDI();
}
