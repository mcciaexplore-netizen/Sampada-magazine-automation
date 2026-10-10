from __future__ import annotations

import calendar

import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pdfplumber
from PIL import Image, ImageDraw, ImageFont

# Expose backend FastAPI app for deployment servers looking at main.py
try:
    from backend_api import app  # noqa: F401
except ImportError:
    app = None


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
STOPWORDS = {
    "about", "after", "again", "against", "also", "and", "are", "because",
    "been", "before", "being", "between", "both", "business", "but", "can",
    "could", "does", "every", "food", "from", "have", "has", "had", "his", "her",
    "into", "its", "more", "most", "not", "now", "only", "other", "our", "over",
    "such", "than", "that", "the", "their", "them", "there", "these", "they",
    "this", "through", "under", "very", "was", "were", "when", "where", "which",
    "while", "with", "would", "you", "your", "will", "may", "how", "what", "who",
    "why", "all", "each", "out", "per", "article", "sampada",
    # Marathi stopwords
    "आहे", "आणि", "आहेत", "या", "च्या", "केले", "होते", "करणे", "एक", "तर", "नाही",
    "काही", "त्या", "त्ामुळये", "मध्ये", "झाले", "येथे", "असे", "दिले", "होती"
}


def media_slug(title: str, fallback: str = "Article") -> str:
    clean = re.sub(r"['’]s\b", "", title, flags=re.I)
    clean = re.sub(r"['’]", "", clean)
    clean = re.sub(r"[–—\-:;,/|#@!%&*+]+", " ", clean)
    parts = re.findall(r"[A-Za-z0-9]+|[\u0900-\u097f]+", clean)
    if not parts:
        return fallback
    slug_parts = [p.capitalize() if p.isascii() else p for p in parts]
    return "-".join(slug_parts)


def slugify(value: str, fallback: str = "article") -> str:
    ascii_part = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if ascii_part:
        return ascii_part[:72]
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"{fallback}-{digest}"


def clean_text(value: str) -> str:
    value = value.replace("\u00ad", "").replace("\x00", " ")
    value = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", " ", value)
    value = re.sub(r"(?<=\w)-\n(?=\w)", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def issue_overrides(config: dict, issue_key: str) -> dict:
    """Per-issue manual overrides (titles, video titles, categories), keyed by YYYY-MM."""
    return config.get("issue_overrides", {}).get(issue_key, {})


def full_magazine_title(issue_key: str) -> str:
    try:
        year, month = (int(part) for part in issue_key.split("-")[:2])
        return f"{calendar.month_name[month]} {year} Issue Summary"
    except (ValueError, IndexError):
        return "Issue Summary"


_STOP = set("the a an and or of to in on for with is are was were be by as at it its this that from has have had will not but their they which who into more also can than".split())


def summarize_article(text: str, count: int = 2) -> str:
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text))]
    sentences = [x for x in sentences if 50 <= len(x) <= 320 and sum(c.isdigit() for c in x) < 12 and "|" not in x and "@" not in x]
    if len(sentences) <= count:
        return " ".join(sentences)
    freq = Counter(w for x in sentences for w in re.findall(r"[a-z']{3,}", x.lower()) if w not in _STOP)
    def score(i: int) -> float:
        words = [w for w in re.findall(r"[a-z']{3,}", sentences[i].lower()) if w not in _STOP]
        return sum(freq[w] for w in words) / (len(words) ** 0.5 or 1) + (3 if i == 0 else 0)
    best = sorted(sorted(range(len(sentences)), key=score, reverse=True)[:count])
    return " ".join(sentences[i] for i in best)


def build_issue_summary(issue_key: str, articles: list[tuple[str, str]]) -> str:
    try:
        year, month = (int(part) for part in issue_key.split("-")[:2])
        label = f"{calendar.month_name[month]} {year}"
    except (ValueError, IndexError):
        label = "this month"
    parts = [f"Welcome to the {label} issue of Sampada, from MCCIA. Here is a summary of this month's edition."]
    for title, body in articles:
        summary = summarize_article(body)
        if summary and not title.startswith("Article starting"):
            parts.append(f"{title.rstrip('.')}. {summary}")
    parts.append("That was the Sampada summary. Read the full articles in the magazine.")
    return "\n\n".join(parts)


