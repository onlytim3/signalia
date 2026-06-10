"""Signalia — notifications. Dispatches to console + Telegram + email."""
import smtplib
import ssl
from email.mime.text import MIMEText

import requests
import config as C


def send_telegram(text):
    if not (C.TELEGRAM_TOKEN and C.TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{C.TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": C.TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
    except Exception as e:
        print("telegram error:", e)


def send_email(subject, text):
    if not (C.SMTP_HOST and C.SMTP_USER and C.EMAIL_TO):
        return
    try:
        msg = MIMEText(text)
        msg["Subject"] = subject
        msg["From"] = C.SMTP_USER
        msg["To"] = C.EMAIL_TO
        ctx = ssl.create_default_context()
        with smtplib.SMTP(C.SMTP_HOST, C.SMTP_PORT) as s:
            s.starttls(context=ctx)
            s.login(C.SMTP_USER, C.SMTP_PASS)
            s.sendmail(C.SMTP_USER, [C.EMAIL_TO], msg.as_string())
    except Exception as e:
        print("email error:", e)


def notify(subject, text):
    print(f"\n=== {subject} ===\n{text}\n")   # also shows in Render logs
    send_telegram(f"{subject}\n\n{text}")
    send_email(subject, text)
