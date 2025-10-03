# alpha_alert.py
import os, json, re, time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ================== 설정 ==================
# 바이낸스 알파(상장/리스트) 페이지 URL — 필요시 정확 주소로 바꿔 써
ALPHA_URL = "https://www.binance.com/en/feed/alpha"   # 예시: 상장 관련 글이 모이는 피드
TIMEOUT = 20
SEEN_FILE = Path("seen_ids.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AlphaWatcher/1.0; +https://github.com/)"
}
# ========================================

TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

def send_telegram(text: str):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없습니다.")
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True})
    # 디버그용 출력
    print("Telegram API response (python):", r.text)
    r.raise_for_status()

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")

def get_soup(url: str) -> BeautifulSoup:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def extract_contracts(html: str) -> list[str]:
    """EVM 계열 컨트랙트(0x + 40hex) 주소 탐지 + 스캐너 링크 추출"""
    addrs = set(re.findall(r"(0x[a-fA-F0-9]{40})", html))
    # 스캐너류 링크(etherscan/bscscan/arbitrum/polygonscan 등)도 같이 찾아서 붙임
    scan_links = re.findall(r'https?://(?:www\.)?(?:etherscan|bscscan|arbiscan|polygonscan|snowtrace|basescan|optimistic\.etherscan)\.[^\s"\'<>]+', html)
    return sorted(addrs.union(scan_links))

def extract_twitter_links(soup: BeautifulSoup) -> list[str]:
    links = []
    for a in soup.select('a[href*="twitter.com/"]'):
        href = a.get("href")
        if href and "intent/tweet" not in href:
            links.append(href)
    return sorted(set(links))

def parse_feed_items(soup: BeautifulSoup):
    """
    피드에서 글 카드들 파싱.
    각 카드에 대해 (id, title, url) 반환.
    사이트 구조가 자주 바뀔 수 있어 널널하게 잡음.
    """
    items = []
    # a 태그 중 게시글/상세로 보이는 것들 수집
    for a in soup.select('a[href*="/en/feed/"]'):
        href = a.get("href", "")
        title = (a.get_text(strip=True) or "")[:200]
        if not href or "/en/feed/" not in href:
            continue
        # 절대 URL로 변환
        if href.startswith("/"):
            url = "https://www.binance.com" + href
        else:
            url = href
        # 간단 id: 상세 URL 전체를 id로 사용
        post_id = url
        # 제목에 listing/new/announce 같은 키워드가 있는 것만 우선 필터 (느슨하게)
        kw = title.lower()
        if any(k in kw for k in ["list", "listing", "launch", "announcement", "adds", "trading", "pair"]):
            items.append((post_id, title, url))
    # 중복 제거
    uniq = {}
    for pid, t, u in items:
        uniq[pid] = (pid, t, u)
    return list(uniq.values())

def enrich_details(url: str):
    """상세 페이지 들어가서 트위터/컨트랙트 등 추가 수집"""
    soup = get_soup(url)
    html = str(soup)
    tws = extract_twitter_links(soup)
    contracts = extract_contracts(html)

    # 제목/심볼 후보
    title = soup.title.get_text(strip=True) if soup.title else url
    # 심볼은 ( ... (XYZ) ) 패턴 같은 걸 느슨하게 캐치
    m = re.search(r"\(([A-Z0-9]{2,10})\)", title)
    symbol = m.group(1) if m else None

    return {
        "title": title,
        "symbol": symbol,
        "twitters": tws[:3],         # 너무 많으면 3개까지
        "contracts": contracts[:5],  # 5개까지
    }

def make_message(title: str, url: str, symbol: str | None, tws: list[str], contracts: list[str]) -> str:
    lines = []
    lines.append("🟡 *Binance Alpha – New Listing Detected*")
    lines.append(f"• Title: {title}")
    if symbol:
        lines.append(f"• Symbol: {symbol}")
    lines.append(f"• Source: {url}")
    if tws:
        lines.append("• Twitter: " + ", ".join(tws))
    if contracts:
        lines.append("• Contract: " + ", ".join(contracts))
    return "\n".join(lines)

def main():
    seen = load_seen()
    feed = get_soup(ALPHA_URL)
    candidates = parse_feed_items(feed)

    new_count = 0
    for pid, title, url in candidates:
        if pid in seen:
            continue
        # 상세 들어가서 보강 후 ‘상장’ 키워드인지 재확인(여기선 느슨)
        try:
            detail = enrich_details(url)
        except Exception as e:
            print(f"[warn] detail fetch failed for {url}: {e}")
            continue

        msg = make_message(
            title=detail.get("title") or title,
            url=url,
            symbol=detail.get("symbol"),
            tws=detail.get("twitters", []),
            contracts=detail.get("contracts", []),
        )
        # 전송
        send_telegram(msg)
        # 너무 빠른 연속 호출 회피
        time.sleep(1.5)

        seen.add(pid)
        new_count += 1

    # 새 글이 있을 때만 기록 갱신
    if new_count > 0:
        save_seen(seen)
        print(f"[info] sent {new_count} new alerts")
    else:
        print("[info] no new listing — no alert sent")

if __name__ == "__main__":
    main()
