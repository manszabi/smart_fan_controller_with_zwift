#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <OneButton.h>
#include "esp_sleep.h"
#include <WiFi.h>
#include <AsyncTCP.h>
#include <ESPAsyncWebServer.h>
#include <WebSerial.h>
#include <SPIFFS.h>
#include <ArduinoJson.h>
#include <ElegantOTA.h>
#include <nvs.h>
#include <nvs_flash.h>

// ============================================================
// VERZIÓ INFORMÁCIÓ
// ============================================================
#define FIRMWARE_VERSION "5.2.0"
#define FIRMWARE_DATE "2026-03-03"

// ============================================================
// PIN KIOSZTÁS
// ============================================================
#define RELAY_FAN1 10
#define RELAY_FAN2 9
#define RELAY_FAN3 8
#define RELAY_ROLLER 2
#define RELAY_EN 21
#define BUTTON_PIN 3
#define LED_YELLOW 5
#define LED_RED 4

// ============================================================
// IDŐZÍTÉSEK
// ============================================================
const unsigned long INACTIVITY_MS = 1800000;
const unsigned long RELAY_SWITCH_DELAY_MS = 100;
const unsigned long LED_BLINK_INTERVAL = 500;
const unsigned long HEARTBEAT_INTERVAL = 2000;
const unsigned long HEARTBEAT_PULSE = 100;
const unsigned long BLE_RESTART_DELAY = 500;
const unsigned long WIFI_CONNECT_TIMEOUT = 20000;
// Debounce védelem
unsigned long lastDoubleClickTime = 0;
const unsigned long DOUBLE_CLICK_COOLDOWN = 1000;  // 1 sec cooldown
// AP mód timeout
bool apMode = false;
unsigned long apStartTime = 0;
const unsigned long AP_TIMEOUT_MS = 180000;
volatile bool zoneChanging = false;  // GLOBALIS VALTOZO - tedd a tobbi melle!
// BLE nelkuli zona timeout
unsigned long bleDisconnectTime = 0;
const unsigned long BLE_ZONE_TIMEOUT_MS = 600000;  // 10 perc
// WiFi STA mód timeout
unsigned long wifiStartTime = 0;
const unsigned long WIFI_STA_TIMEOUT_MS = 180000;
bool wifiTimeoutDisabled = false;  // WebSerial "notimeout" paranccsal tiltható
bool wifiStopRequested = false;   // WebSerial "wifistop" parancs flag
bool wifiStopPending = false;     // WiFi leállítás folyamatban (millis késleltetéssel)
unsigned long wifiStopRequestTime = 0;  // A kérés időpontja
// Deep sleep visszaszámláló
unsigned long lastSleepCountdown = 0;
const unsigned long SLEEP_COUNTDOWN_INTERVAL = 30000;  // 0,5 percenként

// ============================================================
// BLE UUIDs
// ============================================================
#define SERVICE_UUID "0000ffe0-0000-1000-8000-00805f9b34fb"
#define CHARACTERISTIC_UUID "0000ffe1-0000-1000-8000-00805f9b34fb"

// ============================================================
// BLE AUTH
// ============================================================
#define BLE_AUTH_PIN "123456"
#define MAX_AUTH_ATTEMPTS 5
#define AUTH_LOCKOUT_TIME_MS 60000  // 60 sec lockout

bool isAuthenticated = false;
int authAttempts = 0;
unsigned long lockoutStart = 0;  // millis() when lockout began (0 = not locked)

// ============================================================
// WiFi KONFIGURÁCIÓ
// ============================================================
struct WiFiConfig {
  char ssid[33] = "wifi";
  char password[65] = "0123456789";
  IPAddress ip = IPAddress(192, 168, 0, 100);
  IPAddress gateway = IPAddress(192, 168, 0, 1);
  IPAddress subnet = IPAddress(255, 255, 255, 0);
};

WiFiConfig wifiConfig;
bool wifiConnected = false;

const char* AP_SSID = "ESP32-Setup";
const char* AP_PASSWORD = "12345678";
const IPAddress AP_IP(192, 168, 4, 1);

// ============================================================
// WEB SZERVER
// ============================================================
AsyncWebServer server(80);
AsyncWebSocket ws("/ws");

// ============================================================
// GLOBÁLIS VÁLTOZÓK
// ============================================================
BLEServer* pServer = NULL;
BLECharacteristic* pCharacteristic = NULL;
bool bleConnected = false;
bool bleEnabled = true;

OneButton button(BUTTON_PIN, true, true);

int currentZone = 0;
int manualZoneIndex = 0;
bool rollerActive = false;
bool relaysEnabled = false;
bool manualMode = false;
bool resetMode = false;

unsigned long lastActivityTime = 0;
unsigned long lastRedToggle = 0;
unsigned long lastYellowToggle = 0;
unsigned long lastHeartbeat = 0;
unsigned long lastHeartbeat_red = 0;
bool redLedState = false;
bool yellowLedState = false;
bool heartbeatPulse = false;
bool heartbeatPulse_red = false;
bool bleNeedsRestart = false;
unsigned long bleRestartTime = 0;

static unsigned long ota_progress_millis = 0;
static bool otaInProgress = false;

// Boot számolás
//RTC_DATA_ATTR int bootCount = 0;  // Megmarad deep sleep alatt
RTC_NOINIT_ATTR int bootCount = 0;
// ============================================================
// PARANCS FORRÁS PRIORITÁS
// ============================================================
enum CommandSource {
  SRC_NONE,
  SRC_BUTTON,    // Legmagasabb prioritás (fizikai)
  SRC_BLE,       // Közepes prioritás
  SRC_WEBSERIAL  // Legalacsonyabb prioritás
};

CommandSource activeSource = SRC_NONE;
unsigned long sourceLockedUntil = 0;
const unsigned long SOURCE_LOCK_MS = 2000;  // 2 mp-ig tartja a forrást

// ============================================================
// SAFE WEBSERIAL PRINT (csak ha WiFi csatlakozva)
// ============================================================
void wsPrint(const char* msg) {
  if (wifiConnected) WebSerial.print(msg);
}

void wsPrint(const String& msg) {
  if (wifiConnected) WebSerial.print(msg);
}

void wsPrint(int val) {
  if (wifiConnected) WebSerial.print(val);
}

void wsPrintln(const char* msg) {
  if (wifiConnected) WebSerial.println(msg);
}

void wsPrintln(const String& msg) {
  if (wifiConnected) WebSerial.println(msg);
}

void wsPrintln(int val) {
  if (wifiConnected) WebSerial.println(val);
}

void wsPrintln() {
  if (wifiConnected) WebSerial.println();
}

// ============================================================
// SERIAL + WEBSERIAL EGYBEN
// ============================================================
void logPrint(const char* msg) {
  Serial.print(msg);
  wsPrint(msg);
}

