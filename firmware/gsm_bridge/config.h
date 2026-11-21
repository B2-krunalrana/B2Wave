#pragma once
// ---------------------------------------------------------------------------
//  GSM Bridge - user configuration
//  Board: KTRON ESP32-S3 + SIMCom A7672S 4G DevKit (KSTM2098)
// ---------------------------------------------------------------------------

// OPTIONAL: hard-code Wi-Fi here. Leave empty ("") to use the setup portal:
// the board opens a hotspot "GSM-Bridge-Setup" and you pick your Wi-Fi in a
// browser. Credentials saved from the portal always win over these.
#define DEFAULT_WIFI_SSID "B2-ios"
#define DEFAULT_WIFI_PASS "12345678"

#define DEVICE_NAME      "gsm-bridge"        // mDNS -> http://gsm-bridge.local
#define SETUP_AP_SSID    "B2Wave-Setup"
#define SETUP_AP_PASS    "12345678"          // min 8 chars

// Network ports
#define BRIDGE_TCP_PORT  7000                // raw AT bridge (used by the PC app)
#define DISCOVERY_PORT   7002                // UDP auto-discovery

// Modem UART (A7672S default is 115200 auto-baud)
#define MODEM_BAUD       115200

// ---- Board pin map (from KTRON KiCad symbol / pinout) ----------------------
#define PIN_4G_TX        17   // "4G_TX"
#define PIN_4G_RX        18   // "4G_RX"
#define PIN_4G_RI        16   // ring indicator (input)
#define PIN_4G_DTR       9    // held LOW = module stays awake
#define PIN_4G_STATUS    10   // module power status (input)
#define PIN_4G_RESET     13   // pulse to hard-reset module
#define PIN_LED_WIFI     11
#define PIN_LED_4G       12
#define PIN_LED_SYS      14

#define LED_ON           HIGH
#define LED_OFF          LOW
#define RESET_ACTIVE     HIGH   // GPIO drives a transistor that pulls RESET_N low
#define RESET_PULSE_MS   2500
