#!/usr/bin/env python3
import imaplib
import email
import time
from email.header import decode_header
import os
import re
import subprocess
import sys
import argparse

# =========================
# ACCOUNT CONFIG BLOCK
# =========================
ACCOUNTS = {
    "prod": {
        "IMAP_SERVER": "mail.tiborprotection.org",
        "IMAP_PORT": 993,
        "EMAIL_ACCOUNT": "toolboxes@tiborprotection.org",
        "EMAIL_PASSWORD": "Toolboxes123!",
    },

    # ---- Fill in your test account here ----
    "test": {
        "IMAP_SERVER": "imap.gmail.com",  # change if different
        "IMAP_PORT": 993,
        "EMAIL_ACCOUNT": "rjrseo@gmail.com",  # <-- put test email here
        "EMAIL_PASSWORD": "uapi jeet xyji mpnj",          # <-- put test password here
    },
}
# =========================

# ---- Piper config ----
PIPER_DIR = os.path.expanduser("~/toolboxes/piper")
PIPER_BIN = os.path.join(PIPER_DIR, "piper")
PIPER_MODEL = os.path.join(PIPER_DIR, "en_US-lessac-medium.onnx")

OUTPUT_DIR = "emails_audio"  # WAV output folder
CHECK_INTERVAL_DEFAULT = 15  # seconds


# ===== Helper Functions =====

def announce_startup(account_name, mailbox, mode):
    msg = f"Mail reader online. Account {account_name}. Mailbox {mailbox}. Mode {mode}."
    try:
        piper_speak(msg, "/tmp/mail_startup.wav")
    except Exception as e:
        print(f"⚠️ Startup announcement failed: {e}")



def connect(cfg: dict, mailbox: str):
    mail = imaplib.IMAP4_SSL(cfg["IMAP_SERVER"], cfg["IMAP_PORT"])
    mail.login(cfg["EMAIL_ACCOUNT"], cfg["EMAIL_PASSWORD"])
    mail.select(mailbox)
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


def get_first_text_part(msg) -> str:
    """
    "Simple" mode: minimal parsing. Just returns the first text-ish part we can find.
    Prefers text/plain, falls back to text/html stripped-ish.
    """
    def decode_payload(part):
        payload = part.get_payload(decode=True)
        if not payload:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="ignore")
        except Exception:
            return payload.decode(errors="ignore")

    if msg.is_multipart():
        # Prefer text/plain
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if ctype == "text/plain":
                text = decode_payload(part)
                if text.strip():
                    return text

        # Fall back to text/html (very light cleanup)
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if ctype == "text/html":
                html = decode_payload(part)
                # minimal tag stripping
                text = re.sub(r"<[^>]+>", " ", html)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    return text
    else:
        text = msg.get_payload(decode=True)
        if text:
            try:
                return text.decode(errors="ignore")
            except Exception:
                return str(text)

    return ""


def get_email_body_pretty(msg) -> str:
    """
    "Normal" mode: prefer text/plain body and ignore attachments.
    """
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition") or "").lower():
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(errors="ignore")
    return ""


def piper_speak(text: str, wav_path: str):
    if not os.path.exists(PIPER_BIN):
        raise FileNotFoundError(f"Piper binary not found: {PIPER_BIN}")
    if not os.path.exists(PIPER_MODEL):
        raise FileNotFoundError(f"Piper model not found: {PIPER_MODEL}")

    text = (text or "").strip()
    if not text:
        return

    # Prevent super long reads
    if len(text) > 4000:
        text = text[:4000] + "… message truncated."

    subprocess.run(
        [PIPER_BIN, "--model", PIPER_MODEL, "--output_file", wav_path],
        input=text.encode("utf-8"),
        check=True
    )
    subprocess.run(["aplay", "-q", wav_path], check=True)


def parse_args():
    p = argparse.ArgumentParser(description="IMAP email reader with Piper TTS.")
    p.add_argument("--account", default="prod", choices=sorted(ACCOUNTS.keys()),
                   help="Which account config to use (prod/test).")
    p.add_argument("--mode", default="normal", choices=["normal", "simple"],
                   help="normal = From/Subject/Body; simple = read message straight through.")
    p.add_argument("--mailbox", default="INBOX", help="Mailbox to monitor (default INBOX).")
    p.add_argument("--interval", type=int, default=CHECK_INTERVAL_DEFAULT, help="Check interval seconds.")
    return p.parse_args()


def main():
    args = parse_args()

    cfg = ACCOUNTS.get(args.account)
    if not cfg:
        print(f"Unknown account: {args.account}", file=sys.stderr)
        sys.exit(2)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    mail = connect(cfg, args.mailbox)
    seen_ids = set(get_unseen_emails(mail))
    print(f"Monitoring {cfg['EMAIL_ACCOUNT']} / {args.mailbox} (mode={args.mode})... ({len(seen_ids)} unseen)")

    # Speak once on startup
    announce_startup(args.account, args.mailbox, args.mode)


    while True:
        try:
            time.sleep(args.interval)
            current_ids = set(get_unseen_emails(mail))
            new_ids = current_ids - seen_ids

            for email_id in new_ids:
                status, msg_data = mail.fetch(email_id, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(msg_data[0][1])

                sender = msg.get("From", "Unknown sender")
                subject_raw = msg.get("Subject", "")
                subject = clean_subject(subject_raw)

                if args.mode == "simple":
                    # No fancy parsing: just grab first available text and read it.
                    text = get_first_text_part(msg)
                    if text.strip():
                        wav_file = os.path.join(OUTPUT_DIR, f"{email_id.decode()}_{subject or 'No_Subject'}.wav")
                        print(f"📬 NEW EMAIL (simple) from {sender} → reading straight through...")
                        piper_speak(text, wav_file)
                    else:
                        print(f"📬 NEW EMAIL (simple) from {sender} → no readable text found")

                else:
                    # Normal: announce sender/subject then read body
                    body = get_email_body_pretty(msg)
                    if body.strip():
                        header_wav = os.path.join(OUTPUT_DIR, f"{email_id.decode()}_header.wav")
                        body_wav = os.path.join(OUTPUT_DIR, f"{email_id.decode()}_{subject}.wav")
                        header = f"New email from {sender}. Subject: {subject or 'No Subject'}."
                        print(f"📬 NEW EMAIL from {sender}, Subject: {subject} → Playing audio...")
                        piper_speak(header, header_wav)
                        piper_speak(body, body_wav)
                    else:
                        print(f"📬 NEW EMAIL from {sender}, Subject: {subject} → No text to read")

            seen_ids = current_ids

        except Exception as e:
            print(f"⚠️ Error: {e}. Reconnecting...")
            time.sleep(5)
            try:
                mail = connect(cfg, args.mailbox)
                seen_ids = set(get_unseen_emails(mail))
            except Exception as e2:
                print(f"❌ Reconnect failed: {e2}")
                time.sleep(10)


if __name__ == "__main__":
    main()
