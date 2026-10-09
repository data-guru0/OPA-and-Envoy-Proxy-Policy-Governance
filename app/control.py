"""NexaBank policy control plane (stdlib only). Everything the UI's governance tabs need, without a terminal.

1. Policy distribution: every 2 s, push changed files in ./policies into OPA (REST Policy/Data API) and remember
   per-file status (loaded / REJECTED + OPA's error). In production: signed OPA bundles, or OPAL.
2. PEP configuration: render envoy/listeners.yaml into the volume Envoy watches. The UI can switch
   failure_mode_allow (fail-open / fail-closed) and cut Envoy's link to OPA, live. In production: Istio / xDS.
3. Audit: OPA ships its decision logs here (POST /logs); Envoy's ext_authz counters come from its admin API.

Why not `opa run --watch`? File-change events don't cross a Windows -> Docker bind mount, so we poll.
"""
import collections
import gzip
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OPA_URL = os.environ.get("OPA_URL", "http://opa:8181")
ENVOY_ADMIN = os.environ.get("ENVOY_ADMIN", "http://envoy:9901")
POLICIES = os.environ.get("POLICY_DIR", "/policies")
TEMPLATE = os.environ.get("ENVOY_TEMPLATE", "/envoy-src/listeners.yaml")
LDS = os.environ.get("ENVOY_LDS", "/envoy-dynamic/lds.yaml")

STATUS = {}                                  # file -> {"state": "loaded"|"rejected", "error": str, "time": str}
ENVOY = {"fail_mode": "closed", "opa_link": "up", "rendered": "", "error": None}
DECISIONS = collections.deque(maxlen=100)    # newest first
lock = threading.Lock()


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def http(method, url, body=None, raw=False, timeout=5):
    """JSON (or raw text, if raw=True) HTTP call. Returns (status, parsed body)."""
    data = body.encode() if isinstance(body, str) else json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "text/plain" if isinstance(body, str) else "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        r = e
    except (urllib.error.URLError, OSError) as e:
        return 503, {"error": str(e)}
    with r:
        text = r.read().decode(errors="replace")
    if raw:
        return r.status, text
    try:
        return r.status, json.loads(text) if text else {}
    except ValueError:
        return r.status, {"error": text[:300]}


# ---------------------------------------------------------------- 1. policy distribution

def opa_errors(res):
    errs = res.get("errors") or []
    lines = [f"{e.get('location', {}).get('row', '?')}:{e.get('location', {}).get('col', '?')} {e.get('code')}: {e.get('message')}"
             for e in errs]
    return "\n".join(lines) or str(res.get("message", res))


def policy_files():
    out = {}
    for d, _, names in os.walk(POLICIES):
        for n in names:
            if n.endswith((".rego", ".json")):
                p = os.path.join(d, n)
                with open(p, encoding="utf-8") as f:
                    out[os.path.relpath(p, POLICIES).replace(os.sep, "/")] = f.read()
    return out


def sync_loop():
    synced, instance = {}, ""  # file -> sha256 OPA has (suffix "!" = rejected version); OPA's per-process id
    template_hash = None
    while True:
        try:
            code, res = http("GET", f"{OPA_URL}/v1/config", timeout=3)
            now = res.get("result", {}).get("labels", {}).get("id") if code == 200 else None
            if now != instance:  # OPA started, restarted (empty again) or went away: push everything again
                instance, synced = now, {}
                log(f"OPA is up (instance {now[:8]}): syncing ./policies" if now else "OPA unreachable: waiting")
            if now:
                code, res = http("GET", f"{OPA_URL}/v1/policies", timeout=3)
                loaded = {p["id"] for p in res.get("result", [])} if code == 200 else set()
                current = policy_files()
                for name, text in current.items():
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    if synced.get(name) == digest and (name.endswith(".json") or name in loaded):
                        continue
                    if synced.get(name) == digest + "!":  # this broken version was already reported: wait for an edit
                        continue
                    if name.endswith(".rego"):
                        c, r = http("PUT", f"{OPA_URL}/v1/policies/{name}", text)
                    else:
                        try:
                            c, r = http("PUT", f"{OPA_URL}/v1/data/{os.path.dirname(name)}", json.loads(text or "{}"))
                        except ValueError as e:
                            c, r = 400, {"message": f"invalid JSON: {e}"}
                    ok = c in (200, 204)
                    with lock:
                        STATUS[name] = {"state": "loaded" if ok else "rejected", "error": None if ok else opa_errors(r),
                                        "time": time.strftime("%H:%M:%S")}
                    log(f"loaded   {name}" if ok else f"REJECTED {name} (OPA keeps the last good version)\n  {opa_errors(r)}")
                    synced[name] = digest if ok else digest + "!"
                for name in [n for n in loaded if n not in current and n.endswith(".rego")]:
                    http("DELETE", f"{OPA_URL}/v1/policies/{name}")
                    synced.pop(name, None)
                    log(f"removed  {name}")
                with lock:
                    for name in [n for n in STATUS if n not in current]:
                        del STATUS[name]
            # the Envoy listener template may also be edited by hand
            with open(TEMPLATE, encoding="utf-8") as f:
                h = hashlib.sha256(f.read().encode()).hexdigest()
            if h != template_hash:
                template_hash = h
                render_envoy()
        except Exception as e:  # keep the control plane alive whatever happens
            log("sync error:", e)
        time.sleep(2)


