#!/usr/bin/env python3
"""
IMAP (IDLE) -> read NEW messages -> extract the actual message body
-> REMOVE forwarded header junk + REMOVE signatures/footers
-> convert to speech with Piper
-> SAVE WAV files in ./emails_audio (same directory as script)
-> play via aplay
"""

import os
import sys
import ssl
import time
import subprocess
import re
from html import unescape
import email
import email.header
from imapclient import IMAPClient

# -------------------------
# CONFIG
# -------------------------
EMAIL_ADDRESS = "toolboxes@tiborprotection.org"
APP_PASSWORD = "Toolboxes123!"
HOST = "mail.tiborprotection.org"

PIPER_MODEL = "/home/rj/piper-voices/en_US-amy-medium/en_US-amy-medium.onnx"

IDLE_SECONDS = 60 * 29
MAX_SPEAK_CHARS = 1200

# 🔑 Audio folder ALWAYS next to this script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EMAIL_AUDIO_DIR = os.path.join(SCRIPT_DIR, "emails_audio")

# Reduce onnxruntime noise
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

def safe_filename(s, max_len=30):
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", s or "email")
    s = re.sub(r"_+", "_", s).strip("_")
    return (s[:max_len] or "email")


# -------------------------
# HTML -> TEXT
# -------------------------
def html_to_text(html):
    html = unescape(html or "")
    # preserve basic breaks
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p\s*>", "\n", html)
    html = re.sub(r"(?i)</div\s*>", "\n", html)
    # drop tags
    html = re.sub(r"<[^>]+>", " ", html)
    # normalize whitespace
    html = html.replace("\r", "")
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


# -------------------------
# BODY CLEANING (headers + signatures/footers)
# -------------------------
JUNK_LINE_PREFIXES = (
    "return-path:", "delivered-to:", "received:", "dkim-signature:",
    "x-", "mime-version:", "content-type:", "content-transfer-encoding:",
    "message-id:", "in-reply-to:", "references:", "date:", "from:", "to:",
    "sent:", "subject:", "reply-to:", "cc:", "bcc:",
    "envelope-to:", "delivery-date:",
)

FORWARDED_MARKERS = (
    "----- forwarded message -----",
    "begin forwarded message",
    "end forwarded message",
)

FOOTER_MARKERS_CONTAINS = (
    "confidentiality notice",
    "intended recipient",
    "this message is intended",
    "if you are not the intended",
    "please consider the environment",
    "unsubscribe",
    "privacy policy",
)

SIGNOFF_PREFIXES = (
    "thanks", "thank you", "thx",
    "regards", "best", "best regards", "kind regards",
    "sincerely", "respectfully", "warm regards",
    "cheers",
)

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(\+?\d[\d\s().-]{7,}\d)")

def looks_like_header_or_forwarded_junk(line: str) -> bool:
    low = line.strip().lower()
    if not low:
        return False

    if any(low.startswith(p) for p in JUNK_LINE_PREFIXES):
        return True
    if any(m in low for m in FORWARDED_MARKERS):
        return True

    # border/table artifact lines
    stripped = line.strip()
    if stripped and set(stripped) <= set("-=_|*~• "):
        return True

    return False

def is_separator_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    # signature separators like "--", "___", "-----", etc.
    if s == "--":
        return True
    if s.startswith("-- "):   # standard signature delimiter
        return True
    if len(s) >= 8 and set(s) <= set("-_="):
        return True
    return False

def is_signoff_line(line: str) -> bool:
    low = line.strip().lower().rstrip(":,;")
    if not low:
        return False
    # e.g. "Thanks," / "Regards," / "Best,"
    return any(low == p or low.startswith(p + " ") for p in SIGNOFF_PREFIXES)

