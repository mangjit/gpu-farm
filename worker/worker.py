"""
GPU FARM WORKER — paste this entire file into ONE cell and run it.
Works unchanged on Google Colab, Kaggle, and Lightning AI.
Auto-detects platform + GPU, polls the brain for jobs, generates
video (LTX-Video), reports errors, and self-terminates when idle.
"""
import os
import sys
import json
import time
import threading
import subprocess
import traceback

# ========================= EDIT THESE =========================
BRAIN_URL = "https://your-brain.onrender.com"   # your Render app URL
API_KEY = "your-shared-secret"                  # must match brain API_KEY
WORKER_NAME = ""                                # optional friendly name
MODE = "video"        # "video" | "llm" | "both"
LLM_MODEL = "qwen2.5:7b-instruct"   # pulled by Ollama in llm/both mode
EXPOSE_LLM = True     # start cloudflared tunnel + register URL with brain
# ==============================================================

MAX_IDLE_CYCLES = 20        # 20 x 30s = 10 min idle before shutdown
POLL_S = 30
HEARTBEAT_S = 60

STATE = {"status": "idle"}


def detect_platform():
    if os.path.exists("/kaggle"):
        return "kaggle"
    try:
        import google.colab  # noqa: F401
        return "colab"
    except Exception:
        pass
    if os.environ.get("LIGHTNING_CLOUD_PROJECT_ID") or os.path.exists("/teamspace"):
        return "lightning"
    return "generic"


PLATFORM = detect_platform()

# ---------------- storage ----------------
if PLATFORM == "colab":
    from google.colab import drive
    drive.mount("/content/drive")
    OUT_DIR = "/content/drive/MyDrive/videos"
    CACHE = "/content/drive/MyDrive/hf_cache"
    os.environ.setdefault("HF_HOME", CACHE)
    os.makedirs(CACHE, exist_ok=True)
elif PLATFORM == "kaggle":
    OUT_DIR = "/kaggle/working/videos"   # persists as notebook output
else:
    OUT_DIR = os.path.abspath("./videos")
os.makedirs(OUT_DIR, exist_ok=True)


def gpu_class():
    try:
        import torch
        if not torch.cuda.is_available():
            return "cpu"
        name = torch.cuda.get_device_name(0).lower()
        for k in ("a100", "l4", "t4", "p100", "m4000"):
            if k in name:
                return k
        return "gpu"
    except Exception:
        return "cpu"


GPU = gpu_class()
WORKER_ID = WORKER_NAME or (PLATFORM + "-" + GPU + "-" + str(os.getpid())[-4:])
print("platform:", PLATFORM, "| gpu:", GPU, "| worker:", WORKER_ID)

# ---------------- deps (only install what is missing) ----------------
def ensure(import_name, pip_name=None):
    try:
        __import__(import_name)
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                        pip_name or import_name], check=False)


for mod, pkg in [("requests", "requests"), ("diffusers", "diffusers"),
                 ("transformers", "transformers"), ("accelerate", "accelerate"),
                 ("sentencepiece", "sentencepiece"), ("imageio", "imageio[ffmpeg]")]:
    ensure(mod, pkg)

import requests

# ---------------- brain client ----------------
CAPS = {"video": ["video_draft", "video_pro"],
        "llm": ["llm"],
        "both": ["video_draft", "video_pro", "llm"]}[MODE]

H = {"x-api-key": API_KEY}
ME = {"worker_id": WORKER_ID, "platform": PLATFORM, "gpu": GPU, "caps": CAPS}


def api(method, path, **kw):
    return requests.request(method, BRAIN_URL.rstrip("/") + path,
                            headers=H, timeout=30, **kw)


reg = api("POST", "/api/register", json=ME).json()
KNOWN_FIXES = reg.get("known_fixes", {})
print("registered. known fixes so far:", KNOWN_FIXES)


def heartbeat_loop():
    while True:
        try:
            api("POST", "/api/heartbeat/%s?status=%s" % (WORKER_ID, STATE["status"]))
        except Exception:
            pass
        time.sleep(HEARTBEAT_S)


threading.Thread(target=heartbeat_loop, daemon=True).start()

# ---------------- ollama + tunnel (llm / both mode) ----------------
def sh(cmd, check=False):
    return subprocess.run(cmd, shell=isinstance(cmd, str), check=check,
                          capture_output=True, text=True)


