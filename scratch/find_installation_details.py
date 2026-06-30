import os

log_dir = "scratch_logs"
files = sorted(os.listdir(log_dir), key=lambda x: int(x.split("_")[1].split(".")[0]))

for file in files:
    path = os.path.join(log_dir, file)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    
    # Let's search for the word "Exception", "failed", "error", or "exit code" in relation to winget setup
    if "Transfera-Setup" in content:
        print(f"File {file} contains Transfera-Setup. Length: {len(content)}")
        # Print lines that mention it
        for line in content.splitlines():
            if "Transfera-Setup" in line or "failed" in line.lower() or "error" in line.lower() or "exit" in line.lower() or "abort" in line.lower():
                print(f"  {line[:150]}")
