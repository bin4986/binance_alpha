#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Binance Alpha 'Listings/상장' 신규 글을 감지해서 텔레그램으로 알려주는 스크립트.
- GitHub Actions에서 주기 실행(워크플로우 아래 참고) 또는 로컬에서 실행 가능
- 404 문제: 구 CMS 경로(/bapi/composite/...) -> 신 CMS 경로(/bapi/apex/.../cms/article/list/query)로 교체
- GitHub Actions의 403 회피용 기본 헤더 포함
- 트위터 링크/컨트랙트 주소(ETH/BNB/Arb 등 EVM, Solana base58) 자동 추출
"""

import os
import re
import json
import time
from pathlib import Path
from typing import Dict, List, Any, Optional

import requests

# ================== 설정 ==================
# 새로 바뀐 CMS API 엔드포인트
CMS_LIST_API = "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query"
CMS_DETAIL_API = "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/detail/query"

# 상장/Listing 카테고리(카탈로그) ID
# (운영에서 변경될 수 있음. 동작 안 하면 catalogId 파라미터를 빼고 키워드 필터에만 의존하도록 바꿔도 됨)
DEFAULT_QUERY = {
    "type": 1,          # 최신순
    "pageNo": 1,
    "pageSize": 30,
    "catalogId": 48     # Listings / New Crypto Listings (변경되면 주석 처리해도 작동함)
}

TIMEOUT = 20
RETRIES = 1           # 403/429 등일 때 재시도 횟수
SLEEP_BETWEEN = 2     # 재시도 대기

SEEN_FILE = Path("seen_ids.json")  # 이미 보낸 글 ID 저장

# 텔레그램
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.binance.com",
    "Referer": "https://www.binance.com/en",
}

# 상장 식별 키워드(제목/요약/본문에 하나라도 있으면 상장으로 간주)
LISTING_KEYWORDS = [
    "listing", "listed", "new listing", "lists",
    "상장", "거래 개시", "입금", "상장 안내"
]

# 컨트랙트/트위터 추출 정규식
RE_EVM = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
RE_TW  = re.compile(r"https?://(?:www\.)?twitter\.com/[A-Za-z0-9_]+", re.IGNORECASE)
# 솔라나(대략 32~44자 base58)
RE_SOL = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# =====================================================

def req_json(url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    r = requests.get(url, params=params, headers=DEFAULT_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def safe_fetch(url: str, params: Dict[str, Any], retries: int = RETRIES) -> Dict[str, Any]:
    for i in range(retries + 1):
        try:
            return req_json(url, params)
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code in (403, 429) and i < retries:
                time.sleep(SLEEP_BETWEEN + i)
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
    SEEN_FILE.write_text(json.dumps(sorted(list(seen))), encoding="utf-8")

def looks_like_listing(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in LISTING_KEYWORDS)

def fetch_listing_articles(pages: int = 1) -> List[Dict[str, Any]]:
    """목록 API에서 글들을 긁어오고, 상장 관련만 필터."""
    result: List[Dict[str, Any]] = []
    for page in range(1, pages + 1):
        params = dict(DEFAULT_QUERY)
        params["pageNo"] = page
        data = safe_fetch(CMS_LIST_API, params=params)
        articles = data.get("data", {}).get("articles") or data.get("data", {}).get("catalogs") or []
        # 응답 스키마가 바뀌더라도 최대한 유연하게 처리
        for a in articles:
            # 대표적으로 'id', 'title', 'brief', 'releaseDate' 등을 가정
            aid = a.get("id") or a.get("articleId") or a.get("code")
            title = a.get("title") or ""
            brief = a.get("brief") or a.get("summary") or ""
            if not aid:
                continue
            if looks_like_listing(title) or looks_like_listing(brief):
                result.append({
                    "id": str(aid),
                    "title": title,
                    "brief": brief,
                    "release": a.get("releaseDate") or a.get("ctime") or "",
                })
    return result

def fetch_detail(article_id: str) -> Dict[str, Any]:
    """상세 API에서 본문 가져오기(트위터/컨트랙트 추출용)."""
    params = {"id": article_id}
    data = safe_fetch(CMS_DETAIL_API, params=params)
    content = data.get("data", {}).get("content") or ""
    # 어떤 응답은 content가 HTML일 수 있음
    return {
        "content": content,
    }

def extract_refs(text: str) -> Dict[str, List[str]]:
    if not text:
        return {"evm": [], "sol": [], "twitter": []}
    evm = list(dict.fromkeys(RE_EVM.findall(text)))
    sol = list(dict.fromkeys(RE_SOL.findall(text)))
    tw  = list(dict.fromkeys(RE_TW.findall(text)))
    return {"evm": evm, "sol": sol, "twitter": tw}

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
    link = f"https://www.binance.com/en/feed/post/{aid}"  # 피드 경로(미러용)
    # 컨트랙트/트위터 요약
    evm = refs["evm"]
    sol = refs["sol"]
    tw  = refs["twitter"]

    lines = [
        "🟡 <b>Binance Alpha: New Listing</b>",
        f"📰 <b>{title}</b>",
        f"🔗 <a href='{link}'>Alpha Post</a>",
    ]
    if evm:
        lines.append("🧾 <b>Contracts</b>\n" + "\n".join(f"• <code>{c}</code>" for c in evm[:6]))
    if sol:
        lines.append("🧾 <b>Solana-like Keys</b>\n" + "\n".join(f"• <code>{c}</code>" for c in sol[:6]))
    if tw:
        lines.append("🐦 <b>Twitter</b>\n" + "\n".join(f"• {u}" for u in tw[:5]))
    return "\n".join(lines)

def process_once(pages: int = 1) -> int:
    seen = load_seen()
    articles = fetch_listing_articles(pages)
    sent = 0
    for a in articles:
        aid = a["id"]
        if aid in seen:
            continue
        # 상세에서 본문 파싱
        try:
            detail = fetch_detail(aid)
        except Exception as e:
            # 상세 실패해도 제목 알림은 보냄
            detail = {"content": ""}
            print(f"[warn] detail fetch failed for {aid}: {e}")

        refs = extract_refs(detail.get("content", ""))
        msg = format_message(a, refs)
        try:
            send_telegram(msg)
            sent += 1
            seen.add(aid)
        except Exception as e:
            print(f"[error] telegram send failed for {aid}: {e}")

    save_seen(seen)
    return sent

def main():
    pages = int(os.getenv("PAGES", "1"))
    sent = process_once(pages)
    print(f"done. sent={sent}")

if __name__ == "__main__":
    main()