void logPrint(const String& msg) {
  Serial.print(msg);
  wsPrint(msg);
}

void logPrint(int val) {
  Serial.print(val);
  wsPrint(val);
}

void logPrintln(const char* msg) {
  Serial.println(msg);
  wsPrintln(msg);
}

void logPrintln(const String& msg) {
  Serial.println(msg);
  wsPrintln(msg);
}

void logPrintln(int val) {
  Serial.println(val);
  wsPrintln(val);
}

void logPrintln() {
  Serial.println();
  wsPrintln();
}

// ============================================================
// FORWARD DECLARATIONS
// ============================================================
void setFanZone(int zone, CommandSource source = SRC_NONE);
void switchOnZone(int zone);
void activateRoller(CommandSource source = SRC_NONE);
void deactivateRoller(CommandSource source = SRC_NONE);
void enableRelays();
void disableRelays();
void handleLEDs(unsigned long currentMillis);
void enterDeepSleep();
void updateActivityTime();
bool loadWiFiConfig();
void saveWiFiConfig();
void setupWiFiSTA();
void setupWiFiAP();
void setupWebServer();
void setupConfigPortal();
void recvMsg(uint8_t* data, size_t len);
void onOTAStart();
void onOTAProgress(size_t current, size_t final_size);
void onOTAEnd(bool success);
void handleClick();
void handleLongPressStop();
void handleDoubleClick();
void handleMultiClick();

// ============================================================
// WebSerial CALLBACK
// ============================================================
void recvMsg(uint8_t* data, size_t len) {
  WebSerial.println("Received data...");
  char buf[64];
  size_t copyLen = (len < sizeof(buf) - 1) ? len : sizeof(buf) - 1;
  memcpy(buf, data, copyLen);
  buf[copyLen] = '\0';
  WebSerial.println(buf);

  if (strcmp(buf, "help") == 0) {
    WebSerial.println("Parancsok: help, off, reboot, status, zone0, zone1, zone2, zone3, rolleron, rolleroff, notimeout, wifistop");
  } else if (strcmp(buf, "off") == 0) {
    enterDeepSleep();
  } else if (strcmp(buf, "reboot") == 0) {
    bootCount = 98;
    ESP.restart();
  } else if (strcmp(buf, "status") == 0) {
    WebSerial.println("========== STATUS ==========");
    // Firmware
    WebSerial.print("Firmware: v");
    WebSerial.print(FIRMWARE_VERSION);
    WebSerial.print(" (");
    WebSerial.print(FIRMWARE_DATE);
    WebSerial.println(")");
    // Uptime
    unsigned long uptimeSec = millis() / 1000;
    WebSerial.print("Uptime: ");
    WebSerial.print(uptimeSec / 3600);
    WebSerial.print("h ");
    WebSerial.print((uptimeSec % 3600) / 60);
    WebSerial.print("m ");
    WebSerial.print(uptimeSec % 60);
    WebSerial.println("s");
    // Boot count
    WebSerial.print("Boot count: ");
    WebSerial.println(bootCount);
    // Zone + Roller
    WebSerial.print("Zone: ");
    WebSerial.println(currentZone);
    WebSerial.print("Roller: ");
    WebSerial.println(rollerActive ? "ON" : "OFF");
    WebSerial.print("Manual mode: ");
    WebSerial.println(manualMode ? "IGEN" : "NEM");
    WebSerial.print("Relays enabled: ");
    WebSerial.println(relaysEnabled ? "IGEN" : "NEM");
    // Parancs forrás
    WebSerial.print("Active source: ");
    WebSerial.println(activeSource == SRC_BUTTON ? "Gomb" : (activeSource == SRC_BLE ? "BLE" : (activeSource == SRC_WEBSERIAL ? "WebSerial" : "None")));
    WebSerial.print("Source locked: ");
    WebSerial.println(millis() < sourceLockedUntil ? "IGEN" : "NEM");
    // BLE
    WebSerial.print("BLE: ");
    WebSerial.println(bleConnected ? "Connected" : "Disconnected");
    WebSerial.print("BLE enabled: ");
    WebSerial.println(bleEnabled ? "IGEN" : "NEM");
    WebSerial.print("BLE Auth: ");
    WebSerial.println(strlen(BLE_AUTH_PIN) > 0 ? "ON" : "OFF");
    WebSerial.print("BLE Authenticated: ");
    WebSerial.println(isAuthenticated ? "YES" : "NO");
    WebSerial.print("Auth attempts: ");
    WebSerial.print(authAttempts);
    WebSerial.print("/");
    WebSerial.println(MAX_AUTH_ATTEMPTS);
    // WiFi
    WebSerial.print("WiFi: ");
    WebSerial.println(WiFi.localIP());
    WebSerial.print("WiFi RSSI: ");
    WebSerial.print(WiFi.RSSI());
    WebSerial.println(" dBm");
    WebSerial.print("WiFi timeout: ");
    WebSerial.println(wifiTimeoutDisabled ? "TILTVA" : "AKTIV");
    // WiFi timeout visszaszámláló
    if (!wifiTimeoutDisabled) {
      unsigned long wifiElapsed = millis() - wifiStartTime;
      if (wifiElapsed < WIFI_STA_TIMEOUT_MS) {
        unsigned long wifiRemain = (WIFI_STA_TIMEOUT_MS - wifiElapsed) / 1000;
        WebSerial.print("WiFi leall: ");
        WebSerial.print(wifiRemain / 60);
        WebSerial.print("m ");
        WebSerial.print(wifiRemain % 60);
        WebSerial.println("s mulva");
      }
    }
    WebSerial.print("OTA in progress: ");
    WebSerial.println(otaInProgress ? "IGEN" : "NEM");
    // Deep sleep visszaszámláló
    unsigned long sleepElapsed = millis() - lastActivityTime;
    if (sleepElapsed < INACTIVITY_MS) {
      unsigned long sleepRemain = (INACTIVITY_MS - sleepElapsed) / 1000;
      WebSerial.print("Deep sleep: ");
      WebSerial.print(sleepRemain / 60);
      WebSerial.print("m ");
      WebSerial.print(sleepRemain % 60);
      WebSerial.println("s mulva");
    }
    // Memória
    WebSerial.print("Free heap: ");
    WebSerial.println(ESP.getFreeHeap());
    WebSerial.println("============================");
  } else if (strcmp(buf, "zone0") == 0) {
    setFanZone(0, SRC_WEBSERIAL);
  } else if (strcmp(buf, "zone1") == 0) {
    setFanZone(1, SRC_WEBSERIAL);
  } else if (strcmp(buf, "zone2") == 0) {
    setFanZone(2, SRC_WEBSERIAL);
  } else if (strcmp(buf, "zone3") == 0) {
    setFanZone(3, SRC_WEBSERIAL);
  } else if (strcmp(buf, "rolleron") == 0) {
    activateRoller(SRC_WEBSERIAL);
  } else if (strcmp(buf, "rolleroff") == 0) {
    deactivateRoller(SRC_WEBSERIAL);
  } else if (strcmp(buf, "notimeout") == 0) {
    wifiTimeoutDisabled = true;
    WebSerial.println("WiFi STA timeout TILTVA - WiFi nem fog leallni");
    Serial.println(F("WebSerial: WiFi timeout tiltva"));
  } else if (strcmp(buf, "wifistop") == 0) {
    WebSerial.println("WiFi leallitas kerelmezes...");
    Serial.println(F("WebSerial: WiFi manualis leallitas kerelmezes"));
    wifiStopRequested = true;
  }
}

