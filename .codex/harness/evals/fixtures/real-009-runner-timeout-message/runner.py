def timeout_result(timeout_seconds: int) -> dict:
    return {
        "exit_code": 124,
        "stderr": "command timed out",
    }
