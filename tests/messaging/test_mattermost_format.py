from __future__ import annotations

import json

import pytest

from viki_agent.messaging.mattermost_format import (
    MAX_FINAL_POSTS,
    MAX_FINAL_TEXT_BYTES,
    MAX_POST_BYTES,
    chat_text,
    final_posts,
    literal_json,
    literal_text,
)

SHORTENED = "\n\nReply shortened. Open VIKI Agent desktop for the complete reply."


def body(post: str, language: str = "text") -> str:
    post = post.removesuffix(SHORTENED)
    prefix, suffix = f"```{language}\n", "\n```"
    assert post.startswith(prefix) and post.endswith(suffix)
    result = post[len(prefix) : -len(suffix)]
    assert not any(char in result for char in "`@<>&")
    return result


def test_permission_literal_round_trips_without_markup() -> None:
    hostile = (
        "````\n@all @channel <script>&</script> "
        "![image](https://example.test/image) [link](https://example.test/) "
        "\x00\x1b\x7f\r\t\u0085\u202e\u2066\u200e\u03bb\U0001f9ed"
    )
    request = {"tool": "Bash`@<>", "input": {"command": hostile, "count": 2, "ok": True}}
    post = literal_json(request)
    encoded = body(post, "json")
    assert post.isascii()
    assert all(32 <= ord(char) < 127 for char in encoded)
    assert json.loads(encoded) == request
    assert "\\u0060" in encoded and "\\u0040" in encoded
    assert "\\u202e" in encoded and "\\ud83e\\udded" in encoded


@pytest.mark.parametrize("value", [None, False, 3, -0.0, [], {"nested": [1, "x", None]}])
def test_json_values_round_trip(value: object) -> None:
    assert json.loads(body(literal_json(value), "json")) == value


@pytest.mark.parametrize(
    "value", [object(), {1: "coerced"}, ("coerced",), {"set"}, float("nan"), float("inf")]
)
def test_invalid_json_refuses_without_echoing_content(value: object) -> None:
    with pytest.raises(ValueError, match="^mattermost_invalid_content$"):
        literal_json(value)


def test_recursive_json_refuses() -> None:
    value: list[object] = []
    value.append(value)
    with pytest.raises(ValueError, match="^mattermost_invalid_content$"):
        literal_json(value)


def test_complete_json_counts_fence_and_escaping_at_boundary() -> None:
    assert len(literal_json("a" * (MAX_POST_BYTES - 14)).encode()) == MAX_POST_BYTES
    with pytest.raises(ValueError, match="^mattermost_content_too_large$"):
        literal_json("a" * (MAX_POST_BYTES - 13))
    count = (MAX_POST_BYTES - 14) // 6
    assert len(literal_json("@" * count).encode()) <= MAX_POST_BYTES
    with pytest.raises(ValueError, match="^mattermost_content_too_large$"):
        literal_json("@" * (count + 1))


def test_literal_text_preserves_readable_unicode_and_inert_markup() -> None:
    text = "\u03bb\U0001f9ed\n```\n@all <img>& ![image](https://example.test/image)"
    encoded = body(literal_text(text))
    assert "\u03bb\U0001f9ed\n" in encoded
    assert "\\u0060\\u0060\\u0060\n\\u0040all" in encoded
    assert "\\u003cimg\\u003e\\u0026" in encoded


def test_literal_text_escapes_control_and_format_characters() -> None:
    text = "\x00\x1b\x7f\u0085\r\t\u202e\u2066\u200e\u200d\U000e0001"
    encoded = body(literal_text(text))
    assert encoded == (
        "\\u0000\\u001b\\u007f\\u0085\\u000d\\u0009\\u202e\\u2066\\u200e\\u200d\\U000e0001"
    )


@pytest.mark.parametrize("text", ["\ud800", "\udfff"])
def test_invalid_unicode_refuses(text: str) -> None:
    for format_request in (literal_json, literal_text, final_posts):
        with pytest.raises(ValueError, match="^mattermost_invalid_content$"):
            format_request(text)


@pytest.mark.parametrize("unit", ["a", "@", "\U0001f9ed"])
def test_complete_text_counts_final_bytes_at_boundary(unit: str) -> None:
    size = len(body(literal_text(unit)).encode())
    count = (MAX_POST_BYTES - 12) // size
    assert len(literal_text(unit * count).encode()) == MAX_POST_BYTES
    with pytest.raises(ValueError, match="^mattermost_content_too_large$"):
        literal_text(unit * (count + 1))


def test_chat_reply_keeps_native_layout_and_code() -> None:
    text = (
        "# Summary\n\nFirst **bold** paragraph.\n\n- one\n- two\n1. third\n\n"
        "Use `code`, `print(values[0])`, and `C:\\Work\\notes`.\n\n"
        "```python\nprint(values[0])\n```"
    )
    assert chat_text(text) == text
    assert final_posts(text) == [text]
    assert final_posts("") == [""]