def detect_language(text: str) -> str:
    devanagari = len(re.findall(r"[\u0900-\u097f]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return "mr" if devanagari > max(20, latin * 0.20) else "en"


def page_number_to_pdf_index(printed_page: int, offset: int) -> int:
    return max(0, printed_page - offset - 1)


def parse_contents(text: str) -> list[tuple[str, int]]:
    """Parse dot-leader entries from an InDesign contents page.

    Wrapped entries are accumulated until a line ending in a page number is found.
    Author-only lines and obvious section labels are removed later.
    """
    entries: list[tuple[str, int]] = []
    pending = ""
    for raw in text.splitlines():
        line = re.sub(r"^[l\u2022\uf0b7]\s*", "", raw).strip()
        if not line:
            continue
        pending = f"{pending} {line}".strip()
        match = re.search(r"\.{2,}\s*(\d{1,3})\s*$", pending)
        if not match:
            continue
        title = re.sub(r"\.{2,}\s*\d{1,3}\s*$", "", pending).strip(" -\t")
        title = re.sub(r"\s+", " ", title)
        page = int(match.group(1))
        pending = ""
        if len(title) < 5 or title.isupper():
            continue
        entries.append((title, page))
    return entries


def contents_page_numbers(page: pdfplumber.page.Page) -> list[int]:
    """Read article start pages from dot-leader tokens without mixing columns."""
    words = page.extract_words()
    found: set[int] = set()
    for index, word in enumerate(words):
        token = word["text"]
        match = re.search(r"\.{2,}\s*(\d{1,3})$", token)
        if match:
            found.add(int(match.group(1)))
            continue
        if re.fullmatch(r"\.{2,}", token):
            # In the Marathi entry the page number is a separate word slightly above.
            for candidate in words[max(0, index - 5): index + 6]:
                if re.fullmatch(r"\d{1,3}", candidate["text"]):
                    if abs(float(candidate["top"]) - float(word["top"])) < 9:
                        found.add(int(candidate["text"]))
    return sorted(number for number in found if 2 <= number <= 200)


def title_words(page: pdfplumber.page.Page) -> list[dict]:
    """Words that make up the headline of an article start page (largest real text, drop caps ignored)."""
    all_words = [w for w in page.extract_words(extra_attrs=["size"]) if float(w["top"]) < page.height * 0.8]
    real = [w for w in all_words if len(w["text"]) > 1]
    if not real:
        return []
    body_size = Counter(round(float(w["size"]), 1) for w in real).most_common(1)[0][0]
    for size in sorted({round(float(w["size"]), 1) for w in real}, reverse=True):
        if size <= body_size * 1.3:
            break
        group = [w for w in all_words if abs(float(w["size"]) - size) < 0.6]
        tokens = [w["text"] for w in group]
        if len(group) < 2 and len(tokens[0]) < 6:
            continue
        if sum(len(t) == 1 and t.isalpha() for t in tokens) > len(tokens) * 0.5:
            continue
        return group
    return []


def title_from_page(page: pdfplumber.page.Page, fallback: str) -> str:
    group = title_words(page)
    if not group:
        return fallback
    group.sort(key=lambda w: (round(float(w["top"]) / 6), float(w["x0"])))
    candidate = re.sub(r"\s+", " ", " ".join(w["text"] for w in group)).strip()
    return candidate if 4 <= len(candidate) <= 180 else fallback


QR_PLACEHOLDER = re.compile(r"scan\s+the\s+qr", re.I)
QR_NOISE = re.compile(r"scan\s+the\s+qr(?:\s+code)?(?:\s+to)?|listen\s+to\s+the\s+gist\s+of\s+the(?:\s+article)?", re.I)


def strip_qr_noise(text: str) -> str:
    return re.sub(r"[ 	]{2,}", " ", QR_NOISE.sub("", text))


_TEXT_CACHE: dict[tuple[str, int], list[str]] = {}


def pdf_page_texts(pdf_path: Path) -> list[str]:
    """Plain text of every page (pdfium is ~50x faster than pdfminer). Cached per file version."""
    import pypdfium2 as pdfium
    key = (str(pdf_path), Path(pdf_path).stat().st_mtime_ns)
    if key not in _TEXT_CACHE:
        document = pdfium.PdfDocument(str(pdf_path))
        try:
            _TEXT_CACHE[key] = [(document[i].get_textpage().get_text_range() or "").replace(chr(13) + chr(10), chr(10)) for i in range(len(document))]
        finally:
            document.close()
    return _TEXT_CACHE[key]


def find_qr_placeholder_pages(texts: list[str]) -> list[int]:
    """1-based PDF pages carrying the "Scan the QR code to listen..." box (page 1 is the contents note)."""
    found = []
    for index, text in enumerate(texts):
        if index and QR_PLACEHOLDER.search(text):
            found.append(index + 1)
    return found


def infer_offset(texts: list[str]) -> int:
    offsets: list[int] = []
    for index, text in enumerate(texts):
        matches = re.findall(r"(?:^|\n)\s*(\d{1,3})\s*\|\s*SAMPADA", text, re.I)
        matches += re.findall(r"SAMPADA\s*\|\s*(?:[A-Za-z]+\s+\d{4}\s*\|\s*)?(\d{1,3})", text, re.I)
        for value in matches:
            offset = int(value) - (index + 1)
            if 0 <= offset <= 10:
                offsets.append(offset)
    if offsets:
        return Counter(offsets).most_common(1)[0][0]
    return 2


def extract_articles(pdf_path: Path, output_dir: Path, config: dict, min_page: int = 2) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    articles_dir = output_dir / "articles"
    if articles_dir.exists():
        shutil.rmtree(articles_dir)
    articles_dir.mkdir(exist_ok=True)
    with pdfplumber.open(pdf_path) as pdf:
        page_numbers = contents_page_numbers(pdf.pages[0])
        entries = [(f"Article starting on page {number}", number) for number in page_numbers]
        texts = pdf_page_texts(pdf_path)
        offset = infer_offset(texts)
        valid = [(title, page) for title, page in entries if min_page <= page <= len(pdf.pages) + offset]
        if not valid:
            raise RuntimeError("No contents entries found. Export a selectable-text PDF or edit manifest.csv manually.")

        rows = []
        all_page_text = [text for text in (strip_qr_noise(clean_text(t)) for t in texts) if text]
        full_row = None
        overrides = issue_overrides(config, output_dir.name)
        title_overrides = {int(key): value for key, value in overrides.get("article_titles", {}).items()}
        for i, (toc_title, printed_start) in enumerate(valid):
            next_printed = valid[i + 1][1] if i + 1 < len(valid) else len(pdf.pages) + offset + 1
            start_idx = page_number_to_pdf_index(printed_start, offset)
            end_idx = min(len(pdf.pages) - 1, page_number_to_pdf_index(next_printed, offset) - 1)
            if end_idx < start_idx:
                end_idx = start_idx
            page_texts = [strip_qr_noise(clean_text(texts[p])) for p in range(start_idx, end_idx + 1)]
            body = "\n\n".join(text for text in page_texts if text)
            title = title_overrides.get(printed_start, title_from_page(pdf.pages[start_idx], toc_title))
            slug = slugify(title)
            article_file = articles_dir / f"{printed_start:03d}-{slug}.txt"
            article_file.write_text(body, encoding="utf-8")
            rows.append({
                "id": f"p{printed_start:03d}",
                "selected": "yes",
                "printed_start_page": printed_start,
                "printed_end_page": max(printed_start, next_printed - 1),
                "pdf_start_page": start_idx + 1,
                "pdf_end_page": end_idx + 1,
                "language": detect_language(body),
                "title": title,
                "article_file": str(article_file.relative_to(output_dir)),
            })

    if config.get("include_full_magazine", False):
        summaries = [(row["title"], (output_dir / row["article_file"]).read_text(encoding="utf-8"))
                     for row in rows if row["language"] == "en"]
        full_file = articles_dir / "000-issue-summary.txt"
        full_file.write_text(build_issue_summary(output_dir.name, summaries), encoding="utf-8")
        rows.insert(0, {
            "id": "full", "selected": "no", "printed_start_page": 1,
            "printed_end_page": len(pdf.pages) + offset, "pdf_start_page": 1,
            "pdf_end_page": len(pdf.pages), "language": "en",
            "title": full_magazine_title(output_dir.name), "article_file": str(full_file.relative_to(output_dir)),
        })

    manifest = output_dir / "manifest.csv"
    with manifest.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (output_dir / "extraction.json").write_text(
        json.dumps({"source_pdf": str(pdf_path), "page_offset": offset, "article_count": len(rows)}, indent=2),
        encoding="utf-8",
    )
    return manifest


def iter_manifest(manifest: Path) -> Iterable[dict]:
    with manifest.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("id", "").strip().lower() == "full":
                continue
            if row.get("selected", "yes").strip().lower() in {"yes", "y", "1", "true"}:
                yield row


def keywords_for(text: str, title: str, limit: int = 25) -> list[str]:
    latin = re.findall(r"[A-Za-z][A-Za-z'-]{3,}", f"{title} {title} {text}")
    dev = re.findall(r"[\u0900-\u097f]{3,}", f"{title} {title} {text}")
    tokens = [token.strip("'-").lower() for token in latin + dev]
    counts = Counter(token for token in tokens if token not in STOPWORDS and not token.isdigit() and len(token) > 3)
    return [word for word, _ in counts.most_common(limit)]


LEAD_NOISE = ("| sampada", "sampada |", "@gmail.com", "@mcciapune.com", "shutterstock", "adobe stock", "moc.kcot", "kcotsrettuhs")
LEAD_LABELS = {"cover story", "analysis", "conversations", "discoveries", "briefings", "etcetera", "innovation", "advocacy", "roundtable", "materials"}


def extract_clean_lead(body: str, title: str) -> str:
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    for p in paragraphs:
        lines = [l.strip() for l in p.split("\n") if l.strip()]
        valid_lines = []
        for l in lines:
            ll = l.lower()
            if any(noise in ll for noise in LEAD_NOISE) or re.search(r"scan\s+the\s+qr", ll):
                continue
            if len(l) < 25 and (l.isupper() or ll in LEAD_LABELS):
                continue
            valid_lines.append(l)
        clean_p = " ".join(valid_lines).strip()
        if len(clean_p) > 90:
            sentences = [s.strip() for s in re.split(r"(?<=[.!?।])\s+", clean_p) if s.strip()]
            if sentences:
                lead = " ".join(sentences[:2]) if len(sentences) >= 2 else clean_p
                return lead[:420]
    return title


def metadata_and_script(manifest: Path, work_dir: Path, config: dict) -> Path:
    metadata_dir = work_dir / "metadata"
    scripts_dir = work_dir / "scripts"
    metadata_dir.mkdir(exist_ok=True)
    scripts_dir.mkdir(exist_ok=True)
    output_rows = []
    for row in iter_manifest(manifest):
        article_path = work_dir / row["article_file"]
        raw_article = strip_qr_noise(article_path.read_text(encoding="utf-8"))
        body = clean_text(raw_article)
        title = row["title"].strip()
        paragraphs = [p.strip() for p in body.split("\n\n") if len(p.strip()) > 60]
        narration = "\n\n".join(paragraphs)
        if narration.lower().startswith(title.lower()):
            narration = narration[len(title):].lstrip(" :\n")
        english_override = config.get("english_narration_overrides", {}).get(row["id"])
        intro = f"{title}.\n\n"
        outro = f"\n\nThis article is from {config['magazine_name']}, published by {config['publisher']}."
        script = (english_override.strip() if english_override else intro + narration) + outro
        keywords = keywords_for(body, title, limit=20)
        hashtags = [f"#{config['magazine_name']}", f"#{config['publisher']}"]
        for k in keywords:
            tag = f"#{slugify(k).replace('-', '')}"
            if tag.lower() not in [h.lower() for h in hashtags] and len(tag) > 3 and not tag[1:].isdigit():
                hashtags.append(tag)
            if len(hashtags) >= 9:
                break

        lead = extract_clean_lead(raw_article, title)

        is_marathi = row["language"] == "mr"
        if is_marathi:
            hook = f"🎧 सविस्तर माहिती आणि धोरणात्मक दृष्टिकोन जाणून घेण्यासाठी संपूर्ण ऑडिओ नक्की ऐका.\n\n📖 वाचा संपदा मासिक ({config['publisher']})."
        else:
            hook = f"🎧 Tune in to discover the key insights, strategies, and growth opportunities shaping the future of this sector.\n\n📖 Read in {config['magazine_name']} by {config['publisher']}."

        description = f"{lead}\n\n{hook}\n\n" + " ".join(hashtags)
        mslug = media_slug(title)
        script_path = scripts_dir / f"{mslug}.txt"
        meta_path = metadata_dir / f"{mslug}.json"
        if not script_path.exists() or script_path.read_text(encoding="utf-8") != script:
            script_path.write_text(script, encoding="utf-8")
        meta = {"id": row["id"], "title": title[:100], "description": description, "keywords": keywords, "language": row["language"], "script_file": str(script_path.relative_to(work_dir))}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        output_rows.append({
            "id": row["id"], "title": title[:100],
            "audio_file": f"{mslug}.mp3",
            "video_file": f"{mslug}.mp4",
            "youtube_url": "", "description": description,
            "keyword": " ".join(word.title() for word in keywords[:2]),
            "language": row["language"], "keywords": ", ".join(keywords),
            "script_file": str(script_path.relative_to(work_dir)),
        })
    review = work_dir / "youtube_metadata.csv"
    with review.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(output_rows[0]))
        writer.writeheader(); writer.writerows(output_rows)
    return review


async def synthesize_one(text: str, voice: str, output: Path, rate: str = "+0%") -> list:
    """Synthesize one chunk and return its subtitle cues from the same request."""
    import edge_tts
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            output.unlink(missing_ok=True)
            submaker = edge_tts.SubMaker()
            with output.open("wb") as stream:
                async for message in edge_tts.Communicate(text, voice, rate=rate).stream():
                    if message["type"] == "audio":
                        stream.write(message["data"])
                    elif message["type"] in ("SentenceBoundary", "WordBoundary"):
                        submaker.feed(message)
            if output.exists() and output.stat().st_size > 1024:
                return list(submaker.cues)
        except Exception as error:
            last_error = error
        if attempt < 2:
            await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Voice generation failed after 3 attempts: {last_error}")


async def synthesize_chunks(chunks: list[str], voice: str, rate: str, chunk_files: list[Path], limit: int = 4) -> list[list]:
    gate = asyncio.Semaphore(limit)

    async def one(chunk: str, path: Path) -> list:
        async with gate:
            return await synthesize_one(chunk, voice, path, rate)

    return list(await asyncio.gather(*(one(c, p) for c, p in zip(chunks, chunk_files))))


def cues_to_srt(chunk_cues: list[list]) -> str:
    def stamp(seconds: float) -> str:
        total_ms = int(seconds * 1000)
        return f"{total_ms // 3600000:02d}:{total_ms // 60000 % 60:02d}:{total_ms // 1000 % 60:02d},{total_ms % 1000:03d}"

    lines, index, offset = [], 1, 0.0
    for cues in chunk_cues:
        if not cues:
            continue
        for cue in cues:
            start, end = cue.start.total_seconds() + offset, cue.end.total_seconds() + offset
            lines += [str(index), f"{stamp(start)} --> {stamp(end)}", cue.content, ""]
            index += 1
        offset += cues[-1].end.total_seconds() + 0.1
    return "\n".join(lines)


def split_narration(text: str, limit: int = 4500) -> list[str]:
    """Pack whole sentences into chunks of at most `limit` characters (long sentences are word-wrapped)."""
    sentences: list[str] = []
    for paragraph in (part.strip() for part in text.split("\n\n") if part.strip()):
        for sentence in re.split(r"(?<=[.!?\u0964])\s+", paragraph):
            sentences.extend(textwrap.wrap(sentence, width=limit, break_long_words=False, break_on_hyphens=False) or [sentence])
        sentences[-1] += "\n\n"
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}" if current and not current.endswith("\n\n") else f"{current}{sentence}"
        if current and len(candidate.strip()) > limit:
            chunks.append(current.strip())
            current = sentence
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _narrate_row(row: dict, work_dir: Path, config: dict, audio_dir: Path) -> Path:
    mslug = media_slug(row["title"])
    script_path = work_dir / "scripts" / f"{mslug}.txt"
    if not script_path.exists():
        candidates = list((work_dir / "scripts").glob(f"*{row['id']}*.txt")) + list((work_dir / "scripts").glob(f"*{mslug}*.txt"))
        if candidates:
            script_path = candidates[0]
        else:
            raise FileNotFoundError(f"Run metadata first: {script_path}")
    output = audio_dir / f"{mslug}.mp3"
    reuse = config.get("performance", {}).get("reuse_completed_files", True)
    srt = work_dir / "captions" / f"{mslug}.srt"
    if reuse and srt.exists() and output.exists() and output.stat().st_size > 4096 and output.stat().st_mtime >= script_path.stat().st_mtime:
        return output
    script_text = script_path.read_text(encoding="utf-8")
    script_lang = detect_language(script_text)
    voice = config.get("voices", {}).get(script_lang) or config.get("voices", {}).get(row.get("language", "en")) or config.get("voices", {}).get(config.get("audio_language", "en"), "en-IN-NeerjaNeural")
    rate = config.get("speech_rate", "+0%")
    chunks = split_narration(script_text)
    chunk_dir = audio_dir / ".chunks" / row["id"]
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk_files = [chunk_dir / f"{index:04d}.mp3" for index in range(len(chunks))]
    chunk_cues = asyncio.run(synthesize_chunks(chunks, voice, rate, chunk_files))
    captions_dir = work_dir / "captions"
    captions_dir.mkdir(exist_ok=True)
    (captions_dir / f"{mslug}.srt").write_text(cues_to_srt(chunk_cues), encoding="utf-8")
    if len(chunk_files) == 1:
        shutil.copyfile(chunk_files[0], output)
    else:
        ffmpeg = ffmpeg_executable()
        inputs = [item for path in chunk_files for item in ("-i", str(path))]
        labels = "".join(f"[{index}:a]" for index in range(len(chunk_files)))
        subprocess.run(
            [ffmpeg, "-y", *inputs, "-filter_complex", f"{labels}concat=n={len(chunk_files)}:v=0:a=1[out]", "-map", "[out]", "-b:a", "192k", str(output)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    shutil.rmtree(chunk_dir)
    return output


def narrate(manifest: Path, work_dir: Path, config: dict) -> None:
    audio_dir = work_dir / "audio"
    audio_dir.mkdir(exist_ok=True)
    rows = list(iter_manifest(manifest))
    workers = max(1, min(int(config.get("performance", {}).get("audio_workers", 4)), len(rows)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sampada-audio") as executor:
        futures = [executor.submit(_narrate_row, row, work_dir, config, audio_dir) for row in rows]
        for future in as_completed(futures):
            future.result()


def find_font(size: int, prefer_bold: bool = False, is_devanagari: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if is_devanagari:
        candidates = [
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "Nirmala.ttc",
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "NirmalaB.ttf",
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "Nirmala.ttf",
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
        ]
    elif prefer_bold:
        candidates = [
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arialbd.ttf",
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "NirmalaB.ttf",
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
        ]
    else:
        candidates = [
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "Nirmala.ttc",
        ]
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except Exception:
                continue
    return ImageFont.load_default()


def crop_title_from_pdf(pdf_path: Path, pdf_page: int) -> Image.Image | None:
    """Render the headline of an article start page as an image (needed for Devanagari, which PIL cannot shape)."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[pdf_page - 1]
            group = title_words(page)
            if not group:
                return None
            x0 = max(0, min(float(w["x0"]) for w in group) - 4)
            x1 = min(float(page.width), max(float(w["x1"]) for w in group) + 4)
            y0 = max(0, min(float(w["top"]) for w in group))
            y1 = min(float(page.height), max(float(w["bottom"]) for w in group))
            crop = page.crop((x0, y0, x1, y1)).to_image(resolution=300).original.convert("RGB")
    except Exception:
        return None
    return crop


def paste_pdf_title(image: Image.Image, crop: Image.Image, width: int, top: int, bottom: int, scale: float) -> None:
    gray = crop.convert("L")
    dark_text = gray.resize((1, 1)).getpixel((0, 0)) > 110
    mask_src = gray.point(lambda p: 255 if p < 150 else 0) if dark_text else gray.point(lambda p: 255 if p > 150 else 0)
    bbox = mask_src.getbbox()
    if bbox:
        crop, mask_src = crop.crop(bbox), mask_src.crop(bbox)
    target_w = min(int(width * 0.84), int(crop.width * 3))
    ratio = target_w / crop.width
    target_h = int(crop.height * ratio)
    max_h = bottom - top - int(60 * scale)
    if target_h > max_h:
        ratio = max_h / crop.height
        target_w, target_h = int(crop.width * ratio), max_h
    mask = mask_src.resize((target_w, target_h), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(image)
    box_w, box_h = target_w + int(40 * scale), target_h + int(22 * scale)
    gx, gy = (width - box_w) // 2, (top + bottom - box_h) // 2
    draw.rectangle((gx, gy, gx + box_w, gy + box_h), fill="#159447")
    image.paste(Image.new("RGB", (target_w, target_h), (255, 255, 255)), ((width - target_w) // 2, (top + bottom - target_h) // 2), mask=mask)


def title_card(title: str, subtitle: str, output: Path, config: dict, article_id: str = "", category: str = "", pdf_path: Path | None = None, pdf_page: int = 0) -> None:
    width, height = config["video"]["width"], config["video"]["height"]
    template_value = config.get("video", {}).get("template", "")
    template_path = ROOT / template_value if template_value else None
    if template_path and template_path.exists():
        image = Image.open(template_path).convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(image)
        scale = width / 1280
        top, bottom, split = int(286 * scale), int(447 * scale), int(430 * scale)

        # 1. Clear default middle banner area
        draw.rectangle((0, top, split, bottom), fill="#0E5F9F")
        draw.rectangle((split, top, width, bottom), fill="#F5F3EF")
        grid = "#DDDED9"
        for x in (549, 695, 842, 988, 1135):
            draw.line((int(x * scale), top, int(x * scale), bottom), fill=grid, width=max(1, int(2 * scale)))
        draw.line((split, int(293 * scale), width, int(293 * scale)), fill=grid, width=max(1, int(2 * scale)))
        draw.line((split, int(440 * scale), width, int(440 * scale)), fill=grid, width=max(1, int(2 * scale)))

        # 2. Clear old static "Export Success" template badge
        draw.rectangle((int(700 * scale), int(30 * scale), int(1220 * scale), int(240 * scale)), fill="#F5F3EF")
        for x in (842, 988, 1135):
            draw.line((int(x * scale), int(30 * scale), int(x * scale), int(240 * scale)), fill=grid, width=max(1, int(2 * scale)))
        draw.line((int(700 * scale), int(146 * scale), int(1220 * scale), int(146 * scale)), fill=grid, width=max(1, int(2 * scale)))

        # 3. Optional category badge (only when configured for this issue)
        if category:
            b_font = find_font(max(20, int(34 * scale)), prefer_bold=True, is_devanagari=any(ord(c) > 128 for c in category))
            box = draw.textbbox((0, 0), category, font=b_font)
            bw, bh = box[2] - box[0], box[3] - box[1]
            bx, by = int(1050 * scale) - bw // 2, int(110 * scale)
            pad_x, pad_y = int(22 * scale), int(10 * scale)
            draw.rounded_rectangle((bx - pad_x, by - pad_y, bx + bw + pad_x, by + bh + pad_y + 4), radius=12, fill="#E8F5E9", outline="#0D7038", width=max(1, int(2 * scale)))
            draw.text((bx, by), category, font=b_font, fill="#0D7038")

        # 4. Title: Devanagari headlines are cropped from the magazine page itself
        if re.search(r"[ऀ-ॿ]", title) and pdf_path and pdf_path.exists() and pdf_page:
            crop = crop_title_from_pdf(pdf_path, pdf_page)
            if crop is not None:
                paste_pdf_title(image, crop, width, top, bottom, scale)
                output.parent.mkdir(exist_ok=True)
                image.save(output, quality=95)
                return

        font = find_font(max(24, int(39 * scale)), prefer_bold=True, is_devanagari=(detect_language(title) == "mr"))
        words = title.split()
        lines: list[str] = []
        current = ""
        max_text_width = int(width * 0.88)
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_text_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        lines = lines[:3]
        line_height = max(42, int(55 * scale))
        y = int((top + bottom - line_height * len(lines)) / 2)
        for line in lines:
            box = draw.textbbox((0, 0), line, font=font)
            text_width = box[2] - box[0]
            x = (width - text_width) // 2
            pad_x, pad_y = int(16 * scale), int(7 * scale)
            draw.rectangle((x - pad_x, y - pad_y, x + text_width + pad_x, y + line_height - pad_y), fill="#159447")
            draw.text((x, y), line, font=font, fill="white")
            y += line_height
        output.parent.mkdir(exist_ok=True)
        image.save(output, quality=95)
        return

    image = Image.new("RGB", (width, height), config["video"]["background"])
    draw = ImageDraw.Draw(image)
    accent = config["video"]["accent"]
    draw.rectangle((110, 110, 128, height - 110), fill=accent)
    title_font = find_font(72, prefer_bold=True)
    small_font = find_font(34)
    max_chars = 35 if detect_language(title) == "mr" else 30
    lines = textwrap.wrap(title, width=max_chars)[:6]
    y = 230
    for line in lines:
        draw.text((190, y), line, font=title_font, fill="white")
        y += 98
    draw.text((190, height - 185), subtitle, font=small_font, fill=accent)
    output.parent.mkdir(exist_ok=True)
    image.save(output, quality=95)


def ffmpeg_executable() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _render_video_row(row: dict, work_dir: Path, config: dict, cards: Path, videos: Path, ffmpeg: str, pdf_path: Path | None = None) -> Path:
    mslug = media_slug(row["title"])
    card = cards / f"{mslug}.jpg"
    audio_dir = work_dir / "audio"
    audio = audio_dir / f"{mslug}.mp3"
    if not audio.exists():
        matches = list(audio_dir.glob(f"{row['id']}-*.mp3")) + list(audio_dir.glob(f"*{mslug}*.mp3"))
        if matches:
            audio = matches[0]
        else:
            raise FileNotFoundError(f"Run narrate first: {audio}")
    video = videos / f"{mslug}.mp4"

    reuse = config.get("performance", {}).get("reuse_completed_files", True)
    if reuse and video.exists() and video.stat().st_size > 100_000 and video.stat().st_mtime >= audio.stat().st_mtime:
        return video
    overrides = issue_overrides(config, work_dir.name)
    display_title = overrides.get("title_overrides", {}).get(row["id"], row["title"])
    category = overrides.get("article_categories", {}).get(row["id"], "")
    title_card(
        display_title,
        f"{config['magazine_name']} | {config['publisher']}",
        card,
        config,
        article_id=row["id"],
        category=category,
        pdf_path=pdf_path,
        pdf_page=int(row.get("pdf_start_page") or 0),
    )
    fps = str(max(1, int(config.get("video", {}).get("fps", 1))))
    cmd = [
        ffmpeg, "-y", "-framerate", fps, "-loop", "1", "-i", str(card), "-i", str(audio),
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage", "-r", fps,
        "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p", "-shortest",
        "-movflags", "+faststart", str(video),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return video


def _video_context(work_dir: Path):
    cards = work_dir / "cards"; cards.mkdir(exist_ok=True)
    videos = work_dir / "videos"; videos.mkdir(exist_ok=True)
    pdf_path = None
    extraction_file = work_dir / "extraction.json"
    if extraction_file.exists():
        try:
            candidate = Path(json.loads(extraction_file.read_text(encoding="utf-8")).get("source_pdf", ""))
            if candidate.exists():
                pdf_path = candidate
        except Exception:
            pass
    if not pdf_path:
        for p in list(ROOT.glob("*.pdf")) + list(work_dir.glob("*.pdf")):
            if p.exists():
                pdf_path = p
                break
    return cards, videos, ffmpeg_executable(), pdf_path


def render_videos(manifest: Path, work_dir: Path, config: dict) -> None:
    cards, videos, ffmpeg, pdf_path = _video_context(work_dir)
    rows = list(iter_manifest(manifest))
    workers = max(1, min(int(config.get("performance", {}).get("video_workers", 2)), len(rows)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sampada-video") as executor:
        futures = [executor.submit(_render_video_row, row, work_dir, config, cards, videos, ffmpeg, pdf_path) for row in rows]
        for future in as_completed(futures):
            future.result()


def produce_media(manifest: Path, work_dir: Path, config: dict) -> None:
    """Narrate and render each article end to end, so video encoding overlaps with other articles' narration."""
    audio_dir = work_dir / "audio"; audio_dir.mkdir(exist_ok=True)
    cards, videos, ffmpeg, pdf_path = _video_context(work_dir)
    performance = config.get("performance", {})
    rows = list(iter_manifest(manifest))
    workers = max(1, min(int(performance.get("audio_workers", 8)), len(rows)))
    encode_gate = threading.BoundedSemaphore(max(1, int(performance.get("video_workers", 4))))

    def job(row: dict) -> None:
        _narrate_row(row, work_dir, config, audio_dir)
        with encode_gate:
            _render_video_row(row, work_dir, config, cards, videos, ffmpeg, pdf_path)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sampada-media") as executor:
        for future in as_completed([executor.submit(job, row) for row in rows]):
            future.result()


def create_links_template(manifest: Path, work_dir: Path) -> Path:
    path = work_dir / "youtube_links.csv"
    existing = {}
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            existing = {row["id"]: row.get("youtube_url", "") for row in csv.DictReader(stream)}
    rows = [{"id": row["id"], "title": row["title"], "youtube_url": existing.get(row["id"], "")} for row in iter_manifest(manifest)]
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    return path


def make_qr_codes(links_csv: Path, work_dir: Path) -> int:
    import qrcode
    qr_dir = work_dir / "qr_codes"; qr_dir.mkdir(exist_ok=True)
    url_re = re.compile(r"^https?://(?:www\.)?(?:youtube\.com|youtu\.be)/", re.I)
    with links_csv.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    missing = []
    for row in rows:
        url = row.get("youtube_url", "").strip()
        if not url:
            missing.append(row["id"]); continue
        if not url_re.match(url):
            raise ValueError(f"Invalid YouTube URL for {row['id']}: {url}")
        image = qrcode.make(url)
        mslug = media_slug(row["title"])
        image.save(qr_dir / f"{mslug}.png")
    if missing:
        print(f"Skipped {len(missing)} rows without URLs: {', '.join(missing)}")
    return len(rows) - len(missing)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sampada magazine-to-YouTube automation")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "work")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("extract", help="Extract article titles and text from the magazine PDF")
    p.add_argument("pdf", type=Path)
    sub.add_parser("metadata", help="Generate YouTube metadata and narration scripts")
    sub.add_parser("narrate", help="Create multilingual MP3 narration with Edge TTS")
    sub.add_parser("video", help="Create 1080p MP4 videos from title cards and narration")
    sub.add_parser("links", help="Create/preserve the CSV in which YouTube URLs are entered")
    p = sub.add_parser("qr", help="Create QR PNGs from YouTube URLs")
    p.add_argument("--links-csv", type=Path)
    p = sub.add_parser("all", help="Run extraction through video creation")
    p.add_argument("pdf", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    work_dir = args.work_dir.resolve(); work_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    manifest = work_dir / "manifest.csv"
    if args.command in {"extract", "all"}:
        manifest = extract_articles(args.pdf.resolve(), work_dir, config)
        print(f"Review article selection and titles: {manifest}")
    if args.command in {"metadata", "all"}:
        print(f"Metadata review file: {metadata_and_script(manifest, work_dir, config)}")
    if args.command in {"narrate", "all"}:
        narrate(manifest, work_dir, config); print(f"Audio: {work_dir / 'audio'}")
    if args.command in {"video", "all"}:
        render_videos(manifest, work_dir, config); print(f"Videos: {work_dir / 'videos'}")
    if args.command == "links":
        print(f"Paste YouTube URLs here: {create_links_template(manifest, work_dir)}")
    if args.command == "qr":
        links = args.links_csv.resolve() if args.links_csv else work_dir / "youtube_links.csv"
        make_qr_codes(links, work_dir); print(f"QR codes: {work_dir / 'qr_codes'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
