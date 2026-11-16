"""Pins for the Linux check images and for the root/user split of their runner.

Dockerfile: every image (FROM, COPY or ADD `--from=`, RUN `--mount=…,from=`) is pinned by digest;
every download (curl, wget, a remote ADD) is checked by `sha256sum -c` against a SHA-256 written
in the Dockerfile; nothing installs through pip, a global npm install or npx; each apt-get stage
pins its own archive snapshot before packages install - an Ubuntu stage by `APT::Snapshot`, a
Debian stage by snapshot.debian.org at a fixed time (bookworm's apt, 2.6.1, ignores
`APT::Snapshot`); uv installs an exact Python patch version; Node's tarball is unpacked without
its archive owner; and main's and oldgit's builds fail when uid 1000 could change anything under
/usr/local or /opt.

run.sh: root never chowns or chmods /out or Electron's chrome-sandbox, sets no setuid bit, aims its
own redirects only at /out, copies the user's reports into /out only by reading them as the user,
and runs node, npm, uv and xvfb-run only as the user. Both files are plain LF text.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LINUX_DIR = REPO / "scripts" / "linux"
DOCKERFILE = LINUX_DIR / "Dockerfile"
RUN_SH = LINUX_DIR / "run.sh"
GITATTRIBUTES = REPO / ".gitattributes"

_PINNED_RE = re.compile(r"@sha256:[0-9a-fA-F]{64}$")
_SNAPSHOT_RE = re.compile(r'APT::Snapshot\s+"(\d{8}T\d{6}Z)"')
_DEBIAN_SNAPSHOT_RE = re.compile(r"snapshot\.debian\.org/archive/\S+/\d{8}T\d{6}Z")
_UBUNTU_MARKERS = ("ubuntu", "noble", "jammy", "focal", "bionic")
_APT_INSTALL_RE = re.compile(r"apt-get install")
_UV_PYTHON_RE = re.compile(r"uv python install (\d+\.\d+\.\d+)")
_CFT_URL_RE = re.compile(
    r"chrome-for-testing-public/(\d+\.\d+\.\d+\.\d+)/linux64/chrome-linux64\.zip"
)
_FETCHER_RE = re.compile(r"(?<![\w.-])(?:curl|wget)(?![\w.-])")
_CHECKSUM_LINE_RE = re.compile(r"[0-9a-fA-F]{64} [ *](\S+)")  # sha256sum's "<hash>  <file>"
_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})
_INSTALLER_RES = (
    re.compile(r"(?<![\w.-])pip[\d.]*\s+install(?![\w-])"),  # pip, pip3, python -m pip, uv pip
    re.compile(r"(?<![\w./-])npm(?![\w.-])[^&|;]*?\s(?:-g|--global|--location[= ]global)\b"),
    re.compile(r"(?<![\w./-])npx(?![\w.-])"),
)
_OWNERSHIP_CHECK_RE = re.compile(
    r"find /usr/local /opt .*! -user root .*! -group root .*! -type l -perm -0002"
)
_USER_TOOL_RE = re.compile(r"(?<![\w./-])(?:node|npm|npx|uv|uvx|xvfb-run)(?![\w./-])")
_CHANGE_OWNER_OR_MODE_RE = re.compile(r"(?<![\w./-])(?:chown|chmod)(?![\w.-])[^\n;&|]*")
_SETUID_MODE_RE = re.compile(r"\s(?:[0-7]?[2-7][0-7]{3}|[ugoa]*[+=][rwxXt]*s)")
_REDIRECT_RE = re.compile(r"\d*>>?(&?)\s*(\"[^\"]*\"|[^\s;&|)]+)")


def _instructions(text: str) -> list[tuple[str, str]]:
    """(KEYWORD, arguments) per Dockerfile instruction, in order: comment and blank lines dropped
    (Docker skips them inside a continuation too), backslash continuations joined, keywords read
    in any case, and ONBUILD's wrapped instruction read as its own."""
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
        else:
            lines.append(pending + line)
            pending = ""
    if pending:
        lines.append(pending)
    instructions: list[tuple[str, str]] = []
    for line in lines:
        keyword, args = _split_keyword(line)
        if keyword.upper() == "ONBUILD":
            keyword, args = _split_keyword(args)
        instructions.append((keyword.upper(), args))
    return instructions


def _split_keyword(line: str) -> tuple[str, str]:
    parts = line.split(maxsplit=1)
    return (parts[0], parts[1].strip()) if len(parts) == 2 else (line.strip(), "")


