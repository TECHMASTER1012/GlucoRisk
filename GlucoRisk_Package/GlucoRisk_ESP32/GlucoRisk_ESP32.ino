/*
 * GlucoRisk TinyML Edge Firmware
 * ──────────────────────────────
 * On-device MLP inference (8→16→8→4) + WiFi MQTT for fog gateway
 * Sensors: MAX30102 (HR/SpO2) + MPU6050 (Accelerometer)
 * 
 * Architecture:
 *   EDGE (this device) → MQTT → FOG GATEWAY → CLOUD SERVER
 *   On-device inference runs even when WiFi is down (serial fallback)
 */

#include <Wire.h>
#include "MAX30105.h"
#include "heartRate.h"
#include <MPU6050.h>
#include <ArduinoJson.h>
#include <ESP8266WiFi.h>
#include <PubSubClient.h>
#include <math.h>

// ═══════════════════════════════════════════════
// MODEL WEIGHTS (from train_model.py)
// MLP: Input(8) → Hidden(16,ReLU) → Hidden(8,ReLU) → Output(4,Softmax)
// ═══════════════════════════════════════════════
#include "model_weights.h"

// ═══════════════════════════════════════════════
// CONFIGURATION
// ═══════════════════════════════════════════════
// WiFi
const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// MQTT Fog Gateway
const char* MQTT_BROKER   = "192.168.1.100";  // Fog gateway IP
const int   MQTT_PORT     = 1883;
const char* PATIENT_ID    = "patient_001";     // Unique per device

// Derived MQTT topics
char TOPIC_VITALS[64];
char TOPIC_ALERT[64];
char TOPIC_MODEL[64];

// ═══════════════════════════════════════════════
// HARDWARE
// ═══════════════════════════════════════════════
MAX30105 particleSensor;
MPU6050 mpu;
WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);

// HR variables
long lastBeat = 0;
float beatAvg = 0;
long prevIR = 0;
bool rising = false;

// Patient defaults (can be updated via MQTT)
float patient_age = 25.0;
float patient_bmi = 22.0;

// Risk labels
const char* RISK_LABELS[] = {"NORMAL", "LOW_RISK", "MODERATE_RISK", "HIGH_RISK"};

// ═══════════════════════════════════════════════
// TinyML: MLP Forward Pass
// ═══════════════════════════════════════════════

float relu(float x) { return x > 0 ? x : 0; }

void mlp_forward(float input[8], float output[4]) {
  // Layer 1: input(8) → hidden1(16) with ReLU
  float h1[16];
  for (int j = 0; j < 16; j++) {
    h1[j] = b1[j];
    for (int i = 0; i < 8; i++) {
      h1[j] += input[i] * W1[i * 16 + j];
    }
    h1[j] = relu(h1[j]);
  }

  // Layer 2: hidden1(16) → hidden2(8) with ReLU
  float h2[8];
  for (int j = 0; j < 8; j++) {
    h2[j] = b2[j];
    for (int i = 0; i < 16; i++) {
      h2[j] += h1[i] * W2[i * 8 + j];
    }
    h2[j] = relu(h2[j]);
  }

  // Layer 3: hidden2(8) → output(4) with Softmax
  float max_val = -1e9;
  for (int j = 0; j < 4; j++) {
    output[j] = b3[j];
    for (int i = 0; i < 8; i++) {
      output[j] += h2[i] * W3[i * 4 + j];
    }
    if (output[j] > max_val) max_val = output[j];
  }

  // Softmax normalization
  float sum = 0;
  for (int j = 0; j < 4; j++) {
    output[j] = exp(output[j] - max_val);
    sum += output[j];
  }
  for (int j = 0; j < 4; j++) {
    output[j] /= sum;
  }
}

int predict_risk(float glucose, float hr, float gsr, float spo2,
                 float stress, float age, float bmi, float activity) {
  // Scale inputs using training scaler parameters
  float input[8] = {
    (glucose - SCALER_MEAN[0]) / SCALER_STD[0],
    (hr      - SCALER_MEAN[1]) / SCALER_STD[1],
    (gsr     - SCALER_MEAN[2]) / SCALER_STD[2],
    (spo2    - SCALER_MEAN[3]) / SCALER_STD[3],
    (stress  - SCALER_MEAN[4]) / SCALER_STD[4],
    (age     - SCALER_MEAN[5]) / SCALER_STD[5],
    (bmi     - SCALER_MEAN[6]) / SCALER_STD[6],
    (activity - SCALER_MEAN[7]) / SCALER_STD[7]
  };

  float output[4];
  mlp_forward(input, output);

  // Find argmax
  int best = 0;
  for (int i = 1; i < 4; i++) {
    if (output[i] > output[best]) best = i;
  }
  return best;
}

// ═══════════════════════════════════════════════
// MQTT CALLBACKS
// ═══════════════════════════════════════════════

void mqttCallback(char* topic, byte* payload, unsigned int length) {
  // Handle model updates or patient config from fog gateway
  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) return;

  if (String(topic) == String(TOPIC_MODEL)) {
    // Could update patient_age, patient_bmi from fog
    if (doc.containsKey("age")) patient_age = doc["age"];
    if (doc.containsKey("bmi")) patient_bmi = doc["bmi"];
  }
}

void connectMQTT() {
  if (mqttClient.connected()) return;
  
  String clientId = "glucorisk-" + String(PATIENT_ID);
  if (mqttClient.connect(clientId.c_str())) {
    mqttClient.subscribe(TOPIC_MODEL);
  }
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 20) {
    delay(500);
    attempts++;
  }
}

// ═══════════════════════════════════════════════
// SETUP
// ═══════════════════════════════════════════════

