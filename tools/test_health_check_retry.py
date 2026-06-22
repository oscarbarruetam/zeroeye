#!/usr/bin/env python3
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import health_check


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _handler(status: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            return

    return Handler


class HealthCheckRetryTests(unittest.TestCase):
    def _start_server(self, status: int = 200):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(status))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def test_http_retries_transient_refusal_then_success(self):
        port = _unused_port()
        server_holder = {}

        def delayed_start():
            time.sleep(0.08)
            server = ThreadingHTTPServer(("127.0.0.1", port), _handler(200))
            server_holder["server"] = server
            server.serve_forever()

        thread = threading.Thread(target=delayed_start, daemon=True)
        thread.start()
        try:
            status, detail, code, metadata = health_check.check_http_service(
                "127.0.0.1",
                port,
                "/health",
                timeout=1,
                retry_count=5,
                retry_backoff=0.05,
            )

            self.assertEqual(status, "OK")
            self.assertEqual(detail, "HTTP 200")
            self.assertEqual(code, 200)
            self.assertGreater(metadata["attempts"], 1)
            self.assertGreater(metadata["retry_attempts"], 0)
            self.assertIn("final_latency_ms", metadata)
        finally:
            server = server_holder.get("server")
            if server:
                server.shutdown()
                server.server_close()

    def test_tcp_exhausted_retries_reports_attempt_count(self):
        status, detail, latency, metadata = health_check.check_tcp_port(
            "127.0.0.1",
            _unused_port(),
            timeout=1,
            retry_count=2,
            retry_backoff=0.01,
        )

        self.assertEqual(status, "CRITICAL")
        self.assertEqual(detail, "Connection refused")
        self.assertEqual(latency, 0)
        self.assertEqual(metadata["attempts"], 3)
        self.assertEqual(metadata["retry_attempts"], 2)
        self.assertIn("final_latency_ms", metadata)

    def test_http_server_error_is_not_retried(self):
        server = self._start_server(status=500)
        try:
            status, detail, code, metadata = health_check.check_http_service(
                "127.0.0.1",
                server.server_address[1],
                "/health",
                timeout=1,
                retry_count=3,
                retry_backoff=0.01,
            )

            self.assertEqual(status, "CRITICAL")
            self.assertEqual(code, 500)
            self.assertIn("HTTP 500", detail)
            self.assertEqual(metadata["attempts"], 1)
            self.assertEqual(metadata["retry_attempts"], 0)
        finally:
            server.shutdown()
            server.server_close()

    def test_json_results_include_retry_metadata(self):
        server = self._start_server(status=200)
        original_services = health_check.SERVICES
        original_infrastructure = health_check.INFRASTRUCTURE
        try:
            health_check.SERVICES = {
                "unit": {
                    "host": "127.0.0.1",
                    "port": server.server_address[1],
                    "path": "/health",
                    "timeout": 1,
                }
            }
            health_check.INFRASTRUCTURE = {}

            results = health_check.run_health_checks(
                service="unit",
                json_output=True,
                retry_count=1,
                retry_backoff=0.01,
            )

            service_result = results["services"]["unit"]
            self.assertEqual(service_result["status"], "OK")
            self.assertEqual(service_result["attempts"], 1)
            self.assertEqual(service_result["retry_attempts"], 0)
            self.assertIn("final_latency_ms", service_result)
        finally:
            health_check.SERVICES = original_services
            health_check.INFRASTRUCTURE = original_infrastructure
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
