"""
After the quota failure: probe what the service account CAN still do.
  A. move a file the USER owns  (inbox -> processed -> back)
  B. OVERWRITE the contents of an existing file the USER owns
     (no new ownership => no SA quota consumed => may work)
"""
import sys
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from google.oauth2 import service_account

key_path, root_id = sys.argv[1], sys.argv[2]
creds = service_account.Credentials.from_service_account_file(
    key_path, scopes=["https://www.googleapis.com/auth/drive"])
drive = build("drive", "v3", credentials=creds)


def children(pid, extra=""):
    return drive.files().list(
        q=f"'{pid}' in parents and trashed = false {extra}",
        fields="files(id, name, mimeType, owners(emailAddress), capabilities(canEdit))",
    ).execute().get("files", [])


folders = {f["name"]: f["id"] for f in children(root_id)
           if f["mimeType"] == "application/vnd.google-apps.folder"}

# ---- A. move -----------------------------------------------------------------
inbox = [f for f in children(folders["inbox"]) if f["mimeType"] != "application/vnd.google-apps.folder"]
if not inbox:
    print("[A] SKIP  inbox is empty")
else:
    f = inbox[0]
    print(f"[A] testing move with: {f['name']}  (owner: {f['owners'][0]['emailAddress']})")
    try:
        drive.files().update(fileId=f["id"], addParents=folders["processed"],
                             removeParents=folders["inbox"], fields="id").execute()
        drive.files().update(fileId=f["id"], addParents=folders["inbox"],
                             removeParents=folders["processed"], fields="id").execute()
        print("[A] OK    moved inbox -> processed -> inbox again. MOVE WORKS.")
    except Exception as e:
        print(f"[A] FAIL  {e}")

# ---- B. overwrite an existing user-owned file --------------------------------
reports = [f for f in children(folders["reports"]) if f["mimeType"] != "application/vnd.google-apps.folder"]
print(f"\n[B] reports/ holds {len(reports)} file(s): "
      f"{', '.join(r['name'] for r in reports) or '(empty)'}")
if not reports:
    print("[B] SKIP  need one placeholder file in reports/ to test this")
else:
    r = reports[0]
    print(f"[B] testing overwrite of: {r['name']}  "
          f"(owner: {r['owners'][0]['emailAddress']}, canEdit: {r['capabilities']['canEdit']})")
    try:
        drive.files().update(
            fileId=r["id"],
            media_body=MediaInMemoryUpload(b"overwritten by the robot\n", mimetype="text/plain"),
            fields="id, name, size",
        ).execute()
        print("[B] OK    OVERWRITE WORKS -> reports can stay in Drive.")
    except Exception as e:
        print(f"[B] FAIL  {e}")
