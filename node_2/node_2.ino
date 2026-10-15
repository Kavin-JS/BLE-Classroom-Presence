/*
  Multi-Node BLE Classroom Presence Verification
  Board: ESP32 Dev Module (ESP32-WROOM-32)
  Arduino-ESP32 core: 3.x compatible

  IMPORTANT:
  - Upload node_1.ino to Node 1 and node_2.ino to Node 2.
  - This sketch intentionally uses WiFi.begin(SSID, PASSWORD), not a fixed
    BSSID/channel. The AP/channel may change; hard-locking it can prevent joins.
  - Server API expected:
      POST /api/heartbeat
      POST /api/detection
  - The server must be reachable at SERVER_IP:SERVER_PORT from the ESP32.
*/

#include <WiFi.h>
#include <WiFiClient.h>
#include <HTTPClient.h>
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>

// --------------------------- NODE CONFIG ---------------------------
#define NODE_ID "NODE_2"

// --------------------------- NETWORK ------------------------------
static const char* WIFI_SSID     = "Amrita_CHN2";
static const char* WIFI_PASSWORD = "amrita@321";

static const char* SERVER_IP = "11.12.10.198";
static const uint16_t SERVER_PORT = 5000;

static const char* API_HEARTBEAT = "/api/heartbeat";
static const char* API_DETECTION = "/api/detection";

// --------------------------- BLE -----------------------------------
static const char* DEVICE_1 = "ATTEND-KAVIN";
static const char* DEVICE_2 = "ATTEND-LOHITH";
static const uint32_t BLE_SCAN_SECONDS = 3;

// --------------------------- TIMERS --------------------------------
static const uint32_t HEARTBEAT_INTERVAL_MS = 5000;
static const uint32_t WIFI_RETRY_INTERVAL_MS = 10000;
static const uint32_t HTTP_CONNECT_TIMEOUT_MS = 2500;
static const uint32_t HTTP_READ_TIMEOUT_MS = 3000;
static const uint32_t LOOP_IDLE_MS = 20;

// --------------------------- STATUS LEDS ---------------------------
// Optional external LEDs: GPIO -> 220/330-ohm resistor -> LED anode;
// LED cathode -> GND. Do not connect LEDs without resistors.
static const uint8_t LED_WIFI = 25;
static const uint8_t LED_BLE  = 26;
static const uint8_t LED_OK   = 27;
static const uint8_t LED_ERR  = 14;

BLEScan* scanner = nullptr;
uint32_t lastHeartbeatMs = 0;
uint32_t lastWiFiAttemptMs = 0;
uint32_t lastHttpSuccessMs = 0;
uint32_t successfulPosts = 0;
uint32_t failedPosts = 0;

static void led(uint8_t pin, bool on) {
  digitalWrite(pin, on ? HIGH : LOW);
}

static String endpointUrl(const char* endpoint) {
  String url;
  url.reserve(80);
  url = F("http://");
  url += SERVER_IP;
  url += ':';
  url += String(SERVER_PORT);
  url += endpoint;
  return url;
}

static bool connectWiFi(uint32_t timeoutMs = 18000) {
  Serial.printf("\n[%s] Connecting to Wi-Fi SSID: %s\n", NODE_ID, WIFI_SSID);
  led(LED_WIFI, false);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);

  // Do not lock to a BSSID or channel: campus AP/channel selection may vary.
  WiFi.disconnect(false, false);
  delay(150);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  const uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED &&
         (uint32_t)(millis() - started) < timeoutMs) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("[%s] Wi-Fi connect timeout; status=%d\n", NODE_ID, (int)WiFi.status());
    led(LED_WIFI, false);
    led(LED_ERR, true);
    return false;
  }

  led(LED_WIFI, true);
  led(LED_ERR, false);
  Serial.printf("[%s] Wi-Fi connected\n", NODE_ID);
  Serial.printf("  IP:      %s\n", WiFi.localIP().toString().c_str());
  Serial.printf("  Gateway: %s\n", WiFi.gatewayIP().toString().c_str());
  Serial.printf("  Mask:    %s\n", WiFi.subnetMask().toString().c_str());
  Serial.printf("  RSSI:    %d dBm\n", WiFi.RSSI());
  Serial.printf("  Channel: %d\n", WiFi.channel());
  Serial.printf("  BSSID:   %s\n", WiFi.BSSIDstr().c_str());
  return true;
}

static bool ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    led(LED_WIFI, true);
    return true;
  }

  led(LED_WIFI, false);
  const uint32_t now = millis();
  if ((uint32_t)(now - lastWiFiAttemptMs) < WIFI_RETRY_INTERVAL_MS) {
    return false;
  }

  lastWiFiAttemptMs = now;
  return connectWiFi();
}

