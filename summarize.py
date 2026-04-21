#!/usr/bin/env python3
"""
Daily digest of Simon Willison's blog.

Fetches yesterday's posts, scrapes full article text, summarizes by topic
using claude-haiku-3-5, and updates feed.xml as an RSS feed.
"""
import os
import re
import sys
import time
import tempfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

import anthropic

FEED_URL = "https://simonwillison.net/atom/everything/"
FEED_FILE = "feed.xml"
MAX_ITEMS = 30  # keep ~1 month of daily digests
REQUEST_TIMEOUT = 10  # seconds per HTTP request
MAX_RETRIES = 3
RETRY_BACKOFF = 60  # seconds between retries on rate limit

RSS_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("", RSS_NS)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_url(url: str, timeout: int = REQUEST_TIMEOUT) -> str:
    """Fetch URL, return response body as string. Raises on non-200."""
    req = Request(url, headers={"User-Agent": "simon-rss/1.0 (daily digest bot)"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_feed() -> ET.Element:
    """Fetch Simon's Atom feed, return parsed XML root."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"Fetching feed (attempt {attempt})...")
            body = fetch_url(FEED_URL)
            root = ET.fromstring(body)
            print(f"Feed fetched OK.")
            return root
        except HTTPError as e:
            print(f"HTTP {e.code} fetching feed: {e}", file=sys.stderr)
            if attempt == MAX_RETRIES:
                raise
            time.sleep(5 * attempt)
        except URLError as e:
            print(f"Network error fetching feed: {e}", file=sys.stderr)
            if attempt == MAX_RETRIES:
                raise
            time.sleep(5 * attempt)
        except ET.ParseError as e:
            print(f"Feed XML parse error: {e}", file=sys.stderr)
            raise  # no point retrying a parse error


def filter_by_date(root: ET.Element, target_date: date) -> list[dict]:
    """Extract entries published on target_date (UTC). Returns list of dicts."""
    ns = {"atom": RSS_NS}
    entries = []
    for entry in root.findall("atom:entry", ns):
        published_el = entry.find("atom:published", ns)
        if published_el is None or not published_el.text:
            print("Warning: entry missing published timestamp, skipping.", file=sys.stderr)
            continue
        try:
            # Parse ISO 8601 with timezone
            pub_str = published_el.text.strip()
            # Python 3.11+ handles Z, older needs manual replacement
            pub_str = pub_str.replace("Z", "+00:00")
            pub_dt = datetime.fromisoformat(pub_str)
            pub_date = pub_dt.astimezone(timezone.utc).date()
        except ValueError as e:
            print(f"Warning: could not parse timestamp '{published_el.text}': {e}", file=sys.stderr)
            continue

        if pub_date != target_date:
            continue

        title_el = entry.find("atom:title", ns)
        link_el = entry.find("atom:link[@rel='alternate']", ns)
        if link_el is None:
            link_el = entry.find("atom:link", ns)

        tags = [
            cat.get("term", "")
            for cat in entry.findall("atom:category", ns)
            if cat.get("term")
        ]

        # Feed summary as fallback text
        summary_el = entry.find("atom:summary", ns)
        feed_summary = strip_html(summary_el.text or "") if summary_el is not None else ""

        entries.append({
            "title": title_el.text if title_el is not None else "(no title)",
            "url": link_el.get("href", "") if link_el is not None else "",
            "tags": tags,
            "feed_summary": feed_summary,
            "full_text": "",  # filled in by fetch_article_texts()
        })

    return entries


# ---------------------------------------------------------------------------
# Article text scraping
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    """Remove HTML tags and decode entities."""
    # Remove script/style blocks
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove all other tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common HTML entities
    import html as html_module
    text = html_module.unescape(text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_article_text(url: str) -> str | None:
    """
    Fetch a single article page and extract the main text.
    Returns None if the fetch fails or content can't be found.
    Uses the feed summary as fallback (caller's responsibility).
    """
    if not url:
        return None
    try:
        body = fetch_url(url, timeout=REQUEST_TIMEOUT)
    except (HTTPError, URLError, OSError) as e:
        print(f"Warning: could not fetch {url}: {e}", file=sys.stderr)
        return None

    # Simon's blog: content is in <div class="entry entryPage">
    # ends just before <div class="entryFooter">
    m = re.search(
        r'<div class="entry entryPage">(.*?)<div class="entryFooter">',
        body,
        re.DOTALL,
    )
    if not m:
        print(f"Warning: could not find entry content in {url}", file=sys.stderr)
        return None

    text = strip_html(m.group(1))
    return text if text else None


def fetch_article_texts(entries: list[dict]) -> list[dict]:
    """
    Fetch full article text for each entry.
    Falls back to feed_summary if fetch fails.
    If ALL fetches fail, raises RuntimeError.
    """
    success_count = 0
    for entry in entries:
        text = fetch_article_text(entry["url"])
        if text:
            entry["full_text"] = text
            success_count += 1
            print(f"  Fetched: {entry['title'][:60]} ({len(text)} chars)")
        else:
            entry["full_text"] = entry["feed_summary"]
            print(f"  Fallback to feed summary: {entry['title'][:60]}")
        time.sleep(0.5)  # be polite to Simon's server

    if success_count == 0 and entries:
        raise RuntimeError("All article fetches failed — aborting to avoid empty summary.")

    return entries


# ---------------------------------------------------------------------------
# LLM summarization
# ---------------------------------------------------------------------------

def build_prompt(entries: list[dict], target_date: date) -> str:
    posts_text = ""
    for e in entries:
        tags_str = ", ".join(e["tags"]) if e["tags"] else "no tags"
        posts_text += f"\n---\n[TITLE]: {e['title']}\n[URL]: {e['url']}\n[TAGS]: {tags_str}\n[TEXT]: {e['full_text'][:3000]}\n"

    return f"""You are summarizing Simon Willison's blog posts from {target_date.isoformat()}.

Group the posts by topic and write one paragraph per topic group.
Topics should be inferred from the content and tags (for example: "AI & LLMs", "Datasette & Tools", "Open Source Releases", "Interesting Links & Essays").
Write in German. Keep each paragraph to 3-5 sentences. Be concrete — name the specific projects, tools, or ideas Simon discussed.
Use **bold topic headings** before each paragraph.

Here are the posts from that day:
{posts_text}
"""


def call_llm(prompt: str) -> str:
    """Call claude-haiku-3-5, with retry on rate limit. Returns summary text."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"Calling LLM (attempt {attempt})...")
            message = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            result = message.content[0].text.strip()
            if not result:
                raise ValueError("LLM returned empty response")
            print(f"LLM response: {result[:80]}...")
            return result
        except anthropic.RateLimitError:
            print(f"Rate limited. Waiting {RETRY_BACKOFF}s...", file=sys.stderr)
            if attempt == MAX_RETRIES:
                raise
            time.sleep(RETRY_BACKOFF)
        except (anthropic.APIStatusError, anthropic.APITimeoutError) as e:
            print(f"API error: {e}", file=sys.stderr)
            if attempt == MAX_RETRIES:
                raise
            time.sleep(10 * attempt)


# ---------------------------------------------------------------------------
# RSS feed update
# ---------------------------------------------------------------------------

RSS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Simon Willison – Tägliche Zusammenfassung</title>
    <link>https://simonwillison.net/</link>
    <description>Tägliche KI-Zusammenfassung von Simon Willisons Blog</description>
    <language>de</language>
    <atom:link href="" rel="self" type="application/rss+xml"/>
  </channel>
</rss>
"""


def make_rss_item(summary: str, entries: list[dict], target_date: date) -> str:
    """Build an RSS <item> XML string for the daily digest."""
    title = f"Simon Willison – {target_date.strftime('%d. %B %Y')}"
    pub_date = datetime(
        target_date.year, target_date.month, target_date.day,
        12, 0, 0, tzinfo=timezone.utc
    ).strftime("%a, %d %b %Y %H:%M:%S +0000")

    # Build a list of source links for the description footer
    links_html = "\n".join(
        f'<li><a href="{e["url"]}">{e["title"]}</a></li>'
        for e in entries
        if e["url"]
    )

    # Convert markdown bold headings to HTML
    summary_html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", summary)
    summary_html = summary_html.replace("\n\n", "</p><p>")
    summary_html = f"<p>{summary_html}</p>"

    description = (
        f"{summary_html}"
        f"<hr/><p><strong>Quellen ({len(entries)} Posts):</strong></p>"
        f"<ul>{links_html}</ul>"
    )
    # Escape for XML
    import html as html_module
    description_escaped = html_module.escape(description)

    return (
        f"  <item>\n"
        f"    <title>{html_module.escape(title)}</title>\n"
        f"    <link>https://simonwillison.net/</link>\n"
        f"    <guid isPermaLink=\"false\">simon-digest-{target_date.isoformat()}</guid>\n"
        f"    <pubDate>{pub_date}</pubDate>\n"
        f"    <description>{description_escaped}</description>\n"
        f"  </item>"
    )


def update_rss(summary: str, entries: list[dict], target_date: date) -> None:
    """
    Read existing feed.xml (or create fresh), prepend new item,
    keep MAX_ITEMS items, write atomically via temp file.
    """
    if os.path.exists(FEED_FILE):
        try:
            with open(FEED_FILE, "r", encoding="utf-8") as f:
                existing_xml = f.read()
        except OSError as e:
            print(f"Warning: could not read {FEED_FILE}: {e} — starting fresh", file=sys.stderr)
            existing_xml = RSS_TEMPLATE
    else:
        existing_xml = RSS_TEMPLATE

    # Extract existing items (simple regex — avoids full re-parse complexity)
    existing_items = re.findall(r"  <item>.*?</item>", existing_xml, re.DOTALL)

    new_item = make_rss_item(summary, entries, target_date)

    # Keep last MAX_ITEMS-1 existing items, prepend new one
    kept_items = existing_items[: MAX_ITEMS - 1]
    all_items = [new_item] + kept_items

    items_xml = "\n".join(all_items)

    # Rebuild the feed
    new_feed = re.sub(
        r"(</channel>)",
        f"\n{items_xml}\n  </channel>",
        RSS_TEMPLATE,
        count=1,
    )

    # Atomic write: write to temp file, then replace
    dir_name = os.path.dirname(os.path.abspath(FEED_FILE)) or "."
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=dir_name,
            prefix=".feed_tmp_", suffix=".xml", delete=False
        ) as tmp:
            tmp.write(new_feed)
            tmp_path = tmp.name
        os.replace(tmp_path, FEED_FILE)
        print(f"feed.xml updated ({len(all_items)} items total).")
    except OSError as e:
        # Clean up temp file if possible
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        print(f"ERROR: could not write {FEED_FILE}: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    yesterday = date.today() - timedelta(days=1)
    print(f"Processing posts for {yesterday}...")

    # 1. Fetch and parse feed
    root = fetch_feed()

    # 2. Filter to yesterday's entries
    entries = filter_by_date(root, yesterday)
    print(f"Found {len(entries)} entries for {yesterday}.")

    if not entries:
        print("No posts yesterday — nothing to do.")
        sys.exit(0)

    # 3. Fetch full article texts
    print("Fetching article texts...")
    entries = fetch_article_texts(entries)

    # 4. Build prompt and call LLM
    prompt = build_prompt(entries, yesterday)
    summary = call_llm(prompt)

    # 5. Update RSS feed (atomic write)
    update_rss(summary, entries, yesterday)

    print("Done.")


if __name__ == "__main__":
    main()
