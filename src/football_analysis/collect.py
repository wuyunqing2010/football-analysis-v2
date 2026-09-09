from __future__ import annotations

import hashlib
import gzip
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

import duckdb
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import SiteConfig, load_sites
from .storage import init_database


SOURCE_B_ENDPOINT = "https://www.tiantianyouliao.cn/api/match/race-zc/getJcZcRace"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
    ),
}


@dataclass(frozen=True)
class NormalizedMatch:
    source_id: str
    source_match_id: str
    race_no: str
    competition: str
    home_team: str
    away_team: str
    kickoff: str
    sale_status: str
    sale_deadline: str | None
    markets: tuple[tuple[str, str, float | None, float], ...]
    final_score: str | None = None
    half_score: str | None = None
    result_json: str | None = None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number > 1.0 else None
    except (TypeError, ValueError):
        return None


def _market(name: str, values: Iterable[Any], labels: Iterable[str], line: float | None = None):
    rows = []
    for label, value in zip(labels, values):
        odds = _number(value)
        if odds is not None:
            rows.append((name, label, line, odds))
    return rows


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", []):
            return value
    return None


def _score(item: dict[str, Any]) -> str | None:
    for key in ("finalScore", "final_score", "result_score", "full_score", "score"):
        value = item.get(key)
        if isinstance(value, str):
            match = re.search(r"(\d+)\s*[:：-]\s*(\d+)", value)
            if match:
                return f"{match.group(1)}:{match.group(2)}"
        if isinstance(value, dict):
            nested = _score(value)
            if nested:
                return nested
    for key in ("result", "match_result", "raceZc"):
        value = item.get(key)
        if isinstance(value, dict):
            nested = _score(value)
            if nested:
                return nested
    home = _first(item, "host_score", "home_score", "homeScore", "hostScore")
    away = _first(item, "guest_score", "away_score", "awayScore", "guestScore")
    if home is not None and away is not None:
        try:
            return f"{int(home)}:{int(away)}"
        except (TypeError, ValueError):
            pass
    return None


def parse_source_a(payload: dict[str, Any]) -> list[NormalizedMatch]:
    output: list[NormalizedMatch] = []
    seen: set[str] = set()
    for item in _walk_dicts(payload.get("data") or payload):
        source_match_id = _first(item, "match_id", "match_id2", "matchId")
        home_name = _first(item, "host_name_s", "host_name", "home_name", "homeTeamName")
        away_name = _first(item, "guest_name_s", "guest_name", "away_name", "awayTeamName")
        if source_match_id is None or home_name is None or away_name is None:
            continue
        identity = str(source_match_id)
        if identity in seen:
            continue
        seen.add(identity)
        markets = []
        for api_name, market_name in (("SportteryNWDL", "SPF"), ("SportteryWDL", "RQSPF")):
            play = (item.get("list") or {}).get(api_name)
            if not isinstance(play, dict):
                continue
            odds = play.get("odds") or {}
            try:
                line = float(play.get("boundary") or 0) if market_name == "RQSPF" else None
            except (TypeError, ValueError):
                line = None
            markets += _market(market_name, [odds.get("3"), odds.get("1"), odds.get("0")], ["H", "D", "A"], line)
        plain_sp = item.get("spf") or item.get("spfOdds")
        if isinstance(plain_sp, dict):
            markets += _market("SPF", [_first(plain_sp, "3", "win", "home"),
                                                _first(plain_sp, "1", "draw"),
                                                _first(plain_sp, "0", "lose", "away")], ["H", "D", "A"])
        final_score = _score(item)
        half_score = _first(item, "half_score", "halfScore")
        output.append(NormalizedMatch(
            source_id="source_a", source_match_id=identity,
            race_no=str(_first(item, "serial_no", "race_no", "raceNo") or ""),
            competition=str(_first(item, "league_name", "leagueName") or ""),
            home_team=str(home_name), away_team=str(away_name),
            kickoff=str(_first(item, "match_time", "matchTime", "start_time") or ""),
            sale_status=str(_first(item, "sale_status", "status") or ""),
            sale_deadline=_first(item, "bet_time", "sellStopTime", "sale_deadline"),
            markets=tuple(markets), final_score=final_score,
            half_score=str(half_score) if half_score else None,
            result_json=json.dumps(item.get("match_result") or item.get("result") or {}, ensure_ascii=False),
        ))
    return output


