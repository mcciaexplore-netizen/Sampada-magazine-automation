from __future__ import annotations

import io
import json
import logging
import os
import tempfile
from pathlib import Path

import pandas as pd
import requests
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import blob_store
import job_state
from magazine_app import (
    CONFIG_PATH,
    build_excel,
    detect_qr_pdf_pages,
    select_articles_containing_qr,
    save_manifest,
    status_summary,
)
import google_drive
from main import (
    _narrate_row,
    _render_video_row,
    _video_context,
    create_links_template,
    extract_articles,
    iter_manifest,
    load_config,
    make_qr_codes,
    metadata_and_script,
)


log = logging.getLogger("sampada")
app = FastAPI(title="Sampada media studio API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Every piece of generated output for an issue lives at this Vercel Blob prefix instead
# of a local work/<issue_key>/ folder, since serverless functions share no persistent disk.
SCRATCH_ROOT = Path(tempfile.gettempdir()) / "sampada"


def issue_prefix(issue_key: str) -> str:
    return f"issues/{issue_key}/"


def validate_issue_key(issue_key: str) -> None:
    if not issue_key or any(ch not in "0123456789-" for ch in issue_key):
        raise HTTPException(400, "Invalid issue key")


def scratch_dir(issue_key: str) -> Path:
    path = SCRATCH_ROOT / issue_key
    path.mkdir(parents=True, exist_ok=True)
    return path


def pull_issue(issue_key: str) -> Path:
    """Mirror this issue's Blob contents into a fresh scratch directory under /tmp."""
    work_dir = scratch_dir(issue_key)
    blob_store.download_prefix_to_dir(issue_prefix(issue_key), work_dir)
    return work_dir


def push_issue(issue_key: str, work_dir: Path, subdirs: tuple[str, ...] | None = None) -> None:
    blob_store.upload_dir_to_prefix(work_dir, issue_prefix(issue_key), subdirs=subdirs)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled server error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {str(exc)}"},
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
            "/api/automate",
            "/api/job/{issue_key}",
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


def issue_context(issue_key: str) -> tuple[Path, Path, dict]:
    validate_issue_key(issue_key)
    work_dir = pull_issue(issue_key)
    manifest = work_dir / "manifest.csv"
    state_path = work_dir / "app_state.json"
    if not manifest.exists() or not state_path.exists():
        raise HTTPException(404, "Analyze this edition first")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    return work_dir, manifest, state


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


def make_captions(work_dir: Path, config: dict) -> str | None:
    try:
        from generate_captions import main as gen_captions_main
        gen_captions_main(work_dir, config)
    except Exception as error:
        log.exception("Caption generation failed")
        return f"Captions failed: {error}"
    return None


def frame_records(frame: pd.DataFrame) -> list[dict]:
    safe = frame.fillna("")
    return safe.to_dict(orient="records")


def write_excel(metadata_csv: Path, state: dict, work_dir: Path) -> Path:
    output = work_dir / f"Sampada_{state['year']}_{state['month']:02d}_YouTube.xlsx"
    build_excel(metadata_csv, output, state["month"], state["year"])
    return output


def remote_status(issue_key: str) -> dict:
    """Status counts without pulling the whole issue, so polling endpoints stay cheap."""
    manifest_bytes = blob_store.get_bytes(f"{issue_prefix(issue_key)}manifest.csv")
    selected = 0
    if manifest_bytes:
        frame = pd.read_csv(io.BytesIO(manifest_bytes), encoding="utf-8-sig")
        selected = int(frame["selected"].astype(str).str.lower().eq("yes").sum())

    def count(subdir: str, suffix: str) -> int:
        names = blob_store.list_prefix(f"{issue_prefix(issue_key)}{subdir}/")
        return sum(1 for name in names if name.endswith(suffix))

    return {
        "selected": selected,
        "audio": count("audio", ".mp3"),
        "videos": count("videos", ".mp4"),
        "qr": count("qr_codes", ".png"),
    }


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/status/{issue_key}")
def issue_status(issue_key: str) -> dict:
    validate_issue_key(issue_key)
    if blob_store.get_bytes(f"{issue_prefix(issue_key)}manifest.csv") is None:
        raise HTTPException(404, "Analyze this edition first")
    return {"stats": remote_status(issue_key)}


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
    issue_key = f"{year}-{month:02d}"
    work_dir = scratch_dir(issue_key)
    pdf_path = work_dir / "magazine.pdf"
    with pdf_path.open("wb") as stream:
        while chunk := await file.read(1024 * 1024):
            stream.write(chunk)

    config = load_config(CONFIG_PATH)
    try:
        manifest = extract_articles(pdf_path, work_dir, config)
    except RuntimeError as error:
        raise HTTPException(422, str(error))
    except Exception as error:
        log.exception("PDF extraction failed: %s", error)
        raise HTTPException(422, f"Failed to extract articles from PDF: {error}")

    try:
        qr_pages = detect_qr_pdf_pages(pdf_path)
    except Exception as error:
        log.exception("QR detection failed: %s", error)
        qr_pages = []
    frame = select_articles_containing_qr(manifest, qr_pages)
    state = {"year": year, "month": month, "qr_pages": qr_pages}
    (work_dir / "app_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    push_issue(issue_key, work_dir)
    return {
        "issue_key": issue_key,
        "rows": frame_records(frame),
        "qr_pages": qr_pages,
        "fallback": not bool(qr_pages),
        "stats": status_summary(work_dir),
        "monthly_folder": f"Vercel Blob: {issue_prefix(issue_key)}",
    }


def prepare_selection(payload: ReviewPayload) -> tuple[Path, Path, dict]:
    work_dir, manifest, state = issue_context(payload.issue_key)
    frame = normalise_rows(payload.rows)
    save_manifest(frame, manifest)
    return work_dir, manifest, state


@app.post("/api/review")
def review(payload: ReviewPayload) -> dict:
    work_dir, manifest, state = prepare_selection(payload)
    config = load_config(CONFIG_PATH)
    metadata_csv = metadata_and_script(manifest, work_dir, config)
    output = write_excel(metadata_csv, state, work_dir)
    push_issue(payload.issue_key, work_dir)
    return {"message": "Excel and narration scripts created", "xlsx": str(output.name), "stats": status_summary(work_dir)}


@app.post("/api/audio")
def audio(payload: IssuePayload) -> dict:
    work_dir, manifest, _state = issue_context(payload.issue_key)
    config = load_config(CONFIG_PATH)
    for row in iter_manifest(manifest):
        _narrate_row(row, work_dir, config, work_dir / "audio")
    warning = make_captions(work_dir, config)
    push_issue(payload.issue_key, work_dir, subdirs=("audio", "captions"))
    return {"message": "Audio narration and captions generated" + (f" ({warning})" if warning else ""), "stats": status_summary(work_dir)}


@app.post("/api/video")
def video(payload: IssuePayload) -> dict:
    work_dir, manifest, _state = issue_context(payload.issue_key)
    config = load_config(CONFIG_PATH)
    cards, videos, ffmpeg, pdf_path = _video_context(work_dir)
    for row in iter_manifest(manifest):
        _render_video_row(row, work_dir, config, cards, videos, ffmpeg, pdf_path)
    push_issue(payload.issue_key, work_dir, subdirs=("cards", "videos"))
    return {"message": "Video rendering complete", "stats": status_summary(work_dir)}


@app.get("/api/links/{issue_key}")
def get_links(issue_key: str) -> dict:
    work_dir, manifest, _state = issue_context(issue_key)
    path = create_links_template(manifest, work_dir)
    push_issue(issue_key, work_dir)
    return {"rows": frame_records(pd.read_csv(path, encoding="utf-8-sig"))}


# --- 1-click automation: one QStash-queued HTTP call per article, instead of a
#     background thread that can't survive past the response on Vercel. ---

QSTASH_API = "https://qstash.upstash.io/v2/publish"


def app_base_url() -> str:
    base = os.environ.get("APP_BASE_URL")
    if base:
        return base.rstrip("/")
    vercel_url = os.environ.get("VERCEL_URL")
    if vercel_url:
        return f"https://{vercel_url}"
    raise RuntimeError("Set APP_BASE_URL to this deployment's public URL")


def enqueue_article_job(issue_key: str, article_id: str) -> None:
    token = os.environ.get("QSTASH_TOKEN")
    destination = f"{app_base_url()}/api/worker/article"
    if not token:
        # Local/dev fallback with no queue configured: run inline so the flow still works.
        run_article_job(issue_key, article_id)
        return
    response = requests.post(
        f"{QSTASH_API}/{destination}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"issue_key": issue_key, "article_id": article_id},
        timeout=30,
    )
    response.raise_for_status()


def verify_qstash_signature(signature: str, body: bytes) -> None:
    current_key = os.environ.get("QSTASH_CURRENT_SIGNING_KEY")
    next_key = os.environ.get("QSTASH_NEXT_SIGNING_KEY")
    if not current_key or not next_key:
        return  # no queue configured (local dev / inline fallback) -- nothing to verify
    from qstash import Receiver
    receiver = Receiver(current_signing_key=current_key, next_signing_key=next_key)
    try:
        receiver.verify(signature=signature, body=body.decode("utf-8"), url=f"{app_base_url()}/api/worker/article")
    except Exception as error:
        raise HTTPException(401, f"Invalid QStash signature: {error}")


@app.post("/api/automate")
def automate(payload: ReviewPayload) -> dict:
    """Queue the full pipeline (one call per article); poll /api/job/{issue_key} for the result."""
    work_dir, manifest, _state = prepare_selection(payload)
    config = load_config(CONFIG_PATH)
    metadata_and_script(manifest, work_dir, config)
    push_issue(payload.issue_key, work_dir)

    rows = list(iter_manifest(manifest))
    if not rows:
        raise HTTPException(400, "Select at least one article")
    job_state.start_job(payload.issue_key, total=len(rows))
    for row in rows:
        enqueue_article_job(payload.issue_key, row["id"])
    return {"status": "running"}


def run_article_job(issue_key: str, article_id: str) -> None:
    work_dir = pull_issue(issue_key)
    manifest = work_dir / "manifest.csv"
    config = load_config(CONFIG_PATH)
    rows = {row["id"]: row for row in iter_manifest(manifest)}
    row = rows.get(article_id)
    if row is None:
        job_state.mark_job_failed(issue_key, f"Unknown article id: {article_id}")
        return
    try:
        cards, videos, ffmpeg, pdf_path = _video_context(work_dir)
        _narrate_row(row, work_dir, config, work_dir / "audio")
        _render_video_row(row, work_dir, config, cards, videos, ffmpeg, pdf_path)
        push_issue(issue_key, work_dir, subdirs=("audio", "captions", "cards", "videos"))
    except Exception as error:
        log.exception("Article worker failed: %s", article_id)
        job_state.mark_job_failed(issue_key, f"{article_id}: {error}")
        return

    completed = job_state.mark_job_article_done(issue_key)
    job = job_state.get_job(issue_key)
    if job and job.get("status") == "running" and completed >= job.get("total", 0):
        if job_state.try_acquire_finalize_lock(issue_key):
            finalize_automation(issue_key, work_dir, manifest, config)


def finalize_automation(issue_key: str, work_dir: Path, manifest: Path, config: dict) -> None:
    state_path = work_dir / "app_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    metadata_csv = work_dir / "youtube_metadata.csv"
    warning = make_captions(work_dir, config)
    output_xlsx = write_excel(metadata_csv, state, work_dir)
    push_issue(issue_key, work_dir)
    message = "Audio, videos, captions and Excel plan are ready."
    result: dict = {"xlsx": output_xlsx.name, "stats": status_summary(work_dir)}
    if google_drive.is_configured():
        try:
            result.update(google_drive.publish_issue(work_dir, state, metadata_csv))
            push_issue(issue_key, work_dir)  # persist google_state.json for future publishes
            message = "Everything generated, uploaded to Google Drive, and the Google Sheet was created."
        except Exception as error:
            log.exception("Google upload failed")
            message += f" Google upload failed: {error}"
    if warning:
        message += f" {warning}"
    result["message"] = message
    job_state.mark_job_done(issue_key, result)


@app.post("/api/worker/article")
async def worker_article(request: Request) -> dict:
    body = await request.body()
    verify_qstash_signature(request.headers.get("Upstash-Signature", ""), body)
    payload = json.loads(body)
    issue_key, article_id = payload["issue_key"], payload["article_id"]
    validate_issue_key(issue_key)
    run_article_job(issue_key, article_id)
    return {"status": "ok"}


@app.get("/api/job/{issue_key}")
def job_status(issue_key: str) -> dict:
    validate_issue_key(issue_key)
    job = job_state.get_job(issue_key)
    if job is None:
        return {"status": "none", "stats": remote_status(issue_key)}
    return {**job, "stats": remote_status(issue_key)}


@app.get("/api/google/status")
def google_status() -> dict:
    return {"configured": google_drive.is_configured(), "help": google_drive.SETUP_HELP}


@app.post("/api/google/publish")
def google_publish(payload: IssuePayload) -> dict:
    work_dir, _manifest, state = issue_context(payload.issue_key)
    metadata_csv = work_dir / "youtube_metadata.csv"
    if not metadata_csv.exists():
        raise HTTPException(409, "Create Excel & scripts first")
    if not google_drive.is_configured():
        raise HTTPException(400, google_drive.SETUP_HELP)
    try:
        result = google_drive.publish_issue(work_dir, state, metadata_csv)
        push_issue(payload.issue_key, work_dir)
    except Exception as error:
        log.exception("Google upload failed")
        raise HTTPException(502, f"Google upload failed: {error}")
    return {"message": "Uploaded to Google Drive and Google Sheet updated", **result}


@app.post("/api/qr")
def qr(payload: LinksPayload) -> dict:
    work_dir, manifest, state = issue_context(payload.issue_key)
    path = create_links_template(manifest, work_dir)
    pd.DataFrame(payload.rows).to_csv(path, index=False, encoding="utf-8-sig")
    try:
        created = make_qr_codes(path, work_dir)
    except ValueError as error:
        raise HTTPException(400, str(error))
    if created == 0:
        raise HTTPException(400, "Add at least one valid YouTube URL")
    metadata_csv = work_dir / "youtube_metadata.csv"
    result: dict = {}
    message = f"Created {created} QR code(s)"
    if metadata_csv.exists():
        write_excel(metadata_csv, state, work_dir)
        push_issue(payload.issue_key, work_dir)
        if google_drive.is_configured():
            try:
                result.update(google_drive.publish_issue(work_dir, state, metadata_csv))
                push_issue(payload.issue_key, work_dir)
                message += "; QR codes and YouTube links added to the Google Sheet"
            except Exception as error:
                log.exception("Google upload failed")
                message += f" (Google Sheet update failed: {error})"
    else:
        push_issue(payload.issue_key, work_dir)
    return {"message": message, "stats": status_summary(work_dir), **result}
