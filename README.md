# GPU Farm — one brain, many free GPUs

One Render app (the **brain**) + one worker script that runs unchanged on
**Colab / Kaggle / Lightning AI**. Control everything from Telegram or a
phone dashboard. All state in Upstash Redis, all videos in Google Drive.

```
Telegram / Dashboard → Render brain (Redis queue) → workers poll & render → Drive
```

## 1. Upstash Redis (2 min)

1. upstash.com → sign up → **Create Database** → type: Redis, free plan.
2. Copy the **TLS URL** (`rediss://default:xxxx@xxx.upstash.io:6379`) from the database page.

## 2. Telegram bot (3 min)

1. Message **@BotFather** → `/newbot` → copy the token.
2. Message **@userinfobot** → copy your numeric chat id.

## 3. Deploy the brain (5 min)

```powershell
cd brain
git init; git add .; git commit -m brain
# create an EMPTY repo on github.com, then:
git remote add origin https://github.com/YOU/gpu-farm-brain.git
git push -u origin main
```

1. render.com → **New → Web Service** → connect that repo.
2. Runtime auto-detected (render.yaml included). Add env vars:

| Key | Value |
|---|---|
| `REDIS_URL` | your `rediss://...` from step 1 |
| `API_KEY` | any long random string — this is your master password |
| `TELEGRAM_TOKEN` | bot token from step 2 |
| `TELEGRAM_CHAT_ID` | your chat id from step 2 |

3. After deploy, note your URL: `https://YOUR-APP.onrender.com`
4. Register the Telegram webhook (run once, in PowerShell):

```powershell
curl "https://api.telegram.org/bot<TELEGRAM_TOKEN>/setWebhook?url=https://YOUR-APP.onrender.com/telegram"
```

5. Keep the free dyno awake: cron-job.org → ping `https://YOUR-APP.onrender.com/healthz` every 10 min.

## 4. Run a worker

**Colab:** new notebook → GPU runtime → paste ALL of `worker/worker.py` into one
cell → edit the 3 config lines at the top (`BRAIN_URL`, `API_KEY`) → Run.
(Optional: enable background execution so it survives closing the tab.)

**Kaggle:** new notebook → Settings → Accelerator: GPU P100 → Internet: On →
Persistence: Files only → same paste + edit + Run.

**Lightning:** new Studio → T4 → same thing.

The same script auto-detects where it's running. Run several at once —
the brain hands each job to exactly one worker whose GPU matches the job kind.

## 5. Use it

- **Telegram:** `/video a cat skateboarding through neon Tokyo` ·
  `/pro cinematic mountain drone shot` · `/ask explain LoRA in one paragraph` ·
  `/tunnel` (get the live OpenAI-compatible LLM API URL) · `/status` · `/jobs`
- **Dashboard:** `https://YOUR-APP.onrender.com/?key=YOUR_API_KEY`
  (bookmark this on your phone)
- **API:** `POST /api/submit` with header `x-api-key` — usable from any script.
- **LLM from your local machine:** run `/tunnel` in Telegram, then point any
  OpenAI-compatible client at it:

```python
from openai import OpenAI
client = OpenAI(base_url="<tunnel-url>/v1", api_key="anything")
print(client.chat.completions.create(
    model="qwen2.5:7b-instruct",
    messages=[{"role": "user", "content": "hello"}]).choices[0].message.content)
```

## Worker modes

The worker script has a `MODE` switch at the top:

| MODE | Claims | What it runs |
|---|---|---|
| `video` | draft + pro video jobs | LTX-Video pipeline |
| `llm` | `/ask` jobs | Ollama + your chosen model |
| `both` | everything | video + LLM on one GPU |

In `llm`/`both` mode the worker auto-installs Ollama, pulls `LLM_MODEL`,
opens a cloudflared tunnel, and registers the URL with the brain — so
`/tunnel` always shows your live, OpenAI-compatible API endpoint.

Typical split on your Pro account: **Session 1** = T4 in `llm` mode,
**Session 2** = T4/A100 in `video` mode. Both keep running with the
browser closed (Colab background execution).

## Hands-free operation (the fun part)

See `scripts/kaggle_autostart.md` — Kaggle can run your worker notebook
**on a schedule** (free, native feature). Each scheduled wake checks the
queue and exits in ~2 minutes if there's no work, so a few wakes per day
cost almost nothing. Your farm effectively turns itself on, works, and
sleeps. Colab paid tiers have the same feature under Share → Schedule.

## How the self-healing works

- Worker dies mid-job → **reaper** (brain, every 2 min) requeues the job.
- Job throws an error → brain **classifies** it (OOM / transient / missing file /
  auth), auto-retries up to 3×, and logs the fix into a `known_fixes` registry
  that every worker downloads at startup — the farm gets smarter per failure.
- Worker idle 10 min → Colab worker calls `runtime.unassign()` so it stops
  burning your compute units; Kaggle/lightning sessions just end.

## Limits & assumptions

- Worker currently implements **video generation via LTX-Video**
  (`video_draft` / `video_pro` kinds). `train` / `llm` kinds are queued and
  routed by GPU class — extend `run_job()` in the worker for those.
- Videos save to `Drive/videos` (Colab) or `/kaggle/working/videos` (Kaggle —
  download via notebook output, or wire in the Drive API if you want).
- Free-tier reality: Render sleeps after 15 min idle (the cron ping fixes it),
  Kaggle needs phone verification, Lightning credits don't roll over.
- First model download (~10 GB) is slow; afterwards Colab workers use the
  Drive HF cache and load in ~1 min.

## Costs

$0 beyond your existing Google One subscription.