def parse_source_b(payload: dict[str, Any]) -> list[NormalizedMatch]:
    output: list[NormalizedMatch] = []
    seen: set[str] = set()
    race_lists = [node.get("raceVoList") for node in _walk_dicts(payload)
                  if isinstance(node.get("raceVoList"), list)]
    for race_list in race_lists:
        for item in race_list:
            race, info = item.get("race") or {}, item.get("matchInfo") or {}
            identity = str(race.get("id") or race.get("raceNo") or item.get("matchId") or "")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            line = float(item.get("concede") or 0)
            markets = []
            markets += _market("SPF", item.get("spfSpArr") or [], ["H", "D", "A"])
            markets += _market("RQSPF", item.get("rqspfSpArr") or [], ["H", "D", "A"], line)
            markets += _market("ZJQS", item.get("zjqsSpArr") or [], ["0", "1", "2", "3", "4", "5", "6", "7+"])
            # Provider ordering for these two markets is not assumed: preserve stable slots.
            markets += _market("BF", item.get("bfSpArr") or [], [f"slot_{i:02d}" for i in range(31)])
            markets += _market("BQC", item.get("bqcSpArr") or [], [f"slot_{i:02d}" for i in range(9)])
            result = race.get("raceZc") or {}
            output.append(NormalizedMatch(
                source_id="source_b", source_match_id=identity,
                race_no=str(item.get("viewRaceNo") or race.get("raceNo") or ""),
                competition=str(info.get("leagueName") or race.get("leagueName") or ""),
                home_team=str(info.get("homeTeamName") or race.get("homeTeam") or ""),
                away_team=str(info.get("awayTeamName") or race.get("guestTeam") or ""),
                kickoff=str(info.get("matchTime") or race.get("matchTime") or ""),
                sale_status=str((race.get("status") or {}).get("name") or ""),
                sale_deadline=race.get("sellStopTime"), markets=tuple(markets),
                final_score=result.get("finalScore") or None, half_score=result.get("halfScore") or None,
                result_json=json.dumps(result.get("matchResult") or [], ensure_ascii=False),
            ))
    return output


