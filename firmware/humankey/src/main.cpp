// HumanKey: the hardware key.
//
// Shows the 4 lines the gate sends, waits for a button, and signs the decision with
// an Ed25519 key that is generated on the board and never leaves it. The gate holds
// only the public key, so nothing on the laptop can forge a "yes".
//
// Serial protocol (115200 baud, one JSON object per line), ARCHITECTURE.md section 5:
//   gate -> key  {"t":"ping"}
//   key  -> gate {"t":"pong","id":"humankey-<chip>","counter":N,"pub":"<base64 public key>"}
//   gate -> key  {"t":"show","req":"<contractId>","text":"L1\nL2\nL3\nL4","c":"<contract sha256>"}
//   key  -> gate {"t":"decision","req":...,"d":"approve|reject|timeout","counter":N,
//                 "th":"<sha256 hex of the text shown>","id":...,"sig":"<base64>"}
// Signed message: req|c|th|d|counter. The counter lives in flash and goes up on every
// decision, so an old "yes" cannot be replayed. A timeout is never signed.
//
// Boards (pick the PlatformIO env):
//   esp32s3   ESP32-S3 DevKit: SSD1306 128x64 on SDA 8 / SCL 9, BT1 (approve) on 4,
//             BT2 (reject) on 5, buttons to GND; on-board RGB LED on 48 shows the state.
//   nodemcuv2 NodeMCU (ESP8266): SSD1306 on SDA 12 / SCL 13, BT1 on 4, BT2 on 5.
#include <Arduino.h>
#include <ArduinoJson.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Crypto.h>
#include <EEPROM.h>
#include <Ed25519.h>
#include <SHA256.h>
#include <Wire.h>

#define APPROVE_PIN 4
#define REJECT_PIN 5
#if defined(ESP32)
#define OLED_SDA 8
#define OLED_SCL 9
#define RGB_PIN 48
#else
#define OLED_SDA 12
#define OLED_SCL 13
#endif
#define TIMEOUT_MS 60000UL

#define EE_MAGIC 0x484B4559UL  // "HKEY"
struct Saved {
  uint32_t magic;
  uint8_t priv[32];
  uint32_t counter;
};

Adafruit_SSD1306 oled(128, 64, &Wire, -1);
bool oledOk = false;
Saved saved;
uint8_t pub[32];
String deviceId;

// ---------------------------------------------------------------- helpers
String b64(const uint8_t *data, size_t len) {
  static const char *T = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  String out;
  for (size_t i = 0; i < len; i += 3) {
    uint32_t n = (uint32_t)data[i] << 16;
    if (i + 1 < len) n |= (uint32_t)data[i + 1] << 8;
    if (i + 2 < len) n |= data[i + 2];
    out += T[(n >> 18) & 63];
    out += T[(n >> 12) & 63];
    out += i + 1 < len ? T[(n >> 6) & 63] : '=';
    out += i + 2 < len ? T[n & 63] : '=';
  }
  return out;
}

String sha256Hex(const String &s) {
  SHA256 h;
  uint8_t d[32];
  h.update((const uint8_t *)s.c_str(), s.length());
  h.finalize(d, sizeof d);
  char hex[65];
  for (int i = 0; i < 32; i++) sprintf(hex + 2 * i, "%02x", d[i]);
  return String(hex);
}

void led(uint8_t r, uint8_t g, uint8_t b) {
#if defined(ESP32)
  neopixelWrite(RGB_PIN, r, g, b);
#endif
}

uint32_t hwRandom() {
#if defined(ESP32)
  return esp_random();
#else
  return RANDOM_REG32;
#endif
}

String chipId() {
#if defined(ESP32)
  return String((uint32_t)(ESP.getEfuseMac() >> 16), HEX);
#else
  return String(ESP.getChipId(), HEX);
#endif
}

void save() {
  EEPROM.put(0, saved);
  EEPROM.commit();
}

// Two-colour SSD1306: rows 0-15 are yellow (title), rows 16-63 blue (body + footer).
void screen(const String &title, const String &body, const String &footer) {
  if (!oledOk) return;
  oled.clearDisplay();
  oled.setTextSize(1);
  oled.setTextColor(SSD1306_WHITE);
  oled.setCursor(0, 4);
  oled.print(title);
  int y = 18, start = 0;
  while (start <= (int)body.length() && y <= 48) {
    int nl = body.indexOf('\n', start);
    if (nl < 0) nl = body.length();
    oled.setCursor(0, y);
    oled.print(body.substring(start, nl));
    start = nl + 1;
    y += 10;
  }
  if (footer.length()) {
    oled.setCursor(0, 56);
    oled.print(footer);
  }
  oled.display();
}

