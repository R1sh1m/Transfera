import urllib.request
import json
import os

os.makedirs("scratch_logs", exist_ok=True)

# URL template:
# https://dev.azure.com/shine-oss/8b78618a-7973-49d8-9174-4360829d979b/_apis/build/builds/354511/logs/{log_id}

for log_id in range(1, 71):
    url = f"https://dev.azure.com/shine-oss/8b78618a-7973-49d8-9174-4360829d979b/_apis/build/builds/354511/logs/{log_id}"
    log_path = f"scratch_logs/log_{log_id}.txt"
    try:
        urllib.request.urlretrieve(url, log_path)
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        
        # Search for key terms
        if "Transfera" in content or "E_ABORT" in content or "Installation failed" in content or "80004004" in content:
            print(f"Match found in log {log_id}!")
            # Print the first few matching lines
            for line in content.splitlines():
                if any(term in line for term in ["Transfera", "E_ABORT", "Installation failed", "80004004", "exit code"]):
                    print(f"  {line}")
    except Exception as e:
        print(f"Error fetching log {log_id}: {e}")
