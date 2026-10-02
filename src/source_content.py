"""Đọc phần văn bản cần thiết từ trang nguồn để AI tóm tắt chính xác hơn."""

import html
import json
import re
import codecs
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from .logger import get_logger


logger = get_logger("source_content")
MAX_DOWNLOAD_BYTES = 2_000_000
MAX_TEXT_CHARS = 8_000


def _clean(value: str) -> str:
    return " ".join(html.unescape(str(value or "")).split())


class _ArticleHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta_descriptions = []
        self.paragraphs = []
        self.scoped_paragraphs = []
        self.titles = []
        self._contexts = []
        self._paragraph_scope = 0
        self._heading_tag = ""
        self._heading_parts = []
        self.json_ld = []
        self._skip_depth = 0
        self._in_paragraph = False
        self._paragraph_parts = []
        self._in_json_ld = False
        self._json_parts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        attributes = {str(key).casefold(): str(value or "") for key, value in attrs}
        scope = self._contexts[-1][1] if self._contexts else 0
        blocked = self._contexts[-1][2] if self._contexts else False
        marker = f"{attributes.get('id', '')} {attributes.get('class', '')}".casefold()
        blocked = blocked or tag in {"footer", "header", "nav", "aside"} or bool(re.search(
            r"(?:related|sidebar|recommend|comment|advert|breadcrumb|footer|menu)", marker
        ))
        if tag in {"main", "article"}:
            scope = max(scope, 1 if tag == "main" else 2)
        if attributes.get("itemprop", "").casefold() == "articlebody" or re.search(
            r"(?:article[-_](?:body|content)|(?:detail|content)[-_](?:content|detail|body)|"
            r"(?:news|story)[-_](?:body|content)|vns_content|fck_detail)", marker
        ):
            scope = 3
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self._contexts.append((tag, scope, blocked))
        if tag in {"style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag == "script":
            script_type = attributes.get("type", "").casefold()
            if "ld+json" in script_type:
                self._in_json_ld = True
                self._json_parts = []
            else:
                self._skip_depth += 1
        elif tag == "meta":
            key = (attributes.get("name") or attributes.get("property") or "").casefold()
            if key in {"description", "og:description", "twitter:description"}:
                content = _clean(attributes.get("content"))
                if content:
                    self.meta_descriptions.append(content)
            elif key in {"og:title", "twitter:title"}:
                if attributes.get("content"):
                    self.titles.append(_clean(attributes["content"]))
        elif tag in {"h1", "title"} and not self._skip_depth:
            self._heading_tag = tag
            self._heading_parts = []
        elif tag == "p" and not self._skip_depth and not blocked:
            self._in_paragraph = True
            self._paragraph_parts = []
            self._paragraph_scope = scope

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag == "script":
            if self._in_json_ld:
                payload = "".join(self._json_parts).strip()
                if payload:
                    self.json_ld.append(payload)
                self._in_json_ld = False
                self._json_parts = []
            elif self._skip_depth:
                self._skip_depth -= 1
        elif tag in {"style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "p" and self._in_paragraph:
            paragraph = _clean(" ".join(self._paragraph_parts))
            if len(paragraph) >= 35:
                self.paragraphs.append(paragraph)
                self.scoped_paragraphs.append((self._paragraph_scope, paragraph))
            self._in_paragraph = False
            self._paragraph_parts = []
        elif tag == self._heading_tag:
            heading = _clean(" ".join(self._heading_parts))
            if heading:
                self.titles.append(heading)
            self._heading_tag = ""
            self._heading_parts = []
        for index in range(len(self._contexts) - 1, -1, -1):
            if self._contexts[index][0] == tag:
                del self._contexts[index:]
                break

    def handle_data(self, data):
        if self._in_json_ld:
            self._json_parts.append(data)
        elif self._in_paragraph and not self._skip_depth:
            self._paragraph_parts.append(data)
        elif self._heading_tag and not self._skip_depth:
            self._heading_parts.append(data)


def _json_article_bodies(payload, result: list[str]) -> None:
    if isinstance(payload, list):
        for item in payload:
            _json_article_bodies(item, result)
        return
    if not isinstance(payload, dict):
        return
    body = _clean(payload.get("articleBody"))
    if body:
        result.append(body)
    graph = payload.get("@graph")
    if graph:
        _json_article_bodies(graph, result)


def _title_words(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFD", _clean(value).casefold().replace("đ", "d"))
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    return set(re.findall(r"[a-z0-9]+", normalized)) - {"va", "cua", "tai", "cho", "la", "ve", "trong"}


def _extract_article_text(raw_html: str, expected_title: str = "") -> str:
    parser = _ArticleHTMLParser()
    try:
        parser.feed(raw_html)
    except Exception:
        pass
    expected_words = _title_words(expected_title)
    if expected_words and parser.titles and not any(
        len(expected_words & _title_words(title)) >= min(3, len(expected_words))
        and len(expected_words & _title_words(title)) / len(expected_words) >= 0.45
        for title in parser.titles
    ):
        # Trang chuyển hướng/shell video có thể chứa một bài hoặc chân trang khác.
        # Không ghi đè mô tả RSS bằng nội dung không khớp bài đang xử lý.
        return ""
    bodies = []
    for raw_json in parser.json_ld:
        try:
            _json_article_bodies(json.loads(raw_json), bodies)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    best = max(bodies, key=len, default="")
    if not best and parser.scoped_paragraphs:
        scope = max(item[0] for item in parser.scoped_paragraphs)
        best = " ".join(dict.fromkeys(text for rank, text in parser.scoped_paragraphs if rank == scope))
    if not best and parser.meta_descriptions:
        best = max(parser.meta_descriptions, key=len)
    best = re.sub(r"\s+", " ", best).strip()
    return best[:MAX_TEXT_CHARS]


def _safe_public_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.casefold()
    return host not in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} and not host.endswith(".local")


def fetch_source_description(url: str, timeout: float = 18, expected_title: str = "") -> str:
    """Tải giới hạn trang công khai, theo redirect an toàn và trích văn bản bài viết."""
    current_url = str(url or "").strip()
    headers = {"User-Agent": "Mozilla/5.0 AutoUpFeed/2.0 (+article-summary)"}
    for _ in range(5):
        if not _safe_public_url(current_url):
            return ""
        response = requests.get(
            current_url,
            timeout=timeout,
            headers=headers,
            allow_redirects=False,
            stream=True,
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location", "")
            current_url = urljoin(current_url, location)
            continue
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").casefold()
        if content_type and "html" not in content_type:
            return ""
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            raw.extend(chunk)
            if len(raw) >= MAX_DOWNLOAD_BYTES:
                break
        # Một số báo trả sai dạng "utf-8,gbk". Dùng charset hợp lệ đầu tiên;
        # không gọi apparent_encoding sau iter_content vì body stream đã được đọc.
        encoding = str(response.encoding or "utf-8").split(",", 1)[0].strip()
        try:
            codecs.lookup(encoding)
        except LookupError:
            encoding = "utf-8"
        return _extract_article_text(
            bytes(raw[:MAX_DOWNLOAD_BYTES]).decode(encoding, errors="replace"),
            expected_title=expected_title,
        )
    return ""
