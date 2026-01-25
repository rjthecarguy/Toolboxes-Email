# Talking Gmail Monitor (Raspberry Pi + Piper TTS)

## Current State
- Runs as a systemd service: talking-mail.service
- Uses venv at: /home/rj/toolboxes/venv
- Script: mailV2.py
- Piper models stored locally at: /home/rj/piper-voices (gitignored)
- Uses IMAP IDLE via IMAPClient
- Speaks ONLY new emails after startup (UID baseline logic)

## How to Run Manually
/home/rj/toolboxes/venv/bin/python /home/rj/toolboxes/mailV2.py

## How to Debug Service
sudo systemctl status talking-mail
journalctl -u talking-mail -f

## Known Fixes
- Piper CLI missing → use `sys.executable -m piper`
- Must NOT use system Python, only venv Python
- Audio may need startup delay in service

Project: Talking Email Project (Pi)
Last state:
- mailV2.py working with UID baseline logic
- Piper via python -m piper
- systemd service talking-mail.service
- piper-voices/ gitignored
Next goals:
- Add SMS alerts for urgent subjects
- Add Home Assistant / MQTT integration
