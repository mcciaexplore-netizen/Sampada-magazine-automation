# Sampada magazine-to-YouTube automation

## How it works

1. Upload the Sampada PDF. Every article ends with a "Scan the QR code to listen to the gist of the article" box (or a real QR in the final PDF); articles whose pages contain it are selected automatically. If none is found, tick articles manually.
2. One click generates narration audio, SRT captions, 1080p videos and the Excel plan, uploads Audio / Videos / Captions to Google Drive and writes a Google Sheet (article, audio link, video link, YouTube title, description, captions, YouTube link, QR link).
3. Upload the videos to YouTube, paste the YouTube links in the app, and it creates the QR codes, uploads them to Drive and fills the Sheet's YouTube link and QR columns.

## Google Drive / Sheet setup (one time)

For local development: Google Cloud Console > create a project > enable **Google Drive API** and **Google Sheets API** > Credentials > Create OAuth client ID > Desktop app. Download the JSON as `google_credentials.json` in this folder; the first upload opens a browser to sign in (token cached in `google_token.json`).

For the Vercel deployment below, use a **service account** instead (there's no browser to sign in from inside a serverless function):

1. Google Cloud Console > IAM & Admin > Service Accounts > create one, enable the Drive and Sheets APIs for the project, and create a JSON key for it.
2. Service accounts have no storage of their own, so share a **Shared Drive** (or a folder inside one) with the service account's email address, and note that folder's ID.
3. Set the `GOOGLE_SERVICE_ACCOUNT_JSON` env var to the key file's contents, and `GOOGLE_DRIVE_FOLDER_ID` to the shared folder ID.

Optional: `GOOGLE_DRIVE_PUBLIC=1` makes uploaded files viewable by anyone with the link.

Per-issue manual overrides (titles, video titles, category badges) live in `config.json` under `issue_overrides` keyed by `YYYY-MM`.

## Deploying on Vercel

This runs as a single Vercel project: the React app builds to a static site, and the FastAPI backend (`backend_api.py`, exposed at `api/index.py`) runs as a Python serverless function, both under one domain (`vercel.json` routes `/api/*` to the function and everything else to the frontend).

Serverless functions have no persistent disk and can't run background threads past the response, so generated files live in **Vercel Blob** and job/queue state lives in **Upstash Redis** + **Upstash QStash** instead of the local `work/` folder and in-process thread the CLI/local dev server uses. Required setup:

1. Attach the **Vercel Blob**, **Upstash for Redis**, and **Upstash QStash/Workflow** integrations to the project from the Vercel Marketplace — these auto-inject `BLOB_READ_WRITE_TOKEN`; `KV_REST_API_URL`/`KV_REST_API_TOKEN` (the Upstash Marketplace integration uses the Vercel KV naming, not `UPSTASH_REDIS_REST_URL`/`UPSTASH_REDIS_REST_TOKEN` — `job_state.py` accepts either); and `QSTASH_TOKEN`/`QSTASH_CURRENT_SIGNING_KEY`/`QSTASH_NEXT_SIGNING_KEY`.
2. Set `APP_BASE_URL` to this deployment's public URL (used to target QStash callbacks at `/api/worker/article`).
3. Set the Google service-account env vars above.
4. In the Vercel project settings, make sure **Root Directory** is the repository root (not `frontend/`) — `vercel.json` here builds both the frontend and the API.
5. The Python function bundle exceeds the standard 250/500MB limit, so enable **Fluid Compute** (Project Settings → Functions) and set `VERCEL_SUPPORT_LARGE_FUNCTIONS=1` as a project environment variable — this works on the Hobby plan, Vercel Pro is not required for it.

Without the QStash env vars set, `/api/automate` falls back to running each article's narration/video job inline in the same request — fine for local testing with `vercel dev`, but not recommended in production since it reintroduces the function duration limit the queue is meant to avoid.

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
