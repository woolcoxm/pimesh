#!/usr/bin/env python3
"""PiMesh gateway v2 — OpenAI-compatible endpoint + control plane + web UI.

 /v1/*            OpenAI-compatible proxy (chat/completions, completions, models)
 /api/status      live cluster status for the dashboard
 /api/models/disk gguf files available on henry
 /api/desired     GET/POST desired deep-lane state (model, ctx) — watchdog applies
 /api/config      GET/POST UI settings (routing hints, thinking mode, ...)
 /api/actions/*   restart worker rpc / restart coordinator / refresh
 /                the single-page app (pimesh/www/)
Stdlib only; runs on the Pi.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000
FAST = "http://127.0.0.1:8082"
DEEP = "http://127.0.0.1:8081"
BASE = "/home/kram/pimesh"
WWW = BASE + "/www"
DESIRED = BASE + "/desired.json"
CONFIG = BASE + "/ui_config.json"
STATE_FILE = BASE + "/gateway_state.json"
WORKERS = BASE + "/workers.json"
DEFAULT_MODEL = "/home/kram/models/30b/Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf"

WORKER_DEFS = [
    {"ip": "10.0.0.176", "name": "pi2"},
    {"ip": "10.0.0.63", "name": "pi8gb"},
]

HISTORY = []
HIST_LOCK = threading.Lock()
START = time.time()

DEEP_HINTS = ("code", "explain", "analyz", "write", "essay", "refactor",
              "debug", "step", "compare", "summari", "translate", "plan ")

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
        ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".json": "application/json"}


def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def config():
    return load_json(CONFIG, {"route_hints": True, "fast_thinking": False,
                              "refresh_s": 5})

# request history survives gateway restarts
HISTORY.extend(load_json(STATE_FILE, {}).get("history", []))


def probe(base, path="/health", timeout=4.0):
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def backend_up(base):
    # /health returns 503 when all slots are busy — /v1/models is the true
    # liveness signal (200 whenever the server is up, 503 only while loading)
    return probe(base, "/v1/models")


def port_open(ip, port, timeout=2.0):
    import socket
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def pick_model(model_name, messages):
    last = ""
    if messages:
        for m in reversed(messages):
            if m.get("role") == "user":
                last = m.get("content") or ""
                break
    text = last if isinstance(last, str) else json.dumps(last)
    cfg = config()
    deep_ok = backend_up(DEEP)
    fast_ok = backend_up(FAST)
    wants_deep = cfg.get("route_hints", True) and (
        len(text) > 1200 or any(h in text.lower() for h in DEEP_HINTS))
    if model_name == "mesh-30b" or (model_name in (None, "auto", "default", "gpt-3.5-turbo") and wants_deep):
        return (DEEP, "mesh-30b") if deep_ok else ((FAST, "axera-fast") if fast_ok else (DEEP, "mesh-30b"))
    return (FAST, "axera-fast") if fast_ok else ((DEEP, "mesh-30b") if deep_ok else (FAST, "axera-fast"))


def record(route_model, backend, status, note=""):
    with HIST_LOCK:
        HISTORY.append({"t": time.strftime("%H:%M:%S"), "model": route_model,
                        "backend": backend, "status": status, "note": note})
        del HISTORY[:-40]
    save_json(STATE_FILE, {"history": HISTORY[-40:]})


def sys_info():
    info = {}
    try:
        mem = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            mem[k] = int(v.strip().split()[0])
        info["ram_total_gb"] = round(mem["MemTotal"] / 1048576, 1)
        info["ram_avail_gb"] = round(mem["MemAvailable"] / 1048576, 1)
    except Exception:
        pass
    try:
        info["load"] = open("/proc/loadavg").read().split()[0:3]
    except Exception:
        pass
    try:
        out = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True,
                             text=True, timeout=3).stdout
        info["temp"] = re.search(r"([\d.]+)", out).group(1)
    except Exception:
        pass
    return info


def npu_info():
    try:
        out = subprocess.run(["timeout", "8", "/usr/bin/axcl/axcl-smi"],
                             capture_output=True, text=True, timeout=12).stdout
        # axcl-smi prints two "X MiB / Y MiB" pairs: system memory and CMM.
        # CMM is the one with the big total (~7 GB on the 8850).
        pairs = re.findall(r"(\d+)\s*MiB\s*/\s*(\d+)\s*MiB", out)
        cmm = max(pairs, key=lambda m: int(m[1])) if pairs else None
        temp = re.search(r"\|\s*--\s+(\d+)C", out) or re.search(r"(\d+)C", out)
        return {"present": True,
                "cmm_used": cmm[0] + " MiB" if cmm else "?",
                "cmm_total": cmm[1] + " MiB" if cmm else "?",
                "temp": temp.group(1) if temp else "?"}
    except Exception:
        return {"present": False}


def desired():
    d = load_json(DESIRED, {})
    if not d.get("model"):
        d["model"] = DEFAULT_MODEL
    d.setdefault("ctx", 32768)
    return d


def coordinator_state():
    up = backend_up(DEEP)
    st = {"running": up, "model": desired()["model"], "ctx": desired()["ctx"]}
    try:
        log = open(BASE + "/deep.log", errors="replace").read()[-4000:]
        m = re.findall(r"tg = +([\d.]+) t/s", log)
        if m:
            st["last_tps"] = float(m[-1])
        st["loading"] = ("listening" not in log) if up else False
    except Exception:
        pass
    return st


def api_status():
    w = load_json(WORKERS, {})
    for d in WORKER_DEFS:
        for wr in w.get("workers", []):
            if wr.get("ip") == d["ip"]:
                wr.setdefault("name", d["name"])
    return {
        "uptime_s": int(time.time() - START),
        "fast": {"up": backend_up(FAST), "url": FAST},
        "deep": coordinator_state(),
        "pool": w.get("rpclist", ""),
        "workers_file": w,
        "workers": WORKER_DEFS,
        "npu": npu_info(),
        "sys": sys_info(),
        "desired": desired(),
        "config": config(),
        "history": HISTORY[-15:],
    }


def api_disk_models():
    out = []
    for root, _, files in os.walk("/home/kram/models"):
        for f in files:
            if f.lower().endswith(".gguf"):
                p = os.path.join(root, f)
                try:
                    stt = os.stat(p)
                    out.append({"path": p, "name": f,
                                "gb": round(stt.st_size / 1e9, 1),
                                "mtime": int(stt.st_mtime)})
                except Exception:
                    pass
    out.sort(key=lambda x: -x["mtime"])
    return out


PAGE404 = b'{"error":"not found"}'


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        fp = os.path.normpath(WWW + path)
        if not fp.startswith(WWW) or not os.path.isfile(fp):
            self._json(404, {"error": "not found"})
            return
        ext = os.path.splitext(fp)[1]
        body = open(fp, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p.startswith(("/v1/",)):
            self._proxy_get()
        elif p in ("/", "/index.html", "/app.js", "/style.css"):
            self._static(p)
        elif p == "/health":
            self._json(200, {"ok": True, "fast": backend_up(FAST),
                             "deep": backend_up(DEEP)})
        elif p == "/api/status":
            self._json(200, api_status())
        elif p == "/api/models/disk":
            self._json(200, {"models": api_disk_models()})
        elif p == "/api/desired":
            self._json(200, desired())
        elif p == "/api/config":
            self._json(200, config())
        elif p == "/deeplog":
            try:
                body = open(BASE + "/deep.log", errors="replace").read()[-6000:].encode()
            except Exception:
                body = b"(no log yet)"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._static(p) if p.endswith((".html", ".js", ".css", ".svg", ".png")) else self._json(404, PAGE404)

    def _proxy_get(self):
        if self.path.startswith("/v1/models"):
            data = []
            if backend_up(FAST):
                data.append({"id": "axera-fast", "object": "model",
                             "owned_by": "pimesh", "lane": "npu"})
            if backend_up(DEEP):
                data.append({"id": "mesh-30b", "object": "model",
                             "owned_by": "pimesh", "lane": "rpc-mesh"})
            data.append({"id": "auto", "object": "model", "owned_by": "pimesh"})
            self._json(200, {"object": "list", "data": data})
            return
        base = DEEP if backend_up(DEEP) else FAST
        try:
            up = urllib.request.urlopen(base + self.path, timeout=30)
            body = up.read()
            self.send_response(up.status)
            self.send_header("Content-Type", up.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._json(502, {"error": str(e)})

    def do_POST(self):
        p = self.path.split("?")[0]
        if p in ("/v1/chat/completions", "/v1/completions"):
            self._proxy_post(p)
        elif p == "/api/desired":
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            d = desired()
            if body.get("model"):
                d["model"] = body["model"]
            if body.get("ctx"):
                d["ctx"] = int(body["ctx"])
            save_json(DESIRED, d)
            record("ui", "control", "desired", f"{os.path.basename(d['model'])} ctx={d['ctx']}")
            self._json(200, {"ok": True, "desired": d,
                             "note": "watchdog applies within 60s"})
        elif p == "/api/config":
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            cfg = config()
            for k in ("route_hints", "fast_thinking", "refresh_s"):
                if k in body:
                    cfg[k] = body[k]
            save_json(CONFIG, cfg)
            self._json(200, {"ok": True, "config": cfg})
        elif p == "/api/actions/worker_restart":
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            ip = body.get("ip", "")
            if ip not in [d["ip"] for d in WORKER_DEFS]:
                self._json(400, {"error": "unknown worker"})
                return
            try:
                r = subprocess.run(
                    ["sshpass", "-p", os.environ.get("MESH_SSH_PASS",""), "ssh", "-o",
                     "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                     f"kram@{ip}",
                     "pkill -f 'ggml-rp[c]-server'; sleep 1; nohup /home/kram/pimesh-llama.cpp/build/bin/ggml-rpc-server -H 0.0.0.0 -p 50052 >/home/kram/pimesh-rpc.log 2>&1 </dev/null & echo restarted"],
                    capture_output=True, text=True, timeout=30)
                self._json(200, {"ok": True, "out": r.stdout.strip()})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif p == "/api/actions/coordinator_restart":
            try:
                subprocess.run(["pkill", "-f", "llama-server.*808[1]"], timeout=10)
                time.sleep(2)
                r = subprocess.run(["/home/kram/pimesh/deeplane.sh"],
                                   capture_output=True, text=True, timeout=120)
                self._json(200, {"ok": True, "out": r.stdout.strip()[-200:]})
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif p == "/api/actions/service_restart":
            try:
                r = subprocess.run(
                    ["systemctl", "restart", "pimesh-gateway"],
                    capture_output=True, text=True, timeout=30)
                self._json(200, {"ok": r.returncode == 0})
            except Exception as e:
                self._json(500, {"error": str(e)})
        else:
            self._json(404, PAGE404)

    def _proxy_post(self, path):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._json(400, {"error": "bad json"})
            return
        model = body.get("model") or "auto"
        base, routed = pick_model(model, body.get("messages"))
        body["model"] = routed
        if routed == "axera-fast" and "chat_template_kwargs" not in body:
            if config().get("fast_thinking") is False:
                body["chat_template_kwargs"] = {"enable_thinking": False}
        payload = json.dumps(body).encode()
        t0 = time.time()
        try:
            req = urllib.request.Request(base + path, data=payload,
                                         headers={"Content-Type": "application/json"})
            up = urllib.request.urlopen(req, timeout=600)
        except Exception as e:
            record(model, routed, "ERR", str(e)[:60])
            other = DEEP if base == FAST else FAST
            try:
                body["model"] = "mesh-30b" if other == DEEP else "axera-fast"
                req = urllib.request.Request(other + path, data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
                up = urllib.request.urlopen(req, timeout=600)
                base, routed = other, body["model"]
            except Exception as e2:
                self._json(503, {"error": f"both lanes down: {e2}"})
                return
        self.send_response(up.status)
        self.send_header("Content-Type", up.headers.get("Content-Type", "application/json"))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = up.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            pass
        try:
            up.close()
        except Exception:
            pass
        record(model, routed, "ok" if up.status == 200 else up.status,
               f"{time.time()-t0:.1f}s")


if __name__ == "__main__":
    os.makedirs(BASE, exist_ok=True)
    if not os.path.isdir(WWW):
        os.makedirs(WWW, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"pimesh gateway v2 on :{PORT}", flush=True)
    srv.serve_forever()
