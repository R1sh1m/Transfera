import os

log_dir = "scratch_logs"
if os.path.exists(log_dir):
    files = sorted(os.listdir(log_dir), key=lambda x: int(x.split("_")[1].split(".")[0]))
    for file in files:
        path = os.path.join(log_dir, file)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        if "Transfera" in content or "E_ABORT" in content or "Installation failed" in content or "80004004" in content:
            print(f"Match in {file}:")
            # print lines that have keywords
            for line in content.splitlines():
                if any(term in line for term in ["Transfera", "E_ABORT", "Installation failed", "80004004", "exit code"]):
                    print(f"  {line}")
