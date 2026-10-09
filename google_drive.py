"""Upload Sampada outputs to Google Drive and keep a Google Sheet of links up to date.

Authentication (pick one):
  * OAuth desktop client (personal Google account): save the client JSON as ``google_credentials.json``
    next to this file (or point GOOGLE_OAUTH_CLIENT_FILE at it). A browser opens once to sign in;
    the token is cached in ``google_token.json``.
  * Service account: set GOOGLE_SERVICE_ACCOUNT_JSON to the key file path (or the JSON itself) and
    set GOOGLE_DRIVE_FOLDER_ID to a Shared Drive / folder the service account can write to.

Optional: GOOGLE_DRIVE_FOLDER_ID (parent folder; default is My Drive root),
          GOOGLE_DRIVE_PUBLIC=1 (make uploaded files viewable by anyone with the link).
"""
from __future__ import annotations

import calendar
import csv
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCOPES = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]
FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
MIME = {".mp3": "audio/mpeg", ".mp4": "video/mp4", ".srt": "application/x-subrip", ".png": "image/png"}
SETUP_HELP = (
    "Google is not configured. Save an OAuth desktop client as google_credentials.json in the project folder "
    "(Google Cloud Console > APIs & Services > Credentials; enable Drive API and Sheets API), "
    "or set GOOGLE_SERVICE_ACCOUNT_JSON. See README."
)


def _oauth_client_file() -> Path:
    return Path(os.environ.get("GOOGLE_OAUTH_CLIENT_FILE") or ROOT / "google_credentials.json")


def is_configured() -> bool:
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")) or _oauth_client_file().exists() or (ROOT / "google_token.json").exists()


def _credentials():
    service_account = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if service_account:
        from google.oauth2 import service_account as sa
        if service_account.startswith("{"):
            return sa.Credentials.from_service_account_info(json.loads(service_account), scopes=SCOPES)
        return sa.Credentials.from_service_account_file(service_account, scopes=SCOPES)
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    token_path = ROOT / "google_token.json"
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES) if token_path.exists() else None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        from google_auth_oauthlib.flow import InstalledAppFlow
        creds = InstalledAppFlow.from_client_secrets_file(str(_oauth_client_file()), SCOPES).run_local_server(port=0)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


_creds_cache = None


def _drive_service():
    from googleapiclient.discovery import build
    global _creds_cache
    if _creds_cache is None:
        _creds_cache = _credentials()
    return build("drive", "v3", credentials=_creds_cache, cache_discovery=False)


def _services():
    from googleapiclient.discovery import build
    drive = _drive_service()
    return drive, build("sheets", "v4", credentials=_creds_cache, cache_discovery=False)


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _find(drive, name: str, parent: str, mime: str | None = None) -> dict | None:
    query = f"name = '{_q(name)}' and '{parent}' in parents and trashed = false"
    if mime:
        query += f" and mimeType = '{mime}'"
    found = drive.files().list(q=query, fields="files(id, webViewLink)", pageSize=1, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    return (found.get("files") or [None])[0]


def _folder(drive, name: str, parent: str) -> str:
    existing = _find(drive, name, parent, FOLDER_MIME)
    if existing:
        return existing["id"]
    body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}
    return drive.files().create(body=body, fields="id", supportsAllDrives=True).execute()["id"]


def _upload(drive, path: Path, parent: str, public: bool) -> str:
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(path), mimetype=MIME.get(path.suffix.lower(), "application/octet-stream"), resumable=True)
    existing = _find(drive, path.name, parent)
    if existing:
        item = drive.files().update(fileId=existing["id"], media_body=media, fields="id, webViewLink", supportsAllDrives=True).execute()
    else:
        body = {"name": path.name, "parents": [parent]}
        item = drive.files().create(body=body, media_body=media, fields="id, webViewLink", supportsAllDrives=True).execute()
        if public:
            drive.permissions().create(fileId=item["id"], body={"type": "anyone", "role": "reader"}, supportsAllDrives=True).execute()
    return item["webViewLink"]


def _sheet_values(rows: list[dict]) -> list[list[str]]:
    header = ["ID", "Article", "Language", "Audio (Drive link)", "Video (Drive link)", "YouTube title", "YouTube description",
              "YouTube captions (SRT file)", "YouTube captions (text)", "YouTube link", "QR code (Drive link)"]
    values = [header]
    for row in rows:
        values.append([row["id"], row["title"], "Marathi" if row["language"] == "mr" else "English", row["audio_link"], row["video_link"],
                       row["title"], row["description"], row["caption_link"], row["caption_text"][:45000], row["youtube_url"], row["qr_link"]])
    return values


