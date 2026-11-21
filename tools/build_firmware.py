"""
Developer tool: compile the ESP32 firmware and pack it into one file
(firmware/bin/gsm_bridge.bin) that the app's Setup page flashes at 0x0.

    tools\\.pio-venv\\Scripts\\python tools\\build_firmware.py
(end users never need this - the .bin is shipped prebuilt)
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware"
BUILD = FW / ".pio" / "build" / "esp32s3"
OUT = FW / "bin"
PIO_PKGS = Path.home() / ".platformio" / "packages"


def main():
    subprocess.run([sys.executable, "-m", "platformio", "run"], cwd=FW, check=True)
    boot_app0 = next(PIO_PKGS.glob("framework-arduinoespressif32*/tools/partitions/boot_app0.bin"))
    OUT.mkdir(exist_ok=True)
    subprocess.run([sys.executable, "-m", "esptool", "--chip", "esp32s3", "merge-bin",
                    "-o", str(OUT / "gsm_bridge.bin"), "--flash-mode", "keep",
                    "--flash-freq", "keep", "--flash-size", "16MB",
                    "0x0", str(BUILD / "bootloader.bin"),
                    "0x8000", str(BUILD / "partitions.bin"),
                    "0xe000", str(boot_app0),
                    "0x10000", str(BUILD / "firmware.bin")], check=True)
    ver = re.search(r'#define FW_VERSION "([^"]+)"',
                    (FW / "gsm_bridge" / "gsm_bridge.ino").read_text()).group(1)
    (OUT / "version.json").write_text(json.dumps({"version": ver, "chip": "esp32s3"}))
    print(f"\nOK -> {OUT / 'gsm_bridge.bin'}  (v{ver})")


if __name__ == "__main__":
    main()
