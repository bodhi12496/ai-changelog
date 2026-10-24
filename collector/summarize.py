"""
AI-written one-line summaries for repos, tools and news.

Works with any OpenAI-compatible chat API. The default provider is GitHub
Models, which is free and needs no signup when run inside GitHub Actions:
the workflow's built-in GITHUB_TOKEN has access once the workflow grants
`models: read`. Gemini and Groq (both have free tiers) are optional
fallbacks; add GEMINI_API_KEY or GROQ_API_KEY as repository secrets to use them.

Each item is summarised once and the result is kept in items.json, so later
runs only pay for new items. Requests are batched and spaced out to stay
well inside free-tier rate limits.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

USER_AGENT = "ai-changelog/1.0 (open-source AI news aggregator)"

SYSTEM_PROMPT = """You write one-line summaries for a public feed of new AI and data science items.

For each item, write ONE plain-English sentence of 12 to 28 words that says what it is and what it is useful for.
Rules:
- Use only facts stated in the provided text. Never invent numbers, benchmarks, names or claims.
- Start with the subject or an action, e.g. "Open-source library that..." or "OpenAI releases...". Do not start with "This repository", "This article" or "The author".
- No hype words (revolutionary, game-changing), no emojis, no markdown.
- If the text does not say enough to tell what the item is, return an empty string for it.

Reply with JSON only, in this exact shape:
{"summaries": {"<item key>": "<sentence>", ...}}"""


# ---------------------------------------------------------------- context gathering

class _TextExtractor(HTMLParser):
    """Pulls meta descriptions and paragraph text out of an HTML page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: list[str] = []
        self.paras: list[str] = []
        self._in_p = 0
        self._skip = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "og:description", "twitter:description") and a.get("content"):
                self.meta.append(a["content"])
        elif tag in ("script", "style", "nav", "footer", "header", "aside"):
            self._skip += 1
        elif tag == "p" and not self._skip:
            self._in_p += 1
            self._buf = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header", "aside") and self._skip:
            self._skip -= 1
        elif tag == "p" and self._in_p:
            self._in_p -= 1
            text = " ".join("".join(self._buf).split())
            if len(text) > 60:
                self.paras.append(text)

    def handle_data(self, data):
        if self._in_p and not self._skip:
            self._buf.append(data)


def _get(url: str, headers: dict | None = None, timeout: int = 10, max_bytes: int = 400_000) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(max_bytes), r.headers.get("Content-Type", "")


def page_text(url: str, limit: int = 1500) -> str:
    try:
        body, ctype = _get(url)
    except Exception:
        return ""
    if "html" not in ctype.lower():
        return ""
    p = _TextExtractor()
    try:
        p.feed(body.decode("utf-8", errors="replace"))
    except Exception:
        pass
    parts = list(dict.fromkeys(p.meta[:1] + p.paras[:8]))
    return " ".join(parts)[:limit]


MD_NOISE = [
    (re.compile(r"<!--.*?-->", re.S), " "),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),          # images / badges
    (re.compile(r"\[!\[.*?\]\(.*?\)\]\(.*?\)"), " "),     # linked badges
    (re.compile(r"<[^>]+>"), " "),                        # html tags
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),        # links -> text
    (re.compile(r"```.*?```", re.S), " "),                # code blocks
    (re.compile(r"^[#>*\-\s|:]+", re.M), ""),             # md line prefixes, tables
]


