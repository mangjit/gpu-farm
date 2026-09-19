# 🎛️ GPU Farm — one brain, many free GPUs

Turn free GPU tiers (Google Colab, Kaggle, Lightning AI) into one automated,
self-healing render farm + private LLM API — controlled entirely from
**Telegram** or a phone dashboard. All state lives in Upstash Redis, all
videos land in Google Drive.

```
Telegram / Dashboard ──▶ Render brain (Redis queue) ──▶ workers poll & render ──▶ Drive
```

**Total cost: $0** (beyond your existing Google One subscription).

---

## 🚀 Full setup — zero to working farm (~25 min)

### Step 1 — Upstash Redis (2 min) · the farm's memory

1. Go to **console.upstash.com** → sign up (free)
2. **Create Database** → name `gpu-farm` → type **Redis** → pick nearest region
3. Open the database → copy the **TLS endpoint**, it looks like:
   `rediss://default:AaBbCc...@apn1-xxx.upstash.io:6379`

### Step 2 — Telegram bot (3 min) · your remote control

1. In Telegram, message **@BotFather** → send `/newbot` → follow the prompts
   → copy the **token** (looks like `123456:ABC-DEF...`)
2. Message **@userinfobot** → copy your **chat id** (a number like `123456789`)

### Step 3 — Deploy the brain on Render (7 min)

1. **render.com** → sign up/login with GitHub
2. **New + → Web Service** → connect your `gpu-farm` repo
3. Settings:
   - **Root Directory:** `brain`  ← important! (the app lives in the subfolder)
   - Build/start commands auto-fill from `render.yaml` — leave them
4. **Environment → Add Environment Variable** — add these 4:

| Key | Value |
|---|---|
| `REDIS_URL` | your `rediss://...` from Step 1 |
| `API_KEY` | make up a long random password — **save it** |
| `TELEGRAM_TOKEN` | token from Step 2 |
| `TELEGRAM_CHAT_ID` | your chat id from Step 2 |

5. **Deploy** → wait ~2 min → you get `https://gpu-farm-brain.onrender.com`
6. ✅ **Test:** open `https://gpu-farm-brain.onrender.com/healthz`
   → must show `{"ok":true}`

### Step 4 — Connect Telegram to the brain (1 min)

Open this URL in your browser (replace the two placeholders):

```
https://api.telegram.org/bot<YOUR_TELEGRAM_TOKEN>/setWebhook?url=https://gpu-farm-brain.onrender.com/telegram
```

Must return `{"ok":true,...}`.
✅ **Test:** message your bot `/help` — if it replies, the brain is live. 🎉

### Step 5 — Keep Render awake (2 min)

Free Render sleeps after 15 min idle. Fix:

1. **cron-job.org** → free account → **Create cronjob**
2. URL: `https://gpu-farm-brain.onrender.com/healthz` → every **10 minutes**

### Step 6 — Start your first worker (5 min)

**Google Colab** (use your Pro account):

1. **colab.research.google.com** → New notebook
2. **Runtime → Change runtime type → T4 GPU**
3. Copy the **entire** `worker/worker.py` from this repo → paste into one cell
4. Edit the config lines at the top:

   ```python
   BRAIN_URL = "https://gpu-farm-brain.onrender.com"
   API_KEY   = "your-same-api-key-from-step-3"
   MODE      = "video"        # "video" | "llm" | "both"
   ```

5. Run the cell → it prints `registered. known fixes so far: ...` → **live** ✅
6. Pro perk: the notebook keeps running with the browser tab closed.

**Kaggle** (optional 2nd worker — free 30 h/week P100):

- New notebook → Settings → Accelerator: **GPU P100** → Internet: **On** →
  Persistence: **Files only** → paste the worker (`MODE = "both"`) → Run.

**Lightning AI** (optional 3rd worker — ~15 h/month T4):

- New Studio → T4 → same paste + run.

The **same script** auto-detects where it runs. Run several workers at once —
the brain hands each job to exactly one worker whose GPU matches the job kind.

### Step 7 — Use it 🎬

**From Telegram:**

