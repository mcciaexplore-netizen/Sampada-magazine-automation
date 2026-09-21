from __future__ import annotations

import calendar
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from magazine_app import (
    CONFIG_PATH,
    ROOT,
    build_excel,
    copy_outputs,
    detect_qr_pdf_pages,
    monthly_paths,
    safe_segment,
    save_manifest,
    select_articles_containing_qr,
    status_summary,
)
from main import create_links_template, extract_articles, load_config, make_qr_codes, metadata_and_script, narrate, render_videos


st.set_page_config(page_title="Sampada media studio", page_icon=":material/auto_awesome_motion:", layout="wide")
st.session_state.setdefault("issue_dir", None)
st.session_state.setdefault("pdf_path", None)
st.session_state.setdefault("manifest_path", None)
st.session_state.setdefault("qr_pages", [])


@st.cache_data(show_spinner=False, max_entries=12)
def cached_qr_scan(pdf_path: str, modified_ns: int) -> list[int]:
    del modified_ns
    return detect_qr_pdf_pages(Path(pdf_path))

st.title("Sampada media studio")
st.caption("Upload a monthly edition, review QR-enabled articles, and generate the Excel sheet, narration, videos and QR codes.")

with st.sidebar:
    st.header("Edition settings")
    today = datetime.now()
    year = st.number_input("Year", min_value=2000, max_value=2100, value=today.year, step=1)
    month_name = st.selectbox("Month", list(calendar.month_name)[1:], index=today.month - 1)
    month = list(calendar.month_name).index(month_name)
    drive_root_text = st.text_input(
        "Google Drive synced folder",
        value=str(ROOT / "drive_demo"),
        help="Choose a folder that Google Drive for desktop already synchronizes.",
    )
    st.caption("The app creates Sampada / Year / Month / Audio, Videos, QR Codes and Data.")

upload = st.file_uploader("Upload the Sampada PDF", type=["pdf"], accept_multiple_files=False)
analyze = st.button("Analyze edition", type="primary", icon=":material/document_search:", disabled=upload is None)

if analyze and upload is not None:
    try:
        drive_root = Path(drive_root_text).expanduser().resolve()
        drive_root.mkdir(parents=True, exist_ok=True)
        drive_paths = monthly_paths(drive_root, int(year), month)
        issue_dir = ROOT / "work" / f"{int(year)}-{month:02d}"
        issue_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = issue_dir / safe_segment(upload.name)
        pdf_path.write_bytes(upload.getvalue())
        config = load_config(CONFIG_PATH)
        is_august_sample = int(year) == 2026 and month == 8
        if not is_august_sample:
            config = {**config, "include_full_magazine": False, "selected_pages": [], "article_titles": {}}
        with st.status("Analyzing the magazine", expanded=True) as status:
            st.write("Extracting contents and article page ranges")
            manifest = extract_articles(pdf_path, issue_dir, config)
            st.write("Scanning rendered pages for QR symbols")
            qr_pages = cached_qr_scan(str(pdf_path), pdf_path.stat().st_mtime_ns)
            select_articles_containing_qr(manifest, qr_pages, use_existing_fallback=is_august_sample)
            status.update(label="Edition ready for review", state="complete", expanded=False)
        st.session_state.issue_dir = str(issue_dir)
        st.session_state.pdf_path = str(pdf_path)
        st.session_state.manifest_path = str(manifest)
        st.session_state.qr_pages = qr_pages
        st.session_state.drive_paths = {key: str(value) for key, value in drive_paths.items()}
    except Exception as exc:
        st.error(f"Analysis failed: {exc}", icon=":material/error:")