def _flags(args: str) -> list[str]:
    """An instruction's leading `--flag` words, which come before its operands."""
    flags: list[str] = []
    for word in args.split():
        if not word.startswith("--"):
            break
        flags.append(word)
    return flags


def _operands(args: str) -> list[str]:
    return [word for word in args.split() if not word.startswith("--")]


def _commands(script: str) -> list[tuple[list[str], str]]:
    """A shell script's simple commands as (words, the separator after it), split at &&, ||, ;,
    |, & and parentheses. Quoted text stays one word, so a command quoted for `sh -c` stays
    hidden in it."""
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    commands: list[tuple[list[str], str]] = []
    words: list[str] = []
    for token in lexer:
        if token in _SEPARATORS:
            commands.append((words, token))
            words = []
        else:
            words.append(token)
    commands.append((words, ""))
    return [(words, separator) for words, separator in commands if words]


def unpinned_images(text: str) -> list[str]:
    """Every image that a FROM, a COPY or ADD `--from=`, or a RUN `--mount=…,from=…` names without
    an `@sha256:` digest of 64 hex characters at its end. A name that an earlier `FROM … AS <name>`
    declared is a build stage, not an image."""
    stages: set[str] = set()
    offenders: list[str] = []
    for keyword, args in _instructions(text):
        operands = _operands(args)
        refs = operands[:1] if keyword == "FROM" else []
        for flag in _flags(args):
            if flag.startswith("--from="):
                refs.append(flag.removeprefix("--from="))
            elif flag.startswith("--mount="):
                options = flag.removeprefix("--mount=").split(",")
                refs += [o.removeprefix("from=") for o in options if o.startswith("from=")]
        offenders += [r for r in refs if r.lower() not in stages and not _PINNED_RE.search(r)]
        if keyword == "FROM" and len(operands) >= 3 and operands[1].lower() == "as":
            stages.add(operands[2].lower())
    return offenders


def _url_name(args: list[str]) -> str | None:
    urls = [arg for arg in args if "://" in arg]
    return urls[0].split("?")[0].rstrip("/").rsplit("/", 1)[-1] if urls else None


def _fetch_target(tool: str, args: list[str]) -> str | None:
    """The file that a curl or wget call with `args` writes, or None when it writes to stdout or
    the file cannot be read off its arguments."""
    flag, long = ("o", "--output") if tool == "curl" else ("O", "--output-document")
    redirect = next((args[i + 1] for i, arg in enumerate(args[:-1]) if arg in (">", ">>")), None)
    target = None
    for i, arg in enumerate(args):
        value = args[i + 1] if i + 1 < len(args) else None
        if arg == long or re.fullmatch(rf"-[A-Za-z]*{flag}", arg):  # -o FILE, -fsSLo FILE
            target = value
        elif arg.startswith(f"{long}="):
            target = arg.removeprefix(f"{long}=")
        elif attached := re.fullmatch(rf"-{flag}(\S+)", arg):  # -oFILE
            target = attached.group(1)
        elif tool == "curl" and (arg == "--remote-name" or re.fullmatch(r"-[A-Za-z]*O\w*", arg)):
            target = _url_name(args)
        elif tool == "wget" and arg.startswith(("-P", "--directory-prefix")):
            return None
    if tool == "wget" and target is None:
        target = _url_name(args)  # wget's own default
    return redirect if target in (None, "-") else target


def unverified_downloads(text: str) -> list[str]:
    """Every download whose file no `echo '<sha256>  <file>' | sha256sum -c` checks: for curl and
    wget, later in the same RUN; for a remote ADD, in the RUN right after it. A curl or wget that
    sits inside a quoted word (`sh -c '…'`, `"$(…)"`) counts as unchecked."""
    offenders: list[str] = []
    pending: list[str] = []  # files that a remote ADD fetched, for the next RUN to check
    for keyword, args in _instructions(text):
        commands = _commands(args) if keyword == "RUN" else []
        checked: dict[str, list[int]] = {}
        for index, (words, _) in enumerate(commands):
            if index and "sha256sum" in words and {"-c", "--check"} & set(words):
                previous, separator = commands[index - 1]
                for word in previous if separator == "|" else []:
                    if line := _CHECKSUM_LINE_RE.fullmatch(word):
                        checked.setdefault(line.group(1), []).append(index)
        offenders += [name for name in pending if name not in checked]
        pending = []
        if keyword == "ADD" and ("://" in args or "git@" in args):
            operands = _operands(args)
            remote = [word for word in operands[:-1] if "://" in word or word.startswith("git@")]
            if not remote:
                offenders.append(f"ADD {args}")  # the JSON form, or a source not read here
            for source in remote:
                name = source.rstrip("/").rsplit("/", 1)[-1]
                pending.append(operands[-1] + name if operands[-1].endswith("/") else operands[-1])
        seen = 0
        for index, (words, _) in enumerate(commands):
            for at, word in enumerate(words):
                tool = word.rsplit("/", 1)[-1]
                if tool not in ("curl", "wget"):
                    continue
                seen += 1
                target = _fetch_target(tool, words[at + 1 :])
                if target is None or not any(i > index for i in checked.get(target, [])):
                    offenders.append(" ".join(words[at:]))
        if len(_FETCHER_RE.findall(args)) > seen:
            offenders.append(f"{keyword} {args}")
    return offenders + pending


