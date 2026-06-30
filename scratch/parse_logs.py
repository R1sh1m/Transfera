import json

with open("logs_list.json", "r", encoding="utf-8") as f:
    data = json.load(f)

print(f"Total logs: {data['count']}")
for log in data['value']:
    print(f"ID: {log['id']}, LineCount: {log.get('lineCount')}, URL: {log['url']}")
