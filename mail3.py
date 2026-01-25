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
import threading
import queue

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

    "test": {
        "IMAP_SERVER": "mail.tiborprotection.org",
        "IMAP_PORT": 993,
        "EMAIL_ACCOUNT": "testaddress@tiborprotection.org",
        "EMAIL_PASSWORD": "PUT_TEST_PASSWORD_HERE",
    },
}
# =========================

# ---- Piper config ----
PIPER_DIR = os.path.expanduser("~/toolboxes/piper")
PIPER_BIN = os.path.join(PIPER_DIR, "piper")
PIPER_MODEL = os.path.join(PIPER_DIR, "en_US-lessac-medium.onnx")

OUTPUT_DIR = "emails_audio"
CHECK_INTERVAL_DEFAULT = 15


# ===== Helper Functions =====
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
    p = argparse.ArgumentParser(description="IMAP email reader with Piper TTS (optimized).")
    p.add_argument("--account", default="prod", choices=sorted(ACCOUNTS.keys()),
                   help="Which account config to use (prod/test).")
    p.add_argument("--mode", default="normal", choices=["normal", "simple"],
                   help="normal = From/Subject/Body; simple = read message straight through.")
    p.add_argument("--mailbox", default="INBOX", help="Mailbox to monitor (default INBOX).")
    p.add_argument("--interval", type=int, default=CHECK_INTERVAL_DEFAULT, help="Check interval seconds.")
    p.add_argument("--keepalive", type=int, default=120,
                   help="Send IMAP NOOP every N seconds to keep connection alive.")
    return p.parse_args()


# ---------- IMAP fetch helpers (faster than RFC822) ----------
def uid_search_unseen(mail):
    # UID search avoids sequence-number weirdness and is stable across sessions
    status, data = mail.uid("search", None, "UNSEEN")
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def uid_fetch_headers(mail, uid: bytes):
    # Fetch only From/Subject (cheap) and do not set \Seen
    status, data = mail.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
    if status != "OK" or not data or not data[0]:
        return {}
    raw = data[0][1]
    msg = email.message_from_bytes(raw)
    return {
        "from": msg.get("From", "Unknown sender"),
        "subject_raw": msg.get("Subject", ""),
    }


def uid_fetch_best_text(mail, uid: bytes) -> str:
    """
    Fetch message structure, then fetch only the best text part using BODY.PEEK.
    Falls back to fetching TEXT if structure parsing fails.
    """
    # 1) Get BODYSTRUCTURE/structure hints
    status, data = mail.uid("fetch", uid, "(BODYSTRUCTURE)")
    if status != "OK" or not data:
        return ""

    # If BODYSTRUCTURE parsing is too messy, fallback to a simpler approach:
    # fetch the whole TEXT section (still smaller than RFC822 for many servers).
    # Not all servers support BODY.PEEK[TEXT] well, but most do.
    status2, data2 = mail.uid("fetch", uid, "(BODY.PEEK[TEXT])")
    if status2 == "OK" and data2 and data2[0] and isinstance(data2[0], tuple):
        raw = data2[0][1] or b""
        # Build a synthetic message so we can reuse email parsing
        try:
            msg = email.message_from_bytes(raw)
            # If it's not a full message, decoding may be weird; just decode bytes
        except Exception:
            msg = None

        if msg and msg.is_multipart():
            return extract_first_text_part(msg)
        else:
            # best-effort decode
            try:
                return raw.decode("utf-8", errors="ignore")
            except Exception:
                return raw.decode(errors="ignore")

    return ""