// ============================================================
// OTA CALLBACKS
// ============================================================
void onOTAStart() {
  otaInProgress = true;
  logPrintln("OTA update started!");
}

void onOTAProgress(size_t current, size_t final_size) {
  otaInProgress = true;
  if (millis() - ota_progress_millis > 1000) {
    ota_progress_millis = millis();
    Serial.printf("OTA Progress: %u/%u bytes\n", current, final_size);
  }
}

void onOTAEnd(bool success) {
  if (success) {
    logPrintln("OTA update finished!");
  } else {
    logPrintln("OTA update error!");
  }
  delay(2000);
  otaInProgress = false;
  bootCount = 98;
  ESP.restart();
}

// ============================================================
// SPIFFS - WiFi KONFIG MENTÉS (kézi JSON, kevesebb memória)
// ============================================================
void saveWiFiConfig() {
  Serial.println(F("WiFi konfig mentes..."));

  File file = SPIFFS.open("/config.json", "w");
  if (!file) {
    Serial.println(F("HIBA: config.json nem irhato!"));
    return;
  }

  file.print("{");
  file.print("\"ssid\":\"");
  file.print(wifiConfig.ssid);
  file.print("\",");
  file.print("\"password\":\"");
  file.print(wifiConfig.password);
  file.print("\",");
  file.print("\"ip\":\"");
  file.print(wifiConfig.ip.toString());
  file.print("\",");
  file.print("\"gateway\":\"");
  file.print(wifiConfig.gateway.toString());
  file.print("\",");
  file.print("\"subnet\":\"");
  file.print(wifiConfig.subnet.toString());
  file.print("\"");
  file.print("}");
  file.close();

  Serial.println(F("WiFi konfig mentve!"));
}

// ============================================================
// SPIFFS - WiFi KONFIG BETÖLTÉS
// ============================================================
bool loadWiFiConfig() {
  if (!SPIFFS.exists("/config.json")) {
    Serial.println(F("config.json nem letezik, alapertelmezett mentes"));
    saveWiFiConfig();
    return true;
  }

  File file = SPIFFS.open("/config.json", "r");
  if (!file) {
    Serial.println(F("HIBA: config.json nem olvashato!"));
    return false;
  }

  JsonDocument doc;
  DeserializationError error = deserializeJson(doc, file);
  file.close();

  if (error) {
    Serial.print(F("JSON hiba: "));
    Serial.println(error.c_str());
    return false;
  }

  strncpy(wifiConfig.ssid, doc["ssid"] | "kisszoba", sizeof(wifiConfig.ssid) - 1);
  wifiConfig.ssid[sizeof(wifiConfig.ssid) - 1] = '\0';
  strncpy(wifiConfig.password, doc["password"] | "csemegi@", sizeof(wifiConfig.password) - 1);
  wifiConfig.password[sizeof(wifiConfig.password) - 1] = '\0';

  String ipStr = doc["ip"] | "192.168.0.100";
  String gatewayStr = doc["gateway"] | "192.168.0.1";
  String subnetStr = doc["subnet"] | "255.255.255.0";

  wifiConfig.ip.fromString(ipStr);
  wifiConfig.gateway.fromString(gatewayStr);
  wifiConfig.subnet.fromString(subnetStr);

  Serial.print(F("WiFi konfig betoltve - SSID: "));
  Serial.println(wifiConfig.ssid);

  return true;
}

// ============================================================
// WiFi STA MÓD
// ============================================================
void setupWiFiSTA() {
  Serial.println(F("WiFi STA mod - Csatlakozas"));

  WiFi.mode(WIFI_STA);
  WiFi.config(wifiConfig.ip, wifiConfig.gateway, wifiConfig.subnet);

  Serial.print(F("Csatlakozas: "));
  Serial.println(wifiConfig.ssid);

  WiFi.begin(wifiConfig.ssid, wifiConfig.password);

  unsigned long startAttempt = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startAttempt < WIFI_CONNECT_TIMEOUT) {
    delay(500);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED) {
    wifiConnected = true;
    wifiStartTime = millis();  // <-- TIMEOUT SZAMLALO INDUL
    Serial.println();
    Serial.println(F("WiFi csatlakozva!"));
    Serial.print(F("  IP: "));
    Serial.println(WiFi.localIP());
    Serial.print(F("  Signal: "));
    Serial.print(WiFi.RSSI());
    Serial.println(F(" dBm"));
    Serial.print(F("  WiFi timeout: "));
    Serial.print(WIFI_STA_TIMEOUT_MS / 60000);
    Serial.println(F(" perc"));
  } else {
    wifiConnected = false;
    Serial.println();
    Serial.println(F("WiFi csatlakozas sikertelen! AP Fallback..."));
    WiFi.disconnect(true);
    delay(100);
    setupWiFiAP();
  }
}

// ============================================================
// WiFi AP MÓD (Fallback)
// ============================================================
void setupWiFiAP() {
  Serial.println(F("WiFi AP mod - Config Portal"));

  WiFi.mode(WIFI_AP);
  WiFi.softAPConfig(AP_IP, AP_IP, IPAddress(255, 255, 255, 0));
  WiFi.softAP(AP_SSID, AP_PASSWORD);

  apMode = true;
  apStartTime = millis();

  Serial.print(F("AP SSID: "));
  Serial.println(AP_SSID);
  Serial.print(F("AP IP: "));
  Serial.println(WiFi.softAPIP());
  Serial.print(F("AP timeout: "));
  Serial.print(AP_TIMEOUT_MS / 60000);
  Serial.println(F(" perc"));
}

