# alpha_alert.py
# -*- coding: utf-8 -*-
"""
Binance 'New Cryptocurrency Listing' 공지 중
'Will Be Available on Binance Alpha' (또는 Alpha 신규 상장 명시)만 감지하여
텔레그램으로 알림을 보내는 스크립트.

환경변수(Secrets):
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
선택:
- ONCE=1  (설정 시 1회 실행 후 종료; 미설정 시 while 루프로 주기 실행)

요구 라이브러리:
- requests
- beautifulsoup4
"""

from __future__ import annotations

import os
import re
import json
import time
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from bs4 import BeautifulSoup

# ================== 설정 ==================
TIMEOUT = 20
SLEEP_BETWEEN_MSGS = 1.0
LOCALE = "en"                   # 공지 언어 (en 권장: 가장 먼저 업데이트됨)
CATALOG_ID = 48                 # Binance CMS: New Cryptocurrency Listing
PAGE_SIZE = 30                  # 최근 30개 기사 확인
SEEN_FILE = Path("seen_alpha_ids.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AlphaWatcher/1.1; +https://github.com/)",
    "Accept": "application/json, text/plain, */*",
}

CMS_LIST_API = "https://www.binance.com/bapi/composite/v1/public/cms/article/list"
CMS_DETAIL_API = "https://www.binance.com/bapi/composite/v1/public/cms/article/detail"

TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
# =========================================


# ---------- 도우미 ----------
def tg_send(text: str) -> None:
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 필요합니다.")
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
            "parse_mode": "HTML",
        },
        timeout=TIMEOUT,
    )
    # 디버깅용 출력
    print("Telegram:", r.status_code, r.text[:200])
    r.raise_for_status()


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False), encoding="utf-8")


def req_json(url: str, params: dict) -> dict:
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---------- Binance CMS ----------
def fetch_listing_articles(page_no: int = 1) -> List[dict]:
    params = {
        "catalogId": CATALOG_ID,
        "pageNo": page_no,
        "pageSize": PAGE_SIZE,
        "type": 1,
    }
    data = req_json(CMS_LIST_API, params)
    return data.get("data", {}).get("articles", []) or []


def fetch_article_detail(article_code: str) -> dict:
    params = {"articleCode": article_code, "lang": LOCALE}
    data = req_json(CMS_DETAIL_API, params)
    return data.get("data", {}) or {}


def looks_like_alpha_listing(title: str, html: str) -> bool:
    """
    'Will Be Available on Binance Alpha' 또는 Alpha 신규 상장임을 뚜렷이 말하는지 검사
    """
    soup = BeautifulSoup(html or "", "html.parser")
    text = (title or "") + " " + soup.get_text(" ", strip=True)
    low = text.lower()
    # 핵심 패턴
    if "will be available on binance alpha" in low:
        return True
    if "binance alpha" in low and any(k in low for k in ("list", "listing", "launch", "trading")):
        return True
    return False


# ---------- 추출 로직 ----------
TW_DOMAINS = ("twitter.com", "x.com")

EXPLORER_PATTERNS = [
    r"https?://(?:www\.)?etherscan\.io/(?:token|address)/[0-9a-zA-Z]{10,}",
    r"https?://(?:www\.)?bscscan\.com/(?:token|address)/[0-9a-zA-Z]{10,}",
    r"https?://(?:www\.)?basescan\.org/(?:token|address)/[0-9a-zA-Z]{10,}",
    r"https?://(?:www\.)?arbiscan\.io/(?:token|address)/[0-9a-zA-Z]{10,}",
    r"https?://(?:www\.)?polygonscan\.com/(?:token|address)/[0-9a-zA-Z]{10,}",
    r"https?://(?:www\.)?optimistic\.etherscan\.io/(?:token|address)/[0-9a-zA-Z]{10,}",
    r"https?://(?:www\.)?solscan\.io/token/[1-9A-HJ-NP-Za-km-z]{32,44}",
    r"https?://(?:www\.)?tonviewer\.com/[0-9A-Za-z_-]{20,}",
    r"https?://(?:www\.)?tonscan\.org/(?:token|address)/[0-9A-Za-z_-]{20,}",
    r"https?://(?:www\.)?suiscan\.xyz/(?:token|object)/[0-9a-f]{32,}",
    r"https?://(?:www\.)?explorer\.sui\.io/object/[0-9a-f]{32,}",
]

EVM_ADDR_RE = re.compile(r"(0x[a-fA-F0-9]{40})")


