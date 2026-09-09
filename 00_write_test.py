"""
STEP 9 - the go/no-go gate.

Proves four things against the real Drive folder, in order:
  1. the service account can SEE the shared folder
  2. it can LIST what is in inbox/
  3. it can CREATE a file in reports/   <- the one that may fail on quota
  4. it can MOVE a file between folders

Run:
    ./.venv/bin/python 00_write_test.py /path/to/key.json <MU-Automation folder id>
"""
import sys
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/drive"]

if len(sys.argv) < 3:
    sys.exit("usage: python 00_write_test.py <key.json> <root_folder_id>")

key_path, root_id = sys.argv[1], sys.argv[2]

creds = service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)
drive = build("drive", "v3", credentials=creds)
print(f"authenticated as: {creds.service_account_email}\n")


def children(parent_id):
    """Everything directly inside one folder."""
    out, token = [], None
    while True:
        resp = drive.files().list(
            q=f"'{parent_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageToken=token,
        ).execute()
        out += resp.get("files", [])
        token = resp.get("nextPageToken")
        if not token:
            return out


# --- 1. can it see the shared root at all? ------------------------------------
root = drive.files().get(fileId=root_id, fields="id, name").execute()
print(f"[1/4] OK   can see the shared folder: {root['name']}")

# --- 2. find the three subfolders --------------------------------------------
folders = {f["name"]: f["id"] for f in children(root_id)
           if f["mimeType"] == "application/vnd.google-apps.folder"}
missing = [n for n in ("inbox", "processed", "reports") if n not in folders]
if missing:
    sys.exit(f"[2/4] FAIL missing subfolder(s): {', '.join(missing)}")
print(f"[2/4] OK   found inbox / processed / reports")

inbox = children(folders["inbox"])
print(f"           inbox currently holds {len(inbox)} file(s): "
      f"{', '.join(f['name'] for f in inbox) or '(empty)'}")

# --- 3. THE RISKY ONE: create a file in reports/ ------------------------------
try:
    made = drive.files().create(
        body={"name": "_write_test.txt", "parents": [folders["reports"]]},
        media_body=MediaInMemoryUpload(b"write test\n", mimetype="text/plain"),
        fields="id, name",
    ).execute()
    print(f"[3/4] OK   created {made['name']} in reports/  <- no quota problem")
except Exception as e:
    print(f"[3/4] FAIL could not create a file: {e}")
    print("\n           -> fall back to writing reports into the GitHub repo.")
    sys.exit(1)

# --- 4. move it: reports/ -> processed/ -> and clean up ----------------------
drive.files().update(fileId=made["id"],
                     addParents=folders["processed"],
                     removeParents=folders["reports"],
                     fields="id, parents").execute()
print("[4/4] OK   moved the file reports/ -> processed/")

drive.files().delete(fileId=made["id"]).execute()
print("           cleaned up the test file")
print("\nALL FOUR PASSED - reports can go to Drive. Proceeding as planned.")
