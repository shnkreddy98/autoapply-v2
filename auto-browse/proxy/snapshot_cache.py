_latest: str = ""


def set(raw: str) -> None:
    global _latest
    _latest = raw


def get() -> str:
    return _latest
