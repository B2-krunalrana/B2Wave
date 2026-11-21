/*
 * GSM Bridge firmware for KTRON ESP32-S3 + A7672S 4G DevKit
 * ---------------------------------------------------------
 * Turns the board into a Wi-Fi <-> modem bridge:
 *   - TCP port 7000 : transparent pipe to the A7672S AT UART (the PC app talks here)
 *   - UDP port 7002 : auto-discovery ("GSM_BRIDGE_DISCOVER" -> JSON reply)
 *   - HTTP port 80  : status page, modem reset, Wi-Fi setup
 *   - mDNS          : http://gsm-bridge.local
 *
 * First boot (no Wi-Fi saved): joins hotspot "GSM-Bridge-Setup" (pass 12345678),
 * open http://192.168.4.1 and choose your Wi-Fi.
 *
 * Arduino IDE: Board "ESP32S3 Dev Module", USB CDC On Boot: Disabled,
 *              Flash 16MB, PSRAM "OPI PSRAM". Upload over the ESP_UART USB port.
 */
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <WiFiUdp.h>
#include <DNSServer.h>
#include "config.h"

#define FW_VERSION "1.1.0"

HardwareSerial Modem(1);
WebServer http(80);
WiFiServer bridge(BRIDGE_TCP_PORT);
WiFiClient client;
WiFiUDP udp;
DNSServer dns;
Preferences prefs;

bool setupMode = false;
bool modemOk = false;
bool pinsSwapped = false;
uint32_t modemBaud = MODEM_BAUD;
uint8_t rstIdle = 0;                 // level that keeps 4G_RESET released (auto-detected)
uint32_t lastProbe = 0, lastModemRx = 0, lastBlink = 0;
uint32_t bytesUp = 0, bytesDown = 0;
String clientIp = "";

// bring-up state machine
enum { BU_WAIT, BU_READY, BU_FAILED } buState = BU_WAIT;
uint32_t buStart = 0;
uint8_t buTried = 0;                 // bit0: idle LOW tried, bit1: idle HIGH tried
const uint32_t BAUDS[] = {115200, 9600, 57600, 230400, 460800, 921600};

// ---------------------------------------------------------------- log ring buffer
#define LOG_LINES 120
String logBuf[LOG_LINES];
int logHead = 0, logCount = 0;

void blog(const char *fmt, ...) {
  char buf[256];
  va_list ap; va_start(ap, fmt); vsnprintf(buf, sizeof(buf), fmt, ap); va_end(ap);
  Serial.printf("[bridge] %s\n", buf);
  char ts[16]; snprintf(ts, sizeof(ts), "%7.1fs ", millis() / 1000.0);
  logBuf[logHead] = String(ts) + buf;
  logHead = (logHead + 1) % LOG_LINES;
  if (logCount < LOG_LINES) logCount++;
}

String logDump() {
  String s;
  for (int i = 0; i < logCount; i++) {
    s += logBuf[(logHead - logCount + i + LOG_LINES) % LOG_LINES];
    s += "\n";
  }
  return s;
}

String printable(const uint8_t *b, size_t n) {
  String s;
  for (size_t i = 0; i < n; i++) {
    char c = b[i];
    if (c == '\r') s += "\\r"; else if (c == '\n') s += "\\n";
    else if (c >= 0x20 && c < 0x7f) s += c;
    else { char h[6]; snprintf(h, sizeof(h), "<%02X>", (uint8_t)c); s += h; }
  }
  return s;
}

// ---------------------------------------------------------------- modem low level
void beginModem(bool swapped, uint32_t baud = 0) {
  if (baud == 0) baud = modemBaud;
  Modem.end();
  Modem.setRxBufferSize(8192);
  int rx = swapped ? PIN_4G_TX : PIN_4G_RX;
  int tx = swapped ? PIN_4G_RX : PIN_4G_TX;
  Modem.begin(baud, SERIAL_8N1, rx, tx);
  pinsSwapped = swapped;
}

void applyResetIdle() {
  pinMode(PIN_4G_RESET, OUTPUT);
  digitalWrite(PIN_4G_RESET, rstIdle);
}