// ============================================================
// WEB SZERVER SETUP
// ============================================================
void setupWebServer() {
  if (wifiConnected) {
    // STA MÓD: WebSocket + WebSerial + ElegantOTA

    ws.onEvent([](AsyncWebSocket* server, AsyncWebSocketClient* client,
                  AwsEventType type, void* arg, uint8_t* data, size_t len) {
      if (type == WS_EVT_CONNECT) {
        Serial.println(F("WebSocket kliens csatlakozott"));
      } else if (type == WS_EVT_DISCONNECT) {
        Serial.println(F("WebSocket kliens szétkapcsolodott"));
      }
    });
    server.addHandler(&ws);

    // Főoldal
    server.on("/", HTTP_GET, [](AsyncWebServerRequest* request) {
      request->send(200, "text/html",
                    "<html><head><meta charset='UTF-8'></head>"
                    "<body style='font-family:monospace;background:#1e1e1e;color:#4ec9b0;padding:50px;text-align:center;'>"
                    "<h1>ESP32 Fan Controller v" FIRMWARE_VERSION "</h1>"
                    "<p><a href='/webserial' style='color:#569cd6;'>WebSerial</a> | "
                    "<a href='/update' style='color:#569cd6;'>OTA Update</a></p>"
                    "</body></html>");
    });

    // ElegantOTA
    ElegantOTA.begin(&server);
    ElegantOTA.onStart(onOTAStart);
    ElegantOTA.onProgress(onOTAProgress);
    ElegantOTA.onEnd(onOTAEnd);
    ElegantOTA.setAutoReboot(false);

    // WebSerial (CSAK STA módban!)
    WebSerial.begin(&server);
    WebSerial.onMessage(recvMsg);

    Serial.println(F("WebSerial es ElegantOTA elindítva!"));
    Serial.print(F("  WebSerial: http://"));
    Serial.print(WiFi.localIP());
    Serial.println(F("/webserial"));
    Serial.print(F("  OTA: http://"));
    Serial.print(WiFi.localIP());
    Serial.println(F("/update"));

  } else {
    // AP MÓD: Config Portal CSAK (nincs WebSerial!)
    Serial.println(F("Web szerver elindítva, Config Portal. (port 80)"));
    setupConfigPortal();
  }

  server.begin();
  Serial.println(F("Web szerver elindítva (port 80)"));
}

// ============================================================
// HTML - CONFIG PORTAL (PROGMEM)
// ============================================================
const char CONFIG_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>ESP32 WiFi Setup</title>
</head>
<body style="font-family:Arial; padding:20px;">
<h2>WiFi beállítás</h2>

<form action="/save" method="POST">

  <label>SSID:</label><br>
  <input type="text" name="ssid" value="%SSID%"><br><br>

  <label>Jelszó:</label><br>
  <input type="password" name="password" value="%PASSWORD%"><br><br>

  <label>IP cím:</label><br>
  <input type="text" name="ip" value="%IP%"><br><br>

  <label>Gateway:</label><br>
  <input type="text" name="gateway" value="%GATEWAY%"><br><br>

  <button type="submit">Mentés</button>
</form>

</body>
</html>
)rawliteral";

String configProcessor(const String& var) {
  if (var == "SSID") return String(wifiConfig.ssid);
  if (var == "PASSWORD") return String(wifiConfig.password);
  if (var == "IP") return wifiConfig.ip.toString();
  if (var == "GATEWAY") return wifiConfig.gateway.toString();
  return String();
}

// ============================================================
// CONFIG PORTAL (AP módban)
// ============================================================
void setupConfigPortal() {
  server.on("/", HTTP_GET, [](AsyncWebServerRequest* request) {
    request->send_P(200, "text/html", CONFIG_HTML, configProcessor);
  });

  server.on("/save", HTTP_POST, [](AsyncWebServerRequest* request) {
    if (request->hasParam("ssid", true) && request->hasParam("password", true) && request->hasParam("ip", true) && request->hasParam("gateway", true)) {

      strncpy(wifiConfig.ssid, request->getParam("ssid", true)->value().c_str(), sizeof(wifiConfig.ssid) - 1);
      strncpy(wifiConfig.password, request->getParam("password", true)->value().c_str(), sizeof(wifiConfig.password) - 1);
      wifiConfig.ip.fromString(request->getParam("ip", true)->value());
      wifiConfig.gateway.fromString(request->getParam("gateway", true)->value());

      saveWiFiConfig();

      request->send(200, "text/html",
                    "<html><body><h2>Mentve! Újraindítás...</h2></body></html>");

      delay(2000);
      bootCount = 98;
      ESP.restart();
    } else {
      request->send(400, "text/plain", "Hianyzo parameterek!");
    }
  });
}

// ============================================================
// BLE CALLBACKS
// ============================================================
class MyServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer* pServer) {
    bleConnected = true;
    Serial.println(F("BLE kliens csatlakozott"));
    wsPrintln("BLE kliens csatlakozott");
    bleDisconnectTime = 0;  // Timeout torles
  };

  void onDisconnect(BLEServer* pServer) {
    bleConnected = false;
    isAuthenticated = false;
    authAttempts = 0;
    lockoutStart = 0;
    bleDisconnectTime = millis();
    Serial.println(F("BLE kliens lecsatlakozott"));
    wsPrintln("BLE kliens lecsatlakozott");

    logPrint("  Zona marad: ");
    logPrintln(currentZone);
    logPrint("  Gorgo marad: ");
    logPrintln(rollerActive ? "BE" : "KI");

    if (bleEnabled) {
      bleNeedsRestart = true;
      bleRestartTime = 0;
    }
  }
};

class MyCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* pCharacteristic) {
    // Ellenőrzés: van-e még BLE kapcsolat
    if (!bleConnected) {
      logPrintln("BLE parancs elutasitva - nincs kapcsolat");
      return;
    }

    String val = pCharacteristic->getValue();
    val.trim();

    if (val.length() == 0) {
      return;
    }

    logPrint("BLE parancs: ");
    if (val.startsWith("AUTH:")) {
      logPrintln("AUTH:****");
    } else {
      logPrintln(val);
    }

