"""1단계: 토스증권 API 연동 확인 스크립트 (읽기 전용 — 주문 없음).

실행 전 준비:
  1. .env.example을 .env로 복사하고 client_id/secret을 채운다
  2. pip3 install -r requirements.txt
  3. python3 check_connection.py
"""

import os
import sys

from dotenv import load_dotenv

from toss.client import TossClient

load_dotenv()

CLIENT_ID = os.getenv("TOSS_CLIENT_ID")
CLIENT_SECRET = os.getenv("TOSS_CLIENT_SECRET")

if not CLIENT_ID or not CLIENT_SECRET:
    sys.exit(
        "[!] .env 파일에 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET을 설정하세요.\n"
        "    (.env.example을 .env로 복사해서 채우면 됩니다)"
    )


def main():
    client = TossClient(CLIENT_ID, CLIENT_SECRET)

    # 1. 시세 조회 (삼성전자, SK하이닉스)
    print("=" * 50)
    print("[1/3] 시세 조회 테스트")
    for p in client.get_prices(["005930", "000660"]):
        print(f"  {p.get('symbol')}: {p.get('lastPrice')} {p.get('currency')}")

    # 2. 일봉 캔들 5개
    print("=" * 50)
    print("[2/3] 캔들 조회 테스트 (삼성전자 최근 일봉 5개)")
    for c in client.get_candles("005930", interval="1d", count=5):
        print(f"  {c.get('timestamp', '')[:10]}  "
              f"시가 {c.get('openPrice')}  종가 {c.get('closePrice')}  "
              f"거래량 {c.get('volume')}")

    # 3. 계좌 및 잔고
    print("=" * 50)
    print("[3/3] 계좌 조회 테스트")
    accounts = client.get_accounts()
    if not accounts:
        print("  계좌가 조회되지 않았습니다.")
        return
    for acc in accounts:
        seq = acc.get("accountSeq")
        print(f"  계좌: {acc}")
        holdings = client.get_holdings(str(seq))
        items = holdings.get("items", [])
        if items:
            for it in items:
                print(f"    보유: {it.get('symbol')} {it.get('name')} "
                      f"{it.get('quantity')}주 @ {it.get('averagePurchasePrice')}")
        else:
            print("    보유 종목 없음")

    print("=" * 50)
    print("✅ 연동 성공! 다음 단계(전략 + 백테스트)로 넘어갈 준비가 됐습니다.")


if __name__ == "__main__":
    main()