// Send "AT" and look for "OK". Only used when no PC client is attached.
bool probeOnce(uint32_t timeoutMs) {
  while (Modem.available()) Modem.read();
  Modem.print("AT\r");
  String resp;
  uint32_t t0 = millis();
  while (millis() - t0 < timeoutMs) {
    while (Modem.available()) resp += (char)Modem.read();
    if (resp.indexOf("OK") >= 0) return true;
    delay(5);
  }
  if (resp.length()) blog("probe got non-OK bytes: %s", printable((const uint8_t *)resp.c_str(), resp.length()).c_str());
  return false;
}

void modemFound(bool sw, uint32_t baud) {
  blog("MODEM OK: pins %s, %lu baud, reset idle=%d", sw ? "swapped" : "normal", (unsigned long)baud, rstIdle);
  prefs.putBool("swap", sw);
  prefs.putUChar("rst_idle", rstIdle);
  if (baud != MODEM_BAUD) {        // lock module to 115200 for the PC app
    Modem.printf("AT+IPR=%d\r", MODEM_BAUD); delay(200);
    beginModem(sw, MODEM_BAUD);
  }
  modemBaud = MODEM_BAUD;
  Modem.print("ATE0\r");           // no echo -> cleaner parsing on the PC
  delay(100);
  while (Modem.available()) Modem.read();
}

// Quick check at the working baud, both UART orientations.
bool detectModem() {
  for (int i = 0; i < 2; i++) {
    bool sw = (i == 0) ? pinsSwapped : !pinsSwapped;
    beginModem(sw, MODEM_BAUD);
    delay(20);
    for (int k = 0; k < 2; k++) {
      if (probeOnce(300)) { modemFound(sw, MODEM_BAUD); return true; }
    }
  }
  beginModem(prefs.getBool("swap", false), MODEM_BAUD);
  return false;
}

// Slow check: every baud, both orientations (module may have a fixed odd baud).
bool detectModemAllBauds() {
  for (uint32_t b : BAUDS) {
    for (int i = 0; i < 2; i++) {
      bool sw = i == 1;
      beginModem(sw, b);
      delay(20);
      if (probeOnce(250) || probeOnce(250)) { modemFound(sw, b); return true; }
    }
  }
  beginModem(prefs.getBool("swap", false), MODEM_BAUD);
  return false;
}

void resetModem() {
  blog("pulsing 4G_RESET (active=%d for %d ms)", !rstIdle, RESET_PULSE_MS);
  pinMode(PIN_4G_RESET, OUTPUT);
  digitalWrite(PIN_4G_RESET, !rstIdle);
  delay(RESET_PULSE_MS);
  digitalWrite(PIN_4G_RESET, rstIdle);
  modemOk = false;
  buState = BU_WAIT; buStart = millis(); lastProbe = millis() + 3000;
}

// Non-blocking bring-up: probe; if silent for 25 s, flip the reset-line idle level
// (a floating / wrong level can hold the module in reset); remember what works.
void serviceBringUp(bool pcAttached) {
  if ((int32_t)(millis() - lastProbe) < 0) return;
  if (buState == BU_READY) {
    if (pcAttached) return;
    if (millis() - lastModemRx > 30000) {          // silent for a while -> re-check
      lastProbe = millis() + 10000;
      if (detectModem()) lastModemRx = millis();
      else { modemOk = false; buState = BU_WAIT; buStart = millis(); blog("modem went silent"); }
    }
    return;
  }
  lastProbe = millis() + (buState == BU_FAILED ? 15000 : 2000);
  if (detectModem()) { modemOk = true; lastModemRx = millis(); buState = BU_READY; return; }
  if (buState == BU_FAILED) return;
  if (millis() - buStart > 25000) {
    buTried |= (rstIdle ? 2 : 1);
    if (detectModemAllBauds()) { modemOk = true; lastModemRx = millis(); buState = BU_READY; return; }
    if (buTried != 3) {
      rstIdle = !rstIdle;
      applyResetIdle();
      blog("no reply - trying 4G_RESET idle level %d", rstIdle);
      buStart = millis();
    } else {
      buState = BU_FAILED;
      rstIdle = prefs.getUChar("rst_idle", 0);
      applyResetIdle();
      blog("BRING-UP FAILED: module silent at all bauds / both reset levels. Check power (VIN 5-12V 2A).");
    }
  }
}

