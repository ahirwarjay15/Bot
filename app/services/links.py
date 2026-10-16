"""Async shortener adapters with provider discovery, response parsing and bounded caching.

Only documented/common API contracts are attempted. No TLS verification is disabled:
a browser User-Agent cannot guarantee bypassing a provider's anti-bot challenge.
"""
import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import time
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
_CACHE_TTL_SECONDS = 30 * 60
_CACHE_MAX_ENTRIES = 1000
_LINK_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_LOCK = asyncio.Lock()


def _normalized_provider_url(base_url: str) -> str:
    """Normalize a provider URL and add /api when only a domain was supplied."""
    value = (base_url or "").strip()
    if not value:
        raise ValueError("Shortener provider URL is required")
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Shortener provider URL must be a valid HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("Shortener provider URL must not contain embedded credentials")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal", ".test")):
        raise ValueError("Shortener provider URL cannot target a local or internal hostname")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Shortener provider URL cannot target a private or non-public IP address")
    path = parsed.path.rstrip("/")
    if host in {"gplinks.com", "www.gplinks.com", "gplinks.in", "www.gplinks.in"} and path in {"", "/api"}:
        return "https://api.gplinks.com/st"
    if host == "api.gplinks.com" and path in {"", "/st"}:
        return "https://api.gplinks.com/st"
    if host in {"ouo.io", "www.ouo.io", "ouo.press", "www.ouo.press"}:
        return f"{parsed.scheme}://{parsed.netloc}"
    if path:
        return value.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}/api"


def _shrtfly_endpoint(base_url: str) -> str | None:
    """Backward-compatible ShrtFly endpoint resolver."""
    parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
    if (parsed.hostname or "").lower() in {"shrtfly.com", "www.shrtfly.com", "api.shrtfly.com"}:
        return _normalized_provider_url(base_url)
    return None


def _shrtfly_params(api_token: str, long_url: str) -> dict[str, str]:
    """Legacy helper retained for tests and integrations."""
    return {"api": api_token, "url": long_url}


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except (AttributeError, ValueError):
        return False


def _extract_short_url(data, text: str | None = None) -> str:
    """Extract a URL from common JSON keys or a plain-text API response."""
    if isinstance(data, str):
        candidate = data.strip().strip('"').replace("\\/", "/")
        if _is_http_url(candidate) and not re.search(r"\\s|<html", candidate, re.I):
            return candidate
        try:
            data = json.loads(candidate)
        except (TypeError, ValueError):
            if candidate and candidate.lstrip().startswith(("<!DOCTYPE", "<html", "<HTML")):
                raise ValueError(
                    "Provider returned HTML instead of JSON; an anti-bot or Cloudflare page may be blocking the API"
                )
            if text is None:
                raise ValueError(f"Provider returned an unrecognized response: {candidate[:120] or 'empty response'}")

    if isinstance(data, dict):
        keys = (
            "short_url", "shortUrl", "shortenedUrl", "shortened_url",
            "shortenedLink", "shortened_link", "shortlink", "shortLink",
            "short_link", "shortened", "short", "result_url", "link", "url",
        )
        for key in keys:
            candidate = data.get(key)
            if isinstance(candidate, str) and _is_http_url(candidate):
                return candidate.strip().replace("\\/", "/")
        for key in ("data", "result", "output"):
            nested = data.get(key)
            if isinstance(nested, (dict, str, list)):
                try:
                    return _extract_short_url(nested)
                except ValueError:
                    pass
        status = str(data.get("status", "")).lower()
        if status in {"error", "failed", "failure", "false"} or data.get("success") is False:
            message = data.get("message") or data.get("error") or data.get("msg")
            raise ValueError(str(message or "Provider rejected the API request"))
        message = data.get("message") or data.get("error") or data.get("msg")
        if message:
            raise ValueError(str(message))

    if text:
        candidate = text.strip().strip('"').replace("\\/", "/")
        if _is_http_url(candidate) and not re.search(r"\\s|<html", candidate, re.I):
            return candidate
        if candidate.lower().startswith(("<!doctype html", "<html")):
            raise ValueError("Provider returned HTML instead of a short URL (possible anti-bot challenge)")
    raise ValueError("Provider response did not contain a valid short URL")


async def _fetch(session, method: str, endpoint: str, *, params=None, json_data=None, headers=None):
    async with session.request(method, endpoint, params=params, json=json_data, headers=headers) as response:
        body = await response.text()
        if response.status >= 400:
            detail = body.strip().replace("\\n", " ")[:140]
            if detail.lower().startswith(("<!doctype html", "<html")):
                detail = "HTML/anti-bot response"
            raise ValueError(f"Provider returned HTTP {response.status}: {detail or response.reason}")
        try:
            data = json.loads(body)
        except (TypeError, ValueError):
            data = None
        return _extract_short_url(data, body)


def _cache_key(domain: str, token: str, long_url: str) -> str:
    token_hash = hashlib.sha256((token or "").encode()).hexdigest()
    return hashlib.sha256(f"{domain}|{token_hash}|{long_url}".encode()).hexdigest()


async def _cache_get(key: str) -> str | None:
    now = time.monotonic()
    async with _CACHE_LOCK:
        item = _LINK_CACHE.get(key)
        if not item:
            return None
        expires, value = item
        if expires <= now:
            _LINK_CACHE.pop(key, None)
            return None
        return value


