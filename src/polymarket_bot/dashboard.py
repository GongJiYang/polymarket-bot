"""Process-shared local web dashboard for strategy telemetry."""

from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from polymarket_bot.contracts import StrategyContext, StrategyEvaluation
from polymarket_bot.runners import Decision

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
HEALTH_MARKER = "polymarket-bot-dashboard-v1"
MAX_BODY_BYTES = 1_000_000
MAX_PRICE_POINTS = 600
_CHAINLINK_STRATEGY_IDS = {
    "chainlink_terminal_spot_v9",
    "eth_chainlink_terminal_spot_v5",
    "sol_chainlink_terminal_spot_v3",
    "xrp_chainlink_terminal_spot_v3",
    "doge_chainlink_terminal_spot_v3",
}
_LOCAL_OPENER = build_opener(ProxyHandler({}))


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "value"):
        return value.value  # type: ignore[union-attr]
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _metrics(evaluation: StrategyEvaluation) -> dict[str, object]:
    return {
        name: _json_value(value) if isinstance(value, (Decimal, datetime)) else value
        for name, value in evaluation.metrics
    }


def build_dashboard_event(
    decision: Decision,
    context: StrategyContext,
    *,
    run_id: str,
    process_id: int,
) -> dict[str, object]:
    evaluations = {
        evaluation.strategy_id: _metrics(evaluation)
        for evaluation in decision.strategy_evaluations
    }
    candidate = decision.candidate
    chainlink_strategy_id = next(
        (
            evaluation.strategy_id
            for evaluation in decision.strategy_evaluations
            if evaluation.strategy_id in _CHAINLINK_STRATEGY_IDS
        ),
        "",
    )
    prices = context.prices[-MAX_PRICE_POINTS:]
    return {
        "run_id": run_id,
        "process_id": process_id,
        "mode": decision.mode,
        "observed_at": decision.observed_at.isoformat(),
        "market": {
            "id": context.market.market_id,
            "title": context.market.title,
            "asset": context.market.asset,
            "interval": context.market.interval.value,
            "window_start": context.market.window_start.isoformat(),
            "window_end": context.market.window_end.isoformat(),
        },
        "source": context.source,
        "opening": str(context.opening),
        "current_spot": [
            context.current_spot[0].isoformat(),
            str(context.current_spot[1]),
        ],
        "prices": [[at.isoformat(), str(price)] for at, price in prices],
        "threshold": str(context.threshold),
        "quantity": str(context.quantity),
        "target_all_in_debit": (
            str(context.target_all_in_debit)
            if context.target_all_in_debit is not None
            else None
        ),
        "evaluations": evaluations,
        "candidate": None
        if candidate is None
        else {
            "strategy_id": candidate.strategy_id,
            "direction": candidate.direction,
            "terminal_probability": str(candidate.terminal_probability),
            "top_ask": str(candidate.top_ask),
            "max_price": str(candidate.max_price),
            "expected_fill_price": str(candidate.expected_fill_price),
            "fee_per_share": str(candidate.fee_per_share),
            "net_edge": str(candidate.net_edge),
            "signal_metric": candidate.signal_metric,
            "signal_strength": str(candidate.signal_strength),
        },
        "execution_state": decision.execution_state,
        "chainlink_strategy_id": chainlink_strategy_id,
    }