def looks_like_contact_info(line: str) -> bool:
    """Detect lines that are likely signature contact info."""
    low = line.strip().lower()
    if not low:
        return False

    if EMAIL_RE.search(line):
        return True
    if PHONE_RE.search(line):
        return True

    # common contact/address-ish keywords
    if any(k in low for k in ("www.", "http://", "https://", "street", "st.", "ave", "avenue",
                              "suite", "ste", "road", "rd", "blvd", "boulevard",
                              "city", "state", "zip", "phone", "fax")):
        return True

    # “Company | Title | Dept” style lines often in signatures
    if "|" in line and len(line) < 120:
        return True

    return False

def strip_signature_and_footer(lines):
    """
    Remove signature blocks and long boilerplate footers.
    Strategy:
      1) Cut at standard signature delimiter "-- " or separator lines.
      2) If a signoff line is found (Thanks/Regards/etc.), keep it (optional) but cut off after it.
      3) If footer boilerplate markers appear, cut at that point.
    """
    if not lines:
        return lines

    # 1) Cut at explicit signature delimiter/separators
    for i, ln in enumerate(lines):
        if ln.strip() == "--" or ln.strip().startswith("-- "):
            return lines[:i]
        if is_separator_line(ln):
            # Many newsletters use separators; only treat as signature separator if later lines look like contact/footer
            tail = lines[i+1:i+6]
            if any(looks_like_contact_info(t) for t in tail) or any(
                any(m in (t.strip().lower()) for m in FOOTER_MARKERS_CONTAINS) for t in tail
            ):
                return lines[:i]

    # 2) Cut after signoff line (Thanks, / Regards, etc.)
    for i, ln in enumerate(lines):
        if is_signoff_line(ln):
            # If what follows looks like contact info, cut here (don’t read address/email)
            tail = lines[i+1:i+8]
            if any(looks_like_contact_info(t) for t in tail) or any(
                any(m in (t.strip().lower()) for m in FOOTER_MARKERS_CONTAINS) for t in tail
            ):
                return lines[:i+1]  # keep "Thanks," line, drop rest

    # 3) Cut at boilerplate footer markers
    for i, ln in enumerate(lines):
        low = ln.strip().lower()
        if any(m in low for m in FOOTER_MARKERS_CONTAINS):
            return lines[:i]

    return lines

def clean_message_text(text):
    """
    Clean message text:
      - remove quoted-printable artifacts
      - remove forwarded/header junk lines inside body
      - strip signatures + contact info + boilerplate footers
      - collapse whitespace
    """
    if not text:
        return ""

    text = unescape(text)
    text = text.replace("\r", "")
    text = text.replace("=20", " ")
    text = text.replace("=\n", "")  # quoted-printable soft break

    raw_lines = [ln.rstrip() for ln in text.split("\n")]

    kept = []
    for ln in raw_lines:
        s = ln.strip()
        if not s:
            # keep paragraph breaks lightly
            kept.append("")
            continue

        if looks_like_header_or_forwarded_junk(ln):
            continue

        # Remove lots of pipes/tables while keeping words
        ln = re.sub(r"[|]+", " ", ln)
        ln = re.sub(r"\s+", " ", ln).strip()

        kept.append(ln)

    # remove extra blank lines
    normalized = []
    last_blank = False
    for ln in kept:
        blank = (ln.strip() == "")
        if blank and last_blank:
            continue
        normalized.append(ln)
        last_blank = blank

    # strip signature/footer
    normalized = strip_signature_and_footer(normalized)

    # final join: keep some paragraph feel, but not too much
    out = " ".join([ln.strip() for ln in normalized if ln.strip() != ""])
    out = re.sub(r"\s+", " ", out).strip()

    return out[:MAX_SPEAK_CHARS]


