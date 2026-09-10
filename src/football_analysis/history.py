from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import duckdb
from playwright.async_api import async_playwright

from .collect import (
    HEADERS,
    NormalizedMatch,
    _fetch,
    _insert,
    _match_key,
    _record_raw_payload,
    _save_raw,
)
from .adapters import adapter_for
from .config import SiteConfig, load_sites
from .privacy import sanitize
from .storage import init_database


MODEL_ENDPOINT = "https://www.tiantianyouliao.cn/api/forecast/smart/select/details"
MOBILE_UA = HEADERS["User-Agent"]
MAX_BODY = 6 * 1024 * 1024
BLOCKED = {"确认解锁", "购买", "支付", "提交订单", "立即下单", "下一步"}
DENIED_URL_PARTS = (
    "/recommend/", "/recommended", "/specialist", "/member/", "/order/",
    "/pay/", "/project", "/sharebuy/", "/shop/",
)
PAGINATION_TEXT = (
    re.compile(r"^加载更多"),
    re.compile(r"^下一页$"),
    re.compile(r"^更早"),
    re.compile(r"^前一天$"),
    re.compile(r"^上一页$"),
)
PAGE_KEYS = {"page", "pageindex", "pageno", "pagenum", "current", "currentpage"}


def _objective(value: Any) -> Any:
    """Keep objective inputs while dropping provider recommendations and identities."""
    blocked_parts = ("recommend", "specialist", "primaryrec", "secondaryrec", "member")
    if isinstance(value, dict):
        return {
            key: _objective(child)
            for key, child in value.items()
            if not any(part in key.casefold() for part in blocked_parts)
        }
    if isinstance(value, list):
        return [_objective(child) for child in value]
    return value


def _contains_key(value: Any, target: str) -> bool:
    if isinstance(value, dict):
        return target in value or any(_contains_key(child, target) for child in value.values())
    if isinstance(value, list):
        return any(_contains_key(child, target) for child in value)
    return False


def ingest_model_details(
    payload: dict[str, Any],
    db_path: str | Path,
    run_id: str,
    captured: datetime,
    digest: str,
) -> int:
    del run_id  # retained in the public signature for backwards compatibility
    inserted = 0
    with duckdb.connect(str(db_path)) as con:
        for day, races in (payload.get("data") or {}).items():
            if not isinstance(races, dict):
                continue
            for race_label, bundle in races.items():
                model_map = (bundle or {}).get("modelDetailsResMap") or {}
                for model_type, details in model_map.items():
                    if model_type == "SPECIALIST":
                        continue
                    candidates: list[dict[str, Any]] = []
                    if isinstance(details, dict):
                        for key, value in details.items():
                            if key.endswith("Dto") and isinstance(value, dict):
                                candidates.append(value)
                            elif key == "halfFullDetails" and isinstance(value, dict):
                                candidates.append(value)
                    for dto in candidates:
                        match_id = str(dto.get("matchId") or f"{day}-{race_label}")
                        home = dto.get("home") or {}
                        away = dto.get("away") or {}
                        home_name = str(dto.get("homeName") or home.get("home") or "")
                        away_name = str(dto.get("awayName") or away.get("home") or "")
                        match_time = str(dto.get("matchTime") or f"{day} 00:00:00")
                        stub = NormalizedMatch(
                            "source_b",
                            match_id,
                            str(dto.get("raceNo") or race_label),
                            str(dto.get("compName") or ""),
                            home_name,
                            away_name,
                            match_time,
                            "",
                            None,
                            (),
                        )
                        key = _match_key(stub)
                        safe = _objective(dto)
                        feature_json = json.dumps(safe, ensure_ascii=False, sort_keys=True)
                        feature_digest = hashlib.sha256(feature_json.encode()).hexdigest()
                        feature_id = hashlib.sha256(
                            f"source_b|{match_id}|{model_type}|{feature_digest}".encode()
                        ).hexdigest()
                        exists = con.execute(
                            "SELECT 1 FROM objective_features WHERE feature_id=?", [feature_id]
                        ).fetchone()
                        con.execute(
                            "INSERT OR IGNORE INTO objective_features VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            [
                                feature_id,
                                "source_b",
                                match_id,
                                key,
                                captured,
                                model_type,
                                feature_json,
                                digest,
                            ],
                        )
                        if exists is None:
                            inserted += 1
    return inserted