if st.session_state.manifest_path:
    issue_dir = Path(st.session_state.issue_dir)
    manifest = Path(st.session_state.manifest_path)
    config = load_config(CONFIG_PATH)
    frame = pd.read_csv(manifest, encoding="utf-8-sig")
    counts = status_summary(issue_dir)
    has_metadata = (issue_dir / "youtube_metadata.csv").exists()
    has_audio = counts["audio"] >= counts["selected"] > 0
    metrics = st.columns(4)
    metrics[0].metric("Selected articles", counts["selected"])
    metrics[1].metric("Audio files", counts["audio"])
    metrics[2].metric("Videos", counts["videos"])
    metrics[3].metric("QR codes", counts["qr"])

    if st.session_state.qr_pages:
        st.success(f"QR symbols detected on PDF pages: {', '.join(map(str, st.session_state.qr_pages))}", icon=":material/qr_code_2:")
    else:
        st.warning("No embedded QR symbols were found. This appears to be a proof PDF; review the preselected titles before continuing.", icon=":material/rate_review:")

    st.subheader("Review QR-enabled titles")
    display_columns = ["selected", "title", "language", "printed_start_page", "printed_end_page", "selection_reason"]
    review = frame[display_columns].copy()
    review["selected"] = review["selected"].astype(str).str.lower().eq("yes")
    edited = st.data_editor(
        review,
        key="article_editor",
        hide_index=True,
        disabled=["printed_start_page", "printed_end_page", "selection_reason"],
        column_config={
            "selected": st.column_config.CheckboxColumn("Create media"),
            "title": st.column_config.TextColumn("Title", pinned=True, width="large"),
            "language": st.column_config.SelectboxColumn("Language", options=["en", "mr", "hi"]),
            "printed_start_page": st.column_config.NumberColumn("Start page", format="%d"),
            "printed_end_page": st.column_config.NumberColumn("End page", format="%d"),
        },
    )

    with st.container(horizontal=True):
        save_review = st.button("Save review and create Excel", type="primary", icon=":material/table_view:", disabled=not edited["selected"].any())
        create_audio = st.button("Create audio", icon=":material/graphic_eq:", disabled=not has_metadata, help=None if has_metadata else "Create the Excel and scripts first.")
        create_video = st.button("Create videos", icon=":material/movie:", disabled=not has_audio, help=None if has_audio else "Create all audio files first.")

    if save_review:
        try:
            frame.loc[:, "selected"] = edited["selected"].map({True: "yes", False: "no"})
            frame.loc[:, "title"] = edited["title"].astype(str).str.strip()
            frame.loc[:, "language"] = edited["language"]
            if frame.loc[frame["selected"] == "yes", "title"].eq("").any():
                raise ValueError("Every selected article must have a title.")
            save_manifest(frame, manifest)
            metadata_csv = metadata_and_script(manifest, issue_dir, config)
            drive_paths = {key: Path(value) for key, value in st.session_state.drive_paths.items()}
            xlsx_path = drive_paths["data"] / f"Sampada_{int(year)}_{month:02d}_YouTube.xlsx"
            build_excel(metadata_csv, xlsx_path, month, int(year))
            st.session_state.xlsx_path = str(xlsx_path)
            st.success(f"Excel created in {xlsx_path}", icon=":material/check_circle:")
            st.rerun()
        except Exception as exc:
            st.error(f"Could not create the Excel file: {exc}", icon=":material/error:")

    if create_audio:
        try:
            with st.status("Generating multilingual narration", expanded=True) as status:
                narrate(manifest, issue_dir, config)
                copy_outputs(issue_dir, {key: Path(value) for key, value in st.session_state.drive_paths.items()})
                status.update(label="Audio complete", state="complete", expanded=False)
            st.rerun()
        except Exception as exc:
            st.error(f"Audio generation failed: {exc}", icon=":material/error:")

    if create_video:
        try:
            with st.status("Rendering 1080p videos", expanded=True) as status:
                render_videos(manifest, issue_dir, config)
                copy_outputs(issue_dir, {key: Path(value) for key, value in st.session_state.drive_paths.items()})
                status.update(label="Videos complete", state="complete", expanded=False)
            st.rerun()
        except Exception as exc:
            st.error(f"Video rendering failed: {exc}", icon=":material/error:")

    st.subheader("YouTube links and QR codes")
    links_path = create_links_template(manifest, issue_dir)
    links = pd.read_csv(links_path, encoding="utf-8-sig")
    edited_links = st.data_editor(
        links,
        key="youtube_editor",
        hide_index=True,
        disabled=["id", "title"],
        column_config={"youtube_url": st.column_config.LinkColumn("YouTube URL", width="large")},
    )
    if st.button("Save links and create QR codes", icon=":material/qr_code_2:"):
        try:
            edited_links.to_csv(links_path, index=False, encoding="utf-8-sig")
            created = make_qr_codes(links_path, issue_dir)
            if created == 0:
                raise ValueError("Add at least one valid YouTube URL before creating QR codes.")
            copy_outputs(issue_dir, {key: Path(value) for key, value in st.session_state.drive_paths.items()})
            st.success(f"Created {created} QR code(s) and copied them to the monthly Drive folder.", icon=":material/check_circle:")
        except Exception as exc:
            st.error(f"QR generation failed: {exc}", icon=":material/error:")

    if st.session_state.get("xlsx_path"):
        xlsx_path = Path(st.session_state.xlsx_path)
        if xlsx_path.exists():
            st.download_button(
                "Download Excel",
                data=xlsx_path.read_bytes(),
                file_name=xlsx_path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                icon=":material/download:",
            )
