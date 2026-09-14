"""Fetch news via Google News RSS, extract each article's lede with trafilatura.

Scoring text = title + lede paragraph, falling back to title alone if the page
can't be fetched or trafilatura can't find a clean lede (paywalls, consent
walls, bot-blocking are all common and expected -- this must never raise).
"""
from urllib.parse import quote

import feedparser
import trafilatura


def fetch_rss(query: str, max_articles: int = 8) -> list[dict]:
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
    feed = feedparser.parse(url)
    items = []
    for entry in feed.entries[:max_articles]:
        items.append(
            {
                "title": getattr(entry, "title", "").strip(),
                "link": getattr(entry, "link", ""),
                "published": getattr(entry, "published", ""),
            }
        )
    return items


def extract_lede(url: str) -> str:
    """Best-effort lede paragraph. Returns '' on any failure -- never raises."""
    if not url:
        return ""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        if not text:
            return ""
        first_para = text.strip().split("\n\n")[0].strip()
        # Guard against trafilatura returning one giant blob with no paragraph breaks.
        return first_para[:600]
    except Exception:
        return ""


def fetch_and_score_group(queries: list[str], max_articles_per_query: int, score_fn) -> list[dict]:
    """Fetch every query in the group, dedupe by URL, extract + score each article.

    Returns a list of article dicts: title, link, published, lede, finbert, vader.
    """
    articles = []
    seen_urls = set()
    for query in queries:
        for item in fetch_rss(query, max_articles_per_query):
            url = item["link"]
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            lede = extract_lede(url)
            text_for_scoring = f"{item['title']}. {lede}" if lede else item["title"]
            scores = score_fn(text_for_scoring)

            articles.append({**item, "lede": lede, **scores})
    return articles
