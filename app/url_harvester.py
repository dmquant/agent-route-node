"""
Web Source URL Harvester & ROS Archiver (`app/url_harvester.py`).

Scoped strictly to the three primary research hands:
    HARVEST_ENABLED_HANDS = {"claude", "codex", "agy"}

What it does:
1. After a `claude`, `codex`, or `agy` task completes in `task_puller.py`,
   scans the task's `output` text and any newly written `.md` / `.json` files
   in `workspace_dir` for cited external web URLs (`http://`, `https://`).
2. Filters out internal infrastructure hosts (`localhost`, `127.0.0.1`, `*.local`,
   `*.dmquant.net`, `*.infloop.ai`, `*.workers.dev`), XML/JSON schema URIs,
   and static media/code assets (`.png`, `.jpg`, `.css`, `.js`, etc.).
3. Downloads up to `URL_HARVEST_MAX_URLS` (default 25) unique URLs concurrently
   (concurrency 5, timeout 12s, max 2 MB per URL), checking `refusal_envelope()`
   and WAF/bot-challenge signatures so refusal/challenge pages are never stored
   as fake source documents.
4. Converts HTML into clean readable Markdown (preserving `<title>`, meta
   description, headings, links, tables, and article text) and writes each
   verified source to:
       `workspace_dir/sources/<idx:02d>_<domain>_<sha8>.md`
   along with a complete provenance manifest:
       `workspace_dir/sources/index.json`
5. Because `harvest_urls_for_task()` runs right before `sync_session_now()`,
   `workspace_sync.py` automatically uploads `sources/index.json` and every
   `sources/*.md` to R2 (`POST /api/sessions/<session_id>/workspace/upload`),
   and `@ai-institute`'s `archiveSessionWorkspace` archives and indexes them
   into ROS (`sessions/<session_id>/sources/...`, D1 `session_files`, and Vectorize).
"""

import asyncio
import hashlib
import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.institute_files import refusal_envelope

_logger = logging.getLogger("agent-route.url-harvester")

# Strictly limited to Claude Code, Codex, and AGY hands per operator directive
HARVEST_ENABLED_HANDS = frozenset({"claude", "codex", "agy"})

MAX_URLS_PER_TASK = int(os.getenv("URL_HARVEST_MAX_URLS", "25"))
FETCH_CONCURRENCY = int(os.getenv("URL_HARVEST_CONCURRENCY", "5"))
FETCH_TIMEOUT_S = float(os.getenv("URL_HARVEST_TIMEOUT_S", "12.0"))
MAX_SOURCE_BYTES = int(os.getenv("URL_HARVEST_MAX_BYTES", str(2 * 1024 * 1024)))  # 2 MB

_URL_RE = re.compile(r"https?://[^\s<>\"'`)\]]+", re.IGNORECASE)

_SKIP_HOST_SUFFIXES = (
    ".local",
    ".internal",
    ".dmquant.net",
    ".infloop.ai",
    ".workers.dev",
    ".r2.cloudflarestorage.com",
)

_SKIP_EXACT_HOSTS = frozenset({
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "schemas.openxmlformats.org",
    "www.w3.org",
    "json-schema.org",
    "purl.org",
    "example.com",
    "www.example.com",
})

_SKIP_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".css", ".js", ".mjs", ".map", ".woff", ".woff2", ".ttf", ".eot",
    ".mp4", ".mp3", ".wav", ".ogg", ".webm", ".avi", ".mov",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".dmg", ".pkg",
})

_BOT_CHALLENGE_PATTERNS = (
    re.compile(r"<title[^>]*>\s*(Just a moment\.\.\.|Attention Required! \| Cloudflare|Access Denied|403 Forbidden|Robot or human\?|Security Check)\s*</title>", re.I),
    re.compile(r"cf-browser-verification|cf_chl_opt|challenge-platform/h/b/orchestrate", re.I),
    re.compile(r"Please enable JS and disable any ad blocker", re.I),
)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


def _clean_url(raw: str) -> str:
    # Strip trailing markdown/prose punctuation and unbalanced parens/brackets
    u = raw.strip()
    while u and u[-1] in ".,;:!?'\"`>":
        u = u[:-1]
    while u.endswith(")") and u.count(")") > u.count("("):
        u = u[:-1]
    while u.endswith("]") and u.count("]") > u.count("["):
        u = u[:-1]
    # Strip URL fragment
    if "#" in u:
        u = u.split("#", 1)[0]
    return u


