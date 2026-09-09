"""
================================================================================
 MU-AUTOMATION  ·  the unattended job
================================================================================

THE ONE QUESTION THIS FILE ANSWERS
    "Has a new sales file arrived in my Drive folder, and if so, is it worth
     anything to finance?"

...asked by nobody, at 2am, with no human in the room.

WHAT MAKES IT DIFFERENT FROM THE SESSION 3 SCRIPT
    Session 3's script was run BY A PERSON who was looking at the output. If it
    printed something odd, that person noticed. This one prints into a log
    nobody reads. So every place where the Session 3 script could rely on a
    human noticing, this one has to handle explicitly:

        a column got renamed        -> REFUSE and shout, do not guess
        a value is blank            -> count it separately, never treat as zero
        a value is unreadable       -> same, and say so
        nothing to do today         -> SAY SO, so silence never looks healthy
        something went wrong        -> exit non-zero, so CI emails a human

THE STATE PROBLEM, AND HOW IT IS SOLVED
    This runs on a rented machine that is DESTROYED after every run. There is
    no disk to remember anything on. So how does it know which files it has
    already done?

        It moves them.

    A file in inbox/ has not been processed. A file in processed/ has. The
    folder structure IS the database. No table, no state file, no timestamp to
    keep in sync - and a human can open the folder and see exactly what the
    robot has and has not touched.

AUTHENTICATION, IN ORDER OF PREFERENCE
    GDRIVE_SA_KEY   an environment variable holding the whole service-account
                    JSON. This is how GitHub Actions supplies it.
    --key PATH      a local key file. This is how you run it on your Mac.

EXIT CODES  (the only thing an automated system can "say")
    0   healthy - and that INCLUDES "there was nothing to do"
    1   a human needs to look at this
================================================================================
"""

# --- Standard library. Nothing to install; these ship with Python. -----------
import csv                                  # read/write comma-separated files
import io                                   # treat a string in memory as a file
import json                                 # parse the service-account key
import os                                   # read environment variables
import sys                                  # command-line args, and exit codes
from datetime import datetime, timezone     # timestamps for the log and ledger

# --- Third-party: Google's own client libraries (see requirements.txt) -------
# service_account   turns a key file into a set of credentials
# build             constructs a client object for a named Google API
# HttpError         the exception Google raises when it refuses a request
# MediaInMemoryUpload  lets us upload bytes we built in memory, with no temp file
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload


# =============================================================================
#  CONFIGURATION
# =============================================================================

# What we are asking Google's permission to do. "drive" is the broad scope -
# read and write any file the caller can reach. That sounds alarming and is not,
# because a service account can reach NOTHING by default. Its access is exactly
# the folders a human has shared with it. Scope is wide; reach is one folder.
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Drive has no separate concept of "folder" - a folder is a file with this
# special type. So this constant is how we tell folders from real files.
FOLDER_MIME = "application/vnd.google-apps.folder"

# --- Settings that come from the environment, so the code never changes ------
# This is the same idea as Session 3's signoff_report.py: the things that vary
# move OUT of the code and INTO the call. Here they arrive as environment
# variables because that is how a CI system passes settings in.

# Which Drive folder to watch. NOT a secret - a folder id grants nothing on its
# own; access is controlled by who the folder is shared with.
ROOT_ID = os.environ.get("DRIVE_ROOT_ID", "")

# The credit policy: orders above this need finance sign-off. Named, not buried,
# so a policy change is a one-line change. Defaults to Rs 1,00,000.
THRESHOLD = float(os.environ.get("THRESHOLD", "100000"))

# The report file. It must ALREADY EXIST in reports/ - see overwrite() below for
# why the robot cannot create it.
LEDGER_NAME = os.environ.get("LEDGER_NAME", "insights_latest.csv")

# The columns this analysis cannot work without. A file missing any of them is
# not a file we can analyse - and pretending otherwise is exactly how a
# confident wrong number reaches a slide deck. This set is what turns "schema
# drift" from an invisible disaster into a loud refusal.
REQUIRED_COLUMNS = {"order_id", "order_value", "status"}

