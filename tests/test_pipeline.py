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
