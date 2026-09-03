from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException, Query, Request
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE = "https://fanqienovel.com"
UA = (
    "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"
)

app = FastAPI(title="Fanqie Public Web -> Legado", version="0.3.0")
log = logging.getLogger("fanqie")

NAVIGATION_TIMEOUT_MS = 15_000
SEARCH_RESULTS_TIMEOUT_MS = 12_000
ENDPOINT_TIMEOUT_SECONDS = 35
SEARCH_API_TIMEOUT_SECONDS = 15


def _allowed_url(value: str, prefixes: tuple[str, ...]) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "unsupported URL scheme")
    if parsed.hostname not in {"fanqienovel.com", "www.fanqienovel.com"}:
        raise HTTPException(400, "only fanqienovel.com URLs are allowed")
    if not any(parsed.path.startswith(p) for p in prefixes):
        raise HTTPException(400, "unsupported Fanqie URL path")
    return value


def _has_private_use_chars(text: str) -> bool:
    return any(
        ("\uE000" <= ch <= "\uF8FF")
        or ("\U000F0000" <= ch <= "\U000FFFFD")
        or ("\U00100000" <= ch <= "\U0010FFFD")
        for ch in text
    )


def _api_url(request: Request, path: str, target: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}{path}?url={quote(target, safe='')}"


async def _new_page(playwright):
    browser = await playwright.chromium.launch(
        headless=True,
        timeout=15_000,
        args=["--disable-dev-shm-usage", "--no-sandbox"],
    )
    context = await browser.new_context(user_agent=UA, locale="zh-CN")
    context.set_default_timeout(8_000)
    context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
    page = await context.new_page()
    return browser, page


async def _close_browser(browser, request_id: str) -> None:
    if browser is None:
        return
    try:
        await asyncio.wait_for(browser.close(), timeout=5)
    except Exception as exc:
        log.warning("request_id=%s stage=browser_close error=%r", request_id, exc)


def _consume_task_result(task: asyncio.Task) -> None:
    """Consume a cancelled background task without delaying the HTTP response."""
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


def _search_public_api_sync(query: str, request_id: str):
    params = urlencode({
        "filter": "",
        "page_count": 30,
        "page_index": 0,
        "query_type": 0,
        "query_word": query,
    })
    target = f"{BASE}/api/author/search/search_book/v1?{params}"
    req = UrlRequest(target, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{BASE}/search/{quote(query)}",
    })
    started = time.monotonic()
    with urlopen(req, timeout=SEARCH_API_TIMEOUT_SECONDS) as response:
        body = response.read()
        headers = {key.lower(): value for key, value in response.headers.items()}
        status = response.status

    blocked = bool(headers.get("bdturing-verify") or headers.get("x-vc-bdturing-parameters"))
    debug = {
        "requestId": request_id,
        "elapsedMs": round((time.monotonic() - started) * 1000),
        "searchApi": {"status": status, "blocked": blocked, "bytes": len(body)},
    }
    if blocked:
        return [], debug
    if not body:
        raise RuntimeError("Fanqie search API returned an empty response")

    payload = json.loads(body)
    data = payload.get("data") or {}
    raw_books = data.get("search_book_data_list") or []
    rows = []
    for book in raw_books:
        book_id = str(book.get("book_id") or book.get("bookId") or "").strip()
        name = book.get("book_name") or book.get("bookName") or ""
        if isinstance(name, dict):
            name = name.get("text") or name.get("str") or ""
        name = re.sub(r"\s+", " ", str(name)).strip()
        if book_id and name:
            rows.append({"name": name, "href": f"{BASE}/page/{book_id}"})
    return rows, debug


async def _search_public_api(query: str, request_id: str):
    return await asyncio.wait_for(
        asyncio.to_thread(_search_public_api_sync, query, request_id),
        timeout=SEARCH_API_TIMEOUT_SECONDS + 2,
    )