| Command | Does |
|---|---|
| `/video a cat skateboarding through neon Tokyo, cinematic` | draft render (any GPU) |
| `/pro epic mountain drone shot at dawn` | routed to A100/L4 only |
| `/ask write 5 video prompt ideas about oceans` | answered by your farm LLM |
| `/tunnel` | shows your live OpenAI-compatible LLM API URL |
| `/status` | worker health 🟢/🔴 + queue length |
| `/jobs` | last 10 jobs |

**Dashboard (bookmark on your phone):**
`https://gpu-farm-brain.onrender.com/?key=YOUR_API_KEY`

**API (from any script):**
`POST /api/submit` with header `x-api-key: YOUR_API_KEY` and JSON
`{"prompt": "...", "kind": "video_draft"}`

**LLM from your local machine** (VS Code / Cline / any OpenAI client):

```python
from openai import OpenAI
client = OpenAI(base_url="<tunnel-url-from-/tunnel>/v1", api_key="anything")
print(client.chat.completions.create(
    model="qwen2.5:7b-instruct",
    messages=[{"role": "user", "content": "hello"}]).choices[0].message.content)
```

A few minutes after `/video`, Telegram pings you ✅ with the file path —
the MP4 is in **Google Drive → videos/** (auto-syncs to your PC if you have
Drive for desktop installed).

---

## 🧠 How the self-healing works

- **Worker dies mid-job** → the brain's reaper (every 2 min) requeues the job
  and Telegrams you. Colab dies at peak hours? Kaggle picks the job up.
- **Job throws an error** → brain classifies it (OOM / transient / missing
  file / auth) → auto-retries up to 3× → the fix is logged into a shared
  `known_fixes` registry that **every worker downloads at startup**. The farm
  literally gets more reliable each time it breaks.
- **OOM retry auto-shrinks** frames + resolution, so the retry usually fits.
- **Idle 10 min** → Colab workers call `runtime.unassign()` to stop burning
  your compute units; Kaggle/Lightning sessions just end.

## 🤖 Hands-free operation

See `scripts/kaggle_autostart.md` — Kaggle runs your worker notebook **on a
schedule** (free, native). Each wake checks the queue and exits in ~2 minutes
if there's no work, so a few wakes/day cost almost nothing. Your farm wakes
itself up, works, and sleeps. Colab paid tiers have the same under
Share → Schedule.

## ⚙️ Worker modes

| `MODE` | Claims | Runs |
|---|---|---|
| `video` | draft + pro video jobs | LTX-Video pipeline |
| `llm` | `/ask` jobs | Ollama + your model |
| `both` | everything | video + LLM on one GPU |

In `llm`/`both` mode the worker auto-installs Ollama, pulls `LLM_MODEL`,
opens a cloudflared tunnel, and registers the URL with the brain — `/tunnel`
always shows your live API endpoint.

**Typical split on a Google One Pro account:**
Session 1 = T4 in `llm` mode · Session 2 = T4/A100 in `video` mode.

## 📋 First-run notes & limits

- First render is slow (~5–10 min): LTX-Video downloads once into your Drive
  HF cache. Afterwards ~2–4 min per clip.
- Videos: Colab → `Drive/videos` · Kaggle → `/kaggle/working/videos`
  (notebook output).
- The worker currently implements **video generation (LTX-Video)** and
  **LLM (Ollama)**. The `train` job kind is routed by GPU class — extend
  `run_job()` in the worker to use it.
- Free-tier reality: Kaggle needs phone verification; Lightning credits
  don't roll over; Colab compute units are your real budget (~100/mo Pro).
- Test suite: `cd brain && python test_import.py` — 14 checks, all passing.

## 📁 Repo layout

```
gpu-farm/
├── README.md                  ← you are here
├── brain/                     ← deploy this folder to Render (root dir: brain)
│   ├── app.py                 ← queue, routing, Telegram bot, dashboard,
│   │                            reaper, error classifier, tunnel registry
│   ├── requirements.txt
│   ├── render.yaml
│   └── test_import.py         ← test suite (14 checks)
├── worker/
│   └── worker.py              ← one-cell paste for Colab/Kaggle/Lightning
└── scripts/
    └── kaggle_autostart.md    ← hands-free scheduled wake-up
```
