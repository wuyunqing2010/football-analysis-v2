import json
from pathlib import Path

import pytest

from football_analysis.config import load_sites


def test_load_sites(tmp_path):
    path = tmp_path / "sites.json"
    path.write_text(
        json.dumps({"sites": [{"id": "a", "name": "A", "url": "https://example.com"}]}),
        encoding="utf-8",
    )
    sites = load_sites(path)
    assert sites[0].id == "a"


def test_rejects_non_https(tmp_path):
    path = tmp_path / "sites.json"
    path.write_text(
        json.dumps({"sites": [{"id": "a", "name": "A", "url": "http://example.com"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_sites(path)


def test_bundled_config_uses_only_the_two_selected_sources():
    path = Path(__file__).parents[1] / "config" / "sites.local.json"
    assert [site.id for site in load_sites(path)] == ["source_a", "source_b"]
