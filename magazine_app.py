from __future__ import annotations

import calendar
import csv
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
import pdfplumber

from main import (
    create_links_template,
    extract_articles,
    load_config,
    make_qr_codes,
    metadata_and_script,
    narrate,
    render_videos,
)


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"


def safe_segment(value: str) -> str:
    source = Path(value.strip())
    stem = "".join(ch for ch in source.stem if ch.isalnum() or ch in " -_").strip(" .")
    suffix = source.suffix.lower() if source.suffix.lower() in {".pdf", ".xlsx", ".csv"} else ""
    cleaned = f"{stem}{suffix}"
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("Folder name is empty or invalid.")
    return cleaned


def monthly_paths(base: Path, year: int, month: int) -> dict[str, Path]:
    month_name = calendar.month_name[month]
    root = base.resolve() / "Sampada" / str(year) / f"{month:02d} - {month_name}"
    paths = {
        "root": root,
        "audio": root / "Audio",
        "videos": root / "Videos",
        "qr": root / "QR Codes",
        "data": root / "Data",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def detect_qr_pdf_pages(pdf_path: Path, resolution: int = 180) -> list[int]:
    detector = cv2.QRCodeDetector()
    found: list[int] = []
    with pdfplumber.open(pdf_path) as pdf:
        for index, page in enumerate(pdf.pages):
            rgb = np.array(page.to_image(resolution=resolution, antialias=False).original.convert("RGB"))
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            ok, _points = detector.detectMulti(gray)
            if ok:
                found.append(index + 1)
    return found


def select_articles_containing_qr(manifest: Path, qr_pdf_pages: list[int], use_existing_fallback: bool) -> pd.DataFrame:
    frame = pd.read_csv(manifest, encoding="utf-8-sig")
    if qr_pdf_pages:
        frame["selected"] = frame.apply(
            lambda row: "yes" if any(int(row.pdf_start_page) <= page <= int(row.pdf_end_page) for page in qr_pdf_pages) else "no",
            axis=1,
        )
        frame["selection_reason"] = frame.apply(
            lambda row: "QR detected in article pages" if row["selected"] == "yes" else "No QR detected",
            axis=1,
        )
    else:
        if not use_existing_fallback:
            frame["selected"] = "no"
        frame["selection_reason"] = frame["selected"].map(
            {"yes": "Proof fallback / review required", "no": "No QR detected"}
        )
    frame.to_csv(manifest, index=False, encoding="utf-8-sig")
    return frame


def save_manifest(frame: pd.DataFrame, manifest: Path) -> None:
    required = {
        "id", "selected", "printed_start_page", "printed_end_page", "pdf_start_page",
        "pdf_end_page", "language", "title", "article_file",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Missing manifest columns: {', '.join(sorted(missing))}")
    frame.to_csv(manifest, index=False, encoding="utf-8-sig")


def build_excel(metadata_csv: Path, output_xlsx: Path, month: int, year: int) -> None:
    node = Path(r"C:\Users\Aarushi Gupta\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe")
    builder = ROOT / "tools" / "build_excel.mjs"
    subprocess.run(
        [str(node), str(builder), str(metadata_csv), str(output_xlsx), calendar.month_name[month], str(year)],
        cwd=ROOT,
        check=True,
    )


def copy_outputs(work_dir: Path, drive_paths: dict[str, Path]) -> None:
    mappings = (("audio", "audio"), ("videos", "videos"), ("qr_codes", "qr"))
    for local_name, drive_key in mappings:
        source = work_dir / local_name
        if not source.exists():
            continue
        for file in source.iterdir():
            if file.is_file():
                shutil.copy2(file, drive_paths[drive_key] / file.name)


def status_summary(work_dir: Path) -> dict[str, int]:
    def count(name: str, pattern: str) -> int:
        path = work_dir / name
        return len(list(path.glob(pattern))) if path.exists() else 0
    manifest = work_dir / "manifest.csv"
    selected = 0
    if manifest.exists():
        frame = pd.read_csv(manifest, encoding="utf-8-sig")
        selected = int(frame["selected"].astype(str).str.lower().eq("yes").sum())
    return {
        "selected": selected,
        "audio": count("audio", "*.mp3"),
        "videos": count("videos", "*.mp4"),
        "qr": count("qr_codes", "*.png"),
    }
