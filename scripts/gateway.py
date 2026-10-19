#!/usr/bin/env python3
"""Local demo endpoint; closes routing while a handover is in progress."""

import http.client, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = Path(__file__).resolve().parents[1] / ".local/state.json"


class Gateway(BaseHTTPRequestHandler):
    def handle_request(self):
        try:
            s = json.loads(STATE.read_text())
            route = s.get("route")
            if s["phase"] != "ready" or not route:
                self.send_error(503, "Handover in progress")
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            upstream = http.client.HTTPConnection(
                route["host"], route["port"], timeout=10
            )
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in {"host", "connection", "transfer-encoding"}
            }
            upstream.request(self.command, self.path, body=body, headers=headers)
            response = upstream.getresponse()
            payload = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in {
                    "connection",
                    "transfer-encoding",
                    "content-length",
                    "server",
                    "date",
                }:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Lab-Cluster", s["active"])
            self.end_headers()
            self.wfile.write(payload)
            upstream.close()
        except Exception:
            # A failed POST can have an unknown commit outcome. Never retry it here.
            self.send_error(
                502, "Upstream unavailable; reconcile writes before retrying"
            )

    do_GET = do_POST = do_DELETE = handle_request


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8023), Gateway).serve_forever()
