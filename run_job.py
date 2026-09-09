"""
MU-Automation - the unattended job.

Runs on a schedule with nobody watching. Every run it:

    1. looks in  inbox/     for files it has not seen
    2. VALIDATES each one - and refuses rather than guessing
    3. runs the Session 3 sign-off analysis on the good ones
    4. appends a row per file to  reports/insights_latest.csv
    5. moves the file to  processed/  (good) or  quarantine/  (bad)

There is no database and no state file. `processed/` IS the state:
a file that has moved has been done. You can open the folder and see
exactly what the robot has and has not touched.

Auth, in order of preference:
    GDRIVE_SA_KEY   env var holding the whole service-account JSON  (CI)
    --key PATH      a local key file                               (your Mac)

Exit code 0 = healthy (including "nothing to do").
Exit code 1 = something needs a human. CI turns that into an email.
"""

import csv
import io
import json
import os
import sys
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"

ROOT_ID = os.environ.get("DRIVE_ROOT_ID", "")
THRESHOLD = float(os.environ.get("THRESHOLD", "100000"))
LEDGER_NAME = os.environ.get("LEDGER_NAME", "insights_latest.csv")

# The columns this job needs. A file missing any of them is not something
# we can analyse, and pretending otherwise is how a wrong number ships.
REQUIRED_COLUMNS = {"order_id", "order_value", "status"}

LEDGER_HEADER = [
    "run_utc", "source_file", "rows_read", "orders_above_threshold",
    "total_value_above", "skipped_blank", "skipped_unreadable", "outcome",
]


