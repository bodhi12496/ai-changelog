#!/usr/bin/env python3
"""
AI Changelog collector.

Pulls new AI / data-science papers, GitHub repos, Hugging Face models,
tools and news from free public sources, merges them into a rolling
archive, and writes the files the website reads:

    site/data/items.json   all items (rolling window, newest first)
    site/data/status.json  per-source health for the status panel
    site/data/feed.xml     RSS feed of the latest items

Standard library only, so it runs anywhere Python 3.9+ does.
Usage:  python collector/collect.py
Env:    GITHUB_TOKEN (optional, raises GitHub API rate limits)
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from summarize import summarize_items  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "collector" / "sources.json"
DATA_DIR = ROOT / "site" / "data"
ITEMS_PATH = DATA_DIR / "items.json"
STATUS_PATH = DATA_DIR / "status.json"
FEED_PATH = DATA_DIR / "feed.xml"

SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
USER_AGENT = "ai-changelog/1.0 (+https://github.com/; open-source AI news aggregator)"
NOW = datetime.now(timezone.utc)

AI_KEYWORDS = re.compile(
    r"\b(ai|a\.i\.|agi|llms?|gpts?|chatgpt|ml|machine[- ]learning|deep[- ]learning|"
    r"neural|transformers?|diffusion|agents?|agentic|rag|embeddings?|claude|gemini|"
    r"llama|mistral|openai|anthropic|deepseek|qwen|hugging ?face|data[- ]science|"
    r"pandas|polars|pytorch|tensorflow|jax|kaggle|datasets?|inference|fine-?tun\w*|"
    r"mcp|chatbots?|copilot|computer vision|nlp|jupyter|duckdb|vector (db|database|search)|"
    r"reinforcement learning|robotics|multimodal|text-to-\w+|speech recognition|ocr)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- helpers

def http_get(url: str, headers: dict | None = None, timeout: int = 30, retries: int = 2) -> bytes:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise last_err  # pragma: no cover


def get_json(url: str, headers: dict | None = None):
    return json.loads(http_get(url, headers).decode("utf-8"))


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return WS_RE.sub(" ", s).strip()


def short(s: str | None, limit: int = 280) -> str:
    """First sentence or two, capped at `limit` characters."""
    s = clean_text(s)
    if len(s) <= limit:
        return s
    sentences = re.split(r"(?<=[.!?])\s+", s)
    out = ""
    for sent in sentences:
        if len(out) + len(sent) + 1 > limit:
            break
        out = f"{out} {sent}".strip()
    if not out:
        out = s[: limit - 1].rsplit(" ", 1)[0] + "…"
    return out


def iso(dt: datetime | None) -> str:
    dt = dt or NOW
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


def item(*, id, type, title, url, source, description="", published=None,
         tags=None, score=None, score_label=None, discussion=None, authors=None) -> dict:
    return {
        "id": id,
        "type": type,
        "title": clean_text(title)[:300],
        "description": short(description),
        "url": url,
        "source": source,
        "published": iso(published),
        "tags": [t for t in (tags or []) if t][:6],
        "score": score,
        "score_label": score_label,
        "discussion": discussion,
        "authors": (authors or [])[:4],
    }


def humanize(n: int | float | None) -> str:
    if n is None:
        return "0"
    n = float(n)
    for unit, div in (("M", 1_000_000), ("k", 1_000)):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + unit
    return str(int(n))


# ---------------------------------------------------------------- arXiv

ATOM = "{http://www.w3.org/2005/Atom}"


def arxiv_id_from_url(u: str) -> str:
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", u or "")
    return m.group(1) if m else (u or "")


def parse_arxiv(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    out = []
    for e in root.findall(f"{ATOM}entry"):
        raw_id = (e.findtext(f"{ATOM}id") or "").strip()
        aid = arxiv_id_from_url(raw_id)
        if not aid:
            continue
        cats = [c.get("term") for c in e.findall(f"{ATOM}category")]
        authors = [clean_text(a.findtext(f"{ATOM}name")) for a in e.findall(f"{ATOM}author")]
        out.append(item(
            id=f"arxiv:{aid}",
            type="paper",
            title=e.findtext(f"{ATOM}title"),
            url=f"https://arxiv.org/abs/{aid}",
            source="arXiv",
            description=e.findtext(f"{ATOM}summary"),
            published=parse_date(e.findtext(f"{ATOM}published")),
            tags=cats,
            authors=authors,
        ))
        # Full abstract, used only as input for the AI summary; stripped before saving.
        out[-1]["_abstract"] = clean_text(e.findtext(f"{ATOM}summary"))[:2000]
    return out


def fetch_arxiv(cfg: dict) -> list[dict]:
    query = " OR ".join(f"cat:{c}" for c in cfg["categories"])
    params = urllib.parse.urlencode({
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": cfg.get("max_results", 150),
    })
    return parse_arxiv(http_get(f"https://export.arxiv.org/api/query?{params}", timeout=60))


# ---------------------------------------------------------------- Hugging Face

def parse_hf_daily_papers(data: list) -> list[dict]:
    out = []
    for row in data or []:
        p = row.get("paper") or row
        aid = p.get("id") or arxiv_id_from_url(p.get("url", ""))
        if not aid:
            continue
        upvotes = p.get("upvotes") or row.get("upvotes") or 0
        authors = [a.get("name", "") for a in p.get("authors", []) if isinstance(a, dict)]
        keywords = p.get("ai_keywords") or []
        out.append(item(
            id=f"arxiv:{arxiv_id_from_url(aid) or aid}",
            type="paper",
            title=p.get("title") or row.get("title"),
            url=f"https://arxiv.org/abs/{aid}",
            source="Hugging Face Papers",
            description=p.get("ai_summary") or p.get("summary") or row.get("summary"),
            published=parse_date(p.get("publishedAt") or row.get("publishedAt")),
            tags=["trending"] + list(keywords)[:4],
            score=upvotes,
            score_label="upvotes",
            discussion=f"https://huggingface.co/papers/{aid}",
            authors=authors,
        ))
    return out


def fetch_hf_papers(cfg: dict) -> list[dict]:
    return parse_hf_daily_papers(get_json("https://huggingface.co/api/daily_papers?limit=50"))


PIPELINE_NAMES = {
    "text-generation": "Text generation",
    "image-text-to-text": "Vision-language",
    "text-to-image": "Text-to-image",
    "text-to-video": "Text-to-video",
    "text-to-speech": "Text-to-speech",
    "automatic-speech-recognition": "Speech recognition",
    "feature-extraction": "Embeddings",
    "sentence-similarity": "Embeddings",
    "image-to-video": "Image-to-video",
    "any-to-any": "Multimodal",
}


def parse_hf_models(data: list) -> list[dict]:
    out = []
    for m in data or []:
        mid = m.get("id") or m.get("modelId")
        if not mid:
            continue
        pipe = m.get("pipeline_tag") or ""
        kind = PIPELINE_NAMES.get(pipe, pipe.replace("-", " ").capitalize() if pipe else "Model")
        org = mid.split("/")[0] if "/" in mid else "the community"
        downloads = m.get("downloads") or 0
        likes = m.get("likes") or 0
        desc = f"{kind} model from {org}. {humanize(downloads)} downloads and {humanize(likes)} likes so far."
        lib = m.get("library_name")
        tags = [pipe, lib] + [t for t in (m.get("tags") or []) if ":" not in t and t not in (pipe, lib)][:3]
        out.append(item(
            id=f"hf-model:{mid}",
            type="model",
            title=mid,
            url=f"https://huggingface.co/{mid}",
            source="Hugging Face",
            description=desc,
            published=parse_date(m.get("createdAt") or m.get("lastModified")),
            tags=tags,
            score=likes,
            score_label="likes",
        ))
    return out


def fetch_hf_models(cfg: dict) -> list[dict]:
    url = f"https://huggingface.co/api/models?sort=trendingScore&direction=-1&limit={cfg.get('limit', 40)}"
    return parse_hf_models(get_json(url))


def parse_hf_spaces(data: list) -> list[dict]:
    out = []
    for s in data or []:
        sid = s.get("id")
        if not sid:
            continue
        card = s.get("cardData") or {}
        desc = (card.get("short_description") or s.get("shortDescription")
                or card.get("title") or s.get("title") or "")
        sdk = s.get("sdk") or card.get("sdk") or ""
        if not desc:
            desc = f"Interactive {sdk or 'demo'} app on Hugging Face Spaces."
        out.append(item(
            id=f"hf-space:{sid}",
            type="tool",
            title=card.get("title") or sid.split("/")[-1].replace("-", " "),
            url=f"https://huggingface.co/spaces/{sid}",
            source="Hugging Face Spaces",
            description=desc,
            published=parse_date(s.get("createdAt") or s.get("lastModified")),
            tags=["demo", sdk],
            score=s.get("likes") or 0,
            score_label="likes",
        ))
    return out


def fetch_hf_spaces(cfg: dict) -> list[dict]:
    url = f"https://huggingface.co/api/spaces?sort=trendingScore&direction=-1&limit={cfg.get('limit', 30)}&full=true"
    return parse_hf_spaces(get_json(url))


# ---------------------------------------------------------------- GitHub

def parse_github(data: dict) -> list[dict]:
    out = []
    for r in (data or {}).get("items", []):
        full = r.get("full_name")
        if not full or r.get("fork") or r.get("archived"):
            continue
        desc = r.get("description") or "No description provided."
        out.append(item(
            id=f"gh:{full.lower()}",
            type="repo",
            title=full,
            url=r.get("html_url") or f"https://github.com/{full}",
            source="GitHub",
            description=desc,
            published=parse_date(r.get("created_at")),
            tags=([r.get("language")] if r.get("language") else []) + (r.get("topics") or [])[:5],
            score=r.get("stargazers_count") or 0,
            score_label="stars",
        ))
    return out


def fetch_github(cfg: dict) -> list[dict]:
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    since = (NOW - timedelta(days=cfg.get("lookback_days", 7))).strftime("%Y-%m-%d")
    results: dict[str, dict] = {}
    errors = []
    for i, topic in enumerate(cfg["topics"]):
        q = f"topic:{topic} created:>={since} stars:>={cfg.get('min_stars', 10)}"
        params = urllib.parse.urlencode({"q": q, "sort": "stars", "order": "desc",
                                         "per_page": cfg.get("per_topic", 25)})
        try:
            for it in parse_github(get_json(f"https://api.github.com/search/repositories?{params}", headers)):
                results.setdefault(it["id"], it)
        except Exception as e:  # keep going with other topics
            errors.append(f"{topic}: {e}")
        # Search API: 30 req/min with a token, 10 without.
        if i < len(cfg["topics"]) - 1:
            time.sleep(2.5 if token else 7)
    if errors and not results:
        raise RuntimeError("; ".join(errors))
    return list(results.values())


# ---------------------------------------------------------------- Hacker News

def parse_hn(data: dict, *, type: str, keyword_filter: bool = True) -> list[dict]:
    out = []
    for h in (data or {}).get("hits", []):
        title = h.get("title") or ""
        text = clean_text(h.get("story_text"))
        if keyword_filter and not AI_KEYWORDS.search(f"{title} {text[:300]}"):
            continue
        oid = h.get("objectID")
        hn_url = f"https://news.ycombinator.com/item?id={oid}"
        url = h.get("url") or hn_url
        points, comments = h.get("points") or 0, h.get("num_comments") or 0
        if text:
            desc = text
        else:
            domain = urllib.parse.urlparse(url).netloc.replace("www.", "")
            desc = (f"Launched on Show HN. {points} points and {comments} comments so far."
                    if type == "tool" else
                    f"Trending on Hacker News from {domain}. {points} points and {comments} comments.")
        clean_title = re.sub(r"^Show HN:\s*", "", title)
        out.append(item(
            id=f"hn:{oid}",
            type=type,
            title=clean_title,
            url=url,
            source="Show HN" if type == "tool" else "Hacker News",
            description=desc,
            published=datetime.fromtimestamp(h.get("created_at_i") or NOW.timestamp(), tz=timezone.utc),
            tags=["launch"] if type == "tool" else [],
            score=points,
            score_label="points",
            discussion=hn_url,
        ))
    return out


def fetch_hn(cfg: dict) -> list[dict]:
    since = int((NOW - timedelta(hours=cfg.get("lookback_hours", 48))).timestamp())
    base = "https://hn.algolia.com/api/v1/search_by_date"
    show = urllib.parse.urlencode({
        "tags": "show_hn", "hitsPerPage": 300,
        "numericFilters": f"points>={cfg.get('show_hn_min_points', 15)},created_at_i>{since}",
    })
    news = urllib.parse.urlencode({
        "tags": "story", "hitsPerPage": 500,
        "numericFilters": f"points>={cfg.get('news_min_points', 100)},created_at_i>{since}",
    })
    tools = parse_hn(get_json(f"{base}?{show}"), type="tool")
    tool_ids = {t["id"] for t in tools}
    stories = [s for s in parse_hn(get_json(f"{base}?{news}"), type="news") if s["id"] not in tool_ids]
    return tools + stories


# ---------------------------------------------------------------- RSS / Atom

def parse_feed(xml_bytes: bytes, source_name: str) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    out = []
    # RSS 2.0
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        desc = it.findtext("description") or it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
        cats = [clean_text(c.text) for c in it.findall("category") if c.text]
        if not link:
            continue
        out.append(item(
            id=f"rss:{guid or link}",
            type="news",
            title=it.findtext("title"),
            url=link,
            source=source_name,
            description=desc,
            published=parse_date(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date")),
            tags=cats[:3],
        ))
    # Atom
    for e in root.iter(f"{ATOM}entry"):
        link = ""
        for l in e.findall(f"{ATOM}link"):
            if l.get("rel", "alternate") == "alternate":
                link = l.get("href", "")
                break
        if not link:
            continue
        desc = e.findtext(f"{ATOM}summary") or e.findtext(f"{ATOM}content")
        out.append(item(
            id=f"rss:{(e.findtext(f'{ATOM}id') or link).strip()}",
            type="news",
            title=e.findtext(f"{ATOM}title"),
            url=link,
            source=source_name,
            description=desc,
            published=parse_date(e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated")),
            tags=[c.get("term") for c in e.findall(f"{ATOM}category")][:3],
        ))
    return out


# ---------------------------------------------------------------- merge & write

def load_existing() -> list[dict]:
    try:
        return json.loads(ITEMS_PATH.read_text("utf-8")).get("items", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def merge(existing: list[dict], fresh: list[dict], cfg: dict) -> list[dict]:
    by_id = {it["id"]: it for it in existing}
    now_s = iso(NOW)
    for it in fresh:
        old = by_id.get(it["id"])
        if old is None:
            it["first_seen"] = now_s
            by_id[it["id"]] = it
            continue
        # Update live numbers, keep first_seen; merge tags (e.g. arXiv + HF "trending").
        merged = {**old}
        for k in ("score", "score_label", "discussion"):
            if it.get(k) is not None:
                merged[k] = it[k]
        merged["tags"] = list(dict.fromkeys((it.get("tags") or []) + (old.get("tags") or [])))[:6]
        if it["source"] == "Hugging Face Papers":
            merged["source"] = it["source"]
            if it.get("description"):
                merged["description"] = it["description"]
        if not merged.get("description"):
            merged["description"] = it.get("description", "")
        if it.get("_abstract") and "ai_summary" not in merged:
            merged["_abstract"] = it["_abstract"]
        by_id[it["id"]] = merged

    cutoff = NOW - timedelta(days=cfg.get("retention_days", 30))
    kept = []
    for it in by_id.values():
        seen = parse_date(it.get("first_seen")) or NOW
        if seen >= cutoff:
            kept.append(it)

    caps = cfg.get("max_items_per_type", {})
    kept.sort(key=lambda x: (x.get("first_seen", ""), x.get("published", "")), reverse=True)
    counts: dict[str, int] = {}
    final = []
    for it in kept:
        counts[it["type"]] = counts.get(it["type"], 0) + 1
        if counts[it["type"]] <= caps.get(it["type"], 10_000):
            final.append(it)
    return final


def write_feed(items: list[dict]) -> None:
    def esc(s):
        return html.escape(s or "", quote=True)
    rows = []
    for it in items[:150]:
        pub = parse_date(it.get("published")) or NOW
        rows.append(
            "<item>"
            f"<title>{esc('[' + it['type'] + '] ' + it['title'])}</title>"
            f"<link>{esc(it['url'])}</link>"
            f"<guid isPermaLink=\"false\">{esc(it['id'])}</guid>"
            f"<pubDate>{format_datetime(pub)}</pubDate>"
            f"<category>{esc(it['type'])}</category>"
            f"<description>{esc(it.get('ai_summary') or it.get('description'))}</description>"
            "</item>"
        )
    link = SITE_URL or "https://github.com/"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        "<title>AI Changelog</title>"
        f"<link>{esc(link)}</link>"
        "<description>New AI and data science papers, repos, models, tools and news.</description>"
        f"<lastBuildDate>{format_datetime(NOW)}</lastBuildDate>"
        + "".join(rows) +
        "</channel></rss>\n"
    )
    FEED_PATH.write_text(xml, "utf-8")


def run_source(name: str, fn, cfg, status: dict, bucket: list) -> None:
    t0 = time.time()
    try:
        got = fn(cfg)
        bucket.extend(got)
        status[name] = {"ok": True, "count": len(got), "ms": int((time.time() - t0) * 1000)}
        print(f"  ok   {name:<28} {len(got):>4} items")
    except Exception as e:
        status[name] = {"ok": False, "count": 0, "error": f"{type(e).__name__}: {e}"[:200]}
        print(f"  FAIL {name:<28} {e}", file=sys.stderr)
        if os.environ.get("DEBUG"):
            traceback.print_exc()


def main() -> int:
    cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fresh: list[dict] = []
    status: dict[str, dict] = {}
    print(f"Collecting at {iso(NOW)}")

    # arXiv first, HF papers after, so HF's summaries/upvotes merge onto arXiv entries.
    plan = [
        ("arXiv", fetch_arxiv, cfg.get("arxiv", {})),
        ("Hugging Face Papers", fetch_hf_papers, cfg.get("huggingface_papers", {})),
        ("Hugging Face Models", fetch_hf_models, cfg.get("huggingface_models", {})),
        ("Hugging Face Spaces", fetch_hf_spaces, cfg.get("huggingface_spaces", {})),
        ("GitHub", fetch_github, cfg.get("github", {})),
        ("Hacker News", fetch_hn, cfg.get("hackernews", {})),
    ]
    for name, fn, sub in plan:
        if sub.get("enabled", True):
            run_source(name, fn, sub, status, fresh)

    for feed in cfg.get("rss", []):
        def fetch_feed(f=feed):
            entries = parse_feed(http_get(f["url"]), f["name"])
            cutoff = NOW - timedelta(days=cfg.get("retention_days", 30))
            return [e for e in entries if (parse_date(e["published"]) or NOW) >= cutoff]
        run_source(feed["name"], lambda _c, fn=fetch_feed: fn(), None, status, fresh)

    # Dedupe within this run (e.g. HF paper also on arXiv): later wins on merge.
    deduped: dict[str, dict] = {}
    for it in fresh:
        if it["id"] in deduped:
            deduped[it["id"]] = merge([deduped[it["id"]]], [it], {"retention_days": 3650})[0]
        else:
            deduped[it["id"]] = it

    items = merge(load_existing(), list(deduped.values()), cfg)
    ok_sources = sum(1 for s in status.values() if s["ok"])

    if items:
        try:
            status["AI summaries"] = summarize_items(items, cfg.get("ai_summaries", {}))
        except Exception as e:   # summaries are a bonus; never lose the run over them
            status["AI summaries"] = {"ok": False, "count": 0, "error": f"{type(e).__name__}: {e}"[:200]}
            print(f"  FAIL AI summaries: {e}", file=sys.stderr)

    if not items:
        print("No items collected; leaving existing data untouched.", file=sys.stderr)
        return 1

    for it in items:
        it.pop("_abstract", None)   # summary input only; keeps items.json small

    ITEMS_PATH.write_text(json.dumps(
        {"generated_at": iso(NOW), "count": len(items), "items": items},
        ensure_ascii=False, separators=(",", ":")), "utf-8")
    STATUS_PATH.write_text(json.dumps(
        {"generated_at": iso(NOW), "sources": status}, indent=1), "utf-8")
    write_feed(items)
    print(f"Wrote {len(items)} items from {ok_sources} working sources.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
