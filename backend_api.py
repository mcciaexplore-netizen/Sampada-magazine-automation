from __future__ import annotations

import calendar
import shutil
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
from main import (
    create_links_template,
    extract_articles,
    load_config,
    make_qr_codes,
    metadata_and_script,
    narrate,
    render_videos,
)


app = FastAPI(title="Sampada media studio API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
        "https://sampada-magazine-automation.vercel.app",
        "*",
    ],
    allow_origin_regex=r"^https:\/\/.*\.vercel\.app$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
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
    import json
    state = json.loads(state_path.read_text(encoding="utf-8"))
    drive_paths = {key: Path(value) for key, value in state["drive_paths"].items()}
    return issue_dir, manifest, state, drive_paths


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
    drive_root: str = Form(...),
) -> dict:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Upload a PDF magazine")
    if not 2000 <= year <= 2100 or not 1 <= month <= 12:
        raise HTTPException(400, "Invalid month or year")
    issue_key = f"{year}-{month:02d}"
    issue_dir = ROOT / "work" / issue_key
    issue_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = issue_dir / "magazine.pdf"
    with pdf_path.open("wb") as stream:
        while chunk := await file.read(1024 * 1024):
            stream.write(chunk)

    config = load_config(CONFIG_PATH)
    manifest = extract_articles(pdf_path, issue_dir, config)
    qr_pages = detect_qr_pdf_pages(pdf_path, resolution=96)
    frame = select_articles_containing_qr(manifest, qr_pages, use_existing_fallback=True)
    drive_paths = monthly_paths(Path(drive_root), year, month)
    import json
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


@app.post("/api/review")
def review(payload: ReviewPayload) -> dict:
    issue_dir, manifest, state, drive_paths = issue_context(payload.issue_key)
    frame = pd.DataFrame(payload.rows)
    if frame.empty or not frame["selected"].astype(str).str.lower().isin(["yes", "true", "1"]).any():
        raise HTTPException(400, "Select at least one article")
    frame["selected"] = frame["selected"].map(lambda value: "yes" if str(value).lower() in {"yes", "true", "1"} else "no")
    if "id" in frame.columns:
        frame.loc[frame["id"].astype(str).str.lower().eq("full"), "selected"] = "no"
    if "language" not in frame.columns or frame["language"].isna().all():
        frame["language"] = "en"
    missing_articles = [
        str(path)
        for path in (issue_dir / value for value in frame["article_file"].astype(str))
        if not path.exists()
    ]
    if missing_articles:
        source_pdf = issue_dir / "magazine.pdf"
        if not source_pdf.exists():
            raise HTTPException(409, "Article cache is incomplete. Analyze the edition again.")
        config = load_config(CONFIG_PATH)
        extract_articles(source_pdf, issue_dir, config)
    save_manifest(frame, manifest)
    config = load_config(CONFIG_PATH)
    metadata_csv = metadata_and_script(manifest, issue_dir, config)
    output = drive_paths["data"] / f"Sampada_{state['year']}_{state['month']:02d}_YouTube.xlsx"
    build_excel(metadata_csv, output, state["month"], state["year"])
    return {"message": "Excel and narration scripts created", "xlsx": str(output), "stats": status_summary(issue_dir)}


@app.post("/api/audio")
def audio(payload: IssuePayload) -> dict:
    issue_dir, manifest, _state, drive_paths = issue_context(payload.issue_key)
    config = load_config(CONFIG_PATH)
    narrate(manifest, issue_dir, config)
    try:
        from generate_captions import main as gen_captions_main
        gen_captions_main(issue_dir, config)
    except Exception:
        pass
    copy_outputs(issue_dir, drive_paths)
    return {"message": "Audio narration and captions generated", "stats": status_summary(issue_dir)}


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
    issue_dir, manifest, state, drive_paths = issue_context(payload.issue_key)
    frame = pd.DataFrame(payload.rows)
    if frame.empty or not frame["selected"].astype(str).str.lower().isin(["yes", "true", "1"]).any():
        raise HTTPException(400, "Select at least one article")
    frame["selected"] = frame["selected"].map(lambda value: "yes" if str(value).lower() in {"yes", "true", "1"} else "no")
    if "id" in frame.columns:
        frame.loc[frame["id"].astype(str).str.lower().eq("full"), "selected"] = "no"
    if "language" not in frame.columns or frame["language"].isna().all():
        frame["language"] = "en"
    
    save_manifest(frame, manifest)
    config = load_config(CONFIG_PATH)
    
    # 1. Metadata and Scripts
    metadata_csv = metadata_and_script(manifest, issue_dir, config)
    
    # 2. Narration Audio
    narrate(manifest, issue_dir, config)
    
    # 3. Captions
    try:
        from generate_captions import main as gen_captions_main
        gen_captions_main(issue_dir, config)
    except Exception:
        pass
        
    # 4. Videos with Title Cards
    render_videos(manifest, issue_dir, config)
    
    # 5. Build Comprehensive Excel Workbook
    output_xlsx = drive_paths["data"] / f"Sampada_{state['year']}_{state['month']:02d}_YouTube.xlsx"
    build_excel(metadata_csv, output_xlsx, state["month"], state["year"])
    
    # 6. Copy / Sync all outputs to Google Drive
    copy_outputs(issue_dir, drive_paths)
    
    return {
        "message": "Full automation completed! Audios, videos, SRT captions, and Excel plan generated & synced to Google Drive.",
        "xlsx": str(output_xlsx),
        "stats": status_summary(issue_dir),
        "drive_folder": str(drive_paths["root"])
    }


@app.post("/api/qr")
def qr(payload: LinksPayload) -> dict:
    issue_dir, manifest, _state, drive_paths = issue_context(payload.issue_key)
    path = create_links_template(manifest, issue_dir)
    pd.DataFrame(payload.rows).to_csv(path, index=False, encoding="utf-8-sig")
    created = make_qr_codes(path, issue_dir)
    if created == 0:
        raise HTTPException(400, "Add at least one valid YouTube URL")
    copy_outputs(issue_dir, drive_paths)
    return {"message": f"Created {created} QR code(s)", "stats": status_summary(issue_dir)}

