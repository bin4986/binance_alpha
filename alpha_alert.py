#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Binance Alpha 상장 감지 → 텔레그램 알림
- API 차단 회피: Alpha 피드 HTML에서 Next.js 초기 JSON 스크레이핑
- 새 글 없으면 '없으면 없음!' 도 보내기(기본 on)
- 최초 실행 시 1회 '연결 정상' 안내 보내기
"""

import os, re, json, time
from pathlib import Path
from typing import Dict, List, Any, Optional
import requests

# ---------- 설정 ----------
ALPHA_URLS = [
    "https://www.binance.com/en/feed/alpha",
    "https://www.binance.com/ko/feed/alpha",
]

TIMEOUT = 20
SEEN_FILE = Path("seen_ids.json")
INIT_FLAG = Path(".alpha_alert_initialized")  # 최초 연결 안내 1회만 보내기용 플래그 파일

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

ALWAYS_NOTIFY_NO_RESULT = os.getenv("ALWAYS_NOTIFY_NO_RESULT", "1") == "1"
NO_RESULT_MESSAGE = os.getenv("NO_RESULT_MESSAGE", "없으면 없음! ✅ (새 상장 알림 없음)")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Referer": "https://www.binance.com/en",
    "Origin": "https://www.binance.com",
}

LISTING_KEYWORDS = [
    "listing", "listed", "new listing", "lists",
    "상장", "거래 개시", "입금", "상장 안내",
    "will list", "listings", "launchpool", "launchpad"
]

RE_EVM = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
RE_TW  = re.compile(r"https?://(?:www\.)?twitter\.com/[A-Za-z0-9_]+", re.IGNORECASE)
RE_SOL = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
RE_NEXT_DATA = re.compile(rb'id="__NEXT_DATA__"[^>]*>\s*({.*?})\s*</script>', re.DOTALL)
RE_APP_DATA  = re.compile(rb'id="__APP_DATA"[^>]*>\s*({.*?})\s*</script>', re.DOTALL)
RE_DATA_STATE= re.compile(rb'data-state="([^"]+)"')  # 일부 페이지는 data-state에 JSON을 담음

# ---------- HTTP ----------
def http_get(url: str) -> bytes:
    r = requests.get(url, headers=DEFAULT_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content

# ---------- JSON 추출 ----------
def _json_from_candidates(html: bytes) -> Optional[dict]:
    m = RE_NEXT_DATA.search(html)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    m = RE_APP_DATA.search(html)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    m = RE_DATA_STATE.search(html)
    if m:
        raw = m.group(1)
        try:
            s = raw.decode("utf-8")
            s = s.replace("&quot;", '"').replace("&amp;", "&").replace("&#x27;", "'")
            return json.loads(s)
        except Exception:
            pass
    return None

# ---------- 로컬 상태 ----------
def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()

def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(list(seen)), ensure_ascii=False), encoding="utf-8")

# ---------- 판별 ----------
def looks_like_listing(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in LISTING_KEYWORDS)

# ---------- 스크레이핑 ----------
def scrape_alpha_feed() -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    def pick_from_tree(obj: Any):
        if isinstance(obj, dict):
            if ("id" in obj or "articleId" in obj or "code" in obj) and ("title" in obj or "brief" in obj or "summary" in obj):
                aid = str(obj.get("id") or obj.get("articleId") or obj.get("code"))
                title = (obj.get("title") or "").strip()
                brief = (obj.get("brief") or obj.get("summary") or "").strip()
                if aid and (looks_like_listing(title) or looks_like_listing(brief)):
                    results.append({
                        "id": aid, "title": title, "brief": brief,
                        "release": obj.get("releaseDate") or obj.get("ctime") or ""
                    })
            for v in obj.values(): pick_from_tree(v)
        elif isinstance(obj, list):
            for v in obj: pick_from_tree(v)

    for url in ALPHA_URLS:
        try:
            html = http_get(url)
            data = _json_from_candidates(html)
            if data:
                pick_from_tree(data)
                if results: break
        except Exception as e:
            print(f"[warn] alpha GET failed: {url} {e}")

    uniq: Dict[str, Dict[str, Any]] = {}
    for a in results: uniq[a["id"]] = a
    return list(uniq.values())

def scrape_alpha_detail(article_id: str) -> str:
    urls = [
        f"https://www.binance.com/en/feed/post/{article_id}",
        f"https://www.binance.com/ko/feed/post/{article_id}",
    ]
    def find_content(o: Any) -> Optional[str]:
        if isinstance(o, dict):
            for k in ("content","body","html","md","markdown","richText"):
                v = o.get(k)
                if isinstance(v, str) and len(v) > 20: return v
            for v in o.values():
                c = find_content(v)
                if c: return c
        elif isinstance(o, list):
            for v in o:
                c = find_content(v)
                if c: return c
        return None

    for url in urls:
        try:
            html = http_get(url)
            data = _json_from_candidates(html)
            if not data: continue
            c = find_content(data)
            if c: return c
        except Exception as e:
            print(f"[warn] detail GET failed: {url} {e}")
    return ""

# ---------- 텔레그램 ----------
def send_telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ TELEGRAM ENV not set; message would be:", text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=TIMEOUT)
    r.raise_for_status()

def extract_refs(text: str) -> Dict[str, List[str]]:
    if not text: return {"evm": [], "sol": [], "twitter": []}
    evm = list(dict.fromkeys(RE_EVM.findall(text)))
    sol = list(dict.fromkeys(RE_SOL.findall(text)))
    tw  = list(dict.fromkeys(RE_TW.findall(text)))
    return {"evm": evm, "sol": sol, "twitter": tw}

def format_message(a: Dict[str, Any], refs: Dict[str, List[str]]) -> str:
    title = a.get("title","").strip()
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
def process_once() -> int:
    seen = load_seen()
    sent = 0

    # 최초 연결 안내(1회)
    if not INIT_FLAG.exists():
        try:
            send_telegram("✅ alpha_alert.py 초기 연결 성공! (GitHub Actions ↔ Telegram OK)")
            INIT_FLAG.write_text("ok", encoding="utf-8")
            print("[info] initial connect message sent")
        except Exception as e:
            print(f"[warn] initial connect notify failed: {e}")

    articles = scrape_alpha_feed()
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
            print(f"[info] sent listing id={aid} title={a.get('title','')[:60]}")
        except Exception as e:
            print(f"[error] telegram send failed for {aid}: {e}")

    save_seen(seen)

    # 새 글이 없으면 '없으면 없음!' 전송 (기본 on)
    if sent == 0 and ALWAYS_NOTIFY_NO_RESULT:
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
            send_telegram(f"{NO_RESULT_MESSAGE} • {ts}")
            print("[info] no-result heartbeat sent")
        except Exception as e:
            print(f"[warn] no-result notify failed: {e}")

    return sent

def main():
    sent = process_once()
    print(f"done. sent={sent}")