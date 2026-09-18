# Kaggle auto-starter — wake the worker on a schedule, hands-free

Kaggle is the one free provider with **native notebook scheduling** — no
external service needed. This is how your farm wakes itself up.

## One-time setup (10 min)

### 1. Local: install the Kaggle CLI + credentials

```powershell
python -m pip install kaggle
```
- kaggle.com → your profile → **Settings → API → Create New Token**
- Save the downloaded `kaggle.json` to `C:\Users\<you>\.kaggle\kaggle.json`

### 2. Create the kernel folder

Make a folder `kaggle-worker/` with two files:

**`kernel-metadata.json`**
```json
{
  "id": "YOUR_KAGGLE_USERNAME/gpu-farm-worker",
  "title": "gpu-farm-worker",
  "code_file": "worker.ipynb",
  "language": "python",
  "kernel_type": "notebook",
  "is_private": true,
  "enable_gpu": true,
  "enable_tpu": false,
  "enable_internet": true,
  "dataset_sources": [],
  "competition_sources": [],
  "kernel_sources": []
}
```

**`worker.ipynb`** — a notebook whose single code cell is the entire
contents of `../worker/worker.py` with `MODE = "both"` and your
`BRAIN_URL` / `API_KEY` filled in. (Create it in the Kaggle web UI, or
convert locally: `jupyter nbconvert` — simplest is to make the notebook
on kaggle.com once, paste the worker, Save Version, then pull it.)

### 3. Push + verify

```powershell
cd kaggle-worker
kaggle kernels push
```
Check it ran green: `kaggle kernels status YOUR_USERNAME/gpu-farm-worker`

### 4. Schedule it

On the kernel's page on kaggle.com → **Schedule** → e.g. run every day at
08:00 (or a few times per day). Each scheduled run:

1. Starts, registers with the brain, polls the queue.
2. If there's work → renders videos / answers LLM jobs.
3. If the queue stays empty for 10 min → the worker exits on its own,
   so a scheduled wake with no work costs you ~2 minutes of quota.

Combined with the brain's reaper, this gives you a farm that wakes up,
works, and goes back to sleep **without you opening a browser**.

## Gotchas

- Kaggle free GPU quota: **30 h/week** (P100). A few short scheduled wakes
  per day barely dents it; heavy render days are what consume it.
- Scheduled runs need **Internet: On** in the kernel settings (to reach
  your brain) — already set via `enable_internet` above.
- Output videos land in `/kaggle/working/videos` = the kernel's output
  (downloadable from the kernel page). For auto-delivery to Drive, add
  the Drive-upload snippet to the worker (Google API OAuth) — or keep
  Colab as the video worker and use Kaggle for LLM/training jobs.
- Colab equivalent: Google Colab (paid tiers) has **"Schedule"** under
  the Share menu — same idea, one click, no API needed.
