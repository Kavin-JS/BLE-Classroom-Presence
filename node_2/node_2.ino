#include <WiFi.h>
#include <HTTPClient.h>
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>

// ============================================================
// NODE CONFIGURATION
// ============================================================

#define NODE_ID "Node_2"

// ============================================================
// WIFI CONFIGURATION
// ============================================================

const char* WIFI_SSID     = "Amrita_CHN2";
const char* WIFI_PASSWORD = "amrita@321";

// Laptop running Flask
const char* SERVER_IP = "11.12.10.198";
const uint16_t SERVER_PORT = 5000;

// Known 2.4 GHz Amrita_CHN2 AP
// Channel: 13
// BSSID:  E4:D1:24:2E:24:01
const uint8_t WIFI_BSSID[] = {
  0xE4,
  0xD1,
  0x24,
  0x2E,
  0x24,
  0x01
};

const uint8_t WIFI_CHANNEL = 13;

// ============================================================
// BLE CONFIGURATION
// ============================================================

const uint16_t BLE_SCAN_TIME = 4;

// Registered BLE advertising names
const char* DEVICE_1 = "ATTEND-KAVIN";
const char* DEVICE_2 = "ATTEND-LOHITH";

// ============================================================
// TIMING
// ============================================================

const unsigned long HEARTBEAT_INTERVAL = 5000;
const unsigned long WIFI_RETRY_INTERVAL = 5000;

// ============================================================
// HTTP
// ============================================================

const uint16_t HTTP_CONNECT_TIMEOUT = 4000;
const uint16_t HTTP_RESPONSE_TIMEOUT = 6000;

// ============================================================
// LED PINS
// ============================================================

#define LED_WIFI 25
#define LED_BLE  26
#define LED_OK   27
#define LED_ERR  14

// ============================================================
// GLOBALS
// ============================================================

BLEScan* pBLEScan = nullptr;

unsigned long lastHeartbeat = 0;
unsigned long lastWiFiRetry = 0;

// ============================================================
// LED FUNCTIONS
// ============================================================

void setWiFiLED(bool state) {
  digitalWrite(LED_WIFI, state ? HIGH : LOW);
}

void setBLELED(bool state) {
  digitalWrite(LED_BLE, state ? HIGH : LOW);
}

void setOKLED(bool state) {
  digitalWrite(LED_OK, state ? HIGH : LOW);
}

void setErrorLED(bool state) {
  digitalWrite(LED_ERR, state ? HIGH : LOW);
}

// ============================================================
// WIFI CONNECTION
// ============================================================

bool connectWiFi() {

  Serial.println();
  Serial.println("========================================");
  Serial.printf("[%s] Connecting to %s\n", NODE_ID, WIFI_SSID);
  Serial.println("Target: 2.4 GHz / Channel 13");
  Serial.println("BSSID: E4:D1:24:2E:24:01");
  Serial.println("========================================");

  WiFi.mode(WIFI_STA);

  // Disable modem sleep for more stable communication
  WiFi.setSleep(false);

  WiFi.setAutoReconnect(true);

  // Do not save connection settings to flash
  WiFi.persistent(false);

  WiFi.disconnect(false, false);
  delay(150);

  WiFi.begin(
    WIFI_SSID,
    WIFI_PASSWORD,
    WIFI_CHANNEL,
    WIFI_BSSID,
    true
  );

  unsigned long startTime = millis();

  while (
    WiFi.status() != WL_CONNECTED &&
    millis() - startTime < 20000
  ) {

    delay(250);
    Serial.print(".");
  }

  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {

    Serial.println();
    Serial.println("========================================");
    Serial.printf("[%s] Wi-Fi CONNECTED\n", NODE_ID);
    Serial.println("========================================");

    Serial.print("IP:       ");
    Serial.println(WiFi.localIP());

    Serial.print("Gateway:  ");
    Serial.println(WiFi.gatewayIP());

    Serial.print("Subnet:   ");
    Serial.println(WiFi.subnetMask());

    Serial.print("RSSI:     ");
    Serial.print(WiFi.RSSI());
    Serial.println(" dBm");

    Serial.print("Channel:  ");
    Serial.println(WiFi.channel());

    Serial.print("BSSID:    ");
    Serial.println(WiFi.BSSIDstr());

    setWiFiLED(true);

    return true;
  }

  Serial.println();
  Serial.printf(
    "[%s] Wi-Fi connection FAILED\n",
    NODE_ID
  );

  Serial.print("Wi-Fi status: ");
  Serial.println(WiFi.status());

  setWiFiLED(false);

  return false;
}

