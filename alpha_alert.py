# alpha_alert.py
import os, requests, sys

TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

def send(msg: str):
    if not TOKEN or not CHAT_ID:
        print("❌ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수 없음")
        sys.exit(1)
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": msg})
    print("Telegram API response (python):", r.text)
    r.raise_for_status()

if __name__ == "__main__":
    send("🚀 alpha_alert.py 실행 성공! GitHub Actions → Telegram OK")
