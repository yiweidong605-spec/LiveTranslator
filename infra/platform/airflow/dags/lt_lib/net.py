"""Internet probes for the platform healthcheck (pure Python, no Airflow: unit-tested in CI).

Plain-HTTP probes reveal a captive portal (it answers instead of the real site); the HTTPS probe
cannot be faked by a portal (it would fail with a TLS error instead). The internet counts as up if
ANY probe succeeds: a single probe target is a single point of failure (see the 2026-11-10 neverssl
incident in the project notes).
"""
from __future__ import annotations

import socket
import statistics
import time

import requests

# (name, url, check(response) -> bool)
PROBES = [
    ("google", "http://connectivitycheck.gstatic.com/generate_204", lambda r: r.status_code == 204),
    ("microsoft", "http://www.msftconnecttest.com/connecttest.txt",
     lambda r: r.status_code == 200 and r.text.strip() == "Microsoft Connect Test"),
    ("apple", "http://captive.apple.com/hotspot-detect.html",
     lambda r: r.status_code == 200 and "Success" in r.text),
    ("google_https", "https://www.google.com/generate_204", lambda r: r.status_code == 204),
]

FIX = {
    "portal": ("The apartment captive portal is intercepting traffic (session expired). Log in again from the "
               "desktop: `ssh -D 1080 swt17`, start Edge with --proxy-server=socks5://127.0.0.1:1080, open "
               "http://captive.apple.com, press Ctrl+F5 and log in (it shows 'Success' once you are online)."),
    "dns": ("DNS does not resolve. On the mini PC run `resolvectl status` and `resolvectl query google.com`; "
            "`sudo resolvectl flush-caches` if answers look wrong."),
    "timeout": ("Connections are silently dropped. Usually the portal expired without redirecting, or the "
                "apartment network is down. Check `ping -c 3 1.1.1.1` on the mini PC, then try the portal login."),
    "unreachable": ("No route to the internet. Check the cable and that `ip route` shows a default route "
                    "(`sudo netplan apply` re-runs DHCP)."),
    "error": "Unexpected error, see the per-probe details above.",
}


def ipv4_only():
    """The apartment has no IPv6. Without this, a failed IPv4 attempt is reported as the (misleading)
    IPv6 'Network is unreachable' error, which is exactly what confused the first diagnosis."""
    import urllib3.util.connection as uc

    uc.allowed_gai_family = lambda: socket.AF_INET


def probe(session, name: str, url: str, check, timeout: float = 5.0) -> dict:
    """Return {name, outcome, ms, detail}; outcome is ok / portal / dns / timeout / unreachable / error."""
    t0 = time.perf_counter()
    try:
        r = session.get(url, timeout=timeout, allow_redirects=False)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        if check(r):
            return {"name": name, "outcome": "ok", "ms": ms, "detail": f"HTTP {r.status_code}"}
        where = r.headers.get("location", "")
        return {"name": name, "outcome": "portal", "ms": ms,
                "detail": f"unexpected HTTP {r.status_code} {where}".strip()}
    except requests.exceptions.SSLError as e:  # someone answered HTTPS with the wrong certificate
        outcome, err = "portal", e
    except requests.exceptions.Timeout as e:
        outcome, err = "timeout", e
    except requests.exceptions.ConnectionError as e:
        msg, err = str(e), e
        if any(k in msg for k in ("NameResolution", "Failed to resolve", "Name or service not known",
                                  "Temporary failure in name resolution")):
            outcome = "dns"
        elif any(k in msg for k in ("Network is unreachable", "No route to host", "Connection refused")):
            outcome = "unreachable"
        else:
            outcome = "error"
    except Exception as e:  # noqa: BLE001
        outcome, err = "error", e
    ms = round((time.perf_counter() - t0) * 1000, 1)
    return {"name": name, "outcome": outcome, "ms": ms, "detail": f"{type(err).__name__}: {str(err)[:160]}"}


def run_probes(probes=PROBES, timeout: float = 5.0, retries: int = 1) -> list[dict]:
    session = requests.Session()
    session.trust_env = False  # measure the direct connection, never an HTTP proxy from the environment
    results = []
    for name, url, check in probes:
        res = probe(session, name, url, check, timeout)
        for _ in range(retries):  # retry transient failures, but a portal answer is a real answer
            if res["outcome"] in ("ok", "portal"):
                break
            res = probe(session, name, url, check, timeout)
        results.append(res)
    return results


def summarize(results: list[dict]) -> tuple[dict, str | None]:
    """Metrics for MLflow, plus an error message if the internet is down (None when healthy)."""
    ok = [r for r in results if r["outcome"] == "ok"]
    metrics = {
        "internet_ok": int(bool(ok)),
        "internet_probes_ok": len(ok),
        "internet_probes_total": len(results),
        "portal_detected": int(any(r["outcome"] == "portal" for r in results)),
    }
    for r in results:
        metrics[f"probe_{r['name']}_ok"] = int(r["outcome"] == "ok")
    if ok:
        metrics["internet_latency_ms"] = round(statistics.median(r["ms"] for r in ok), 1)
        return metrics, None
    # all failed: name the most telling cause (a portal answer beats a timeout, etc.)
    outcomes = [r["outcome"] for r in results]
    cause = next(c for c in ("portal", "dns", "unreachable", "timeout", "error") if c in outcomes)
    return metrics, f"No internet: all {len(results)} probes failed, cause = {cause}. {FIX[cause]}"