// ============================================================
// ENSURE WIFI
// ============================================================

bool ensureWiFi() {

  if (WiFi.status() == WL_CONNECTED) {
    setWiFiLED(true);
    return true;
  }

  setWiFiLED(false);

  if (
    millis() - lastWiFiRetry <
    WIFI_RETRY_INTERVAL
  ) {
    return false;
  }

  lastWiFiRetry = millis();

  return connectWiFi();
}

// ============================================================
// SERVER URL
// ============================================================

String getServerURL(const char* endpoint) {

  String url;

  url.reserve(64);

  url += "http://";
  url += SERVER_IP;
  url += ":";
  url += String(SERVER_PORT);
  url += endpoint;

  return url;
}

// ============================================================
// HTTP POST
// ============================================================

int sendPOST(
  const char* endpoint,
  const String& json
) {

  if (!ensureWiFi()) {
    return -1;
  }

  String url = getServerURL(endpoint);

  Serial.print("[");
  Serial.print(NODE_ID);
  Serial.print("] POST ");
  Serial.println(url);

  WiFiClient client;

  HTTPClient http;

  http.setConnectTimeout(
    HTTP_CONNECT_TIMEOUT
  );

  http.setTimeout(
    HTTP_RESPONSE_TIMEOUT
  );

  // Use HTTP/1.0 to avoid persistent connections
  // accumulating on a small embedded client.
  http.useHTTP10(true);

  if (!http.begin(client, url)) {

    Serial.printf(
      "[%s] HTTP begin failed\n",
      NODE_ID
    );

    setErrorLED(true);

    return -1;
  }

  http.addHeader(
    "Content-Type",
    "application/json"
  );

  int httpCode = http.POST(json);

  if (httpCode > 0) {

    Serial.print("[");
    Serial.print(NODE_ID);
    Serial.print("] HTTP response: ");
    Serial.println(httpCode);

    if (
      httpCode >= 200 &&
      httpCode < 300
    ) {

      setOKLED(true);
      setErrorLED(false);

    } else {

      setErrorLED(true);
    }

  } else {

    Serial.print("[");
    Serial.print(NODE_ID);
    Serial.print("] HTTP POST failed, code=");
    Serial.println(httpCode);

    setErrorLED(true);
  }

  http.end();

  return httpCode;
}

// ============================================================
// HEARTBEAT
// ============================================================

void sendHeartbeat() {

  if (!ensureWiFi()) {
    return;
  }

  String json;

  json.reserve(128);

  json += "{";
  json += "\"node_id\":\"";
  json += NODE_ID;
  json += "\",";

  json += "\"ip\":\"";
  json += WiFi.localIP().toString();
  json += "\",";

  json += "\"wifi_rssi\":";
  json += String(WiFi.RSSI());

  json += "}";

  sendPOST(
    "/api/heartbeat",
    json
  );
}

// ============================================================
// REGISTERED DEVICE CHECK
// ============================================================

bool isRegisteredDevice(
  const String& name
) {

  return (
    name == DEVICE_1 ||
    name == DEVICE_2
  );
}

// ============================================================
// BLE DETECTION UPLOAD
// ============================================================

void sendDetection(
  const String& deviceName,
  const String& mac,
  int rssi
) {

  String json;

  json.reserve(180);

  json += "{";

  json += "\"node_id\":\"";
  json += NODE_ID;
  json += "\",";

  json += "\"device_name\":\"";
  json += deviceName;
  json += "\",";

  json += "\"mac\":\"";
  json += mac;
  json += "\",";

  json += "\"rssi\":";
  json += String(rssi);

  json += "}";

  sendPOST(
    "/api/detection",
    json
  );
}

// ============================================================
// BLE SCAN
// ============================================================

