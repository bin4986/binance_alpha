#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time
from pathlib import Path
from typing import Dict, List, Any, Optional
import requests

ALPHA_URLS = [
    "https://www.binance.com/en/feed/alpha",
    "https://www.binance.com/ko/feed/alpha",
]

TIMEOUT = 20
SEEN_FILE = Path("seen_ids.json")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Referer": "https://www.binance.com/en",
    "Origin": "https://www.binance.com",
}

LISTING_KEYWORDS = [
    "listing", "listed", "new listing", "lists",
    "상장", "거래 개시", "입금", "상장 안내"
]

RE_EVM = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
RE_TW  = re.compile(r"https?://(?:www\.)?twitter\.com/[A-Za-z0-9_]+", re.IGNORECASE)
RE_SOL = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
RE_NEXT_DATA = re.compile(rb'id="__NEXT_DATA__"[^>]*>\s*({.*?})\s*</script>', re.DOTALL)
RE_APP_DATA  = re.compile(rb'id="__APP_DATA"[^>]*>\s*({.*?})\s*</script>', re.DOTALL)
RE_DATA_STATE= re.compile(rb'data-state="([^"]+)"')  # 일부 페이지는 data-state에 JSON을 담음(escape 주의)

def http_get(url: str) -> bytes:
    r = requests.get(url, headers=DEFAULT_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content

def _json_from_candidates(html: bytes) -> Optional[dict]:
    # 1) __NEXT_DATA__
    m = RE_NEXT_DATA.search(html)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 2) __APP_DATA
    m = RE_APP_DATA.search(html)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 3) data-state (html-escaped)
    m = RE_DATA_STATE.search(html)
    if m:
        raw = m.group(1)
        try:
            # HTML 엔티티 디코딩
            s = raw.decode("utf-8")
            s = s.replace("&quot;", '"').replace("&amp;", "&").replace("&#x27;", "'")
            return json.loads(s)
        except Exception:
            pass
    return None

def looks_like_listing(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in LISTING_KEYWORDS)

def scrape_alpha_feed(pages: int = 1) -> List[Dict[str, Any]]:
    """
    Alpha 피드 HTML의 초기 상태(JSON)에서 글 목록 추출.
    페이지 매김은 클라이언트 사이드인 경우가 많아서,
    여러 언어 URL을 시도하고, JSON 트리에서 article list 비슷한 키들을 폭넓게 탐색.
    """
    results: List[Dict[str, Any]] = []

    def pick_from_tree(obj: Any):
        # 트리 전체를 도는 제네릭 탐색: article-like dict들을 수집
        if isinstance(obj, dict):
            # 대표적으로 id/title/brief 조합을 가진 노드 추출
            if ("id" in obj or "articleId" in obj or "code" in obj) and ("title" in obj or "brief" in obj or "summary" in obj):
                aid = str(obj.get("id") or obj.get("articleId") or obj.get("code"))
                title = (obj.get("title") or "").strip()
                brief = (obj.get("brief") or obj.get("summary") or "").strip()
                if aid and (looks_like_listing(title) or looks_like_listing(brief)):
                    results.append({
                        "id": aid,
                        "title": title,
                        "brief": brief,
                        "release": obj.get("releaseDate") or obj.get("ctime") or ""
                    })
            for v in obj.values():
                pick_from_tree(v)
        elif isinstance(obj, list):
            for v in obj:
                pick_from_tree(v)

    # 여러 URL 후보를 시도
    for url in ALPHA_URLS:
        try:
            html = http_get(url)
        except Exception as e:
            print(f"[warn] alpha GET failed: {url} {e}")
            continue
        data = _json_from_candidates(html)
        if not data:
            print(f"[warn] no JSON state found on {url[:60]}...")
            continue
        pick_from_tree(data)
        if results:
            break

    # 중복 제거(같은 글이 여러 트리 경로에서 발견될 수 있음)
    uniq: Dict[str, Dict[str, Any]] = {}
    for a in results:
        uniq[a["id"]] = a
    return list(uniq.values())

def scrape_alpha_detail(article_id: str) -> str:
    # 상세는 /en/feed/post/<id> 형태 페이지에서 본문 텍스트를 JSON 상태로 얻는 식으로 처리
    urls = [
        f"https://www.binance.com/en/feed/post/{article_id}",
        f"https://www.binance.com/ko/feed/post/{article_id}",
    ]
    for url in urls:
        try:
            html = http_get(url)
            data = _json_from_candidates(html)
            if not data:
                continue
            # 본문 텍스트/HTML 비슷한 키를 폭넓게 탐색
            content = ""

            def find_content(o: Any):
                nonlocal content
                if content:
                    return
                if isinstance(o, dict):
                    # 흔한 키 이름들
                    for k in ("content", "body", "html", "md", "markdown", "richText"):
                        if k in o and isinstance(o[k], str) and len(o[k]) > 20:
                            content = o[k]
                            return
                    for v in o.values():
                        find_content(v)
                elif isinstance(o, list):
                    for v in o:
                        find_content(v)

            find_content(data)
            if content:
                return content
        except Exception as e:
            print(f"[warn] detail GET failed: {url} {e}")
            continue
    return ""

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

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()

def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(list(seen)), ensure_ascii=False), encoding="utf-8")

def process_once(pages: int = 1) -> int:
    seen = load_seen()
    sent = 0
    articles = scrape_alpha_feed(pages)
    # 최신만 던지는 정책 유지
    for a in articles:
        aid = a["id"]
        if aid in seen:
            continue
        content = scrape_alpha_detail(aid)
        refs = extract_refs(content)
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
    sent = process_once(pages)
    print(f"done. sent={sent}")

if __name__ == "__main__":
    main()
