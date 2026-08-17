"""Builds naver-webtoon-translator-firefox.zip, ready to upload to
addons.mozilla.org for signing.

AMO requires the manifest file inside the zip to be literally named
manifest.json, so this packages a copy of manifest.firefox.json under that
name alongside the shared extension files, rather than touching the Chrome
manifest.json in place.

Uses Python's zipfile with explicit forward-slash arcnames (regardless of
host OS) rather than PowerShell's Compress-Archive, which was confirmed to
produce archives some tools mis-parse.

Usage: run with the backend venv's Python (or any Python 3):
    ../backend/.venv/Scripts/python.exe package_firefox.py
"""

import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
FILES = ["background.js", "content.js", "content.css"]
FONTS_DIR = "fonts"
ZIP_PATH = ROOT / "naver-webtoon-translator-firefox.zip"


def main() -> None:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(ROOT / "manifest.firefox.json", arcname="manifest.json")

        for name in FILES:
            zf.write(ROOT / name, arcname=name)

        for path in sorted((ROOT / FONTS_DIR).iterdir()):
            if path.is_file():
                zf.write(path, arcname=f"{FONTS_DIR}/{path.name}")

    print(f"Built {ZIP_PATH}")


if __name__ == "__main__":
    main()