    if (val.startsWith("AUTH:")) {
      // Lockout ellenőrzés (overflow-safe: elapsed time check)
      if (lockoutStart != 0 && millis() - lockoutStart < AUTH_LOCKOUT_TIME_MS) {
        logPrintln("BLE AUTH LOCKED - varj!");
        pCharacteristic->setValue("AUTH_LOCKED");
        pCharacteristic->notify();
        return;
      }

      String receivedPin = val.substring(5);
      String correctPin = BLE_AUTH_PIN;

      if (correctPin.length() == 0 || receivedPin == correctPin) {
        isAuthenticated = true;
        authAttempts = 0;
        logPrintln("BLE AUTH OK");
        pCharacteristic->setValue("AUTH_OK");
        pCharacteristic->notify();
      } else {
        authAttempts++;
        logPrint("BLE AUTH SIKERTELEN (kiserlet: ");
        logPrint(authAttempts);
        logPrintln(")");

        if (authAttempts >= MAX_AUTH_ATTEMPTS) {
          lockoutStart = millis();
          logPrintln("BLE AUTH LOCKOUT - 60 sec");
          pCharacteristic->setValue("AUTH_LOCKED");
        } else {
          pCharacteristic->setValue("AUTH_FAIL");
        }
        pCharacteristic->notify();
      }

    } else if (val.startsWith("LEVEL:")) {
      // Ha PIN be van állítva, csak autentikált kapcsolatból fogad el parancsot
      String correctPin = BLE_AUTH_PIN;
      if (correctPin.length() > 0 && !isAuthenticated) {
        logPrintln("LEVEL elutasitva - nincs AUTH");
        pCharacteristic->setValue("AUTH_REQUIRED");
        pCharacteristic->notify();
        return;
      }

      // Szám validálás: pontosan 1 digit karakter a 6. pozíciótól
      if (val.length() != 7 || !isDigit(val.charAt(6))) {
        logPrintln("  HIBA: ervenytelen zona ertek");
        return;
      }

      int zone = val.charAt(6) - '0';

      // Tartomány ellenőrzés
      if (zone > 3) {
        logPrint("  HIBA: zona kivul: ");
        logPrintln(zone);
        return;
      }

      // Dupla ellenőrzés: még mindig csatlakozva?
      if (!bleConnected) {
        logPrintln("  BLE megszakadt parancs kozben!");
        return;
      }

      setFanZone(zone, SRC_BLE);

    } else {
      logPrint("  Ismeretlen parancs: ");
      logPrintln(val);
    }
  }
};

// ============================================================
// GOMB ESEMÉNYEK
// ============================================================
void handleClick() {

  logPrintln("Gomb: Rovid kattintas");

  if (!rollerActive) {
    enableRelays();
    delay(100);
    activateRoller(SRC_BUTTON);
  } else {
    if (currentZone == 0) {
      deactivateRoller(SRC_BUTTON);
      delay(100);
      disableRelays();
    }
  }
}

void handleLongPressStop() {
  logPrintln("Gomb: Hosszu nyomas vege - Deep Sleep");
  enterDeepSleep();
}

void handleDoubleClick() {
  // Debounce: ne fusson le 1 sec-en belül kétszer
  unsigned long now = millis();
  if (now - lastDoubleClickTime < DOUBLE_CLICK_COOLDOWN) {
    return;
  }
  lastDoubleClickTime = now;

  logPrintln("Gomb: Dupla kattintas");

  if (!manualMode) {
    manualMode = true;
    logPrintln("Manualis mod AKTIV - minden leall");

    // BLE leállítás
    if (bleConnected) {
      pServer->disconnect(0);
      delay(100);
    }
    BLEDevice::stopAdvertising();
    bleEnabled = false;
    bleConnected = false;

    if (!otaInProgress) {
      if (wifiConnected) {
        // STA mód: WebSocket + Web szerver leállítás
        ws.closeAll();
        delay(50);
        ws.cleanupClients();
        server.end();
        delay(50);
      } else if (apMode) {
        // AP mód: Config Portal leállítás
        server.end();
        delay(50);
      }

      // WiFi leállítás
      if (apMode) {
        WiFi.softAPdisconnect(true);
        apMode = false;
      }
      WiFi.disconnect(true);
      delay(50);
      WiFi.mode(WIFI_OFF);
      wifiConnected = false;
    }

    if (otaInProgress) {
      Serial.println(F("BLE OFF, WiFi/WebSerial/OTA aktiv (OTA folyamatban)"));
    } else {
      Serial.println(F("BLE OFF, WiFi OFF, WebSerial OFF, OTA OFF"));
    }

    manualZoneIndex = 1;
    setFanZone(manualZoneIndex, SRC_BUTTON);

  } else {
    // 0 → 1 → 2 → 3 → 0 → 1 → 2 → 3 ...
    manualZoneIndex = (manualZoneIndex + 1) % 4;
    setFanZone(manualZoneIndex, SRC_BUTTON);
  }
}

