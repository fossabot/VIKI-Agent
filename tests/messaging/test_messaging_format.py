"""Provider caps preserve complete escaped requests across independent literal posts."""

from __future__ import annotations

import json
import re
import tracemalloc

import pytest

from viki_agent.messaging.mattermost_format import (
    chat_text,
    final_posts,
    literal_json,
    literal_text,
)
from viki_agent.messaging.messaging_channel import Provider


def content(post: str) -> str:
    post = re.sub(r"^Part [0-9]+ of [0-9]+\n", "", post)
    assert post.startswith(("```text\n", "```json\n")) and post.endswith("\n```")
    return post[8:-4]


@pytest.mark.parametrize(("provider", "limit"), [("slack", 4000), ("discord", 2000)])
def test_complete_request_survives_expansion(provider: Provider, limit: int) -> None:
    from viki_agent.messaging.messaging_format import render

    request = {"tool": "Edit", "input": {"hidden": "@<>&`\n" * 350}}
    formatted = literal_json(request)
    parts = render(provider, formatted)
    assert len(parts) > 1
    assert json.loads("".join(content(part) for part in parts)) == request
    for index, part in enumerate(parts, 1):
        assert part.startswith(f"Part {index} of {len(parts)}\n")
        assert len(part.encode("utf-16-le")) // 2 <= limit
        assert not any(char in content(part) for char in "@<>&`")


@pytest.mark.parametrize("provider", ["slack", "discord"])
def test_astral_characters_and_json_surrogate_pairs_remain_whole(provider: Provider) -> None:
    from viki_agent.messaging.messaging_format import render

    text = "\U0001f9ed" * 1500
    assert "".join(content(part) for part in render(provider, literal_text(text))) == text
    request = {"value": "\U0001f9ed" * 800}
    parts = render(provider, literal_json(request))
    assert json.loads("".join(content(part) for part in parts)) == request
    for part in parts:
        body = content(part).removeprefix('{"value":"').removesuffix('"}')
        assert re.fullmatch(r"(?:\\ud83e\\udded)*", body) is not None
        assert len(part) <= (4000 if provider == "slack" else 2000)


