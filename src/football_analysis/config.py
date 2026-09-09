from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SiteConfig:
    id: str
    name: str
    url: str
    wait_seconds: int = 25
    dismiss_texts: tuple[str, ...] = ()
    click_texts: tuple[str, ...] = ()
    after_click_wait_seconds: int = 20


def load_sites(path: str | Path) -> list[SiteConfig]:
    config_path = Path(path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    sites = []
    for item in payload.get("sites", []):
        normalized = dict(item)
        normalized["dismiss_texts"] = tuple(normalized.get("dismiss_texts", ()))
        normalized["click_texts"] = tuple(normalized.get("click_texts", ()))
        sites.append(SiteConfig(**normalized))
    if not sites:
        raise ValueError(f"No sites configured in {config_path}")
    seen: set[str] = set()
    for site in sites:
        if site.id in seen:
            raise ValueError(f"Duplicate site id: {site.id}")
        if not site.url.startswith("https://"):
            raise ValueError(f"Only HTTPS sources are accepted: {site.id}")
        seen.add(site.id)
    return sites