// ---------------------------------------------------------------- diagnostics
// Full hardware report (~10 s, blocking).
String runDiag() {
  String r;
  auto line = [&](const String &s) { r += s; r += "\n"; };
  line("fw=" FW_VERSION " uptime=" + String(millis() / 1000) + "s heap=" + String(ESP.getFreeHeap()));
  line("wifi: ssid=" + WiFi.SSID() + " ip=" + WiFi.localIP().toString() + " rssi=" + String(WiFi.RSSI()) + " setupMode=" + String(setupMode));
  line("modem: ok=" + String(modemOk) + " state=" + String((int)buState) + " swapped=" + String(pinsSwapped) + " rst_idle=" + String(rstIdle) + " tried=" + String(buTried) + " pc_client=" + clientIp);
  String st, ri;
  for (int i = 0; i < 20; i++) { st += digitalRead(PIN_4G_STATUS) ? '1' : '0'; ri += digitalRead(PIN_4G_RI) ? '1' : '0'; delay(50); }
  line("pins over 1s: STATUS(gpio10)=" + st + " RI(gpio16)=" + ri + " RESET(gpio13 out)=" + String(digitalRead(PIN_4G_RESET)) + " DTR(gpio9 out)=" + String(digitalRead(PIN_4G_DTR)));
  Modem.end();
  pinMode(PIN_4G_TX, INPUT); pinMode(PIN_4G_RX, INPUT);
  delay(5);
  line("idle levels (UART detached): gpio17=" + String(digitalRead(PIN_4G_TX)) + " gpio18=" + String(digitalRead(PIN_4G_RX)));
  pinMode(PIN_4G_TX, INPUT_PULLDOWN); pinMode(PIN_4G_RX, INPUT_PULLDOWN);
  delay(5);
  line("with pulldown:              gpio17=" + String(digitalRead(PIN_4G_TX)) + " gpio18=" + String(digitalRead(PIN_4G_RX)) + "  (a line still 1 here is driven HIGH by the module = its TX)");
  uint8_t buf[200];
  for (int i = 0; i < 2; i++) {
    for (uint32_t b : BAUDS) {
      beginModem(i == 1, b);
      delay(20);
      while (Modem.available()) Modem.read();
      Modem.print("AT\r");
      delay(250);
      Modem.print("AT\r");
      uint32_t t0 = millis(); size_t n = 0;
      while (millis() - t0 < 350) { while (Modem.available() && n < sizeof(buf)) buf[n++] = Modem.read(); delay(2); }
      line(String("probe ") + (i ? "swapped" : "normal ") + " @" + String(b) + ": " + (n ? String(n) + " bytes: " + printable(buf, n) : String("no bytes")));
    }
  }
  beginModem(prefs.getBool("swap", false), MODEM_BAUD);
  line("--- last log ---");
  r += logDump();
  return r;
}

String modemRaw(const String &cmd, uint32_t waitMs) {
  while (Modem.available()) Modem.read();
  Modem.print(cmd); Modem.print("\r");
  String r; uint32_t t0 = millis();
  while (millis() - t0 < waitMs) {
    while (Modem.available()) r += (char)Modem.read();
    if (r.indexOf("\r\nOK\r\n") >= 0 || r.indexOf("ERROR") >= 0) break;
    delay(5);
  }
  return printable((const uint8_t *)r.c_str(), r.length());
}

String jsonStatus() {
  String s = "{";
  s += "\"name\":\"" DEVICE_NAME "\",";
  s += "\"fw\":\"" FW_VERSION "\",";
  s += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
  s += "\"port\":" + String(BRIDGE_TCP_PORT) + ",";
  s += "\"ssid\":\"" + WiFi.SSID() + "\",";
  s += "\"rssi\":" + String(WiFi.RSSI()) + ",";
  s += "\"modem\":" + String(modemOk ? "true" : "false") + ",";
  s += "\"modem_power\":" + String(digitalRead(PIN_4G_STATUS) ? "true" : "false") + ",";
  s += "\"pins_swapped\":" + String(pinsSwapped ? "true" : "false") + ",";
  s += "\"rst_idle\":" + String(rstIdle) + ",";
  s += "\"bringup\":\"" + String(buState == BU_READY ? "ready" : buState == BU_FAILED ? "failed" : "probing") + "\",";
  s += "\"client\":\"" + clientIp + "\",";
  s += "\"bytes_up\":" + String(bytesUp) + ",";
  s += "\"bytes_down\":" + String(bytesDown) + ",";
  s += "\"uptime\":" + String(millis() / 1000);
  s += "}";
  return s;
}