def unpinned_installs(text: str) -> list[str]:
    """Every instruction that installs through pip, a global npm install or npx, which no pin in
    the Dockerfile covers."""
    return [
        f"{keyword} {args}"
        for keyword, args in _instructions(text)
        if any(pattern.search(args) for pattern in _INSTALLER_RES)
    ]


def tar_extractions_keeping_owners(text: str) -> list[str]:
    """Every tar extraction in a RUN without `--no-same-owner`: run as root, tar gives what it
    unpacks the archive's owner (uid 1000 for Node's tarball)."""
    offenders: list[str] = []
    for keyword, args in _instructions(text):
        for words, _ in _commands(args) if keyword == "RUN" else []:
            for at, word in enumerate(words):
                rest = words[at + 1 :]
                if word.rsplit("/", 1)[-1] != "tar":
                    continue
                extracts = any(
                    arg in ("--extract", "--get") or re.fullmatch(r"-[A-Za-z]*x[A-Za-z]*", arg)
                    for arg in rest
                ) or bool(rest and re.fullmatch(r"[A-Za-z]*x[A-Za-z]*", rest[0]))
                if extracts and "--no-same-owner" not in rest:
                    offenders.append(" ".join(words[at:]))
    return offenders


def _stages(text: str) -> dict[str, list[tuple[str, str]]]:
    """Each named stage's instructions after its FROM."""
    stages: dict[str, list[tuple[str, str]]] = {}
    current: list[tuple[str, str]] = []
    for keyword, args in _instructions(text):
        if keyword == "FROM":
            operands = _operands(args)
            current = []
            if len(operands) >= 3 and operands[1].lower() == "as":
                stages[operands[2].lower()] = current
        else:
            current.append((keyword, args))
    return stages


def _stage_instructions(text: str) -> list[tuple[str | None, str, list[tuple[str, str]]]]:
    """(stage name or None, its base image, its instructions after FROM), one per stage, in file
    order. A stage without `AS <name>` has no name and is never referenced by `--from=`."""
    stages: list[tuple[str | None, str, list[tuple[str, str]]]] = []
    for keyword, args in _instructions(text):
        if keyword == "FROM":
            operands = _operands(args)
            name = operands[2] if len(operands) >= 3 and operands[1].lower() == "as" else None
            stages.append((name, operands[0] if operands else "", []))
        elif stages:
            stages[-1][2].append((keyword, args))
    return stages


def unsnapshotted_apt_stages(text: str) -> list[str]:
    """Every stage that runs `apt-get install` without first pinning its archive the way its own
    base image needs: an Ubuntu stage by `APT::Snapshot "<timestamp>"`, a Debian stage by a
    snapshot.debian.org URL carrying a fixed timestamp - bookworm's apt (2.6.1) ignores
    `APT::Snapshot`, so a Debian stage relying on it alone is flagged too, not accepted. A stage
    whose base image this rule does not recognize as Ubuntu or Debian is flagged as unproven."""
    offenders: list[str] = []
    for name, image, instructions in _stage_instructions(text):
        body = "\n".join(args for _, args in instructions)
        install = _APT_INSTALL_RE.search(body)
        if not install:
            continue
        before = body[: install.start()]
        has_apt_snapshot = bool(_SNAPSHOT_RE.search(before))
        has_debian_snapshot = bool(_DEBIAN_SNAPSHOT_RE.search(before))
        lowered = image.lower()
        label = name or image
        if any(marker in lowered for marker in _UBUNTU_MARKERS):
            if not has_apt_snapshot:
                offenders.append(label)
        elif "debian" in lowered:
            if not has_debian_snapshot or has_apt_snapshot:
                offenders.append(label)
        else:
            offenders.append(label)
    return offenders