void handleMultiClick() {
  int clicks = button.getNumberClicks();

  if (clicks == 3) {
    resetMode = true;

    Serial.println(F("Három kattintás!"));
    // Itt csinálod amit akarsz
    if (!SPIFFS.begin(true)) {
      Serial.println(F("SPIFFS mount hiba!"));
      return;
    }

    if (SPIFFS.exists("/config.json")) {
      SPIFFS.remove("/config.json");
      Serial.println(F("WiFi config törölve."));
    } else {
      Serial.println(F("config.json nem létezik."));
    }

    // NVS WiFi adatok törlése
    WiFi.disconnect(true, true);
    Serial.println(F("WiFi NVS törölve."));

    // Törli az összes párosított eszközt
    if (bleConnected) {
      pServer->disconnect(0);
      delay(200);
    }
    BLEDevice::stopAdvertising();
    delay(200);

    // BLE + minden más NVS törlése

    nvs_flash_init();
    nvs_flash_erase();
    nvs_flash_init();

    Serial.println(F("// BLE + minden más NVS törölve!"));
    delay(1000);
    bootCount = 98;
    ESP.restart();
  }

  if (clicks > 3) {
    Serial.println(F("Háromnál több kattintás jött!"));
    Serial.println(F("Semmi nem történt."));
    return;
  }
}

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(115200);
  delay(1000);

  esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();

  Serial.println();
  Serial.println(F("============================================================"));
  Serial.println(F("  Xiao ESP32C3 Ventilator + Gorgo Vezerlo"));
  Serial.print(F("  Firmware: v"));
  Serial.print(FIRMWARE_VERSION);
  Serial.print(F(" ("));
  Serial.print(FIRMWARE_DATE);
  Serial.println(F(")"));
  Serial.println(F("============================================================"));

  if (wakeup_reason == ESP_SLEEP_WAKEUP_GPIO) {
    Serial.println(F("Ébresztés: Gomb nyomás (GPIO3)"));
    bootCount = 0;  // Reset counter gombnyomásnál
  } else {
    bootCount++;
    Serial.printf("Boot count: %d\n", bootCount);

    if (bootCount == 0 || bootCount == 99) {
      Serial.println(F("Restart/boot - az ESP marad ébren (WiFi/BLE indul)"));
      // Itt folytasd: WiFi.begin(), BLEDevice::init(), stb.
    } else {
      Serial.println(F("Automatikus visszaalvás..."));
      Serial.flush();
      delay(100);

      pinMode(BUTTON_PIN, INPUT_PULLUP);
      esp_deep_sleep_enable_gpio_wakeup(BIT(BUTTON_PIN), ESP_GPIO_WAKEUP_GPIO_LOW);
      esp_deep_sleep_start();
    }
  }

  // GPIO INIT
  Serial.println(F("[1/7] GPIO pin mode..."));
  pinMode(RELAY_FAN1, OUTPUT);
  pinMode(RELAY_FAN2, OUTPUT);
  pinMode(RELAY_FAN3, OUTPUT);
  pinMode(RELAY_ROLLER, OUTPUT);
  pinMode(RELAY_EN, OUTPUT);
  pinMode(LED_YELLOW, OUTPUT);
  pinMode(LED_RED, OUTPUT);

  // KRITIKUS: Relek AZONNAL OFF + EN tiltas ELOSZOR!
  Serial.println(F("[2/7] Relek biztonsagi OFF..."));
  digitalWrite(RELAY_EN, LOW);  // Tiltas ELSO!
  delay(100);
  digitalWrite(RELAY_FAN1, HIGH);
  digitalWrite(RELAY_FAN2, HIGH);
  digitalWrite(RELAY_FAN3, HIGH);
  digitalWrite(RELAY_ROLLER, HIGH);
  relaysEnabled = false;

  Serial.println(F("[3/7] LED boot jelzes..."));
  digitalWrite(LED_YELLOW, HIGH);
  digitalWrite(LED_RED, LOW);

  Serial.println(F("[4/7] Gomb kezelo..."));
  button.attachClick(handleClick);
  button.attachLongPressStop(handleLongPressStop);
  button.attachDoubleClick(handleDoubleClick);
  button.attachMultiClick(handleMultiClick);
  button.setPressTicks(2000);
  button.setClickTicks(400);

  // SPIFFS + WiFi
  Serial.println(F("[5/7] SPIFFS & WiFi..."));
  if (!SPIFFS.begin(true)) {
    Serial.println(F("SPIFFS mount hiba! AP Fallback."));
    wifiConnected = false;
    setupWiFiAP();
  } else {
    Serial.println(F("SPIFFS mount sikeres"));
    if (!loadWiFiConfig()) {
      Serial.println(F("Config betoltes hiba"));
    }
    setupWiFiSTA();
  }

  Serial.println(F("[6/7] Web szerver..."));
  setupWebServer();

  // BLE INIT
  Serial.println(F("[7/7] BLE inicializalas..."));
  BLEDevice::init("FanController");
  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new MyServerCallbacks());

  BLEService* pService = pServer->createService(SERVICE_UUID);
  pCharacteristic = pService->createCharacteristic(
    CHARACTERISTIC_UUID,
    BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_NOTIFY);

  pCharacteristic->setCallbacks(new MyCallbacks());
  pCharacteristic->addDescriptor(new BLE2902());
  pService->start();

  BLEAdvertising* pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->addServiceUUID(SERVICE_UUID);
  pAdvertising->setScanResponse(true);
  pAdvertising->setMinPreferred(0x06);
  pAdvertising->setMaxPreferred(0x12);
  BLEDevice::startAdvertising();

  Serial.println(F("BLE Server: FanController"));

  // BOOT BEFEJEZÉS
  digitalWrite(LED_YELLOW, LOW);

  lastActivityTime = millis();
  lastHeartbeat = millis();

  Serial.println();
  Serial.println(F("Rendszer kesz!"));
  Serial.print(F("Free heap: "));
  Serial.println(ESP.getFreeHeap());
}

// ============================================================
// LOOP
// ============================================================
void loop() {
  unsigned long currentMillis = millis();

  button.tick();
  handleLEDs(currentMillis);

  if (wifiConnected) {
    ws.cleanupClients();
    ElegantOTA.loop();
  }

  // AP mód timeout
  if (apMode && (currentMillis - apStartTime > AP_TIMEOUT_MS)) {
    Serial.print(F("AP mod timeout ("));
    Serial.print(AP_TIMEOUT_MS / 60000);
    Serial.println(F(" perc) - WiFi leallitas"));
    server.end();
    WiFi.softAPdisconnect(true);
    delay(50);
    WiFi.mode(WIFI_OFF);
    apMode = false;
    Serial.println(F("WiFi OFF - csak BLE + gomb aktiv"));
  }

  // WiFi manuális leállítás (WebSerial "wifistop" parancsból)
  // Lépés 1: kérés rögzítése
  if (wifiStopRequested && wifiConnected) {
    wifiStopRequested = false;
    wifiStopRequestTime = currentMillis;
    wifiStopPending = true;
    Serial.println(F("WiFi leallitas kerelmezes - varakozas 100ms..."));
  }

  // Lépés 2: tényleges leállítás 100ms késleltetéssel (millis-alapú, nem delay!)
  if (wifiStopPending && (currentMillis - wifiStopRequestTime >= 100)) {
    wifiStopPending = false;
    Serial.println(F("WiFi manualis leallitas (wifistop parancs)..."));

    ws.closeAll();
    delay(50);
    ws.cleanupClients();

    server.end();
    delay(50);

    WiFi.disconnect(true);
    delay(50);
    WiFi.mode(WIFI_OFF);

    wifiConnected = false;
    wifiTimeoutDisabled = false;

    Serial.println(F("WiFi OFF - csak BLE + gomb aktiv"));
  }

  // WiFi STA mód timeout
  if (wifiConnected && !otaInProgress && !wifiTimeoutDisabled && (currentMillis - wifiStartTime > WIFI_STA_TIMEOUT_MS)) {
    Serial.println(F("============================================================"));
    Serial.print(F("WiFi STA timeout ("));
    Serial.print(WIFI_STA_TIMEOUT_MS / 60000);
    Serial.println(F(" perc) - WiFi + szolgaltatasok leallitasa"));
    Serial.println(F("============================================================"));

    // 1. WebSocket leállítás
    Serial.println(F("  [1/4] WebSocket leallitas..."));
    ws.closeAll();
    delay(50);
    ws.cleanupClients();

    // 2. Web szerver leállítás (WebSerial + ElegantOTA is leáll vele)
    Serial.println(F("  [2/4] Web szerver leallitas (WebSerial + OTA)..."));
    server.end();
    delay(50);

    // 3. WiFi leállítás
    Serial.println(F("  [3/4] WiFi leallitas..."));
    WiFi.disconnect(true);
    delay(50);
    WiFi.mode(WIFI_OFF);

    // 4. Állapot frissítés
    wifiConnected = false;
    Serial.println(F("  [4/4] Allapot frissitve"));

    Serial.print(F("  Felszabaditott memoria - Free heap: "));
    Serial.println(ESP.getFreeHeap());
    Serial.println(F("WiFi OFF - csak BLE + gomb aktiv"));
    Serial.println(F("============================================================"));
  }

  if (bleNeedsRestart && bleEnabled) {
    if (bleRestartTime == 0) bleRestartTime = currentMillis;
    if (currentMillis - bleRestartTime > BLE_RESTART_DELAY) {
      pServer->getAdvertising()->start();
      logPrintln("BLE advertising ujraindítva");
      bleNeedsRestart = false;
      bleRestartTime = 0;
    }
  }

  // Aktivitas frissites - csak edzes kozben (ventilator + gorgo)
  updateActivityTime();

  // BLE nelkul + nem manualis mod: 10 perc utan zone0-ba valt
  // BLE advertising AKTIV MARAD - ha ujra csatlakozik, mukodik tovabb
  // A vegso leallitast a 30 perces deep sleep inaktivitas kezeli
  if (!bleConnected && !manualMode && bleDisconnectTime > 0) {
    if (currentMillis - bleDisconnectTime > BLE_ZONE_TIMEOUT_MS) {
      bleDisconnectTime = 0;  // Csak egyszer fusson le
      if (currentZone != 0) {
        Serial.println(F("BLE timeout (10 perc) - zone0, BLE var kapcsolatra"));
        wsPrintln("BLE timeout - zone0 (BLE advertising aktiv)");
        setFanZone(0, SRC_NONE);
      }
      // BLE NEM all le - bleEnabled=true, advertising fut tovabb
    }
  }

  // Deep Sleep visszaszámláló (percenként)
  unsigned long elapsed = currentMillis - lastActivityTime;

  if (elapsed < INACTIVITY_MS && (currentMillis - lastSleepCountdown > SLEEP_COUNTDOWN_INTERVAL)) {
    unsigned long remaining = INACTIVITY_MS - elapsed;
    unsigned long remainMin = remaining / 60000;
    unsigned long remainSec = (remaining % 60000) / 1000;

    logPrint("Deep Sleep: ");
    logPrint((int)remainMin);
    logPrint(" perc ");
    logPrint((int)remainSec);
    logPrintln(" mp mulva");
    logPrint("Free heap: ");
    logPrintln((int)ESP.getFreeHeap());

    lastSleepCountdown = currentMillis;
  }

  if (elapsed > INACTIVITY_MS && elapsed < (0xFFFFFFFF - INACTIVITY_MS)) {
    logPrintln("Inaktivitas timeout (30 perc) - Deep Sleep");
    enterDeepSleep();
  }

  yield();
}

