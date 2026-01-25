import imaplib
import email
import time
from email.header import decode_header
import os
import re
import subprocess

# ===== CONFIG =====
IMAP_SERVER = "mail.tiborprotection.org"
IMAP_PORT = 993
EMAIL_ACCOUNT = "toolboxes@tiborprotection.org"
EMAIL_PASSWORD = "Toolboxes123!"
CHECK_INTERVAL = 15  # seconds

OUTPUT_DIR = "emails_audio"  # folder to store WAVs

# ---- PIPER CONFIG ----
PIPER_DIR = os.path.expanduser("~/toolboxes/piper")
PIPER_BIN = os.path.join(PIPER_DIR, "piper")
PIPER_MODEL = os.path.join(PIPER_DIR, "en_US-lessac-medium.onnx")
# ----------------------

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ===== Helper Functions =====
def connect():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select("INBOX")
    return mail

def clean_subject(subject):
    if not subject:
        return "No_Subject"
    decoded, encoding = decode_header(subject)[0]
    if isinstance(decoded, bytes):
        decoded = decoded.decode(encoding or "utf-8", errors="ignore")
    decoded = decoded.strip()
    decoded = re.sub(r"\s+", " ", decoded)
    return "".join(c for c in decoded if c.isalnum() or c in (" ", "_", "-")).strip()[:80]

def get_unseen_emails(mail):
    status, data = mail.search(None, "UNSEEN")
    if status != "OK":
        return []
    return data[0].split()

def get_email_body(msg):
    # Prefer text/plain; ignore attachments
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp.lower():
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(errors="ignore")
    return ""

def piper_tts_to_wav_and_play(text, wav_path):
    """
    Generates WAV via Piper and plays it with aplay.
    """
    if not os.path.exists(PIPER_BIN):
        raise FileNotFoundError(f"Piper binary not found: {PIPER_BIN}")
    if not os.path.exists(PIPER_MODEL):
        raise FileNotFoundError(f"Piper model not found: {PIPER_MODEL}")

    # Keep spoken text reasonable (emails can be huge)
    text = text.strip()
    if len(text) > 2000:
        text = text[:2000] + "… Email truncated."

    # Generate wav
    subprocess.run(
        [PIPER_BIN, "--model", PIPER_MODEL, "--output_file", wav_path],
        input=text.encode("utf-8"),
        check=True
    )
    # Play wav quietly (remove -q if you want aplay output)
    subprocess.run(["aplay", "-q", wav_path], check=True)

# ===== Main Loop =====
def main():
    mail = connect()
    seen_ids = set(get_unseen_emails(mail))
    print(f"Monitoring inbox... ({len(seen_ids)} unseen emails)")

    while True:
        try:
            time.sleep(CHECK_INTERVAL)
            current_ids = set(get_unseen_emails(mail))
            new_ids = current_ids - seen_ids

            for email_id in new_ids:
                status, msg_data = mail.fetch(email_id, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(msg_data[0][1])

                sender = msg.get("From", "Unknown sender")
                subject = clean_subject(msg.get("Subject"))
                body = get_email_body(msg)

                if body.strip():
                    wav_file = os.path.join(OUTPUT_DIR, f"{email_id.decode()}_{subject}.wav")

                    # Speak a short header, then the body
                    header = f"New email from {sender}. Subject: {subject}."
                    print(f"📬 NEW EMAIL from {sender}, Subject: {subject} → Playing audio...")

                    # Speak header first
                    piper_tts_to_wav_and_play(header, os.path.join(OUTPUT_DIR, f"{email_id.decode()}_header.wav"))
                    # Then body
                    piper_tts_to_wav_and_play(body, wav_file)
                else:
                    print(f"📬 NEW EMAIL from {sender}, Subject: {subject} → No text to read")

            seen_ids = current_ids

        except Exception as e:
            print(f"⚠️ Connection lost or error: {e}. Reconnecting...")
            time.sleep(5)
            try:
                mail = connect()
                seen_ids = set(get_unseen_emails(mail))
            except Exception as e2:
                print(f"❌ Reconnect failed: {e2}")
                time.sleep(10)

if __name__ == "__main__":
    main()
