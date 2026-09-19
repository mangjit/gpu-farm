"""Hit the LIVE brain API to reproduce the /api/status 500 (stdlib only)."""
import json
import urllib.request

B = "https://gpu-farm-brain.onrender.com"
H = {"x-api-key": "farm-56748a0b7b35453c5a67207e99fd0105e4939558"}


def call(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(B + path, data=data, method=method,
                                 headers={**H, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


for path in ["/healthz", "/api/status"]:
    s, t = call("GET", path)
    print(path, "->", s)
    print(t[:400])
    print("---")

s, t = call("POST", "/api/register", {
    "worker_id": "test-local", "platform": "generic", "gpu": "t4",
    "caps": ["video_draft"]})
print("/api/register ->", s, t[:200])

s, t = call("GET", "/api/status")
print("/api/status after register ->", s)
print(t[:800])
