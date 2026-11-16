"""Paths and startup preflight. Read-only: nothing here writes the vault or ~/.claude."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import claude_agent_sdk

from viki_agent import osfs, winproc, workspaces
from viki_agent.compiled import INSTALL_DIR
from viki_agent.domain import ProviderProblem

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 8765
# CLAUDE_CODE_ENTRYPOINT for spawned claude.exe. With "sdk-py" the CLI drops the built-in
# claude-code-guide agent; "viki-agent" keeps it (capture scenario 15): CLI 2.1.281 sets apart
# only exact values such as sdk-py and claude-vscode.
ENTRYPOINT = "viki-agent"
DATA_FOLDER = "viki-agent"  # the per-OS data folder's name
BUNDLED_CLI = (
    Path(claude_agent_sdk.__file__).parent
    / "_bundled"
    / ("claude.exe" if sys.platform == "win32" else "claude")
)
_REPO = INSTALL_DIR
# True in this checkout (frontend/ is a sibling of src/): a source run uses the Agent SDK's own
# bundled copy of the CLI, the same test `__main__._asset_paths` makes for frontend/dist.
# A compiled app ships no frontend/package.json, so this is always False there, and _cli() then
# never picks up a bundled copy even if one happens to sit on disk.
_SOURCE_CHECKOUT = (_REPO / "frontend" / "package.json").is_file()
# WinGet's package identity for Anthropic.ClaudeCode (its manifest's `Commands: [claude]`), whose
# folder installed_cli() falls back to when WinGet could not create the PATH link beside it.
_WINGET_PACKAGE = "Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe"
# Homebrew's two prefixes, Apple Silicon then Intel: a module-level tuple, not a literal inside
# installed_cli, so a test can point both at a synthetic tree instead of this machine's real one.
_MAC_INSTALLS = (Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude"))
_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
# Edge first, then Chrome, then Chromium: (kind, application bundle name) on macOS, looked for in
# _MAC_APPLICATIONS and then ~/Applications, and (kind, command name) on PATH on Linux.
_MAC_BROWSERS = (("edge", "Microsoft Edge"), ("chrome", "Google Chrome"), ("chromium", "Chromium"))
_MAC_APPLICATIONS = Path("/Applications")
_LINUX_BROWSERS = (
    ("edge", "microsoft-edge-stable"),
    ("edge", "microsoft-edge"),
    ("chrome", "google-chrome-stable"),
    ("chrome", "google-chrome"),
    ("chromium", "chromium"),
    ("chromium", "chromium-browser"),
)
# Claude Code's system-wide managed settings directory per OS (Linux is also every other POSIX
# system): managed-settings.json and its managed-settings.d/*.json drop-ins.
_MANAGED_DIRS = {
    "win32": Path(r"C:\Program Files\ClaudeCode"),
    "darwin": Path("/Library/Application Support/ClaudeCode"),
    "linux": Path("/etc/claude-code"),
}
# The CLI no longer reads this Windows path; it stays as a conservative extra source.
_LEGACY_MANAGED_SETTINGS = Path(r"C:\ProgramData\ClaudeCode\managed-settings.json")
# Scanned files at or above this size are skipped: drop-ins and profile configs are small.
_MAX_SCANNED_BYTES = 1_000_000
# Every settings source _read_json reads - the one fixed file at each of its other call sites,
# and the drop-ins and profile configs below - is read up to this many bytes; a larger file is
# ignored with a warning, exactly like an unreadable one (security.md). The drop-ins and profile
# configs were already capped at 1,000,000 bytes by _scannable below, well under this ceiling.
_READ_WHOLE_LIMIT = 16 * 1024 * 1024
# Settings `env` keys that would move billing off the user's subscription login: credentials,
# custom endpoints, and every CLAUDE_CODE_USE_* provider switch (the bundled CLI honours
# BEDROCK, VERTEX, FOUNDRY, ANTHROPIC_AWS, ANTHROPIC_GOOGLE_CLOUD, MANTLE, GATEWAY, ...).
# Model selection keys (ANTHROPIC_MODEL, ANTHROPIC_DEFAULT_*_MODEL) are fine.
_BILLING_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
)


def _is_billing_key(key: str) -> bool:
    upper = key.upper()
    return (
        upper in _BILLING_ENV_KEYS
        or upper.startswith("CLAUDE_CODE_USE_")
        or (upper.startswith("ANTHROPIC_") and upper.endswith("BASE_URL"))
    )


class ConfigError(RuntimeError):
    """Startup must stop; the message is shown to the user."""


CliSource = Literal["override", "bundled", "installed", ""]


@dataclass(frozen=True)
class Config:
    """Launch facts. A missing vault, a missing CLI and CLAUDE_CONFIG_DIR are recorded here
    rather than raised: the shell opens and reports them, and exactly one predicate —
    `diagnostics.Readiness.may_spawn()` — decides whether claude.exe may be started."""

    vault: Path | None  # load(): the validated VIKI_VAULT override, else None. The launcher
    # replaces it with the active workspace's working directory before create_app runs.
    claude_home: Path
    cli: Path | None  # None when missing or a batch shim
    edge: Path | None
    data_dir: Path
    cli_problem: ProviderProblem | None = None  # "not_found" | "batch_shim"
    vault_problem: str | None = None  # the canonical_root code that refused VIKI_VAULT
    claude_config_dir_set: bool = False
    browser_kind: str = ""  # "edge" | "chrome" | "chromium" | "override" | "" (none found)
    cli_source: CliSource = ""  # "override" | "bundled" | "installed" | "" (not found)

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def edge_profile(self) -> Path:
        return self.data_dir / "edge"

    @property
    def captures_dir(self) -> Path:
        return self.data_dir / "captures"


def vault_path(env: Mapping[str, str]) -> Path | None:
    """The raw VIKI_VAULT override, or None, with no fallback. Not checked here; load()
    canonicalises it."""
    raw = env.get("VIKI_VAULT")
    return Path(raw) if raw else None


def default_data_dir(env: Mapping[str, str]) -> Path:
    """The per-OS state directory, with no containment check: %LOCALAPPDATA%\\viki-agent on
    Windows, ~/Library/Application Support/viki-agent on macOS, and $XDG_DATA_HOME/viki-agent
    (~/.local/share/viki-agent by default) elsewhere. The launcher's fallback admits it
    separately before logging.
    """
    return _data_dir_for(sys.platform, env)


def _data_dir_for(platform: str, env: Mapping[str, str]) -> Path:
    if platform == "win32":
        local = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / DATA_FOLDER
    home = Path(env.get("HOME") or Path.home())
    if platform == "darwin":
        return home / "Library" / "Application Support" / DATA_FOLDER
    # The XDG spec says a relative XDG_DATA_HOME is invalid and must be ignored. A string test,
    # not Path.is_absolute(), so the rule reads the same when a Windows test exercises it.
    xdg = env.get("XDG_DATA_HOME") or ""
    return (Path(xdg) if xdg.startswith("/") else home / ".local" / "share") / DATA_FOLDER


def data_dir(env: Mapping[str, str]) -> Path:
    """The app's own state directory: VIKI_DATA_DIR, else default_data_dir()'s per-OS folder.

    Refused (ConfigError) when it lies inside ~/.claude, ~/.codex or a set VIKI_VAULT: hard
    rule 1, checked here so that it runs before anything, the log file included, is created
    there. Roots registered in the app store are checked after the store opens, by
    WorkspaceService.contains_data_dir().
    """
    own = Path(env.get("VIKI_DATA_DIR") or default_data_dir(env))
    return admit_data_dir(own, env)


def admit_data_dir(own: Path, env: Mapping[str, str]) -> Path:
    """Admit a chosen state path before either ordinary or fallback logging can write there."""
    home = Path.home()
    guards = (home / ".claude", home / ".codex", vault_path(env))
    for protected in guards:
        if protected is None:
            continue
        if _root_key_under(own.resolve(), protected.resolve()):
            raise ConfigError(f"VIKI_DATA_DIR {own} is inside {protected}; choose another folder.")
    return own


def _root_key_under(path: Path, ancestor: Path) -> bool:
    """True when `path` is `ancestor` or lies inside it, compared by this OS's root key so a
    case variant of a protected path cannot slip past the check on a case-insensitive volume
    (macOS); on Linux this is exactly a plain `is_relative_to`. On Windows it can differ from
    `is_relative_to` only for a handful of non-ASCII case foldings (`ntpath.normcase` uses
    LCMapStringEx, not `str.lower()`), and only in a path segment that does not exist yet:
    `resolve()` gives every existing folder the same on-disk spelling in both keys."""
    child, parent = workspaces.root_key(path), workspaces.root_key(ancestor)
    prefix = parent if parent.endswith(os.sep) else parent + os.sep
    return child == parent or child.startswith(prefix)


def load(env: Mapping[str, str] | None = None) -> Config:
    """Resolve paths from the environment. Overrides: VIKI_VAULT, VIKI_CLAUDE_CLI, VIKI_EDGE,
    VIKI_DATA_DIR.

    A missing vault, a missing or unusable CLI and a set CLAUDE_CONFIG_DIR are recorded, never
    raised: the app opens without any of them and says so in the shell. The one
    refusal left is a data directory inside a protected location, which decides where this
    process may write at all. The launcher's own call to data_dir() makes that check before
    the log file exists; load() repeats the same check, by which point logging is already
    configured.

    `cli_source` records which of these resolved it: VIKI_CLAUDE_CLI when set, never falling
    back ("override", or "bundled" when it names BUNDLED_CLI exactly); else the Agent SDK's own
    bundled copy in a source checkout ("bundled"); else the user's own installation
    (`installed_cli`, "installed"); else nothing ("").
    """
    env = os.environ if env is None else env
    own = data_dir(env)
    vault, vault_problem = _vault_override(env, own)
    cli, cli_problem, cli_source = _cli(env)
    edge, browser_kind = _browser_for(sys.platform, env)
    return Config(
        vault=vault,
        claude_home=Path.home() / ".claude",
        cli=cli,
        edge=edge,
        data_dir=own,
        cli_problem=cli_problem,
        vault_problem=vault_problem,
        claude_config_dir_set=bool(env.get("CLAUDE_CONFIG_DIR")),
        browser_kind=browser_kind,
        cli_source=cli_source,
    )


def _browser_for(platform: str, env: Mapping[str, str]) -> tuple[Path | None, str]:
    """The browser that hosts the app window and its kind, or (None, "") when none is found.
    VIKI_EDGE wins on every OS and may name any Chromium-family executable."""
    override = env.get("VIKI_EDGE")
    if override:
        return Path(override), "override"
    if platform == "win32":
        edge = next((Path(p) for p in _EDGE_CANDIDATES if Path(p).is_file()), None)
        return edge, "edge" if edge else ""
    if platform == "darwin":
        home = Path(env.get("HOME") or Path.home())
        for kind, name in _MAC_BROWSERS:
            for apps in (_MAC_APPLICATIONS, home / "Applications"):
                exe = apps / f"{name}.app" / "Contents" / "MacOS" / name
                if exe.is_file():
                    return exe, kind
        return None, ""
    for kind, name in _LINUX_BROWSERS:
        found = shutil.which(name, path=env.get("PATH"))
        if found and not _confined(found):
            return Path(found), kind
    return None, ""


def _confined(exe: str) -> bool:
    """A snap or flatpak launcher, or a shell wrapper that execs one. Their strict confinement
    cannot use a browser profile in the app's hidden data directory, so the window would never
    open; skipping them lets discovery fall through to a browser that can. Ubuntu's
    /usr/bin/chromium-browser is such a wrapper: a #!/bin/sh script whose last line execs
    /snap/bin/chromium. An OSError reading it (permissions, a dangling link) means not confined,
    so discovery treats it as it would a file it cannot inspect at all."""
    if (
        exe.startswith("/snap/")
        or "/flatpak/exports/" in exe
        or os.path.basename(os.path.realpath(exe)) == "snap"
    ):
        return True
    try:
        with open(exe, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return head.startswith(b"#!") and b"/snap/bin/" in head


def _vault_override(env: Mapping[str, str], own: Path) -> tuple[Path | None, str | None]:
    """The VIKI_VAULT override put through the same canonicalisation a registration gets, so a
    launch and the Workspaces screen agree about which roots are usable."""
    raw = env.get("VIKI_VAULT")
    if not raw:
        return None, None
    deny = workspaces.denied_roots(env, data_dir=own, home=Path.home(), install=_REPO)
    try:
        root = workspaces.canonical_root(raw, deny=deny)
    except workspaces.WorkspaceError as exc:
        log.warning("VIKI_VAULT ignored: %s", exc.code)
        return None, exc.code
    return Path(root.display), None


def installed_cli(platform: str, env: Mapping[str, str]) -> Path | None:
    """The user's own Claude Code installation, found the way Anthropic's installer and each
    package manager places it, in priority order; None when nothing is found anywhere.

    win32: `%USERPROFILE%\\.local\\bin\\claude.exe` (Anthropic's own installer), then
    `%LOCALAPPDATA%\\Microsoft\\WinGet\\Links\\claude.exe` (the PATH link WinGet's `portable`
    installer type creates for its manifest's `Commands` entry), then the WinGet package folder
    itself (`_WINGET_PACKAGE`, used when WinGet could not create that link), then npm's global
    install (`%APPDATA%\\npm\\node_modules\\...`, the same native binary `claude.cmd` calls).
    darwin and linux: `~/.local/bin/claude` (Anthropic's installer there too), then on darwin the
    two fixed Homebrew prefixes in `_MAC_INSTALLS` (Apple Silicon, then Intel). Each candidate is
    tried only when the environment variable that locates it is set, read from `env` and never
    from `Path.home()` (so a test's synthetic environment can never leak this machine's real
    one), and only the first that `resolve_exe` accepts is returned.

    PATH is the last resort, not the first: every official method above has a fixed, known
    location, so checking them first means a `claude(.exe)` that merely happens to be first on
    PATH (an unrelated tool, a differently named project) never wins over Anthropic's own
    installs; `find_on_path` is what then skips shims and relative or empty entries.
    """
    candidates: list[Path] = []
    if platform == "win32":
        if user_profile := env.get("USERPROFILE"):
            candidates.append(Path(user_profile) / ".local" / "bin" / "claude.exe")
        if local_appdata := env.get("LOCALAPPDATA"):
            winget = Path(local_appdata) / "Microsoft" / "WinGet"
            candidates.append(winget / "Links" / "claude.exe")
            candidates.append(winget / "Packages" / _WINGET_PACKAGE / "claude.exe")
        if appdata := env.get("APPDATA"):
            candidates.append(
                Path(appdata)
                / "npm"
                / "node_modules"
                / "@anthropic-ai"
                / "claude-code"
                / "bin"
                / "claude.exe"
            )
    else:
        if home := env.get("HOME"):
            candidates.append(Path(home) / ".local" / "bin" / "claude")
        if platform == "darwin":
            candidates.extend(_MAC_INSTALLS)
    for candidate in candidates:
        try:
            return winproc.resolve_exe(candidate)
        except ValueError, OSError:
            continue
    return winproc.find_on_path("claude.exe" if platform == "win32" else "claude", env.get("PATH"))


def _cli(env: Mapping[str, str]) -> tuple[Path | None, ProviderProblem | None, CliSource]:
    """The provider executable, the reason it cannot be used, and where it came from.

    VIKI_CLAUDE_CLI always wins and never falls back to anything below it, even when it is
    broken: the user who set it gets exactly resolve_exe's result ("batch_shim" or
    "not_found"), never a quieter success from a different install. Its source reads "bundled"
    only when it happens to name BUNDLED_CLI exactly, "override" otherwise.

    With no override, a source checkout (`_SOURCE_CHECKOUT`) runs the Agent SDK's own bundled
    copy; the product path (every compiled app a user runs) is exercised by unit tests here,
    never by a developer's own source run. Everywhere else, including a source checkout whose
    bundled copy is somehow missing, the user's own installation is what runs
    (`installed_cli`): a user's app never bundles or modifies Claude Code.
    """
    override = env.get("VIKI_CLAUDE_CLI")
    if override:
        try:
            resolved = winproc.resolve_exe(override)
        except ValueError:  # a .cmd/.bat shim: CreateProcess cannot run it directly
            return None, "batch_shim", "override"
        except OSError:
            return None, "not_found", "override"
        return resolved, None, "bundled" if resolved == BUNDLED_CLI else "override"
    if _SOURCE_CHECKOUT:
        try:
            return winproc.resolve_exe(BUNDLED_CLI), None, "bundled"
        except ValueError, OSError:
            pass
    found = installed_cli(sys.platform, env)
    if found is not None:
        return found, None, "installed"
    return None, "not_found", ""


def billing_problems(cfg: Config, env: Mapping[str, str] | None = None) -> list[str]:
    """Settings that would bill anything but the user's subscription login. Empty means clear.

    Complements the runtime check on the initialize response's `account` block: this catches
    configuration before any process starts.
    """
    env = os.environ if env is None else env
    sources = [
        cfg.claude_home / "settings.json",
        # Project settings only when a vault workspace is known: with none, the CLI runs in the
        # scratch root, which has no .claude of its own, and reading the process working
        # directory's would scan whatever folder the app happened to be started from.
        *(
            []
            if cfg.vault is None
            else [
                cfg.vault / ".claude" / "settings.json",
                cfg.vault / ".claude" / "settings.local.json",
            ]
        ),
        *_managed_sources(sys.platform),
    ]
    problems: list[str] = []
    for path in sources:
        if not _settings_source_allowed(path, cfg.claude_home, env):
            continue
        settings = _read_json(path)
        if settings.get("apiKeyHelper"):
            problems.append(f"{path}: apiKeyHelper is set")
        settings_env = settings.get("env")
        if isinstance(settings_env, dict):
            problems.extend(
                f"{path}: env.{key} is set" for key in settings_env if _is_billing_key(str(key))
            )
    anthropic_dir = _anthropic_dir(sys.platform, env)
    if anthropic_dir is not None:
        # A parsed key in a profile config, not the word "federation" anywhere under the tree.
        # Matching the bare substring in every file would let a cached page, a log line or a
        # changelog mentioning federation refuse every launch with no way for the user to tell
        # why — a preflight that cries wolf is worse than none, because the next real one gets
        # ignored. Only configs/ is read: the federation block lives there, and
        # credentials/ beside it holds tokens, which the app never opens, even through a
        # symbolic link (a same-account hard link has no target to resolve, an accepted limit).
        for path in _scannable(anthropic_dir / "configs"):
            if not _settings_source_allowed(path, cfg.claude_home, env):
                continue
            data = _read_json(path)
            if any(_names_federation(key, value) for key, value in data.items()):
                problems.append(f"{path}: an Anthropic federation profile is configured")
    return problems


def _managed_sources(platform: str) -> list[Path]:
    """The OS's managed settings file, then its drop-ins in name order, a dot-named one skipped
    (the CLI never applies a disabled drop-in); on Windows the legacy path last."""
    managed = _MANAGED_DIRS.get(platform, _MANAGED_DIRS["linux"])
    drop_ins = (p for p in _scannable(managed / "managed-settings.d") if not p.name.startswith("."))
    sources = [managed / "managed-settings.json", *drop_ins]
    if platform == "win32":
        sources.append(_LEGACY_MANAGED_SETTINGS)
    return sources


def _anthropic_dir(platform: str, env: Mapping[str, str]) -> Path | None:
    """The Anthropic configuration directory, as the CLI finds it, or None when the variable
    that locates it is unset. The launcher strips every ANTHROPIC_* variable from its own and
    the CLI's environment, so in a real launch ANTHROPIC_CONFIG_DIR is absent and the default
    directory, the one the CLI then reads, is the one scanned. Off Windows the CLI's own order is
    ANTHROPIC_CONFIG_DIR, then XDG_CONFIG_HOME/anthropic, then HOME/.config/anthropic; a set
    XDG_CONFIG_HOME need not be absolute. Windows has no such convention and never reads it."""
    if configured := env.get("ANTHROPIC_CONFIG_DIR"):
        return Path(configured)
    if platform == "win32":
        appdata = env.get("APPDATA")
        return Path(appdata) / "Anthropic" if appdata else None
    if xdg := (env.get("XDG_CONFIG_HOME") or "").strip():
        return Path(xdg) / "anthropic"
    home = env.get("HOME")
    return Path(home) / ".config" / "anthropic" if home else None


def _settings_source_allowed(path: Path, claude_home: Path, env: Mapping[str, str] | None) -> bool:
    """Exclude known credential targets without rejecting ordinary linked settings.

    Only the supplied provider home and launch environment locate protected targets; no
    credential contents are inspected. Hard links and concurrent same-account retargeting
    have no stable resolved target to exclude here.
    """
    env = os.environ if env is None else env
    credentials = [claude_home / ".credentials.json"]
    if anthropic := _anthropic_dir(sys.platform, env):
        credentials.append(anthropic / "credentials")
    try:
        target = path.resolve()
        return not any(_root_key_under(target, secret.resolve()) for secret in credentials)
    except (OSError, RuntimeError) as exc:
        log.warning("ignoring unresolved settings %s: %s", path, type(exc).__name__)
        return False


def _scannable(folder: Path) -> list[Path]:
    """The folder's *.json files below the size cap, in name order: not recursive, and a symlink
    is read (the `configs/` caller keeps credentials/ unopened by resolved path instead). A
    missing folder has none. A dotfile is not filtered here: a profile config's name may start
    with a dot, and only the drop-in caller (`_managed_sources`) skips one of those."""
    return [
        path
        for path in sorted(folder.glob("*.json"))
        if path.is_file() and path.stat().st_size < _MAX_SCANNED_BYTES
    ]


def _names_federation(key: str, value: object) -> bool:
    """A top-level JSON key or string value naming federation. Both, because the marker sits in
    either depending on the profile's shape — but parsed, and in a .json file, which is what
    separates a profile from a log line or a cached page that merely says the word."""
    if "federation" in str(key).lower():
        return True
    return isinstance(value, str) and "federation" in value.lower()


def _read_json(path: Path) -> dict[str, object]:
    """The file's JSON object, or {} when it is absent, unreadable or not an object.

    utf-8-sig, not utf-8: PowerShell writes settings.json with a UTF-8 BOM, and plain utf-8 leaves
    that BOM in the decoded string, where json.loads rejects it. The preflight above would then
    read a real settings file as empty and report "clear" for a machine configured to bill
    somewhere else. The CLI's own settings reader uses utf-8-sig for exactly this reason;
    BOM-less UTF-8 decodes identically.
    """
    try:
        # Read up to _READ_WHOLE_LIMIT (security.md): every source this function reads, fixed
        # file or size-capped drop-in alike, gets the same one bounded, finite read.
        data = json.loads(osfs.read_regular(path, _READ_WHOLE_LIMIT).decode("utf-8-sig"))
    except FileNotFoundError:
        return {}  # the ordinary case: most of the settings sources above do not exist
    except (OSError, ValueError, RecursionError) as exc:
        # utf-8-sig does not make every file readable — UTF-16 from PowerShell's Out-File, a
        # half-written file, a locked one, or JSON nested too deep for json.loads (RecursionError,
        # not ValueError). Those still read as "says nothing", which is the preflight's blind
        # spot; the warning is what makes the blind spot visible instead of silent. Not a
        # refusal: a stray unparseable .json among the Anthropic configuration files must never
        # be able to stop the app from starting.
        log.warning("ignoring unreadable %s: %s", path, type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}
