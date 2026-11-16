"""Bounded literal requests and readable chat replies for the Mattermost bridge."""

from __future__ import annotations

import json
import re
import unicodedata

MAX_POST_BYTES = 12 * 1024
MAX_FINAL_POSTS = 4
MAX_FINAL_TEXT_BYTES = 64 * 1024

_TEXT_PREFIX = "```text\n"
_JSON_PREFIX = "```json\n"
_SUFFIX = "\n```"
_TEXT_LIMIT = MAX_POST_BYTES - len(_TEXT_PREFIX) - len(_SUFFIX)
_SHORTENED = "\n\nReply shortened. Open VIKI Agent desktop for the complete reply."
_ESCAPES = {char: f"\\u{ord(char):04x}" for char in "`@<>&"}
_JSON_ESCAPES = str.maketrans(_ESCAPES)
_CHAT_ESCAPES = {char: f"\\u{ord(char):04x}" for char in "\\`@<>&~[]"}
_INLINE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_FENCE_OPEN = re.compile(r"^(`{3,8})([A-Za-z0-9_-]{0,32})[ ]{0,32}(?:\n|$)")
_FENCE_CLOSE = re.compile(r"^(`{3,8})[ ]{0,32}(?:\n|$)")


def _json_budget(value: object, remaining: int) -> int:
    if type(value) not in (str, int, float, bool, list, dict, type(None)):
        raise TypeError
    if isinstance(value, (list, dict)):
        remaining -= 2 + max(0, len(value) - 1)
        if isinstance(value, dict):
            remaining -= len(value)
    elif isinstance(value, str):
        if len(value) + 2 > remaining:
            raise OverflowError
        remaining -= len(value.encode("utf-8")) + 2
    else:
        remaining -= len(json.dumps(value, allow_nan=False))
    if remaining < 0:
        raise OverflowError
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError
            remaining = _json_budget(key, remaining)
            remaining = _json_budget(item, remaining)
    elif isinstance(value, list):
        for item in value:
            remaining = _json_budget(item, remaining)
    return remaining


def _fenced(body: str, prefix: str = _TEXT_PREFIX) -> str:
    post = prefix + body + _SUFFIX
    if len(post.encode("utf-8")) > MAX_POST_BYTES:
        raise ValueError("mattermost_content_too_large")
    return post


def _escape(char: str) -> str:
    code = ord(char)
    if 0xD800 <= code <= 0xDFFF:
        raise ValueError("mattermost_invalid_content")
    if char in _ESCAPES:
        return _ESCAPES[char]
    # Newlines retain paragraph layout; other controls are visible escape sequences.
    if char != "\n" and unicodedata.category(char) in {"Cc", "Cf"}:
        return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"
    return char


def literal_json(value: object) -> str:
    """Render a complete JSON value, or refuse an invalid or oversized request."""
    try:
        _json_budget(value, MAX_POST_BYTES - len(_JSON_PREFIX) - len(_SUFFIX))
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except OverflowError:
        raise ValueError("mattermost_content_too_large") from None
    except TypeError, ValueError, UnicodeError, RecursionError:
        raise ValueError("mattermost_invalid_content") from None
    return _fenced(encoded.translate(_JSON_ESCAPES), _JSON_PREFIX)


def literal_text(text: str) -> str:
    """Render complete readable text, without shortening a request."""
    if not isinstance(text, str):
        raise ValueError("mattermost_invalid_content")
    parts: list[str] = []
    size = 0
    for char in text:
        escaped = _escape(char)
        size += len(escaped.encode("utf-8"))
        if size > _TEXT_LIMIT:
            raise ValueError("mattermost_content_too_large")
        parts.append(escaped)
    return _fenced("".join(parts))


def _chat_escape(char: str) -> str:
    code = ord(char)
    if 0xD800 <= code <= 0xDFFF:
        raise ValueError("mattermost_invalid_content")
    if char in _CHAT_ESCAPES:
        return _CHAT_ESCAPES[char]
    if char != "\n" and unicodedata.category(char) in {"Cc", "Cf"}:
        return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"
    return char


def _chat_fragments(text: str) -> list[tuple[str, int]]:
    """Return bounded Markdown pieces; fence transitions are +1/-1."""
    pieces: list[tuple[str, int]] = []
    split = text.split("\n")
    lines = [part + ("\n" if index < len(split) - 1 else "") for index, part in enumerate(split)]
    later_closes = [0] * len(lines)
    next_close = 0
    for index in range(len(lines) - 1, -1, -1):
        later_closes[index] = next_close
        match = _FENCE_CLOSE.fullmatch(lines[index])
        if match:
            next_close = max(next_close, len(match.group(1)))
    fence_width = 0
    for index, line in enumerate(lines):
        if fence_width:
            closer = _FENCE_CLOSE.fullmatch(line)
            if closer and len(closer.group(1)) >= fence_width:
                pieces.append((line, -1))
                fence_width = 0
            else:
                pieces.extend((_escape(char), 0) for char in line)
            continue
        opener = _FENCE_OPEN.fullmatch(line)
        if opener and later_closes[index] >= len(opener.group(1)):
            pieces.append((line, 1))
            fence_width = len(opener.group(1))
            continue
        pieces.extend(_prose_fragments(line))
    return pieces


