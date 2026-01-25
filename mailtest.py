#!/usr/bin/env python3
"""
Talking Gmail Monitor (new mail only)
- On startup, captures current highest UID in INBOX (baseline)
- Speaks ONLY messages that arrive AFTER startup (UID > baseline)
- Uses IMAP IDLE (IMAPClient) + Piper TTS + aplay
"""

import os
import ssl
import time
import email
import tempfile
import subprocess
from imapclient import IMAPClient

# -------------------------
# CONFIG (EDIT THESE)
# -------------------------
#EMAIL_ADDRESS = "rjrseo@gmail.com"
#APP_PASSWORD = "uapi jeet xyji mpnj"  # Use a Google App Password (recommended)


#HOST = "imap.gmail.com"

EMAIL_ADDRESS = "toolboxes@tiborprotection.org"
APP_PASSWORD = "Toolboxes123!"  # Use a Google App Password (recommended)


HOST = "mail.tiborprotection.org"

# Path to your Piper .onnx model file
PIPER_MODEL = "/home/rj/piper-voices/en_US-amy-medium/en_US-amy-medium.onnx"

# Gmail often drops longer IDLE sessions; recycle periodically
IDLE_SECONDS = 60 * 29

# Max chars of email body to read aloud
BODY_PREVIEW_CHARS = 200

# Optional: reduce onnxruntime logging noise
os.environ.setdefault("ORT_LOG_SEVERITY_LEVEL", "3")


# -------------------------
# HELPERS
# -------------------------
def decode_mime_words(s):
    if not s:
        return ""
    parts = email.header.decode_header(s)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def clean_for_speech(text):
    text = (text or "").replace("\r", " ").replace("\n", " ")
    while "  " in text:
        text = text.replace("  ", " ")
    return text.strip()


def get_body_snippet(msg, max_len=BODY_PREVIEW_CHARS):
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = (part.get("Content-Disposition") or "").lower()
                if ctype == "text/plain" and "attachment" not in disp:
                    payload = part.get_payload(decode=True)
                    if payload:
                        return clean_for_speech(payload.decode(errors="replace")[:max_len])
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                return clean_for_speech(payload.decode(errors="replace")[:max_len])
    except Exception:
        pass
    return ""


def speak_piper(text):
    text = clean_for_speech(text)
    if not text:
        return

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
        subprocess.run(
            ["piper", "--model", PIPER_MODEL, "--output_file", f.name],
            input=text.encode("utf-8"),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,   # <- this removes the GPU discovery line
        )
        subprocess.run(["aplay", f.name], check=True)


def connect():
    context = ssl.create_default_context()
    server = IMAPClient(HOST, ssl=True, ssl_context=context)
    server.login(EMAIL_ADDRESS, APP_PASSWORD)
    server.select_folder("INBOX")
    return server


def get_highest_uid(server):
    # IMAPClient provides a direct way to get UIDNEXT (next uid that will be assigned)
    status = server.folder_status("INBOX", ["UIDNEXT"])
    # UIDNEXT is "next uid", so the current highest UID is UIDNEXT - 1
    uidnext = int(status.get(b"UIDNEXT") or status.get("UIDNEXT"))
    return max(0, uidnext - 1)


def fetch_message(server, uid):
    raw = server.fetch([uid], ["RFC822"])[uid][b"RFC822"]
    return email.message_from_bytes(raw)


# -------------------------
# MAIN
# -------------------------
def main():
    print("📡 Starting talking email monitor (new mail only)...")

    server = connect()

    # Baseline: ignore everything up to the current highest UID
    last_seen_uid = get_highest_uid(server)
    print(f"✅ Baseline UID set to {last_seen_uid} (older mail will be ignored)")

    speak_piper("Email monitor started.")

    try:
        while True:
            try:
                server.idle()
                responses = server.idle_check(timeout=IDLE_SECONDS)
                server.idle_done()

                # New message(s) indicated
                if any(r[1] == b"EXISTS" for r in responses):
                    # Only look for unseen messages with UID greater than our baseline
                    # IMAP SEARCH uses "UID X:*" to specify UID range
                    uids = server.search(["UNSEEN", "UID", f"{last_seen_uid + 1}:*"])

                    for uid in sorted(uids):
                        msg = fetch_message(server, uid)

                        subject = decode_mime_words(msg.get("Subject")) or "No subject"
                        sender = decode_mime_words(msg.get("From")) or "Unknown sender"
                        snippet = get_body_snippet(msg, BODY_PREVIEW_CHARS)

                        print("\n📬 New email (post-start):")
                        print("From:", sender)
                        print("Subject:", subject)
                        if snippet:
                            print("Preview:", snippet)
                        print("-" * 60)

                        if snippet:
                            speak_piper(f"New email from {sender}. Subject: {subject}. Message says: {snippet}")
                        else:
                            speak_piper(f"New email from {sender}. Subject: {subject}.")

                        # Advance baseline as we successfully handle each one
                        if uid > last_seen_uid:
                            last_seen_uid = uid

                time.sleep(1)

            except KeyboardInterrupt:
                raise

            except Exception as e:
                print("⚠️ Connection error; reconnecting...", repr(e))
                try:
                    try:
                        server.logout()
                    except Exception:
                        pass
                    time.sleep(5)
                    server = connect()
                    # Re-establish baseline to current highest UID after reconnect,
                    # so we still don't read backlog.
                    last_seen_uid = get_highest_uid(server)
                    print(f"🔄 Reconnected. Baseline UID reset to {last_seen_uid}.")
                except Exception as e2:
                    print("⚠️ Reconnect failed; retrying in 10s...", repr(e2))
                    time.sleep(10)

    except KeyboardInterrupt:
        print("\n🛑 Stopping monitor.")
        try:
            speak_piper("Email monitor stopped.")
        except Exception:
            pass
        try:
            server.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
