"""
GPU Farm Brain — central queue + health monitor + Telegram remote.
Deploys on Render free tier. State lives in Upstash Redis.
Workers (Colab / Kaggle / Lightning) poll /api/next for jobs.
"""
import os
import json
import time
import uuid
import asyncio
import threading
import traceback
from typing import Optional

import redis
import httpx
from fastapi import FastAPI, Request, Header, HTTPException, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ----------------------------- config -----------------------------
API_KEY = os.environ.get("API_KEY", "change-me")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")  # your personal chat id
REDIS_URL = os.environ.get("REDIS_URL", "")
WORKER_TIMEOUT_S = int(os.environ.get("WORKER_TIMEOUT_S", "300"))
MAX_ATTEMPTS = 3

if not REDIS_URL:
    raise RuntimeError("REDIS_URL env var is required (Upstash rediss:// URL)")
if not REDIS_URL.startswith(("rediss://", "redis://")):
    raise RuntimeError("REDIS_URL must start with rediss:// — got: "
                       + REDIS_URL[:15] + "...")
r = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    protocol=2,                 # RESP2 — Upstash closes RESP3 handshakes from some networks
    socket_timeout=10,
    socket_connect_timeout=10,
    socket_keepalive=True,
    health_check_interval=30,   # drop stale connections before they break requests
    retry_on_timeout=True,
)


