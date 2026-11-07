from notion_mirror.urls import (
    ext_from_content_type,
    guess_filename,
    is_notion_file_url,
    url_identity,
)


def test_internal_s3_file_url():
    assert is_notion_file_url(
        "https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/FILE/logo.png?X-Amz-Signature=abc"
    )


def test_legacy_s3_url():
    assert is_notion_file_url(
        "https://s3-us-west-2.amazonaws.com/secure.notion-static.com/FILE/logo.png?sig=1"
    )


def test_notion_image_proxy():
    assert is_notion_file_url(
        "https://www.notion.so/image/https%3A%2F%2Fexample.com%2Fa.png?table=block&id=b"
    )


def test_page_link_is_not_a_file():
    assert not is_notion_file_url("https://www.notion.so/My-page-1234abcdef")
    assert not is_notion_file_url("https://myspace.notion.site/page-123")


def test_external_url_is_not_notion():
    assert not is_notion_file_url("https://example.com/image.png")
    assert not is_notion_file_url("")


def test_identity_strips_volatile_signature():
    url = "https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/FILE/a.png?X-Amz-Signature=zzz&other=1"
    assert url_identity(url) == "https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/FILE/a.png"


def test_guess_filename():
    assert guess_filename("https://host/directory/logo.png?x=1") == "logo.png"
    assert guess_filename("https://host/directory/final%20report.pdf") == "final report.pdf"
    assert guess_filename("https://host/") == "file"


def test_ext_from_content_type():
    assert ext_from_content_type("image/jpeg; charset=binary") == ".jpg"
    assert ext_from_content_type("application/pdf") == ".pdf"
    assert ext_from_content_type("") == ""
    assert ext_from_content_type("text/plain") == ".txt"