# The shape of the report. Every run appends one row per file processed.
# Note what is in here: not just the answer, but how many rows were SKIPPED and
# why. A report that states only its total is a report you cannot audit.
LEDGER_HEADER = [
    "run_utc",                  # when this row was written (UTC)
    "source_file",              # which file produced it
    "rows_read",                # how many data rows were examined
    "orders_above_threshold",   # the answer finance asked for
    "total_value_above",        # and what those orders are worth
    "skipped_blank",            # rows with no order_value at all
    "skipped_unreadable",       # rows with text where a number should be
    "outcome",                  # OK, or REFUSED with the reason
]


def log(msg):
    """
    Print one timestamped line.

    This is the ONLY channel through which this program can tell a human
    anything. So every meaningful event goes through here - including the
    boring "nothing happened" case. A log that is silent when idle is
    indistinguishable from a job that died three weeks ago.

    flush=True forces the line out immediately rather than letting Python
    buffer it. Without it, a crash can lose the last few lines - which are
    exactly the ones you need in order to understand the crash.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{stamp}] {msg}", flush=True)


# =============================================================================
#  AUTHENTICATION  -  how the robot proves who it is
# =============================================================================

def connect():
    """
    Turn a key file into an authenticated Drive client.

    WHAT IS ACTUALLY HAPPENING HERE, because it is not obvious:

    The key file contains an RSA PRIVATE KEY. The library does not send that
    key to Google. Instead it builds a small message ("I am mu-drive-bot, it
    is now 16:43, I want the drive scope"), SIGNS it with the private key, and
    posts the signature to Google. Google holds the matching public key, checks
    the signature, and hands back an ACCESS TOKEN valid for about an hour.

    So it behaves like a signature nobody can forge, rather than a password
    handed over. The secret itself never crosses the network.

    All of that is one line - from_service_account_file - which is why this is
    worth a comment rather than being obvious from the code.
    """
    # Preferred path: the whole JSON arrived as an environment variable.
    # This is how GitHub Actions injects the repository secret.
    raw = os.environ.get("GDRIVE_SA_KEY")
    if raw:
        info = json.loads(raw)                      # text -> dictionary
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES)
    else:
        # Fallback for running on your own machine: --key /path/to/key.json
        if "--key" not in sys.argv:
            sys.exit("no credentials: set GDRIVE_SA_KEY or pass --key <file.json>")
        # sys.argv is the list of words typed on the command line. Find "--key"
        # and take the word after it.
        path = sys.argv[sys.argv.index("--key") + 1]
        creds = service_account.Credentials.from_service_account_file(
            path, scopes=SCOPES)

    # Log WHICH identity we are. When permissions mysteriously fail, this line
    # is the first thing you want to see - usually the answer is that this
    # email was never added to the folder.
    log(f"authenticated as {creds.service_account_email}")

    # build() returns a client whose methods mirror the Drive API.
    # cache_discovery=False stops the library trying to write a cache file to
    # disk - pointless on a machine that is about to be destroyed, and it emits
    # warnings when the disk is read-only.
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# =============================================================================
#  DRIVE HELPERS  -  four small operations, each wrapping one API call
# =============================================================================

def list_children(drive, parent_id, folders_only=False):
    """
    Everything sitting directly inside one folder.

    Two details that matter:

    trashed = false     Drive does not delete things; it moves them to a bin
                        where they still show up in queries. Without this, a
                        file you deleted last week comes back as "new".

    PAGINATION           Drive returns results in pages. If you read only the
                        first page you get a partial answer with no error and
                        no warning - the same silent-truncation failure as
                        Excel's row limit in Session 1. The while-loop below
                        keeps asking until Google stops handing back a
                        nextPageToken.
    """
    # The query language is Drive-specific but readable.
    q = f"'{parent_id}' in parents and trashed = false"
    if folders_only:
        q += f" and mimeType = '{FOLDER_MIME}'"

    out, token = [], None
    while True:
        resp = drive.files().list(
            q=q,
            pageToken=token,           # None on the first call
            orderBy="createdTime",     # oldest first - process in arrival order
            # `fields` asks for only what we use. Ask for everything and the
            # response is many times larger for no benefit.
            fields="nextPageToken, files(id, name, mimeType, size, createdTime)",
        ).execute()

        out += resp.get("files", [])
        token = resp.get("nextPageToken")
        if not token:                  # no more pages - we have everything
            return out


def download_text(drive, file_id):
    """
    Fetch a file's contents as text.

    get_media returns raw bytes; a CSV is text, so we decode it.

    "utf-8-sig" rather than plain "utf-8" is deliberate. Files exported from
    Excel very often begin with an invisible three-byte marker (a BOM). Decoded
    as plain utf-8, that marker becomes part of the FIRST COLUMN NAME - so
    "order_id" silently becomes "﻿order_id", every lookup for "order_id"
    fails, and the schema check rejects a perfectly good file. The "-sig"
    variant strips it. This is a real, common, extremely confusing bug.
    """
    return drive.files().get_media(fileId=file_id).execute().decode("utf-8-sig")


def move(drive, file_id, from_id, to_id):
    """
    Move a file between folders - which is how this program remembers.

    Drive has no "move" operation. A file's location is just a list of parent
    folders, so a move is: add the new parent, remove the old one. The file
    itself is untouched - same id, same contents, same owner.

    That last point is why this works at all. The file belongs to YOU, and
    re-parenting it does not create anything new, so no storage is consumed
    and the service account's zero storage quota never comes into it.
    """
    drive.files().update(
        fileId=file_id,
        addParents=to_id,
        removeParents=from_id,
        fields="id",
    ).execute()


def overwrite(drive, file_id, text):
    """
    Replace the contents of an existing file.

    THIS IS THE CONSTRAINT THAT SHAPED THE WHOLE REPORT DESIGN.

    A service account has no Google Drive of its own and no storage quota. So
    it CANNOT CREATE a file - there would be nobody to own the bytes. Trying
    returns: "Service Accounts do not have storage quota."

    But it CAN overwrite a file that you own, because then the bytes are billed
    to your quota, not its.

    Hence the design: you create insights_latest.csv once, by hand. Forever
    after, the robot reads it, appends to it, and writes the whole thing back.
    That is also why the report is a single running LEDGER rather than one new
    file per run - the robot is physically incapable of making a new file.
    """
    drive.files().update(
        fileId=file_id,
        media_body=MediaInMemoryUpload(text.encode("utf-8"), mimetype="text/csv"),
        fields="id, size",
    ).execute()


# =============================================================================
#  THE ANALYSIS  -  the same arithmetic as Session 3, with teeth
# =============================================================================

def analyse(text, source_name):
    """
    Run the finance sign-off count over one file's contents.

    Returns a ledger row on success.
    Raises ValueError if the file is not analysable at all.

    That distinction is the important design decision in this whole file.
    "I analysed it and the answer is zero" and "I could not analyse it" look
    identical in a total, and they mean opposite things. So the second one is
    an exception, not a number.
    """
    # DictReader reads the first line as column NAMES and hands back each row
    # as a labelled record - row["order_value"] rather than row[10]. Worth it:
    # it survives somebody reordering the columns.
    # io.StringIO lets DictReader read from a string as though it were a file.
    reader = csv.DictReader(io.StringIO(text))

    # ---- GATE 1: does this file even have the columns we need? --------------
    # `reader.fieldnames` is the header row. `or []` guards against a
    # completely empty file, where fieldnames is None.
    present = set(reader.fieldnames or [])
    missing = REQUIRED_COLUMNS - present        # set subtraction = what is absent

    if missing:
        # Refuse, and say precisely what is wrong and what was there instead.
        # "Something went wrong" costs a human twenty minutes; this costs five
        # seconds. The message is written for the person reading the log at
        # 9am, who was not thinking about this file at all.
        raise ValueError(
            f"schema drift - missing column(s): {', '.join(sorted(missing))}. "
            f"Found: {', '.join(sorted(present)) or '(no header)'}"
        )

    # ---- The counters -------------------------------------------------------
    rows_read = above = 0          # rows examined; rows over the threshold
    total_above = 0.0              # what those rows add up to, in rupees
    skipped_blank = 0              # order_value was empty
    skipped_unreadable = 0         # order_value held text, e.g. "N/A", "REVISED"

    for row in reader:
        rows_read += 1

        # Everything in a CSV is TEXT. .strip() removes stray spaces, which are
        # invisible and will otherwise make "  " look like a real value.
        # `or ""` guards against the column being absent on this particular row.
        raw = (row.get("order_value") or "").strip()

        # ---- A blank is not a zero and not a small order --------------------
        # It usually means billing has not run yet. We cannot judge it, so we
        # count it in its own bucket and move on. Silently treating it as zero
        # would quietly shrink the answer and nothing would say so.
        if raw == "":
            skipped_blank += 1
            continue                # skip the rest of this row

        # ---- Text where a number should be is ALSO not a zero ---------------
        # float() raises ValueError on "N/A" or "REVISED". We catch it, count
        # it separately from blanks - the two have different causes and
        # different fixes - and move on.
        try:
            value = float(raw)
        except ValueError:
            skipped_unreadable += 1
            continue

        # ---- The filter itself. One comparison. -----------------------------
        # This single line is what finance actually asked for. Everything else
        # in this function exists to make sure this line is asked a fair
        # question.
        if value > THRESHOLD:
            above += 1
            total_above += value

    # ---- GATE 2: a valid header with no data is not a successful run --------
    # This catches a truncated upload or an empty export. Without it, the job
    # would cheerfully record "0 orders above threshold" - a real-looking
    # number derived from nothing.
    if rows_read == 0:
        raise ValueError("file has a valid header but no data rows")

    # The ledger row. Note that the skipped counts travel WITH the answer -
    # they are part of the result, not a footnote.
    return {
        "run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "source_file": source_name,
        "rows_read": rows_read,
        "orders_above_threshold": above,
        "total_value_above": f"{total_above:.2f}",   # 2 decimals, as rupees
        "skipped_blank": skipped_blank,
        "skipped_unreadable": skipped_unreadable,
        "outcome": "OK",
    }


# =============================================================================
#  THE LEDGER  -  read, append, write back
# =============================================================================

def read_ledger(drive, file_id):
    """
    The rows already in the report.

    We must read before writing because overwrite() replaces the ENTIRE file.
    Skip this step and every run wipes the history it should be adding to.

    The filter on the last line handles the very first run, when the file is
    still the placeholder you uploaded by hand and contains no real rows.
    """
    try:
        text = download_text(drive, file_id)
    except HttpError:
        # Cannot read it - treat as empty rather than crashing. Worst case we
        # lose history; we never lose the current run's result.
        return []

    rows = list(csv.DictReader(io.StringIO(text)))
    # Keep only rows that look like ours. A placeholder line, or a blank
    # trailing row, has no run_utc.
    return [r for r in rows if r.get("run_utc")]


def write_ledger(drive, file_id, rows):
    """
    Write the whole ledger - old rows plus new - back to Drive.

    Built in memory first (io.StringIO) so nothing touches the disk of a
    machine that is about to be destroyed.

    extrasaction="ignore" means an unexpected extra key in a row is dropped
    rather than raising. Deliberate: a malformed row should not be able to
    take down the whole report.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LEDGER_HEADER, extrasaction="ignore")
    writer.writeheader()          # the column names
    writer.writerows(rows)        # every row, oldest first
    overwrite(drive, file_id, buf.getvalue())


