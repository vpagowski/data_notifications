#!/usr/bin/env python3

import csv
import io
import json
import os
import smtplib
import sys
from email.mime.text import MIMEText
from urllib.parse import quote
from urllib.request import urlopen, Request

STATE_FILE = "state.json"


def env(name, required=True, default=None):
    val = os.environ.get(name, default)
    if required and not val:
        print(f"ERROR: missing required environment variable {name}", file=sys.stderr)
        sys.exit(1)
    return val


def fetch_csv(url):
    #Encoder shenanigans
    safe_url = quote(url, safe=":/?&=,%")
    req = Request(safe_url, headers={"User-Agent": "erddap-alert-bot"})
    with urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")
    return list(csv.reader(io.StringIO(text)))


def get_latest_value(erddap_url, value_column):
    """ERDDAP CSV format: row0 = column names, row1 = units, row2+ = data,
    ordered oldest -> newest. We take the last non-empty row."""
    rows = fetch_csv(erddap_url)
    if len(rows) < 3:
        raise RuntimeError(f"ERDDAP returned no data rows. Raw response head: {rows[:3]}")

    header = rows[0]
    if value_column not in header:
        raise RuntimeError(
            f"Column '{value_column}' not found in ERDDAP response. "
            f"Available columns: {header}"
        )
    col_idx = header.index(value_column)
    time_idx = header.index("time") if "time" in header else 0

    data_rows = [r for r in rows[2:] if len(r) > col_idx and r[col_idx].strip() != ""]
    if not data_rows:
        raise RuntimeError("No rows with a non-empty value found.")

    last = data_rows[-1]
    return float(last[col_idx]), last[time_idx]


def get_subscribers(sheet_csv_url):
    rows = fetch_csv(sheet_csv_url)
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]

    def col(name, row):
        try:
            return row[header.index(name)].strip()
        except (ValueError, IndexError):
            return ""

    subs = []
    for row in rows[1:]:
        if not row:
            continue
        email = col("Email Address", row)
        threshold_raw = col("Alert threshold", row)
        if not email or not threshold_raw:
            continue
        try:
            threshold = float(threshold_raw)
        except ValueError:
            continue
        subs.append({"email": email, "threshold": threshold})
    return subs


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"alerted": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_email(to_email, subject, body, smtp_host, smtp_port, smtp_user, smtp_pass):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_email
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
    print(f"Emailed {to_email}")


def main():
    erddap_url = env("ERDDAP_URL")
    value_column = env("VALUE_COLUMN")
    sheet_csv_url = env("SIGNUP_SHEET_CSV_URL")
    label = env("VALUE_LABEL", required=False, default=value_column)

    smtp_host = env("SMTP_HOST")
    smtp_port = int(env("SMTP_PORT", required=False, default="587"))
    smtp_user = env("SMTP_USER")
    smtp_pass = env("SMTP_PASS")

    value, timestamp = get_latest_value(erddap_url, value_column)
    print(f"Latest {label}: {value} at {timestamp}")

    subscribers = get_subscribers(sheet_csv_url)
    print(f"Loaded {len(subscribers)} subscriber(s)")

    state = load_state()
    alerted = state["alerted"]

    for sub in subscribers:
        key = sub["email"]
        below = value < sub["threshold"]

        if below and key not in alerted:
            # Just dropped below threshold -- send one alert, then go quiet.
            body = (
                f"Alert: {label} is {value} (below your threshold of "
                f"{sub['threshold']}) as of {timestamp} UTC."
            )
            send_email(key, f"{label} alert: dropped below threshold", body, smtp_host, smtp_port, smtp_user, smtp_pass)
            alerted[key] = timestamp
        elif not below and key in alerted:
            # Just recovered above threshold -- send one recovery notice,
            # then re-arm so the next dip triggers a fresh alert.
            body = (
                f"Update: {label} is back up to {value} (above your threshold of "
                f"{sub['threshold']}) as of {timestamp} UTC."
            )
            send_email(key, f"{label} alert: back above threshold", body, smtp_host, smtp_port, smtp_user, smtp_pass)
            del alerted[key]
        # else: no state change since the last check -- stay quiet.

    save_state(state)


if __name__ == "__main__":
    main()
