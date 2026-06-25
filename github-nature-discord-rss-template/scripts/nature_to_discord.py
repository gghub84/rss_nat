import hashlib
import html
import json
import os
import re
import time
from pathlib import Path

import feedparser
import requests


ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / ".state" / "seen.json"


def clean_text(value, limit=450):
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[: limit - 1] + "..." if len(text) > limit else text


def item_key(entry):
    raw = entry.get("id") or entry.get("guid") or entry.get("link")
    if not raw:
        raw = f"{entry.get('title', '')}|{entry.get('published', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def contains_any(text, keywords):
    if not keywords:
        return True
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def post_to_discord(webhook_url, feed_name, entry):
    title = clean_text(entry.get("title", "Untitled"), 250)
    link = entry.get("link", "")
    summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
    published = entry.get("published", "")

    payload = {
        "username": "Nature Updates",
        "embeds": [
            {
                "title": title,
                "url": link,
                "description": summary or None,
                "color": 3066993,
                "fields": [
                    {"name": "Feed", "value": feed_name[:1024], "inline": True},
                    {"name": "Published", "value": published[:1024] or "Unknown", "inline": True},
                ],
            }
        ],
    }

    for _ in range(3):
        response = requests.post(webhook_url, json=payload, timeout=20)
        if response.status_code == 429:
            retry_after = response.json().get("retry_after", 2)
            time.sleep(float(retry_after) + 0.5)
            continue
        response.raise_for_status()
        return


def post_setup_message(webhook_url, seeded_count):
    payload = {
        "username": "Nature Updates",
        "content": f"Nature RSS watcher is set up. Seeded {seeded_count} existing items; future matching items will be posted here.",
    }
    requests.post(webhook_url, json=payload, timeout=20).raise_for_status()


def main():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL repository secret.")

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    first_run = not STATE_FILE.exists()
    seen = set()
    if STATE_FILE.exists():
        seen = set(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("seen", []))

    post_on_first_run = os.environ.get("POST_ON_FIRST_RUN", "false").lower() == "true"
    max_posts = int(config.get("max_posts_per_run", 25))
    posted = 0
    newly_seen = set()

    for feed in config.get("feeds", []):
        url = feed["url"]
        if "PASTE_YOUR_PUBMED_RSS_URL" in url:
            raise RuntimeError("Replace the placeholder URL in config.json with your PubMed RSS URL.")

        parsed = feedparser.parse(url)
        if parsed.bozo:
            print(f"Warning: could not cleanly parse {feed['name']}: {parsed.bozo_exception}")

        entries = list(parsed.entries)
        entries.reverse()

        for entry in entries:
            key = item_key(entry)
            if key in seen or key in newly_seen:
                continue

            searchable = " ".join(
                [
                    entry.get("title", ""),
                    entry.get("summary", ""),
                    entry.get("description", ""),
                    entry.get("author", ""),
                ]
            )
            include_ok = contains_any(searchable, feed.get("include_keywords", []))
            exclude_hit = contains_any(searchable, feed.get("exclude_keywords", [])) if feed.get("exclude_keywords") else False
            newly_seen.add(key)

            if not include_ok or exclude_hit:
                continue
            if first_run and not post_on_first_run:
                continue
            if posted >= max_posts:
                continue

            post_to_discord(webhook_url, feed["name"], entry)
            posted += 1
            time.sleep(1)

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    all_seen = sorted(seen | newly_seen)
    STATE_FILE.write_text(json.dumps({"seen": all_seen}, indent=2) + "\n", encoding="utf-8")

    if first_run and not post_on_first_run:
        post_setup_message(webhook_url, len(newly_seen))

    print(f"Posted {posted} item(s). Seen item count: {len(all_seen)}.")


if __name__ == "__main__":
    main()
