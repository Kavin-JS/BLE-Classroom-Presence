/* ================================================================
   Multi-Node BLE-Based Classroom Presence Verification System
   ESP32 Firmware — NODE_2
   ================================================================
   Role of this node:
     1. Connects to Wi-Fi (college network).
     2. Continuously scans for BLE advertisements.
     3. Matches advertised "Local Name" against the registered
        device list (configured via nRF Connect on each phone).
     4. Reports each match (device_id + RSSI) to the laptop's
        Flask server over HTTP.
     5. Sends a periodic heartbeat so the server/dashboard knows
        this node is alive even if no device is currently in range.
     6. Drives 4 status LEDs (Green/Blue/Yellow/Red).

   node_1.ino is IDENTICAL to this file except for the NODE_ID
   constant below ("NODE_2" instead of "NODE_2"). Everything else
   — wiring, logic, timing — is the same on both boards.
   ================================================================ */

#include <WiFi.h>
#include <HTTPClient.h>
#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>

// ================================================================
// ============================ CONFIG ===========================
// ================================================================

// ---- Node identity (THE ONLY LINE THAT DIFFERS FROM node_1.ino) ----
const char* NODE_ID = "NODE_2";

// ---- Wi-Fi credentials (hardcoded per project requirements) ----
const char* WIFI_SSID     = "Amrita_CHN2";
const char* WIFI_PASSWORD = "amrita@321";

// ---- Laptop Flask server ----
const char* SERVER_IP      = "11.12.10.198";
const uint16_t SERVER_PORT = 5000;

// ---- Registered BLE device "Local Names" ----
// These MUST exactly match the "Local name" you configure in the
// nRF Connect > Advertiser packet on each phone (see README).
const char* REGISTERED_DEVICES[] = { "ATTEND-KAVIN", "ATTEND-LOHITH" };
const int   NUM_REGISTERED_DEVICES = 2;

// ---- BLE scan timing ----
const int BLE_SCAN_TIME_SECONDS = 4;   // length of one active scan cycle

// ---- Heartbeat timing ----
const unsigned long HEARTBEAT_INTERVAL_MS = 5000; // send heartbeat every 5s

// ---- LED pins (IDENTICAL wiring on both nodes) ----
const int LED_GREEN_PIN  = 25; // System ready (Wi-Fi connected)
const int LED_BLUE_PIN   = 26; // Actively scanning BLE
const int LED_YELLOW_PIN = 27; // Data transfer to laptop (brief flash)
const int LED_RED_PIN    = 14; // Issue: Wi-Fi down or server unreachable

const unsigned long YELLOW_FLASH_MS = 150; // how long the "sent data" flash lasts

// ---- HTTP retry/failure handling ----
// This college network has been observed to have very high latency
// and jitter under load (round-trips of several seconds are normal,
// not a sign of a blocked connection) — timeout is set generously to
// avoid false "failures" that are really just slow, not broken.
const unsigned long HTTP_TIMEOUT_MS = 8000;
const int MAX_CONSECUTIVE_HTTP_FAILURES = 3;

// ================================================================
// ========================= INTERNAL STATE ========================
// ================================================================

BLEScan* pBLEScan;
unsigned long lastHeartbeatMillis = 0;
unsigned long yellowLedOffAt = 0;
int consecutiveHttpFailures = 0;

// ================================================================
// ========================= HELPER FUNCTIONS =======================
// ================================================================

bool isRegisteredDevice(const String &name) {
  for (int i = 0; i < NUM_REGISTERED_DEVICES; i++) {
    if (name == REGISTERED_DEVICES[i]) return true;
  }
  return false;
}

void setLed(int pin, bool on) {
  digitalWrite(pin, on ? HIGH : LOW);
}

void flashYellow() {
  setLed(LED_YELLOW_PIN, true);
  yellowLedOffAt = millis() + YELLOW_FLASH_MS;
}

void serviceYellowLed() {
  if (yellowLedOffAt != 0 && millis() >= yellowLedOffAt) {
    setLed(LED_YELLOW_PIN, false);
    yellowLedOffAt = 0;
  }
}

void updateRedLed() {
  bool problem = (WiFi.status() != WL_CONNECTED) ||
                 (consecutiveHttpFailures >= MAX_CONSECUTIVE_HTTP_FAILURES);
  setLed(LED_RED_PIN, problem);
}

void updateGreenLed() {
  setLed(LED_GREEN_PIN, WiFi.status() == WL_CONNECTED);
}

