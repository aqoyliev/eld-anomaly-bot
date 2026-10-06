from typing import Iterable, List

from aiogram import types

# Telegram rejects messages longer than 4096 characters (MessageIsTooLong).
# Keep a margin: the limit applies after HTML entity parsing, but we measure
# the raw HTML, which is always at least as long, so this errs on the safe side.
TELEGRAM_LIMIT = 4000


def split_chunks(parts: Iterable[str], sep: str = "\n", limit: int = TELEGRAM_LIMIT) -> List[str]:
    """Pack ``parts`` (joined by ``sep``) into as few messages as possible,
    never splitting a single part. A part that alone exceeds ``limit`` is split
    on line boundaries so no HTML tag is cut in half."""
    chunks: List[str] = []
    current = ""
    for part in parts:
        pieces = [part] if len(part) <= limit else _split_lines(part, limit)
        for piece in pieces:
            candidate = f"{current}{sep}{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def _split_lines(text: str, limit: int) -> List[str]:
    out: List[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                out.append(current)
            current = line[:limit]
    if current:
        out.append(current)
    return out


async def answer_long(message: types.Message, parts: Iterable[str], sep: str = "\n") -> None:
    """Send ``sep.join(parts)``, split across several messages when it would
    exceed Telegram's length limit."""
    for chunk in split_chunks(parts, sep):
        await message.answer(chunk)
