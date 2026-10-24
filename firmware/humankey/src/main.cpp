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

// Stored after Saved, so boards enrolled before this version keep their key and counter.
#define EXT_MAGIC 0x484B5832UL  // "HKX2"
#define MAX_ACCTS 32
struct Account {
  char vendor[8];
  char hash[17];
  char last4[5];
};
struct Ext {
  uint32_t magic;
  uint32_t lastExpected;  // count witness: invoices in the source at the last signed decision
  uint8_t n;
  Account acct[MAX_ACCTS];  // the device's own ledger: last account a human approved per vendor
};

Adafruit_SSD1306 oled(128, 64, &Wire, -1);
Ext ext;
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
  EEPROM.put(sizeof(Saved), ext);
  EEPROM.commit();
}

int findAccount(const String &vendor) {
  for (int i = 0; i < ext.n; i++)
    if (vendor == ext.acct[i].vendor) return i;
  return -1;
}

void storeAccount(const String &vendor, const String &hash, const String &last4) {
  int i = findAccount(vendor);
  if (i < 0) {
    if (ext.n >= MAX_ACCTS) return;
    i = ext.n++;
  }
  strlcpy(ext.acct[i].vendor, vendor.c_str(), sizeof ext.acct[i].vendor);
  strlcpy(ext.acct[i].hash, hash.c_str(), sizeof ext.acct[i].hash);
  strlcpy(ext.acct[i].last4, last4.c_str(), sizeof ext.acct[i].last4);
}

// Two-colour SSD1306: rows 0-15 are yellow (title), rows 16-63 blue (body + footer).
// alert fills the yellow band and cuts the title out of it, so a warning shows from across the desk.
void screen(const String &title, const String &body, const String &footer, bool alert = false) {
  if (!oledOk) return;
  oled.clearDisplay();
  oled.setTextSize(1);
  if (alert) oled.fillRect(0, 0, 128, 15, SSD1306_WHITE);
  oled.setTextColor(alert ? SSD1306_BLACK : SSD1306_WHITE);
  oled.setCursor((128 - 6 * (int)title.length()) / 2, 4);
  oled.print(title);
  oled.setTextColor(SSD1306_WHITE);
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

void idle() {
  led(0, 0, 8);
  if (!oledOk) return;
  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);
  oled.setTextSize(1);
  oled.setCursor(0, 4);
  oled.print("HUMANKEY");
  oled.setTextSize(2);
  oled.setCursor(34, 26);
  oled.print("READY");
  oled.setTextSize(1);
  String foot = String(ext.n) + " accounts  #" + String(saved.counter);
  oled.setCursor((128 - 6 * foot.length()) / 2, 56);
  oled.print(foot);
  oled.display();
}

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

// "$12,488" from 12488.01, like the gate's Python formatting.
String money(double amount) {
  long n = lround(amount);
  String digits = String(n < 0 ? -n : n), out = "";
  for (int i = 0; i < (int)digits.length(); i++) {
    if (i && (digits.length() - i) % 3 == 0) out += ",";
    out += digits[i];
  }
  return "$" + out;
}

