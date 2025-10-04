#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time
from pathlib import Path
from typing import Dict, List, Any, Optional
import requests

# ===== 엔드포인트 (POST 전용) =====
CMS_LIST_API = os.getenv("CMS_LIST_API", "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query")
CMS_DETAIL_API = os.getenv("CMS_DETAIL_API", "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/detail/query")

TIMEOUT, RETRIES, RETRY_SLEEP = 20, 1, 2

SEEN_FILE = Path("seen_ids.json")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 헤더(403/지역 이슈 회피 + JSON POST)
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Origin": "https://www.binance.com",
    "Referer": "https://www.binance.com/en",
    "Content-Type": "application/json",
    # 약간의 호환성 헤더(일부 리전에서 필요할 때가 있음)
    "X-UI-LANG": "en",
}

# 상장 판별 키워드
LISTING_KEYWORDS = ["listing","listed","new listing","lists","상장","거래 개시","입금","상장 안내"]

RE_EVM = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
RE_TW  = re.compile(r"https?://(?:www\.)?twitter\.com/[A-Za-z0-9_]+", re.IGNORECASE)
RE_SOL = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# ---------- 공통 유틸 ----------
def post_json(url: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    r = requests.post(url, headers=DEFAULT_HEADERS, json=payload or {}, timeout=TIMEOUT)
    try:
        r.raise_for_status()
    except requests.HTTPError:
        # 디버그용: 서버가 준 에러 본문을 같이 출력
        print(f"[HTTP {r.status_code}] body: {r.text[:500]}")
        raise
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
    SEEN_FILE.write_text(json.dumps(sorted(list(seen)), ensure_ascii=False), encoding="utf-8")

def looks_like_listing(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in LISTING_KEYWORDS)

# ---------- 목록 가져오기 ----------
def fetch_listing_articles(pages: int = 1) -> List[Dict[str, Any]]:
    """
    리전/버전에 따라 list/query 페이로드 스키마가 다를 수 있어서
    여러 후보(payload variants)를 순차 시도한다.
    """
    results: List[Dict[str, Any]] = []

    def try_one(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = safe_post(CMS_LIST_API, payload)
        dd = data.get("data") or {}
        # 응답 스키마도 가끔 다름
        articles = dd.get("articles") or dd.get("catalogs") or dd.get("list") or []
        picked: List[Dict[str, Any]] = []
        for a in articles:
            aid   = str(a.get("id") or a.get("articleId") or a.get("code") or "")
            title = (a.get("title") or "").strip()
            brief = (a.get("brief") or a.get("summary") or "").strip()
            if not aid:
                continue
            if looks_like_listing(title) or looks_like_listing(brief):
                picked.append({
                    "id": aid,
                    "title": title,
                    "brief": brief,
                    "release": a.get("releaseDate") or a.get("ctime") or ""
                })
        return picked

    for p in range(1, pages + 1):
        # 페이로드 후보들(가장 가능성 높은 순서)
        variants = [
            {"catalogId": "48", "pageNo": p, "pageSize": 30},
            {"catalogId": 48,   "pageNo": p, "pageSize": 30},
            {"pageNo": p, "pageSize": 30, "type": 1},
            {"pageNo": p, "pageSize": 30, "type": 1, "lang": "en"},
            {"pageNo": p, "pageSize": 30, "catalogs": [48]},
            {"pageNo": p, "pageSize": 30, "lang": "en"},
        ]

        last_err: Optional[Exception] = None
        picked: List[Dict[str, Any]] = []
        for pay in variants:
            try:
                picked = try_one(pay)
                # 데이터가 비어 있어도 200이면 성공인 셈이므로 break
                break
            except Exception as e:
                last_err = e
                continue
        if picked:
            results.extend(picked)
        elif last_err:
            # 모든 후보 실패 → 마지막 에러 다시 던지되, 어떤 페이로드들을 썼는지 보여줌
            print("[debug] all payload variants failed for page", p, "variants=", variants)
            raise last_err

    return results

# ---------- 상세 + 레퍼런스 추출 ----------
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

# ---------- 텔레그램 ----------
def send_telegram(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ TELEGRAM ENV not set; printing message:\n", msg)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
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

# ---------- 메인 ----------
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
            seen.add(aid); sent += 1
        except Exception as e:
            print(f"[error] telegram send failed for {aid}: {e}")
    save_seen(seen)
    return sent

def main():
    pages = int(os.getenv("PAGES", "1"))
    if "composite/v1/public/cms" in CMS_LIST_API:
        raise RuntimeError("Wrong CMS URL. Use apex POST endpoint.")
    sent = process_once(pages)
    print(f"done. sent={sent}")

if __name__ == "__main__":
    main()