// ---------------------------------------------------------------- web pages
const char PAGE_HEAD[] PROGMEM = R"HTML(<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>B2Wave</title>
<style>body{font-family:system-ui,sans-serif;background:#0f1115;color:#e7e9ee;max-width:520px;margin:0 auto;padding:16px}
.card{background:#181b22;border:1px solid #2a2f3a;border-radius:12px;padding:16px;margin:12px 0}
input,select,button{width:100%;box-sizing:border-box;padding:10px;margin:6px 0;border-radius:8px;border:1px solid #2a2f3a;background:#0f1115;color:#e7e9ee;font-size:15px}
button{background:#3b82f6;border:0;font-weight:600;cursor:pointer}button.warn{background:#b45309}
td{padding:4px 8px}td:first-child{color:#8b93a7}</style></head><body><h2>B2Wave</h2>)HTML";

void pageSetup() {
  String opts;
  int n = WiFi.scanNetworks();
  for (int i = 0; i < n; i++) {
    opts += "<option>" + WiFi.SSID(i) + "</option>";
  }
  String p = FPSTR(PAGE_HEAD);
  p += "<div class=card><b>Connect to your Wi-Fi</b><form method=post action=/save>"
       "<select name=s>" + opts + "</select><input name=s2 placeholder='...or type SSID'>"
       "<input name=p type=password placeholder='Wi-Fi password'><button>Save &amp; reboot</button></form></div></body></html>";
  http.send(200, "text/html", p);
}

void pageStatus() {
  String p = FPSTR(PAGE_HEAD);
  p += "<div class=card><table>";
  p += "<tr><td>IP</td><td>" + WiFi.localIP().toString() + "</td></tr>";
  p += "<tr><td>Wi-Fi</td><td>" + WiFi.SSID() + " (" + String(WiFi.RSSI()) + " dBm)</td></tr>";
  p += "<tr><td>Modem</td><td>" + String(modemOk ? "responding" : "NOT responding") + "</td></tr>";
  p += "<tr><td>PC app</td><td>" + (clientIp.length() ? clientIp : String("not connected")) + "</td></tr>";
  p += "<tr><td>Bridge</td><td>tcp://" + WiFi.localIP().toString() + ":" + String(BRIDGE_TCP_PORT) + "</td></tr>";
  p += "<tr><td>Firmware</td><td>" FW_VERSION "</td></tr></table></div>";
  p += "<div class=card><form method=post action=/reset4g><button class=warn>Hard-reset 4G module</button></form>"
       "<form method=post action=/forget><button class=warn>Forget Wi-Fi (setup mode)</button></form></div></body></html>";
  http.send(200, "text/html", p);
}

void setupHttp() {
  http.on("/", HTTP_GET, [] { setupMode ? pageSetup() : pageStatus(); });
  http.on("/api/status", HTTP_GET, [] { http.send(200, "application/json", jsonStatus()); });
  http.on("/save", HTTP_POST, [] {
    String s = http.arg("s2").length() ? http.arg("s2") : http.arg("s");
    prefs.putString("ssid", s);
    prefs.putString("pass", http.arg("p"));
    http.send(200, "text/html", String(FPSTR(PAGE_HEAD)) +
              "<div class=card>Saved. Rebooting and joining <b>" + s +
              "</b>...<br>Then open <b>http://" DEVICE_NAME ".local</b> or run the PC app.</div>");
    delay(1500);
    ESP.restart();
  });
  http.on("/api/log", HTTP_GET, [] { http.send(200, "text/plain", logDump()); });
  http.on("/api/diag", HTTP_GET, [] { http.send(200, "text/plain", runDiag()); });
  http.on("/api/at", HTTP_GET, [] {
    if (client && client.connected()) { http.send(409, "text/plain", "PC app is attached; use the app console"); return; }
    http.send(200, "text/plain", modemRaw(http.arg("c"), http.arg("t").length() ? http.arg("t").toInt() : 3000));
  });
  http.on("/api/reset4g", HTTP_GET, [] { resetModem(); http.send(200, "text/plain", "ok"); });
  http.on("/api/setrst", HTTP_GET, [] {
    rstIdle = http.arg("v").toInt() ? 1 : 0; prefs.putUChar("rst_idle", rstIdle); applyResetIdle();
    buState = BU_WAIT; buStart = millis(); buTried = 0;
    http.send(200, "text/plain", "rst_idle=" + String(rstIdle));
  });
  http.on("/reset4g", HTTP_POST, [] {
    http.sendHeader("Location", "/"); http.send(303);
    resetModem();
  });
  http.on("/forget", HTTP_POST, [] {
    prefs.remove("ssid"); prefs.remove("pass");
    http.send(200, "text/plain", "Wi-Fi forgotten, rebooting into setup mode");
    delay(1000); ESP.restart();
  });
  http.onNotFound([] {
    if (setupMode) { http.sendHeader("Location", "http://192.168.4.1/"); http.send(302); }
    else http.send(404, "text/plain", "not found");
  });
  http.begin();
}

// ---------------------------------------------------------------- Wi-Fi
bool joinWifi(const String &ssid, const String &pass);

// saved network first, then the built-in default (B2-ios)
bool connectWifi() {
  String ssid = prefs.getString("ssid", "");
  String pass = prefs.getString("pass", "");
  if (ssid.length() && joinWifi(ssid, pass)) return true;
  String dssid = DEFAULT_WIFI_SSID;
  if (dssid.length() && dssid != ssid && joinWifi(dssid, DEFAULT_WIFI_PASS)) return true;
  return false;
}

bool joinWifi(const String &ssid, const String &pass) {
  if (ssid.isEmpty()) return false;
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(DEVICE_NAME);
  WiFi.setSleep(false);                 // low latency for the bridge
  WiFi.setAutoReconnect(true);
  WiFi.begin(ssid.c_str(), pass.c_str());
  blog("joining Wi-Fi '%s'", ssid.c_str());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    digitalWrite(PIN_LED_WIFI, (millis() / 150) % 2 ? LED_ON : LED_OFF);
    delay(50);
  }
  return WiFi.status() == WL_CONNECTED;
}

void startSetupPortal() {
  setupMode = true;
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(SETUP_AP_SSID, SETUP_AP_PASS);
  dns.start(53, "*", WiFi.softAPIP());
  // keep trying the saved/default network in the background
  String ssid = prefs.getString("ssid", DEFAULT_WIFI_SSID);
  String pass = prefs.getString("pass", DEFAULT_WIFI_PASS);
  if (ssid.length()) { WiFi.setAutoReconnect(true); WiFi.begin(ssid.c_str(), pass.c_str()); }
  blog("SETUP MODE: join Wi-Fi '%s' (pass %s) and open http://192.168.4.1",
       SETUP_AP_SSID, SETUP_AP_PASS);
}

// ---------------------------------------------------------------- bridge
void serviceBridge() {
  // accept new client (newest wins - handy if the PC app restarts)
  if (bridge.hasClient()) {
    WiFiClient nc = bridge.accept();
    if (client && client.connected()) client.stop();
    client = nc;
    client.setNoDelay(true);
    clientIp = client.remoteIP().toString();
    blog("PC app connected from %s", clientIp.c_str());
  }
  if (client && !client.connected()) {
    blog("PC app disconnected");
    client.stop();
    clientIp = "";
  }

  static uint8_t buf[1024];
  // modem -> PC
  size_t n = Modem.available();
  if (n) {
    n = Modem.readBytes(buf, min(n, sizeof(buf)));
    lastModemRx = millis();
    if (client && client.connected()) { client.write(buf, n); bytesDown += n; }
    else {                                   // nobody listening: keep it in the log
      static String sniff;
      for (size_t i = 0; i < n; i++) {
        char c = buf[i];
        if (c == '\n' || sniff.length() > 120) { sniff.trim(); if (sniff.length()) blog("modem> %s", sniff.c_str()); sniff = ""; }
        else if (c != '\r') sniff += (c >= 0x20 && c < 0x7f) ? c : '?';
      }
    }
  }
  // PC -> modem
  if (client && client.connected()) {
    int a = client.available();
    if (a > 0) {
      int r = client.read(buf, min((size_t)a, sizeof(buf)));
      if (r > 0) { Modem.write(buf, r); bytesUp += r; }
    }
  }
}

void serviceDiscovery() {
  int len = udp.parsePacket();
  if (len <= 0) return;
  char msg[64] = {0};
  udp.read(msg, sizeof(msg) - 1);
  if (strncmp(msg, "GSM_BRIDGE_DISCOVER", 19) == 0) {
    String s = jsonStatus();
    udp.beginPacket(udp.remoteIP(), udp.remotePort());
    udp.write((const uint8_t *)s.c_str(), s.length());
    udp.endPacket();
  }
}

// ---------------------------------------------------------------- USB setup channel
// The PC Setup wizard talks to the board over the ESP_UART USB port with
// one-line commands. Replies start with a keyword so log lines never confuse it.
String jsonEsc(const String &s) {
  String o;
  for (char c : s) { if (c == '"' || c == '\\') o += '\\'; if ((uint8_t)c >= 0x20) o += c; }
  return o;
}

String modemQuery(const char *cmd, uint32_t timeoutMs = 1500) {
  while (Modem.available()) Modem.read();
  Modem.print(cmd); Modem.print("\r");
  String r; uint32_t t0 = millis();
  while (millis() - t0 < timeoutMs) {
    while (Modem.available()) r += (char)Modem.read();
    if (r.indexOf("\nOK") >= 0 || r.startsWith("OK") || r.indexOf("ERROR") >= 0) break;
    delay(5);
  }
  r.replace("\r", ""); r.trim();
  int ok = r.lastIndexOf("OK");
  if (ok >= 0 && ok == (int)r.length() - 2) { r = r.substring(0, ok); r.trim(); }
  r.replace("\n", " | ");
  return r;
}

void cmdModemTest() {
  if (!modemOk && detectModem()) { modemOk = true; buState = BU_READY; lastModemRx = millis(); }
  String s = "MODEM {\"ok\":" + String(modemOk ? "true" : "false");
  if (modemOk) {
    s += ",\"model\":\"" + jsonEsc(modemQuery("AT+CGMM")) + "\"";
    s += ",\"sim\":\"" + jsonEsc(modemQuery("AT+CPIN?")) + "\"";
    s += ",\"csq\":\"" + jsonEsc(modemQuery("AT+CSQ")) + "\"";
    s += ",\"reg\":\"" + jsonEsc(modemQuery("AT+CEREG?")) + "\"";
    s += ",\"creg\":\"" + jsonEsc(modemQuery("AT+CREG?")) + "\"";
    s += ",\"op\":\"" + jsonEsc(modemQuery("AT+COPS?", 3000)) + "\"";
  }
  s += ",\"power\":" + String(digitalRead(PIN_4G_STATUS) ? "true" : "false");
  s += ",\"swapped\":" + String(pinsSwapped ? "true" : "false");
  s += ",\"bringup\":\"" + String(buState == BU_READY ? "ready" : buState == BU_FAILED ? "failed" : "probing") + "\"";
  s += ",\"rst_idle\":" + String(rstIdle) + "}";
  Serial.println(s);
}

void cmdScan() {
  int n = WiFi.scanNetworks();
  String s = "SCAN [";
  for (int i = 0; i < n; i++) {
    if (i) s += ",";
    s += "{\"ssid\":\"" + jsonEsc(WiFi.SSID(i)) + "\",\"rssi\":" + String(WiFi.RSSI(i)) +
         ",\"open\":" + String(WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "true" : "false") + "}";
  }
  Serial.println(s + "]");
}

void cmdWifi(const String &arg) {
  int tab = arg.indexOf('\t');
  String ssid = tab >= 0 ? arg.substring(0, tab) : arg;
  String pass = tab >= 0 ? arg.substring(tab + 1) : "";
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  if (setupMode) { dns.stop(); WiFi.softAPdisconnect(true); setupMode = false; }
  WiFi.disconnect(true);
  delay(200);
  if (connectWifi()) {
    MDNS.end();
    if (MDNS.begin(DEVICE_NAME)) {
      MDNS.addService("http", "tcp", 80);
      MDNS.addService("gsmbridge", "tcp", BRIDGE_TCP_PORT);
    }
    Serial.println("WIFI OK " + WiFi.localIP().toString());
  } else {
    wl_status_t st = WiFi.status();
    Serial.println(String("WIFI FAIL ") + (st == WL_NO_SSID_AVAIL ? "network not found" :
                   st == WL_CONNECT_FAILED ? "wrong password" : "timeout"));
    startSetupPortal();
  }
}

void serviceUsbCommands() {
  static String line;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c != '\n') { if (line.length() < 300) line += c; continue; }
    String cmd = line; line = "";
    cmd.trim();
    if (cmd == "PING") Serial.println("PONG " DEVICE_NAME " " FW_VERSION);
    else if (cmd == "STATUS") Serial.println("STATUS " + jsonStatus());
    else if (cmd == "MODEM") cmdModemTest();
    else if (cmd == "SCAN") cmdScan();
    else if (cmd.startsWith("WIFI ")) cmdWifi(cmd.substring(5));
    else if (cmd == "RESET4G") { resetModem(); Serial.println("RESET4G OK"); }
    else if (cmd == "DIAG" || cmd == "LOG") {
      String r = cmd == "DIAG" ? runDiag() : logDump();
      int s = 0;
      while (s < (int)r.length()) { int e = r.indexOf('\n', s); if (e < 0) e = r.length(); Serial.println(cmd + " " + r.substring(s, e)); s = e + 1; }
      Serial.println(cmd + " END");
    }
    else if (cmd.startsWith("AT")) Serial.println("ATR " + modemRaw(cmd, 3000));
    else if (cmd.startsWith("SETRST ")) {
      rstIdle = cmd.substring(7).toInt() ? 1 : 0; prefs.putUChar("rst_idle", rstIdle); applyResetIdle();
      buState = BU_WAIT; buStart = millis(); buTried = 0;
      Serial.println("SETRST OK " + String(rstIdle));
    }
    else if (cmd == "REBOOT") { Serial.println("REBOOT OK"); delay(100); ESP.restart(); }
  }
}

// ---------------------------------------------------------------- main
void setup() {
  Serial.begin(115200);
  delay(200);
  blog("GSM Bridge v%s booting", FW_VERSION);

  pinMode(PIN_LED_WIFI, OUTPUT); pinMode(PIN_LED_4G, OUTPUT); pinMode(PIN_LED_SYS, OUTPUT);
  pinMode(PIN_4G_DTR, OUTPUT);  digitalWrite(PIN_4G_DTR, LOW);   // keep modem awake
  pinMode(PIN_4G_RI, INPUT);
  pinMode(PIN_4G_STATUS, INPUT);
  prefs.begin("bridge", false);
  rstIdle = prefs.getUChar("rst_idle", 0);
  applyResetIdle();                                              // never leave RESET floating
  blog("4G_RESET idle level %d, waiting for module", rstIdle);
  beginModem(prefs.getBool("swap", false), MODEM_BAUD);
  buState = BU_WAIT; buStart = millis(); lastProbe = millis() + 1500;

  if (!connectWifi()) startSetupPortal();
  else blog("Wi-Fi OK  ip=%s  rssi=%d", WiFi.localIP().toString().c_str(), WiFi.RSSI());

  if (MDNS.begin(DEVICE_NAME)) {
    MDNS.addService("http", "tcp", 80);
    MDNS.addService("gsmbridge", "tcp", BRIDGE_TCP_PORT);
  }
  setupHttp();
  bridge.begin();
  bridge.setNoDelay(true);
  udp.begin(DISCOVERY_PORT);
  blog("ready: tcp %d, udp %d, http://%s.local", BRIDGE_TCP_PORT, DISCOVERY_PORT, DEVICE_NAME);
}

void loop() {
  if (setupMode) {
    dns.processNextRequest();
    if (WiFi.status() == WL_CONNECTED) {       // hotspot came up later -> leave setup mode
      dns.stop(); WiFi.softAPdisconnect(true); WiFi.mode(WIFI_STA); setupMode = false;
      blog("Wi-Fi OK  ip=%s", WiFi.localIP().toString().c_str());
    }
  }
  http.handleClient();
  serviceDiscovery();
  serviceBridge();
  serviceUsbCommands();

  // re-probe the modem only while no PC app owns the line
  bool pcAttached = client && client.connected();
  serviceBringUp(pcAttached);

  // LEDs: Wi-Fi solid when connected, 4G solid when modem answers, SYS heartbeat
  digitalWrite(PIN_LED_WIFI, (setupMode ? (millis() / 500) % 2 : WiFi.status() == WL_CONNECTED) ? LED_ON : LED_OFF);
  digitalWrite(PIN_LED_4G, modemOk ? LED_ON : LED_OFF);
  if (millis() - lastBlink > (pcAttached ? 250 : 1000)) {
    lastBlink = millis();
    digitalWrite(PIN_LED_SYS, !digitalRead(PIN_LED_SYS));
  }
  delay(1);
}
