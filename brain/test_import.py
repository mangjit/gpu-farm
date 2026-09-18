"""Import-time validation of app.py with a mocked Redis backend."""
import os
import sys
import types

os.environ["REDIS_URL"] = "rediss://fake:6379"


class FakeRedis:
    """Minimal in-memory stand-in for the redis-py client API we use."""

    def __init__(self):
        self.kv = {}
        self.lists = {}
        self.hashes = {}
        self.sets = {}

    @staticmethod
    def from_url(*a, **k):
        return FakeRedis()

    # strings
    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v):
        self.kv[k] = v

    def exists(self, k):
        return k in self.kv or k in self.hashes

    # lists
    def rpush(self, k, v):
        self.lists.setdefault(k, []).append(v)

    def lpush(self, k, v):
        self.lists.setdefault(k, []).insert(0, v)

    def ltrim(self, k, a, b):
        self.lists[k] = self.lists.get(k, [])[a:b + 1]

    def lrange(self, k, a, b):
        lst = self.lists.get(k, [])
        return lst[a:] if b == -1 else lst[a:b + 1]

    def lrem(self, k, n, v):
        if k in self.lists and v in self.lists[k]:
            self.lists[k].remove(v)

    def llen(self, k):
        return len(self.lists.get(k, []))

    # hashes
    def hset(self, k, mapping=None, **kw):
        self.hashes.setdefault(k, {}).update(mapping or kw)

    def hgetall(self, k):
        return self.hashes.get(k, {})

    def hincrby(self, k, f, n=1):
        h = self.hashes.setdefault(k, {})
        h[f] = str(int(h.get(f, "0")) + n)
        return int(h[f])

    # sets
    def sadd(self, k, v):
        self.sets.setdefault(k, set()).add(v)

    def smembers(self, k):
        return self.sets.get(k, set())


fake = FakeRedis()
redis_mod = types.ModuleType("redis")
redis_mod.Redis = FakeRedis
sys.modules["redis"] = redis_mod

import app  # noqa: E402

print("IMPORT_OK")
paths = sorted({r.path for r in app.app.routes if hasattr(r, "path")})
print("routes:", paths)

# exercise the error classifier (the self-improving brain)
for tb, expect in [
    ("torch.cuda.OutOfMemoryError: CUDA out of memory", "oom"),
    ("requests.exceptions.ConnectionError: 503 timeout", "transient"),
    ("FileNotFoundError: no such file", "missing_file"),
    ("403 Forbidden: unauthorized", "auth"),
    ("something completely weird", "unknown"),
]:
    got = app.classify_error(tb)
    status = "OK" if got["cat"] == expect else "MISMATCH"
    print(f"classifier[{status}]: {expect} -> {got['cat']} retry={got['retry']}")

# simulate the full job lifecycle against the fake redis
jid = app.enqueue("video_draft", "a cat skateboarding", notify_chat="123")
job = app.get_job(jid)
assert job["status"] == "queued", job

reg = app.Register(worker_id="colab-t4-1", platform="colab", gpu="t4",
                   caps=["video_draft", "video_pro"])
out = app.next_job(reg)
assert out["job_id"] == jid, out
assert app.get_job(jid)["status"] == "running"

# a t4 must NOT be able to claim a video_pro job
jid2 = app.enqueue("video_pro", "epic shot")
out2 = app.next_job(app.Register(worker_id="colab-t4-1", platform="colab",
                                 gpu="t4", caps=["video_draft", "video_pro"]))
assert out2["job_id"] is None, "T4 wrongly claimed a pro job!"
print("routing check OK: t4 cannot claim video_pro")

# failure -> classification -> auto-requeue
f = app.Fail(job_id=jid, error="CUDA out of memory during attention")
res = app.fail(f)
assert res["diagnosis"]["cat"] == "oom"
assert app.get_job(jid)["status"] == "queued", "job should be requeued"
assert "oom" in app.r.hgetall(app.FIXES)
print("fail/requeue/known_fixes loop OK")

app.complete(app.Done(job_id=jid, result="/content/drive/MyDrive/videos/x.mp4"))
assert app.get_job(jid)["status"] == "done"
print("complete OK")

s = app._status_payload()
assert s["queue_length"] >= 0 and len(s["workers"]) >= 1
print("status payload OK — workers:", [w["id"] for w in s["workers"]])

# --- v2 features ---

# LLM job flow: enqueue llm, llm-capped worker claims it, answer delivered
j3 = app.enqueue("llm", "what is a transformer?", notify_chat="123")
video_worker = app.Register(worker_id="colab-t4-1", platform="colab",
                            gpu="t4", caps=["video_draft", "video_pro"])
assert app.next_job(video_worker)["job_id"] is None, \
    "video-only worker must not claim llm jobs"
llm_worker = app.Register(worker_id="kaggle-p100-llm", platform="kaggle",
                          gpu="p100", caps=["llm"])
out3 = app.next_job(llm_worker)
assert out3["job_id"] == j3 and out3["job"]["model"] == "qwen2.5:7b-instruct"
app.complete(app.Done(job_id=j3, result="A neural net architecture…"))
assert app.get_job(j3)["status"] == "done"
print("llm job flow OK")

# OOM auto-shrink: frames/resolution must decrease on each OOM retry
j4 = app.enqueue("video_draft", "big render", frames=100, width=700, height=470)
before = app.get_job(j4)
app.next_job(video_worker)
app.fail(app.Fail(job_id=j4, error="CUDA out of memory"))
after = app.get_job(j4)
assert after["status"] == "queued"
assert after["frames"] < before["frames"], "frames must shrink after OOM"
assert after["width"] < before["width"], "width must shrink after OOM"
print("oom auto-shrink OK: %df %dx%d -> %df %dx%d" % (
    before["frames"], before["width"], before["height"],
    after["frames"], after["width"], after["height"]))

# tunnel registry: live worker's tunnel is listed, dead worker's is not
app.set_tunnel(app.Tunnel(worker_id="kaggle-p100-llm",
                          url="https://abc.trycloudflare.com"))
tun = app.get_tunnel()
assert "kaggle-p100-llm" in tun["live_tunnels"]
app.r.hset(app.WORKER + "ghost", mapping={"last_seen": "0", "status": "x",
                                          "platform": "x", "gpu": "t4"})
app.r.sadd(app.WORKERS, "ghost")
app.set_tunnel(app.Tunnel(worker_id="ghost", url="https://dead.trycloudflare.com"))
tun2 = app.get_tunnel()
assert "ghost" not in tun2["live_tunnels"], "dead worker tunnel must be hidden"
print("tunnel registry OK:", list(tun2["live_tunnels"]))

# human_result formatting
msg = app.human_result(app.get_job(j3), "an answer")
assert msg.startswith("🧠")
print("human_result OK")

print("ALL TESTS PASSED")