def _root_shell(text: str) -> str:
    """run.sh as root's shell reads it: comment lines dropped, single-quoted strings blanked (their
    text is data, or a script that another process runs, as in `as_user sh -c '…'`) and backslash
    continuations joined."""
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return re.sub(r"'[^']*'", "''", code).replace("\\\n", " ")


def _before_in_command(code: str, at: int) -> str:
    """What comes before offset `at` in its own simple command of `code`."""
    return re.split(r"&&|\|\||[;|()\n]", code[:at])[-1]


def _as_user(code: str, at: int) -> bool:
    return re.search(r"\bas_user\b", _before_in_command(code, at)) is not None


def test_every_image_is_pinned_by_a_sha256_digest() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert unpinned_images(text) == []


def test_every_download_is_checked_against_a_sha256_in_the_dockerfile() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert unverified_downloads(text) == []


def test_nothing_installs_through_pip_a_global_npm_install_or_npx() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert unpinned_installs(text) == []


def test_tarballs_unpack_without_their_archive_owner() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert tar_extractions_keeping_owners(text) == []


@pytest.mark.parametrize("stage", ["main", "oldgit"])
def test_the_image_build_fails_on_anything_uid_1000_could_change(stage: str) -> None:
    """A RUN after the stage's last change to /usr/local or /opt fails the build on an entry
    there that is not root:root, or that is world-writable."""
    instructions = _stages(DOCKERFILE.read_text(encoding="utf-8"))[stage]
    checks = [
        i
        for i, (keyword, args) in enumerate(instructions)
        if keyword == "RUN" and _OWNERSHIP_CHECK_RE.search(args)
    ]
    assert checks
    later = instructions[checks[-1] + 1 :]
    assert [keyword for keyword, _ in later if keyword == "RUN"] == []
    assert [
        args
        for keyword, args in later
        if keyword in ("COPY", "ADD") and re.search(r"(?:^|\s)/(?:usr/local|opt)\b", args)
    ] == []


def test_every_apt_stage_in_the_dockerfile_is_correctly_snapshotted() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert unsnapshotted_apt_stages(text) == []


def test_uv_installs_an_exact_python_patch_version() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    versions = _UV_PYTHON_RE.findall(text)
    assert versions == ["3.14.4"] * 3


def test_the_chrome_for_testing_url_carries_a_version() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert _CFT_URL_RE.search(text) is not None


_DIGEST = "sha256:" + "0" * 64
_HASH = "a" * 64

_UBUNTU_STAGE = f"FROM ubuntu:24.04@{_DIGEST} AS main\n"
_DEBIAN_STAGE = f"FROM debian:bookworm-slim@{_DIGEST} AS oldgit\n"
_APT_SNAPSHOT_LINE = (
    "RUN printf 'APT::Snapshot \"20261020T000000Z\";\\n' > /etc/apt/apt.conf.d/50snapshot\n"
)
_DEBIAN_SNAPSHOT_LINE = (
    "RUN printf 'URIs: http://snapshot.debian.org/archive/debian/20261020T000000Z\\n' "
    "> /etc/apt/sources.list.d/snapshot.sources\n"
)
_APT_GET_INSTALL_LINE = "RUN apt-get update && apt-get install -y git\n"


@pytest.mark.parametrize(
    "dockerfile",
    [
        _UBUNTU_STAGE + _APT_GET_INSTALL_LINE,
        _DEBIAN_STAGE + _APT_GET_INSTALL_LINE,
        _DEBIAN_STAGE + _APT_SNAPSHOT_LINE + _APT_GET_INSTALL_LINE,
        f"FROM scratch@{_DIGEST} AS mystery\n" + _APT_GET_INSTALL_LINE,
    ],
    ids=[
        "ubuntu-no-snapshot",
        "debian-no-snapshot-url",
        "debian-relies-on-apt-snapshot",
        "unrecognized-base",
    ],
)
def test_unsnapshotted_apt_stages_flags_each_case(dockerfile: str) -> None:
    assert unsnapshotted_apt_stages(dockerfile)


def test_unsnapshotted_apt_stages_accepts_correctly_pinned_stages() -> None:
    ubuntu_ok = _UBUNTU_STAGE + _APT_SNAPSHOT_LINE + _APT_GET_INSTALL_LINE
    debian_ok = _DEBIAN_STAGE + _DEBIAN_SNAPSHOT_LINE + _APT_GET_INSTALL_LINE
    assert unsnapshotted_apt_stages(ubuntu_ok) == []
    assert unsnapshotted_apt_stages(debian_ok) == []