def _attach_error_logging(app):
    @app.middleware("http")
    async def log_errors(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception:
            print("UNHANDLED ERROR on", request.url.path)
            traceback.print_exc()
            raise

app = FastAPI(title="GPU Farm Brain")
_attach_error_logging(app)

QUEUE = "queue:pending"      # redis list of pending job ids
RECENT = "jobs:recent"       # redis list of recent job ids (newest first)
JOB = "job:"                 # hash prefix per job
WORKER = "worker:"           # hash prefix per worker
WORKERS = "workers"          # set of worker ids
FIXES = "known_fixes"        # hash: error category -> fix hint (self-improving log)

# job kind -> gpu classes allowed to claim it (worker gpu must be in list)
KIND_CAPS = {
    "video_draft": ["t4", "l4", "p100", "a100", "m4000"],
    "video_pro": ["a100", "l4"],
    "train": ["p100", "a100", "t4"],
    "llm": ["t4", "l4", "a100", "p100"],
}

HELP_TEXT = (
    "GPU Farm remote\n"
    "/video <prompt>  — queue a draft video (any GPU)\n"
    "/pro <prompt>    — queue on A100/L4 only\n"
    "/ask <question>  — ask the farm LLM\n"
    "/tunnel          — current LLM API URL (OpenAI-compatible)\n"
    "/status          — queue + worker health\n"
    "/jobs            — last 10 jobs\n"
    "/help            — this message"
)


def auth(x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(403, "bad api key")


# ----------------------------- helpers -----------------------------
def save_job(job: dict):
    r.set(JOB + job["id"], json.dumps(job))


def get_job(jid: str) -> Optional[dict]:
    raw = r.get(JOB + jid)
    return json.loads(raw) if raw else None


def enqueue(kind: str, prompt: str, notify_chat: str = "",
            frames: int = 97, width: int = 704, height: int = 480,
            steps: int = 30, model: str = "qwen2.5:7b-instruct") -> str:
    jid = uuid.uuid4().hex[:8]
    job = {
        "id": jid, "kind": kind, "prompt": prompt, "status": "queued",
        "frames": frames, "width": width, "height": height, "steps": steps,
        "model": model,
        "attempts": 0, "notify_chat": notify_chat, "created": time.time(),
        "started": None, "worker": None, "result": None, "error": None,
    }
    save_job(job)
    r.rpush(QUEUE, jid)
    r.lpush(RECENT, jid)
    r.ltrim(RECENT, 0, 49)
    return jid


async def tg_send(text: str, chat: str = ""):
    chat = chat or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not chat:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(
                "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage",
                json={"chat_id": chat, "text": text},
            )
    except Exception as e:
        print("telegram send failed:", e)


def classify_error(tb: str) -> dict:
    """Rule-based classifier. The self-improving loop: categories + fixes
    are logged to Redis so every failure makes the system smarter."""
    t = tb.lower()
    if "out of memory" in t or "outofmemoryerror" in t or "cuda oom" in t:
        return {"cat": "oom", "retry": True,
                "fix": "shrink frames/resolution and retry with cpu offload"}
    if any(k in t for k in ("connectionerror", "timeout", "503", "temporarily", "broken pipe")):
        return {"cat": "transient", "retry": True,
                "fix": "retry, preferably on a different worker"}
    if "filenotfounderror" in t or "no such file" in t:
        return {"cat": "missing_file", "retry": False,
                "fix": "check drive mount / HF cache path"}
    if "401" in t or "403" in t or "unauthorized" in t or "forbidden" in t:
        return {"cat": "auth", "retry": False,
                "fix": "check HF token / API key on that worker"}
    return {"cat": "unknown", "retry": True, "fix": "retry once on another worker"}


# ----------------------------- worker API -----------------------------
class Register(BaseModel):
    worker_id: str
    platform: str
    gpu: str
    caps: list = ["video_draft", "llm"]


@app.post("/api/register", dependencies=[Depends(auth)])
def register(w: Register):
    r.hset(WORKER + w.worker_id, mapping={
        "platform": w.platform, "gpu": w.gpu,
        "caps": ",".join(w.caps), "status": "idle",
        "last_seen": time.time(),
    })
    r.sadd(WORKERS, w.worker_id)
    fixes = r.hgetall(FIXES)
    return {"ok": True, "known_fixes": fixes}


@app.post("/api/next", dependencies=[Depends(auth)])
def next_job(w: Register):
    r.hset(WORKER + w.worker_id, mapping={
        "platform": w.platform, "gpu": w.gpu,
        "caps": ",".join(w.caps), "status": "idle",
        "last_seen": time.time(),
    })
    r.sadd(WORKERS, w.worker_id)
    ids = r.lrange(QUEUE, 0, -1)
    for jid in ids:
        job = get_job(jid)
        if not job or job["status"] != "queued":
            r.lrem(QUEUE, 1, jid)
            continue
        allowed = KIND_CAPS.get(job["kind"], ["t4", "l4", "a100"])
        if w.gpu not in allowed:
            continue
        if job["kind"] not in w.caps:
            continue
        job["status"] = "running"
        job["worker"] = w.worker_id
        job["started"] = time.time()
        job["attempts"] += 1
        save_job(job)
        r.lrem(QUEUE, 1, jid)
        return {"job_id": jid, "job": job}
    return {"job_id": None}


@app.post("/api/heartbeat/{wid}", dependencies=[Depends(auth)])
def heartbeat(wid: str, status: str = "idle"):
    if r.exists(WORKER + wid):
        r.hset(WORKER + wid, mapping={"last_seen": time.time(), "status": status})
    return {"ok": True}


class Done(BaseModel):
    job_id: str
    result: str


def human_result(job: dict, result: str) -> str:
    """What Telegram shows when a job finishes."""
    if job["kind"] == "llm":
        return "🧠 %s\nQ: %s\n\n%s" % (job["id"], job["prompt"][:150], result)
    return "✅ done %s\n%s" % (job["id"], result)


@app.post("/api/complete", dependencies=[Depends(auth)])
def complete(d: Done):
    job = get_job(d.job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    job["status"] = "done"
    job["result"] = d.result
    save_job(job)
    asyncio.run(tg_send(human_result(job, d.result),
                        job.get("notify_chat", "")))
    return {"ok": True}


class Fail(BaseModel):
    job_id: str
    error: str


@app.post("/api/fail", dependencies=[Depends(auth)])
def fail(f: Fail):
    job = get_job(f.job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    diag = classify_error(f.error)
    seen = int(r.hincrby("fix_counts", diag["cat"], 1))
    r.hset(FIXES, mapping={diag["cat"]: diag["fix"] + " (seen %dx)" % seen})
    if diag["retry"] and job["attempts"] < MAX_ATTEMPTS:
        if diag["cat"] == "oom":
            # GPU-aware auto-shrink: each OOM retry renders smaller
            job["frames"] = max(33, int(job["frames"] * 0.7))
            job["width"] = max(384, int(job["width"] * 0.85))
            job["height"] = max(320, int(job["height"] * 0.85))
        job["status"] = "queued"
        job["error"] = diag["cat"] + ": " + f.error[-400:]
        save_job(job)
        r.rpush(QUEUE, job["id"])
        note = "♻️ %s failed (%s) — auto-retry %d/%d" % (
            job["id"], diag["cat"], job["attempts"], MAX_ATTEMPTS)
    else:
        job["status"] = "failed"
        job["error"] = diag["cat"] + ": " + f.error[-400:]
        save_job(job)
        note = "❌ %s FAILED (%s)\n%s" % (job["id"], diag["cat"], f.error[-300:])
    asyncio.run(tg_send(note, job.get("notify_chat", "")))
    return {"ok": True, "diagnosis": diag}


class Submit(BaseModel):
    prompt: str
    kind: str = "video_draft"
    frames: int = 97
    width: int = 704
    height: int = 480
    steps: int = 30


@app.post("/api/submit", dependencies=[Depends(auth)])
def submit(s: Submit):
    jid = enqueue(s.kind, s.prompt, frames=s.frames,
                  width=s.width, height=s.height, steps=s.steps)
    return {"job_id": jid}


@app.get("/api/status", dependencies=[Depends(auth)])
def status():
    return _status_payload()


# ------------- LLM tunnel registry -------------
# LLM-mode workers expose Ollama through a cloudflared URL and register it
# here so your local tools (and /tunnel) always know where the API lives.
class Tunnel(BaseModel):
    worker_id: str
    url: str


@app.post("/api/tunnel", dependencies=[Depends(auth)])
def set_tunnel(t: Tunnel):
    r.hset("tunnels", mapping={t.worker_id: t.url})
    return {"ok": True}


@app.get("/api/tunnel", dependencies=[Depends(auth)])
def get_tunnel():
    now = time.time()
    live = {}
    for wid, url in r.hgetall("tunnels").items():
        w = r.hgetall(WORKER + wid)
        if w and now - float(w.get("last_seen", 0)) < WORKER_TIMEOUT_S:
            live[wid] = url
    return {"live_tunnels": live}


def _status_payload():
    now = time.time()
    workers = []
    for wid in r.smembers(WORKERS):
        w = r.hgetall(WORKER + wid)
        if not w:
            continue
        age = int(now - float(w.get("last_seen", 0)))
        w["id"] = wid
        w["alive"] = age < WORKER_TIMEOUT_S
        w["last_seen_s_ago"] = age
        workers.append(w)
    jobs = [get_job(j) for j in r.lrange(RECENT, 0, 9)]
    return {
        "queue_length": r.llen(QUEUE),
        "workers": sorted(workers, key=lambda x: x["id"]),
        "recent_jobs": [j for j in jobs if j],
        "known_fixes": r.hgetall(FIXES),
    }


# ----------------------------- telegram bot -----------------------------
@app.post("/telegram")
async def telegram_webhook(req: Request):
    upd = await req.json()
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat or not text:
        return {"ok": True}
    if TELEGRAM_CHAT_ID and chat != TELEGRAM_CHAT_ID:
        await tg_send("unauthorized chat id: " + chat)  # tells YOU someone tried
        return {"ok": True}

    if text.startswith("/video "):
        jid = enqueue("video_draft", text[7:].strip(), notify_chat=chat)
        await tg_send("queued draft %s 🎬" % jid, chat)
    elif text.startswith("/pro "):
        jid = enqueue("video_pro", text[5:].strip(), notify_chat=chat)
        await tg_send("queued PRO %s 🚀 (needs an A100/L4 worker)" % jid, chat)
    elif text.startswith("/ask "):
        jid = enqueue("llm", text[5:].strip(), notify_chat=chat)
        await tg_send("queued LLM %s 🧠 (needs a worker in llm mode)" % jid, chat)
    elif text.startswith("/tunnel"):
        now = time.time()
        live = []
        for wid, url in r.hgetall("tunnels").items():
            w = r.hgetall(WORKER + wid)
            if w and now - float(w.get("last_seen", 0)) < WORKER_TIMEOUT_S:
                live.append("%s → %s" % (wid, url))
        await tg_send(
            "live LLM tunnels (OpenAI-compatible, api_key any string):\n"
            + ("\n".join(u + "/v1" for u in live) if live
               else "none — start an llm worker first"), chat)
    elif text.startswith("/status"):
        s = _status_payload()
        lines = ["queue: %d" % s["queue_length"], "workers:"]
        for w in s["workers"]:
            lines.append("  %s %s [%s/%s] %s" % (
                "🟢" if w["alive"] else "🔴", w["id"], w.get("platform"),
                w.get("gpu"), w.get("status")))
        await tg_send("\n".join(lines), chat)
    elif text.startswith("/jobs"):
        s = _status_payload()
        lines = []
        for j in s["recent_jobs"]:
            lines.append("%s %s — %s" % (j["id"], j["status"], j["prompt"][:40]))
        await tg_send("\n".join(lines) or "no jobs yet", chat)
    elif text.startswith("/help") or text.startswith("/start"):
        await tg_send(HELP_TEXT, chat)
    else:
        await tg_send("unknown command — try /help", chat)
    return {"ok": True}


# ----------------------------- dashboard -----------------------------
DASH = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>GPU Farm</title><style>
body{font-family:system-ui;background:#0d1117;color:#e6edf3;max-width:720px;margin:auto;padding:16px}
input,button{width:100%;padding:12px;margin:4px 0;border-radius:8px;border:1px solid #30363d;background:#161b22;color:#e6edf3;font-size:16px}
button{background:#238636;border:0;font-weight:700}
.pro{background:#8957e5}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px;margin:8px 0;font-size:14px}
small{color:#8b949e}.on{color:#3fb950}.off{color:#f85149}
</style>
<h2>🎛️ GPU Farm</h2>
<div class=card><input id=p placeholder="describe your video…">
<button onclick="q('video_draft')">Queue draft (any GPU)</button>
<button class=pro onclick="q('video_pro')">Queue PRO (A100/L4)</button></div>
<div class=card id=w>workers…</div>
<div class=card id=j>jobs…</div>
<script>
const KEY=new URLSearchParams(location.search).get('key')||'';
async function q(kind){const p=document.getElementById('p').value;if(!p)return;
 await fetch('/api/submit',{method:'POST',headers:{'content-type':'application/json','x-api-key':KEY},
 body:JSON.stringify({prompt:p,kind:kind})});document.getElementById('p').value='';refresh();}
async function refresh(){try{
 const s=await(await fetch('/api/status',{headers:{'x-api-key':KEY}})).json();
 document.getElementById('w').innerHTML='<b>workers</b> (queue: '+s.queue_length+')<br>'+
  s.workers.map(w=>`<span class=${w.alive?'on':'off'}>●</span> ${w.id} <small>${w.platform}/${w.gpu} — ${w.status}</small>`).join('<br>')||'<small>none</small>';
 document.getElementById('j').innerHTML='<b>recent jobs</b><br>'+
  s.recent_jobs.map(j=>`${j.id} — <b>${j.status}</b> <small>${j.prompt.slice(0,50)}</small><br><small>${j.result||j.error||''}</small>`).join('<br>')||'<small>none</small>';
}catch(e){}}
refresh();setInterval(refresh,8000);
</script>"""


@app.get("/", response_class=HTMLResponse)
def dash(key: str = ""):
    if key != API_KEY:
        raise HTTPException(403, "open this page with ?key=YOUR_API_KEY")
    return DASH


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/envcheck", dependencies=[Depends(auth)])
def envcheck():
    """Safe diagnostic: which env vars are set + live Redis ping."""
    return {
        "REDIS_URL_set": bool(REDIS_URL),
        "REDIS_URL_scheme": REDIS_URL.split("://")[0] if REDIS_URL else "",
        "API_KEY_set": bool(API_KEY and API_KEY != "change-me"),
        "TELEGRAM_TOKEN_set": bool(TELEGRAM_TOKEN),
        "TELEGRAM_CHAT_ID_set": bool(TELEGRAM_CHAT_ID),
        "redis_ping": r.ping(),
    }


# ----------------------------- reaper -----------------------------
def reaper_loop():
    """Every 2 min: if a worker died mid-job, requeue its job so
    another worker (or a restarted one) picks it up."""
    while True:
        time.sleep(120)
        try:
            now = time.time()
            for wid in r.smembers(WORKERS):
                w = r.hgetall(WORKER + wid)
                if not w:
                    continue
                if now - float(w.get("last_seen", 0)) > WORKER_TIMEOUT_S:
                    for jid in r.lrange(RECENT, 0, 49):
                        job = get_job(jid)
                        if (job and job["status"] == "running"
                                and job.get("worker") == wid
                                and job["attempts"] < MAX_ATTEMPTS):
                            job["status"] = "queued"
                            save_job(job)
                            r.rpush(QUEUE, jid)
                            asyncio.run(tg_send(
                                "⚠️ worker %s died mid-job %s — requeued" % (wid, jid)))
        except Exception as e:
            print("reaper error:", e)


@app.on_event("startup")
def startup():
    threading.Thread(target=reaper_loop, daemon=True).start()


