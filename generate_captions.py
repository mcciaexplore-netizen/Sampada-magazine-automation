import asyncio
import csv
import datetime
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import edge_tts
from edge_tts.submaker import Subtitle

ROOT = Path(__file__).resolve().parent
work_dir = ROOT / "work"
manifest_path = work_dir / "manifest.csv"
captions_dir = work_dir / "captions"
captions_dir.mkdir(parents=True, exist_ok=True)

with open(ROOT / "config.json", "r", encoding="utf-8") as f:
    config = json.load(f)


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
    import hashlib
    ascii_part = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if ascii_part:
        return ascii_part[:72]
    return f"article-{hashlib.sha1(value.encode()).hexdigest()[:10]}"


def detect_language(text: str) -> str:
    devanagari = len(re.findall(r"[\u0900-\u097f]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return "mr" if devanagari > max(20, latin * 0.20) else "en"


def split_narration(text: str, limit: int = 4500) -> list[str]:
    import textwrap
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    chunks = []
    current = ""
    for paragraph in paragraphs:
        pieces = textwrap.wrap(paragraph, width=limit, break_long_words=False, break_on_hyphens=False) or [paragraph]
        for piece in pieces:
            candidate = f"{current}\n\n{piece}".strip()
            if current and len(candidate) > limit:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


async def generate_chunk_srt(chunk_text: str, voice: str, rate: str) -> list[Subtitle]:
    comm = edge_tts.Communicate(chunk_text, voice, rate=rate)
    submaker = edge_tts.SubMaker()
    async for msg in comm.stream():
        if msg["type"] in ("SentenceBoundary", "WordBoundary"):
            submaker.feed(msg)
    return submaker.cues


def timedelta_to_srt_time(td: datetime.timedelta) -> str:
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    milliseconds = int(td.microseconds / 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def cues_to_srt(all_cues: list[Subtitle]) -> str:
    lines = []
    for idx, cue in enumerate(all_cues, start=1):
        lines.append(str(idx))
        lines.append(f"{timedelta_to_srt_time(cue.start)} --> {timedelta_to_srt_time(cue.end)}")
        lines.append(cue.content)
        lines.append("")
    return "\n".join(lines)


def process_article(row: dict) -> None:
    mslug = media_slug(row["title"])
    script_path = work_dir / "scripts" / f"{mslug}.txt"
    if not script_path.exists():
        candidates = list((work_dir / "scripts").glob(f"*{row['id']}*.txt")) + list((work_dir / "scripts").glob(f"*{mslug}*.txt"))
        if candidates:
            script_path = candidates[0]
        else:
            print(f"Script missing: {script_path}")
            return
    srt_output = captions_dir / f"{mslug}.srt"
    script_text = script_path.read_text(encoding="utf-8")
    lang = detect_language(script_text)
    voice = config.get("voices", {}).get(lang) or config.get("voices", {}).get(row.get("language", "en"), "en-IN-NeerjaNeural")
    rate = config.get("speech_rate", "+10%")

    chunks = split_narration(script_text)
    all_cues: list[Subtitle] = []
    time_offset = 0.0

    for chunk in chunks:
        cues = asyncio.run(generate_chunk_srt(chunk, voice, rate))
        if not cues:
            continue
        chunk_duration = cues[-1].end.total_seconds()
        for cue in cues:
            shifted_start = cue.start + datetime.timedelta(seconds=time_offset)
            shifted_end = cue.end + datetime.timedelta(seconds=time_offset)
            all_cues.append(Subtitle(index=len(all_cues) + 1, start=shifted_start, end=shifted_end, content=cue.content))
        time_offset += chunk_duration + 0.1

    srt_content = cues_to_srt(all_cues)
    srt_output.write_text(srt_content, encoding="utf-8")
    print(f"Generated SRT for {row['id']}: {srt_output.name} ({len(all_cues)} cues)")


def main():
    with open(manifest_path, "r", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r.get("selected", "").lower() in ("yes", "y", "true", "1") and r.get("id") != "full"]

    print(f"Generating captions for {len(rows)} articles...")
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(process_article, rows))
    print("All captions generated successfully in work/captions/")


if __name__ == "__main__":
    main()