def test_unsnapshotted_apt_stages_skips_a_stage_with_no_apt_get_install() -> None:
    assert unsnapshotted_apt_stages(_DEBIAN_STAGE + "RUN echo hi\n") == []


@pytest.mark.parametrize(
    "dockerfile",
    [
        "from ubuntu:24.04\n",
        f"FROM base@{_DIGEST} AS main\nCOPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/bin/\n",
        f"FROM base@{_DIGEST} AS main\nRUN --mount=type=bind,from=alpine:3,target=/a true\n",
        f"FROM base@{_DIGEST} AS uv\nFROM uv:0.11 AS main\n",
    ],
    ids=["lowercase-from", "copy-from-by-tag", "mount-from-by-tag", "stage-name-with-a-tag"],
)
def test_unpinned_images_flags_each_spelling(dockerfile: str) -> None:
    assert unpinned_images(dockerfile)


@pytest.mark.parametrize(
    "run",
    [
        "curl -fsSL -o /tmp/node.tar.xz https://x/node.tar.xz && tar -xJf /tmp/node.tar.xz",
        f"curl -fsSLo /tmp/a https://x/a && echo '{_HASH}  /tmp/b' | sha256sum -c",
        f"echo '{_HASH}  /tmp/a' | sha256sum -c && curl -fsSLo /tmp/a https://x/a",
        "wget -qO /tmp/a https://x/a",
        f"wget -qO- https://x/a | tar -x && echo '{_HASH}  /tmp/a' | sha256sum -c",
        "curl -fsSL https://x/install.sh | sh",
        f"sh -c 'curl -fsSLo /tmp/a https://x/a' && echo '{_HASH}  /tmp/a' | sha256sum -c",
    ],
    ids=[
        "curl-o-unchecked",
        "checks-another-file",
        "checked-before-the-download",
        "wget-unchecked",
        "wget-to-a-pipe",
        "curl-to-a-pipe",
        "curl-inside-quotes",
    ],
)
def test_unverified_downloads_flags_each_spelling(run: str) -> None:
    assert unverified_downloads(f"FROM base@{_DIGEST}\nRUN {run}\n")


def test_unverified_downloads_flags_a_remote_add() -> None:
    assert unverified_downloads(f"FROM base@{_DIGEST}\nADD https://x/a.tgz /tmp/a.tgz\n")


@pytest.mark.parametrize(
    "run",
    [
        f"curl -fsSL -o /tmp/a https://x/a && echo '{_HASH}  /tmp/a' | sha256sum -c",
        f"wget -q -O /tmp/a https://x/a && echo '{_HASH} */tmp/a' | sha256sum --check",
    ],
    ids=["curl", "wget"],
)
def test_unverified_downloads_accepts_a_checked_file(run: str) -> None:
    assert unverified_downloads(f"FROM base@{_DIGEST}\nRUN {run}\n") == []


@pytest.mark.parametrize(
    "command",
    [
        "pip install requests",
        "python3 -m pip install requests",
        "npm i -g pnpm",
        "npm install --global pnpm",
        "npx some-tool",
    ],
)
def test_unpinned_installs_flags_each_installer(command: str) -> None:
    assert unpinned_installs(f"FROM base@{_DIGEST}\nRUN {command}\n")


def test_run_sh_never_chowns_or_chmods_out() -> None:
    code = _root_shell(RUN_SH.read_text(encoding="utf-8"))
    changes = _CHANGE_OWNER_OR_MODE_RE.findall(code)
    assert changes
    assert [c for c in changes if re.search(r"\$\{?OUT\b|(?<![\w$}])/out\b", c)] == []


def test_run_sh_never_chmods_or_chowns_chrome_sandbox_or_sets_setuid() -> None:
    code = _root_shell(RUN_SH.read_text(encoding="utf-8"))
    changes = _CHANGE_OWNER_OR_MODE_RE.findall(code)
    assert [c for c in changes if "chrome-sandbox" in c or _SETUID_MODE_RE.search(c)] == []


def test_run_sh_runs_node_npm_uv_and_xvfb_run_only_as_the_user() -> None:
    code = _root_shell(RUN_SH.read_text(encoding="utf-8"))
    tools = list(_USER_TOOL_RE.finditer(code))
    assert tools
    assert [m.group(0) for m in tools if not _as_user(code, m.start())] == []