def _allowed_response(site: SiteConfig, url: str) -> bool:
    lowered = url.casefold()
    if any(part in lowered for part in DENIED_URL_PARTS):
        return False
    return any(part in lowered for part in adapter_for(site.id).history_url_parts)


def _page_field(value: Any, path: tuple[str, ...] = ()) -> tuple[tuple[str, ...], int] | None:
    if not isinstance(value, dict):
        return None
    for key, child in value.items():
        if key.casefold() in PAGE_KEYS:
            try:
                return path + (key,), int(child)
            except (TypeError, ValueError):
                pass
        found = _page_field(child, path + (key,))
        if found:
            return found
    return None


def _api_cursor(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in reversed(records):
        parts = urlsplit(record["url"])
        query = parse_qsl(parts.query, keep_blank_values=True)
        for key, value in query:
            if key.casefold() in PAGE_KEYS:
                try:
                    page = int(value)
                except ValueError:
                    continue
                return {
                    "strategy": "api_page",
                    "method": record["method"],
                    "url": record["url"],
                    "page_key": key,
                    "page_value": page,
                    "request_json": record.get("request_json"),
                }
        found = _page_field(record.get("request_json"))
        if found:
            path, page = found
            return {
                "strategy": "api_page",
                "method": record["method"],
                "url": record["url"],
                "json_path": list(path),
                "page_value": page,
                "request_json": record.get("request_json"),
            }
    return None


def _set_nested(value: dict[str, Any], path: list[str], replacement: int) -> None:
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement


def _paged_url(url: str, page_key: str, page_value: int) -> str:
    parts = urlsplit(url)
    query = [
        (key, str(page_value) if key == page_key else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _payload_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _click(page: Any, pattern: re.Pattern[str] | str) -> bool:
    locator = page.get_by_text(pattern, exact=isinstance(pattern, str))
    for item in await locator.all():
        if not await item.is_visible() or not await item.is_enabled():
            continue
        label = (await item.inner_text()).strip()
        if label in BLOCKED:
            continue
        try:
            await item.click(timeout=5_000)
        except Exception:
            await item.evaluate("element => element.click()")
        return True
    return False


async def _click_first(page: Any, patterns: tuple[re.Pattern[str] | str, ...]) -> str | None:
    for pattern in patterns:
        if await _click(page, pattern):
            return pattern.pattern if isinstance(pattern, re.Pattern) else pattern
    return None


async def _page_signature(page: Any) -> tuple[int, str]:
    value = await page.evaluate(
        """() => {
          const body = document.body;
          return [Math.max(body.scrollHeight, document.documentElement.scrollHeight),
                  (body.innerText || '').slice(-4000)];
        }"""
    )
    return int(value[0]), hashlib.sha256(str(value[1]).encode()).hexdigest()


async def _advance(page: Any) -> str | None:
    before_height, before_text = await _page_signature(page)
    clicked = await _click_first(page, PAGINATION_TEXT)
    if clicked:
        await page.wait_for_timeout(3_000)
        return f"click:{clicked}"

    await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    await page.wait_for_timeout(3_000)
    after_height, after_text = await _page_signature(page)
    if after_height > before_height or after_text != before_text:
        return "scroll"
    return None


async def _enter_history(page: Any, site: SiteConfig) -> str:
    if site.id == "source_a":
        await _click(page, "×")
        await _click_first(page, ("竞彩足球", "竞足"))
        await page.wait_for_timeout(8_000)
        entered = await _click_first(
            page,
            ("赛果战报", "开奖大厅", re.compile(r"^赛果"), re.compile(r"^历史赛果")),
        )
        if not entered:
            return "首页公开数据（未发现独立历史入口）"
        return entered

    await _click(page, "知道了")
    await _click_first(page, ("竞足", "竞彩足球"))
    await page.wait_for_timeout(8_000)
    entered = await _click_first(
        page,
        (re.compile(r"^显示停售"), re.compile(r"^已停售"), re.compile(r"^赛果")),
    )
    if not entered:
        return "首页公开数据（未发现独立停售入口）"
    return entered


async def _continue_api_pages(
    context: Any,
    site: SiteConfig,
    cursor: dict[str, Any],
    max_actions: int,
    payloads: dict[str, dict[str, Any]],
) -> tuple[list[str], dict[str, Any], bool]:
    actions: list[str] = []
    current = int(cursor["page_value"])
    caught_up = False
    working = json.loads(json.dumps(cursor, ensure_ascii=False))
    for _ in range(max_actions):
        next_page = current + 1
        url = working["url"]
        request_json = working.get("request_json")
        if working.get("page_key"):
            url = _paged_url(url, working["page_key"], next_page)
        elif working.get("json_path") and isinstance(request_json, dict):
            _set_nested(request_json, working["json_path"], next_page)
        else:
            caught_up = True
            break
        try:
            if working.get("method", "GET").upper() == "POST":
                response = await context.request.post(
                    url, data=request_json, headers={"Referer": site.url}
                )
            else:
                response = await context.request.get(
                    url, headers={"Referer": site.url}
                )
            body = await response.body()
            if response.status >= 400 or len(body) > MAX_BODY:
                caught_up = True
                break
            value = json.loads(body.decode("utf-8", errors="replace"))
            if not isinstance(value, dict):
                caught_up = True
                break
            safe = sanitize(value)
            digest = _payload_digest(safe)
            if digest in payloads:
                caught_up = True
                break
            payloads[digest] = {
                "url": url,
                "method": working.get("method", "GET"),
                "status": response.status,
                "content_type": response.headers.get("content-type")
                or "application/json",
                "request_json": request_json,
                "payload": safe,
            }
            actions.append(f"api_page:{next_page}")
            current = next_page
            working["url"] = url
            working["page_value"] = current
            working["request_json"] = request_json
        except Exception:
            caught_up = True
            break
    return actions, working, caught_up


async def _browser_history(
    site: SiteConfig,
    max_actions: int,
    replay_actions: int,
    max_replay: int,
    resume_cursor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    captured = datetime.now(timezone.utc)
    payloads: dict[str, dict[str, Any]] = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, args=["--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent=MOBILE_UA,
            viewport={"width": 390, "height": 844},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = await context.new_page()

        async def on_response(response: Any) -> None:
            if response.request.resource_type not in {"xhr", "fetch"}:
                return
            if not _allowed_response(site, response.url):
                return
            try:
                body = await response.body()
                if len(body) > MAX_BODY:
                    return
                value = json.loads(body.decode("utf-8", errors="replace"))
                if not isinstance(value, dict):
                    return
                safe = sanitize(value)
                request_json = None
                if response.request.post_data:
                    try:
                        request_json = sanitize(json.loads(response.request.post_data))
                    except (TypeError, json.JSONDecodeError):
                        pass
                digest = _payload_digest(safe)
                payloads[digest] = {
                    "url": response.url,
                    "method": response.request.method,
                    "status": response.status,
                    "content_type": response.headers.get("content-type")
                    or "application/json",
                    "request_json": request_json,
                    "payload": safe,
                }
            except Exception:
                return

        page.on("response", on_response)
        await page.goto(site.url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(max(5, site.wait_seconds) * 1_000)
        history_entry = await _enter_history(page, site)
        await page.wait_for_timeout(8_000)

        replayed = 0
        actions: list[str] = []
        stagnant = 0
        caught_up = False
        cursor = resume_cursor
        if cursor and cursor.get("strategy") == "api_page":
            actions, cursor, caught_up = await _continue_api_pages(
                context, site, cursor, max_actions, payloads
            )
        else:
            for _ in range(min(replay_actions, max_replay)):
                action = await _advance(page)
                if action is None:
                    break
                replayed += 1

            last_payload_count = len(payloads)
            for _ in range(max_actions):
                action = await _advance(page)
                if action is None:
                    caught_up = True
                    break
                actions.append(action)
                current = len(payloads)
                stagnant = stagnant + 1 if current == last_payload_count else 0
                last_payload_count = current
                if stagnant >= 3:
                    caught_up = True
                    break
            if not caught_up:
                caught_up = len(actions) < max_actions

        title, url = await page.title(), page.url
        detected_cursor = _api_cursor(list(payloads.values()))
        if detected_cursor and (
            cursor is None
            or int(detected_cursor.get("page_value", 0))
            >= int(cursor.get("page_value", 0))
        ):
            cursor = detected_cursor
        await context.close()
        await browser.close()

    return {
        "captured": captured,
        "payloads": list(payloads.values()),
        "title": title,
        "url": url,
        "history_entry": history_entry,
        "replayed": replayed,
        "actions": actions,
        "caught_up": caught_up,
        "cursor": cursor,
    }


def _load_state(db: Path, source_id: str) -> dict[str, Any]:
    with duckdb.connect(str(db)) as con:
        row = con.execute(
            """SELECT pages_completed, last_cursor, unique_payloads,
                      matches_seen, consecutive_empty
               FROM backfill_state WHERE source_id=?""",
            [source_id],
        ).fetchone()
    if row is None:
        return {
            "pages_completed": 0,
            "last_cursor": None,
            "unique_payloads": 0,
            "matches_seen": 0,
            "consecutive_empty": 0,
        }
    cursor = None
    if row[1]:
        try:
            cursor = json.loads(row[1])
        except (TypeError, json.JSONDecodeError):
            cursor = None
    return {
        "pages_completed": int(row[0] or 0),
        "last_cursor": cursor,
        "unique_payloads": int(row[2] or 0),
        "matches_seen": int(row[3] or 0),
        "consecutive_empty": int(row[4] or 0),
    }


def _update_state(
    db: Path,
    source_id: str,
    *,
    status: str,
    pages_completed: int,
    cursor: dict[str, Any],
    unique_payloads: int,
    matches_seen: int,
    consecutive_empty: int,
    error: str | None = None,
) -> None:
    with duckdb.connect(str(db)) as con:
        bounds = con.execute(
            """SELECT min(n.kickoff_time), max(n.kickoff_time)
               FROM source_matches s
               JOIN normalized_matches n USING(match_key)
               WHERE s.source_id=?""",
            [source_id],
        ).fetchone()
        con.execute(
            """INSERT INTO backfill_state
               (source_id, oldest_match_time, newest_match_time, pages_completed,
                last_cursor, last_run_at, status, error_message, unique_payloads,
                matches_seen, consecutive_empty)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET
                 oldest_match_time=excluded.oldest_match_time,
                 newest_match_time=excluded.newest_match_time,
                 pages_completed=excluded.pages_completed,
                 last_cursor=excluded.last_cursor,
                 last_run_at=excluded.last_run_at,
                 status=excluded.status,
                 error_message=excluded.error_message,
                 unique_payloads=excluded.unique_payloads,
                 matches_seen=excluded.matches_seen,
                 consecutive_empty=excluded.consecutive_empty""",
            [
                source_id,
                bounds[0],
                bounds[1],
                pages_completed,
                json.dumps(cursor, ensure_ascii=False, sort_keys=True),
                datetime.now(timezone.utc),
                status,
                error,
                unique_payloads,
                matches_seen,
                consecutive_empty,
            ],
        )


def _record_run(
    db: Path,
    run_id: str,
    source_id: str,
    started: datetime,
    status: str,
    title: str,
    url: str,
    payload_count: int,
    error: str | None,
) -> None:
    with duckdb.connect(str(db)) as con:
        con.execute(
            """INSERT OR REPLACE INTO collection_runs
               (run_id, source_id, started_at, finished_at, status, page_title,
                final_url, json_response_count, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                f"{run_id}-{source_id}-history",
                source_id,
                started,
                datetime.now(timezone.utc),
                status,
                title,
                url,
                payload_count,
                error,
            ],
        )


def backfill(
    config_path: str | Path,
    data_root: str | Path,
    db_path: str | Path | None = None,
    max_actions: int = 50,
    max_replay: int = 500,
) -> dict[str, Any]:
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    db = Path(db_path) if db_path else root / "football.duckdb"
    init_database(db)
    sites = load_sites(config_path)
    run_id = uuid.uuid4().hex
    summary: dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "sources": {},
    }

    # This public endpoint contains objective model inputs. Recommendation and
    # specialist fields are deliberately discarded before normalization.
    source_b = next(site for site in sites if site.id == "source_b")
    try:
        payload = _fetch(MODEL_ENDPOINT, source_b.url)
        captured = datetime.now(timezone.utc)
        raw, digest = _save_raw(root, "source_b_model", captured, payload)
        new_raw = _record_raw_payload(
            db, run_id, "source_b_model", captured, MODEL_ENDPOINT, raw, digest
        )
        features = ingest_model_details(payload, db, run_id, captured, digest)
        summary["source_b_model"] = {
            "status": "ok",
            "new_objective_features": features,
            "new_raw_object": new_raw,
            "raw": str(raw),
        }
    except Exception as exc:
        summary["source_b_model"] = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    for site in sites:
        state = _load_state(db, site.id)
        started = datetime.now(timezone.utc)
        try:
            result = asyncio.run(
                _browser_history(
                    site,
                    max_actions=max_actions,
                    replay_actions=state["pages_completed"],
                    max_replay=max_replay,
                    resume_cursor=state["last_cursor"],
                )
            )
            matches_seen = odds_count = result_count = new_payloads = 0
            for record in result["payloads"]:
                payload = record["payload"]
                raw, digest = _save_raw(root, site.id, result["captured"], payload)
                if _record_raw_payload(
                    db,
                    run_id,
                    site.id,
                    result["captured"],
                    record["url"],
                    raw,
                    digest,
                    method=record["method"],
                    status=record["status"],
                    content_type=record["content_type"],
                ):
                    new_payloads += 1
                matches = adapter_for(site.id).parser(payload)
                if matches:
                    counts = _insert(
                        db, run_id, result["captured"], digest, matches
                    )
                    matches_seen += counts["matches"]
                    odds_count += counts["odds"]
                    result_count += counts["results"]
                if site.id == "source_b" and _contains_key(
                    payload, "modelDetailsResMap"
                ):
                    ingest_model_details(
                        payload, db, run_id, result["captured"], digest
                    )

            if state["last_cursor"] and state["last_cursor"].get("strategy") == "api_page":
                total_pages = state["pages_completed"] + len(result["actions"])
            else:
                total_pages = result["replayed"] + len(result["actions"])
            empty = (
                state["consecutive_empty"] + 1
                if new_payloads == 0 and odds_count == 0 and result_count == 0
                else 0
            )
            status = "caught_up" if result["caught_up"] else "running"
            total_unique = state["unique_payloads"] + new_payloads
            total_matches = state["matches_seen"] + matches_seen
            cursor = result["cursor"] or {
                "strategy": "replay_then_advance",
                "replay_actions": total_pages,
            }
            cursor.update({
                "last_actions": result["actions"][-10:],
                "history_entry": result["history_entry"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            _update_state(
                db,
                site.id,
                status=status,
                pages_completed=total_pages,
                cursor=cursor,
                unique_payloads=total_unique,
                matches_seen=total_matches,
                consecutive_empty=empty,
            )
            summary["sources"][site.id] = {
                "status": status,
                "captured_payloads": len(result["payloads"]),
                "new_payloads": new_payloads,
                "matches_seen": matches_seen,
                "new_odds": odds_count,
                "results_seen": result_count,
                "replayed_pages": result["replayed"],
                "new_pages": len(result["actions"]),
                "resume_page": total_pages,
                "title": result["title"],
            }
            _record_run(
                db,
                run_id,
                site.id,
                started,
                status,
                result["title"],
                result["url"],
                len(result["payloads"]),
                None,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            cursor = state["last_cursor"] or {
                "strategy": "replay_then_advance",
                "replay_actions": state["pages_completed"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _update_state(
                db,
                site.id,
                status="error",
                pages_completed=state["pages_completed"],
                cursor=cursor,
                unique_payloads=state["unique_payloads"],
                matches_seen=state["matches_seen"],
                consecutive_empty=state["consecutive_empty"],
                error=error,
            )
            summary["sources"][site.id] = {"status": "error", "error": error}
            _record_run(
                db, run_id, site.id, started, "error", "", site.url, 0, error
            )

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    output = root / "last-backfill.json"
    temporary = output.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(output)
    return summary
