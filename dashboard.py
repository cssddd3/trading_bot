"""웹 대시보드 — 봇 상태를 브라우저로 본다 (읽기 전용).

- 러너가 매 틱 `logs/dashboard.json` 스냅샷을 쓰고, 이 서버는 그 파일만 읽는다
  (토스 API를 직접 부르지 않음 — 토큰/레이트리밋을 봇과 다투지 않는 구조).
- 러너 내부에서 데몬 스레드로 뜬다 (config.DASHBOARD). 봇이 살아있으면 대시보드도 산다.
- **읽기 전용**: 주문/제어 기능 없음. 제어는 텔레그램(/stop /flat ...)만.
- 접속: 같은 컴퓨터에선 http://localhost:8787 , 같은 와이파이의 폰에선 http://<맥IP>:8787

단독 실행도 가능 (봇이 꺼져 있을 때 마지막 스냅샷 보기): python3 dashboard.py
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config

def snap_path(live: bool) -> Path:
    return config.LOG_DIR / ("dashboard_live.json" if live else "dashboard_dryrun.json")


SNAP_PATH = snap_path(True)      # 서버가 서빙할 파일 — start_server에서 모드에 맞게 설정
_BP_CACHE: dict = {"ts": 0.0, "KRW": None, "USD": None}   # 예수금 5분 캐시 (ACCOUNT 1/s 제한)

PAGE = """<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>toss-trader</title>
<style>
:root{--bg:#F7F5F0;--card:#FFF;--ink:#26221C;--mut:#7C7466;--line:#DDD7CB;
--red:#C24A33;--blue:#33608C;--ok:#3E7A4E;--soft:#EFEBE2}
@media (prefers-color-scheme:dark){:root{--bg:#1C1915;--card:#26221D;--ink:#EAE4D8;
--mut:#A39A88;--line:#3D3730;--red:#E07A5F;--blue:#7FA6CC;--ok:#7DB98C;--soft:#2E2A24}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.6 "Noto Sans KR",-apple-system,sans-serif;padding:16px}
main{max-width:760px;margin:0 auto}
h1{font-size:1.15rem;margin:0;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.age{font-size:.78rem;color:var(--mut)}
.pill{font-size:.72rem;border-radius:99px;padding:2px 10px;background:var(--soft)}
.pill.on{background:var(--ok);color:#fff}.pill.off{background:var(--red);color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 16px;margin:14px 0}
.card h2{font-size:.78rem;letter-spacing:.1em;color:var(--mut);margin:0 0 8px;
text-transform:uppercase;font-weight:500}
table{width:100%;border-collapse:collapse;font-size:.9rem}
td,th{padding:6px 4px;border-bottom:1px solid var(--line);text-align:left}
th{font-size:.72rem;color:var(--mut);font-weight:500}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr:last-child td{border-bottom:none}
.up{color:var(--red);font-weight:700}.dn{color:var(--blue);font-weight:700}
.big{font-size:1.5rem;font-weight:700;font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:560px){.grid{grid-template-columns:1fr}}
.mut{color:var(--mut);font-size:.82rem}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{background:var(--soft);border-radius:6px;padding:2px 9px;font-size:.82rem}
ul{margin:4px 0;padding-left:18px;font-size:.85rem}
</style></head><body><main>
<h1>🤖 toss-trader <span id="mode" class="pill"></span>
<span id="halted" class="pill"></span><span class="age" id="age"></span></h1>
<div class="grid">
<div class="card"><h2>평가손익 (보유분)</h2><div class="big" id="unreal">—</div>
<div class="mut" id="realized"></div></div>
<div class="card"><h2>예산 · 예수금</h2><div id="budget" style="font-size:.9rem">—</div></div>
</div>
<div class="card"><h2>보유 포지션</h2>
<table id="pos"><thead><tr><th>종목</th><th class="n">수량</th><th class="n">평단→현재</th>
<th class="n">수익률</th><th class="n">손절선</th></tr></thead><tbody></tbody></table>
<div class="mut" id="nopos" style="display:none">보유 없음</div></div>
<div class="card"><h2>예약 주문</h2><div id="pending" class="mut">없음</div></div>
<div class="card"><h2>오늘 감시 종목</h2><div id="watch" class="chips"></div></div>
<div class="card"><h2>최근 체결</h2><ul id="trades"></ul></div>
<div class="card"><h2>최근 시그널</h2><ul id="signals"></ul></div>
<div class="card"><h2>시스템</h2><div id="sys" class="mut"></div>
<div class="mut">제어는 텔레그램에서: /stop /resume /flat /budget · 이 화면은 읽기 전용</div></div>
</main>
<script>
function fmt(n, d){return n==null?"—":Number(n).toLocaleString("ko-KR",
  {maximumFractionDigits:d==null?0:d, minimumFractionDigits:0})}
function cls(v){return v>0?"up":(v<0?"dn":"")}
async function load(){
  let d;
  try{ d = await (await fetch("/data")).json(); }catch(e){ return; }
  const age = Math.round((Date.now()/1000) - d.ts);
  document.getElementById("age").textContent =
    age<90 ? age+"초 전 갱신" : Math.round(age/60)+"분 전 갱신 (봇 정지?)";
  const m = document.getElementById("mode");
  m.textContent = d.mode; m.className = "pill " + (age<90?"on":"off");
  const h = document.getElementById("halted");
  h.textContent = d.halted ? "매수중지" : "가동중";
  h.className = "pill " + (d.halted?"off":"on");
  const u = d.unrealized_krw;
  const ue = document.getElementById("unreal");
  ue.textContent = (u>0?"+":"") + fmt(u) + "원";
  ue.className = "big " + cls(u);
  document.getElementById("realized").textContent =
    "실현손익 누계 KR " + fmt(d.realized.KR) + "원 · US " + fmt(d.realized.US) + "원";
  document.getElementById("budget").innerHTML = d.budget_lines.join("<br>");
  const tb = document.querySelector("#pos tbody"); tb.innerHTML = "";
  document.getElementById("nopos").style.display = d.positions.length?"none":"block";
  for(const p of d.positions){
    const r = p.ret*100;
    tb.insertAdjacentHTML("beforeend",
      `<tr><td>${p.name||p.symbol}<div class="mut">${p.symbol}</div></td>`+
      `<td class="n">${p.qty}</td><td class="n">${p.px_str}</td>`+
      `<td class="n ${cls(r)}">${r>0?"+":""}${r.toFixed(2)}%</td>`+
      `<td class="n">${p.stop_str}</td></tr>`);
  }
  document.getElementById("pending").textContent = d.pending.length
    ? d.pending.map(p=>`${p.symbol} ${p.action} (다음 시가) — ${p.reason}`).join(" · ") : "없음";
  const w = document.getElementById("watch"); w.innerHTML = "";
  for(const s of d.watch) w.insertAdjacentHTML("beforeend", `<span class="chip">${s}</span>`);
  const tr = document.getElementById("trades"); tr.innerHTML = "";
  for(const t of d.trades) tr.insertAdjacentHTML("beforeend", `<li>${t}</li>`);
  if(!d.trades.length) tr.innerHTML = "<li class='mut'>아직 없음</li>";
  const sg = document.getElementById("signals"); sg.innerHTML = "";
  for(const s of d.signals) sg.insertAdjacentHTML("beforeend", `<li>${s}</li>`);
  if(!d.signals.length) sg.innerHTML = "<li class='mut'>아직 없음</li>";
  document.getElementById("sys").textContent = d.sys;
}
load(); setInterval(load, 10000);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):                          # noqa: N802
        if self.path == "/data":
            try:
                body = SNAP_PATH.read_bytes()
            except OSError:
                body = b"{}"
            ctype = "application/json"
        elif self.path in ("/", "/index.html"):
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                 # 요청 로그로 watch.log 오염 방지
        pass


def start_server(port: int, live: bool = True) -> bool:
    """데몬 스레드로 기동. 포트 사용 중이면 False (다른 인스턴스가 이미 서빙)."""
    global SNAP_PATH
    SNAP_PATH = snap_path(live)
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    except OSError:
        return False
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="dashboard").start()
    return True


def write_snapshot(dr) -> None:
    """러너가 매 틱 호출 — 현재 상태를 dashboard.json으로 (원자적 저장)."""
    import time as _t
    from run_dryrun import now_kst
    path = snap_path(dr.live)
    positions, unreal = [], 0.0
    for s, p in dr.pf.positions.items():
        live = dr.last_price(s) or p["avg_price"]
        us = config.market_of(s) == "US"
        diff = dr.to_krw(s, (live - p["avg_price"]) * p["quantity"])
        unreal += diff
        f = (lambda v: f"${v:,.2f}") if us else (lambda v: f"{v:,.0f}")
        positions.append({
            "symbol": s, "name": dr._names.get(s, ""), "qty": f"{p['quantity']:g}",
            "px_str": f"{f(p['avg_price'])} → {f(live)}",
            "ret": live / p["avg_price"] - 1,
            "stop_str": f(p["stop_price"]) if p.get("stop_price") else "—"})
    if dr.live and dr.broker and _t.time() - _BP_CACHE["ts"] > 300:
        try:
            _BP_CACHE["KRW"] = dr.broker.buying_power("KRW")
            _BP_CACHE["USD"] = dr.broker.buying_power("USD")
            _BP_CACHE["ts"] = _t.time()
        except Exception:                   # noqa: BLE001 - 조회 실패 시 이전 값 유지
            pass
    budget_lines = []
    for m in config.MARKETS:
        used = dr.exposure_krw(m)
        total = dr.budget_total(m)
        line = (f"{m}: 예산 {total:,.0f}원 · 투자중 {used:,.0f}원"
                f" · 여유 {max(0, total - used):,.0f}원")
        bp = _BP_CACHE["KRW"] if m == "KR" else _BP_CACHE["USD"]
        if bp is not None:
            line += (f" · <b>예수금 {bp:,.0f}원</b>" if m == "KR"
                     else f" · <b>예수금 ${bp:,.2f}</b>")
        budget_lines.append(line)
    watch = [f"{s} {dr._names.get(s, '')}".strip() for s in dr.symbols]

    def tail_csv(path, n):
        try:
            lines = Path(path).read_text().strip().splitlines()[1:]
            return [", ".join(x.split(",")[:6]) for x in lines[-n:]][::-1]
        except OSError:
            return []
    snap = {
        "ts": _t.time(), "time": f"{now_kst():%m-%d %H:%M:%S}",
        "mode": dr.tag, "halted": dr.pf.halted,
        "unrealized_krw": round(unreal),
        "realized": {m: round(dr.pf.realized_pnl.get(m, 0)) for m in config.MARKETS},
        "budget_lines": budget_lines, "positions": positions,
        "pending": [{"symbol": s, **p} for s, p in dr.pf.pending.items()],
        "watch": watch,
        "trades": tail_csv(dr.trades_path, 8),
        "signals": tail_csv(dr.signals_path, 8),
        "sys": (f"전략 {dr.key} · 시세 "
                f"{'실시간' if dr.stream and dr.stream.connected else 'REST'}"
                f" · 공시 {'ON' if dr.dart else 'OFF'}"
                f" · LLM {config.LLM_MODELS['scout']}/{config.LLM_MODELS['filter']}"),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False))
    tmp.replace(path)


if __name__ == "__main__":
    port = config.DASHBOARD["port"]
    if start_server(port):
        print(f"대시보드: http://localhost:{port}  (마지막 스냅샷 표시 — 봇이 꺼져 있으면 갱신 안 됨)")
        threading.Event().wait()
    else:
        print(f"포트 {port} 사용 중 — 봇이 이미 서빙 중일 가능성 (http://localhost:{port})")