def _is_harvestable_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower().strip(".")
    if not host or "." not in host:
        return False
    if host in _SKIP_EXACT_HOSTS:
        return False
    if any(host.endswith(suf) for suf in _SKIP_HOST_SUFFIXES):
        return False
    if host.startswith(("10.", "192.168.", "127.", "169.254.")):
        return False
    path_lower = (parsed.path or "").lower()
    ext = os.path.splitext(path_lower)[1]
    if ext in _SKIP_EXTENSIONS:
        return False
    return True


def extract_urls_from_task(output_text: str, workspace_dir: str) -> list[str]:
    """Collect unique, ordered external source URLs from task output and workspace reports."""
    candidates: list[str] = []
    seen: set[str] = set()

    def _scan_text(text: str) -> None:
        if not text:
            return
        for match in _URL_RE.findall(text):
            cleaned = _clean_url(match)
            if cleaned and cleaned not in seen and _is_harvestable_url(cleaned):
                seen.add(cleaned)
                candidates.append(cleaned)

    _scan_text(output_text or "")

    if workspace_dir and os.path.isdir(workspace_dir):
        for root, dirs, files in os.walk(workspace_dir):
            dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", "sources", ".venv"}]
            for fn in sorted(files):
                if not fn.endswith((".md", ".json", ".txt")) or fn.endswith(".NOT_PUBLISHED.md"):
                    continue
                fpath = os.path.join(root, fn)
                try:
                    if os.path.getsize(fpath) > 2 * 1024 * 1024:
                        continue
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        _scan_text(f.read())
                except OSError:
                    pass

    return candidates[:MAX_URLS_PER_TASK]


