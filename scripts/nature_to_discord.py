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


def state_key(feed, channel_name, entry):
    feed_id = feed.get("id") or feed.get("name") or feed.get("url")
    raw = f"{channel_name}|{feed_id}|{item_key(entry)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def contains_any(text, keywords):
    if not keywords:
        return True
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def load_webhooks():
    webhooks = {}

    raw_json = os.environ.get("DISCORD_WEBHOOKS_JSON", "").strip()
    if raw_json:
        try:
            webhooks.update(json.loads(raw_json))
        except json.JSONDecodeError as exc:
            raise RuntimeError("DISCORD_WEBHOOKS_JSON is not valid JSON.") from exc

    legacy_default = os.environ.get("DISCORD_WEBHOOK_URL")
    if legacy_default:
        webhooks.setdefault("default", legacy_default)
        webhooks.setdefault("nature", legacy_default)

    return webhooks


def channel_settings(config, feed, webhooks):
    channel_name = feed.get("channel") or config.get("default_channel", "default")
    channels = config.get("channels", {})
    channel_config = channels.get(channel_name, {})

    webhook_env = feed.get("webhook_env") or channel_config.get("webhook_env")
    webhook_key = feed.get("webhook") or channel_config.get("webhook") or channel_name

    webhook_url = os.environ.get(webhook_env) if webhook_env else None
    if not webhook_url:
        webhook_url = webhooks.get(webhook_key)

    if not webhook_url:
        raise RuntimeError(
            f"Missing Discord webhook for feed '{feed.get('name', 'unnamed')}'. "
            f"Expected key '{webhook_key}' in DISCORD_WEBHOOKS_JSON"
            + (f" or env var '{webhook_env}'." if webhook_env else ".")
        )

    username = (
        feed.get("username")
        or channel_config.get("username")
        or config.get("username")
        or "RSS Updates"
    )

    return channel_name, webhook_url, username


def post_to_discord(webhook_url, username, feed_name, entry):
    title = clean_text(entry.get("title", "Untitled"), 250)
    link = entry.get("link", "")
    summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
    published = entry.get("published", "")

    payload = {
        "username": username,
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


def post_setup_message(webhook_url, username, seeded_count):
    payload = {
        "username": username,
        "content": f"RSS watcher is set up. Seeded {seeded_count} existing items; future matching items will be posted here.",
    }
    requests.post(webhook_url, json=payload, timeout=20).raise_for_status()


def main():
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    webhooks = load_webhooks()
    if not webhooks:
        raise RuntimeError(
            "Missing Discord webhook settings. Set DISCORD_WEBHOOKS_JSON or DISCORD_WEBHOOK_URL."
        )

    first_run = not STATE_FILE.exists()
    seen = set()
    if STATE_FILE.exists():
        seen = set(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("seen", []))

    post_on_first_run = os.environ.get("POST_ON_FIRST_RUN", "false").lower() == "true"
    max_posts = int(config.get("max_posts_per_run", 25))
    posted = 0
    newly_seen = set()
    seeded_by_channel = {}

    for feed in config.get("feeds", []):
        url = feed["url"]
        if "PASTE_YOUR_PUBMED_RSS_URL" in url:
            raise RuntimeError("Replace the placeholder URL in config.json with your PubMed RSS URL.")

        channel_name, webhook_url, username = channel_settings(config, feed, webhooks)
        channel_seed_key = f"{channel_name}|{username}|{webhook_url}"
        seeded_by_channel.setdefault(
            channel_seed_key,
            {"webhook_url": webhook_url, "username": username, "count": 0},
        )

        parsed = feedparser.parse(url)
        if parsed.bozo:
            print(f"Warning: could not cleanly parse {feed['name']}: {parsed.bozo_exception}")

        entries = list(parsed.entries)
        entries.reverse()

        for entry in entries:
            legacy_key = item_key(entry)
            scoped_key = state_key(feed, channel_name, entry)
            if legacy_key in seen or scoped_key in seen or scoped_key in newly_seen:
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

            if not include_ok or exclude_hit:
                newly_seen.add(scoped_key)
                continue

            if first_run and not post_on_first_run:
                newly_seen.add(scoped_key)
                seeded_by_channel[channel_seed_key]["count"] += 1
                continue

            if posted >= max_posts:
                continue

            post_to_discord(webhook_url, username, feed["name"], entry)
            newly_seen.add(scoped_key)
            posted += 1
            time.sleep(1)

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    all_seen = sorted(seen | newly_seen)
    STATE_FILE.write_text(json.dumps({"seen": all_seen}, indent=2) + "\n", encoding="utf-8")

    if first_run and not post_on_first_run:
        for channel in seeded_by_channel.values():
            if channel["count"] > 0:
                post_setup_message(channel["webhook_url"], channel["username"], channel["count"])

    print(f"Posted {posted} item(s). Seen item count: {len(all_seen)}.")


if __name__ == "__main__":
    main()
