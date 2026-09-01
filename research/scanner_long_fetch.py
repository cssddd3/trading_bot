"""4년치 일봉 수집 (전 상장사, 1000봉) — 전환 스캐너 장기 검증용. 캐시 재실행 안전."""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from toss.client import TossClient

OUT = config.DATA_DIR / "scanner" / "daily1000"
OUT.mkdir(parents=True, exist_ok=True)
corps = json.loads((config.DATA_DIR / "newsmom" / "corps.json").read_text())
client = TossClient(*config.credentials())
todo = [s for s in corps if not (OUT / f"{s}.json").exists()]
print(f"{len(corps)}종목 중 신규 {len(todo)}종목 (5페이지×200봉)")
for k, sym in enumerate(todo):
    if k % 200 == 0 and k:
        print(f"  ...{k}/{len(todo)}")
    rows, before = [], None
    for _ in range(5):
        try:
            r = client.get_candles(sym, interval="1d", count=200, before=before)
        except Exception:
            break
        rows += r.get("candles", [])
        before = r.get("nextBefore")
        if not before:
            break
        time.sleep(0.055)
    (OUT / f"{sym}.json").write_text(json.dumps(rows))
    time.sleep(0.02)
print("수집 완료")