# =============================================================================
#  MAIN  -  the run, start to finish
# =============================================================================

def main():
    # ---- Refuse to start half-configured -----------------------------------
    # Fail loudly at second zero rather than doing something surprising at
    # second thirty.
    if not ROOT_ID:
        sys.exit("DRIVE_ROOT_ID is not set")

    drive = connect()

    # ---- Find the folders by NAME, not by hard-coded id --------------------
    # So you can rearrange your Drive without editing this file. The cost is
    # one extra API call; the benefit is that the code is about the shape of
    # the workflow, not about your particular folder ids.
    folders = {f["name"]: f["id"]
               for f in list_children(drive, ROOT_ID, folders_only=True)}

    for needed in ("inbox", "processed", "reports"):
        if needed not in folders:
            sys.exit(f"missing required folder: {needed}/")

    # quarantine/ is optional. If it is absent we still run, and bad files stay
    # in inbox/ instead. Degrade, do not crash - but SAY which mode you are in
    # (see the log line further down).
    has_quarantine = "quarantine" in folders

    # ---- The ledger must already exist -------------------------------------
    # Because the service account cannot create files. This check turns an
    # obscure 403 quota error deep in the run into a clear instruction now.
    ledger = next((f for f in list_children(drive, folders["reports"])
                   if f["name"] == LEDGER_NAME), None)
    if ledger is None:
        sys.exit(f"missing {LEDGER_NAME} in reports/ - create it once by hand")

    # ---- What is waiting for us? -------------------------------------------
    # Exclude folders, in case somebody drops a folder into inbox/.
    incoming = [f for f in list_children(drive, folders["inbox"])
                if f["mimeType"] != FOLDER_MIME]

    # ---- The boring case, and it MUST be logged ----------------------------
    # 95 runs out of every 96 end here. This single line is what lets you tell
    # "there was nothing to do" apart from "this has been dead since Tuesday".
    # Return 0: having nothing to do is a healthy outcome, not a failure.
    if not incoming:
        log("no new files in inbox/ - skipped")
        return 0

    log(f"found {len(incoming)} new file(s): {', '.join(f['name'] for f in incoming)}")
    # Log the threshold too. The same file gives a different answer at a
    # different threshold, so the setting is part of the result.
    log(f"threshold: Rs {THRESHOLD:,.0f}")

    new_rows = []      # ledger rows produced by this run
    failures = 0       # how many files we refused

    for f in incoming:
        name = f["name"]

        try:
            text = download_text(drive, f["id"])
            row = analyse(text, name)

        except ValueError as e:
            # ---- THE REFUSAL PATH ------------------------------------------
            # Reached only when the file cannot be analysed at all. Four things
            # happen, and all four matter:
            failures += 1

            #   1. say so in the log, with the reason
            log(f"REFUSED  {name}: {e}")

            #   2. record it in the REPORT, not just the log. Somebody reading
            #      the ledger must be able to see that a file arrived and was
            #      rejected. A gap in a report is invisible; a REFUSED row is not.
            new_rows.append({
                "run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
                "source_file": name,
                "rows_read": "", "orders_above_threshold": "",
                "total_value_above": "", "skipped_blank": "",
                "skipped_unreadable": "",
                "outcome": f"REFUSED - {e}",
            })

            #   3. get it out of inbox/, so the next run does not fail on the
            #      same file forever - but into quarantine/, NOT processed/,
            #      because it was not processed.
            if has_quarantine:
                move(drive, f["id"], folders["inbox"], folders["quarantine"])
                log("         moved to quarantine/")
            else:
                log("         left in inbox/ (no quarantine folder)")

            #   4. (further down) exit non-zero, which is what actually
            #      summons a human.
            continue

        # ---- THE SUCCESS PATH ----------------------------------------------
        # Log the answer AND the disclosure counts on the same line. This is
        # Session 3's habit - a filter reports what matched, never what it
        # dropped, so you announce the drops yourself.
        log(f"OK       {name}: {row['rows_read']} rows, "
            f"{row['orders_above_threshold']} above threshold, "
            f"Rs {float(row['total_value_above']):,.2f}, "
            f"{row['skipped_blank']} blank, {row['skipped_unreadable']} unreadable")

        new_rows.append(row)

        # Only NOW move it. If anything above had thrown, the file would still
        # be sitting in inbox/ and the next run would retry it. Moving last is
        # what makes a crashed run safe to re-run.
        move(drive, f["id"], folders["inbox"], folders["processed"])
        log("         moved to processed/")

    # ---- One write at the end, not one per file ----------------------------
    # Fewer API calls, and the ledger is never left half-updated.
    existing = read_ledger(drive, ledger["id"])
    write_ledger(drive, ledger["id"], existing + new_rows)
    log(f"ledger updated: {len(existing)} existing + {len(new_rows)} new "
        f"= {len(existing) + len(new_rows)} rows -> reports/{LEDGER_NAME}")

    # ---- Tell the outside world whether a human is needed ------------------
    # This return value is the whole notification system. GitHub Actions marks
    # a non-zero exit as a failed run and emails you. We did not build
    # alerting; we just had to be honest about the exit code.
    if failures:
        log(f"FAILED: {failures} file(s) refused - a human needs to look")
        return 1
    return 0


# Standard Python entry point: run main() only when this file is executed
# directly, not when it is imported. That is what let me import analyse() and
# test it against five broken files without touching Drive at all.
if __name__ == "__main__":
    sys.exit(main())
