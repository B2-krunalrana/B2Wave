# B2Wave: SMS, calls and voice campaigns from your browser

Board: **KTRON ESP32-S3 + SIMCom A7672S 4G DevKit** (KSTM2098)

```
 Browser (PC / phone)  <--WebSocket-->  Python app (gsm_app.py)  <--Wi-Fi TCP 7000-->  ESP32-S3  <--UART-->  A7672S  <--LTE-->  network
                                                                  (or USB cable straight to the 4G_USB port)
```

## What works now (Phase 1)
- Send and receive SMS (English and Unicode such as Hindi or emoji), shown as chat threads and saved on the PC
- Place, answer and hang up calls, with a DTMF keypad, call log, ringtone and desktop notifications
- Live signal, operator and SIM status, plus an AT command console
- Auto-discovers the board on your Wi-Fi. No IP setup needed.

> **Voice audio in Phase 1** goes through the board's **SPEAKER / MIC pads** (back side).
> Getting voice into your PC speaker and mic is Phase 2 (see the end of this file).

---

## Step 1: Hardware
1. Insert a **nano SIM** with no PIN lock, contacts down, into the slot on the back.
2. Connect the **LTE antenna** to the `LTE` U.FL connector. Without it, calls and SMS fail.
3. Optional, for call audio: connect an 8 to 32 Ω speaker to `SPK` and an electret mic to `MIC`.
4. Plug USB-C into the **ESP_UART** port (the USB-C port nearer the SIMCom chip).
   For 4G power, a 5–12 V 2 A supply on VIN is the most reliable. USB alone can brown out on LTE peaks.

## Step 2: Run the app. It does the rest.
Double-click **`run.bat`**. The first run installs Python packages into `app/.venv`, then your browser opens http://127.0.0.1:8080 on the **Setup** page:

| Step | What happens |
|---|---|
| 1 · Connect | Click **Detect board**. It finds the ESP32-S3 on the ESP_UART USB port. |
| 2 · Install | One click flashes the prebuilt firmware with esptool, with a progress bar. **No Arduino IDE.** |
| 3 · Test | Checks the ESP32 firmware, 4G module, SIM, signal and network registration, and tells you what to fix. |
| 4 · Link | **Wi-Fi:** pick your network and type the password; the board joins it. **USB:** move the cable to 4G_USB and set USB_SELECT to 4G; the page detects the port. |
| 5 · Done | Opens Messages and Phone. |

- If flashing can't connect: hold **BOOT**, tap **RESET**, release BOOT, then click Install again.
- If no COM port appears at all: install the CH343 USB driver (link shown on the page).
- `run.bat --lan` lets you open the app from your phone at `http://<pc-ip>:8080`.

Developers: rebuild the firmware with `tools\.pio-venv\Scripts\python tools\build_firmware.py` (writes `firmware/bin/gsm_bridge.bin`).

### Alternative: no Wi-Fi, USB cable only
Set the **USB_SELECT** switch to `4G` and plug USB-C into the **4G_USB** port. Install the SIMCom A76xx USB driver if Windows shows unknown devices. In the app, choose Settings > USB cable > the "AT" COM port.

### Try it without the board
```
app\.venv\Scripts\python tools\fake_modem.py     (then start run.bat; Auto finds it)
```
In the fake-modem window, type `sms +911234567890 hi` or `ring +911234567890`.

---

## Troubleshooting
| Symptom | Fix |
|---|---|
| Status page says "Modem NOT responding" | Use a VIN 5–12 V supply, then press "Hard-reset 4G module". The firmware already tries TX/RX in both directions. |
| `SIM: NOT INSERTED` / `SIM PIN` | Reseat the SIM. Remove the PIN lock in a phone. |
| Network "searching" / "denied" | Check the antenna. Check that the SIM supports 4G VoLTE (Jio needs VoLTE; Airtel and Vi also work on 2G). |
| Calls connect but no sound | Phase 1 audio is on the board's SPK/MIC pads. Try `AT+CLVL=5` in the Console. |
| "Mobile AT Interface" has a yellow warning in Device Manager | Install the SIMCom A76xx driver: [A7600X-Windows-Driver.7z](https://files.waveshare.com/wiki/ESP32-S3-A7670E-4G/A7600X-Windows-Driver.7z). Extract it, then in Device Manager choose Update driver, then Browse, pick the folder and tick Include subfolders. Afterwards you should see "SimTech HS-USB AT Port 9011". |
| No COM port for the board (CP2102 shows an error) | Install the [CP210x driver](https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers) |
| SMS fails with `+CMS ERROR` | No credit or SMS centre missing. Check `AT+CSCA?` in the Console. |

Files: `app/` is the Python app (`modem.py` = AT engine, `gsm_app.py` = server, `static/index.html` = UI). `firmware/` is the ESP32 bridge. `app/data/` holds your saved messages, calls and settings.

## Voice campaigns (batch IVR calls)
Open **Campaigns**:
1. Upload any audio file. The browser converts it to 8 kHz telephone WAV and stores it on the 4G module (about 20 s fits). You can use English text-to-speech instead.
2. Paste phone numbers, choose the prompt, and click **Start campaign**.
3. Each number is dialled. When answered, the prompt plays and any key the person presses (1, 2, ...) is captured live and saved per number. Download the results as CSV.

## Talk on calls (Audio page)
The module firmware (A131B01A7672M6) can't stream call audio over USB, so voice uses the SPK/MIC pads. The **Audio** page has wiring diagrams for:
- **A:** a speaker and mic on the pads
- **B:** a USB sound card, plus the built-in PC audio bridge
- **C:** an I2S codec, for voice over Wi-Fi

## Old notes: voice through the PC speaker and mic
The A7672S has no digital audio over its UART, so voice can't travel over the same Wi-Fi link as AT commands. Options:
1. **USB voice (no extra hardware):** with the 4G_USB cable to the PC, the A7672S can stream call audio as PCM over USB (`AT+CPCMREG`). The app would relay it to the browser mic and speaker over WebRTC/WebSocket.
2. **Wi-Fi voice (extra hardware):** add an I2S mic and amp/ADC module wired to the SPK/MIC pads. The ESP32 streams that audio over Wi-Fi to the app and browser.