// ============================================================
// VENTILÁTOR ZÓNA BEÁLLÍTÁS
// ============================================================

void setFanZone(int zone, CommandSource source) {
  // Mutex: ne fusson ketszer egyszerre
  if (zoneChanging) {
    logPrintln("ZONA VALTAS BLOKKOLVA - mar folyamatban!");
    return;
  }

  unsigned long now = millis();

  // Forrás prioritás ellenőrzés
  if (activeSource != SRC_NONE && source != SRC_NONE && now < sourceLockedUntil) {
    if (source > activeSource) {  // Nagyobb szám = alacsonyabb prioritás
      logPrint("ZONA VALTAS ELUTASITVA - ");
      logPrint(source == SRC_WEBSERIAL ? "WebSerial" : "BLE");
      logPrint(" blokkolt, aktiv forras: ");
      logPrintln(activeSource == SRC_BUTTON ? "Gomb" : (activeSource == SRC_BLE ? "BLE" : "WebSerial"));
      return;
    }
  }

  zoneChanging = true;

  // Forrás zárolás
  if (source != SRC_NONE) {
    activeSource = source;
    sourceLockedUntil = now + SOURCE_LOCK_MS;
    logPrint("Parancs forras: ");
    logPrintln(source == SRC_BUTTON ? "Gomb" : (source == SRC_BLE ? "BLE" : "WebSerial"));
  }

  if (zone < 0) zone = 0;
  if (zone > 3) zone = 3;

  if (zone == currentZone) {
    logPrint("Zona mar beallitva: ");
    logPrintln(zone);
    zoneChanging = false;
    return;
  }

  logPrint("Zona valtas: ");
  logPrint(currentZone);
  logPrint(" -> ");
  logPrintln(zone);

  // ELOSZOR: MINDEN rele OFF (KRITIKUS!)
  digitalWrite(RELAY_FAN1, HIGH);
  digitalWrite(RELAY_FAN2, HIGH);
  digitalWrite(RELAY_FAN3, HIGH);

  // Frissitsd AZONNAL
  currentZone = zone;

  // Varakozas (rele vedelmi ido)
  delay(RELAY_SWITCH_DELAY_MS);

  // UTANA: csak 1 rele ON
  if (zone > 0) switchOnZone(zone);

  zoneChanging = false;
}

void switchOnZone(int zone) {
  switch (zone) {
    case 1:
      digitalWrite(RELAY_FAN1, LOW);
      logPrintln("  RELAY_FAN1 ON (LOW/33%)");
      break;
    case 2:
      digitalWrite(RELAY_FAN2, LOW);
      logPrintln("  RELAY_FAN2 ON (MED/66%)");
      break;
    case 3:
      digitalWrite(RELAY_FAN3, LOW);
      logPrintln("  RELAY_FAN3 ON (HIGH/100%)");
      break;
  }
}

// ============================================================
// GÖRGŐ VEZÉRLÉS
// ============================================================
void activateRoller(CommandSource source) {
  if (rollerActive) return;

  unsigned long now = millis();
  if (activeSource != SRC_NONE && source != SRC_NONE && now < sourceLockedUntil) {
    if (source > activeSource) {
      logPrintln("GORGO BE ELUTASITVA - alacsonyabb prioritas");
      return;
    }
  }

  if (source != SRC_NONE) {
    activeSource = source;
    sourceLockedUntil = now + SOURCE_LOCK_MS;
  }

  digitalWrite(RELAY_ROLLER, LOW);
  rollerActive = true;
  logPrintln("Gorgo BE");
}

void deactivateRoller(CommandSource source) {
  if (!rollerActive) return;

  unsigned long now = millis();
  if (activeSource != SRC_NONE && source != SRC_NONE && now < sourceLockedUntil) {
    if (source > activeSource) {
      logPrintln("GORGO KI ELUTASITVA - alacsonyabb prioritas");
      return;
    }
  }

  if (source != SRC_NONE) {
    activeSource = source;
    sourceLockedUntil = now + SOURCE_LOCK_MS;
  }

  digitalWrite(RELAY_ROLLER, HIGH);
  rollerActive = false;
  logPrintln("Gorgo KI");
}

// ============================================================
// RELÉ ENGEDÉLYEZÉS
// ============================================================
void enableRelays() {
  digitalWrite(RELAY_EN, HIGH);
  relaysEnabled = true;
  logPrintln("Relek engedelyezve");
}

// ============================================================
// RELÉ TÍLTÁS
// ============================================================
void disableRelays() {
  digitalWrite(RELAY_EN, LOW);
  relaysEnabled = false;
  logPrintln("Relek Tiltva!");
}

