from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "app.db"
STATIC_DIR = ROOT / "static"

APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "change-me")
APP_SECRET = os.getenv("APP_SECRET", "change-this-secret-before-production")
ALLOW_PRIVATE_URLS = os.getenv("ALLOW_PRIVATE_URLS", "false").lower() in {"1", "true", "yes"}
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_MB", "30")) * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "45"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("openlist-image-sync")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


class AppError(Exception):
    pass


class RemoteRequestError(AppError):
    pass


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def init(self) -> None:
        with self.lock, self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    interval_minutes INTEGER NOT NULL DEFAULT 60,
                    extraction_mode TEXT NOT NULL DEFAULT 'auto',
                    json_path TEXT NOT NULL DEFAULT '',
                    css_selector TEXT NOT NULL DEFAULT '',
                    headers_json TEXT NOT NULL DEFAULT '{}',
                    filename_template TEXT NOT NULL DEFAULT '{date}_{index}_{hash8}.{ext}',
                    target_subdir TEXT NOT NULL DEFAULT '',
                    max_items INTEGER NOT NULL DEFAULT 20,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_run_at TEXT,
                    last_run_at TEXT,
                    last_status TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL,
                    remote_url TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    bytes_size INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_sha256_uploaded
                    ON downloads(sha256) WHERE status = 'uploaded';

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER,
                    level TEXT NOT NULL DEFAULT 'info',
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_logs_created_at ON logs(created_at DESC);
                """
            )

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.lock, self.connect() as connection:
            cursor = connection.execute(sql, params)
            connection.commit()
            return int(cursor.lastrowid or 0)

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.lock, self.connect() as connection:
            return connection.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.lock, self.connect() as connection:
            return list(connection.execute(sql, params).fetchall())

    def setting(self, key: str, default: str = "") -> str:
        row = self.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


db = Database(DB_PATH)
db.init()


def fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(APP_SECRET.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    try:
        return fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise AppError("无法解密已保存的凭据，请检查 APP_SECRET 是否发生变化") from exc


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("OpenList 地址必须是 http:// 或 https:// 开头的完整地址")
    return value


def normalize_remote_path(value: str) -> str:
    value = (value or "/").strip().replace("\\", "/")
    parts = [part for part in value.split("/") if part and part not in {".", ".."}]
    return "/" + "/".join(parts)


def safe_subdir(value: str) -> str:
    value = (value or "").strip().replace("\\", "/")
    parts = [part.strip() for part in value.split("/") if part.strip() and part.strip() not in {".", ".."}]
    return "/".join(parts)


def join_remote_path(base: str, subdir: str, filename: str) -> str:
    filename = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", filename).strip(" .") or "image.bin"
    base_path = normalize_remote_path(base).rstrip("/")
    sub_path = safe_subdir(subdir)
    return "/" + "/".join(part for part in [base_path.strip("/"), sub_path, filename] if part)


def valid_http_url(value: str, *, allow_private: bool | None = None) -> str:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只支持 http:// 或 https:// 地址")
    should_allow_private = ALLOW_PRIVATE_URLS if allow_private is None else allow_private
    if not should_allow_private:
        hostname = parsed.hostname.lower().rstrip(".")
        if hostname in {"localhost", "localhost.localdomain"}:
            raise ValueError("默认禁止访问 localhost；如确实需要，请设置 ALLOW_PRIVATE_URLS=true")
        try:
            address = ipaddress.ip_address(hostname)
            if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
                raise ValueError("默认禁止访问内网地址；如确实需要，请设置 ALLOW_PRIVATE_URLS=true")
        except ValueError as exc:
            if "默认禁止访问" in str(exc):
                raise
    return value


def parse_headers(value: str) -> dict[str, str]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("请求头必须是 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise ValueError("请求头必须是 JSON 对象")
    result: dict[str, str] = {}
    for key, item in parsed.items():
        if not isinstance(key, str) or not isinstance(item, (str, int, float, bool)):
            raise ValueError("请求头的键和值必须是字符串或简单值")
        result[key] = str(item)
    return result


def slugify(value: str) -> str:
    value = re.sub(r"[^\w\-\u4e00-\u9fff]+", "-", value, flags=re.UNICODE).strip("-")
    return value[:60] or "source"


def extension_for(content_type: str, url: str, data: bytes) -> str:
    content_type = (content_type or "").split(";", 1)[0].lower()
    mapping = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/avif": "avif",
        "image/bmp": "bmp",
        "image/svg+xml": "svg",
        "image/heic": "heic",
    }
    if content_type in mapping:
        return mapping[content_type]
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if suffix in {"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "svg", "heic"}:
        return "jpg" if suffix == "jpeg" else suffix
    guessed = mimetypes.guess_extension(content_type or "")
    if guessed:
        return guessed.lstrip(".")
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"GIF8"):
        return "gif"
    return "bin"


def filename_from_response(headers: httpx.Headers, final_url: str) -> str:
    disposition = headers.get("content-disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)", disposition, re.I)
    if match:
        return unquote(match.group(1) or match.group(2)).strip()
    path_name = unquote(Path(urlparse(final_url).path).name)
    return path_name or "image"


def is_probable_image_url(value: str, key_hint: str = "") -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    suffix = Path(parsed.path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg", ".heic"}:
        return True
    return any(token in key_hint.lower() for token in ("image", "img", "photo", "pic", "avatar", "thumb", "url", "src"))


def resolve_json_path(data: Any, path: str) -> list[Any]:
    if not path or path.strip() in {"$", "."}:
        return [data]
    expression = path.strip()
    if expression.startswith("$"):
        expression = expression[1:]
    expression = expression.lstrip(".")
    tokens = re.findall(r"([^\.\[\]]+)|\[(\*|\d+)\]", expression)
    current = [data]
    for text_token, bracket_token in tokens:
        token = text_token or bracket_token
        next_values: list[Any] = []
        for item in current:
            if token == "*":
                if isinstance(item, dict):
                    next_values.extend(item.values())
                elif isinstance(item, list):
                    next_values.extend(item)
            elif isinstance(item, dict) and token in item:
                next_values.append(item[token])
            elif isinstance(item, list) and token.isdigit() and int(token) < len(item):
                next_values.append(item[int(token)])
        current = next_values
    return current


def walk_json_for_urls(value: Any, key_hint: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        candidate = value.strip()
        # In an explicitly selected JSON path, a URL may have no image suffix
        # (for example a random-image endpoint). The image response check later
        # filters out non-image links.
        if candidate.startswith(("http://", "https://")) and (not key_hint or is_probable_image_url(candidate, key_hint)):
            found.append(candidate)
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(walk_json_for_urls(item, str(key)))
    elif isinstance(value, list):
        for item in value:
            found.extend(walk_json_for_urls(item, key_hint))
    return found


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value for key, value in attrs if value}
        for key in ("src", "data-src", "data-original", "data-lazy-src", "href"):
            if attr_map.get(key):
                self.links.append(attr_map[key] or "")
        if attr_map.get("content") and attr_map.get("property", "").lower() in {"og:image", "twitter:image"}:
            self.links.append(attr_map["content"] or "")


@dataclass
class Candidate:
    url: str
    data: bytes | None = None
    content_type: str = ""
    headers: httpx.Headers | None = None


def unique_urls(urls: list[str], base_url: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        candidate = urljoin(base_url, raw.strip())
        if not candidate.startswith(("http://", "https://")):
            continue
        try:
            candidate = valid_http_url(candidate)
        except ValueError:
            continue
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def extract_candidates(response: httpx.Response, source: sqlite3.Row) -> list[Candidate]:
    mode = str(source["extraction_mode"] or "auto").lower()
    content_type = response.headers.get("content-type", "").lower()
    source_url = str(response.url)
    data = response.content
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise RemoteRequestError(f"API 响应超过 {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB 限制")

    url_suffix = Path(urlparse(source_url).path).suffix.lower()
    looks_like_image = url_suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg", ".heic"}
    if mode == "direct" or (mode == "auto" and (content_type.startswith("image/") or looks_like_image)):
        return [Candidate(source_url, data=data, content_type=content_type, headers=response.headers)]

    text = data.decode(response.encoding or "utf-8", errors="replace")
    urls: list[str] = []
    if mode in {"auto", "json"} or "json" in content_type:
        try:
            parsed = response.json()
            selected: list[Any] = resolve_json_path(parsed, source["json_path"])
            for item in selected:
                urls.extend(walk_json_for_urls(item))
                if isinstance(item, str) and item.startswith(("http://", "https://")):
                    urls.append(item)
        except (ValueError, json.JSONDecodeError):
            if mode == "json":
                raise RemoteRequestError("接口返回内容不是有效 JSON，请检查提取模式或 JSON 路径")

    if mode in {"auto", "html"} or "html" in content_type:
        soup = BeautifulSoup(text, "html.parser")
        selector = str(source["css_selector"] or "").strip()
        selected_nodes = soup.select(selector) if selector else soup.find_all(["img", "source", "meta", "a"])
        for node in selected_nodes:
            for attr in ("src", "data-src", "data-original", "data-lazy-src", "href", "content"):
                value = node.get(attr)
                if value:
                    urls.append(str(value))
            if node.name == "meta" and str(node.get("property", "")).lower() not in {"og:image", "twitter:image"}:
                continue

    if mode in {"auto", "text"}:
        urls.extend(re.findall(r"https?://[^\s\"'<>]+", text))

    return [Candidate(url) for url in unique_urls(urls, source_url)]


def render_filename(template: str, source: sqlite3.Row, index: int, digest: str, content_type: str, url: str, data: bytes) -> str:
    ext = extension_for(content_type, url, data)
    now = utc_now()
    values = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H-%M-%S"),
        "index": index,
        "hash8": digest[:8],
        "hash": digest,
        "ext": ext,
        "source": slugify(str(source["name"])),
        "original": Path(filename_from_response(httpx.Headers(), url)).stem[:80],
    }
    selected = template.strip() or "{date}_{index}_{hash8}.{ext}"
    try:
        rendered = selected.format(**values)
    except (KeyError, ValueError):
        rendered = f"{values['date']}_{index}_{values['hash8']}.{ext}"
    rendered = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", rendered).strip(" .")
    if "." not in rendered:
        rendered = f"{rendered}.{ext}"
    return rendered[:180]


def openlist_config() -> dict[str, str]:
    return {
        "base_url": db.setting("openlist.base_url"),
        "username": db.setting("openlist.username"),
        "target_path": db.setting("openlist.target_path", "/Images"),
        "has_password": bool(db.setting("openlist.password")),
    }


def openlist_credentials() -> tuple[str, str, str, str]:
    base_url = db.setting("openlist.base_url")
    username = db.setting("openlist.username")
    encrypted = db.setting("openlist.password")
    target_path = db.setting("openlist.target_path", "/Images")
    if not base_url or not username or not encrypted:
        raise AppError("请先在设置中填写 OpenList 地址、用户名和密码")
    return base_url, username, decrypt_secret(encrypted), target_path


class OpenListClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = normalize_base_url(base_url)
        self.username = username
        self.password = password
        self.token: str | None = None
        self.client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    async def login(self) -> None:
        response = await self.client.post(
            f"{self.base_url}/api/auth/login",
            json={"username": self.username, "password": self.password, "otp_code": ""},
        )
        if response.status_code >= 400:
            raise AppError(f"OpenList 登录失败（HTTP {response.status_code}）")
        payload = response.json()
        code = payload.get("code") if isinstance(payload, dict) else None
        if code not in (None, 0, 200):
            raise AppError(str(payload.get("message") or "OpenList 登录失败"))
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        token = data.get("token") if isinstance(data, dict) else data if isinstance(data, str) else None
        token = token or (payload.get("token") if isinstance(payload, dict) else None)
        if not token:
            raise AppError("OpenList 登录响应中没有找到 token，请确认账号密码或版本接口")
        self.token = str(token)

    def headers(self) -> dict[str, str]:
        if not self.token:
            raise AppError("OpenList 尚未登录")
        return {"Authorization": self.token}

    async def me(self) -> dict[str, Any]:
        response = await self.client.get(f"{self.base_url}/api/me", headers=self.headers())
        if response.status_code >= 400:
            raise AppError(f"读取 OpenList 用户信息失败（HTTP {response.status_code}）")
        payload = response.json()
        if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200):
            raise AppError(str(payload.get("message") or "读取 OpenList 用户信息失败"))
        return payload if isinstance(payload, dict) else {"data": payload}

    async def list_path(self, path: str) -> dict[str, Any]:
        response = await self.client.post(
            f"{self.base_url}/api/fs/list",
            headers=self.headers(),
            json={"path": normalize_remote_path(path), "password": "", "page": 1, "per_page": 100, "refresh": False},
        )
        if response.status_code >= 400:
            raise AppError(f"读取 OpenList 目录失败（HTTP {response.status_code}）")
        payload = response.json()
        if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200):
            raise AppError(str(payload.get("message") or "读取 OpenList 目录失败"))
        return payload if isinstance(payload, dict) else {"data": payload}

    async def mkdir(self, path: str) -> None:
        path = normalize_remote_path(path)
        if path == "/":
            return
        response = await self.client.post(f"{self.base_url}/api/fs/mkdir", headers=self.headers(), json={"path": path})
        if response.status_code >= 400:
            try:
                payload = response.json()
                message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
            except ValueError:
                message = ""
            if not any(word in message.lower() for word in ("exist", "已存在", "already")):
                raise AppError(f"创建 OpenList 目录失败（HTTP {response.status_code}）")

    async def ensure_directory(self, path: str) -> None:
        normalized = normalize_remote_path(path)
        current = ""
        for part in normalized.strip("/").split("/"):
            current += "/" + part
            await self.mkdir(current)

    async def upload(self, path: str, data: bytes, content_type: str) -> dict[str, Any]:
        encoded_path = quote(normalize_remote_path(path), safe="/")
        headers = {
            **self.headers(),
            "File-Path": encoded_path,
            "As-Task": "false",
            "Content-Type": content_type.split(";", 1)[0] or "application/octet-stream",
            "Last-Modified": utc_iso(),
        }
        response = await self.client.put(f"{self.base_url}/api/fs/put", headers=headers, content=data)
        if response.status_code >= 400:
            raise AppError(f"上传到 OpenList 失败（HTTP {response.status_code}）")
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text[:500]}
        if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200):
            raise AppError(str(payload.get("message") or "上传到 OpenList 失败"))
        return payload if isinstance(payload, dict) else {"data": payload}


async def create_openlist_client() -> tuple[OpenListClient, str]:
    base_url, username, password, target_path = openlist_credentials()
    client = OpenListClient(base_url, username, password)
    try:
        await client.login()
    except Exception:
        await client.close()
        raise
    return client, target_path


async def fetch_source(source: sqlite3.Row) -> list[Candidate]:
    url = valid_http_url(str(source["url"]))
    headers = parse_headers(str(source["headers_json"] or "{}"))
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        if response.status_code >= 400:
            raise RemoteRequestError(f"图片 API 返回 HTTP {response.status_code}")
        candidates = extract_candidates(response, source)
        for candidate in candidates:
            candidate.url = valid_http_url(candidate.url)
        return candidates


async def download_candidate(candidate: Candidate, source: sqlite3.Row) -> tuple[bytes, str, str, httpx.Headers]:
    if candidate.data is not None:
        return candidate.data, candidate.content_type or "application/octet-stream", candidate.url, candidate.headers or httpx.Headers()
    headers = parse_headers(str(source["headers_json"] or "{}"))
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers=headers) as client:
        response = await client.get(candidate.url)
        if response.status_code >= 400:
            raise RemoteRequestError(f"图片地址返回 HTTP {response.status_code}")
        if len(response.content) > MAX_DOWNLOAD_BYTES:
            raise RemoteRequestError(f"图片超过 {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB 限制")
        content_type = response.headers.get("content-type", "application/octet-stream")
        if not content_type.startswith("image/") and not is_probable_image_url(str(response.url), "image"):
            raise RemoteRequestError("提取到的地址不是图片响应")
        return response.content, content_type, str(response.url), response.headers


def log_event(message: str, source_id: int | None = None, level: str = "info") -> None:
    logger.info(message)
    db.execute(
        "INSERT INTO logs(source_id, level, message, created_at) VALUES(?, ?, ?, ?)",
        (source_id, level, message, utc_iso()),
    )
    db.execute(
        "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 2000)"
    )


def update_source_run(source_id: int, *, status_value: str, error: str | None = None, next_run: datetime | None = None) -> None:
    db.execute(
        "UPDATE sources SET last_run_at=?, last_status=?, last_error=?, next_run_at=?, updated_at=? WHERE id=?",
        (utc_iso(), status_value, error, utc_iso(next_run) if next_run else None, utc_iso(), source_id),
    )


async def run_source(source_id: int) -> dict[str, Any]:
    source = db.fetchone("SELECT * FROM sources WHERE id = ?", (source_id,))
    if not source:
        raise AppError("图片来源不存在")
    interval = max(1, int(source["interval_minutes"] or 60))
    next_run = utc_now() + timedelta(minutes=interval)
    log_event(f"开始抓取：{source['name']}", source_id)
    try:
        candidates = await fetch_source(source)
        max_items = max(1, min(100, int(source["max_items"] or 20)))
        candidates = candidates[:max_items]
        if not candidates:
            update_source_run(source_id, status_value="empty", next_run=next_run)
            log_event("接口未提取到图片地址", source_id, "warning")
            return {"source_id": source_id, "found": 0, "uploaded": 0, "skipped": 0, "failed": 0}

        openlist, target_path = await create_openlist_client()
        uploaded = 0
        skipped = 0
        failed = 0
        try:
            source_subdir = str(source["target_subdir"] or "")
            await openlist.ensure_directory("/".join(part for part in [target_path, source_subdir] if part))
            for index, candidate in enumerate(candidates, 1):
                try:
                    content, content_type, final_url, response_headers = await download_candidate(candidate, source)
                    digest = hashlib.sha256(content).hexdigest()
                    known = db.fetchone(
                        "SELECT id FROM downloads WHERE sha256 = ? AND status = 'uploaded' LIMIT 1", (digest,)
                    )
                    if known:
                        skipped += 1
                        log_event(f"跳过重复图片：{candidate.url}", source_id)
                        continue
                    filename = render_filename(
                        str(source["filename_template"] or ""), source, index, digest, content_type, final_url, content
                    )
                    remote_path = join_remote_path(target_path, source_subdir, filename)
                    await openlist.upload(remote_path, content, content_type)
                    db.execute(
                        "INSERT INTO downloads(source_id, remote_url, file_path, sha256, bytes_size, status, created_at) VALUES(?, ?, ?, ?, ?, 'uploaded', ?)",
                        (source_id, final_url, remote_path, digest, len(content), utc_iso()),
                    )
                    uploaded += 1
                    log_event(f"已上传：{remote_path}", source_id)
                except Exception as exc:  # one bad image should not abort the rest of the source
                    failed += 1
                    log_event(f"图片处理失败：{candidate.url}；{exc}", source_id, "error")
                    db.execute(
                        "INSERT INTO downloads(source_id, remote_url, file_path, sha256, bytes_size, status, error, created_at) VALUES(?, ?, '', '', 0, 'failed', ?, ?)",
                        (source_id, candidate.url, str(exc), utc_iso()),
                    )
        finally:
            await openlist.close()

        result = {"source_id": source_id, "found": len(candidates), "uploaded": uploaded, "skipped": skipped, "failed": failed}
        final_status = "success" if failed == 0 else "partial"
        update_source_run(source_id, status_value=final_status, next_run=next_run)
        log_event(f"抓取完成：发现 {len(candidates)}，上传 {uploaded}，跳过 {skipped}，失败 {failed}", source_id)
        return result
    except Exception as exc:
        update_source_run(source_id, status_value="error", error=str(exc), next_run=next_run)
        log_event(f"抓取失败：{exc}", source_id, "error")
        raise


class Scheduler:
    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.running: dict[int, asyncio.Task[Any]] = {}
        self.locks: dict[int, asyncio.Lock] = {}

    async def start(self) -> None:
        self.task = asyncio.create_task(self.loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        tasks = list(self.running.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def loop(self) -> None:
        while True:
            try:
                now = utc_iso()
                rows = db.fetchall(
                    "SELECT id FROM sources WHERE enabled = 1 AND (next_run_at IS NULL OR next_run_at <= ?)",
                    (now,),
                )
                for row in rows:
                    source_id = int(row["id"])
                    if source_id not in self.running or self.running[source_id].done():
                        self.running[source_id] = asyncio.create_task(self.run_safely(source_id))
            except Exception as exc:
                logger.exception("scheduler loop error: %s", exc)
            await asyncio.sleep(20)

    async def run_safely(self, source_id: int) -> None:
        lock = self.locks.setdefault(source_id, asyncio.Lock())
        async with lock:
            try:
                await run_source(source_id)
            except Exception:
                pass


scheduler = Scheduler()
app = FastAPI(title="OpenList Image Sync", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def session_token() -> str:
    expires = int((utc_now() + timedelta(days=7)).timestamp())
    payload = f"{APP_USERNAME}:{expires}".encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(APP_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def valid_session(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    encoded, signature = value.rsplit(".", 1)
    expected = hmac.new(APP_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        user, expires = base64.urlsafe_b64decode(padded).decode("utf-8").split(":", 1)
        return user == APP_USERNAME and int(expires) > int(utc_now().timestamp())
    except (ValueError, UnicodeDecodeError):
        return False


async def require_auth(request: Request) -> None:
    if not valid_session(request.cookies.get("image_sync_session")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")


class LoginBody(BaseModel):
    username: str
    password: str


class OpenListUpdate(BaseModel):
    base_url: str
    username: str
    password: str | None = None
    target_path: str = "/Images"


ExtractionMode = Literal["auto", "direct", "json", "html", "text"]


class SourcePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str
    enabled: bool = True
    interval_minutes: int = Field(default=60, ge=1, le=10080)
    extraction_mode: ExtractionMode = "auto"
    json_path: str = Field(default="", max_length=300)
    css_selector: str = Field(default="", max_length=300)
    headers_json: str = Field(default="{}", max_length=10000)
    filename_template: str = Field(default="{date}_{index}_{hash8}.{ext}", max_length=300)
    target_subdir: str = Field(default="", max_length=300)
    max_items: int = Field(default=20, ge=1, le=100)


def payload_dict(payload: BaseModel) -> dict[str, Any]:
    return payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()


def source_output(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["enabled"] = bool(result["enabled"])
    return result


def validate_source_payload(payload: SourcePayload) -> dict[str, Any]:
    data = payload_dict(payload)
    data["url"] = valid_http_url(data["url"])
    parse_headers(data["headers_json"])
    data["target_subdir"] = safe_subdir(data["target_subdir"])
    return data


@app.on_event("startup")
async def startup() -> None:
    if APP_SECRET.startswith("change-"):
        logger.warning("APP_SECRET 仍为示例值，请在生产环境修改")
    await scheduler.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    await scheduler.stop()


@app.get("/api/session")
async def session_state(request: Request) -> dict[str, Any]:
    return {"authenticated": valid_session(request.cookies.get("image_sync_session")), "username": APP_USERNAME}


@app.post("/api/login")
async def login(body: LoginBody) -> JSONResponse:
    if not hmac.compare_digest(body.username, APP_USERNAME) or not hmac.compare_digest(body.password, APP_PASSWORD):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    response = JSONResponse({"ok": True})
    response.set_cookie("image_sync_session", session_token(), httponly=True, samesite="lax", max_age=7 * 86400)
    return response


@app.post("/api/logout", dependencies=[Depends(require_auth)])
async def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie("image_sync_session")
    return response


@app.get("/api/overview", dependencies=[Depends(require_auth)])
async def overview() -> dict[str, Any]:
    source_count = db.fetchone("SELECT COUNT(*) AS count FROM sources")
    enabled_count = db.fetchone("SELECT COUNT(*) AS count FROM sources WHERE enabled = 1")
    uploaded_count = db.fetchone("SELECT COUNT(*) AS count FROM downloads WHERE status = 'uploaded'")
    failed_count = db.fetchone("SELECT COUNT(*) AS count FROM downloads WHERE status = 'failed'")
    bytes_row = db.fetchone("SELECT COALESCE(SUM(bytes_size), 0) AS total FROM downloads WHERE status = 'uploaded'")
    return {
        "source_count": int(source_count["count"]),
        "enabled_count": int(enabled_count["count"]),
        "uploaded_count": int(uploaded_count["count"]),
        "failed_count": int(failed_count["count"]),
        "uploaded_bytes": int(bytes_row["total"]),
        "openlist": openlist_config(),
    }


@app.get("/api/openlist", dependencies=[Depends(require_auth)])
async def get_openlist() -> dict[str, Any]:
    return openlist_config()


@app.put("/api/openlist", dependencies=[Depends(require_auth)])
async def put_openlist(body: OpenListUpdate) -> dict[str, Any]:
    try:
        base_url = normalize_base_url(body.base_url)
        target_path = normalize_remote_path(body.target_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.set_setting("openlist.base_url", base_url)
    db.set_setting("openlist.username", body.username.strip())
    db.set_setting("openlist.target_path", target_path)
    if body.password:
        db.set_setting("openlist.password", encrypt_secret(body.password))
    return openlist_config()


@app.post("/api/openlist/test", dependencies=[Depends(require_auth)])
async def test_openlist() -> dict[str, Any]:
    client, target_path = await create_openlist_client()
    try:
        user = await client.me()
        listing = await client.list_path(target_path)
        data = listing.get("data", {}) if isinstance(listing, dict) else {}
        content = data.get("content", []) if isinstance(data, dict) else []
        return {"ok": True, "target_path": target_path, "user": user.get("data", user), "items": content[:20]}
    finally:
        await client.close()


@app.get("/api/openlist/list", dependencies=[Depends(require_auth)])
async def list_openlist(path: str | None = None) -> dict[str, Any]:
    client, target_path = await create_openlist_client()
    try:
        return await client.list_path(path or target_path)
    finally:
        await client.close()


@app.get("/api/sources", dependencies=[Depends(require_auth)])
async def get_sources() -> list[dict[str, Any]]:
    return [source_output(row) for row in db.fetchall("SELECT * FROM sources ORDER BY id DESC")]


@app.post("/api/sources", dependencies=[Depends(require_auth)])
async def create_source(body: SourcePayload) -> dict[str, Any]:
    try:
        data = validate_source_payload(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    now = utc_iso()
    source_id = db.execute(
        """
        INSERT INTO sources(name, url, enabled, interval_minutes, extraction_mode, json_path, css_selector,
                            headers_json, filename_template, target_subdir, max_items, created_at, updated_at, next_run_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["name"].strip(), data["url"], int(data["enabled"]), data["interval_minutes"], data["extraction_mode"],
            data["json_path"], data["css_selector"], data["headers_json"], data["filename_template"],
            data["target_subdir"], data["max_items"], now, now, utc_iso(utc_now() + timedelta(minutes=data["interval_minutes"])),
        ),
    )
    row = db.fetchone("SELECT * FROM sources WHERE id = ?", (source_id,))
    assert row
    return source_output(row)


