#!/usr/bin/env python3
"""
IMAP (IDLE) -> read NEW messages -> extract actual message body
-> cut at known fixed signature block (from one source)
-> announce "New Tattletale message" before reading the body
-> speak via Piper (WAV saved in ./emails_audio next to script)
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
import email.message  # <-- FIX: ensure email.message is imported
from imapclient import IMAPClient
from datetime import datetime
from zoneinfo import ZoneInfo
import quopri

# -------------------------
# CONFIG
# -------------------------
EMAIL_ADDRESS = "toolboxes@tiborprotection.org"
APP_PASSWORD = "Toolboxes123!"
HOST = "mail.tiborprotection.org"

PIPER_MODEL = "/home/rj/piper-voices/en_US-amy-medium/en_US-amy-medium.onnx"

IDLE_SECONDS = 60 * 29
MAX_SPEAK_CHARS = 1200

# Audio folder ALWAYS next to this script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EMAIL_AUDIO_DIR = os.path.join(SCRIPT_DIR, "emails_audio")

# What to announce before the body
ANNOUNCEMENT = "New Tattletale message. Sent at"

# Hard cut at the first occurrence of this signature phrase (normalized)
SIGNATURE_CUTOFF_PHRASES = [
    "6269 Frost Rd, Westerville, OH 43082",
]

# Optional: reduce onnxruntime noise
os.environ.setdefault("ORT_LOG_SEVERITY_LEVEL", "3")

# -------------------------
# HELPERS
# -------------------------
def set_pcm_volume():
    try:
        subprocess.run(
            ["/usr/bin/amixer", "sset", "PCM", "100%", "unmute"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        print("🔊 PCM volume set to 100%")
    except Exception as e:
        print("⚠️ Could not set PCM volume:", e)

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
    s = (s or "").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return (s[:max_len] or "email")

def pacific_timestamp():
    tz = ZoneInfo("America/Los_Angeles")
    now = datetime.now(tz)
    return now.strftime("%B %d, %Y at %I:%M %p Pacific Standard Time")

def extract_embedded_html_from_raw_dump(text: str) -> str:
    """
    Some forwards/relays include a raw email dump inside a text/plain body,
    with quoted-printable HTML like '=3D' sequences. This extracts and decodes
    the embedded HTML for better TTS output.
    """
    if not text:
        return ""

    low = text.lower()
    if "<html" not in low and "<!doctype" not in low:
        return ""

    start = low.find("<!doctype")
    if start == -1:
        start = low.find("<html")
    if start == -1:
        return ""

    html_blob = text[start:]

    if "=3d" in low or "quoted-printable" in low:
        try:
            html_blob = quopri.decodestring(html_blob.encode("utf-8", errors="replace")).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            pass

    return html_blob

# -------------------------
# HTML -> TEXT
# -------------------------
def html_to_text(html: str) -> str:
    html = unescape(html or "")

    # Remove style/script blocks so CSS doesn't dominate the output
    html = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", html)
    html = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)

    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p\s*>", "\n", html)
    html = re.sub(r"(?i)</div\s*>", "\n", html)
    html = re.sub(r"<[^>]+>", " ", html)
    html = html.replace("\r", "")
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()

# -------------------------
# CLEANING / SIGNATURE STRIPPING
# -------------------------
JUNK_LINE_PREFIXES = (
    "return-path:", "delivered-to:", "received:", "dkim-signature:",
    "x-", "mime-version:", "content-type:", "content-transfer-encoding:",
    "message-id:", "in-reply-to:", "references:", "date:", "from:", "to:",
    "subject:", "reply-to:", "cc:", "bcc:",
    "envelope-to:", "delivery-date:",
)

FORWARDED_MARKERS = (
    "----- forwarded message -----",
    "begin forwarded message",
    "end forwarded message",
)

def looks_like_header_or_forwarded_junk(line: str) -> bool:
    low = line.strip().lower()
    if not low:
        return False
    if any(low.startswith(p) for p in JUNK_LINE_PREFIXES):
        return True
    if any(m in low for m in FORWARDED_MARKERS):
        return True
    stripped = line.strip()
    if stripped and set(stripped) <= set("-=_|*~• "):
        return True
    return False

def strip_known_signature(text: str) -> str:
    """Hard cut at the first occurrence of any configured signature phrase."""
    if not text:
        return ""

    phrases = [p.strip() for p in SIGNATURE_CUTOFF_PHRASES if p and p.strip()]
    if not phrases:
        return text

    low = text.lower()
    cut_positions = []
    for p in phrases:
        idx = low.find(p.lower())
        if idx != -1:
            cut_positions.append(idx)

    if not cut_positions:
        return text

    cut_at = min(cut_positions)
    return text[:cut_at].rstrip()

def clean_message_text(text: str) -> str:
    if not text:
        return ""

    text = unescape(text)
    text = text.replace("\r", "")
    text = text.replace("=20", " ")
    text = text.replace("=\n", "")  # quoted-printable soft breaks

    raw_lines = [ln.rstrip() for ln in text.split("\n")]
    kept = []
    for ln in raw_lines:
        s = ln.strip()
        if not s:
            kept.append("")
            continue
        if looks_like_header_or_forwarded_junk(ln):
            continue
        ln = re.sub(r"[|]+", " ", ln)
        ln = re.sub(r"\s+", " ", ln).strip()
        kept.append(ln)

    # Collapse multiple blanks
    normalized = []
    last_blank = False
    for ln in kept:
        blank = (ln.strip() == "")
        if blank and last_blank:
            continue
        normalized.append(ln)
        last_blank = blank

    out = " ".join([ln.strip() for ln in normalized if ln.strip()])
    out = re.sub(r"\s+", " ", out).strip()

    # Hard cut at signature/address
    out = strip_known_signature(out)

    return out[:MAX_SPEAK_CHARS]

def is_meaningful(text: str) -> bool:
    if not text:
        return False
    if not re.search(r"[A-Za-z]", text):
        return False
    words = re.findall(r"[A-Za-z]{2,}", text)
    return len(words) >= 5

def extract_message_text(msg: email.message.Message) -> str:
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

    # Rescue path: sometimes the "plain" part is actually a raw forwarded email dump
    # containing quoted-printable HTML (e.g., lots of "=3D" sequences).
    embedded_html = extract_embedded_html_from_raw_dump(plain)
    if embedded_html:
        rescued = clean_message_text(html_to_text(embedded_html))
        if rescued:
            return rescued

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
def speak_piper(text: str, subject: str = "", sender: str = ""):
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
    print("✂️ Signature cutoff phrases:", SIGNATURE_CUTOFF_PHRASES)
    set_pcm_volume()

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
                        print("Body:", (body[:250] + "…") if len(body) > 250 else body)
                        print("-" * 60)

                        sent_time = pacific_timestamp()

                        if body:
                            speak_text = f"{ANNOUNCEMENT} {sent_time}. {body}"
                        else:
                            speak_text = f"{ANNOUNCEMENT} {sent_time}. Message body was empty."

                        speak_piper(speak_text, subject=subject, sender=sender)

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
        print("\n🛑 Stopping monitor.")
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
