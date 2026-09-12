"""猫娘插件通用面板服务模板（本地 127.0.0.1，零第三方依赖）

每个插件复制本文件并注入：
- html_provider() -> str            面板页 HTML
- endpoints: {("GET"|"POST", 路由): 处理函数(json_body)->dict}
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional


class PanelServer:
    def __init__(
        self,
        port: int,
        html_provider: Callable[[], str],
        endpoints: dict[tuple[str, str], Callable[[dict[str, Any]], dict[str, Any]]],
    ):
        self.port = int(port)
        self._html_provider = html_provider
        self._endpoints = endpoints
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _reply(self, body: bytes, ctype: str):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self._reply(b"{}", "application/json")

            def do_GET(self):
                route = self.path.split("?", 1)[0]
                if route in ("/", "/index.html"):
                    self._reply(outer._html_provider().encode("utf-8"), "text/html; charset=utf-8")
                    return
                fn = outer._endpoints.get(("GET", route))
                if fn is None:
                    self.send_error(404)
                    return
                try:
                    payload = fn({})
                    self._reply(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                except Exception as exc:
                    self._reply(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

            def do_POST(self):
                route = self.path.split("?", 1)[0]
                fn = outer._endpoints.get(("POST", route))
                if fn is None:
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    body = json.loads(raw.decode("utf-8", "replace")) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                try:
                    payload = fn(body if isinstance(body, dict) else {})
                    self._reply(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                except Exception as exc:
                    self._reply(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError:
            return False
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name=f"neko-panel-{self.port}"
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None


def find_open_port(preferred: int, attempts: int = 5) -> int:
    """从 preferred 开始找一个可绑定端口（+1 递增）。"""
    import socket

    for offset in range(attempts):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    return preferred


_PAGE_CSS = """
body{margin:0;font-family:'Microsoft YaHei',sans-serif;background:#1b1826;color:#e8e4f2;display:flex;justify-content:center;padding:24px}
.wrap{max-width:860px;width:100%}
h1{font-size:24px;margin:8px 0 4px}
.sub{color:#9a93b5;font-size:14px;margin-bottom:16px}
.card{background:#241f33;border:1px solid #3a3352;border-radius:16px;padding:16px;margin-bottom:16px}
.card h2{font-size:17px;margin:2px 0 12px;color:#c9b9ff}
table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:6px;border-bottom:1px solid #322b4a}
button{background:#6c5ce7;color:#fff;border:0;border-radius:10px;padding:7px 16px;font-size:13px;cursor:pointer;margin-right:8px}
button:hover{background:#7d6ef0}
input[type=text],input[type=number]{background:#2a2440;color:#e8e4f2;border:1px solid #3a3352;border-radius:8px;padding:6px 10px;font-size:14px;width:110px}
.toggle{display:inline-block;padding:4px 12px;border-radius:20px;font-size:13px}
.on{background:#1f4a2e;color:#7be3a2}
.off{background:#4a1f2a;color:#e37b8f}
#status{color:#9a93b5;font-size:13px;margin-left:8px}
.kv td:first-child{color:#9a93b5;width:180px}
"""
