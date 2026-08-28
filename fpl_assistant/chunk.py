"""Split article text into overlapping chunks for search and summarisation."""
from __future__ import annotations

import re


def chunk_text(text: str, target: int = 1200, overlap: int = 200) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n{2,}|(?<=[.!?])\s{2,}", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        if len(buffer) + len(para) + 1 <= target:
            buffer = (buffer + " " + para).strip()
            continue
        if buffer:
            chunks.append(buffer)
        if len(para) <= target:
            buffer = para
        else:
            step = max(1, target - overlap)
            for i in range(0, len(para), step):
                chunks.append(para[i:i + target])
            buffer = ""
    if buffer:
        chunks.append(buffer)
    return chunks