// ============================================================
// LED KEZELÉS
// ============================================================
void handleLEDs(unsigned long currentMillis) {

  if (resetMode) {
    // Gyors villogtatás
    if (currentMillis - lastYellowToggle > 100) {  // 100 ms = gyors villogás
      yellowLedState = !yellowLedState;
      digitalWrite(LED_YELLOW, yellowLedState ? HIGH : LOW);
      lastYellowToggle = currentMillis;
    }
    return;  // FONTOS: ne fusson le a többi LED logika!
  }

  // --- LED_RED logika ---
  if (bleConnected) {
    // BLE csatlakoztatva → folyamatosan világít
    digitalWrite(LED_RED, HIGH);

  } else if (manualMode) {
    // Manuális mód → ne világítson
    digitalWrite(LED_RED, LOW);

  } else if (bleEnabled && !bleConnected) {
    // BLE engedélyezve, de nincs kapcsolat → villog
    if (currentMillis - lastRedToggle > LED_BLINK_INTERVAL) {
      redLedState = !redLedState;
      digitalWrite(LED_RED, redLedState ? HIGH : LOW);
      lastRedToggle = currentMillis;
    }

  } else {
    // Minden más eset → kikapcsolva
    // Heartbeat (pulzálás)
    if (!heartbeatPulse_red) {
      if (currentMillis - lastHeartbeat_red >= HEARTBEAT_INTERVAL) {
        digitalWrite(LED_RED, HIGH);
        heartbeatPulse_red = true;
        lastHeartbeat_red = currentMillis;
      } else {
        digitalWrite(LED_RED, LOW);
      }
    } else {
      if (currentMillis - lastHeartbeat_red >= HEARTBEAT_PULSE) {
        digitalWrite(LED_RED, LOW);
        heartbeatPulse_red = false;
      }
    }
  }

  // --- LED_YELLOW ---
  if (relaysEnabled && rollerActive) {
    // Villog, ha a relék aktívak és a roller mozog
    if (currentMillis - lastYellowToggle > LED_BLINK_INTERVAL) {
      yellowLedState = !yellowLedState;
      digitalWrite(LED_YELLOW, yellowLedState ? HIGH : LOW);
      lastYellowToggle = currentMillis;
    }

  } else {
    // Heartbeat (pulzálás)
    if (!heartbeatPulse) {
      if (currentMillis - lastHeartbeat >= HEARTBEAT_INTERVAL) {
        digitalWrite(LED_YELLOW, HIGH);
        heartbeatPulse = true;
        lastHeartbeat = currentMillis;
      } else {
        digitalWrite(LED_YELLOW, LOW);
      }
    } else {
      if (currentMillis - lastHeartbeat >= HEARTBEAT_PULSE) {
        digitalWrite(LED_YELLOW, LOW);
        heartbeatPulse = false;
      }
    }
  }
}

// ============================================================
// AKTIVITÁS FRISSÍTÉS - csak edzés közben (ventilátor + görgő)
// ============================================================
void updateActivityTime() {
  if (currentZone > 0 && rollerActive) {
    lastActivityTime = millis();
  }
}

// ============================================================
// DEEP SLEEP (TELJES CLEANUP - ESP32C3!)
// ============================================================
void enterDeepSleep() {
  Serial.println(F("============================================================"));
  Serial.println(F("DEEP SLEEP ELOKESZITES"));
  Serial.println(F("============================================================"));

  // 1. Ventilátor OFF
  Serial.println(F("[1/11] Ventilator leallitasa..."));
  if (currentZone > 0) {
    setFanZone(0);
  }
  delay(50);

  // 2. Görgő OFF
  Serial.println(F("[2/11] Gorgo leallitasa..."));
  if (rollerActive) {
    deactivateRoller();
  }
  delay(50);

  // 3. Minden relé OFF (biztonsági)
  Serial.println(F("[3/11] Minden rele OFF..."));
  digitalWrite(RELAY_FAN1, HIGH);
  digitalWrite(RELAY_FAN2, HIGH);
  digitalWrite(RELAY_FAN3, HIGH);
  digitalWrite(RELAY_ROLLER, HIGH);
  delay(50);

  // 4. Relék tiltása
  Serial.println(F("[4/11] Relek tiltasa..."));
  digitalWrite(RELAY_EN, LOW);
  relaysEnabled = false;
  delay(50);

  // 5. LED-ek OFF
  Serial.println(F("[5/11] LED-ek OFF..."));
  digitalWrite(LED_RED, LOW);
  digitalWrite(LED_YELLOW, LOW);

  // 6. WebSocket leállítás
  if (wifiConnected) {
    Serial.println(F("[6/11] WebSocket leallitas..."));
    ws.closeAll();
    delay(100);
    ws.cleanupClients();
  } else {
    Serial.println(F("[6/11] WebSocket - skip (nem aktiv)"));
  }

  // 7. Web szerver leállítás
  if (wifiConnected || apMode) {
    Serial.println(F("[7/11] Web szerver leallitas..."));
    server.end();
    if (apMode) {
      WiFi.softAPdisconnect(true);
      apMode = false;
    }
    delay(100);
  } else {
    Serial.println(F("[7/11] Web szerver - skip (nem aktiv)"));
  }

  // 8. BLE leállítás
  if (bleEnabled) {
    Serial.println(F("[8/11] BLE leallitas..."));
    if (bleConnected) {
      pServer->disconnect(0);
      delay(200);
    }
    BLEDevice::stopAdvertising();
    delay(100);
    bleConnected = false;
    bleEnabled = false;
    Serial.println(F("  BLE leallitva"));
    // NE hivd a deinit()-et! Heap corruption-t okoz ESP32C3-on!
  } else {
    Serial.println(F("[8/11] BLE - skip (mar leallitva)"));
  }

  // 9. WiFi leállítás
  if (WiFi.getMode() != WIFI_OFF) {
    Serial.println(F("[9/11] WiFi leallitas..."));
    WiFi.disconnect(true);
    delay(100);
    WiFi.mode(WIFI_OFF);
    delay(100);
  } else {
    Serial.println(F("[9/11] WiFi - skip (mar leallitva)"));
  }
  wifiConnected = false;

  // 10. SPIFFS unmount
  Serial.println(F("[10/11] SPIFFS unmount..."));
  SPIFFS.end();

  // 11. Ébresztő beállítás (ESP32C3!)
  Serial.println(F("[11/11] Deep sleep ebreszto (GPIO3)..."));
  esp_deep_sleep_enable_gpio_wakeup(BIT(BUTTON_PIN), ESP_GPIO_WAKEUP_GPIO_LOW);

  Serial.println();
  Serial.println(F("DEEP SLEEP aktivalasa..."));
  Serial.println(F("============================================================"));
  Serial.flush();

  delay(100);

  esp_deep_sleep_start();
}