def readme_text(full_name: str, token: str | None, limit: int = 1500) -> str:
    headers = {"Accept": "application/vnd.github.raw+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        body, _ = _get(f"https://api.github.com/repos/{full_name}/readme", headers, max_bytes=60_000)
    except Exception:
        return ""
    text = body.decode("utf-8", errors="replace")
    for pat, rep in MD_NOISE:
        text = pat.sub(rep, text)
    return " ".join(text.split())[:limit]


def build_context(it: dict, gh_token: str | None) -> str:
    """Best available text describing the item, for the model to summarise."""
    base = it.get("description") or ""
    if it["type"] == "repo" and it["id"].startswith("gh:"):
        extra = readme_text(it["title"], gh_token)
        return f"Repo description: {base}\nREADME excerpt: {extra}".strip()
    # Hacker News items only carry a title, so read the linked page.
    if it["id"].startswith("hn:") and it.get("url") and "news.ycombinator.com" not in it["url"]:
        extra = page_text(it["url"])
        if not extra and base.startswith(("Launched on Show HN", "Trending on Hacker News")):
            return ""   # nothing real to summarise from
        return f"{base}\nPage excerpt: {extra}".strip()
    return base


# ---------------------------------------------------------------- API calls

class ProviderExhausted(Exception):
    pass


class Summarizer:
    def __init__(self, cfg: dict, log=print):
        self.cfg = cfg
        self.log = log
        self.providers = [p for p in cfg.get("providers", []) if os.environ.get(p.get("key_env", ""))]
        self.p_idx = 0
        self.m_idx = 0
        self.requests = 0
        self.last_call = 0.0
        self.used_models: set[str] = set()
        self.errors: list[str] = []

    @property
    def available(self) -> bool:
        return self.p_idx < len(self.providers)

    def _current(self):
        p = self.providers[self.p_idx]
        return p, p["models"][self.m_idx]

    def _next_model(self, why: str):
        p, m = self._current()
        self.errors.append(f"{p['name']} / {m}: {why}"[:200])
        self.log(f"    {p['name']} / {m}: {why}; trying next model")
        self.m_idx += 1
        if self.m_idx >= len(p["models"]):
            self._next_provider("no working models left")

    def _next_provider(self, why: str):
        p = self.providers[self.p_idx]
        self.errors.append(f"{p['name']}: {why}"[:200])
        self.log(f"    {p['name']}: {why}; switching provider")
        self.p_idx += 1
        self.m_idx = 0

    def chat(self, user_content: str) -> str:
        while self.available:
            p, model = self._current()
            gap = p.get("min_seconds_between_requests", 4.5)
            wait = self.last_call + gap - time.time()
            if wait > 0:
                time.sleep(wait)
            body = json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.2,
                "max_tokens": 1500,
            }).encode()
            req = urllib.request.Request(p["endpoint"], data=body, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.environ[p['key_env']]}",
                "User-Agent": USER_AGENT,
            })
            self.last_call = time.time()
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self.requests += 1
                self.used_models.add(f"{p['name']} / {model}")
                return data["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as e:
                detail = e.read(400).decode("utf-8", "replace") if e.fp else ""
                if e.code == 429:
                    retry = e.headers.get("Retry-After")
                    if retry and retry.isdigit() and int(retry) <= 30:
                        time.sleep(int(retry) + 1)
                        continue
                    self._next_provider(f"rate limit reached {detail[:100]}")
                elif e.code in (401, 403):
                    self._next_provider(f"not authorised ({e.code}) {detail[:120]}")
                elif e.code in (400, 404, 422):
                    self._next_model(f"HTTP {e.code} {detail[:120]}")
                else:
                    self._next_model(f"HTTP {e.code}")
            except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
                self._next_model(f"{type(e).__name__}")
        raise ProviderExhausted()


def parse_summaries(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    s = data.get("summaries", data)
    return {str(k): v for k, v in s.items() if isinstance(v, str)} if isinstance(s, dict) else {}


def clean_summary(s: str) -> str:
    s = " ".join(s.replace("**", "").split()).strip().strip('"')
    if not (20 <= len(s) <= 320):
        return ""
    if not s.endswith((".", "!", "?")):
        s += "."
    return s


# ---------------------------------------------------------------- entry point

def summarize_items(items: list[dict], cfg: dict, log=print) -> dict:
    """Adds `ai_summary` to items that need one. Mutates items in place."""
    stats = {"ok": True, "count": 0}
    if not cfg.get("enabled", False):
        stats.update(ok=True, note="disabled")
        return stats
    s = Summarizer(cfg, log)
    if not s.available:
        names = ", ".join(p.get("key_env", "?") for p in cfg.get("providers", []))
        log(f"  skip AI summaries: no API key found (looked for {names})")
        stats.update(ok=False, error=f"No API key available ({names})")
        return stats

    types = set(cfg.get("types", ["repo", "news", "tool"]))
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.get("max_age_days", 7))
    batch_size = cfg.get("batch_size", 10)
    max_items = batch_size * cfg.get("max_requests_per_run", 10)

    def fresh(it):
        try:
            seen = datetime.fromisoformat(it.get("first_seen", "").replace("Z", "+00:00"))
        except ValueError:
            return True
        return seen >= cutoff

    todo = [it for it in items
            if it["type"] in types and "ai_summary" not in it and fresh(it)]
    todo.sort(key=lambda it: it.get("score") or 0, reverse=True)   # most popular first
    todo = todo[:max_items]
    if not todo:
        log("  AI summaries: nothing new to summarise")
        return stats

    gh_token = os.environ.get("GITHUB_TOKEN")
    with ThreadPoolExecutor(max_workers=8) as pool:
        contexts = list(pool.map(lambda it: build_context(it, gh_token), todo))

    work = [(it, ctx) for it, ctx in zip(todo, contexts)]
    for it, ctx in work:
        if not ctx.strip():
            it["ai_summary"] = ""   # nothing to work from; don't retry every run

    work = [(it, ctx) for it, ctx in work if ctx.strip()]
    done = 0
    for start in range(0, len(work), batch_size):
        chunk = work[start:start + batch_size]
        payload = [
            {"key": str(i + 1), "type": it["type"], "title": it["title"],
             "source": it["source"], "text": ctx[:1500]}
            for i, (it, ctx) in enumerate(chunk)
        ]
        try:
            reply = s.chat(json.dumps(payload, ensure_ascii=False))
        except ProviderExhausted:
            log("  AI summaries: all providers exhausted for this run; the rest wait for the next run")
            stats["note"] = "stopped early (rate limit or provider unavailable)"
            break
        result = parse_summaries(reply)
        for i, (it, _) in enumerate(chunk):
            text = clean_summary(result.get(str(i + 1), ""))
            it["ai_summary"] = text
            done += bool(text)

    stats["count"] = done
    stats["requests"] = s.requests
    if s.errors:
        stats["errors"] = s.errors[:8]
    if s.requests == 0 and s.errors:
        stats["ok"] = False
        stats["error"] = s.errors[-1]
    if s.used_models:
        stats["model"] = ", ".join(sorted(s.used_models))
    log(f"  ok   {'AI summaries':<28} {done:>4} items ({s.requests} requests)")
    return stats