void idle() { led(0, 0, 8); screen("HUMANKEY", "ready\nsigned so far: " + String(saved.counter), "waiting for the gate"); }

// Result screen, held for 5 s: a filled disc with a check (approve) or an X (reject) cut out of it,
// or a clock (timeout). The OLED is yellow/blue only, so the colour comes from the RGB LED,
// which blinks green (approve), red (reject) or white (timeout) for the same 5 s.
void result(const String &d) {
  uint8_t r = d == "reject" ? 60 : d == "approve" ? 0 : 15;
  uint8_t g = d == "approve" ? 60 : d == "reject" ? 0 : 15;
  uint8_t b = d == "timeout" ? 15 : 0;
  if (oledOk) {
    oled.clearDisplay();
    oled.setTextColor(SSD1306_WHITE);
    oled.setTextSize(1);
    oled.setCursor(0, 4);
    oled.print("HUMANKEY  #" + String(saved.counter));
    const int cx = 26, cy = 40, rad = 23;
    if (d == "timeout") {
      oled.drawCircle(cx, cy, rad, SSD1306_WHITE);
      oled.drawCircle(cx, cy, rad - 1, SSD1306_WHITE);
      for (int w = -1; w <= 1; w++) {
        oled.drawLine(cx + w, cy, cx + w, cy - 15, SSD1306_WHITE);
        oled.drawLine(cx, cy + w, cx + 11, cy + 7 + w, SSD1306_WHITE);
      }
    } else {
      oled.fillCircle(cx, cy, rad, SSD1306_WHITE);
      for (int w = -2; w <= 2; w++) {
        if (d == "approve") {
          oled.drawLine(cx - 13, cy + w, cx - 4, cy + 9 + w, SSD1306_BLACK);
          oled.drawLine(cx - 4, cy + 9 + w, cx + 13, cy - 10 + w, SSD1306_BLACK);
        } else {
          oled.drawLine(cx - 11 + w, cy - 11, cx + 11 + w, cy + 11, SSD1306_BLACK);
          oled.drawLine(cx + 11 + w, cy - 11, cx - 11 + w, cy + 11, SSD1306_BLACK);
        }
      }
    }
    oled.setTextSize(2);
    oled.setCursor(58, 24);
    oled.print(d == "approve" ? "YES" : d == "reject" ? "NO" : "TIME");
    oled.setTextSize(1);
    oled.setCursor(58, 46);
    oled.print(d == "timeout" ? "not signed" : "signed");
    oled.display();
  }
  for (int i = 0; i < 10; i++) {  // 5 s of blinking
    led(r, g, b);
    delay(300);
    led(0, 0, 0);
    delay(200);
  }
}

// ---------------------------------------------------------------- protocol
void pong() {
  JsonDocument out;
  out["t"] = "pong";
  out["id"] = deviceId;
  out["counter"] = saved.counter;
  out["pub"] = b64(pub, 32);
  serializeJson(out, Serial);
  Serial.println();
}

String waitButton() {
  unsigned long start = millis();
  while (millis() - start < TIMEOUT_MS) {
    if (digitalRead(APPROVE_PIN) == LOW) { delay(40); if (digitalRead(APPROVE_PIN) == LOW) return "approve"; }
    if (digitalRead(REJECT_PIN) == LOW) { delay(40); if (digitalRead(REJECT_PIN) == LOW) return "reject"; }
    yield();
    delay(10);
  }
  return "timeout";
}

void show(JsonDocument &in) {
  String req = in["req"] | "";
  String text = in["text"] | "";
  String c = in["c"] | "";
  led(24, 16, 0);
  screen("HUMANKEY  #" + String(saved.counter + 1), text, "BT1=YES       BT2=NO");
  String d = waitButton();
  saved.counter += 1;  // every decision moves the counter, signed or not
  save();
  String th = sha256Hex(text);
  JsonDocument out;
  out["t"] = "decision";
  out["req"] = req;
  out["d"] = d;
  out["counter"] = saved.counter;
  out["th"] = th;
  out["id"] = deviceId;
  if (d != "timeout") {
    String msg = req + "|" + c + "|" + th + "|" + d + "|" + String(saved.counter);
    uint8_t sig[64];
    Ed25519::sign(sig, saved.priv, pub, (const uint8_t *)msg.c_str(), msg.length());
    out["sig"] = b64(sig, 64);
  }
  serializeJson(out, Serial);
  Serial.println();
  result(d);
  idle();
}

