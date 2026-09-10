from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>足球数据状态</title><style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:#f4f6f8;color:#18202a}
main{max-width:760px;margin:auto;padding:18px}.card{background:white;border-radius:14px;padding:16px;margin:12px 0;box-shadow:0 2px 10px #0000000d}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.metric{background:#f7f9fb;border-radius:10px;padding:12px}.value{font-size:25px;font-weight:700}
.ok{color:#16853f}.warning{color:#b56a00}.down{color:#c62828}.unknown{color:#68717d}small{color:#68717d}.match{padding:12px 0;border-top:1px solid #edf0f2}
h1{font-size:24px}h2{font-size:18px;margin:18px 0 10px}button{border:0;border-radius:9px;padding:9px 13px;background:#1769e0;color:white}
</style></head><body><main><h1>足球数据仓库</h1><div id="app" class="card">正在读取状态……</div></main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){try{const r=await fetch('/api/v1/snapshot',{cache:'no-store'});const d=await r.json();
let h=`<div><button onclick="load()">刷新</button> <small>更新：${esc(d.generated_at)}</small></div><h2>仓库</h2><div class="grid">`;
const labels={matches:'比赛',odds_records:'赔率记录',results:'已有赛果',objective_features:'客观字段',latest_collection:'最近采集'};
for(const [k,v] of Object.entries(d.warehouse))h+=`<div class="metric"><small>${esc(labels[k]||k)}</small><div class="value">${esc(v)}</div></div>`;h+='</div><h2>数据源</h2>';
const states={ok:'正常',warning:'可访问/暂无比赛',down:'连续失败'};
for(const s of d.sources)h+=`<div class="card"><b>${esc(s.name||s.source_id)}</b> <span class="${esc(s.status)}">${esc(states[s.status]||s.status)}</span><br><small>HTTP ${esc(s.http_status)} · 可解析比赛 ${esc(s.records_seen)} · 连续失败 ${esc(s.consecutive_failures)}</small></div>`;
h+='<h2>近期比赛概率</h2>';for(const m of d.matches){const p=m.model_probabilities||m.market_probabilities;h+=`<div class="match"><b>${esc(m.home_team)} vs ${esc(m.away_team)}</b><br><small>${esc(m.competition)} · ${esc(m.kickoff_time)} · ${esc(m.source_count)}源</small><br>主 ${Math.round((p.H||0)*100)}%　平 ${Math.round((p.D||0)*100)}%　客 ${Math.round((p.A||0)*100)}%</div>`}document.getElementById('app').innerHTML=h;
}catch(e){document.getElementById('app').innerHTML='<span class="down">状态文件尚未生成，请稍后刷新。</span>'}}load();setInterval(load,60000);
</script></body></html>"""


class StatusHandler(BaseHTTPRequestHandler):
    snapshot_path = Path("/var/lib/football-data/public/snapshot.json")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", PAGE.encode())
            return
        if path in {"/api/v1/snapshot", "/api/v1/status", "/api/v1/matches"}:
            try:
                snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
                if path == "/api/v1/status":
                    snapshot = {key: snapshot[key] for key in (
                        "generated_at", "read_only", "warehouse", "sources", "backfill", "model"
                    )}
                elif path == "/api/v1/matches":
                    snapshot = {"generated_at": snapshot["generated_at"], "matches": snapshot["matches"]}
                body = json.dumps(snapshot, ensure_ascii=False).encode()
                self._send(HTTPStatus.OK, "application/json; charset=utf-8", body)
            except (OSError, ValueError, KeyError) as exc:
                body = json.dumps({"status": "unavailable", "error": str(exc)}, ensure_ascii=False).encode()
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, "application/json; charset=utf-8", body)
            return
        self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", "Not found".encode())

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(host: str = "0.0.0.0", port: int = 8787, data_root: str | Path = "/var/lib/football-data") -> None:
    StatusHandler.snapshot_path = Path(data_root) / "public" / "snapshot.json"
    server = ThreadingHTTPServer((host, int(port)), StatusHandler)
    print(f"只读状态页已启动：http://{host}:{port}", flush=True)
    server.serve_forever()