async def _cache_set(key: str, value: str) -> None:
    now = time.monotonic()
    async with _CACHE_LOCK:
        for old_key, (expires, _) in list(_LINK_CACHE.items()):
            if expires <= now:
                _LINK_CACHE.pop(old_key, None)
        while len(_LINK_CACHE) >= _CACHE_MAX_ENTRIES:
            _LINK_CACHE.pop(next(iter(_LINK_CACHE)))
        _LINK_CACHE[key] = (now + _CACHE_TTL_SECONDS, value)


async def shorten_url(base_url: str, api_token: str, long_url: str) -> str:
    """Shorten a URL using common provider APIs; fail loudly if no API returns a short URL."""
    if not _is_http_url(long_url):
        raise ValueError("The URL to shorten must be a valid HTTP(S) URL")
    token = (api_token or "").strip()
    endpoint = _normalized_provider_url(base_url)
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    domain = (urlparse("https://" + base_url if "://" not in base_url else base_url).hostname or "").lower()
    keyless_hosts = {"is.gd", "www.is.gd", "v.gd", "www.v.gd", "tinyurl.com", "www.tinyurl.com", "cleanuri.com", "www.cleanuri.com"}
    if not token and host not in keyless_hosts and domain not in keyless_hosts:
        raise ValueError("Shortener API token is required for this provider")

    cache_key = _cache_key(domain or host, token, long_url)
    cached = await _cache_get(cache_key)
    if cached:
        return cached

    timeout = aiohttp.ClientTimeout(total=12, connect=5)
    async with aiohttp.ClientSession(timeout=timeout, headers=_BROWSER_HEADERS) as session:
        if "shareus" in domain or "shareus" in host:
            result = await _fetch(
                session, "GET", "https://api.shareus.io/easy_api",
                params={"key": token, "link": long_url},
            )
        elif "ouo." in domain or host in {"ouo.io", "www.ouo.io", "ouo.press", "www.ouo.press"}:
            result = await _fetch(
                session, "GET", f"https://{host}/api/{token}", params={"s": long_url},
            )
        elif "shorte.st" in domain or "gestyy" in domain:
            result = await _fetch(
                session, "PUT", "https://api.shorte.st/v1/data/url",
                json_data={"urlToShorten": long_url},
                headers={"public-api-token": token},
            )
        elif "bit.ly" in domain or "bitly.com" in domain:
            result = await _fetch(
                session, "POST", "https://api-ssl.bitly.com/v4/shorten",
                json_data={"long_url": long_url},
                headers={"Authorization": f"Bearer {token}"},
            )
        elif "adfoc.us" in domain:
            result = await _fetch(
                session, "GET", "https://adfoc.us/api/",
                params={"key": token, "url": long_url},
            )
        elif "cutt.ly" in domain:
            result = await _fetch(
                session, "GET", "https://cutt.ly/api/api.php",
                params={"key": token, "short": long_url},
            )
        elif host in {"is.gd", "www.is.gd", "v.gd", "www.v.gd"} or domain in {"is.gd", "www.is.gd", "v.gd", "www.v.gd"}:
            service = host if host in {"is.gd", "www.is.gd", "v.gd", "www.v.gd"} else domain
            result = await _fetch(
                session, "GET", f"https://{service}/create.php",
                params={"format": "simple", "url": long_url},
            )
        elif "tinyurl.com" in domain:
            result = await _fetch(session, "GET", "https://tinyurl.com/api-create.php", params={"url": long_url})
        elif "cleanuri.com" in domain:
            result = await _fetch(session, "POST", "https://cleanuri.com/api/v1/shorten", json_data={"url": long_url})
        else:
            base_domain = domain.removeprefix("www.").removeprefix("api.")
            candidates = [
                (endpoint, {"api": token, "url": long_url}),
                (f"https://api.{base_domain}/api", {"api": token, "url": long_url}),
                (f"https://{base_domain}/api", {"api": token, "url": long_url, "format": "text"}),
                (f"https://api.{base_domain}/api", {"api": token, "url": long_url, "format": "text"}),
                (f"https://{base_domain}/easy_api", {"key": token, "link": long_url}),
            ]
            seen = set()
            errors = []
            result = None
            for candidate_endpoint, params in candidates:
                signature = (candidate_endpoint, tuple(sorted(params.items())))
                if signature in seen:
                    continue
                seen.add(signature)
                try:
                    result = await _fetch(session, "GET", candidate_endpoint, params=params)
                    break
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                    errors.append(str(exc)[:100])
            if not result:
                detail = errors[-1] if errors else "No compatible API endpoint responded"
                raise ValueError(f"Shortener API discovery failed: {detail}")

    await _cache_set(cache_key, result)
    return result


async def get_shortlink(
    long_url: str,
    primary_domain: str,
    primary_api: str,
    backup_domain: str | None = None,
    backup_api: str | None = None,
) -> str:
    """Try a primary provider and optional backup; never silently return an unshortened URL."""
    try:
        return await shorten_url(primary_domain, primary_api, long_url)
    except Exception as primary_error:
        if backup_domain:
            try:
                return await shorten_url(backup_domain, backup_api or "", long_url)
            except Exception as backup_error:
                raise ValueError(
                    f"Primary and backup shorteners both failed. Primary: {primary_error}; backup: {backup_error}"
                ) from backup_error
        raise
