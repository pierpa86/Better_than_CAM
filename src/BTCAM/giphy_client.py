from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import parse, request


GIPHY_SEARCH_URL = "https://api.giphy.com/v1/gifs/search"
GIPHY_TRENDING_URL = "https://api.giphy.com/v1/gifs/trending"
GIPHY_CATEGORIES_URL = "https://api.giphy.com/v1/gifs/categories"


@dataclass(frozen=True, slots=True)
class GiphyGif:
    id: str
    title: str
    preview_url: str
    gif_url: str
    source_url: str


@dataclass(frozen=True, slots=True)
class GiphyCategory:
    name: str
    query: str
    preview_url: str
    subcategories: tuple[str, ...] = ()


def search_giphy(api_key: str, query: str, limit: int = 12, offset: int = 0) -> list[GiphyGif]:
    query = query.strip()
    if not query:
        return trending_giphy(api_key, limit=limit, offset=offset)
    return _fetch_giphy(
        GIPHY_SEARCH_URL,
        {
            "api_key": api_key,
            "q": query[:50],
            "limit": str(limit),
            "offset": str(max(0, int(offset))),
            "rating": "pg-13",
            "lang": "en",
        },
    )


def trending_giphy(api_key: str, limit: int = 12, offset: int = 0) -> list[GiphyGif]:
    return _fetch_giphy(
        GIPHY_TRENDING_URL,
        {
            "api_key": api_key,
            "limit": str(limit),
            "offset": str(max(0, int(offset))),
            "rating": "pg-13",
        },
    )


def giphy_categories(api_key: str) -> list[GiphyCategory]:
    api_key = str(api_key or "").strip()
    if not api_key:
        raise ValueError("GIPHY API key missing.")

    payload = _fetch_payload(GIPHY_CATEGORIES_URL, {"api_key": api_key})
    results: list[GiphyCategory] = []
    for item in payload.get("data", []):
        if not isinstance(item, dict):
            continue
        category = _parse_giphy_category(item)
        if category is not None:
            results.append(category)
    return results


def download_giphy_file(url: str, timeout: float = 20.0) -> bytes:
    req = request.Request(url, headers={"User-Agent": "BTCAM/0.1"})
    with request.urlopen(req, timeout=timeout) as response:
        return response.read()


def _fetch_giphy(endpoint: str, params: dict[str, str]) -> list[GiphyGif]:
    api_key = str(params.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("GIPHY API key missing.")

    payload = _fetch_payload(endpoint, params)
    results: list[GiphyGif] = []
    for item in payload.get("data", []):
        if not isinstance(item, dict):
            continue
        try:
            results.append(_parse_giphy_item(item))
        except ValueError:
            continue
    return results


def _fetch_payload(endpoint: str, params: dict[str, str]) -> dict[str, object]:
    url = f"{endpoint}?{parse.urlencode(params)}"
    req = request.Request(url, headers={"User-Agent": "BTCAM/0.1"})
    with request.urlopen(req, timeout=15.0) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        return {}
    return payload


def _parse_giphy_item(item: dict[str, object]) -> GiphyGif:
    images = item.get("images")
    if not isinstance(images, dict):
        images = {}

    preview_url = _image_url(images, "fixed_width_small") or _image_url(images, "preview_gif") or _image_url(images, "downsized")
    gif_url = _image_url(images, "downsized") or _image_url(images, "original") or preview_url
    if not preview_url or not gif_url:
        raise ValueError("GIPHY item does not include a usable GIF URL.")

    title = str(item.get("title") or "Giphy").strip() or "Giphy"
    return GiphyGif(
        id=str(item.get("id") or "").strip(),
        title=title,
        preview_url=preview_url,
        gif_url=gif_url,
        source_url=str(item.get("url") or "").strip(),
    )


def _parse_giphy_category(item: dict[str, object]) -> GiphyCategory | None:
    name = str(item.get("name") or "").strip()
    if not name:
        return None

    query = str(item.get("name_encoded") or name).strip() or name
    subcategories: list[str] = []
    for child in item.get("subcategories") or []:
        if not isinstance(child, dict):
            continue
        child_name = str(child.get("name") or child.get("name_encoded") or "").strip()
        if child_name:
            subcategories.append(child_name)

    gif = item.get("gif")
    preview_url = ""
    if isinstance(gif, dict):
        images = gif.get("images")
        if isinstance(images, dict):
            preview_url = _image_url(images, "fixed_width_small") or _image_url(images, "preview_gif") or _image_url(images, "downsized")

    return GiphyCategory(
        name=name,
        query=query,
        preview_url=preview_url,
        subcategories=tuple(subcategories[:4]),
    )


def _image_url(images: dict[str, object], key: str) -> str:
    image = images.get(key)
    if not isinstance(image, dict):
        return ""
    return str(image.get("url") or "").strip()
