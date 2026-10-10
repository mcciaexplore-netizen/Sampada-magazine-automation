from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from magazine_app import (
    CONFIG_PATH,
    ROOT,
    build_excel,
    copy_outputs,
    detect_qr_pdf_pages,
    monthly_paths,
    save_manifest,
    select_articles_containing_qr,
    status_summary,
)
import google_drive
from main import (
    create_links_template,
    extract_articles,
    load_config,
    make_qr_codes,
    metadata_and_script,
    narrate,
    produce_media,
    render_videos,
)


log = logging.getLogger("sampada")
app = FastAPI(title="Sampada media studio API")

DEFAULT_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:3000",
    "https://sampada-magazine-automation.vercel.app",
]
extra_origins = [item.strip() for item in os.environ.get("CORS_ORIGINS", "").split(",") if item.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=DEFAULT_ORIGINS + extra_origins,
    allow_origin_regex=r"^https://sampada-magazine-automation[a-z0-9-]*\.vercel\.app$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "status": "online",
        "service": "Sampada Magazine Automation API",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/api/health",
        "endpoints": [
            "/api/health",
            "/api/status/{issue_key}",
            "/api/analyze",
            "/api/review",
            "/api/audio",
            "/api/video",
            "/api/links/{issue_key}",
            "/api/qr",
            "/api/automate"
        ]
    }


class ReviewPayload(BaseModel):
    issue_key: str
    rows: list[dict]


class IssuePayload(BaseModel):
    issue_key: str


class LinksPayload(BaseModel):
    issue_key: str
    rows: list[dict]


def issue_context(issue_key: str) -> tuple[Path, Path, dict, dict[str, Path]]:
    if not issue_key or any(ch not in "0123456789-" for ch in issue_key):
        raise HTTPException(400, "Invalid issue key")
    issue_dir = (ROOT / "work" / issue_key).resolve()
    if ROOT.resolve() not in issue_dir.parents:
        raise HTTPException(400, "Invalid issue path")
    manifest = issue_dir / "manifest.csv"
    state_path = issue_dir / "app_state.json"
    if not manifest.exists() or not state_path.exists():
        raise HTTPException(404, "Analyze this edition first")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    drive_paths = {key: Path(value) for key, value in state["drive_paths"].items()}
    return issue_dir, manifest, state, drive_paths


def resolve_drive_root(value: str) -> Path:
    """Output folder must live inside the project or the user's home (set SAMPADA_ALLOW_ANY_PATH=1 to lift this)."""
    path = Path(value.strip() or "Sampada Drive").expanduser()
    path = (path if path.is_absolute() else ROOT / path).resolve()
    if os.environ.get("SAMPADA_ALLOW_ANY_PATH") == "1":
        return path
    allowed = [ROOT.resolve(), Path.home().resolve()]
    if not any(path == base or base in path.parents for base in allowed):
        raise HTTPException(400, "Drive folder must be inside the project folder or your home folder")
    return path


def normalise_rows(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty or "selected" not in frame.columns:
        raise HTTPException(400, "Select at least one article")
    frame["selected"] = frame["selected"].map(lambda value: "yes" if str(value).lower() in {"yes", "true", "1"} else "no")
    if not frame["selected"].eq("yes").any():
        raise HTTPException(400, "Select at least one article")
    if "language" not in frame.columns:
        frame["language"] = "en"
    frame["language"] = frame["language"].fillna("").replace("", "en")
    return frame


def make_captions(issue_dir: Path, config: dict) -> str | None:
    try:
        from generate_captions import main as gen_captions_main
        gen_captions_main(issue_dir, config)
    except Exception as error:
        log.exception("Caption generation failed")
        return f"Captions failed: {error}"
    return None


def frame_records(frame: pd.DataFrame) -> list[dict]:
    safe = frame.fillna("")
    return safe.to_dict(orient="records")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/status/{issue_key}")
def issue_status(issue_key: str) -> dict:
    issue_dir, _manifest, _state, _drive_paths = issue_context(issue_key)
    return {"stats": status_summary(issue_dir)}


@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(...),
    year: int = Form(...),
    month: int = Form(...),
    drive_root: str = Form("Sampada Drive"),
) -> dict:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Upload a PDF magazine")
    if not 2000 <= year <= 2100 or not 1 <= month <= 12:
        raise HTTPException(400, "Invalid month or year")
    drive_dir = resolve_drive_root(drive_root)
    issue_key = f"{year}-{month:02d}"
    issue_dir = ROOT / "work" / issue_key
    issue_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = issue_dir / "magazine.pdf"
    with pdf_path.open("wb") as stream:
        while chunk := await file.read(1024 * 1024):
            stream.write(chunk)

    config = load_config(CONFIG_PATH)
    try:
        manifest = extract_articles(pdf_path, issue_dir, config)
    except RuntimeError as error:
        raise HTTPException(422, str(error))
    qr_pages = detect_qr_pdf_pages(pdf_path)
    frame = select_articles_containing_qr(manifest, qr_pages)
    drive_paths = monthly_paths(drive_dir, year, month)
    state = {
        "year": year,
        "month": month,
        "drive_paths": {key: str(value) for key, value in drive_paths.items()},
        "qr_pages": qr_pages,
    }
    (issue_dir / "app_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    return {
        "issue_key": issue_key,
        "rows": frame_records(frame),
        "qr_pages": qr_pages,
        "fallback": not bool(qr_pages),
        "stats": status_summary(issue_dir),
        "monthly_folder": str(drive_paths["root"]),
    }