def publish_issue(issue_dir: Path, state: dict, metadata_csv: Path) -> dict:
    """Upload audio, video, captions and QR codes, then (re)write the Sheet. Returns links."""
    drive, sheets = _services()
    public = os.environ.get("GOOGLE_DRIVE_PUBLIC") == "1"
    parent = os.environ.get("GOOGLE_DRIVE_FOLDER_ID") or "root"
    year, month = int(state["year"]), int(state["month"])
    for name in ("Sampada", str(year), f"{month:02d} - {calendar.month_name[month]}"):
        parent = _folder(drive, name, parent)
    month_folder = parent
    subfolders = {kind: _folder(drive, label, month_folder) for kind, label in
                  (("audio", "Audio"), ("videos", "Videos"), ("captions", "Captions"), ("qr_codes", "QR Codes"))}

    links_csv = issue_dir / "youtube_links.csv"
    urls: dict[str, str] = {}
    if links_csv.exists():
        with links_csv.open("r", encoding="utf-8-sig", newline="") as stream:
            urls = {r["id"]: r.get("youtube_url", "") for r in csv.DictReader(stream)}
    with metadata_csv.open("r", encoding="utf-8-sig", newline="") as stream:
        metadata = list(csv.DictReader(stream))

    def process(item: dict) -> dict:
        drive = _drive_service()  # googleapiclient services are not thread-safe
        stem = Path(item["audio_file"]).stem
        files = {"audio": issue_dir / "audio" / item["audio_file"], "videos": issue_dir / "videos" / item["video_file"],
                 "captions": issue_dir / "captions" / f"{stem}.srt", "qr_codes": issue_dir / "qr_codes" / f"{stem}.png"}
        link = {kind: (_upload(drive, path, subfolders[kind], public) if path.exists() else "") for kind, path in files.items()}
        caption = files["captions"]
        return {"id": item["id"], "title": item["title"], "language": item.get("language", "en"), "description": item["description"],
                "audio_link": link["audio"], "video_link": link["videos"], "caption_link": link["captions"],
                "caption_text": caption.read_text(encoding="utf-8") if caption.exists() else "",
                "youtube_url": urls.get(item["id"], ""), "qr_link": link["qr_codes"]}

    with ThreadPoolExecutor(max_workers=5) as executor:
        rows = list(executor.map(process, metadata))

    google_state_path = issue_dir / "google_state.json"
    google_state = json.loads(google_state_path.read_text(encoding="utf-8")) if google_state_path.exists() else {}
    title = f"Sampada {calendar.month_name[month]} {year} - YouTube Plan"
    sheet_id = google_state.get("sheet_id")
    if sheet_id:
        try:
            drive.files().get(fileId=sheet_id, fields="id", supportsAllDrives=True).execute()
        except Exception:
            sheet_id = None
    if not sheet_id:
        existing = _find(drive, title, month_folder, SHEET_MIME)
        if existing:
            sheet_id = existing["id"]
        else:
            body = {"name": title, "mimeType": SHEET_MIME, "parents": [month_folder]}
            sheet_id = drive.files().create(body=body, fields="id", supportsAllDrives=True).execute()["id"]
    values = _sheet_values(rows)
    sheets.spreadsheets().values().clear(spreadsheetId=sheet_id, range="A:Z").execute()
    sheets.spreadsheets().values().update(spreadsheetId=sheet_id, range="A1", valueInputOption="USER_ENTERED", body={"values": values}).execute()
    first_tab = sheets.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties.sheetId").execute()["sheets"][0]["properties"]["sheetId"]
    sheets.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [
        {"repeatCell": {"range": {"sheetId": first_tab, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}}, "fields": "userEnteredFormat.textFormat.bold"}},
        {"updateSheetProperties": {"properties": {"sheetId": first_tab, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
    ]}).execute()
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    google_state_path.write_text(json.dumps({"sheet_id": sheet_id, "sheet_url": sheet_url}), encoding="utf-8")
    folder_url = f"https://drive.google.com/drive/folders/{month_folder}"
    return {"sheet_url": sheet_url, "drive_folder_url": folder_url}
