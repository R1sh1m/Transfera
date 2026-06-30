with open("backend/engines/organizer.py", "r", encoding="utf-8") as f:
    for i, line in enumerate(f, 1):
        if "def " in line:
            print(f"{i}: {line.strip()}")
