from pathlib import Path

from main import clean_text, detect_language, keywords_for, parse_contents, slugify, split_narration


def test_parse_contents_wrapped_title():
    text = "New Fuel-Cell Catalyst Could\nHelp Power Data Centers....................................55"
    assert parse_contents(text) == [("New Fuel-Cell Catalyst Could Help Power Data Centers", 55)]


def test_language_detection():
    assert detect_language("Food safety systems and reliable packaging") == "en"
    assert detect_language("यश ट्रेडिंग कंपनीकडून फळे आणि भाज्यांची निर्यात केली जाते") == "mr"


def test_slug_non_latin_is_stable():
    assert slugify("मराठी शीर्षक") == slugify("मराठी शीर्षक")


def test_clean_hyphenated_line_break():
    assert clean_text("inter-\nnational") == "international"


def test_keywords_have_signal():
    words = keywords_for("Packaging safety traceability packaging automation", "Food Safety")
    assert "packaging" in words and "traceability" in words


def test_long_narration_is_chunked_without_loss():
    text = "First paragraph.\n\n" + ("second sentence " * 40)
    chunks = split_narration(text, limit=100)
    assert len(chunks) > 2
    assert "First paragraph." in chunks[0]


def test_qr_placeholder_text_is_stripped():
    from main import strip_qr_noise
    text = "Final sentence. Scan the QR code to listen to the gist of the article."
    assert "scan" not in strip_qr_noise(text).lower()
    assert strip_qr_noise(text).startswith("Final sentence.")


def test_articles_selected_only_when_qr_inside_range():
    import tempfile
    from pathlib import Path
    import pandas as pd
    from magazine_app import select_articles_containing_qr
    manifest = Path(tempfile.mkdtemp()) / "manifest.csv"
    pd.DataFrame([
        {"id": "p005", "pdf_start_page": 3, "pdf_end_page": 5, "selected": "yes"},
        {"id": "p008", "pdf_start_page": 6, "pdf_end_page": 12, "selected": "yes"},
    ]).to_csv(manifest, index=False)
    frame = select_articles_containing_qr(manifest, [12])
    assert list(frame["selected"]) == ["no", "yes"]
    assert set(select_articles_containing_qr(manifest, [])["selected"]) == {"no"}


def test_issue_overrides_are_per_issue():
    from main import issue_overrides
    config = {"issue_overrides": {"2026-08": {"article_titles": {"5": "X"}}}}
    assert issue_overrides(config, "2026-08")["article_titles"]["5"] == "X"
    assert issue_overrides(config, "2026-09") == {}
