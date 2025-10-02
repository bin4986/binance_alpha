import os, time, json, re, html
import requests
from urllib.parse import urljoin, urlencode
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "900"))

# Binance CMS API (New Cryptocurrency Listing = catalogId 48)
BINANCE_API = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
CATALOG_ID = 48  # New Cryptocurrency Listing

SEEN_FILE = "seen_alpha_ids.json"

# 체인별 익스플로러 (필요 시 추가)
EXPLORERS = {
    "ETH": "https://etherscan.io/address/{addr}",
    "BNB": "https://bscscan.com/address/{addr}",
    "Base": "https://basescan.org/address/{addr}",
    "Solana": "https://solscan.io/token/{addr}",
    "Sui": "https://suiscan.xyz/mainnet/object/{addr}",
    "TON": "https://tonviewer.com/{addr}",
    "Arbitrum": "https://arbiscan.io/address/{addr}",
    "Polygon": "https://polygonscan.com/address/{addr}",
}

HEADERS = {
    "User-Agent": "AlphaAlertBot/1.0 (+https://binance.com)"
}

def tg_send(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True})

def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_seen(seen_ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen_ids), f, ensure_ascii=False)

def fetch_new_listing_articles(page_no=1, page_size=30):
    params = {"type": 1, "pageNo": page_no, "pageSize": page_size, "catalogId": CATALOG_ID}
    r = requests.get(BINANCE_API, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data.get("data", {}).get("articles", []) or []

def is_alpha_new_listing(title: str) -> bool:
    t = (title or "").lower()
    if "will be available on binance alpha" in t:
        return True
    # 여지는 남겨두되, “alpha listing” 뉘앙스만 잡고 promos/edits는 제외
    if "binance alpha" in t and "will be available" in t:
        return True
    return False

def is_noise(title: str) -> bool:
    t = (title or "").lower()
    # 프로모션/수정/비상장성 글 필터
    noise_keywords = ["promotion", "promo", "fee", "update", "amend", "adjustment", "contest", "campaign"]
    return any(k in t for k in noise_keywords)

def build_announcement_link(code, slug):
    # 코드/슬러그 조합으로 상세 공지 링크 구성
    # 새 형식(“/detail/<code>”)이 보편적이므로 우선 사용
    base = "https://www.binance.com/en/support/announcement/detail/"
    return urljoin(base, code) if code else f"https://www.binance.com/en/support/announcement/{slug or ''}"

def get_article_detail_html(link):
    try:
        r = requests.get(link, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.text
    except Exception:
        return ""

def extract_official_links_and_contracts(page_html: str):
    """
    - 공지 본문이나 ‘Project Links’, ‘Official Links’ 섹션에서
      X(Twitter)/Website/Docs를 우선 수집
    - 공식 사이트를 1~2 hop 팔로우하여 컨트랙트를 추출(0x..., Solana base58, TON Eq..., Sui 0x...)
    - 반드시 공식 도메인/문서에서만 추출
    """
    soup = BeautifulSoup(page_html, "html.parser")

    # 1) 공지 내 X(Twitter) / Website 링크
    x_handle = None
    official_sites = []

    for a in soup.select("a[href]"):
        href = a["href"].strip()
        text = (a.get_text(strip=True) or "").lower()
        # 트위터 링크
        if "x.com" in href or "twitter.com" in href:
            x_handle = href
        # 공식 사이트 후보
        if any(k in text for k in ["official site", "website", "project website", "docs"]) or \
           re.search(r"(official|project|website|docs)", text, re.I):
            official_sites.append(href)
        # Sometimes they just list project domain
        if re.match(r"^https?://[A-Za-z0-9\.\-]+/?$", href):
            official_sites.append(href)

    official_sites = list(dict.fromkeys(official_sites))  # dedupe

    # 2) 공지 본문에서 컨트랙트 패턴 추출(안정성 위해 ‘공식’ 링크에서도만 시도)
    #    먼저 공지 본문에서 직접 패턴 스캔
    contracts = find_contracts_in_text(soup.get_text(" ", strip=True))

    # 3) 공식 사이트/문서 1~2개만 추적해서 추가 추출 (너무 깊게 크롤링하지 않음)
    for site in official_sites[:2]:
        try:
            rr = requests.get(site, headers=HEADERS, timeout=20)
            txt = rr.text
            # 보안상: 외부 사이트에서 추출한 건 "참고"로만, 공지/문서 페이지의 명시 구간일 때만 사용 권장
            contracts.update(find_contracts_in_text(BeautifulSoup(txt, "html.parser").get_text(" ", strip=True)))
            # 트위터 재발견
            if not x_handle:
                m = re.search(r"https?://(x\.com|twitter\.com)/[A-Za-z0-9_]+", txt)
                if m:
                    x_handle = m.group(0)
        except Exception:
            pass

    # 체인별 주소 정리(출처 불명확시 제외 권장 → 여기선 발견만; 실제 알림 전에는 ‘공식 출처’ 여부 판단 필요)
    normalized = normalize_contracts(contracts)
    return x_handle, normalized

def find_contracts_in_text(text: str):
    """
    문자열에서 주소 패턴을 찾아 임시 수집.
    실제 사용 전 '공식 출처' 확인 필요.
    """
    found = set()
    t = text or ""

    # EVM류 (ETH/BNB/Base/Arbitrum/Polygon/Sui의 0x… 형태) - Sui도 0x가 많음
    for m in re.finditer(r"\b0x[a-fA-F0-9]{38,64}\b", t):
        found.add(m.group(0))

    # Solana: base58 대략 32~44자
    for m in re.finditer(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", t):
        found.add(m.group(0))

    # TON: ‘EQ’로 시작하는 주소가 흔함
    for m in re.finditer(r"\bEQA?[A-Za-z0-9_\-]{30,}\b", t):
        found.add(m.group(0))

    return found

def normalize_contracts(raw_set):
    """
    주소 문자열만으로 체인을 100% 단정하긴 어렵지만,
    공지/문서 문맥에서 보조 키워드로 체인 라벨링을 시도할 수 있음.
    여기선 알림 포맷을 위해 단순 라벨 placeholder만 준비.
    """
    # 실제 배포 전에, 공지 본문 주변 문구(“on BNB Chain”, “on Base” 등)로 라벨링 강화 권장
    return {"Unknown": sorted(raw_set)} if raw_set else {}

def format_contract_lines(contracts_by_chain):
    if not contracts_by_chain:
        return "Contracts: (not provided)"
    parts = []
    for chain, addrs in contracts_by_chain.items():
        for addr in addrs:
            # 체인별 익스플로러가 있으면 링크, 없으면 생략
            if chain in EXPLORERS:
                parts.append(f"{chain}: {addr} [{EXPLORERS[chain].format(addr=addr)}]")
            else:
                parts.append(f"{chain}: {addr}")
    return " | " + " ; ".join(parts) if parts else " | Contracts: (not provided)"

def process_once(seen_ids):
    articles = fetch_new_listing_articles()
    new_hits = []
    for art in articles:
        code = art.get("code")  # detail code
        title = art.get("title", "")
        id_ = art.get("id") or code or title
        slug = art.get("slug")
        if id_ in seen_ids:
            continue
        if is_noise(title):
            continue
        if not is_alpha_new_listing(title):
            continue

        link = build_announcement_link(code, slug)
        # 세부 HTML을 열어 토큰명/티커, 공식 링크, 컨트랙트 추출
        html_text = get_article_detail_html(link)
        x_handle, contracts = extract_official_links_and_contracts(html_text)

        # 제목에서 토큰명/티커 추출 (예: “EVAA (EVAA) Will Be Available …”)
        m = re.search(r"([A-Za-z0-9\.\-_ ]+)\s*\(([A-Z0-9\-]{2,15})\)", title)
        if m:
            token_name = m.group(1).strip()
            ticker = m.group(2).strip()
        else:
            token_name = title
            ticker = "N/A"

        # 알림 구성
        x_display = f" | X: {x_handle}" if x_handle else ""
        contract_line = format_contract_lines(contracts)
        msg = f"🚨 Alpha listing: {token_name} ({ticker}) — {link}{x_display}{contract_line}"
        new_hits.append((id_, msg))

    for id_, msg in reversed(new_hits):  # 오래된 것부터 알림
        tg_send(msg)
        seen_ids.add(id_)
    return seen_ids, len(new_hits)

def main_loop():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 .env에 필요합니다.")
    seen = load_seen()
    tg_send("✅ Alpha Listing Monitor started (15m interval).")
    while True:
        try:
            seen, cnt = process_once(seen)
            if cnt == 0:
                # 필요하면 조용히 대기. 아래 줄을 주석 해제하면 '신규 없음' 메시지를 주기적으로 보냄
                # tg_send("No new Alpha listings yet.")
                pass
            save_seen(seen)
        except Exception as e:
            tg_send(f"⚠️ Monitor error: {e}")
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main_loop()