static int postJson(const char* endpoint, const String& payload) {
  if (!ensureWiFi()) {
    Serial.printf("[%s] POST skipped: Wi-Fi is disconnected\n", NODE_ID);
    return -1;
  }

  const String url = endpointUrl(endpoint);
  int lastCode = -1;

  // Two attempts, with a short delay between attempts.
  for (uint8_t attempt = 1; attempt <= 2; ++attempt) {
    WiFiClient client;
    client.setTimeout(HTTP_READ_TIMEOUT_MS);

    HTTPClient http;
    http.setConnectTimeout(HTTP_CONNECT_TIMEOUT_MS);
    http.setTimeout(HTTP_READ_TIMEOUT_MS);
    http.setReuse(false);

    Serial.printf("[%s] POST %s (attempt %u)\n",
                  NODE_ID, url.c_str(), attempt);

    if (!http.begin(client, url)) {
      Serial.printf("[%s] HTTPClient begin failed\n", NODE_ID);
      lastCode = -1;
    } else {
      http.addHeader("Content-Type", "application/json");
      http.addHeader("Connection", "close");
      lastCode = http.POST((uint8_t*)payload.c_str(), payload.length());

      if (lastCode > 0) {
        const String response = http.getString();
        Serial.printf("[%s] HTTP %d | %s\n",
                      NODE_ID, lastCode, response.c_str());
        if (lastCode >= 200 && lastCode < 300) {
          ++successfulPosts;
          lastHttpSuccessMs = millis();
          led(LED_OK, true);
          led(LED_ERR, false);
          http.end();
          return lastCode;
        }
      } else {
        Serial.printf("[%s] HTTP transport error %d: %s\n",
                      NODE_ID, lastCode, http.errorToString(lastCode).c_str());
      }
      http.end();
    }

    if (attempt == 1) {
      delay(250);
      if (WiFi.status() != WL_CONNECTED) {
        WiFi.reconnect();
        delay(250);
      }
    }
  }

  ++failedPosts;
  led(LED_ERR, true);
  Serial.printf("[%s] POST failed. success=%lu failure=%lu\n",
                NODE_ID,
                (unsigned long)successfulPosts,
                (unsigned long)failedPosts);
  return lastCode;
}

static void sendHeartbeat() {
  if (!ensureWiFi()) return;

  String json;
  json.reserve(180);
  json = F("{\"node_id\":\"");
  json += NODE_ID;
  json += F("\",\"ip\":\"");
  json += WiFi.localIP().toString();
  json += F("\",\"wifi_rssi\":");
  json += String(WiFi.RSSI());
  json += F(",\"uptime_sec\":");
  json += String((uint32_t)(millis() / 1000UL));
  json += F(",\"free_heap\":");
  json += String((uint32_t)ESP.getFreeHeap());
  json += '}';

  postJson(API_HEARTBEAT, json);
}

static bool isRegisteredName(const String& name) {
  return name == DEVICE_1 || name == DEVICE_2;
}

static void sendDetection(const String& name, const String& mac, int rssi) {
  // Match the Flask server's /api/detection schema.
  String json;
  json.reserve(180);
  json = F("{\"node_id\":\"");
  json += NODE_ID;
  json += F("\",\"device_name\":\"");
  json += name;
  json += F("\",\"mac\":\"");
  json += mac;
  json += F("\",\"rssi\":");
  json += String(rssi);
  json += '}';

  postJson(API_DETECTION, json);
}

static void scanBLE() {
  if (scanner == nullptr) return;

  led(LED_BLE, true);
  Serial.printf("\n[%s] BLE scan starting (%lu s)\n",
                NODE_ID, (unsigned long)BLE_SCAN_SECONDS);

  BLEScanResults* results = scanner->start(BLE_SCAN_SECONDS, false);
  if (results == nullptr) {
    Serial.printf("[%s] BLE scan returned null results\n", NODE_ID);
    led(LED_BLE, false);
    return;
  }

  const int count = results->getCount();
  Serial.printf("[%s] BLE advertisements found: %d\n", NODE_ID, count);

  for (int i = 0; i < count; ++i) {
    BLEAdvertisedDevice dev = results->getDevice(i);
    const String name = dev.haveName() ? String(dev.getName().c_str()) : String("");
    const String mac = String(dev.getAddress().toString().c_str());
    const int rssi = dev.getRSSI();

    if (name.length()) {
      Serial.printf("[%s] %s | %s | RSSI %d dBm\n",
                    NODE_ID, name.c_str(), mac.c_str(), rssi);
    }

    if (isRegisteredName(name)) {
      Serial.printf("[%s] Registered test device detected: %s\n",
                    NODE_ID, name.c_str());
      sendDetection(name, mac, rssi);
    }
    // Yield briefly so Wi-Fi and background tasks can run.
    delay(5);
  }

  scanner->clearResults();
  led(LED_BLE, false);
}

void setup() {
  Serial.begin(115200);
  delay(700);

  pinMode(LED_WIFI, OUTPUT);
  pinMode(LED_BLE, OUTPUT);
  pinMode(LED_OK, OUTPUT);
  pinMode(LED_ERR, OUTPUT);
  led(LED_WIFI, false);
  led(LED_BLE, false);
  led(LED_OK, false);
  led(LED_ERR, false);

  Serial.println("\n==============================================");
  Serial.printf("  %s - BLE Classroom Presence Node\n", NODE_ID);
  Serial.println("  BLE scan -> Wi-Fi -> Flask HTTP API");
  Serial.println("==============================================");

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);
  lastWiFiAttemptMs = millis();
  connectWiFi();

  BLEDevice::init("");
  scanner = BLEDevice::getScan();
  scanner->setActiveScan(true);
  scanner->setInterval(100);
  scanner->setWindow(80);
  scanner->setDuplicateFilter(false);

  Serial.printf("[%s] Setup complete. Serial Monitor: 115200 baud.\n", NODE_ID);
}

void loop() {
  ensureWiFi();

  const uint32_t now = millis();
  if ((uint32_t)(now - lastHeartbeatMs) >= HEARTBEAT_INTERVAL_MS) {
    lastHeartbeatMs = now;
    sendHeartbeat();
  }

  scanBLE();

  // Since scanning is blocking, send a heartbeat as soon as scan finishes
  // if the interval elapsed during the scan.
  if ((uint32_t)(millis() - lastHeartbeatMs) >= HEARTBEAT_INTERVAL_MS) {
    lastHeartbeatMs = millis();
    sendHeartbeat();
  }

  delay(LOOP_IDLE_MS);
}
