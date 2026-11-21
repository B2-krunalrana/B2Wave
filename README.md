# B2Wave

**SMS, calls, voice campaigns and bulk messaging from your browser**, powered by a
KTRON **ESP32-S3 + SIMCom A7672S 4G** development board (KSTM2098).

B2Wave is a small Python web app that runs on your PC and talks to the 4G module
over USB (or over Wi-Fi through the ESP32 bridge firmware). Everything stays on your
PC. There is no cloud and no account.

```
 Browser (PC / phone)  <--WebSocket-->  B2Wave (Python, aiohttp)  <--USB AT port-->  A7672S  <--LTE/VoLTE-->  network
                                               |                     (or Wi-Fi TCP 7000 -> ESP32-S3 bridge -> UART)
                                               +--> SMTP (Gmail etc.) for emailed reports
```

---

## Features

| Area | What you get |
|---|---|
| **Home** | Live board / SIM / network status, contextual guidance, 7-day activity chart, campaign outcome chart |
| **Messages** | Chat-style SMS threads, Unicode (Gujarati, Hindi, emoji), auto-split long texts, desktop notifications |
| **Phone** | Dialer, answer/decline, in-call keypad (DTMF), call log with network end reason |
| **Campaigns** | One place for calls and SMS: **Call only**, **SMS only**, **Call → SMS by response** (rules: pressed 1 / pressed 2 / no key / not answered…), **SMS → call**. 5-step wizard (type, audience with CSV variables, call setup with key meanings, SMS/rules with templates, review), drafts, SMS template library, audio library, optional **call recording** (AMR on the module, moved to the PC, playable in reports), test with my number, re-run unanswered |
| **Call analytics** | Outcome (Responded, Listened no response, Hung up during prompt, No answer, Busy, Rejected, Unreachable, Invalid number, Network error), who ended the call, ring/talk time, prompt played fully/partly/stopped by key, key timestamps, hold/resume, network cause (AT+CEER), full timeline, CSV |
| **AI voice agent** | LiveKit agent talks on B2Wave calls. Engines: **LiveKit Inference** (no extra keys: Deepgram/AssemblyAI STT, GPT-4.1/Gemini LLM, Cartesia/ElevenLabs TTS), **agent hosted on LiveKit Cloud**, or **own OpenAI key** (Realtime). Paste-the-env setup, step tabs, B2Wave installs/starts/stops the worker, PC test mode, live transcript, history |
| **SIM number** | The SIM's own number is detected (AT+CNUM) and shown at the top of every page; SIM swaps are detected by ICCID; manual number for SIMs that don't store it |
| **Backup & restore** | One .zip with all data (optionally secrets); restore with preview, Merge or Replace, automatic safety copy, live reload of every open page |
| **Email reports** | Gmail / Outlook / Zoho / Yahoo / custom SMTP. Campaign and bulk reports are emailed automatically when finished (HTML summary + CSV) |
| **Setup wizard** | Detects the board, flashes the prebuilt ESP32 firmware (no Arduino IDE), layered tests (Wi-Fi → module → SIM → network), driver guidance, diagnostics report |
| **Talk on calls** | Main guide: solder a **3.5 mm headset socket** once and plug in any wired earphones with mic (parts list, plug pin diagram, wiring, tick-off steps, test, troubleshooting). Live call audio controls (volume, mic, earpiece/loudspeaker path, mute, test voice). Other ways (speaker+mic, PC bridge, Wi-Fi codec) folded under one section |
| **Robustness** | Auto-reconnect after COM renumbering, Wi-Fi fallback, call recovery after restart, single-instance launcher, friendly errors, app log |
| **UI** | Light/dark theme, collapsible sidebar (Ctrl+B), mobile drawer, animated logo, splash + progress loaders, Help & guide page |

---

## Quick start (Windows)