def test_discord_counts_utf16_units_including_fences() -> None:
    from viki_agent.messaging.messaging_format import render

    parts = render("discord", literal_text("\U0001f9ed" * 1500))
    assert len(parts) > 1
    assert all(len(part.encode("utf-16-le")) // 2 <= 2000 for part in parts)


def test_mattermost_keeps_existing_bytes_and_shortened_reply_marker() -> None:
    from viki_agent.messaging.messaging_format import render

    for formatted in [literal_json({"hidden": "@all"}), *final_posts("x" * 65537)]:
        assert render("mattermost", formatted) == (formatted,)
        parts = render("discord", formatted)
        joined = (
            "".join(content(part) for part in parts)
            if formatted.startswith("```")
            else "".join(parts)
        )
        if "Reply shortened." in formatted:
            assert "Reply shortened. Open VIKI Agent desktop for the complete reply." in joined


@pytest.mark.parametrize(
    "formatted",
    [
        "@all",
        "<@owner>",
        "https://example.test",
        "bad\tcontrol",
        "bad\\xescape",
        "```text\n@all\n```",
        "\ud800",
    ],
)
def test_unsupported_literal_grammar_refuses_without_echo(formatted: str) -> None:
    from viki_agent.messaging.messaging_format import render

    with pytest.raises(ValueError, match="^messaging_render_invalid$") as error:
        render("discord", formatted)
    assert formatted not in repr(error.value)


@pytest.mark.parametrize("provider", ["slack", "discord"])
def test_native_chat_preserves_paragraphs_lists_and_code(provider: Provider) -> None:
    from viki_agent.messaging.messaging_format import render

    source = "# Heading\n\n- **Bold** and *italic*\n\n```python\nprint('ready')\n```\n"
    parts = render(provider, chat_text(source))
    assert len(parts) == 1
    post = parts[0]
    assert not post.startswith("```text")
    assert "\n\n- " in post
    if provider == "slack":
        assert post.startswith("*Heading*\n")
        assert "*Bold* and _italic_" in post
        assert "```\nprint('ready')\n```" in post
    else:
        assert post == source


@pytest.mark.parametrize("provider", ["slack", "discord"])
def test_long_code_and_protective_url_wrappers_close_per_part(provider: Provider) -> None:
    from viki_agent.messaging.messaging_format import render

    source = "```python\n" + "x" * 6500 + "\n```\n" + "https://example.test/" + "z" * 3000
    parts = render(provider, chat_text(source))
    assert len(parts) > 2
    assert len(parts) <= 32
    limit = 4000 if provider == "slack" else 2000
    for part in parts:
        assert len(part.encode("utf-16-le")) // 2 <= limit
        assert part.count("```") % 2 == 0
        assert "https://" not in part or "`https://" in part


@pytest.mark.parametrize("provider", ["slack", "discord"])
def test_astral_and_escape_atoms_stay_intact_in_chat(provider: Provider) -> None:
    from viki_agent.messaging.messaging_format import render

    formatted = chat_text("x" * 3800 + "\U0001f9ed" * 400 + " @<>&\\")
    parts = render(provider, formatted)
    assert len(parts) > 1
    assert "".join(re.sub(r"^Part \d+ of \d+\n", "", p) for p in parts) == formatted
    assert all(
        (len(p) if provider == "slack" else len(p.encode("utf-16-le")) // 2)
        <= (4000 if provider == "slack" else 2000)
        for p in parts
    )
    assert all(re.search(r"\\(?:u[0-9a-fA-F]{0,3}|U[0-9a-fA-F]{0,7})$", p) is None for p in parts)


@pytest.mark.parametrize("provider", ["slack", "discord"])
def test_split_protective_code_never_exposes_url_or_mention(provider: Provider) -> None:
    from viki_agent.messaging.messaging_format import render

    parts = render(provider, chat_text("https://example.test/" + "@" * 1024))
    assert len(parts) > 1
    assert all("@" not in part and part.count("`") % 2 == 0 for part in parts)
    assert all(
        (len(part) if provider == "slack" else len(part.encode("utf-16-le")) // 2)
        <= (4000 if provider == "slack" else 2000)
        for part in parts
    )


@pytest.mark.parametrize("provider", ["slack", "discord"])
def test_long_authored_fence_uses_native_closures(provider: Provider) -> None:
    from viki_agent.messaging.messaging_format import render

    parts = render(provider, chat_text("````python\n" + "x" * 5000 + "\n````"))
    assert len(parts) > 1
    for part in parts:
        if provider == "slack":
            assert part.count("```") == 2 and "````" not in part
        else:
            assert part.count("````") == 2


@pytest.mark.parametrize("provider", ["slack", "discord"])
def test_long_authored_inline_code_keeps_all_content_inside_code(provider: Provider) -> None:
    from viki_agent.messaging.messaging_format import render

    source = "`" + "x" * 4100 + "\U0001f9ed C:\\notes\\file https://example.test/path" + "`"
    formatted = chat_text(source)
    authored = "".join(re.findall(r"`([^`\n]+)`", formatted))
    assert "``" in formatted and len(authored) > 4100
    parts = render(provider, formatted)
    assert len(parts) > 1
    assert "".join(re.findall(r"`([^`\n]+)`", part)[0] for part in parts) == authored
    assert all(part.count("`") == 2 for part in parts)
    assert all(
        (len(part) if provider == "slack" else len(part.encode("utf-16-le")) // 2)
        <= (4000 if provider == "slack" else 2000)
        for part in parts
    )


@pytest.mark.parametrize("provider", ["slack", "discord"])
def test_short_authored_code_followed_by_url_stays_inert(provider: Provider) -> None:
    from viki_agent.messaging.messaging_format import render

    formatted = chat_text("`a`https://example.invalid")
    assert formatted == "`a``https://example.invalid`"
    assert render(provider, formatted) == ("`ahttps://example.invalid`",)


@pytest.mark.parametrize("provider", ["slack", "discord"])
def test_adjacent_short_authored_spans_stay_code(provider: Provider) -> None:
    from viki_agent.messaging.messaging_format import render

    assert render(provider, "`one``two`") == ("`onetwo`",)


@pytest.mark.parametrize("provider", ["slack", "discord"])
@pytest.mark.parametrize("source", ["@:/", "[:/", "]:/", "~:/", "`:/", "\\:/", "<:/", ">:/", "&:/"])
def test_visible_escape_cannot_be_mistaken_for_url_scheme(provider: Provider, source: str) -> None:
    from viki_agent.messaging.messaging_format import render

    formatted = chat_text(source)
    assert formatted.startswith("\\u")
    assert render(provider, formatted) == (formatted,)


@pytest.mark.parametrize("provider", ["slack", "discord"])
@pytest.mark.parametrize("raw_url", ["https://example.invalid", "www.example.invalid"])
def test_real_url_adjacent_to_visible_escape_is_still_refused(
    provider: Provider, raw_url: str
) -> None:
    from viki_agent.messaging.messaging_format import render

    with pytest.raises(ValueError, match="^messaging_render_invalid$"):
        render(provider, "\\u0040" + raw_url)


@pytest.mark.parametrize("provider", ["slack", "discord"])
@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_unicode_separators_remain_inside_authored_code(provider: Provider, separator: str) -> None:
    from viki_agent.messaging.messaging_format import render

    inline = chat_text(f"`a{separator}b`")
    assert render(provider, inline) == (inline,)
    fenced = chat_text(f"```python\na{separator}b\n```")
    expected = f"```{'python' if provider == 'discord' else ''}\na{separator}b\n```"
    assert render(provider, fenced) == (expected,)
    prose = chat_text(f"a{separator}b")
    assert render(provider, prose) == (prose,)


@pytest.mark.parametrize("provider", ["slack", "discord"])
def test_blank_chat_refused_before_send(provider: Provider) -> None:
    from viki_agent.messaging.messaging_format import render

    with pytest.raises(ValueError, match="^messaging_render_invalid$"):
        render(provider, " \n ")


def test_source_bound_refuses_before_large_temporary_expansion() -> None:
    from viki_agent.messaging.messaging_format import render

    formatted = "```text\n" + "x" * (2 << 20) + "\n```"
    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="^messaging_render_invalid$"):
            render("slack", formatted)
        assert tracemalloc.get_traced_memory()[1] < 64 << 10
    finally:
        tracemalloc.stop()
