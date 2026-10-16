import pytest
from app.services.links import _extract_short_url, _normalized_provider_url, _shrtfly_endpoint, _shrtfly_params


def test_adlinkfly_domain_gets_api_endpoint():
    assert _normalized_provider_url("shrinkme.io") == "https://shrinkme.io/api"
    assert _normalized_provider_url("https://shrtfly.com/") == "https://shrtfly.com/api"
    assert _normalized_provider_url("https://shortener.example/api") == "https://shortener.example/api"


def test_gplinks_website_uses_api_endpoint():
    assert _normalized_provider_url("https://gplinks.com") == "https://api.gplinks.com/st"
    assert _normalized_provider_url("https://www.gplinks.in/") == "https://api.gplinks.com/st"


@pytest.mark.parametrize("response", [
    {"short_url": "https://short.example/abc"},
    {"shortenedUrl": "https://short.example/abc"},
    {"shortlink": "https://short.example/abc"},
    "https://short.example/abc",
])
def test_supported_response_shapes(response):
    assert _extract_short_url(response) == "https://short.example/abc"


def test_invalid_response_fails():
    with pytest.raises(ValueError):
        _extract_short_url({"status": "error"})


def test_shrtfly_endpoint():
    assert _shrtfly_endpoint("https://shrtfly.com") == "https://shrtfly.com/api"
    assert _shrtfly_endpoint("https://www.shrtfly.com/") == "https://www.shrtfly.com/api"
    assert _shrtfly_endpoint("https://shrtfly.com/api") == "https://shrtfly.com/api"


def test_shrtfly_uses_api_and_url_params():
    assert _shrtfly_params("test-token", "https://example.org/file?start=abc") == {
        "api": "test-token",
        "url": "https://example.org/file?start=abc",
    }


def test_nested_response_shapes():
    assert _extract_short_url({"data": {"short_url": "https://short.example/a"}}) == "https://short.example/a"
    assert _extract_short_url({"result": {"shortenedUrl": "https://short.example/b"}}) == "https://short.example/b"


def test_provider_error_message_is_preserved():
    with pytest.raises(ValueError, match="Invalid API token"):
        _extract_short_url({"status": "error", "message": "Invalid API token"})


def test_cloudflare_html_response_is_reported():
    with pytest.raises(ValueError, match="Cloudflare"):
        _extract_short_url("<!DOCTYPE html><html><title>Just a moment</title></html>")


def test_unknown_plain_text_response_is_not_success():
    with pytest.raises(ValueError, match="unrecognized response"):
        _extract_short_url("invalid API token")
