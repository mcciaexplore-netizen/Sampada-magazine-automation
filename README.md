# Sampada magazine-to-YouTube automation

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

## Native Python desktop frontend

Double-click `start_desktop_app.bat`, or run:

```powershell
.\.venv\Scripts\python.exe desktop_app.py
```

The native Tkinter application uses standard Windows file pickers and does not require a browser, web server, port, upload, or persistent network connection. Long operations run in background threads so the window remains responsive.

## Previous Streamlit frontend

Start the application on Windows:

```powershell
.\.venv\Scripts\streamlit.exe run streamlit_app.py
```

In the sidebar, choose the edition month/year and a folder synchronized by Google Drive for desktop. The app creates:

```text
Sampada\YYYY\MM - Month\
  Audio\
  Videos\
  QR Codes\
  Data\
```

Final editions are scanned for real QR symbols and only matching article ranges are selected. Proof PDFs without embedded QR symbols stop for a human review; the supplied August 2026 proof uses the confirmed 18-row example as its fallback.

## Workflow

1. Extract titles and article text from the magazine.
2. Review `work/manifest.csv`. Set `selected` to `no` for anything that should not become a video, and correct titles if needed.
3. Generate `work/youtube_metadata.csv` and narration scripts.
4. Review the descriptions/scripts, then create audio and videos.
5. Upload the videos to YouTube manually.
6. Create `work/youtube_links.csv`, paste each YouTube URL, and generate QR codes.

## Setup

```powershell
python -m pip install -r requirements.txt
```

Use the bundled Codex Python if your normal Python does not have the packages:

```powershell
& 'C:\Users\Aarushi Gupta\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pip install -r requirements.txt
```

## Commands

```powershell
$pdf = 'C:\Users\Aarushi Gupta\Downloads\Sampada_MCCIA_August 2026_Proof_26-8-2026.pdf'
python main.py extract $pdf
python main.py metadata
python main.py narrate
python main.py video
python main.py links
# Paste URLs into work\youtube_links.csv
python main.py qr
```

To run extraction through video rendering in one command:

```powershell
python main.py all $pdf
```

`narrate` uses Microsoft Edge's online neural voices, so it requires internet access. The configured defaults are `en-IN-NeerjaNeural` for English and `mr-IN-AarohiNeural` for Marathi. Video rendering uses the FFmpeg binary supplied by `imageio-ffmpeg`.

The supplied configuration follows the provided 18-row example: one full-magazine item plus 17 selected articles. Change `selected_pages` or `article_titles` in `config.json` for a different issue.

## Important review points

- PDF layouts vary. Always check `manifest.csv`, especially wrapped headings and article page ranges.
- The metadata generator is deterministic and does not invent facts. Human editing is recommended for marketing polish.
- Long articles and the full-magazine narration are split into safe TTS chunks and rejoined automatically.
- The tool does not publish to YouTube or modify the magazine PDF. Manual uploading keeps account credentials out of the automation.
