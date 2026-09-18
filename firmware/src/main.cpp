#include <M5Unified.h>
#include <ArduinoJson.h>

namespace {
constexpr uint32_t SERIAL_BAUD = 115200;
constexpr uint32_t PC_DISCONNECT_AFTER_MS = 30000;
constexpr uint32_t SERIAL_RECOVERY_AFTER_MS = 5000;
constexpr uint32_t SERIAL_RECOVERY_INTERVAL_MS = 5000;
constexpr bool POWER_OFF_ON_DISCONNECT = false;
constexpr bool SLEEP_DISPLAY_ON_DISCONNECT = true;
constexpr uint8_t DISPLAY_BRIGHTNESS = 120;
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
UsageState renderedState;
bool hasRenderedState = false;
bool displayAsleep = false;
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

bool windowEquals(const WindowInfo& lhs, const WindowInfo& rhs) {
  return lhs.available == rhs.available &&
         lhs.remainingPercent == rhs.remainingPercent &&
         lhs.resetText == rhs.resetText &&
         lhs.resetInText == rhs.resetInText;
}

void recoverSerialIfStale() {
  static uint32_t lastRecoveryMs = 0;
  const uint32_t now = millis();
  if (now - state.lastRxMs < SERIAL_RECOVERY_AFTER_MS) return;
  if (now - lastRecoveryMs < SERIAL_RECOVERY_INTERVAL_MS) return;

  // USB unplug/replug can leave the UART peripheral alive but no longer
  // receiving bytes. Reinitializing the UART is less disruptive than a full
  // ESP32 reset and allows the next bridge heartbeat to reconnect it.
  Serial.flush();
  Serial.end();
  delay(20);
  Serial.begin(SERIAL_BAUD);
  rxLine = "";
  lastRecoveryMs = now;
}

void drawWindowCardFrame(int y, const char* title) {
  M5.Display.drawRoundRect(8, y, 304, 68, 7, TFT_DARKGREY);
  M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
  M5.Display.setTextSize(2);
  M5.Display.setCursor(18, y + 8);
  M5.Display.print(title);
}

void drawWindowContent(int y, const WindowInfo& w) {
  // Keep the title and card frame intact; only clear the changing content.
  M5.Display.fillRect(16, y + 24, 288, 40, TFT_BLACK);
  M5.Display.fillRect(234, y + 4, 72, 23, TFT_BLACK);

  if (!w.available) {
    M5.Display.setTextSize(1);
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
  M5.Display.setTextSize(2);
  M5.Display.setCursor(178, y + 48);
  M5.Display.printf("%s", w.resetText.c_str());
}

void drawStaticLayout() {
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setTextDatum(top_left);

  M5.Display.setTextColor(TFT_CYAN, TFT_BLACK);
  M5.Display.setTextSize(2);
  M5.Display.setCursor(10, 8);
  M5.Display.print("CODEX USAGE");

  drawWindowCardFrame(34, "5 HOUR LIMIT");
  drawWindowCardFrame(108, "WEEKLY LIMIT");

  M5.Display.drawRoundRect(8, 182, 304, 42, 7, TFT_DARKGREY);
  M5.Display.setTextSize(2);
  M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
  M5.Display.setCursor(18, 190);
  M5.Display.print("CREDITS");
}

void drawConnectionStatus(bool connected) {
  M5.Display.setTextSize(1);
  M5.Display.fillRect(238, 6, 72, 20, TFT_BLACK);
  M5.Display.setTextColor(connected ? TFT_GREEN : TFT_RED, TFT_BLACK);
  M5.Display.setCursor(244, 11);
  M5.Display.print(connected ? "USB OK" : "OFFLINE");
}

void drawCreditsContent(const UsageState& current) {
  M5.Display.fillRect(108, 185, 200, 24, TFT_BLACK);
  M5.Display.setTextSize(2);
  M5.Display.setTextColor(TFT_YELLOW, TFT_BLACK);
  M5.Display.setCursor(112, 188);
  if (current.creditsUnlimited) {
    M5.Display.print("UNLIMITED");
  } else {
    M5.Display.print(current.creditsAvailable ? current.creditsBalance : "--");
  }
}

void drawFooterContent(const UsageState& current) {
  M5.Display.fillRect(12, 206, 296, 17, TFT_BLACK);
  M5.Display.setTextSize(1);
  M5.Display.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  M5.Display.setCursor(18, 211);
  M5.Display.printf("Plan: %s", current.plan.c_str());
  M5.Display.setCursor(184, 211);
  M5.Display.printf("Updated: %s", current.updated.c_str());
}

void drawErrorContent(const UsageState& current) {
  M5.Display.fillRect(8, 225, 304, 15, TFT_BLACK);
  if (!current.valid && current.error.length() > 0) {
    M5.Display.setTextColor(TFT_RED, TFT_BLACK);
    M5.Display.setCursor(10, 228);
    M5.Display.print(current.error.substring(0, 48));
  }
}

void refreshScreen() {
  if (!hasRenderedState) {
    drawStaticLayout();
  }

  if (!hasRenderedState || state.connected != renderedState.connected) {
    drawConnectionStatus(state.connected);
  }
  if (!hasRenderedState || !windowEquals(state.fiveHour, renderedState.fiveHour)) {
    drawWindowContent(34, state.fiveHour);
  }
  if (!hasRenderedState || !windowEquals(state.weekly, renderedState.weekly)) {
    drawWindowContent(108, state.weekly);
  }
  if (!hasRenderedState || state.creditsAvailable != renderedState.creditsAvailable ||
      state.creditsUnlimited != renderedState.creditsUnlimited ||
      state.creditsBalance != renderedState.creditsBalance) {
    drawCreditsContent(state);
  }
  if (!hasRenderedState || state.plan != renderedState.plan ||
      state.updated != renderedState.updated) {
    drawFooterContent(state);
  }
  if (!hasRenderedState || state.valid != renderedState.valid ||
      state.error != renderedState.error) {
    drawErrorContent(state);
  }

  renderedState = state;
  hasRenderedState = true;
}

void setDisplayAwake(bool awake) {
  if (awake) {
    if (!displayAsleep) return;
    M5.Display.wakeup();
    M5.Display.setBrightness(DISPLAY_BRIGHTNESS);
    displayAsleep = false;
    // The display controller may have lost its active frame while asleep.
    // Redraw once after waking; normal updates remain partial redraws.
    hasRenderedState = false;
    refreshScreen();
    return;
  }

  if (displayAsleep) return;
  M5.Display.sleep();
  M5.Display.waitDisplay();
  displayAsleep = true;
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
    refreshScreen();
    return;
  }

  String messageType = String((const char*)(doc["type"] | ""));
  if (messageType == "codex_heartbeat") {
    state.connected = true;
    state.error = "";
    state.lastRxMs = millis();
    setDisplayAwake(true);
    // Heartbeats can be the first message after a USB reconnect.  Redraw
    // here so the stale OFFLINE indicator is cleared immediately even when
    // no usage payload has arrived yet.
    refreshScreen();
    return;
  }
  if (messageType != "codex_usage") return;

  state.connected = true;
  state.lastRxMs = millis();
  setDisplayAwake(true);
  state.valid = doc["ok"] | false;
  state.updated = String((const char*)(doc["updated"] | "--"));

  if (!state.valid) {
    state.error = String((const char*)(doc["error"] | "Codex read failed"));
    refreshScreen();
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
  refreshScreen();
}
}  // namespace

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Display.setRotation(1);
  M5.Display.wakeup();
  M5.Display.setBrightness(DISPLAY_BRIGHTNESS);
  M5.Display.setTextWrap(false);

  Serial.begin(SERIAL_BAUD);
  rxLine.reserve(768);
  refreshScreen();
  if (SLEEP_DISPLAY_ON_DISCONNECT) {
    setDisplayAwake(false);
  }
}

void loop() {
  M5.update();
  recoverSerialIfStale();

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

  if (state.connected && millis() - state.lastRxMs > PC_DISCONNECT_AFTER_MS) {
    state.connected = false;
    state.valid = false;
    state.error = "PC disconnected";
      if (POWER_OFF_ON_DISCONNECT) {
        refreshScreen();
        delay(700);
        state.error = "Power off...";
        refreshScreen();
        delay(500);
        M5.Power.powerOff();
      } else if (SLEEP_DISPLAY_ON_DISCONNECT) {
        setDisplayAwake(false);
      } else {
        // Keep the last layout visible so reconnect does not depend on
        // display-controller sleep/wakeup behavior.
        refreshScreen();
      }
  }

  delay(5);
}