def _source_a_endpoint(site: SiteConfig) -> str:
    query = parse_qs(urlparse(site.url).query)
    station_id, station_uuid = query.get("id", [""])[0], query.get("station_uuid", [""])[0]
    if not station_id or not station_uuid:
        raise ValueError("source_a URL is missing id or station_uuid")
    return (
        "https://apic.jindianle.com/api/match/selectlist"
        "?platform=koudai_mobile&_prt=https&ver=20180101000000"
        f"&hide_more=1&single_support=2&station_user_id={station_id}&station_uuid={station_uuid}"
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
def _fetch(url: str, referer: str) -> dict[str, Any]:
    with httpx.Client(headers={**HEADERS, "Referer": referer}, timeout=30, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value


def _match_key(match: NormalizedMatch) -> str:
    clean = lambda value: re.sub(r"[^0-9a-z\u3400-\u9fff]", "", value.casefold())
    race_digits = re.sub(r"\D", "", match.race_no)
    weekday = ""
    weekday_names = {"周一": "1", "周二": "2", "周三": "3", "周四": "4", "周五": "5", "周六": "6", "周日": "7"}
    for name, code in weekday_names.items():
        if name in match.race_no:
            weekday = code
            break
    if not weekday and len(race_digits) == 4:
        weekday = race_digits[0]
    # The final three digits are the official daily race sequence on both sites.
    # Combining them with kickoff date also joins common short/full Chinese team aliases.
    if len(race_digits) >= 3 and weekday:
        seed = "|".join([match.kickoff[:10], weekday, race_digits[-3:]])
    else:
        seed = "|".join([match.kickoff[:16], clean(match.home_team), clean(match.away_team)])
    return hashlib.sha256(seed.encode()).hexdigest()[:24]


def _iso_local(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace(" ", "T") + ("" if "+" in value or value.endswith("Z") else "+08:00")


def _save_raw(root: Path, source_id: str, captured: datetime, payload: dict[str, Any]) -> tuple[Path, str]:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(body).hexdigest()
    # Content-addressed gzip means an unchanged response is stored once globally,
    # even when live collection and history replay encounter it on different days.
    directory = root / "raw" / "objects" / digest[:2]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.json.gz"
    if not path.exists():
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(gzip.compress(body, compresslevel=6, mtime=0))
        temporary.replace(path)
    return path, digest


def _record_raw_payload(db: Path, run_id: str, source_id: str, captured: datetime,
                        url: str, path: Path, digest: str, *, method: str = "GET",
                        status: int = 200, content_type: str = "application/json") -> bool:
    payload_id = hashlib.sha256(f"{source_id}|{digest}".encode()).hexdigest()
    with gzip.open(path, "rb") as stream:
        byte_count = len(stream.read())
    with duckdb.connect(str(db)) as con:
        seen_for_source = con.execute(
            "SELECT 1 FROM raw_payloads WHERE payload_id=?", [payload_id]
        ).fetchone()
        con.execute("""INSERT OR IGNORE INTO payload_objects VALUES (?, ?, ?, ?, 'gzip', ?)""",
                    [digest, str(path), byte_count, path.stat().st_size, captured])
        con.execute("""INSERT OR IGNORE INTO raw_payloads
          (payload_id, run_id, source_id, captured_at, request_method, response_status,
           response_url, content_type, payload_path, payload_sha256)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
          [payload_id, run_id, source_id, captured, method, status, url,
           content_type, str(path), digest])
    return seen_for_source is None


def _insert(db: Path, run_id: str, captured: datetime, digest: str, matches: list[NormalizedMatch]) -> dict[str, int]:
    odds_count = result_count = 0
    with duckdb.connect(str(db)) as con:
        for match in matches:
            key = _match_key(match)
            con.execute("""INSERT INTO normalized_matches VALUES (?, ?, ?, ?, ?, ?, ?)
              ON CONFLICT(match_key) DO UPDATE SET competition=excluded.competition,
              home_team=excluded.home_team, away_team=excluded.away_team,
              kickoff_time=excluded.kickoff_time, last_seen_at=excluded.last_seen_at""",
              [key, match.competition, match.home_team, match.away_team, _iso_local(match.kickoff), captured, captured])
            con.execute("""INSERT INTO source_matches VALUES (?, ?, ?, ?, ?, ?, ?)
              ON CONFLICT(source_id, source_match_id) DO UPDATE SET match_key=excluded.match_key,
              race_no=excluded.race_no, sale_status=excluded.sale_status,
              sale_deadline=excluded.sale_deadline, captured_at=excluded.captured_at""",
              [match.source_id, match.source_match_id, key, match.race_no, match.sale_status,
               _iso_local(match.sale_deadline), captured])
            grouped: dict[tuple[str, float | None], list[tuple[str, float]]] = {}
            for market, selection, line, odds in match.markets:
                grouped.setdefault((market, line), []).append((selection, odds))
            for (market, line), rows in grouped.items():
                inv_sum = sum(1 / odds for _, odds in rows)
                for selection, odds in rows:
                    previous = con.execute("""SELECT odds FROM market_snapshots
                      WHERE source_id=? AND source_match_id=? AND market=? AND selection=?
                        AND coalesce(line, 999999)=coalesce(?, 999999)
                      ORDER BY captured_at DESC LIMIT 1""",
                      [match.source_id, match.source_match_id, market, selection, line]).fetchone()
                    if previous and abs(float(previous[0]) - odds) < 0.000001:
                        continue
                    sid = hashlib.sha256(
                        f"{match.source_id}|{match.source_match_id}|{captured.isoformat()}|"
                        f"{market}|{selection}|{line}|{odds}|{digest}".encode()
                    ).hexdigest()
                    con.execute("INSERT OR IGNORE INTO market_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [sid, run_id, match.source_id, match.source_match_id, key, captured, market,
                         selection, line, odds, 1 / odds, (1 / odds) / inv_sum, digest])
                    odds_count += 1
            if match.final_score:
                con.execute("""INSERT INTO match_results VALUES (?, ?, ?, ?, ?, ?, ?)
                  ON CONFLICT(source_id, source_match_id) DO UPDATE SET final_score=excluded.final_score,
                  half_score=excluded.half_score, result_json=excluded.result_json,
                  captured_at=excluded.captured_at""",
                  [match.source_id, match.source_match_id, key, match.final_score, match.half_score,
                   match.result_json, captured])
                result_count += 1
    return {"matches": len(matches), "odds": odds_count, "results": result_count}


def collect_once(config_path: str | Path, data_root: str | Path, db_path: str | Path | None = None) -> dict[str, Any]:
    root = Path(data_root)
    db = Path(db_path) if db_path else root / "football.duckdb"
    init_database(db)
    sites = {site.id: site for site in load_sites(config_path)}
    captured, run_id = datetime.now(timezone.utc), uuid.uuid4().hex
    summary: dict[str, Any] = {"run_id": run_id, "captured_at": captured.isoformat(), "sources": {}}
    jobs = [("source_a", _source_a_endpoint(sites["source_a"]), parse_source_a, _fetch),
            ("source_b", SOURCE_B_ENDPOINT, parse_source_b, _fetch)]
    for source_id, endpoint, parser, fetcher in jobs:
        site, started = sites[source_id], datetime.now(timezone.utc)
        try:
            payload = fetcher(endpoint, site.url)
            raw_path, digest = _save_raw(root, source_id, captured, payload)
            _record_raw_payload(db, run_id, source_id, captured, endpoint, raw_path, digest)
            matches = parser(payload)
            if not matches:
                raise ValueError("Endpoint returned no matches")
            counts = _insert(db, run_id, captured, digest, matches)
            summary["sources"][source_id] = {"status": "ok", **counts, "raw": str(raw_path)}
            status, error = "ok", None
        except Exception as exc:
            summary["sources"][source_id] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            status, error = "error", str(exc)
        with duckdb.connect(str(db)) as con:
            con.execute("""INSERT OR REPLACE INTO collection_runs
              (run_id, source_id, started_at, finished_at, status, page_title, final_url, json_response_count, error_message)
              VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
              [f"{run_id}-{source_id}", source_id, started, datetime.now(timezone.utc), status,
               endpoint, 1 if status == "ok" else 0, error])
    (root / "last-collection.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