def extract_first_text_part(msg) -> str:
    """
    Walk an email.message.Message and return first good text/plain,
    else fallback to cleaned html.
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
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if ctype == "text/plain":
                text = decode_payload(part)
                if text.strip():
                    return text

        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if ctype == "text/html":
                html = decode_payload(part)
                text = re.sub(r"<[^>]+>", " ", html)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    return text
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            try:
                return payload.decode(errors="ignore")
            except Exception:
                return str(payload)

    return ""


# ---------- Worker thread for TTS/audio ----------
def audio_worker(audio_q: "queue.Queue[dict]"):
    while True:
        item = audio_q.get()
        if item is None:
            return

        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)

            kind = item.get("kind", "body")
            uid_str = item.get("uid_str", "unknown")
            subject = item.get("subject", "No_Subject")
            sender = item.get("sender", "Unknown sender")
            text = item.get("text", "")

            if kind == "header":
                wav = os.path.join(OUTPUT_DIR, f"{uid_str}_header.wav")
                piper_speak(text, wav)
            else:
                wav = os.path.join(OUTPUT_DIR, f"{uid_str}_{subject}.wav")
                piper_speak(text, wav)

        except Exception as e:
            print(f"⚠️ Audio worker error: {e}", file=sys.stderr)
        finally:
            audio_q.task_done()


def main():
    args = parse_args()
    cfg = ACCOUNTS.get(args.account)
    if not cfg:
        print(f"Unknown account: {args.account}", file=sys.stderr)
        sys.exit(2)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Start audio worker
    audio_q: "queue.Queue[dict]" = queue.Queue()
    t = threading.Thread(target=audio_worker, args=(audio_q,), daemon=True)
    t.start()

    mail = connect(cfg, args.mailbox)
    seen_uids = set(uid_search_unseen(mail))  # baseline

    print(f"Monitoring {cfg['EMAIL_ACCOUNT']} / {args.mailbox} (mode={args.mode})... ({len(seen_uids)} unseen)")

    last_keepalive = time.time()

    while True:
        try:
            time.sleep(args.interval)

            # Keepalive
            now = time.time()
            if args.keepalive > 0 and (now - last_keepalive) >= args.keepalive:
                try:
                    mail.noop()
                except Exception:
                    # will reconnect in outer except
                    raise
                last_keepalive = now

            current_uids = set(uid_search_unseen(mail))
            new_uids = current_uids - seen_uids

            # Process new messages quickly, enqueue audio work
            for uid in sorted(new_uids, key=lambda x: int(x)):  # stable order
                hdr = uid_fetch_headers(mail, uid)
                sender = hdr.get("from", "Unknown sender")
                subject_raw = hdr.get("subject_raw", "")
                subject = clean_subject(subject_raw)

                uid_str = uid.decode(errors="ignore")

                if args.mode == "simple":
                    # Fetch best text and read it
                    # (We try TEXT fetch; if it returns partial, still OK for simple mode)
                    # If TEXT fetch returns raw bytes only, we decode best-effort
                    status, data = mail.uid("fetch", uid, "(BODY.PEEK[])")
                    text = ""
                    if status == "OK" and data and data[0] and isinstance(data[0], tuple):
                        msg = email.message_from_bytes(data[0][1])
                        text = extract_first_text_part(msg)

                    if text.strip():
                        print(f"📬 NEW EMAIL (simple) from {sender} → queued for audio")
                        audio_q.put({
                            "kind": "body",
                            "uid_str": uid_str,
                            "subject": subject or "No_Subject",
                            "sender": sender,
                            "text": text
                        })
                    else:
                        print(f"📬 NEW EMAIL (simple) from {sender} → no readable text found")

                else:
                    # Normal: enqueue header + body separately
                    # Fetch body without grabbing full RFC822 when possible
                    status, data = mail.uid("fetch", uid, "(BODY.PEEK[])")
                    body = ""
                    if status == "OK" and data and data[0] and isinstance(data[0], tuple):
                        msg = email.message_from_bytes(data[0][1])
                        body = extract_first_text_part(msg)

                    header_text = f"New email from {sender}. Subject: {subject or 'No Subject'}."
                    print(f"📬 NEW EMAIL from {sender}, Subject: {subject} → queued for audio")

                    audio_q.put({
                        "kind": "header",
                        "uid_str": uid_str,
                        "subject": subject or "No_Subject",
                        "sender": sender,
                        "text": header_text
                    })
                    if body.strip():
                        audio_q.put({
                            "kind": "body",
                            "uid_str": uid_str,
                            "subject": subject or "No_Subject",
                            "sender": sender,
                            "text": body
                        })
                    else:
                        print(f"   ↳ No text body found to read.")

            seen_uids = current_uids

        except Exception as e:
            print(f"⚠️ Error: {e}. Reconnecting...")
            time.sleep(3)
            try:
                mail = connect(cfg, args.mailbox)
                seen_uids = set(uid_search_unseen(mail))
                last_keepalive = time.time()
            except Exception as e2:
                print(f"❌ Reconnect failed: {e2}")
                time.sleep(10)


if __name__ == "__main__":
    main()
