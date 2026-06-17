"""Link preview metadata extraction — fetches title, description, and thumbnail from URLs."""

import asyncio
import re
from typing import Optional
import httpx


async def fetch_link_preview(url: str, timeout: float = 5.0) -> Optional[dict]:
    """
    Fetch Open Graph / meta tags from a URL and return preview metadata.
    Returns None if fetch fails or times out.
    """
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "GFD-Bot/1.0 (+https://globalfd.xyz)",
                "Accept": "text/html,application/xhtml+xml",
            },
        ) as client:
            response = await client.get(url)
            if response.status_code != 200:
                return None
            html = response.text
    except Exception:
        return None

    # Extract OG / Twitter / standard meta tags
    title = _extract_meta(html, ["og:title", "twitter:title"]) or _extract_tag(html, "title")
    description = _extract_meta(html, ["og:description", "twitter:description", "description"])
    image = _extract_meta(html, ["og:image", "twitter:image"])
    site_name = _extract_meta(html, ["og:site_name"])

    if not title and not description:
        return None

    return {
        "url": url,
        "title": (title or "")[:200],
        "description": (description or "")[:500],
        "image": image,
        "site_name": site_name,
    }


def _extract_meta(html: str, names: list[str]) -> Optional[str]:
    """Extract content from <meta property/name> tags."""
    for name in names:
        # og: tags use property=, standard use name=
        patterns = [
            rf'<meta[^>]+property=["\']?{re.escape(name)}["\']?[^>]+content=["\']([^"\']+)["\']',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']?{re.escape(name)}["\']?',
            rf'<meta[^>]+name=["\']?{re.escape(name)}["\']?[^>]+content=["\']([^"\']+)["\']',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']?{re.escape(name)}["\']?',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(1).strip()
    return None


def _extract_tag(html: str, tag: str) -> Optional[str]:
    """Extract text content of an HTML tag."""
    pattern = rf"<{tag}[^>]*>([^<]+)</{tag}>"
    match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


def extract_urls(text: str) -> list[str]:
    """Extract all URLs from a text string."""
    url_pattern = re.compile(
        r"https?://[^\s\)\]\>\"\']+",
        re.IGNORECASE,
    )
    return url_pattern.findall(text or "")


def extract_hashtags(text: str) -> list[str]:
    """Extract normalised hashtags (lowercase, no #) from text. Max 50 chars each, max 30 tags."""
    pattern = re.compile(r"#([a-zA-Z0-9_]{1,50})")
    tags = [m.lower() for m in pattern.findall(text or "")]
    return list(dict.fromkeys(tags))[:30]  # deduplicate, cap at 30


def extract_mentions(text: str) -> list[str]:
    """Extract @usernames from text."""
    pattern = re.compile(r"@([a-zA-Z0-9_]{1,50})")
    return list(dict.fromkeys(pattern.findall(text or "")))
