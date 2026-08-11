"""One-time interactive setup: creates .env from .env.example if it doesn't
exist yet, and prompts for a Gemini API key if one isn't set — run by
setup.bat so a first-time user never has to hand-edit .env in a text editor.

Safe to re-run: leaves an already-configured .env untouched.
"""
import shutil
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"
EXAMPLE_PATH = Path(__file__).parent / ".env.example"
PLACEHOLDER = "your_gemini_api_key_here"


def main() -> None:
    if not ENV_PATH.exists():
        shutil.copy(EXAMPLE_PATH, ENV_PATH)
        print("Created backend/.env from the template.")

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    key_line_idx = next((i for i, line in enumerate(lines) if line.startswith("GOOGLE_API_KEY=")), None)
    current_value = lines[key_line_idx].split("=", 1)[1].strip() if key_line_idx is not None else ""

    if current_value and current_value != PLACEHOLDER:
        print("backend/.env already has a Gemini API key set - leaving it as is.")
        return

    print()
    print("You'll need a free Gemini API key to translate text.")
    print("Get one at: https://aistudio.google.com/apikey")
    print()
    key = input("Paste your Gemini API key here (or press Enter to add it later): ").strip()

    if not key:
        print("Skipped - open backend\\.env in Notepad later and paste your key after GOOGLE_API_KEY=")
        return

    new_line = f"GOOGLE_API_KEY={key}"
    if key_line_idx is not None:
        lines[key_line_idx] = new_line
    else:
        lines.insert(0, new_line)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Saved to backend/.env.")


if __name__ == "__main__":
    main()
