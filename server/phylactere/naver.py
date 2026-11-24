"""Pages d'épisode de comic.naver.com, lues par les scripts (export PDF, traduction des
échantillons, banc d'essai, rapports).

Jumeau Python de la sélection des images de ``extension/content.js`` (``isEpisodeImage``,
``pageImageUrl``) : les URL doivent être les mêmes, puisque leur empreinte est la clé du
cache. Si Naver change sa page, corriger les deux.
"""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

# Navigateur ordinaire : Naver peut refuser un client sans User-Agent de navigateur.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0"


def episode_page_url(series_id: str, episode_id: str) -> str:
    return f"https://comic.naver.com/webtoon/detail?titleId={series_id}&no={episode_id}"


class _ImageCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img":
            values = dict(attrs)
            self.sources.append([v for v in (values.get("src"), values.get("data-src")) if v])


def episode_image_urls(html: str, page_url: str, series_id: str, episode_id: str) -> list[str]:
    """Images de l'épisode dans la page, dans l'ordre et sans doublon : même sélection que
    content.js (URL HTTPS de image-comic.pstatic.net, dossier /<série>/<épisode>/, sans la
    vignette, qui est dans le même dossier)."""
    pattern = re.compile(
        rf"^https://image-comic\.pstatic\.net/.*/{re.escape(series_id)}/"
        rf"{re.escape(episode_id)}/(?!thumbnail)[^/]+$"
    )
    collector = _ImageCollector()
    collector.feed(html)
    urls: list[str] = []
    for candidates in collector.sources:
        for raw in candidates:
            url = urljoin(page_url, raw.strip())
            if pattern.match(url.split("?", 1)[0]):
                if url not in urls:
                    urls.append(url)
                break
    return urls
