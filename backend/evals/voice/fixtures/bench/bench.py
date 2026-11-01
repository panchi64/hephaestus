import csv
import statistics
import time

started = time.perf_counter()
with open("data/latencies.csv", newline="") as f:
    rows = [float(row["ms"]) for row in csv.DictReader(f)]
print(f"rows={len(rows)} p50={statistics.median(rows):.1f}ms "
      f"p95={statistics.quantiles(rows, n=20)[18]:.1f}ms "
      f"took={time.perf_counter() - started:.3f}s")