# -------------------------
# BODY EXTRACTION (plain/html)
# -------------------------
def extract_message_text(msg):
    plain_parts = []
    html_parts = []

    if msg.is_multipart():
        for part in msg.walk():
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue

            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")

            if ctype == "text/plain":
                plain_parts.append(text)
            else:
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")

            if msg.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    plain = "\n".join(plain_parts).strip()
    html = "\n".join(html_parts).strip()

    # Prefer plain if it has real letters/words
    def is_meaningful(t: str) -> bool:
        if not t:
            return False
        if not re.search(r"[A-Za-z]", t):
            return False
        words = re.findall(r"[A-Za-z]{2,}", t)
        return len(words) >= 5

    if is_meaningful(plain):
        return clean_message_text(plain)

    if html:
        return clean_message_text(html_to_text(html))

    if plain:
        return clean_message_text(plain)

    return ""


# -------------------------
# SPEECH (SAVE WAV + PLAY)
# -------------------------
def speak_piper(text, subject="", sender=""):
    text = (text or "").strip()
    if not text:
        return

    os.makedirs(EMAIL_AUDIO_DIR, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    fname = f"{timestamp}_{safe_filename(subject)}_{safe_filename(sender, 20)}.wav"
    wav_path = os.path.join(EMAIL_AUDIO_DIR, fname)

    print(f"💾 Saving WAV: {wav_path}")

    subprocess.run(
        [sys.executable, "-m", "piper", "--model", PIPER_MODEL, "--output_file", wav_path],
        input=text.encode("utf-8"),
        check=True,
    )

    subprocess.run(["aplay", wav_path], check=True)


# -------------------------
# IMAP
# -------------------------
def connect():
    ctx = ssl.create_default_context()
    srv = IMAPClient(HOST, ssl=True, ssl_context=ctx)
    srv.login(EMAIL_ADDRESS, APP_PASSWORD)
    srv.select_folder("INBOX")
    return srv

def get_highest_uid(server):
    status = server.folder_status("INBOX", ["UIDNEXT"])
    uidnext = int(status.get(b"UIDNEXT") or status.get("UIDNEXT"))
    return max(0, uidnext - 1)

def fetch_message(server, uid):
    raw = server.fetch([uid], ["RFC822"])[uid][b"RFC822"]
    return email.message_from_bytes(raw)


# -------------------------
# MAIN
# -------------------------
def main():
    print("📡 Email audio monitor started")
    print(f"📁 Audio directory: {EMAIL_AUDIO_DIR}")

    server = connect()
    last_seen_uid = get_highest_uid(server)
    print(f"✅ Baseline UID set to {last_seen_uid} (older mail ignored)")

    speak_piper("Email monitor started.", "monitor_started", "system")

    try:
        while True:
            try:
                server.idle()
                responses = server.idle_check(timeout=IDLE_SECONDS)
                server.idle_done()

                if any(r[1] == b"EXISTS" for r in responses):
                    uids = server.search(["UID", f"{last_seen_uid + 1}:*"])
                    for uid in sorted(uids):
                        msg = fetch_message(server, uid)

                        subject = decode_mime_words(msg.get("Subject")) or "No subject"
                        sender = decode_mime_words(msg.get("From")) or "Unknown sender"
                        body = extract_message_text(msg)

                        print("\n📬 New email")
                        print("From:", sender)
                        print("Subject:", subject)
                        print("Message:", (body[:200] + "…") if len(body) > 200 else body)
                        print("-" * 60)

                        if body:
                            speak_piper(body, subject, sender)
                        else:
                            speak_piper(
                                f"New email from {sender}. Subject {subject}. Message body was empty.",
                                subject,
                                sender,
                            )

                        last_seen_uid = max(last_seen_uid, uid)

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
                    last_seen_uid = get_highest_uid(server)
                    print(f"🔄 Reconnected. Baseline UID reset to {last_seen_uid}.")
                except Exception as e2:
                    print("⚠️ Reconnect failed; retrying in 10s...", repr(e2))
                    time.sleep(10)

    except KeyboardInterrupt:
        print("\n🛑 Stopped.")
        try:
            speak_piper("Email monitor stopped.", "monitor_stopped", "system")
        except Exception:
            pass
        try:
            server.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
