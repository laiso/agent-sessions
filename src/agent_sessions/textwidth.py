import unicodedata


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def truncate(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    used = 0
    for index, char in enumerate(text):
        used += display_width(char)
        if used > width - 1:
            return text[:index] + "…"
    return text
