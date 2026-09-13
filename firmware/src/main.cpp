#include <M5Unified.h>
#include <ArduinoJson.h>

namespace {
constexpr uint32_t SERIAL_BAUD = 115200;
constexpr uint32_t STALE_AFTER_MS = 120000;
constexpr int SCREEN_W = 320;
constexpr int SCREEN_H = 240;

struct WindowInfo {
  bool available = false;
  int remainingPercent = 0;
  String resetText = "--";
  String resetInText = "--";
};

struct UsageState {
  bool connected = false;
  bool valid = false;
  WindowInfo fiveHour;
  WindowInfo weekly;
  bool creditsAvailable = false;
  bool creditsUnlimited = false;
  String creditsBalance = "--";
  String plan = "--";
  String updated = "--";
  String error = "Waiting for PC...";
  uint32_t lastRxMs = 0;
};

UsageState state;
String rxLine;

uint16_t colorForPercent(int percent) {
  if (percent <= 20) return TFT_RED;
  if (percent <= 40) return TFT_ORANGE;
  return TFT_GREEN;
}

void drawProgressBar(int x, int y, int w, int h, int percent, uint16_t color) {
  M5.Display.drawRoundRect(x, y, w, h, 4, TFT_DARKGREY);
  M5.Display.fillRoundRect(x + 2, y + 2, w - 4, h - 4, 3, TFT_BLACK);
  int inner = map(constrain(percent, 0, 100), 0, 100, 0, w - 4);
  if (inner > 0) {
    M5.Display.fillRoundRect(x + 2, y + 2, inner, h - 4, 3, color);
  }
}

void drawWindowCard(int y, const char* title, const WindowInfo& w) {
  M5.Display.drawRoundRect(8, y, 304, 68, 7, TFT_DARKGREY);
  M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
  M5.Display.setTextSize(1);
  M5.Display.setCursor(18, y + 8);
  M5.Display.print(title);

  if (!w.available) {
    M5.Display.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
    M5.Display.setCursor(18, y + 30);
    M5.Display.print("Not reported by Codex");
    return;
  }

  uint16_t c = colorForPercent(w.remainingPercent);
  M5.Display.setTextColor(c, TFT_BLACK);
  M5.Display.setTextSize(2);
  M5.Display.setCursor(244, y + 7);
  M5.Display.printf("%d%%", w.remainingPercent);

  drawProgressBar(18, y + 31, 196, 12, w.remainingPercent, c);

  M5.Display.setTextSize(1);
  M5.Display.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  M5.Display.setCursor(18, y + 50);
  M5.Display.printf("reset in %s", w.resetInText.c_str());
  M5.Display.setCursor(190, y + 50);
  M5.Display.printf("%s", w.resetText.c_str());
}

void drawScreen() {
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setTextDatum(top_left);

  M5.Display.setTextColor(TFT_CYAN, TFT_BLACK);
  M5.Display.setTextSize(2);
  M5.Display.setCursor(10, 8);
  M5.Display.print("CODEX USAGE");

  M5.Display.setTextSize(1);
  M5.Display.setTextColor(state.connected ? TFT_GREEN : TFT_RED, TFT_BLACK);
  M5.Display.setCursor(244, 11);
  M5.Display.print(state.connected ? "USB OK" : "OFFLINE");

  drawWindowCard(34, "5 HOUR LIMIT", state.fiveHour);
  drawWindowCard(108, "WEEKLY LIMIT", state.weekly);

  M5.Display.drawRoundRect(8, 182, 304, 42, 7, TFT_DARKGREY);
  M5.Display.setTextSize(1);
  M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
  M5.Display.setCursor(18, 190);
  M5.Display.print("CREDITS");
  M5.Display.setTextSize(2);
  M5.Display.setTextColor(TFT_YELLOW, TFT_BLACK);
  M5.Display.setCursor(80, 188);
  if (state.creditsUnlimited) {
    M5.Display.print("UNLIMITED");
  } else {
    M5.Display.print(state.creditsAvailable ? state.creditsBalance : "--");
  }

  M5.Display.setTextSize(1);
  M5.Display.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  M5.Display.setCursor(18, 211);
  M5.Display.printf("Plan: %s", state.plan.c_str());
  M5.Display.setCursor(184, 211);
  M5.Display.printf("Updated: %s", state.updated.c_str());

  if (!state.valid && state.error.length() > 0) {
    M5.Display.setTextColor(TFT_RED, TFT_BLACK);
    M5.Display.setCursor(10, 228);
    M5.Display.print(state.error.substring(0, 48));
  }
}

void parseWindow(JsonVariantConst src, WindowInfo& dst) {
  dst.available = src["available"] | false;
  dst.remainingPercent = src["remainingPercent"] | 0;
  dst.resetText = String((const char*)(src["resetText"] | "--"));
  dst.resetInText = String((const char*)(src["resetInText"] | "--"));
}

void handleJsonLine(const String& line) {
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, line);
  if (err) {
    state.valid = false;
    state.error = "Bad JSON from PC";
    drawScreen();
    return;
  }

  if (String((const char*)(doc["type"] | "")) != "codex_usage") return;

  state.connected = true;
  state.lastRxMs = millis();
  state.valid = doc["ok"] | false;
  state.updated = String((const char*)(doc["updated"] | "--"));

  if (!state.valid) {
    state.error = String((const char*)(doc["error"] | "Codex read failed"));
    drawScreen();
    return;
  }

  state.error = "";
  parseWindow(doc["fiveHour"], state.fiveHour);
  parseWindow(doc["weekly"], state.weekly);

  JsonVariantConst credits = doc["credits"];
  state.creditsAvailable = credits["available"] | false;
  state.creditsUnlimited = credits["unlimited"] | false;
  state.creditsBalance = String((const char*)(credits["balance"] | "--"));
  state.plan = String((const char*)(doc["plan"] | "--"));
  drawScreen();
}
}  // namespace

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Display.setRotation(1);
  M5.Display.setBrightness(120);
  M5.Display.setTextWrap(false);

  Serial.begin(SERIAL_BAUD);
  rxLine.reserve(768);
  drawScreen();
}

void loop() {
  M5.update();

  while (Serial.available() > 0) {
    char ch = static_cast<char>(Serial.read());
    if (ch == '\n') {
      rxLine.trim();
      if (rxLine.length() > 0) handleJsonLine(rxLine);
      rxLine = "";
    } else if (ch != '\r') {
      if (rxLine.length() < 2048) rxLine += ch;
      else rxLine = "";
    }
  }

  if (state.connected && millis() - state.lastRxMs > STALE_AFTER_MS) {
    state.connected = false;
    state.valid = false;
    state.error = "PC data is stale";
    drawScreen();
  }

  delay(5);
}
