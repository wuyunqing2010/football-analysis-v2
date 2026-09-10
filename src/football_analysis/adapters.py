from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .collect import (
    SOURCE_B_ENDPOINT,
    NormalizedMatch,
    _fetch,
    _source_a_endpoint,
    parse_source_a,
    parse_source_b,
)
from .config import SiteConfig


Parser = Callable[[dict[str, Any]], list[NormalizedMatch]]
EndpointBuilder = Callable[[SiteConfig], str]
Fetcher = Callable[[str, str], dict[str, Any]]


@dataclass(frozen=True)
class SourceAdapter:
    source_id: str
    display_name: str
    endpoint_builder: EndpointBuilder
    parser: Parser
    history_url_parts: tuple[str, ...]
    fetcher: Fetcher = _fetch

    def live_endpoint(self, site: SiteConfig) -> str:
        return self.endpoint_builder(site)


def _source_b_endpoint(_: SiteConfig) -> str:
    return SOURCE_B_ENDPOINT


_REGISTRY: dict[str, SourceAdapter] = {
    "source_a": SourceAdapter(
        source_id="source_a",
        display_name="59itou",
        endpoint_builder=_source_a_endpoint,
        parser=parse_source_a,
        history_url_parts=("/api/match/", "/api/paijiang/"),
    ),
    "source_b": SourceAdapter(
        source_id="source_b",
        display_name="tiantianyouliao",
        endpoint_builder=_source_b_endpoint,
        parser=parse_source_b,
        history_url_parts=("/api/match/", "/api/forecast/smart/select/details"),
    ),
}


def register_adapter(adapter: SourceAdapter) -> None:
    """Register a reviewed fallback source without changing storage or reports."""
    if adapter.source_id in _REGISTRY:
        raise ValueError(f"Adapter already registered: {adapter.source_id}")
    _REGISTRY[adapter.source_id] = adapter


def adapter_for(source_id: str) -> SourceAdapter:
    try:
        return _REGISTRY[source_id]
    except KeyError as exc:
        raise ValueError(f"No reviewed adapter for source: {source_id}") from exc


def configured_adapters(sites: list[SiteConfig]) -> list[tuple[SiteConfig, SourceAdapter]]:
    return [(site, adapter_for(site.id)) for site in sites]


def active_source_ids() -> tuple[str, ...]:
    return tuple(_REGISTRY)