// One decision. Before the normal YES/NO screen the key shows its own warnings, which the
// person must acknowledge with BT1 first:
//  - count witness: "w":{"exp","cov","miss":[...]} - fewer invoices checked than exist, or the
//    independent count fell since the last signed decision;
//  - account ledger: "acct":{"v","h","l4"} - the payee account differs from the last account a
//    human approved for this vendor on THIS key (the database can be wrong; this memory is not
//    writable by the laptop).
// The signed message carries what the key saw: req|c|th|d|counter|exp|cov|acct:hash
//
// Clear signing: the gate sends the contract itself ("cj"), not its hash. The key hashes those
// bytes, signs that hash, and takes the invoice, amount, payee account and invoice count from the
// same bytes. If the laptop's screen text disagrees with the contract, the key shows both and
// only lets the person reject.
void show(JsonDocument &in) {
  String req = in["req"] | "";
  String text = in["text"] | "";
  String cj = in["cj"] | "";
  String c = sha256Hex(cj);
  JsonDocument k;
  String problem = "";
  if (!cj.length() || deserializeJson(k, cj)) problem = "no contract";
  JsonVariant pay = k["intent"]["parameters"];
  String inv = pay["invoice_id"] | "";
  double amount = pay["amount"] | 0.0;
  long exp = k["evidence"]["witness"]["exp"] | -1L;
  long cov = k["evidence"]["witness"]["cov"] | -1L;
  String av = pay["payee"]["vendor"] | "";
  String ah = pay["payee"]["iban_hash"] | "";
  String al4 = pay["payee"]["iban_last4"] | "";
  String shownSha = k["evidence"]["displaySha256"] | "";
  String first = text.substring(0, text.indexOf('\n') < 0 ? text.length() : text.indexOf('\n'));
  int dollar = first.indexOf(" $");
  String shownInv = dollar < 0 ? first : first.substring(0, dollar);
  String num = dollar < 0 ? "" : first.substring(dollar + 2);
  num.replace(",", "");
  if (!problem.length() && (String(k["contractId"] | "") != req)) problem = "other request";
  if (!problem.length() && (shownInv != inv || !num.length() || fabs(num.toDouble() - amount) > 0.5))
    problem = "amount";
  if (!problem.length() && shownSha.length() && shownSha != sha256Hex(text)) problem = "text";
  if (problem.length()) {
    led(60, 0, 0);
    String body = problem == "amount" ? "laptop:   " + first.substring(dollar < 0 ? 0 : dollar + 1) +
                                        "\ncontract: " + money(amount) + "\n" + inv + "\nNOT SIGNING"
                                      : "contract " + problem + "\ndoes not match\n\nNOT SIGNING";
    screen("!! MISMATCH !!", body, "ANY BUTTON = NO", true);
    waitButton();
    saved.counter += 1;
    save();
    String th = sha256Hex(text);
    String msg = req + "|" + c + "|" + th + "|reject|" + String(saved.counter) + "|-1|-1|none:";
    uint8_t sig[64];
    Ed25519::sign(sig, saved.priv, pub, (const uint8_t *)msg.c_str(), msg.length());
    JsonDocument out;
    out["t"] = "decision";
    out["req"] = req;
    out["d"] = "reject";
    out["counter"] = saved.counter;
    out["th"] = th;
    out["id"] = deviceId;
    out["exp"] = -1;
    out["cov"] = -1;
    out["acct"] = "none";
    out["ah"] = "";
    out["warned"] = 1;
    out["mismatch"] = problem;
    out["sig"] = b64(sig, 64);
    serializeJson(out, Serial);
    Serial.println();
    result("reject");
    idle();
    return;
  }

  // Which NVIDIA model reasoned about this payment, read from the contract bytes the key signs.
  JsonArray models = k["evidence"]["ai"]["models"].as<JsonArray>();
  if (!models.isNull() && models.size()) {
    String body = "";
    int shown = 0;
    for (JsonVariant m : models) {
      if (shown++ == 2) break;
      body += String((const char *)(m | "")).substring(0, 21) + "\n";
    }
    long calls = k["evidence"]["ai"]["calls"] | 0L;
    body += String(calls) + " AI calls\nsigned into this OK";
    led(0, 10, 30);
    screen("CHECKED BY NVIDIA", body, "", false);
    delay(5000);  // long enough to read: it is part of what the person approves
  }

  String warn = "";
  int nwarn = 0;
  if (exp >= 0 && cov >= 0 && cov < exp) {
    warn += String(exp - cov) + " of " + String(exp) + " NOT checked\n";
    nwarn++;
  }
  if (exp >= 0 && ext.lastExpected > 0 && (uint32_t)exp < ext.lastExpected) {
    warn += "count fell " + String(ext.lastExpected) + "->" + String(exp) + "\n";
    nwarn++;
  }
  String acct = "none";
  if (av.length() && ah.length()) {
    int i = findAccount(av);
    if (i < 0) acct = "first";
    else if (ah == ext.acct[i].hash) acct = "known";
    else {
      acct = "new";
      warn += "NEW ACCOUNT .." + al4 + "\nlast OK'd .." + String(ext.acct[i].last4) + "\n";
      nwarn++;
    }
  }

  String d = "approve";
  if (nwarn) {
    led(60, 0, 0);
    screen("!! KEY WARNING !!", warn, "BT1=I SEE     BT2=NO", true);
    d = waitButton();  // approve here only means "I have seen the warning"
    delay(400);
  }
  if (d == "approve") {
    // The last line is the agent's proposal and is part of the signed text. "PAY this?" means every
    // check passed and the key is only asked because of the amount; anything else carries a risk.
    bool clean = text.endsWith("PAY this?");
    if (clean) led(0, 40, 0);
    else led(60, 12, 0);
    String title = clean ? "CLEAN - OVER LIMIT"
                 : text.endsWith("BLOCK payment?") ? "!! BLOCK ADVISED !!"
                 : text.indexOf("checks passed") >= 0 ? "! AGENT UNSURE !" : "! RISK - CHECK !";
    screen(title, text, "BT1=YES       BT2=NO", !clean);
    d = waitButton();
  }
  saved.counter += 1;  // every decision moves the counter, signed or not
  if (d == "approve") {
    if (exp >= 0) ext.lastExpected = exp;
    if (av.length() && (acct == "first" || acct == "new")) storeAccount(av, ah, al4);
  }
  save();
  String th = sha256Hex(text);
  JsonDocument out;
  out["t"] = "decision";
  out["req"] = req;
  out["d"] = d;
  out["counter"] = saved.counter;
  out["th"] = th;
  out["id"] = deviceId;
  out["exp"] = exp;
  out["cov"] = cov;
  out["acct"] = acct;
  out["ah"] = ah;
  out["warned"] = nwarn;
  if (d != "timeout") {
    String msg = req + "|" + c + "|" + th + "|" + d + "|" + String(saved.counter) + "|" + String(exp) + "|" +
                 String(cov) + "|" + acct + ":" + ah;
    uint8_t sig[64];
    Ed25519::sign(sig, saved.priv, pub, (const uint8_t *)msg.c_str(), msg.length());
    out["sig"] = b64(sig, 64);
  }
  serializeJson(out, Serial);
  Serial.println();
  result(d);
  idle();
}

