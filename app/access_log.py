"""Uvicorn access-log redaction helpers."""

import logging


class QueryStringRedactionFilter(logging.Filter):
    """Remove query strings from Uvicorn access-log request targets."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True

        request_target = args[2]
        if isinstance(request_target, str):
            path, separator, _query = request_target.partition("?")
            if separator:
                record.args = (*args[:2], path, *args[3:])

        return True
