import redis

URL = "rediss://default:gQAAAAAABFyLAAIgcDEzMzNkMzc1MDc5MDY0ZjU0YjliMWRiNzczNDQ5YzNhZA@true-dodo-285835.upstash.io:6379"

try:
    r = redis.Redis.from_url(URL, decode_responses=True)
    print("ping:", r.ping())
    r.set("gpu-farm-test", "hello")
    print("set/get:", r.get("gpu-farm-test"))
    r.delete("gpu-farm-test")
    print("REDIS OK")
except Exception as e:
    print("REDIS FAILED:", type(e).__name__, e)
