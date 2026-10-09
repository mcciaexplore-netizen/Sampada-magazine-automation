# Sampada magazine-to-YouTube automation

## How it works

1. Upload the Sampada PDF. Every article ends with a "Scan the QR code to listen to the gist of the article" box (or a real QR in the final PDF); articles whose pages contain it are selected automatically. If none is found, tick articles manually.
2. One click generates narration audio, SRT captions, 1080p videos and the Excel plan, uploads Audio / Videos / Captions to Google Drive and writes a Google Sheet (article, audio link, video link, YouTube title, description, captions, YouTube link, QR link).
3. Upload the videos to YouTube, paste the YouTube links in the app, and it creates the QR codes, uploads them to Drive and fills the Sheet's YouTube link and QR columns.

## Google Drive / Sheet setup (one time)

1. Google Cloud Console: create a project, enable **Google Drive API** and **Google Sheets API**.
2. Credentials > Create OAuth client ID > Desktop app. Download the JSON and save it as `google_credentials.json` in this folder.
3. Restart the backend. The first upload opens a browser to sign in (token cached in `google_token.json`).

Alternatives: `GOOGLE_SERVICE_ACCOUNT_JSON` (key path or JSON) with `GOOGLE_DRIVE_FOLDER_ID` for a shared folder; `GOOGLE_DRIVE_PUBLIC=1` to make links viewable by anyone with the link. Production CORS: set `CORS_ORIGINS` (comma separated). Deploy the frontend on Vercel (`VITE_API_BASE_URL` = backend URL) and the backend on Render/locally.

Per-issue manual overrides (titles, video titles, category badges) live in `config.json` under `issue_overrides` keyed by `YYYY-MM`.

Known limit: Marathi pages in the PDF use a legacy font, so extracted Marathi text is garbled. Marathi video title cards are cropped from the page image and look correct, but edit Marathi titles/scripts by hand before narration.

## React frontend

Double-click `start_react_frontend.bat`, or start the two services separately:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend_api:app --host 127.0.0.1 --port 8000
cd frontend
npm run dev
```

Open `http://127.0.0.1:5173`. React provides the interface while the local Python API performs PDF extraction, Excel generation, text-to-speech, video rendering and QR creation.

Audio generation runs up to four articles concurrently and video rendering runs up to two encodes concurrently. Completed outputs and TTS chunks are reused after an interruption. Adjust these safe limits under `performance` in `config.json`.

All narration is generated with Microsoft Edge's `en-IN-NeerjaNeural` English voice at a `+10%` speaking rate. This provides a strong quality/speed balance without downloading a large local speech model.

This project converts a selectable-text Sampada PDF into reviewable article text, YouTube metadata, multilingual narration, 1080p videos, and QR codes for manually uploaded YouTube videos.