class DashboardPublisher:
    """Publish runner observations to one loopback dashboard process."""

    def __init__(self, base_url: str, *, run_id: str | None = None) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("dashboard URL must be a loopback HTTP URL")
        self.base_url = base_url.rstrip("/")
        self.run_id = run_id or uuid.uuid4().hex[:12]

    @classmethod
    def connect(
        cls,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = 4.0,
        open_browser: bool = False,
    ) -> "DashboardPublisher":
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("dashboard must bind to a loopback address")
        publisher = cls(f"http://{host}:{port}")
        if publisher.healthy():
            return publisher
        publisher._spawn(host=host, port=port, open_browser=open_browser)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if publisher.healthy():
                return publisher
            time.sleep(0.05)
        raise RuntimeError(f"dashboard did not start at {publisher.base_url}")

    def healthy(self) -> bool:
        try:
            with _LOCAL_OPENER.open(
                f"{self.base_url}/health", timeout=0.25
            ) as response:
                payload = json.load(response)
            return response.status == 200 and payload.get("service") == HEALTH_MARKER
        except (OSError, URLError, ValueError, json.JSONDecodeError):
            return False

    def publish(self, decision: Decision, context: StrategyContext) -> None:
        event = build_dashboard_event(
            decision,
            context,
            run_id=self.run_id,
            process_id=os.getpid(),
        )
        body = json.dumps(event, default=_json_value, separators=(",", ":")).encode()
        request = Request(
            f"{self.base_url}/api/events",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _LOCAL_OPENER.open(request, timeout=0.5) as response:
                response.read()
        except (OSError, URLError):
            # Telemetry must never change an execution result after submission.
            return

    def _spawn(self, *, host: str, port: int, open_browser: bool) -> None:
        state_dir = Path.home() / ".local" / "state" / "polymarket-bot"
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / "dashboard.log").open("ab", buffering=0) as log:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "polymarket_bot.dashboard",
                    "--host",
                    host,
                    "--port",
                    str(port),
                    *(["--open-browser"] if open_browser else []),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )


class _DashboardState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._runs: dict[str, dict[str, object]] = {}

    def put(self, event: dict[str, object]) -> None:
        run_id = event.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id is required")
        with self._lock:
            self._generation += 1
            event["received_at"] = datetime.now(timezone.utc).isoformat()
            event["generation"] = self._generation
            self._runs[run_id] = event

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            runs = sorted(
                self._runs.values(),
                key=lambda event: int(event["generation"]),
                reverse=True,
            )
            return {"generation": self._generation, "runs": runs}


class _DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        self.state = _DashboardState()
        super().__init__(address, _DashboardHandler)


