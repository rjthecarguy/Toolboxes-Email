#!/usr/bin/env python3
import os
import subprocess
import sys

BASE = os.path.expanduser("~/toolboxes/piper")
PIPER_BIN = os.path.join(BASE, "piper")
MODEL = os.path.join(BASE, "en_US-lessac-medium.onnx")
WAV_OUT = "/tmp/piper_tts.wav"

def speak(text: str) -> None:
    if not os.path.exists(PIPER_BIN):
        raise FileNotFoundError(f"Missing piper binary at: {PIPER_BIN}")
    if not os.path.exists(MODEL):
        raise FileNotFoundError(f"Missing model at: {MODEL}")

    # Generate wav
    subprocess.run(
        [PIPER_BIN, "--model", MODEL, "--output_file", WAV_OUT],
        input=text.encode("utf-8"),
        check=True
    )
    # Play wav
    subprocess.run(["aplay", "-q", WAV_OUT], check=True)

def main():
    # If text was passed as args, use it. Otherwise read stdin.
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        text = sys.stdin.read().strip()

    if not text:
        print("Usage: speak.py \"text to say\"  OR  echo \"text\" | speak.py", file=sys.stderr)
        sys.exit(2)

    speak(text)

if __name__ == "__main__":
    main()