def log(msg):
    """One line per event, timestamped. This is the only thing a human reads."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{stamp}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def connect():
    raw = os.environ.get("GDRIVE_SA_KEY")
    if raw:
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        if "--key" not in sys.argv:
            sys.exit("no credentials: set GDRIVE_SA_KEY or pass --key <file.json>")
        path = sys.argv[sys.argv.index("--key") + 1]
        creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    log(f"authenticated as {creds.service_account_email}")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------

def list_children(drive, parent_id, folders_only=False):
    q = f"'{parent_id}' in parents and trashed = false"
    if folders_only:
        q += f" and mimeType = '{FOLDER_MIME}'"
    out, token = [], None
    while True:
        resp = drive.files().list(
            q=q, pageToken=token, orderBy="createdTime",
            fields="nextPageToken, files(id, name, mimeType, size, createdTime)",
        ).execute()
        out += resp.get("files", [])
        token = resp.get("nextPageToken")
        if not token:
            return out


def download_text(drive, file_id):
    return drive.files().get_media(fileId=file_id).execute().decode("utf-8-sig")


def move(drive, file_id, from_id, to_id):
    drive.files().update(fileId=file_id, addParents=to_id,
                         removeParents=from_id, fields="id").execute()


def overwrite(drive, file_id, text):
    """The service account cannot CREATE a file, but it can replace the
    contents of one you own. That is why the ledger is pre-created."""
    drive.files().update(
        fileId=file_id,
        media_body=MediaInMemoryUpload(text.encode("utf-8"), mimetype="text/csv"),
        fields="id, size",
    ).execute()


# ---------------------------------------------------------------------------
# The analysis - the same arithmetic as Session 3's sign-off report
# ---------------------------------------------------------------------------

def analyse(text, source_name):
    """Returns a ledger row. Raises ValueError if the file is not analysable."""
    reader = csv.DictReader(io.StringIO(text))

    present = set(reader.fieldnames or [])
    missing = REQUIRED_COLUMNS - present
    if missing:
        raise ValueError(
            f"schema drift - missing column(s): {', '.join(sorted(missing))}. "
            f"Found: {', '.join(sorted(present)) or '(no header)'}"
        )

    rows_read = above = 0
    total_above = 0.0
    skipped_blank = skipped_unreadable = 0

    for row in reader:
        rows_read += 1
        raw = (row.get("order_value") or "").strip()

        # A blank is not a zero and not a small order. Count it, do not judge it.
        if raw == "":
            skipped_blank += 1
            continue
        try:
            value = float(raw)
        except ValueError:
            # "N/A", "TBD", "REVISED" - also not zero.
            skipped_unreadable += 1
            continue

        if value > THRESHOLD:
            above += 1
            total_above += value

    if rows_read == 0:
        raise ValueError("file has a valid header but no data rows")

    return {
        "run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "source_file": source_name,
        "rows_read": rows_read,
        "orders_above_threshold": above,
        "total_value_above": f"{total_above:.2f}",
        "skipped_blank": skipped_blank,
        "skipped_unreadable": skipped_unreadable,
        "outcome": "OK",
    }


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def read_ledger(drive, file_id):
    """Existing rows, or an empty list if the file is a placeholder."""
    try:
        text = download_text(drive, file_id)
    except HttpError:
        return []
    rows = list(csv.DictReader(io.StringIO(text)))
    return [r for r in rows if r.get("run_utc")]


def write_ledger(drive, file_id, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LEDGER_HEADER, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    overwrite(drive, file_id, buf.getvalue())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not ROOT_ID:
        sys.exit("DRIVE_ROOT_ID is not set")

    drive = connect()

    folders = {f["name"]: f["id"] for f in list_children(drive, ROOT_ID, folders_only=True)}
    for needed in ("inbox", "processed", "reports"):
        if needed not in folders:
            sys.exit(f"missing required folder: {needed}/")
    has_quarantine = "quarantine" in folders

    # The ledger must already exist - the service account cannot create it.
    ledger = next((f for f in list_children(drive, folders["reports"])
                   if f["name"] == LEDGER_NAME), None)
    if ledger is None:
        sys.exit(f"missing {LEDGER_NAME} in reports/ - create it once by hand")

    incoming = [f for f in list_children(drive, folders["inbox"])
                if f["mimeType"] != FOLDER_MIME]

    # The boring case, and it must be logged. Otherwise you cannot tell
    # "nothing to do" from "dead for three weeks".
    if not incoming:
        log("no new files in inbox/ - skipped")
        return 0

    log(f"found {len(incoming)} new file(s): {', '.join(f['name'] for f in incoming)}")
    log(f"threshold: Rs {THRESHOLD:,.0f}")

    new_rows, failures = [], 0

    for f in incoming:
        name = f["name"]
        try:
            text = download_text(drive, f["id"])
            row = analyse(text, name)
        except ValueError as e:
            failures += 1
            log(f"REFUSED  {name}: {e}")
            new_rows.append({
                "run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
                "source_file": name, "rows_read": "", "orders_above_threshold": "",
                "total_value_above": "", "skipped_blank": "", "skipped_unreadable": "",
                "outcome": f"REFUSED - {e}",
            })
            if has_quarantine:
                move(drive, f["id"], folders["inbox"], folders["quarantine"])
                log(f"         moved to quarantine/")
            else:
                log(f"         left in inbox/ (no quarantine folder)")
            continue

        log(f"OK       {name}: {row['rows_read']} rows, "
            f"{row['orders_above_threshold']} above threshold, "
            f"Rs {float(row['total_value_above']):,.2f}, "
            f"{row['skipped_blank']} blank, {row['skipped_unreadable']} unreadable")
        new_rows.append(row)
        move(drive, f["id"], folders["inbox"], folders["processed"])
        log(f"         moved to processed/")

    existing = read_ledger(drive, ledger["id"])
    write_ledger(drive, ledger["id"], existing + new_rows)
    log(f"ledger updated: {len(existing)} existing + {len(new_rows)} new "
        f"= {len(existing) + len(new_rows)} rows -> reports/{LEDGER_NAME}")

    if failures:
        log(f"FAILED: {failures} file(s) refused - a human needs to look")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
