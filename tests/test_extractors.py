import sqlite3

import httpx

from app import extract_candidates, resolve_json_path, render_filename


def source_row(**overrides):
    values = {
        "extraction_mode": "auto",
        "json_path": "",
        "css_selector": "",
        "name": "测试来源",
    }
    values.update(overrides)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE source (extraction_mode TEXT, json_path TEXT, css_selector TEXT, name TEXT)")
    connection.execute("INSERT INTO source VALUES (?, ?, ?, ?)", tuple(values[key] for key in values))
    return connection.execute("SELECT * FROM source").fetchone()


def response(body: bytes, content_type: str) -> httpx.Response:
    request = httpx.Request("GET", "https://api.example.test/images")
    return httpx.Response(200, headers={"content-type": content_type}, content=body, request=request)


def test_json_path_supports_arrays():
    payload = {"data": [{"url": "https://cdn.example.test/a"}, {"url": "https://cdn.example.test/b"}]}
    assert resolve_json_path(payload, "$.data[*].url") == ["https://cdn.example.test/a", "https://cdn.example.test/b"]


def test_json_image_urls_are_extracted_without_file_suffix():
    body = b'{"data":[{"url":"https://cdn.example.test/random?id=1"}]}'
    items = extract_candidates(response(body, "application/json"), source_row(extraction_mode="json", json_path="$.data[*].url"))
    assert [item.url for item in items] == ["https://cdn.example.test/random?id=1"]


def test_html_images_are_extracted():
    body = b'<html><img class="photo" src="/a.jpg"><meta property="og:image" content="https://cdn.example.test/b.png"></html>'
    items = extract_candidates(response(body, "text/html"), source_row(extraction_mode="html"))
    assert [item.url for item in items] == ["https://api.example.test/a.jpg", "https://cdn.example.test/b.png"]


def test_filename_template_has_hash_and_extension():
    name = render_filename("{date}_{hash8}.{ext}", source_row(), 1, "abcdef123456", "image/png", "https://x.test/p", b"png")
    assert name.endswith("_abcdef12.png")
