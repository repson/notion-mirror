from notion_mirror.slugify import slugify


def test_strips_accents_and_uppercase():
    assert slugify("Café Menu") == "cafe-menu"
    assert slugify("Naïve Résumé") == "naive-resume"


def test_odd_characters_become_dashes():
    assert slugify("My page / personal: notes") == "my-page-personal-notes"
    assert slugify("What?!") == "what"


def test_emoji_only_or_empty():
    assert slugify("✅🎯") == "untitled"
    assert slugify("") == "untitled"
    assert slugify(None) == "untitled"


def test_truncates_without_trailing_dashes():
    long_text = "a" * 100
    result = slugify(long_text, max_length=50)
    assert len(result) == 50
    assert result == "a" * 50


def test_numbers_and_dots_are_kept():
    assert slugify("Report 2026 v1.2") == "report-2026-v1.2"
