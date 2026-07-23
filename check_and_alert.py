#!/usr/bin/env python3
import csv
import io
import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from urllib.parse import quote
from urllib.request import urlopen, Request
from zoneinfo import ZoneInfo

STATE_FILE = "state.json"


def to_pacific(utc_timestamp):
    dt = datetime.fromisoformat(utc_timestamp.replace("Z", "+00:00"))
    pacific = dt.astimezone(ZoneInfo("America/Los_Angeles"))
    return pacific.strftime("%Y-%m-%d %I:%M %p %Z")


def env(name, required=True, default=None):
    val = os.environ.get(name, default)
    if required and not val:
        print(f"ERROR: missing required environment variable {name}", file=sys.stderr)
        sys.exit(1)
    return val


def fetch_csv(url):
    safe_url = quote(url, safe=":/?&=,%")
    req = Request(safe_url, headers={"User-Agent": "erddap-alert-bot"})
    with urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")
    return list(csv.reader(io.StringIO(text)))


def get_latest_value(erddap_url, value_column):
    """ERDDAP CSV format: row0 = column names, row1 = units, row2+ = data,
    ordered oldest -> newest. Last non-empty row"""
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
        station = col("Station", row)
        threshold_raw = col("Alert threshold", row)
        if not email or not station or not threshold_raw:
            continue
        try:
            threshold = float(threshold_raw)
        except ValueError:
            continue
        subs.append({"email": email, "threshold": threshold, "station": station})
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
    stations = json.loads(env("STATIONS_JSON"))   # {"CE42": "https://...", "MB42": "https://..."}
    value_column = env("VALUE_COLUMN")
    sheet_csv_url = env("SIGNUP_SHEET_CSV_URL")
    label = env("VALUE_LABEL", required=False, default=value_column)

    smtp_host = env("SMTP_HOST")
    smtp_port = int(env("SMTP_PORT", required=False, default="587"))
    smtp_user = env("SMTP_USER")
    smtp_pass = env("SMTP_PASS")
    force_alert = env("FORCE_ALERT", required=False, default="false").lower() == "true"
    if force_alert:
        print("FORCE_ALERT is on: sending regardless of previous alert state (testing mode)")

    subscribers = get_subscribers(sheet_csv_url)
    print(f"Loaded {len(subscribers)} subscriber(s)")

    by_station = {}
    for sub in subscribers:
        by_station.setdefault(sub["station"], []).append(sub)

    state = load_state()
    alerted = state["alerted"]

    for station_name, erddap_url in stations.items():
        station_subs = by_station.get(station_name, [])
        if not station_subs:
            continue   # nobody signed up for this station -- skip the fetch

        try:
            value, timestamp = get_latest_value(erddap_url, value_column)
        except Exception as e:
            print(f"WARNING: failed to fetch {station_name}: {e}")
            continue
        print(f"[{station_name}] Latest {label}: {value} at {timestamp}")

        for sub in station_subs:
            key = f"{station_name}:{sub['email']}"
            below = value < sub["threshold"]
            was_alerted = key in alerted

            should_send_drop = below and (force_alert or not was_alerted)
            should_send_recovery = (not below) and (force_alert or was_alerted)

            if should_send_drop:
                body = (
                    f"Alert: {station_name} {label} is {value} (below your "
                    f"threshold of {sub['threshold']}) as of {to_pacific(timestamp)}."
                )
                send_email(sub["email"], f"{station_name} {label} alert: dropped below threshold",
                           body, smtp_host, smtp_port, smtp_user, smtp_pass)
                alerted[key] = timestamp
            elif should_send_recovery:
                body = (
                    f"Update: {station_name} {label} is back up to {value} (above "
                    f"your threshold of {sub['threshold']}) as of {to_pacific(timestamp)}."
                )
                send_email(sub["email"], f"{station_name} {label} alert: back above threshold",
                           body, smtp_host, smtp_port, smtp_user, smtp_pass)
                if was_alerted:
                    del alerted[key]

    save_state(state)


if __name__ == "__main__":
    main()