def _prose_fragments(text: str) -> list[tuple[str, int]]:
    pieces: list[tuple[str, int]] = []
    words = iter(
        (match.start(), match.end(), _url_like(match.group()))
        for match in re.finditer(r"\S+", text)
    )
    word = next(words, None)
    cursor = 0
    for match in _INLINE.finditer(text):
        while word is not None and word[1] <= match.start():
            word = next(words, None)
        if word is not None and word[2] and word[0] < match.start():
            continue
        pieces.extend(_unsafe_prose_fragments(text[cursor : match.start()]))
        code = "".join(_escape(char) for char in match.group(1))
        for start in range(0, len(code), 1024):
            pieces.append((f"`{code[start : start + 1024]}`", 0))
        cursor = match.end()
    pieces.extend(_unsafe_prose_fragments(text[cursor:]))
    return pieces


def _unsafe_prose_fragments(text: str) -> list[tuple[str, int]]:
    pieces: list[tuple[str, int]] = []
    for match in re.finditer(r"\S+|\s+", text):
        token = match.group()
        if _url_like(token):
            # Each wrapper is complete, including for a token longer than one post.
            for start in range(0, len(token), 1024):
                part = "".join(_chat_escape(char) for char in token[start : start + 1024])
                pieces.append((f"`{part}`", 0))
                if start + 1024 < len(token):
                    pieces.append((" ", 0))
        else:
            pieces.extend((_chat_escape(char), 0) for char in token)
    return pieces


def _url_like(token: str) -> bool:
    if "://" in token or "www." in token.lower() or token.count(":") >= 2:
        return True
    dots = 0
    for index, char in enumerate(token):
        if char == ".":
            dots += 1
            if (
                0 < index < len(token) - 2
                and token[index - 1].isalnum()
                and token[index + 1].isalnum()
                and token[index + 2].isalnum()
            ):
                return True
        elif (
            char == "@"
            and 0 < index < len(token) - 1
            and token[index - 1].isalnum()
            and token[index + 1].isalnum()
        ):
            return True
        elif char == ":" and index + 1 < len(token):
            start = index - 1
            while (
                start >= max(0, index - 32)
                and token[start]
                in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-"
            ):
                start -= 1
            scheme = token[start + 1 : index]
            if scheme and scheme[0].isascii() and scheme[0].isalpha():
                return True
    return dots >= 3 and any(char.isdigit() for char in token)


def chat_text(text: str) -> str:
    """Render one safe, unfenced Mattermost chat post or refuse oversized content."""
    if not isinstance(text, str):
        raise ValueError("mattermost_invalid_content")
    if len(text) > MAX_POST_BYTES:
        raise ValueError("mattermost_content_too_large")
    parts: list[str] = []
    size = 0
    for piece, _ in _chat_fragments(text):
        size += len(piece.encode("utf-8"))
        if size > MAX_POST_BYTES:
            raise ValueError("mattermost_content_too_large")
        parts.append(piece)
    return "".join(parts)


def _pack_chat(pieces: list[tuple[str, int]], last_limit: int) -> tuple[list[str], bool]:
    posts: list[str] = []
    parts: list[str] = []
    size = 0
    opener = ""
    for piece, transition in pieces:
        piece_size = len(piece.encode("utf-8"))
        fence = piece if transition == 1 else opener
        closure = "\n" + "`" * (len(fence) - len(fence.lstrip("`"))) if fence else ""
        closing = len(closure) if transition != -1 else 0
        limit = last_limit if len(posts) == MAX_FINAL_POSTS - 1 else MAX_POST_BYTES
        if size + piece_size + closing > limit:
            if len(posts) == MAX_FINAL_POSTS - 1:
                if opener:
                    parts.append(closure)
                posts.append("".join(parts))
                return posts, True
            if opener:
                parts.append(closure)
            posts.append("".join(parts))
            parts = [opener] if opener else []
            size = len(opener.encode("utf-8"))
            limit = last_limit if len(posts) == MAX_FINAL_POSTS - 1 else MAX_POST_BYTES
            if size + piece_size + closing > limit:
                raise ValueError("mattermost_content_too_large")
        parts.append(piece)
        size += piece_size
        if transition == 1:
            opener = piece
        elif transition == -1:
            opener = ""
    posts.append("".join(parts))
    return posts, False


def final_posts(text: str) -> list[str]:
    """Split final assistant text into safe chat posts, labelling omitted content."""
    if not isinstance(text, str):
        raise ValueError("mattermost_invalid_content")
    source_size = 0
    source: list[str] = []
    source_shortened = False
    for char in text:
        if 0xD800 <= ord(char) <= 0xDFFF:
            raise ValueError("mattermost_invalid_content")
        size = len(char.encode("utf-8"))
        if source_size + size > MAX_FINAL_TEXT_BYTES:
            source_shortened = True
            break
        source.append(char)
        source_size += size
    pieces = _chat_fragments("".join(source))
    marker_size = len(_SHORTENED.encode("utf-8"))
    posts, shortened = _pack_chat(pieces, MAX_POST_BYTES)
    if shortened or source_shortened:
        posts, _ = _pack_chat(pieces, MAX_POST_BYTES - marker_size)
        posts[-1] += _SHORTENED
    return posts
