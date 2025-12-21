import sys


class Logger:
    def __init__(self, tag: str):
        if tag is None:
            raise ValueError("Missing required argument 'tag'")

        self.tag = tag

    def out(self, msg: str) -> None:
        print(f"[{self.tag}] {msg}")

    def err(self, msg: str) -> None:
        print(f"[{self.tag}] ERROR: {msg}", file=sys.stderr)