def extract_handles_and_contracts(html: str) -> Tuple[List[str], List[str]]:
    soup = BeautifulSoup(html or "", "html.parser")

    # Twitter / X
    handles: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        for dom in TW_DOMAINS:
            if dom in href:
                handles.append(href)
    handles = sorted(set(handles))[:3]

    # Explorers + EVM addresses
    explorers: List[str] = []
    for pat in EXPLORER_PATTERNS:
        explorers += re.findall(pat, html or "")
    explorers = sorted(set(explorers))

    evm_addrs = sorted(set(EVM_ADDR_RE.findall(html or "")))

    # 간단 정규화: 체인 힌트를 넣어주기 위해 도메인별 접두사
    contracts: List[str] = []
    def tag(addr_or_url: str) -> str:
        u = addr_or_url.lower()
        if "etherscan" in u:   chain = "ETH"
        elif "bscscan" in u:   chain = "BNB"
        elif "basescan" in u:  chain = "Base"
        elif "arbiscan" in u:  chain = "Arbitrum"
        elif "polygonscan" in u: chain = "Polygon"
        elif "optimistic.etherscan" in u: chain = "Optimism"
        elif "solscan" in u:   chain = "Solana"
        elif "tonviewer" in u or "tonscan" in u: chain = "TON"
        elif "suiscan" in u or "explorer.sui.io" in u: chain = "Sui"
        else: chain = "EVM"
        return f"{chain}: {addr_or_url}"

    # 탐지된 explorer 링크 우선
    for link in explorers[:8]:
        contracts.append(tag(link))

    # explorer 링크가 전혀 없을 때는 EVM address만 노출(최대 5개)
    if not contracts and evm_addrs:
        for a in evm_addrs[:5]:
            contracts.append(tag(a))

    return handles, contracts


def parse_title_for_token(title: str) -> Tuple[str, str]:
    """
    'Binance Will List ExampleToken (EXM)' 형태에서 토큰명/티커 추출
    """
    t = title or ""
    m = re.search(r"\(([A-Z0-9]{2,10})\)", t)
    ticker = m.group(1) if m else ""
    name = t
    if m:
        # 괄호 앞쪽 부분에서 마지막 대시/콜론 이후 토큰명 비슷한 부분만 남기기
        before = t[: m.start()].strip()
        # 흔한 프리픽스 제거
        before = re.sub(r"^(Binance\s+Will\s+List|Binance\s+Lists|New\s+Cryptocurrency\s+Listing:?)\s+", "", before, flags=re.I)
        name = before.strip(" -:|")
    return name, ticker


def make_alert(token_name: str, ticker: str, url: str, handles: List[str], contracts: List[str]) -> str:
    # 체인별 컨트랙트 정렬: Binance 거래쌍에 쓰일 가능성 높은 ETH/BNB 우선
    priority = ("ETH", "BNB", "Solana", "Base", "Sui", "TON")
    def sort_key(s: str):
        for i, p in enumerate(priority):
            if s.startswith(p + ":"):
                return i
        return len(priority)
    contracts = sorted(contracts, key=sort_key)

    h = handles[0] if handles else ""
    handle_txt = h if not h else f'<a href="{h}">{h}</a>'

    # 컨트랙트는 여러 개일 수 있으므로 공백으로 이어붙임
    ctxt = " ".join(contracts) if contracts else "TBA"

    return (
        f"🚨 Alpha listing: <b>{token_name}</b> ({ticker}) — "
        f'<a href="{url}">Announcement</a> | X: {handle_txt} | Contract(s): {ctxt}'
    )


# ---------- 메인 루틴 ----------
def process_once(seen: set) -> Tuple[set, int]:
    articles = fetch_listing_articles(1)
    sent = 0

    for it in articles:
        code = it.get("code") or it.get("articleCode")
        title = (it.get("title") or "").strip()
        if not code:
            continue
        if code in seen:
            continue

        detail = fetch_article_detail(code)
        html = detail.get("content") or ""
        url = "https://www.binance.com/en/support/announcement/detail/" + code

        if not looks_like_alpha_listing(title, html):
            continue

        token_name, ticker = parse_title_for_token(title)
        handles, contracts = extract_handles_and_contracts(html)

        msg = make_alert(token_name or title, ticker or "", url, handles, contracts)
        tg_send(msg)
        time.sleep(SLEEP_BETWEEN_MSGS)

        seen.add(code)
        sent += 1

    return seen, sent


def main_loop():
    seen = load_seen()
    print("[info] Alpha watcher started (loop mode)")
    while True:
        try:
            seen, sent = process_once(seen)
            if sent:
                save_seen(seen)
                print(f"[info] sent {sent} alert(s)")
            else:
                print("[info] no new alpha listing")
        except Exception as e:
            print("[error]", e)
        # 15분 간격(로컬 테스트 목적). Actions에서는 ONCE=1 권장
        time.sleep(15 * 60)


if __name__ == "__main__":
    ONCE = os.getenv("ONCE") == "1"
    if ONCE:
        seen = load_seen()
        try:
            seen, sent = process_once(seen)
            if sent:
                save_seen(seen)
                print(f"[info] sent {sent} alert(s)")
            else:
                print("[info] no new alpha listing")
        finally:
            pass
    else:
        main_loop()
