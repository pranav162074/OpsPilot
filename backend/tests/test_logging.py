import json
import logging

from opspilot.logging import JsonFormatter, request_id_var


def test_logs_are_json_with_the_request_id() -> None:
    record = logging.LogRecord("opspilot", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    token = request_id_var.set("req-1")
    try:
        line = JsonFormatter().format(record)
    finally:
        request_id_var.reset(token)

    entry = json.loads(line)
    assert entry["message"] == "hello world"
    assert entry["level"] == "INFO"
    assert entry["request_id"] == "req-1"