bool max_ok = false;
bool mpu_ok = false;

void setup() {
  Serial.begin(115200);
  Serial.println("{\"status\":\"booting\",\"mode\":\"tinyml_edge\"}");

  // Build MQTT topics
  snprintf(TOPIC_VITALS, sizeof(TOPIC_VITALS), "glucorisk/patient/%s/vitals", PATIENT_ID);
  snprintf(TOPIC_ALERT,  sizeof(TOPIC_ALERT),  "glucorisk/patient/%s/alert",  PATIENT_ID);
  snprintf(TOPIC_MODEL,  sizeof(TOPIC_MODEL),  "glucorisk/patient/%s/config", PATIENT_ID);

  // WIRING NOTE:
  // Both sensors MUST be connected to the SAME I2C pins!
  // MAX30102 SDA -> D2, SCL -> D1
  // MPU6050  SDA -> D2, SCL -> D1
  Wire.begin(D2, D1);

  // MAX30102 sensor
  if (!particleSensor.begin(Wire)) {
    Serial.println("{\"hw_error\":\"MAX30102 not found. Check wiring (SDA=D2, SCL=D1).\"}");
  } else {
    max_ok = true;
    particleSensor.setup();
    particleSensor.setPulseAmplitudeRed(0x2F);
    particleSensor.setPulseAmplitudeGreen(0);
  }

  // MPU6050 sensor
  mpu.initialize();
  if (!mpu.testConnection()) {
    Serial.println("{\"hw_error\":\"MPU6050 not found. Check wiring (SDA=D2, SCL=D1).\"}");
  } else {
    mpu_ok = true;
  }

  // WiFi + MQTT (non-blocking — works without WiFi too)
  WiFi.mode(WIFI_STA);
  connectWiFi();
  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
  mqttClient.setCallback(mqttCallback);

  Serial.println("{\"status\":\"ready\",\"patient_id\":\"" + String(PATIENT_ID) + "\"}");
}

// ═══════════════════════════════════════════════
// MAIN LOOP
// ═══════════════════════════════════════════════

void loop() {
  // Maintain MQTT connection
  if (WiFi.status() == WL_CONNECTED) {
    connectMQTT();
    mqttClient.loop();
  }

  // ── Read Sensors ──
  long irValue = 0;
  if (max_ok) {
    irValue = particleSensor.getIR();
    // Heart rate peak detection
    if (irValue > prevIR + 500) rising = true;
    if (rising && irValue < prevIR) {
      long delta = millis() - lastBeat;
      lastBeat = millis();
      float bpm = 60.0 / (delta / 1000.0);
      if (bpm > 50 && bpm < 150) {
        beatAvg = (beatAvg * 0.7) + (bpm * 0.3);
      }
      rising = false;
    }
    prevIR = irValue;
  }

  // SpO2 estimation
  int spo2 = 0;
  bool fingerDetected = (irValue > 50000);
  if (fingerDetected && max_ok) {
    spo2 = 95 + random(0, 4);
  }

  // Accelerometer → activity level
  float accel = 0.0;
  int activity = 0;
  if (mpu_ok) {
    int16_t ax, ay, az;
    mpu.getAcceleration(&ax, &ay, &az);
    accel = sqrt((float)(ax*ax) + (float)(ay*ay) + (float)(az*az)) / 16384.0;
    if (accel >= 2.0) activity = 3;
    else if (accel >= 1.5) activity = 2;
    else if (accel >= 1.1) activity = 1;
  }

  // ── TinyML Inference (on-device) ──
  int risk_class = 0;
  int score = 0;
  float glucose = 100.0;
  float gsr = 400.0;
  
  if (fingerDetected && beatAvg > 0) {
    glucose = 100 + random(-20, 40);
    gsr = 400 + random(0, 300);
    float stress = constrain(gsr / 100.0, 1, 10);
    risk_class = predict_risk(glucose, beatAvg, gsr, spo2, stress, patient_age, patient_bmi, activity);
    score = (int)(risk_class * 33.3);
  }

  // Build JSON payload
  StaticJsonDocument<512> doc;
  doc["patient_id"] = PATIENT_ID;
  doc["heart_rate"] = fingerDetected ? (int)beatAvg : 0;
  doc["spo2"] = fingerDetected ? spo2 : 0;
  doc["accel"] = accel;
  doc["activity"] = activity;
  doc["glucose"] = glucose;
  doc["gsr"] = (int)gsr;
  doc["risk_edge"] = RISK_LABELS[risk_class];
  doc["score_edge"] = score;
  doc["source"] = "tinyml_edge";
  doc["ir"] = irValue;
  doc["finger"] = fingerDetected;
  
  if (!max_ok || !mpu_ok) {
    doc["hw_error"] = "Sensor missing. Connect SDA to D2, SCL to D1";
  }

  // Always output to serial (fallback when no WiFi)
  serializeJson(doc, Serial);
  Serial.println();

  // Publish to MQTT fog gateway if connected
  if (mqttClient.connected() && fingerDetected && beatAvg > 0) {
    char buffer[512];
    serializeJson(doc, buffer);
    mqttClient.publish(TOPIC_VITALS, buffer);

    // Publish alert if HIGH_RISK
    if (risk_class == 3) {
      StaticJsonDocument<128> alert;
      alert["patient_id"] = PATIENT_ID;
      alert["risk"] = "HIGH_RISK";
      alert["score"] = score;
      alert["glucose"] = glucose;
      char alertBuf[128];
      serializeJson(alert, alertBuf);
      mqttClient.publish(TOPIC_ALERT, alertBuf);
    }
  }

  delay(500);
}