# ---------------------------------------------------------------- 2. Envoy (PEP) configuration

def render_envoy():
    with open(TEMPLATE, encoding="utf-8") as f:
        text = f.read()
    if ENVOY["fail_mode"] == "open":
        text = text.replace("failure_mode_allow: false", "failure_mode_allow: true")
    if ENVOY["opa_link"] == "down":
        text = re.sub(r"cluster_name: opa\b", "cluster_name: opa_unreachable", text)
    tmp = os.path.join(os.path.dirname(LDS), ".lds.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, LDS)  # atomic move: Envoy's watched_directory picks it up
    ENVOY["rendered"] = text
    log(f"Envoy listeners rendered: fail_mode={ENVOY['fail_mode']} opa_link={ENVOY['opa_link']}")


def envoy_stats():
    code, res = http("GET", f"{ENVOY_ADMIN}/stats?format=json&filter=(ext_authz\\.(ok|denied|error)|lds\\.update)", timeout=3)
    if code != 200:
        return None
    return {s["name"]: s.get("value", 0) for s in res.get("stats", []) if "name" in s
            and (s["name"].startswith("http.") or s["name"].startswith("listener_manager.lds"))}


# ---------------------------------------------------------------- 3. audit: decision logs from OPA

def summarize(ev):
    inp, res = ev.get("input") or {}, ev.get("result")
    http_req = (inp.get("attributes") or {}).get("request", {}).get("http", {})
    allowed = res.get("allowed", res.get("allow")) if isinstance(res, dict) else res
    err = ev.get("error")
    if isinstance(err, dict):  # OPA logs errors as {"code": ..., "message": ...}
        err = err.get("message") or err.get("code") or json.dumps(err)
    return {"time": ev.get("timestamp", "")[11:19], "decision_id": ev.get("decision_id"), "path": ev.get("path"),
            "request": f"{http_req.get('method', '')} {http_req.get('path', '')}".strip() or "(direct query)",
            "allowed": allowed, "error": err, "input": inp, "result": res}


# ---------------------------------------------------------------- HTTP API (reached by the UI through Envoy /ops/*)

class Handler(BaseHTTPRequestHandler):
    def send(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw or b"{}")

    def do_GET(self):
        if self.path == "/ops/status":
            with lock:
                files = [{"file": n, **s} for n, s in sorted(STATUS.items())]
            return self.send(200, {"files": files, "envoy": {k: ENVOY[k] for k in ("fail_mode", "opa_link")},
                                   "stats": envoy_stats()})
        if self.path == "/ops/envoy-config":
            return self.send(200, {"listeners_yaml": ENVOY["rendered"]})
        if self.path == "/ops/decisions":
            return self.send(200, {"decisions": list(DECISIONS)})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        try:
            req = self.body()
        except ValueError:
            return self.send(400, {"error": "invalid JSON"})
        if self.path.startswith("/logs"):  # OPA decision-log upload (a gzip'd JSON array)
            for ev in req if isinstance(req, list) else []:
                DECISIONS.appendleft(summarize(ev))
            return self.send(200, {})
        if self.path == "/ops/query":  # "Ask OPA": query any rule with any input, exactly like a PEP would
            pkg = str(req.get("path", "")).strip().strip("/").replace(".", "/").removeprefix("data/")
            code, res = http("POST", f"{OPA_URL}/v1/data/{pkg}", {"input": req.get("input")}, timeout=5)
            return self.send(200, {"status": code, "response": res})
        if self.path == "/ops/envoy":
            if req.get("fail_mode") in ("open", "closed"):
                ENVOY["fail_mode"] = req["fail_mode"]
            if req.get("opa_link") in ("up", "down"):
                ENVOY["opa_link"] = req["opa_link"]
            render_envoy()
            return self.send(200, {"envoy": {k: ENVOY[k] for k in ("fail_mode", "opa_link")}})
        self.send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    render_envoy()  # Envoy waits for this file (healthcheck)
    threading.Thread(target=sync_loop, daemon=True).start()
    log("NexaBank control plane on :8300 (policy sync, Envoy config, decision logs)")
    ThreadingHTTPServer(("0.0.0.0", 8300), Handler).serve_forever()