def test_failing_files_hands_node_the_report_path_as_an_argument() -> None:
    """The path reaches the script as process.argv, never spliced into its source."""
    text = RUN_SH.read_text(encoding="utf-8")
    body = re.search(r"(?ms)^failing_files\(\) \{$(.*?)^\}$", text)
    assert body is not None
    call = re.search(r"\bas_user node -e '([^']*)' \"\$1\"", body.group(1))
    assert call is not None
    assert "process.argv[1]" in call.group(1)


def test_run_sh_root_redirects_only_into_out() -> None:
    """Root's own redirects land in /out (or /dev/null), and user steps write their reports to
    $REPORTS, a folder of their own."""
    code = _root_shell(RUN_SH.read_text(encoding="utf-8"))
    targets = [m.group(2).strip('"') for m in _REDIRECT_RE.finditer(code) if not m.group(1)]
    assert targets
    assert [t for t in targets if t != "/dev/null" and not re.match(r"\$\{?OUT\}?/", t)] == []
    reports = re.findall(r"--outputFile=(\S+)", code)
    assert reports
    assert [r for r in reports if not r.startswith('"$REPORTS/')] == []


def test_run_sh_root_reads_the_users_files_only_as_the_user() -> None:
    """Reports and pytest's lastfailed reach /out through `as_user cat`: no cp, mv or ln, and a
    cat that root runs itself reads only from /out."""
    code = _root_shell(RUN_SH.read_text(encoding="utf-8"))
    assert re.findall(r"(?<![\w./-])(?:cp|mv|ln)(?![\w.-])", code) == []
    cats = list(re.finditer(r"(?<![\w./-])cat\s+(\S+)", code))
    assert cats
    assert [
        m.group(1)
        for m in cats
        if not _as_user(code, m.start()) and not re.match(r"\"?\$\{?OUT\}?/", m.group(1))
    ] == []


def test_run_sh_pins_one_electron_cache_root_for_install_and_builder() -> None:
    """electron's install.js reads `electron_config_cache` (lowercase: @electron/get's cacheRoot),
    and electron-builder 26 reads `$XDG_CACHE_HOME/electron` (@electron/get's env-paths default);
    ELECTRON_CACHE alone reaches neither, so Electron's zip re-downloads on every desktop and
    package run. Both settings must point at one folder on the named electron volume."""
    text = RUN_SH.read_text(encoding="utf-8")
    main_as_user = re.search(r"(?ms)^else\n(.*?)^fi$", text)
    assert main_as_user is not None
    assert "electron_config_cache=/cache/electron/xdg/electron" in main_as_user.group(1)
    dist_body = re.search(r"(?ms)^package_dist\(\) \{$(.*?)^\}$", text)
    assert dist_body is not None
    assert "XDG_CACHE_HOME=/cache/electron/xdg" in dist_body.group(1)


def test_linux_package_and_compile_prepare_the_snapshots_pinned_sidecar() -> None:
    text = RUN_SH.read_text(encoding="utf-8")

    def body(name: str) -> str:
        match = re.search(rf"(?ms)^{name}\(\) \{{$(.*?)^\}}$", text)
        assert match is not None
        return match.group(1)

    prepare = body("magpie_prepare")
    # The runner also exercises older merge-base trees that have no Magpie build inputs.
    assert 'if [ ! -f "$WORK/scripts/prepare_magpie.py" ]; then\n    return 0' in prepare
    assert "as_user uv run --no-sync python scripts/prepare_magpie.py || return $?" in prepare
    assert 'if [ "$SUITE" = package ]; then' in prepare
    assert 'stage(Path("."), Path("dist/compiled/viki-agent.dist"))' in prepare
    package = body("package_setup")
    assert package.index("magpie_prepare") < package.index("from scripts.licence_inventory")
    assert "magpie_prepare" in body("compile_setup")


def test_no_linux_script_has_a_cr_byte() -> None:
    offenders = {
        str(path.relative_to(REPO)): path.read_bytes().count(b"\r")
        for path in LINUX_DIR.rglob("*")
        if path.is_file()
    }
    assert {name: n for name, n in offenders.items() if n} == {}


def test_gitattributes_pins_shell_scripts_to_lf() -> None:
    text = GITATTRIBUTES.read_text(encoding="utf-8")
    assert re.search(r"(?m)^\*\.sh\s+text\s+eol=lf\s*$", text)
