import logging
import os
from logging.handlers import RotatingFileHandler


class _TzFormatter(logging.Formatter):
    """ISO-8601 timestamps with milliseconds and the local UTC offset."""

    def formatTime(self, record, datefmt=None):  # noqa: N802 (logging API)
        import datetime as _dt

        ts = _dt.datetime.fromtimestamp(record.created).astimezone()
        return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}" + ts.strftime("%z")


def setup_logging(log_file: str, level: int = logging.INFO) -> logging.Logger:
    fmt = _TzFormatter("%(asctime)s %(levelname)-7s %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # requests/urllib3 are chatty at DEBUG; keep them quiet.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return logging.getLogger("resy_sniper")