// Sends one HTTP POST with a raw JSON body. Returns true on HTTP 2xx.
bool postJson(const String &path, const String &jsonBody) {
  if (WiFi.status() != WL_CONNECTED) {
    consecutiveHttpFailures++;
    updateRedLed();
    return false;
  }

  HTTPClient http;
  String url = String("http://") + SERVER_IP + ":" + String(SERVER_PORT) + path;
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(HTTP_TIMEOUT_MS);

  int httpCode = http.POST(jsonBody);
  http.end();

  if (httpCode >= 200 && httpCode < 300) {
    consecutiveHttpFailures = 0;
    updateRedLed();
    flashYellow();
    return true;
  } else {
    consecutiveHttpFailures++;
    updateRedLed();
    Serial.print("[");
    Serial.print(NODE_ID);
    Serial.print("] HTTP POST to ");
    Serial.print(path);
    Serial.print(" failed, code=");
    Serial.println(httpCode);
    return false;
  }
}

void sendDetection(const String &deviceId, int rssi) {
  String json = String("{\"node_id\":\"") + NODE_ID +
                "\",\"device_id\":\"" + deviceId +
                "\",\"rssi\":" + String(rssi) + "}";
  postJson("/detection", json);
}

void sendHeartbeat() {
  String json = String("{\"node_id\":\"") + NODE_ID + "\",\"status\":\"online\"}";
  postJson("/heartbeat", json);
}

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  Serial.print("[");
  Serial.print(NODE_ID);
  Serial.print("] Connecting to Wi-Fi");

  unsigned long startAttempt = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startAttempt < 20000) {
    delay(400);
    Serial.print(".");
    setLed(LED_RED_PIN, (millis() / 400) % 2 == 0); // blink red while trying
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    // Disable Wi-Fi modem sleep. ESP32's default power-save mode can
    // delay or drop packets significantly on a busy/congested AP —
    // exactly the multi-second latency spikes observed on this
    // network. Trades a bit of extra power draw for far more
    // consistent HTTP timing, which matters more for this project.
    WiFi.setSleep(false);

    Serial.print("[");
    Serial.print(NODE_ID);
    Serial.print("] Wi-Fi connected. IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.print("[");
    Serial.print(NODE_ID);
    Serial.println("] Wi-Fi connection FAILED (will retry in loop).");
  }
  updateGreenLed();
  updateRedLed();
}

// ================================================================
// ====================== BLE SCAN + PROCESSING =====================
// ================================================================

void runScanCycle() {
  setLed(LED_BLUE_PIN, true);

  BLEScanResults* foundDevices = pBLEScan->start(BLE_SCAN_TIME_SECONDS, false);

  int count = foundDevices->getCount();
  for (int i = 0; i < count; i++) {
    BLEAdvertisedDevice device = foundDevices->getDevice(i);
    if (!device.haveName()) continue;

    String name = String(device.getName().c_str());
    if (isRegisteredDevice(name)) {
      int rssi = device.getRSSI();
      Serial.print("[");
      Serial.print(NODE_ID);
      Serial.print("] Detected ");
      Serial.print(name);
      Serial.print(" RSSI=");
      Serial.println(rssi);
      sendDetection(name, rssi);
    }
  }

  pBLEScan->clearResults();
  setLed(LED_BLUE_PIN, false);
}

// ================================================================
// ======================= ARDUINO SETUP / LOOP =====================
// ================================================================

void setup() {
  Serial.begin(115200);
  delay(300);

  pinMode(LED_GREEN_PIN, OUTPUT);
  pinMode(LED_BLUE_PIN, OUTPUT);
  pinMode(LED_YELLOW_PIN, OUTPUT);
  pinMode(LED_RED_PIN, OUTPUT);
  setLed(LED_GREEN_PIN, false);
  setLed(LED_BLUE_PIN, false);
  setLed(LED_YELLOW_PIN, false);
  setLed(LED_RED_PIN, false);

  connectWiFi();

  BLEDevice::init("");
  pBLEScan = BLEDevice::getScan();
  pBLEScan->setActiveScan(true);
  pBLEScan->setInterval(100);
  pBLEScan->setWindow(99);

  Serial.print("[");
  Serial.print(NODE_ID);
  Serial.println("] Setup complete. Starting scan loop.");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    updateGreenLed();
    updateRedLed();
    connectWiFi();
  }

  // Heartbeat is checked BEFORE the scan/detection cycle (which can
  // involve several blocking HTTP calls with up to 3s timeout each)
  // so a run of failed detection POSTs can't delay the heartbeat and
  // make the dashboard falsely show this node as offline.
  unsigned long now = millis();
  if (now - lastHeartbeatMillis >= HEARTBEAT_INTERVAL_MS) {
    sendHeartbeat();
    lastHeartbeatMillis = now;
  }

  runScanCycle();

  serviceYellowLed();
  updateGreenLed();
  updateRedLed();
}