// One-time import of known-good accounts, approved on the key itself.
void acctImport(JsonDocument &in) {
  JsonArray items = in["items"].as<JsonArray>();
  led(24, 16, 0);
  screen("ACCOUNT IMPORT", "trust " + String(items.size()) + " vendor\naccounts from\nthe gate?", "BT1=YES       BT2=NO");
  String d = waitButton();
  if (d == "approve") {
    ext.n = 0;
    for (JsonObject it : items) storeAccount(it["v"] | "", it["h"] | "", it["l4"] | "");
    save();
  }
  JsonDocument out;
  out["t"] = "acct_import";
  out["d"] = d;
  out["n"] = ext.n;
  serializeJson(out, Serial);
  Serial.println();
  result(d);
  idle();
}

void acctList() {
  JsonDocument out;
  out["t"] = "acct_list";
  out["last_expected"] = ext.lastExpected;
  JsonArray a = out["items"].to<JsonArray>();
  for (int i = 0; i < ext.n; i++) {
    JsonObject o = a.add<JsonObject>();
    o["v"] = ext.acct[i].vendor;
    o["l4"] = ext.acct[i].last4;
  }
  serializeJson(out, Serial);
  Serial.println();
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

  EEPROM.begin(sizeof(Saved) + sizeof(Ext));
  EEPROM.get(0, saved);
  EEPROM.get(sizeof(Saved), ext);
  if (ext.magic != EXT_MAGIC) {
    memset(&ext, 0, sizeof ext);
    ext.magic = EXT_MAGIC;
  }
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
  else if (t == "acct_import") acctImport(in);
  else if (t == "acct_list") acctList();
  else if (t == "show") show(in);
}
