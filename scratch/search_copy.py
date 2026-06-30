with open("backend/engines/importer.py", "r", encoding="utf-8") as f:
    for i, line in enumerate(f, 1):
        if "def _copy_cache_to_dest" in line:
            print(f"{i}: {line.strip()}")
