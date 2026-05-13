import json


def router_json() -> str:
    return json.dumps(
        {
            "routes": [
                {
                    "id": "simple-local-change",
                    "enabled": True,
                }
            ]
        }
    )
