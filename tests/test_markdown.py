from notion_mirror.markdown import extract_urls, local_file_links, rewrite_links

MD = """# Title

![logo](https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/IMG/logo.png?sig=abc)

An [external](https://example.com "with title") link and an image
![photo](https://www.notion.so/image/https%3A%2F%2Fexample.com%2Ff.png "title2").

Plain text with (parentheses) but no link.
"""


def mapper(url: str):
    if "prod-files-secure" in url:
        return "/backup/files/abc123/logo.png"
    if "/image/" in url:
        return "/backup/files/def456/f.png"
    return None


def test_extract_urls_finds_all():
    urls = extract_urls(MD)
    assert len(urls) == 3


def test_rewrite_links_only_internal_ones():
    result, n = rewrite_links(MD, mapper)
    assert n == 2
    assert "(/backup/files/abc123/logo.png)" in result
    assert "(/backup/files/def456/f.png" in result  # a title follows
    assert "https://example.com" in result  # external untouched


def test_rewrite_links_keeps_link_title():
    result, _ = rewrite_links(MD, mapper)
    assert '(/backup/files/def456/f.png "title2")' in result


def test_rewrite_links_with_noop_mapper():
    result, n = rewrite_links(MD, lambda u: None)
    assert n == 0
    assert result == MD


def test_local_file_links():
    md = "![a](/backup/files/abcdef123456/img.png) and [doc](/backup/files/abcdef123456/doc.pdf)"
    links = local_file_links(md)
    assert [local for local, _ in links] == [
        "/backup/files/abcdef123456/img.png",
        "/backup/files/abcdef123456/doc.pdf",
    ]