def setup_llm():
    """Install Ollama, pull the model, expose via cloudflared, register URL."""
    print("setting up ollama…")
    sh("curl -fsSL https://ollama.com/install.sh | sh")
    subprocess.Popen("ollama serve > /tmp/ollama.log 2>&1", shell=True)
    time.sleep(8)
    pull = sh("ollama pull %s" % LLM_MODEL)
    print("ollama pull:", (pull.stdout or pull.stderr)[-200:])

    if not EXPOSE_LLM:
        return
    if PLATFORM in ("colab", "lightning", "generic"):
        sh("wget -q https://github.com/cloudflare/cloudflared/releases/latest/"
           "download/cloudflared-linux-amd64 -O /tmp/cloudflared")
        sh("chmod +x /tmp/cloudflared")
        subprocess.Popen("/tmp/cloudflared tunnel --url http://localhost:11434"
                         " > /tmp/tunnel.log 2>&1", shell=True)
        url = None
        for _ in range(30):                     # wait up to 60s for the URL
            time.sleep(2)
            try:
                log = open("/tmp/tunnel.log").read()
                m = re.search(r"https://[a-z0-9\-]+\.trycloudflare\.com", log)
                if m:
                    url = m.group(0)
                    break
            except Exception:
                pass
        if url:
            api("POST", "/api/tunnel", json={"worker_id": WORKER_ID, "url": url})
            print("LLM tunnel live:", url + "/v1  (OpenAI-compatible)")
        else:
            print("WARNING: cloudflared did not produce a URL")


if MODE in ("llm", "both"):
    import re
    threading.Thread(target=setup_llm, daemon=True).start()


def run_llm(job):
    r2 = requests.post("http://localhost:11434/api/generate", json={
        "model": job.get("model") or LLM_MODEL,
        "prompt": job["prompt"],
        "stream": False,
    }, timeout=600)
    r2.raise_for_status()
    answer = r2.json().get("response", "").strip()
    STATE["status"] = "idle"
    return answer[:3500]      # telegram message limit is 4096 chars


# ---------------- pipeline (lazy load on first job) ----------------
pipe = None


def get_pipe():
    global pipe
    if pipe is None:
        import torch
        from diffusers import LTXPipeline
        STATE["status"] = "loading-model"
        pipe = LTXPipeline.from_pretrained("Lightricks/LTX-Video",
                                           torch_dtype=torch.bfloat16)
        try:
            pipe.enable_model_cpu_offload()
        except Exception:
            pipe = pipe.to("cuda")
        try:
            pipe.vae.enable_tiling()
        except Exception:
            pass
    return pipe


def run_job(job):
    if job["kind"] == "llm":
        STATE["status"] = "thinking:" + job["id"]
        return run_llm(job)
    from diffusers.utils import export_to_video
    p = get_pipe()
    STATE["status"] = "generating:" + job["id"]
    video = p(prompt=job["prompt"],
              negative_prompt="worst quality, blurry, distorted, watermark",
              width=int(job["width"]), height=int(job["height"]),
              num_frames=int(job["frames"]),
              num_inference_steps=int(job["steps"])).frames[0]
    out = os.path.join(OUT_DIR, job["id"] + ".mp4")
    export_to_video(video, out, fps=24)
    STATE["status"] = "idle"
    return out


def shutdown():
    print("idle timeout — releasing runtime")
    if PLATFORM == "colab":
        from google.colab import runtime
        runtime.unassign()          # stops burning compute units
    elif PLATFORM == "kaggle":
        pass                        # session ends when cell finishes
    raise SystemExit(0)


# ---------------- main loop ----------------
print("worker loop started — polling", BRAIN_URL)
idle = 0
while True:
    try:
        resp = api("POST", "/api/next", json=ME).json()
        jid = resp.get("job_id")
        if jid:
            idle = 0
            job = resp["job"]
            print("picked up job", jid, ":", job["prompt"][:60])
            try:
                result = run_job(job)
                api("POST", "/api/complete", json={"job_id": jid, "result": result})
                print("done:", result)
            except Exception:
                tb = traceback.format_exc()
                print(tb)
                STATE["status"] = "idle"
                api("POST", "/api/fail", json={"job_id": jid, "error": tb})
        else:
            idle += 1
            print("idle %d/%d" % (idle, MAX_IDLE_CYCLES), end="\r")
            if idle >= MAX_IDLE_CYCLES:
                shutdown()
    except SystemExit:
        raise
    except Exception as e:
        print("poll error (retrying):", e)
    time.sleep(POLL_S)