async def _search_with_browser(url: str, request_id: str):
    started = time.monotonic()
    browser = None
    api_state = {"seen": False, "status": None, "blocked": False, "error": None}
    header_tasks: list[asyncio.Task] = []

    async def inspect_search_response(response):
        if "/api/author/search/search_book/" not in response.url:
            return
        api_state["seen"] = True
        api_state["status"] = response.status
        try:
            headers = await response.all_headers()
            api_state["blocked"] = bool(
                headers.get("bdturing-verify") or headers.get("x-vc-bdturing-parameters")
            )
        except Exception as exc:
            api_state["error"] = type(exc).__name__

    def on_response(response):
        task = asyncio.create_task(inspect_search_response(response))
        header_tasks.append(task)

    try:
        async with async_playwright() as p:
            log.info("request_id=%s stage=browser_launch", request_id)
            browser, page = await _new_page(p)
            page.on("response", on_response)
            log.info("request_id=%s stage=navigate url=%s", request_id, url)
            await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)

            try:
                await page.wait_for_function(
                    """() => document.querySelectorAll("a[href^='/page/']").length > 0 ||
                        !document.body.innerText.includes('获取搜索结果中，请稍等')""",
                    timeout=SEARCH_RESULTS_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError:
                log.warning("request_id=%s stage=wait_results outcome=timeout", request_id)

            if header_tasks:
                await asyncio.gather(*header_tasks, return_exceptions=True)
            rows = await page.locator("a[href^='/page/']").evaluate_all(
                "els => els.map(a => ({name:(a.innerText||'').trim(), href:a.href}))"
            )
            page_title = await page.title()
            loading = await page.locator("text=获取搜索结果中，请稍等").count() > 0
            elapsed_ms = round((time.monotonic() - started) * 1000)
            log.info(
                "request_id=%s stage=complete rows=%d loading=%s api=%s elapsed_ms=%d",
                request_id, len(rows), loading, api_state, elapsed_ms,
            )
            return rows, {
                "requestId": request_id,
                "elapsedMs": elapsed_ms,
                "pageTitle": page_title,
                "loading": loading,
                "searchApi": api_state,
            }
    finally:
        await _close_browser(browser, request_id)


@app.get("/health")
async def health():
    return {"ok": True, "service": "fanqie-legado-source", "version": "0.3.0"}


@app.get("/search")
async def search(request: Request, q: str = Query(min_length=1, max_length=80)):
    query = q.strip()
    request_id = uuid.uuid4().hex[:12]
    try:
        rows, debug = await _search_public_api(query, request_id)
    except (TimeoutError, PlaywrightTimeoutError) as exc:
        log.exception("request_id=%s stage=search outcome=timeout", request_id)
        raise HTTPException(504, {
            "code": "fanqie_search_timeout",
            "message": "番茄公开搜索页在限定时间内未返回结果",
            "requestId": request_id,
            "timeoutSeconds": ENDPOINT_TIMEOUT_SECONDS,
        }) from exc
    except Exception as exc:
        log.exception("request_id=%s stage=search outcome=error", request_id)
        raise HTTPException(502, {
            "code": "fanqie_search_failed",
            "message": "番茄公开搜索页访问失败",
            "requestId": request_id,
            "errorType": type(exc).__name__,
        }) from exc

    if debug["searchApi"]["blocked"]:
        raise HTTPException(503, {
            "code": "fanqie_public_search_verification_required",
            "message": "番茄公开搜索接口要求交互式验证，本服务不会绕过该限制",
            **debug,
        })
    out, seen = [], set()
    for row in rows:
        href = row.get("href") or ""
        name = re.sub(r"\s+", " ", row.get("name") or "").strip()
        if "/page/" not in href or not name or href in seen:
            continue
        seen.add(href)
        out.append({
            "name": name,
            "bookUrl": _api_url(request, "/info", href),
            "sourceUrl": href,
        })

    return {"query": query, "count": len(out), "books": out[:30], "debug": debug}


@app.get("/info")
async def info(request: Request, url: str):
    url = _allowed_url(url, ("/page/",))
    try:
        async with async_playwright() as p:
            browser, page = await _new_page(p)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1200)

                def one(selector: str):
                    return page.locator(selector).first

                title = (await one("h1").inner_text()).strip() if await one("h1").count() else ""
                author = ""
                for selector in [".author-name-text", ".author-name", "[class*='author-name']"]:
                    loc = one(selector)
                    if await loc.count():
                        author = (await loc.inner_text()).strip()
                        if author:
                            break

                intro = ""
                for selector in [".page-abstract-content", "[class*='abstract']"]:
                    loc = one(selector)
                    if await loc.count():
                        intro = re.sub(r"\s+", " ", (await loc.inner_text()).strip())
                        if intro:
                            break

                cover = ""
                for selector in ["img.book-cover-img", "img[class*='cover']", "img"]:
                    loc = one(selector)
                    if await loc.count():
                        cover = await loc.get_attribute("src") or ""
                        if cover:
                            cover = urljoin(BASE, cover)
                            break

                chapters_raw = await page.locator("a[href*='/reader/']").evaluate_all(
                    "els => els.map(a => ({name:(a.innerText||'').trim(), href:a.href}))"
                )
            finally:
                await browser.close()
    except PlaywrightTimeoutError:
        raise HTTPException(504, "Fanqie detail page timed out")

    chapters, seen = [], set()
    for row in chapters_raw:
        href = row.get("href") or ""
        name = re.sub(r"\s+", " ", row.get("name") or "").strip()
        if "/reader/" not in href or not name or href in seen:
            continue
        seen.add(href)
        chapters.append({
            "name": name,
            "url": _api_url(request, "/content", href),
            "sourceUrl": href,
        })

    current_api_url = _api_url(request, "/info", url)
    return {
        "name": title,
        "author": author,
        "intro": intro,
        "coverUrl": cover,
        "bookUrl": current_api_url,
        "tocUrl": current_api_url,
        "sourceUrl": url,
        "chapterCount": len(chapters),
        "chapters": chapters,
    }


@app.get("/content")
async def content(url: str):
    url = _allowed_url(url, ("/reader/",))
    try:
        async with async_playwright() as p:
            browser, page = await _new_page(p)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1200)
                body = page.locator("div.muye-reader-content").first
                if not await body.count():
                    return {"readable": False, "reason": "public_content_container_not_found", "content": ""}
                text = (await body.inner_text()).strip()
            finally:
                await browser.close()
    except PlaywrightTimeoutError:
        raise HTTPException(504, "Fanqie reader page timed out")

    if not text:
        return {"readable": False, "reason": "empty_public_content", "content": ""}
    if _has_private_use_chars(text):
        return {"readable": False, "reason": "obfuscated_private_use_characters", "content": ""}

    return {"readable": True, "reason": None, "content": text}
