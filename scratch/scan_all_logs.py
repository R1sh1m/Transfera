import os

log_dir = "scratch_logs"
files = sorted(os.listdir(log_dir), key=lambda x: int(x.split("_")[1].split(".")[0]))

with open("scratch_logs_summary.txt", "w", encoding="utf-8") as out:
    for file in files:
        path = os.path.join(log_dir, file)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [f.readline().strip() for _ in range(5)]
        out.write(f"{file}: {' | '.join([l for l in lines if l])}\n")
print("Done writing summary!")
