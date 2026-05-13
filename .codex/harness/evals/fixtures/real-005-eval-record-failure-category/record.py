def build_record(status: str, failure_category: str | None) -> dict:
    return {
        "status": status,
        "metrics": {
            "failure_category": None,
        },
    }