### 1. Hardware
1. Insert a **nano SIM** (PIN lock off) on the back of the board. Jio, Airtel and Vi all work (Jio calls use VoLTE).
2. Screw the **LTE antenna** onto the `LTE` U.FL connector.
3. **Cable 1:** the `ESP_UART` USB-C port (nearer the SIMCom chip) → PC. Used for setup and flashing.
4. **Cable 2:** the `4G_USB` USB-C port (nearer the ESP32 module) → PC, with the **USB_SELECT switch on "4G"**. SMS, calls and campaigns use this link.

### 2. Drivers (one time)
| Device Manager shows | Install |
|---|---|
| `CP2102 USB to UART Bridge Controller` ⚠ | [Silicon Labs CP210x VCP driver](https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers) |
| `Mobile AT Interface` / `Mobile Diag Interface` ⚠ | [SIMCom A76xx driver](https://files.waveshare.com/wiki/ESP32-S3-A7670E-4G/A7600X-Windows-Driver.7z). Extract it, then Update driver → Browse → tick *Include subfolders*. Afterwards it shows as `SimTech HS-USB AT Port 9011` |

### 3. Run
Install **Python 3.10+**, then double-click **`run.bat`**.
The first run creates `app/.venv` and installs the dependencies; after that the browser opens **http://127.0.0.1:8080**.

- First launch opens **Setup**: *Detect board* → *Install firmware* → layered tests.
- `run.bat --lan` lets phones on the same Wi-Fi open `http://<pc-ip>:8080`.
- Running `run.bat` again safely replaces an already running copy.

### 4. Email reports (optional)
**Settings → Email (SMTP)** → choose Gmail → enter your address and a **Gmail App Password**
(turn on 2-Step Verification, then create one at <https://myaccount.google.com/apppasswords>) → **Send test email**.

### 5. AI voice agent (optional)
1. Create a free project at <https://cloud.livekit.io> (copy the URL, API key, secret) and get an OpenAI API key.
2. **AI voice agent** page → paste the keys → **Save** → **Test LiveKit**.
3. Double-click **`agent
un_agent.bat`** (first run installs LiveKit Agents into `agent/.venv`) and keep it open.
4. **Test with PC mic** to talk to the agent from your PC, or **Call a phone number** to let it talk on a real call.
   In call mode the AI voice goes **into the call** (the caller hears it) through a USB audio adapter connected to the board's headset socket with a Y-splitter and two aux cables (Talk on calls → *Let the PC or the AI agent talk on calls*). B2Wave refuses to route it to PC speakers; optional listen-in.

```
Caller ⇄ A7672S (SPK/MIC) ⇄ USB sound card ⇄ B2Wave page (livekit-client) ⇄ LiveKit room ⇄ agent/agent.py (OpenAI Realtime)
```
`agent/requirements.txt` pins LiveKit Agents **1.2.16** (has LiveKit Inference). Newer releases load native DLLs that Windows Smart App Control blocks on some PCs. The worker uses a random free port for its health endpoint, so it never clashes with port 8081.

---

## Using it

- **Bulk SMS:** paste `number, name` lines or upload a CSV with a `number` column, write the message with variable chips, check the preview, then Send. Keep the gap at 3 s or more.
- **Voice campaign:** upload a short prompt (*"Press 1 to confirm, 2 to cancel"*), add numbers, Start. Watch live key presses, open any row's timeline, download the CSV or have it emailed.
- **Talking on calls:** this module's firmware (A131B01A7672M6) can't stream call audio over USB, so voice uses the board's **SPK/MIC pads**. See the **Talk on calls** page for wiring (a USB sound card for about ₹250 plus the built-in bridge is the recommended way to talk from the PC).

---

## Project structure

```
app/
  gsm_app.py        web server (aiohttp), WebSocket API, connection supervisor, storage
  modem.py          A7672S AT engine: SMS (text/UCS2), calls, URCs, DTMF, hold, CEER, file upload/playback
  campaign.py       voice campaigns + per-call analytics
  bulksms.py        bulk SMS runner with templates and retry
  mailer.py         SMTP email + HTML report templates
  livekit_api.py    LiveKit access tokens (with agent dispatch) + connection test
  backup.py         export / inspect / restore of all data as one .zip
  flasher.py        board detection, esptool flashing, USB diagnostics
  static/index.html single-page UI
  static/vendor/    pinned libraries: Lucide, Chart.js, SweetAlert2, Notyf, livekit-client
  data/             YOUR data: messages, calls, campaigns, audio, settings (git-ignored)
firmware/
  gsm_bridge/       ESP32-S3 Arduino firmware (Wi-Fi bridge, diagnostics, auto reset-level detection)
  bin/              prebuilt firmware flashed by the Setup page
  platformio.ini
agent/
  agent.py          LiveKit AI phone agent (persona arrives as dispatch metadata from B2Wave)
  run_agent.bat     one-click worker launcher (creates agent/.venv)
  .env              LiveKit + OpenAI keys written by B2Wave (git-ignored)
tools/
  board_debug.py    CLI: diag / log / at / reset4g / flash / sms (USB or Wi-Fi)
  build_firmware.py rebuild firmware/bin/gsm_bridge.bin with PlatformIO
  fake_modem.py     simulator: try the whole app without hardware
doc/                board pinout images
run.bat             one-click launcher
```

### Board pin map (KTRON ESP32-S3 + A7672S)
| Function | ESP32-S3 | Header |
|---|---|---|
| 4G TX / RX | GPIO17 / GPIO18 | J6 pin 19 / 21 |
| 4G RI / DTR / STATUS / RESET | GPIO16 / 9 / 10 / 13 | J6 pin 17 / 29 / 31 / 37 |
| LEDs Wi-Fi / 4G / SYS | GPIO11 / 12 / 14 | J6 pin 33 / 35 / 39 |
| Power in | VIN 5–12 V, 2 A | J9 pin 42 (GND 44) |

---

## Development

```bat
:: run the simulator and the app (no hardware needed)
app\.venv\Scripts\python tools\fake_modem.py
run.bat

:: talk to a real board
app\.venv\Scripts\python tools\board_debug.py diag
app\.venv\Scripts\python tools\board_debug.py at "AT+CSQ"

:: rebuild the ESP32 firmware (PlatformIO in its own venv)
python -m venv tools\.pio-venv
tools\.pio-venv\Scripts\python -m pip install platformio esptool
tools\.pio-venv\Scripts\python tools\build_firmware.py
```

- Logs: `app/data/app.log` (all modem traffic), `logs/diag-*.txt` (diagnostic reports).
- Separate data folder for testing: `set B2W_DATA=C:\path\to\testdata` before starting.
- Default Wi-Fi for the bridge firmware is set in `firmware/gsm_bridge/config.h`.

## Troubleshooting
| Problem | Fix |
|---|---|
| Board not found in Setup | Install the CP210x driver, use a data cable |
| "ESP32 to 4G serial link: no reply" | Use the 4G_USB cable (switch on 4G). This is the supported mode on this board. |
| Audio upload fails | Board must be online; uploads go in 8 KB chunks and the PC copy is always kept, so retry with "Module" |
| Gmail "login rejected" | Use an App Password, not your normal password |
| Hear ringing but can't talk | Expected: audio is on the SPK/MIC pads, see *Talk on calls* |
| Anything else | Setup → step 3 → **Diagnostics report** |

## Status / roadmap
- ✅ USB mode: SMS, bulk SMS, calls, campaigns, analytics, email reports
- ⚠️ Wi-Fi-only (standalone) mode: board status works; SMS/calls need the ESP32 → 4G serial link, which doesn't answer on this board revision
- 🔜 ESP32-S3 USB-host mode (switch on ESP) for true cable-free operation
- 🔜 PC voice via a USB sound card bridge or an I2S codec
