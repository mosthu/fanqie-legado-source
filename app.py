from __future__ import annotations

import re
from urllib.parse import quote, urljoin, urlparse

from fastapi import FastAPI, HTTPException, Query
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE = "https://fanqienovel.com"
UA = (
    "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"
)

app = FastAPI(title="Fanqie Public Web -> Legado", version="0.1.0")


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
    # Unicode Private Use Areas often appear in obfuscated page text.
    return any(
        ("\uE000" <= ch <= "\uF8FF")
        or ("\U000F0000" <= ch <= "\U000FFFFD")
        or ("\U00100000" <= ch <= "\U0010FFFD")
        for ch in text
    )


async def _new_page(playwright):
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(user_agent=UA, locale="zh-CN")
    page = await context.new_page()
    return browser, page


@app.get("/health")
async def health():
    return {"ok": True, "service": "fanqie-legado-source", "version": "0.1.0"}


@app.get("/search")
async def search(q: str = Query(min_length=1, max_length=80)):
    url = f"{BASE}/search/{quote(q.strip())}"
    try:
        async with async_playwright() as p:
            browser, page = await _new_page(p)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2500)
                rows = await page.locator("a[href^='/page/']").evaluate_all(
                    "els => els.map(a => ({name:(a.innerText||'').trim(), href:a.href}))"
                )
            finally:
                await browser.close()
    except PlaywrightTimeoutError:
        raise HTTPException(504, "Fanqie search page timed out")

    out = []
    seen = set()
    for row in rows:
        href = row.get("href") or ""
        name = re.sub(r"\s+", " ", row.get("name") or "").strip()
        if "/page/" not in href or not name or href in seen:
            continue
        seen.add(href)
        out.append({"name": name, "bookUrl": href})

    return {"query": q, "count": len(out), "books": out[:30]}


@app.get("/info")
async def info(url: str):
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

    chapters = []
    seen = set()
    for row in chapters_raw:
        href = row.get("href") or ""
        name = re.sub(r"\s+", " ", row.get("name") or "").strip()
        if "/reader/" not in href or not name or href in seen:
            continue
        seen.add(href)
        chapters.append({"name": name, "url": href})

    return {
        "name": title,
        "author": author,
        "intro": intro,
        "coverUrl": cover,
        "bookUrl": url,
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
        return {
            "readable": False,
            "reason": "obfuscated_private_use_characters",
            "content": "",
        }

    return {"readable": True, "reason": None, "content": text}