def prepare_selection(payload: ReviewPayload):
    issue_dir, manifest, state, drive_paths = issue_context(payload.issue_key)
    frame = normalise_rows(payload.rows)
    save_manifest(frame, manifest)
    return issue_dir, manifest, state, drive_paths


def write_excel(metadata_csv: Path, state: dict, drive_paths: dict[str, Path]) -> Path:
    output = drive_paths["data"] / f"Sampada_{state['year']}_{state['month']:02d}_YouTube.xlsx"
    build_excel(metadata_csv, output, state["month"], state["year"])
    return output


@app.post("/api/review")
def review(payload: ReviewPayload) -> dict:
    issue_dir, manifest, state, drive_paths = prepare_selection(payload)
    config = load_config(CONFIG_PATH)
    metadata_csv = metadata_and_script(manifest, issue_dir, config)
    output = write_excel(metadata_csv, state, drive_paths)
    return {"message": "Excel and narration scripts created", "xlsx": str(output), "stats": status_summary(issue_dir)}


@app.post("/api/audio")
def audio(payload: IssuePayload) -> dict:
    issue_dir, manifest, _state, drive_paths = issue_context(payload.issue_key)
    config = load_config(CONFIG_PATH)
    narrate(manifest, issue_dir, config)
    warning = make_captions(issue_dir, config)
    copy_outputs(issue_dir, drive_paths)
    return {"message": "Audio narration and captions generated" + (f" ({warning})" if warning else ""), "stats": status_summary(issue_dir)}


@app.post("/api/video")
def video(payload: IssuePayload) -> dict:
    issue_dir, manifest, _state, drive_paths = issue_context(payload.issue_key)
    render_videos(manifest, issue_dir, load_config(CONFIG_PATH))
    copy_outputs(issue_dir, drive_paths)
    return {"message": "Video rendering complete", "stats": status_summary(issue_dir)}


@app.get("/api/links/{issue_key}")
def get_links(issue_key: str) -> dict:
    issue_dir, manifest, _state, _drive_paths = issue_context(issue_key)
    path = create_links_template(manifest, issue_dir)
    return {"rows": frame_records(pd.read_csv(path, encoding="utf-8-sig"))}


@app.post("/api/automate")
def automate(payload: ReviewPayload) -> dict:
    issue_dir, manifest, state, drive_paths = prepare_selection(payload)
    config = load_config(CONFIG_PATH)
    metadata_csv = metadata_and_script(manifest, issue_dir, config)
    produce_media(manifest, issue_dir, config)
    warning = make_captions(issue_dir, config)
    output_xlsx = write_excel(metadata_csv, state, drive_paths)
    copy_outputs(issue_dir, drive_paths)
    message = "Audio, videos, captions and Excel plan are ready in the output folder."
    result = {"xlsx": str(output_xlsx), "stats": status_summary(issue_dir), "drive_folder": str(drive_paths["root"])}
    if google_drive.is_configured():
        try:
            result.update(google_drive.publish_issue(issue_dir, state, metadata_csv))
            message = "Everything generated, uploaded to Google Drive, and the Google Sheet was created."
        except Exception as error:
            log.exception("Google upload failed")
            message += f" Google upload failed: {error}"
    if warning:
        message += f" {warning}"
    result["message"] = message
    return result


@app.get("/api/google/status")
def google_status() -> dict:
    return {"configured": google_drive.is_configured(), "help": google_drive.SETUP_HELP}


@app.post("/api/google/publish")
def google_publish(payload: IssuePayload) -> dict:
    issue_dir, _manifest, state, _drive_paths = issue_context(payload.issue_key)
    metadata_csv = issue_dir / "youtube_metadata.csv"
    if not metadata_csv.exists():
        raise HTTPException(409, "Create Excel & scripts first")
    if not google_drive.is_configured():
        raise HTTPException(400, google_drive.SETUP_HELP)
    try:
        result = google_drive.publish_issue(issue_dir, state, metadata_csv)
    except Exception as error:
        log.exception("Google upload failed")
        raise HTTPException(502, f"Google upload failed: {error}")
    return {"message": "Uploaded to Google Drive and Google Sheet updated", **result}


@app.post("/api/qr")
def qr(payload: LinksPayload) -> dict:
    issue_dir, manifest, state, drive_paths = issue_context(payload.issue_key)
    path = create_links_template(manifest, issue_dir)
    pd.DataFrame(payload.rows).to_csv(path, index=False, encoding="utf-8-sig")
    try:
        created = make_qr_codes(path, issue_dir)
    except ValueError as error:
        raise HTTPException(400, str(error))
    if created == 0:
        raise HTTPException(400, "Add at least one valid YouTube URL")
    copy_outputs(issue_dir, drive_paths)
    metadata_csv = issue_dir / "youtube_metadata.csv"
    result: dict = {}
    message = f"Created {created} QR code(s)"
    if metadata_csv.exists():
        write_excel(metadata_csv, state, drive_paths)
        if google_drive.is_configured():
            try:
                result.update(google_drive.publish_issue(issue_dir, state, metadata_csv))
                message += "; QR codes and YouTube links added to the Google Sheet"
            except Exception as error:
                log.exception("Google upload failed")
                message += f" (Google Sheet update failed: {error})"
    return {"message": message, "stats": status_summary(issue_dir), **result}
