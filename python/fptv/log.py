import sys

try:
    from systemd import journal

    # Linux
    _HAS_JOURNAL = True
except ImportError:
    # Mac
    _HAS_JOURNAL = False


class Logger:
    def __init__(self, tag: str):
        if tag is None:
            raise ValueError("Missing required argument 'tag'")
        self.tag = tag

    def out(self, msg: str) -> None:
        if _HAS_JOURNAL:
            journal.send(f"{msg}", SYSLOG_IDENTIFIER=self.tag)
        else:
            print(f"[{self.tag}] {msg}")

    def err(self, msg: str) -> None:
        if _HAS_JOURNAL:
            journal.send(f"{msg}", PRIORITY=journal.LOG_ERR, SYSLOG_IDENTIFIER=self.tag)
        else:
            print(f"[{self.tag}] ERROR: {msg}", file=sys.stderr)