def _html_to_readable_markdown(raw_html: str) -> tuple[str, str, str]:
    """Extract (title, meta_description, readable_markdown) from HTML using stdlib."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw_html[:15000], re.I | re.S)
    title = html.unescape(re.sub(r"\s+", " ", title_match.group(1)).strip())[:200] if title_match else ""

    desc_match = re.search(
        r'<meta\s+(?:name|property)=["\'](?:description|og:description)["\']\s+content=["\'](.*?)["\']',
        raw_html[:25000],
        re.I | re.S,
    )
    meta_desc = html.unescape(re.sub(r"\s+", " ", desc_match.group(1)).strip())[:400] if desc_match else ""

    # Remove non-content blocks
    cleaned = re.sub(r"<!--.*?-->", " ", raw_html, flags=re.S)
    for tag in ("script", "style", "noscript", "svg", "iframe", "canvas", "nav", "footer"):
        cleaned = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", " ", cleaned, flags=re.I | re.S)

    # Convert common structural tags to Markdown
    for level in range(1, 7):
        hashes = "#" * level
        cleaned = re.sub(
            rf"<h{level}\b[^>]*>(.*?)</h{level}>",
            lambda m, h=hashes: f"\n\n{h} {_strip_tags(m.group(1))}\n\n",
            cleaned,
            flags=re.I | re.S,
        )
    cleaned = re.sub(
        r"<li\b[^>]*>(.*?)</li>",
        lambda m: f"\n- {_strip_tags(m.group(1))}",
        cleaned,
        flags=re.I | re.S,
    )
    cleaned = re.sub(r"<(?:p|div|section|article|tr|br|hr)\b[^>]*>", "\n", cleaned, flags=re.I)
    cleaned = re.sub(r"</(?:p|div|section|article|tr|table|ul|ol|blockquote)>", "\n", cleaned, flags=re.I)

    text = _strip_tags(cleaned)
    lines = []
    for line in text.splitlines():
        s = re.sub(r"[ \t]+", " ", line).strip()
        if s:
            lines.append(s)
    body_md = "\n\n".join(lines)
    # Cap extracted markdown at 120,000 chars so individual files stay well under R2's 10MB cap
    if len(body_md) > 120_000:
        body_md = body_md[:120_000] + "\n\n...[truncated at 120,000 chars]..."
    return title, meta_desc, body_md


def _strip_tags(fragment: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", fragment)
    return html.unescape(no_tags).strip()


def _is_bot_challenge(text: str) -> Optional[str]:
    sample = text[:8000]
    for pat in _BOT_CHALLENGE_PATTERNS:
        m = pat.search(sample)
        if m:
            return f"bot/WAF challenge detected ({m.group(0)[:80]})"
    return None


def _safe_domain_slug(url: str) -> str:
    host = (urlparse(url).hostname or "web").lower()
    if host.startswith("www."):
        host = host[4:]
    return re.sub(r"[^a-z0-9.-]+", "_", host)[:40].strip("._") or "web"


async def _fetch_and_save_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    idx: int,
    url: str,
    sources_dir: str,
    hand_name: str,
    task_id: str,
    session_id: str,
) -> dict:
    url_hash8 = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    domain_slug = _safe_domain_slug(url)
    fetched_at = datetime.now(timezone.utc).isoformat()
    record = {
        "index": idx,
        "url": url,
        "domain": domain_slug,
        "fetched_at": fetched_at,
        "status": "pending",
        "http_status": None,
        "content_type": None,
        "title": "",
        "raw_sha256": None,
        "byte_length": 0,
        "local_path": None,
        "r2_key": None,
        "error": None,
    }

    async with sem:
        try:
            resp = await client.get(
                url,
                follow_redirects=True,
                timeout=FETCH_TIMEOUT_S,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/json,text/plain,text/markdown;q=0.9,*/*;q=0.5",
                    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                },
            )
            record["http_status"] = resp.status_code
            record["final_url"] = str(resp.url)
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            record["content_type"] = ctype

            if resp.status_code < 200 or resp.status_code >= 300:
                record["status"] = "http_error"
                record["error"] = f"HTTP {resp.status_code}"
                return record

            raw_bytes = resp.content
            if not raw_bytes:
                record["status"] = "empty"
                record["error"] = "0 bytes returned"
                return record
            if len(raw_bytes) > MAX_SOURCE_BYTES:
                raw_bytes = raw_bytes[:MAX_SOURCE_BYTES]

            raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            record["raw_sha256"] = raw_sha256
            record["byte_length"] = len(raw_bytes)

            # Handle binary PDF metadata stub vs text/html/json
            if ctype == "application/pdf" or url.lower().endswith(".pdf"):
                # Verify PDF magic header %PDF-
                if not raw_bytes.startswith(b"%PDF-"):
                    record["status"] = "invalid_pdf"
                    record["error"] = "Declared PDF but missing %PDF- header"
                    return record
                rel_path = f"sources/{idx:02d}_{domain_slug}_{url_hash8}.md"
                abs_path = os.path.join(sources_dir, f"{idx:02d}_{domain_slug}_{url_hash8}.md")
                md_doc = (
                    f"---\n"
                    f"url: {json.dumps(url)}\n"
                    f"final_url: {json.dumps(str(resp.url))}\n"
                    f"domain: {json.dumps(domain_slug)}\n"
                    f"content_type: \"application/pdf\"\n"
                    f"fetched_at: {json.dumps(fetched_at)}\n"
                    f"http_status: {resp.status_code}\n"
                    f"raw_sha256: {json.dumps(raw_sha256)}\n"
                    f"byte_length: {len(raw_bytes)}\n"
                    f"hand: {json.dumps(hand_name)}\n"
                    f"task_id: {json.dumps(task_id)}\n"
                    f"session_id: {json.dumps(session_id)}\n"
                    f"---\n\n"
                    f"# Archived PDF Source Verification ({domain_slug})\n\n"
                    f"- **Source URL**: {url}\n"
                    f"- **Verified PDF Header**: `%PDF-` ({len(raw_bytes):,} bytes)\n"
                    f"- **SHA-256**: `{raw_sha256}`\n"
                )
                with open(abs_path, "w", encoding="utf-8") as fh:
                    fh.write(md_doc)
                record["status"] = "archived"
                record["title"] = f"PDF Document ({domain_slug})"
                record["local_path"] = rel_path
                record["r2_key"] = f"sessions/{session_id}/{rel_path}"
                return record

            decoded = raw_bytes.decode("utf-8", errors="replace")

            # Guard 1: Check refusal envelope (401/403 JSON error bodies)
            refusal = refusal_envelope(decoded)
            if refusal:
                record["status"] = "refused"
                record["error"] = refusal
                return record

            # Guard 2: Check WAF / bot challenge pages
            challenge = _is_bot_challenge(decoded)
            if challenge:
                record["status"] = "bot_challenge"
                record["error"] = challenge
                return record

            if "html" in ctype or decoded.lstrip()[:100].lower().startswith(("<!doctype html", "<html", "<head", "<meta")):
                title, meta_desc, body_md = _html_to_readable_markdown(decoded)
            else:
                title = f"{domain_slug} ({ctype or 'text'})"
                meta_desc = ""
                body_md = decoded[:120_000]

            if len(body_md.strip()) < 40:
                record["status"] = "too_short"
                record["error"] = f"Extracted body too short ({len(body_md.strip())} chars)"
                return record

            rel_path = f"sources/{idx:02d}_{domain_slug}_{url_hash8}.md"
            abs_path = os.path.join(sources_dir, f"{idx:02d}_{domain_slug}_{url_hash8}.md")
            md_doc = (
                f"---\n"
                f"url: {json.dumps(url)}\n"
                f"final_url: {json.dumps(str(resp.url))}\n"
                f"domain: {json.dumps(domain_slug)}\n"
                f"title: {json.dumps(title)}\n"
                f"description: {json.dumps(meta_desc)}\n"
                f"content_type: {json.dumps(ctype or 'text/html')}\n"
                f"fetched_at: {json.dumps(fetched_at)}\n"
                f"http_status: {resp.status_code}\n"
                f"raw_sha256: {json.dumps(raw_sha256)}\n"
                f"byte_length: {len(raw_bytes)}\n"
                f"hand: {json.dumps(hand_name)}\n"
                f"task_id: {json.dumps(task_id)}\n"
                f"session_id: {json.dumps(session_id)}\n"
                f"---\n\n"
                f"# {title or url}\n\n"
                f"- **Source URL**: {url}\n"
                f"- **Fetched At**: `{fetched_at}`\n"
                f"- **Raw SHA-256**: `{raw_sha256}` ({len(raw_bytes):,} bytes)\n"
                + (f"- **Description**: {meta_desc}\n\n" if meta_desc else "\n")
                + "---\n\n"
                + body_md
                + "\n"
            )
            with open(abs_path, "w", encoding="utf-8") as fh:
                fh.write(md_doc)

            record["status"] = "archived"
            record["title"] = title
            record["local_path"] = rel_path
            record["r2_key"] = f"sessions/{session_id}/{rel_path}"
            return record

        except Exception as e:
            record["status"] = "fetch_error"
            record["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            return record


async def harvest_urls_for_task(
    hand_name: str,
    task_id: str,
    session_id: Optional[str],
    output_text: str,
    workspace_dir: str,
) -> Optional[dict]:
    """Extract, download, and save cited web sources into `workspace_dir/sources/`
    for `claude`, `codex`, and `agy` hands so `workspace_sync` uploads them to ROS/R2.
    """
    if hand_name not in HARVEST_ENABLED_HANDS:
        return None
    if os.getenv("ENABLE_URL_HARVEST", "true").lower() in ("false", "0", "no"):
        return None

    urls = extract_urls_from_task(output_text, workspace_dir)
    if not urls:
        return None

    sid = session_id or task_id
    sources_dir = os.path.join(workspace_dir, "sources")
    os.makedirs(sources_dir, exist_ok=True)

    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    async with httpx.AsyncClient() as client:
        tasks = [
            _fetch_and_save_one(client, sem, idx + 1, u, sources_dir, hand_name, task_id, sid)
            for idx, u in enumerate(urls)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    archived_count = sum(1 for r in results if r.get("status") == "archived")
    manifest = {
        "schema": "ros.edge.url_harvest.v1",
        "task_id": task_id,
        "session_id": sid,
        "hand": hand_name,
        "harvested_at": datetime.now(timezone.utc).isoformat(),
        "total_urls_discovered": len(urls),
        "archived_count": archived_count,
        "sources": results,
    }
    index_path = os.path.join(sources_dir, "index.json")
    try:
        with open(index_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
    except OSError as e:
        _logger.warning("Could not write sources/index.json for %s: %s", task_id[:12], e)

    _logger.info(
        "URL-HARVEST %s | hand=%s | discovered=%d | archived=%d -> %s/sources/",
        task_id[:12], hand_name, len(urls), archived_count, sid,
    )
    print(
        f"[url-harvester] {task_id[:8]} ({hand_name}): archived {archived_count}/{len(urls)} "
        f"cited web URL(s) into workspace/sources/ for ROS sync"
    )
    return manifest
