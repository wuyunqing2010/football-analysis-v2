from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Response, async_playwright

from .config import SiteConfig
from .privacy import redact_url, sanitize


MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
MAX_JSON_BYTES = 5 * 1024 * 1024
BLOCKED_ACTION_TEXTS = {"确认解锁", "购买", "支付", "提交订单", "立即下单"}


async def _context(browser: Any) -> BrowserContext:
    return await browser.new_context(
        user_agent=MOBILE_USER_AGENT,
        viewport={"width": 390, "height": 844},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        java_script_enabled=True,
    )


async def _click_text(page: Any, text: str, required: bool) -> str:
    if text in BLOCKED_ACTION_TEXTS:
        raise ValueError(f"Blocked unsafe action text: {text}")
    matches = page.get_by_text(text, exact=True)
    for index in range(await matches.count()):
        candidate = matches.nth(index)
        if not await candidate.is_visible():
            continue
        try:
            await candidate.click(timeout=5_000)
            return "normal"
        except Exception:
            await candidate.evaluate("element => element.click()")
            return "dom_fallback"
    if required:
        raise RuntimeError(f"Visible text not found: {text}")
    return "not_found"


async def probe_site(site: SiteConfig, output_root: Path) -> dict[str, Any]:
    run_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc)
    run_dir = output_root / site.id / started_at.strftime("%Y%m%dT%H%M%SZ")
    payload_dir = run_dir / "json"
    payload_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    actions: list[dict[str, Any]] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await _context(browser)
        page = await context.new_page()

        async def capture(response: Response) -> None:
            request = response.request
            if request.resource_type not in {"xhr", "fetch"}:
                return
            content_type = (response.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                return
            try:
                body = await response.body()
                if len(body) > MAX_JSON_BYTES:
                    errors.append(f"Skipped oversized JSON: {redact_url(response.url)}")
                    return
                parsed = json.loads(body.decode("utf-8", errors="replace"))
                safe_payload = sanitize(parsed)
                request_json = None
                if request.post_data:
                    try:
                        request_json = sanitize(json.loads(request.post_data))
                    except (TypeError, json.JSONDecodeError):
                        request_json = "[NON_JSON_BODY_NOT_STORED]"
                encoded = json.dumps(safe_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                digest = hashlib.sha256(encoded).hexdigest()
                filename = f"{len(records) + 1:04d}-{digest[:12]}.json"
                (payload_dir / filename).write_bytes(encoded)
                records.append(
                    {
                        "method": request.method,
                        "status": response.status,
                        "url": redact_url(response.url),
                        "content_type": content_type,
                        "bytes": len(encoded),
                        "sha256": digest,
                        "file": f"json/{filename}",
                        "request_json": request_json,
                    }
                )
            except Exception as exc:  # probe must continue after one bad response
                errors.append(f"{type(exc).__name__}: {redact_url(response.url)}")

        page.on("response", capture)
        status = "ok"
        try:
            await page.goto(site.url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(site.wait_seconds * 1000)
            for dismiss_text in site.dismiss_texts:
                try:
                    method = await _click_text(page, dismiss_text, required=False)
                    actions.append(
                        {
                            "action": "dismiss_text",
                            "text": dismiss_text,
                            "status": "ok",
                            "method": method,
                        }
                    )
                    if method != "not_found":
                        await page.wait_for_timeout(1_000)
                except Exception as exc:
                    actions.append(
                        {
                            "action": "dismiss_text",
                            "text": dismiss_text,
                            "status": "error",
                            "error": type(exc).__name__,
                        }
                    )
            for click_text in site.click_texts:
                try:
                    before_url = page.url
                    method = await _click_text(page, click_text, required=True)
                    await page.wait_for_timeout(site.after_click_wait_seconds * 1000)
                    actions.append(
                        {
                            "action": "click_text",
                            "text": click_text,
                            "status": "ok",
                            "method": method,
                            "before_url": redact_url(before_url),
                            "after_url": redact_url(page.url),
                        }
                    )
                except Exception as exc:
                    actions.append(
                        {
                            "action": "click_text",
                            "text": click_text,
                            "status": "error",
                            "error": type(exc).__name__,
                        }
                    )
            title = await page.title()
            final_url = page.url
            visible_text = (await page.locator("body").inner_text(timeout=10_000))[:4000]
        except Exception as exc:
            status = "error"
            title = ""
            final_url = page.url
            visible_text = ""
            errors.append(f"{type(exc).__name__}: {exc}")
        await context.close()
        await browser.close()

    report = {
        "run_id": run_id,
        "source_id": site.id,
        "source_name": site.name,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "title": title,
        "final_url": redact_url(final_url),
        "json_response_count": len(records),
        "responses": records,
        "actions": actions,
        "visible_text_sample": visible_text,
        "errors": errors,
    }
    (run_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


async def probe_all(sites: list[SiteConfig], output_root: Path) -> list[dict[str, Any]]:
    results = []
    for site in sites:
        results.append(await probe_site(site, output_root))
    return results


def run_probe(sites: list[SiteConfig], output_root: str | Path) -> list[dict[str, Any]]:
    return asyncio.run(probe_all(sites, Path(output_root)))