// Wiring check: which I2C addresses answer on the screen pins (and on the pins swapped),
// and the level of each button pin (1 = open, 0 = pressed / tied to GND).
void diag() {
  JsonDocument out;
  out["t"] = "diag";
  out["oled"] = oledOk;
  for (int pass = 0; pass < 2; pass++) {
    int sda = pass ? OLED_SCL : OLED_SDA, scl = pass ? OLED_SDA : OLED_SCL;
    Wire.end();
    Wire.begin(sda, scl);
    JsonArray found = out[pass ? "i2c_swapped" : "i2c"].to<JsonArray>();
    for (uint8_t a = 1; a < 127; a++) {
      Wire.beginTransmission(a);
      if (Wire.endTransmission() == 0) found.add(a);
    }
  }
  Wire.end();
  Wire.begin(OLED_SDA, OLED_SCL);
  oledOk = oled.begin(SSD1306_SWITCHCAPVCC, 0x3C);
  out["oled_after"] = oledOk;
  out["bt1"] = digitalRead(APPROVE_PIN);
  out["bt2"] = digitalRead(REJECT_PIN);
  serializeJson(out, Serial);
  Serial.println();
  idle();
}

// Wiring check: try every safe pin pair as SDA/SCL and report where a screen answers.
void findOled() {
#if defined(ESP32)
  const int pins[] = {1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 21, 38, 39, 40, 41, 42, 47};
#else
  const int pins[] = {4, 5, 12, 13, 14, 16};
#endif
  const int n = sizeof(pins) / sizeof(pins[0]);
  JsonDocument out;
  out["t"] = "findoled";
  JsonArray hits = out["hits"].to<JsonArray>();
  for (int i = 0; i < n; i++)
    for (int j = 0; j < n; j++) {
      if (i == j) continue;
      Wire.end();
      Wire.begin(pins[i], pins[j]);
      Wire.setTimeOut(5);
      for (uint8_t a = 0x3C; a <= 0x3D; a++) {
        Wire.beginTransmission(a);
        if (Wire.endTransmission() == 0) {
          JsonObject h = hits.add<JsonObject>();
          h["sda"] = pins[i];
          h["scl"] = pins[j];
          h["addr"] = a;
        }
      }
    }
  Wire.end();
  Wire.begin(OLED_SDA, OLED_SCL);
  for (int p : pins) pinMode(p, INPUT);
  pinMode(APPROVE_PIN, INPUT_PULLUP);
  pinMode(REJECT_PIN, INPUT_PULLUP);
  serializeJson(out, Serial);
  Serial.println();
}

// Wiring check without a meter: the screen board has pull-up resistors on SDA/SCK to its VDD.
// With our own pull-downs on, a line reads 1 only if the screen is powered AND that wire reaches it.
void lines() {
  Wire.end();
  pinMode(OLED_SDA, INPUT_PULLDOWN);
  pinMode(OLED_SCL, INPUT_PULLDOWN);
  delay(5);
  JsonDocument out;
  out["t"] = "lines";
  out["sda"] = digitalRead(OLED_SDA);
  out["scl"] = digitalRead(OLED_SCL);
  serializeJson(out, Serial);
  Serial.println();
  Wire.begin(OLED_SDA, OLED_SCL);
}

// ---------------------------------------------------------------- setup / loop
void setup() {
  pinMode(APPROVE_PIN, INPUT_PULLUP);
  pinMode(REJECT_PIN, INPUT_PULLUP);
  Serial.begin(115200);
  Wire.begin(OLED_SDA, OLED_SCL);
  oledOk = oled.begin(SSD1306_SWITCHCAPVCC, 0x3C);

  EEPROM.begin(sizeof(Saved));
  EEPROM.get(0, saved);
  if (saved.magic != EE_MAGIC) {
    // First boot: a fresh key from the hardware random number generator.
    for (int i = 0; i < 32; i += 4) {
      uint32_t r = hwRandom();
      memcpy(saved.priv + i, &r, 4);
    }
    saved.magic = EE_MAGIC;
    saved.counter = 0;
    save();
  }
  Ed25519::derivePublicKey(pub, saved.priv);
  deviceId = "humankey-" + chipId();
  idle();
  pong();
}

void loop() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (!line.startsWith("{")) return;
  JsonDocument in;
  if (deserializeJson(in, line)) return;
  String t = in["t"] | "";
  if (t == "ping") pong();
  else if (t == "diag") diag();
  else if (t == "findoled") findOled();
  else if (t == "lines") lines();
  else if (t == "show") show(in);
}
