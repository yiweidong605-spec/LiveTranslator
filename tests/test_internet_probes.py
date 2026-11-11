"""check_internet's probe classification (lt_lib.net), against a local HTTP server."""
import http.server
import threading
import time

import pytest

from lt_lib import net


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/204":
            self.send_response(204)
            self.end_headers()
        elif self.path == "/redirect":  # what a captive portal does
            self.send_response(302)
            self.send_header("Location", "http://portal.example/login")
            self.end_headers()
        elif self.path == "/loginpage":  # a portal that answers 200 with its own page
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html>please log in</html>")
        elif self.path == "/hang":
            time.sleep(2)


@pytest.fixture(scope="module")
def base():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def is204(r):
    return r.status_code == 204


@pytest.mark.parametrize("path,outcome", [("/204", "ok"), ("/redirect", "portal"), ("/loginpage", "portal"),
                                          ("/hang", "timeout")])
def test_http_outcomes(base, path, outcome):
    r = net.run_probes([("t", base + path, is204)], timeout=0.5, retries=0)[0]
    assert r["outcome"] == outcome


def test_dns_and_unreachable():
    res = net.run_probes([("dns", "http://no-such-host.invalid/", is204),
                          ("refused", "http://127.0.0.1:1/", is204)], timeout=1, retries=0)
    assert [r["outcome"] for r in res] == ["dns", "unreachable"]


def test_healthy_if_any_probe_succeeds(base):
    res = net.run_probes([("a", base + "/hang", is204), ("b", base + "/204", is204)], timeout=0.5, retries=0)
    metrics, error = net.summarize(res)
    assert error is None and metrics["internet_ok"] == 1 and metrics["internet_probes_ok"] == 1
    assert metrics["probe_a_ok"] == 0 and metrics["probe_b_ok"] == 1


def test_portal_is_reported_before_timeouts(base):
    res = net.run_probes([("a", base + "/hang", is204), ("b", base + "/redirect", is204)], timeout=0.5, retries=0)
    metrics, error = net.summarize(res)
    assert metrics["internet_ok"] == 0 and metrics["portal_detected"] == 1
    assert "cause = portal" in error and "ssh -D 1080" in error  # tells you what to do
