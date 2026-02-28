const int NUM_SLIDERS = 6;
// Analog inputs for sliders
const int analogInputs[NUM_SLIDERS] = {A0, A1, A2, A3, A4, A5};

int analogSliderValues[NUM_SLIDERS];

void setup() {
  for (int i = 0; i < NUM_SLIDERS; i++) {
    pinMode(analogInputs[i], INPUT);
  }

  Serial.begin(9600);
}

void loop() {
  updateSliderValues();
  sendSliderValues();
  delay(10);
}

void updateSliderValues() {
  for (int i = 0; i < NUM_SLIDERS; i++) {
     // Optional: Reverse the value if your sliders are inverted
     // analogSliderValues[i] = 1023 - analogRead(analogInputs[i]);
     analogSliderValues[i] = analogRead(analogInputs[i]);
  }
}

void sendSliderValues() {
  String builtString = "";

  for (int i = 0; i < NUM_SLIDERS; i++) {
    builtString += String((int)analogSliderValues[i]);

    if (i < NUM_SLIDERS - 1) {
      builtString += "|";
    }
  }

  Serial.println(builtString);
}
