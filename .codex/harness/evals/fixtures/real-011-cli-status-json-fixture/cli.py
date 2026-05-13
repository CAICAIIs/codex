import json


def status_json() -> str:
    return json.dumps({"task_count": 2})
