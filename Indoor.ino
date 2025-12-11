#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>

const char* ssid     = "VM9352729";
const char* password = "dj7wxVjgyjry";

const char* TS_WRITE_API_KEY = "Y1K6RARYUFYNZZUB";
const char* TS_CHANNEL_ID    = "3190313";

  "https://api.open-meteo.com/v1/forecast?"
  "latitude=51.4916&longitude=-0.198"
  "&current=temperature_2m,relative_humidity_2m"
  "&timezone=auto";

Adafruit_BME280 bme;
WiFiClientSecure omClient;
WiFiClientSecure tsClient; 

unsigned long lastUpdate = 0;
const unsigned long updateInterval = 60000;  // 60 s


void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.disconnect();
  WiFi.begin(ssid, password);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(500);
  }
}

void setup() {
  Serial.begin(115200);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);
  ensureWiFi();
  Serial.println("WiFi connected!");

  if (!bme.begin(0x76)) {
    Serial.println("BME280 not found at 0x76, trying 0x77...");
    if (!bme.begin(0x77)) {
      Serial.println("Could not find BME280 sensor!");
      while (1) delay(100);
    }
  }

  omClient.setInsecure();
  tsClient.setInsecure();
}

void loop() {
  if (millis() - lastUpdate > updateInterval) {
    lastUpdate = millis();
    Serial.println("\n--- NEW CYCLE ---");

    ensureWiFi();
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("WiFi not connected; skipping this cycle.");
      return;
    }

    float t_in = bme.readTemperature();
    float h_in = bme.readHumidity();
    Serial.printf("Indoor: T=%.2f°C H=%.2f%%\n", t_in, h_in);

    float t_out = NAN, h_out = NAN;
    {
      HTTPClient http;
      if (!http.begin(omClient, OM_URL)) {
        Serial.println("Open-Meteo: begin() failed");
      } else {
        http.setTimeout(8000);
        int status = http.GET();
        if (status == 200) {
          String payload = http.getString();
          // Parse JSON
          DynamicJsonDocument doc(4096);
          auto err = deserializeJson(doc, payload);
          if (!err) {
            float t = doc["current"]["temperature_2m"] | NAN;
            float h = doc["current"]["relative_humidity_2m"] | NAN;
            if (!isnan(t) && !isnan(h)) {
              t_out = t; h_out = h;
              Serial.printf("Outdoor: T=%.2f°C H=%.2f%%\n", t_out, h_out);
            } else {
              Serial.println("Open-Meteo: Missing values → skipping field3/4 this cycle.");
            }
          } else {
            Serial.printf("Open-Meteo JSON parse error: %s\n", err.c_str());
          }
        } else {
          Serial.printf("Open-Meteo: HTTP %d\n", status);
        }
        http.end();
      }
    }

      HTTPClient http;
      if (!http.begin(tsClient, "https://api.thingspeak.com/update")) {
        Serial.println("ThingSpeak upload: begin() failed");
      } else {
        http.addHeader("Content-Type", "application/x-www-form-urlencoded");

        String payload = String("api_key=") + TS_WRITE_API_KEY +
                         "&field1=" + String(t_in, 2) +
                         "&field2=" + String(h_in, 2);
        if (!isnan(t_out)) payload += "&field3=" + String(t_out, 2);
        if (!isnan(h_out)) payload += "&field4=" + String(h_out, 2);

        int httpCode = http.POST(payload);
        String body  = http.getString();
        http.end();

        Serial.printf("ThingSpeak upload → status=%d, body='%s'\n", httpCode, body.c_str());
        if (httpCode == 200 && body != "0") {
          Serial.println("Upload OK (entry id above).");
        } else {
          Serial.println("Upload failed.");
        }
      }
    }
  }
}