def test_chat_reply_neutralizes_markup_and_url_variants() -> None:
    text = (
        "@all ~town <img>&amp; ![image](https://host.test/a) "
        "[link](https://host.test/) example.com 127.0.0.1 user@example.com "
        "mailto:user@example.com tel:+123 javascript:alert(1) www.host.test "
        "2001:db8::1 :// https://host.test/`x` and \\@all \u202e\x00"
    )
    encoded = chat_text(text)
    assert "@" not in encoded and "~" not in encoded and "<" not in encoded
    assert "&" not in encoded and "![" not in encoded and "][" not in encoded
    assert "[link]" not in encoded and "\\@" not in encoded
    for token in (
        "example.com",
        "127.0.0.1",
        "2001:db8::1",
        "mailto:",
        "tel:",
        "javascript:",
        "www.",
    ):
        assert f"`{token}" in encoded or f"{token}" in encoded.split("`")[1::2]
    assert "\\u202e" in encoded and "\\u0000" in encoded
    assert "\\u0060x\\u0060" in encoded
    assert "`://`" in encoded


@pytest.mark.parametrize("text", ["\ud800", "\udfff", 1])
def test_chat_invalid_content(text: object) -> None:
    for formatter in (chat_text, final_posts):
        with pytest.raises(ValueError, match="^mattermost_invalid_content$"):
            formatter(text)  # type: ignore[arg-type]


def test_chat_exact_encoded_limit() -> None:
    assert len(chat_text("a" * MAX_POST_BYTES).encode()) == MAX_POST_BYTES
    with pytest.raises(ValueError, match="^mattermost_content_too_large$"):
        chat_text("a" * (MAX_POST_BYTES + 1))
    assert len(chat_text("@" * (MAX_POST_BYTES // 6)).encode()) == MAX_POST_BYTES
    with pytest.raises(ValueError, match="^mattermost_content_too_large$"):
        chat_text("@" * (MAX_POST_BYTES // 6 + 1))


def test_final_posts_caps_and_shortens_with_marker_in_budget() -> None:
    exact = final_posts("a" * (MAX_POST_BYTES * MAX_FINAL_POSTS))
    assert len(exact) == MAX_FINAL_POSTS
    assert all(len(post.encode()) == MAX_POST_BYTES for post in exact)
    assert "".join(exact) == "a" * (MAX_POST_BYTES * MAX_FINAL_POSTS)
    shortened = final_posts("a" * (MAX_POST_BYTES * MAX_FINAL_POSTS + 1))
    assert len(shortened) == MAX_FINAL_POSTS
    assert all(len(post.encode()) <= MAX_POST_BYTES for post in shortened)
    assert shortened[-1].endswith(SHORTENED)
    assert MAX_FINAL_TEXT_BYTES == 64 * 1024


def test_final_posts_keep_protective_delimiters_together() -> None:
    posts = final_posts("a" * (MAX_POST_BYTES - 6) + " https://example.com/`x` " + "b" * 15000)
    assert all(len(post.encode()) <= MAX_POST_BYTES for post in posts)
    assert all(post.count("`") % 2 == 0 for post in posts)
    assert any("`https://example.com/\\u0060x\\u0060`" in post for post in posts)


def test_unmatched_source_tick_cannot_pair_with_url_wrapper() -> None:
    post = chat_text("Prefix ` Use https://example.test/path")
    assert "\\u0060 Use `https://example.test/path`" in post
    assert post.count("`") == 2


def test_large_url_token_uses_complete_wrappers_at_each_post_boundary() -> None:
    posts = final_posts("https://" + "a" * MAX_FINAL_TEXT_BYTES)
    assert len(posts) == MAX_FINAL_POSTS
    assert all(len(post.encode()) <= MAX_POST_BYTES and post.count("`") % 2 == 0 for post in posts)
    assert posts[-1].endswith(SHORTENED)


def test_long_punctuation_token_stays_bounded() -> None:
    posts = final_posts("a-" * 30000)
    assert len(posts) == MAX_FINAL_POSTS
    assert all(len(post.encode()) <= MAX_POST_BYTES for post in posts)
    assert posts[-1].endswith(SHORTENED)


def test_final_posts_keep_authored_fences_sane_across_posts() -> None:
    posts = final_posts("```python\n" + "a" * (MAX_POST_BYTES + 100) + "\n```")
    assert len(posts) == 2
    assert all(post.startswith("```python\n") and post.endswith("\n```") for post in posts)
    assert MAX_POST_BYTES == 12 * 1024
    assert MAX_FINAL_POSTS == 4


def test_four_tick_fence_keeps_width_across_post_boundary() -> None:
    posts = final_posts("````python\n" + "a" * MAX_POST_BYTES + "\n````")
    assert len(posts) == 2
    assert all(post.startswith("````python\n") and post.endswith("\n````") for post in posts)


def test_unmatched_many_fence_openers_remain_visible_escapes() -> None:
    post = chat_text("```python\n" * 300)
    assert "```" not in post
    assert post.count("\\u0060") == 900


def test_tabbed_fence_lines_escape_controls() -> None:
    post = chat_text("```python\t\ncode\n```\t")
    assert "\t" not in post
    assert post.count("\\u0009") == 2
    assert "```" not in post