class _DashboardHandler(BaseHTTPRequestHandler):
    server: _DashboardServer

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, DASHBOARD_HTML.encode(), "text/html; charset=utf-8")
            return
        if path == "/health":
            self._json(200, {"service": HEALTH_MARKER})
            return
        if path == "/api/snapshot":
            self._json(200, self.server.state.snapshot())
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/events":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("invalid content length")
            event = json.loads(self.rfile.read(length))
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
            self.server.state.put(event)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(202, {"accepted": True})

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, payload: object) -> None:
        self._send(
            status,
            json.dumps(payload, default=_json_value, separators=(",", ":")).encode(),
            "application/json",
        )

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def serve(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("dashboard must bind to a loopback address")
    with _DashboardServer((host, port)) as server:
        if open_browser:
            webbrowser.open(f"http://{host}:{port}")
        server.serve_forever(poll_interval=0.25)


def open_dashboard(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    publisher = DashboardPublisher.connect(host=host, port=port)
    webbrowser.open(publisher.base_url)
    return publisher.base_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open-browser", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        serve(
            host=arguments.host,
            port=arguments.port,
            open_browser=arguments.open_browser,
        )
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        publisher = DashboardPublisher(f"http://{arguments.host}:{arguments.port}")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if publisher.healthy():
                return 0
            time.sleep(0.05)
        raise
    return 0


DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket 策略监控</title>
<style>
:root{color-scheme:dark;--bg:#070b12;--panel:#0d1420;--panel2:#111b2a;--line:#213047;--text:#edf5ff;--muted:#8190a5;--cyan:#35d7ff;--blue:#5d7cff;--green:#31e6a1;--amber:#ffbd5c;--red:#ff5f6d;--p:50}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% -10%,#13263a 0,transparent 38%),var(--bg);color:var(--text);font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;min-height:100vh}
.shell{max-width:1480px;margin:auto;padding:22px}.top{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:18px}.brand{display:flex;align-items:center;gap:13px}.mark{width:36px;height:36px;border:1px solid #2e526c;background:linear-gradient(145deg,#163149,#0c1420);display:grid;place-items:center}.mark svg{width:21px;stroke:var(--cyan)}h1{font:700 17px/1.2 ui-sans-serif,system-ui;margin:0;letter-spacing:.02em}.subtitle{color:var(--muted);font-size:11px;margin-top:3px}.connection{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}.dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 12px var(--green)}
.runbar{display:flex;gap:8px;overflow:auto;padding-bottom:10px}.run{border:1px solid var(--line);background:#0a101a;color:var(--muted);padding:8px 11px;cursor:pointer;white-space:nowrap;font:inherit}.run.active{color:var(--text);border-color:#3e7695;background:#102031}.run b{color:var(--cyan);font-weight:600}.hero{border:1px solid var(--line);background:linear-gradient(115deg,#101b29,#0a111c);padding:18px 20px;display:flex;justify-content:space-between;gap:24px;align-items:center;margin-bottom:12px}.market-title{font:650 19px/1.3 ui-sans-serif,system-ui}.meta{display:flex;gap:16px;color:var(--muted);font-size:11px;margin-top:8px;flex-wrap:wrap}.signal{text-align:right}.signal strong{font:700 25px/1 ui-sans-serif,system-ui;color:var(--green)}.signal.none strong{color:var(--muted)}.signal small{display:block;color:var(--muted);margin-top:7px}
.grid{display:grid;grid-template-columns:1.1fr 1.5fr 1fr;gap:12px}.panel{border:1px solid var(--line);background:linear-gradient(155deg,var(--panel2),var(--panel));padding:16px;min-width:0}.panel h2{font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:#9dabc0;margin:0 0 15px}.prob-wrap{display:grid;grid-template-columns:150px 1fr;align-items:center;gap:12px}.gauge{--p:50;width:136px;height:136px;border-radius:50%;background:conic-gradient(var(--cyan) calc(var(--p)*1%),#27354a 0);display:grid;place-items:center;position:relative}.gauge:before{content:"";position:absolute;inset:13px;border-radius:50%;background:var(--panel)}.gauge div{position:relative;text-align:center}.gauge b{font:700 28px/1 ui-sans-serif,system-ui}.gauge small{display:block;color:var(--muted);margin-top:6px}.prob-label{display:flex;justify-content:space-between;border-bottom:1px solid var(--line);padding:9px 0}.prob-label:last-child{border:0}.up{color:var(--cyan)}.down{color:var(--amber)}
.metrics{display:grid;grid-template-columns:repeat(2,1fr);gap:9px}.metric{background:#0a111b;border:1px solid #1c2a3d;padding:12px}.metric span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em}.metric b{display:block;font:650 18px/1.2 ui-sans-serif,system-ui;margin-top:7px}.metric em{font-style:normal;font-size:10px;color:var(--muted)}.positive{color:var(--green)!important}.negative{color:var(--red)!important}.warn{color:var(--amber)!important}
.chart-panel{grid-column:span 2}.canvas-wrap{height:270px;position:relative}canvas{width:100%;height:100%}.legend{display:flex;gap:18px;color:var(--muted);font-size:10px;margin-top:10px}.swatch{display:inline-block;width:16px;height:2px;vertical-align:middle;margin-right:6px}.swatch.price{background:var(--cyan)}.swatch.open{background:var(--amber)}
.table{width:100%;border-collapse:collapse}.table th{text-align:right;color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.1em;padding:0 7px 9px}.table th:first-child,.table td:first-child{text-align:left}.table td{text-align:right;padding:11px 7px;border-top:1px solid var(--line)}.tag{display:inline-block;border:1px solid currentColor;padding:2px 6px;font-size:10px}.depth{margin-top:15px}.depth-row{margin:11px 0}.depth-head{display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-bottom:5px}.track{height:7px;background:#1b2637;overflow:hidden}.fill{height:100%;background:var(--cyan);width:0}.fill.downfill{background:var(--amber)}
.countdown{font:700 31px/1 ui-sans-serif,system-ui;margin:4px 0 16px}.progress{height:4px;background:#1b2637}.progress div{height:100%;background:linear-gradient(90deg,var(--blue),var(--cyan));width:0}.facts{margin-top:18px}.fact{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--line);color:var(--muted);font-size:11px}.fact b{color:var(--text);font-weight:500;max-width:65%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.empty{border:1px dashed #29394f;padding:60px;text-align:center;color:var(--muted);margin-top:20px}.footer{color:#526177;font-size:10px;margin-top:12px;text-align:right}@media(max-width:1000px){.grid{grid-template-columns:1fr 1fr}.chart-panel{grid-column:span 2}}@media(max-width:680px){.shell{padding:12px}.top,.hero{align-items:flex-start}.hero{display:block}.signal{text-align:left;margin-top:18px}.grid{display:block}.panel{margin-bottom:10px}.prob-wrap{grid-template-columns:130px 1fr}.gauge{width:116px;height:116px}.chart-panel{grid-column:auto}.canvas-wrap{height:210px}}
.all-panel{grid-column:1/-1}.all-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:9px}.strategy-block{background:#0a111b;border:1px solid #1c2a3d;padding:12px}.strategy-block h3{font-size:11px;color:var(--cyan);margin:0 0 9px;overflow-wrap:anywhere}.raw-metric{display:flex;justify-content:space-between;gap:14px;padding:5px 0;border-top:1px solid #172438;font-size:10px}.raw-metric span{color:var(--muted);overflow-wrap:anywhere}.raw-metric b{font-weight:500;text-align:right;overflow-wrap:anywhere}
</style>
</head>
<body><main class="shell">
<header class="top"><div class="brand"><div class="mark"><svg viewBox="0 0 24 24" fill="none" stroke-width="1.8"><path d="M4 17 9 12l3 3 8-9"/><path d="M15 6h5v5"/></svg></div><div><h1>Polymarket 策略监控</h1><div class="subtitle">CHAINLINK 终局 TWAP / 本机实时数据</div></div></div><div class="connection"><i class="dot"></i><span id="connection">正在连接</span></div></header>
<div id="runs" class="runbar"></div><div id="empty" class="empty">正在等待机器人评估。请启动 <b>polymarket-bot shadow</b>。</div>
<section id="dashboard" hidden><div class="hero"><div><div id="marketTitle" class="market-title"></div><div class="meta"><span id="marketMeta"></span><span id="observed"></span><span id="pid"></span></div></div><div id="signal" class="signal"><strong>暂无信号</strong><small>概率优势低于阈值</small></div></div>
<div class="grid">
<section class="panel"><h2>终局概率</h2><div class="prob-wrap"><div id="gauge" class="gauge"><div><b id="gaugeValue">--</b><small>上涨公允概率</small></div></div><div><div class="prob-label"><span class="up">上涨</span><b id="upProb">--</b></div><div class="prob-label"><span class="down">下跌</span><b id="downProb">--</b></div><div class="prob-label"><span>市场卖一价</span><b id="marketAsk">--</b></div></div></div></section>
<section class="panel"><h2>决策收益</h2><div class="metrics"><div class="metric"><span>净优势</span><b id="netEdge">--</b><em>公允概率 − 最高价 − 手续费</em></div><div class="metric"><span>最高买入价</span><b id="maxPrice">--</b><em>卖一价 + 一个价格档位</em></div><div class="metric"><span>每份手续费</span><b id="fee">--</b><em>市场手续费曲线</em></div><div class="metric"><span>入场阈值</span><b id="threshold">--</b><em>最低净优势</em></div></div></section>
<section class="panel"><h2>结算倒计时</h2><div id="countdown" class="countdown">--:--</div><div class="progress"><div id="timeProgress"></div></div><div class="facts"><div class="fact"><span>开盘价</span><b id="opening">--</b></div><div class="fact"><span>当前现货价</span><b id="spot">--</b></div><div class="fact"><span>现货价差</span><b id="spotDelta">--</b></div><div class="fact"><span>波动率 / √秒</span><b id="volatility">--</b></div></div></section>
<section class="panel chart-panel"><h2>Chainlink 官方价格路径</h2><div class="canvas-wrap"><canvas id="priceChart"></canvas></div><div class="legend"><span><i class="swatch price"></i>官方价格</span><span><i class="swatch open"></i>窗口开盘价</span></div></section>
<section class="panel"><h2>可成交报价矩阵</h2><table class="table"><thead><tr><th>方向</th><th>卖一价</th><th>最高价</th><th>手续费</th><th>净优势</th></tr></thead><tbody id="quotes"></tbody></table><div class="depth"><div class="depth-row"><div class="depth-head"><span>上涨方向深度</span><b id="upDepthText">--</b></div><div class="track"><div id="upDepth" class="fill"></div></div></div><div class="depth-row"><div class="depth-head"><span>下跌方向深度</span><b id="downDepthText">--</b></div><div class="track"><div id="downDepth" class="fill downfill"></div></div></div></div></section>
<section class="panel all-panel"><h2>全部策略评估指标</h2><div id="allMetrics" class="all-metrics"></div></section>
</div><div class="footer">仅限本机访问 · 每 750 毫秒刷新 · 页面数据不构成下单授权</div></section></main>
<script>
const $=id=>document.getElementById(id);let snapshot=null,selected=null,timer=null;
const DIRECTIONS={up:'上涨',down:'下跌'},MODES={shadow:'影子模式',live:'实盘模式',replay:'回放模式'},ASSETS={BTC:'比特币',ETH:'以太坊',SOL:'Solana',XRP:'XRP',DOGE:'狗狗币',BNB:'BNB',HYPE:'Hyperliquid'},INTERVALS={'5m':'5分钟','15m':'15分钟'};
const METRIC_LABELS={up_terminal_probability:'上涨终局概率',down_terminal_probability:'下跌终局概率',volatility_per_sqrt_second:'每平方根秒波动率',threshold:'入场阈值',quantity:'数量',up_top_ask:'上涨卖一价',up_max_price:'上涨最高价',up_fee_per_share:'上涨每份手续费',up_net_edge:'上涨净优势',up_available_depth:'上涨可用深度',up_eligible:'上涨是否可入场',down_top_ask:'下跌卖一价',down_max_price:'下跌最高价',down_fee_per_share:'下跌每份手续费',down_net_edge:'下跌净优势',down_available_depth:'下跌可用深度',down_eligible:'下跌是否可入场',selected_direction:'选中方向',signal_active:'信号是否有效',direction:'方向',top_ask:'卖一价',max_price:'最高价',fee_per_share:'每份手续费',net_edge:'净优势'};
const direction=v=>DIRECTIONS[String(v).toLowerCase()]??v,mode=v=>MODES[String(v).toLowerCase()]??String(v).toUpperCase(),asset=v=>ASSETS[v]??v,interval=v=>INTERVALS[v]??v,marketTitle=run=>{if(!String(run.market.title).includes('Up or Down'))return run.market.title;const start=new Date(run.market.window_start),end=new Date(run.market.window_end),day=new Intl.DateTimeFormat('zh-CN',{timeZone:'America/New_York',month:'long',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}),clock=new Intl.DateTimeFormat('zh-CN',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hour12:false});return asset(run.market.asset)+'涨或跌 · '+day.format(start)+'–'+clock.format(end)+'（美东时间）'};
const n=v=>v===null||v===undefined?null:Number(v),pct=v=>v===null?'--':(100*v).toFixed(2)+'%',num=(v,d=4)=>v===null?'--':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:d}),age=s=>{const x=(Date.now()-Date.parse(s))/1000;return x<2?'刚刚':x<60?Math.floor(x)+' 秒前':Math.floor(x/60)+' 分钟前'};
function metric(run,name){const id=run.chainlink_strategy_id;return run.evaluations?.[id]?.[name]??null}
function current(){return snapshot?.runs.find(x=>x.run_id===selected)||snapshot?.runs[0]||null}
function renderRuns(){const root=$('runs');root.innerHTML='';for(const run of snapshot.runs){const b=document.createElement('button'),strong=document.createElement('b');b.className='run '+(run.run_id===current()?.run_id?'active':'');strong.textContent=mode(run.mode);b.append(strong,' · '+asset(run.market.asset)+' '+interval(run.market.interval)+' · '+age(run.received_at));b.onclick=()=>{selected=run.run_id;render()};root.appendChild(b)}}
function tone(el,v){el.classList.remove('positive','negative','warn');if(v===null)return;el.classList.add(v>=0?'positive':'negative')}
function render(){renderRuns();const run=current();$('empty').hidden=!!run;$('dashboard').hidden=!run;if(!run)return;const m=name=>metric(run,name),up=n(m('up_terminal_probability')),down=n(m('down_terminal_probability'));$('marketTitle').textContent=marketTitle(run);$('marketMeta').textContent=mode(run.mode)+' · '+asset(run.market.asset)+' · '+interval(run.market.interval);$('observed').textContent='观测于 '+age(run.observed_at);$('pid').textContent='进程 '+run.process_id;$('gauge').style.setProperty('--p',(up??0)*100);$('gaugeValue').textContent=pct(up);$('upProb').textContent=pct(up);$('downProb').textContent=pct(down);
const c=run.candidate,dir=c?.direction||m('selected_direction');$('marketAsk').textContent=num(n(dir?m(dir+'_top_ask'):m('up_top_ask')));const edge=n(c?.net_edge??(dir?m(dir+'_net_edge'):null)),max=n(c?.max_price??(dir?m(dir+'_max_price'):null)),fee=n(c?.fee_per_share??(dir?m(dir+'_fee_per_share'):null));$('netEdge').textContent=pct(edge);tone($('netEdge'),edge);$('maxPrice').textContent=num(max);$('fee').textContent=num(fee,6);$('threshold').textContent=pct(n(run.threshold));
const sig=$('signal');sig.classList.toggle('none',!c);sig.querySelector('strong').textContent=c?(direction(c.direction)+'信号'):'暂无信号';sig.querySelector('small').textContent=c?('净优势 '+pct(n(c.net_edge))+' · '+c.strategy_id):'概率优势或深度门槛未满足';$('opening').textContent=num(n(run.opening),2);$('spot').textContent=num(n(run.current_spot[1]),2);const delta=n(run.current_spot[1])-n(run.opening);$('spotDelta').textContent=(delta>=0?'+':'')+num(delta,2);tone($('spotDelta'),delta);$('volatility').textContent=num(n(m('volatility_per_sqrt_second')),9);renderQuotes(run,m);renderAllMetrics(run);renderClock(run);drawChart(run)}
function renderQuotes(run,m){const q=$('quotes');q.innerHTML='';const required=n(run.quantity)||1;for(const side of ['up','down']){const ask=n(m(side+'_top_ask')),max=n(m(side+'_max_price')),fee=n(m(side+'_fee_per_share')),edge=n(m(side+'_net_edge')),eligible=m(side+'_eligible')===true;const tr=document.createElement('tr');tr.innerHTML='<td><span class="tag '+side+'">'+direction(side)+(eligible?' · 可入场':'')+'</span></td><td>'+num(ask)+'</td><td>'+num(max)+'</td><td>'+num(fee,6)+'</td><td class="'+(edge!==null&&edge>=0?'positive':'negative')+'">'+pct(edge)+'</td>';q.appendChild(tr);const depth=n(m(side+'_available_depth'))||0;$(side+'DepthText').textContent=num(depth,2)+' / '+num(required,2);$(side+'Depth').style.width=Math.min(100,depth/required*100)+'%'}}
function renderAllMetrics(run){const root=$('allMetrics');root.innerHTML='';for(const [strategy,metrics] of Object.entries(run.evaluations||{})){const block=document.createElement('section'),title=document.createElement('h3');block.className='strategy-block';title.textContent=strategy;block.appendChild(title);for(const [name,value] of Object.entries(metrics)){const row=document.createElement('div'),label=document.createElement('span'),shown=document.createElement('b');row.className='raw-metric';label.textContent=METRIC_LABELS[name]??name.replaceAll('_',' ');shown.textContent=value===null?'--':value===true?'是':value===false?'否':DIRECTIONS[String(value).toLowerCase()]??String(value);row.append(label,shown);block.appendChild(row)}root.appendChild(block)}}
function renderClock(run){const start=Date.parse(run.market.window_start),end=Date.parse(run.market.window_end),left=Math.max(0,(end-Date.now())/1000),total=Math.max(1,end-start),minutes=Math.floor(left/60),seconds=Math.floor(left%60);$('countdown').textContent=String(minutes).padStart(2,'0')+':'+String(seconds).padStart(2,'0');$('timeProgress').style.width=Math.max(0,Math.min(100,(Date.now()-start)/total*100))+'%'}
function drawChart(run){const canvas=$('priceChart'),rect=canvas.getBoundingClientRect(),dpr=devicePixelRatio||1,w=Math.max(1,rect.width),h=Math.max(1,rect.height);canvas.width=w*dpr;canvas.height=h*dpr;const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);const points=run.prices.map(x=>[Date.parse(x[0]),Number(x[1])]).filter(x=>Number.isFinite(x[1]));if(!points.length)return;const opening=Number(run.opening),values=points.map(x=>x[1]).concat(opening),lo=Math.min(...values),hi=Math.max(...values),pad=Math.max((hi-lo)*.15,opening*.00003),min=lo-pad,max=hi+pad,x=i=>20+i/Math.max(1,points.length-1)*(w-38),y=v=>10+(max-v)/(max-min)*(h-30);ctx.strokeStyle='#1c2a3d';ctx.lineWidth=1;for(let i=0;i<4;i++){const yy=10+i*(h-30)/3;ctx.beginPath();ctx.moveTo(20,yy);ctx.lineTo(w-18,yy);ctx.stroke()}ctx.setLineDash([5,5]);ctx.strokeStyle='#ffbd5c';ctx.beginPath();ctx.moveTo(20,y(opening));ctx.lineTo(w-18,y(opening));ctx.stroke();ctx.setLineDash([]);ctx.strokeStyle='#35d7ff';ctx.lineWidth=2;ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(x(i),y(p[1])):ctx.moveTo(x(i),y(p[1])));ctx.stroke();ctx.fillStyle='#35d7ff';const last=points.at(-1);ctx.beginPath();ctx.arc(x(points.length-1),y(last[1]),3.5,0,Math.PI*2);ctx.fill();ctx.fillStyle='#8190a5';ctx.font='10px ui-monospace';ctx.fillText(max.toFixed(2),24,11);ctx.fillText(min.toFixed(2),24,h-8)}
async function update(){try{const r=await fetch('/api/snapshot',{cache:'no-store'});if(!r.ok)throw Error();snapshot=await r.json();$('connection').textContent='已连接 · '+snapshot.runs.length+' 个机器人';$('connection').previousElementSibling.style.background='#31e6a1';render()}catch(e){$('connection').textContent='连接已断开';$('connection').previousElementSibling.style.background='#ff5f6d'}}
window.addEventListener('resize',()=>current()&&drawChart(current()));update();timer=setInterval(update,750);
</script></body></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