@app.put("/api/sources/{source_id}", dependencies=[Depends(require_auth)])
async def update_source(source_id: int, body: SourcePayload) -> dict[str, Any]:
    if not db.fetchone("SELECT id FROM sources WHERE id = ?", (source_id,)):
        raise HTTPException(status_code=404, detail="图片来源不存在")
    try:
        data = validate_source_payload(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = db.fetchone("SELECT next_run_at FROM sources WHERE id = ?", (source_id,))
    next_run = row["next_run_at"] if row and row["next_run_at"] else utc_iso(utc_now() + timedelta(minutes=data["interval_minutes"]))
    db.execute(
        """
        UPDATE sources SET name=?, url=?, enabled=?, interval_minutes=?, extraction_mode=?, json_path=?, css_selector=?,
            headers_json=?, filename_template=?, target_subdir=?, max_items=?, updated_at=?, next_run_at=? WHERE id=?
        """,
        (
            data["name"].strip(), data["url"], int(data["enabled"]), data["interval_minutes"], data["extraction_mode"],
            data["json_path"], data["css_selector"], data["headers_json"], data["filename_template"],
            data["target_subdir"], data["max_items"], utc_iso(), next_run, source_id,
        ),
    )
    updated = db.fetchone("SELECT * FROM sources WHERE id = ?", (source_id,))
    assert updated
    return source_output(updated)


@app.delete("/api/sources/{source_id}", dependencies=[Depends(require_auth)])
async def delete_source(source_id: int) -> dict[str, bool]:
    if not db.fetchone("SELECT id FROM sources WHERE id = ?", (source_id,)):
        raise HTTPException(status_code=404, detail="图片来源不存在")
    db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    return {"ok": True}


@app.post("/api/sources/{source_id}/run", dependencies=[Depends(require_auth)])
async def run_source_now(source_id: int) -> dict[str, Any]:
    lock = scheduler.locks.setdefault(source_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(status_code=409, detail="该来源正在抓取中")
    async with lock:
        try:
            return await run_source(source_id)
        except AppError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/logs", dependencies=[Depends(require_auth)])
async def get_logs(limit: int = Query(default=200, ge=1, le=500)) -> list[dict[str, Any]]:
    rows = db.fetchall("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(row) for row in rows]


@app.get("/api/downloads", dependencies=[Depends(require_auth)])
async def get_downloads(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
    rows = db.fetchall(
        """
        SELECT downloads.*, sources.name AS source_name
        FROM downloads LEFT JOIN sources ON sources.id = downloads.source_id
        ORDER BY downloads.id DESC LIMIT ?
        """,
        (limit,),
    )
    return [dict(row) for row in rows]


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/{path:path}", include_in_schema=False)
async def spa_fallback(path: str) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