void performBLEScan() {

  if (pBLEScan == nullptr) {
    return;
  }

  Serial.println();
  Serial.printf(
    "[%s] Starting BLE scan...\n",
    NODE_ID
  );

  setBLELED(true);

  BLEScanResults* results =
    pBLEScan->start(
      BLE_SCAN_TIME,
      false
    );

  if (results == nullptr) {

    Serial.printf(
      "[%s] BLE scan returned no results.\n",
      NODE_ID
    );

    setBLELED(false);

    return;
  }

  int deviceCount =
    results->getCount();

  Serial.printf(
    "[%s] BLE devices found: %d\n",
    NODE_ID,
    deviceCount
  );

  for (
    int i = 0;
    i < deviceCount;
    i++
  ) {

    BLEAdvertisedDevice device =
      results->getDevice(i);

    String name = "";

    if (device.haveName()) {
      name =
        device.getName().c_str();
    }

    String mac =
      device.getAddress()
           .toString()
           .c_str();

    int rssi =
      device.getRSSI();

    Serial.print("[");
    Serial.print(NODE_ID);
    Serial.print("] BLE: ");

    if (name.length() > 0) {
      Serial.print(name);
    } else {
      Serial.print("(no name)");
    }

    Serial.print(" | ");
    Serial.print(mac);

    Serial.print(" | RSSI: ");
    Serial.print(rssi);
    Serial.println(" dBm");

    // --------------------------------------------------------
    // REGISTERED DEVICE
    // --------------------------------------------------------

    if (
      name.length() > 0 &&
      isRegisteredDevice(name)
    ) {

      Serial.println();
      Serial.printf(
        "[%s] REGISTERED DEVICE DETECTED\n",
        NODE_ID
      );

      Serial.print("Device: ");
      Serial.println(name);

      Serial.print("MAC:    ");
      Serial.println(mac);

      Serial.print("RSSI:   ");
      Serial.print(rssi);
      Serial.println(" dBm");

      sendDetection(
        name,
        mac,
        rssi
      );
    }
  }

  pBLEScan->clearResults();

  setBLELED(false);
}

// ============================================================
// SETUP
// ============================================================

void setup() {

  Serial.begin(115200);

  delay(1000);

  // ----------------------------------------------------------
  // LED SETUP
  // ----------------------------------------------------------

  pinMode(
    LED_WIFI,
    OUTPUT
  );

  pinMode(
    LED_BLE,
    OUTPUT
  );

  pinMode(
    LED_OK,
    OUTPUT
  );

  pinMode(
    LED_ERR,
    OUTPUT
  );

  setWiFiLED(false);
  setBLELED(false);
  setOKLED(false);
  setErrorLED(false);

  // ----------------------------------------------------------
  // STARTUP MESSAGE
  // ----------------------------------------------------------

  Serial.println();
  Serial.println("========================================");
  Serial.printf(
    "        %s\n",
    NODE_ID
  );
  Serial.println(
    "Multi-Node BLE Classroom Presence"
  );
  Serial.println("========================================");

  // ----------------------------------------------------------
  // WIFI
  // ----------------------------------------------------------

  connectWiFi();

  // ----------------------------------------------------------
  // BLE
  // ----------------------------------------------------------

  BLEDevice::init("");

  pBLEScan =
    BLEDevice::getScan();

  pBLEScan->setActiveScan(true);

  pBLEScan->setInterval(100);

  pBLEScan->setWindow(80);

  Serial.println();

  Serial.printf(
    "[%s] Setup complete.\n",
    NODE_ID
  );

  Serial.printf(
    "[%s] Starting scan loop...\n",
    NODE_ID
  );
}

// ============================================================
// MAIN LOOP
// ============================================================

void loop() {

  // ----------------------------------------------------------
  // WIFI
  // ----------------------------------------------------------

  ensureWiFi();

  // ----------------------------------------------------------
  // HEARTBEAT
  // ----------------------------------------------------------

  if (
    millis() - lastHeartbeat >=
    HEARTBEAT_INTERVAL
  ) {

    lastHeartbeat = millis();

    sendHeartbeat();
  }

  // ----------------------------------------------------------
  // BLE
  // ----------------------------------------------------------

  performBLEScan();

  // Small delay prevents a tight loop
  delay(500);
}