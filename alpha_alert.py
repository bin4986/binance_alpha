#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Binance Alpha 'Listings' 신규 글 감지 → 텔레그램 알림
- CMS list/detail: apex 엔드포인트, 반드시 POST + JSON body
- GitHub Actions에서 주기 실행 가능
"""

import os
import re
import json
import time
from pathlib import Path
from typing import Dict, List, Any, Optional

import requests

# ================== 설정 ==================
CMS_LIST_API   = os.getenv(
    "CMS_LIST_API",
    "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
)
CMS_DETAIL_API = os.getenv(
    "CMS_DETAIL_API",
    "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/detail/query",
)

# Listing 카테고리 (변경될 수 있음. 동작 이상하면 catalogId 제거하고 키워드 필터만 사용)
DEFAULT_QUERY = {
    "catalogId": "48",   # Listings / New Crypto Listings
    "pageNo": 1,
    "pageSize": 30
}

TIMEOUT = 20
RETRIES = 1
RETRY_SLEEP = 2

SEEN_FILE = Path("seen_ids.json")

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.binance.com",
    "Referer": "https://www.binance.com/en",
    "Content-Type": "application/json",
}

LISTING_KEYWORDS = [
    "listing", "listed", "new listing", "lists",
    "상장", "거래 개시", "입금", "상장 안내"
]

RE_EVM = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
RE_TW  = re.compile(r"https?://(?:www\.)?twitter\.com/[A-Za-z0-9_]+", re.IGNORECASE)
RE_SOL = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# ================== 유틸 ==================
def post_json(url: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    r = requests.post(url, headers=DEFAULT_HEADERS, json=payload or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def safe_post(url: str, payload: Dict[str, Any], retries: int = RETRIES) -> Dict[str, Any]:
    for i in range(retries + 1):
        try:
            return post_json(url, payload)
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code in (403, 429, 500, 502, 503) and i < retries:
                time.sleep(RETRY_SLEEP + i)
                continue
            raise

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()

def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(list(seen)), ensure_ascii=False, indent=0), encoding="utf-8")

def looks_like_listing(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in LISTING_KEYWORDS)

# ================== 데이터 가져오기 ==================
def fetch_listing_articles(pages: int = 1) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for p in range(1, pages + 1):
        payload = dict(DEFAULT_QUERY)
        payload["pageNo"] = p
        data = safe_post(CMS_LIST_API, payload)

        # 응답 스키마 유연 대응
        dd = data.get("data") or {}
        articles = dd.get("articles") or dd.get("catalogs") or dd.get("list") or []

        for a in articles:
            aid   = str(a.get("id") or a.get("articleId") or a.get("code") or "")
            title = (a.get("title") or "").strip()
            brief = (a.get("brief") or a.get("summary") or "").strip()
            if not aid:
                continue

            if looks_like_listing(title) or looks_like_listing(brief):
                results.append({
                    "id": aid,
                    "title": title,
                    "brief": brief,
                    "release": a.get("releaseDate") or a.get("ctime") or ""
                })
    return results

def fetch_detail(article_id: str) -> Dict[str, Any]:
    payload = {"id": article_id}
    data = safe_post(CMS_DETAIL_API, payload)
    content = (data.get("data") or {}).get("content") or ""
    return {"content": content}

def extract_refs(text: str) -> Dict[str, List[str]]:
    if not text:
        return {"evm": [], "sol": [], "twitter": []}
    evm = list(dict.fromkeys(RE_EVM.findall(text)))
    sol = list(dict.fromkeys(RE_SOL.findall(text)))
    tw  = list(dict.fromkeys(RE_TW.findall(text)))
    return {"evm": evm, "sol": sol, "twitter": tw}

# ================== 텔레그램 ==================
def send_telegram(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ TELEGRAM ENV not set; printing message:\n", msg)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=TIMEOUT)
    r.raise_for_status()

def format_message(a: Dict[str, Any], refs: Dict[str, List[str]]) -> str:
    title = a.get("title", "").strip()
    aid = a.get("id")
    link = f"https://www.binance.com/en/feed/post/{aid}"

    lines = [
        "🟡 <b>Binance Alpha: New Listing</b>",
        f"📰 <b>{title}</b>",
        f"🔗 <a href='{link}'>Alpha Post</a>",
    ]
    if refs["evm"]:
        lines.append("🧾 <b>Contracts</b>\n" + "\n".join(f"• <code>{c}</code>" for c in refs["evm"][:6]))
    if refs["sol"]:
        lines.append("🧾 <b>Solana-like Keys</b>\n" + "\n".join(f"• <code>{c}</code>" for c in refs["sol"][:6]))
    if refs["twitter"]:
        lines.append("🐦 <b>Twitter</b>\n" + "\n".join(f"• {u}" for u in refs["twitter"][:5]))
    return "\n".join(lines)

# ================== 메인 루틴 ==================
def process_once(pages: int = 1) -> int:
    seen = load_seen()
    sent = 0

    articles = fetch_listing_articles(pages)
    for a in articles:
        aid = a["id"]
        if aid in seen:
            continue

        try:
            detail = fetch_detail(aid)
        except Exception as e:
            print(f"[warn] detail fetch failed for {aid}: {e}")
            detail = {"content": ""}

        refs = extract_refs(detail.get("content", ""))
        msg = format_message(a, refs)

        try:
            send_telegram(msg)
            seen.add(aid)
            sent += 1
        except Exception as e:
            print(f"[error] telegram send failed for {aid}: {e}")

    save_seen(seen)
    return sent

def main():
    pages = int(os.getenv("PAGES", "1"))
    # 안전장치: 구 URL 남아있으면 바로 예외
    if "composite/v1/public/cms" in CMS_LIST_API:
        raise RuntimeError("Wrong CMS URL. Use /bapi/apex/v1/public/apex/cms/article/list/query")
    sent = process_once(pages)
    print(f"done. sent={sent}")

if __name__ == "__main__":
    main()
