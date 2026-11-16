"""Independent provider/git/storage readiness, the diagnostics document and redaction.

Never reads a credential file, never logs a token, prompt text or a frame body (hard rules 1
and 8): a probe that raises is logged only as its exception type name, never a traceback or its
message. Exactly one predicate gates every spawn of claude.exe: `Readiness.may_spawn()`. This
module never imports `config.Config` directly -- `_ProviderConfig` below is a structural
(duck-typed) stand-in for the three fields it needs, so it does not depend on config.py;
whatever real `Config` is passed in at runtime only needs to have
those attributes. `diagnostics_document` never touches the filesystem itself: the caller
supplies the already-computed `StorageReadiness`, the same one every other surface uses.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from viki_agent import protection
from viki_agent.domain import (
    ActiveWorkspace,
    Capabilities,
    EffectiveSettings,
    GitReadiness,
    ProviderProblem,
    ProviderReadiness,
    StorageReadiness,
    Workspace,
)

CheckName = Literal["provider_cli", "git"]
logger = logging.getLogger(__name__)


class _ProviderConfig(Protocol):
    """The Config fields readiness needs: `config.Config` satisfies this structurally, so this
    module never needs to import `config.py` itself.
    Declared as read-only properties, not plain attributes, so a frozen dataclass (config.Config
    is one) matches structurally too -- a mutable-attribute Protocol member can only be satisfied
    by another mutable attribute, which a frozen dataclass field is not."""

    @property
    def cli(self) -> Path | None: ...
    @property
    def cli_problem(self) -> ProviderProblem | None: ...
    @property
    def claude_config_dir_set(self) -> bool: ...
    @property
    def browser_kind(self) -> str: ...
    @property
    def cli_source(self) -> str: ...


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _classify_billing_problem(message: str) -> ProviderProblem:
    """Map one of config.billing_problems()'s human-readable strings to a closed code.

    `env.` is checked first, ahead of the looser apiKeyHelper/federation substrings: every
    message is `f"{path}: {condition}"`, and `path` is attacker- or user-chosen text that can
    itself contain any of these words (a folder literally named "apiKeyHelper-backup"). None of
    the three checks is immune to that in general, but `env.` is the one config.py always
    anchors right after ": " with no path segment before it in the billing_env_key case, so
    ordering it first means a path merely containing "apiKeyHelper" or "federation profile"
    text cannot steal a message that is actually reporting an env key.
    """
    if "env." in message:
        return "billing_env_key"
    if "apiKeyHelper" in message:
        return "api_key_helper"
    if "federation profile" in message:
        return "federation_profile"
    return "other"


CLI_TESTED_MIN = "2.1.281"
CLI_TESTED_MAX = "2.1.282"
# Evidence for this range, not a guess at what a newer CLI probably still supports: 2.1.281 is
# profile_capabilities.FIXTURE_VERSION, the version the compatibility-proof fixtures and the
# interplay tests exercise against the Agent SDK's own bundled copy; 2.1.282 is the version
# ClaudeDriver.set_permission_mode was last probed against (2026-10-20, see its docstring).
# Raise only with new evidence of the same kind.


def version_status(version: str) -> Literal["unknown", "tested", "older", "newer"]:
    """Where `version` falls against the CLI versions this adapter has evidence for
    (`CLI_TESTED_MIN` to `CLI_TESTED_MAX`), comparing dotted integers as tuples so "2.1.9" sorts
    below "2.1.10". Anything that is not all dotted integers -- "", a stray suffix the caller
    did not already cut off, or outright garbage -- answers "unknown", never a guess."""
    parts = version.split(".")
    if not all(part.isdigit() for part in parts):
        return "unknown"
    parsed = tuple(int(part) for part in parts)
    if parsed < tuple(int(part) for part in CLI_TESTED_MIN.split(".")):
        return "older"
    if parsed > tuple(int(part) for part in CLI_TESTED_MAX.split(".")):
        return "newer"
    return "tested"


class Readiness:
    """Per-launch observations. Never spawns anything itself; callers feed it what a session
    start, the launch-time billing preflight and the version probes observed."""

    def __init__(
        self,
        *,
        config: _ProviderConfig,
        launch_id: str,
        git_available: Callable[[], bool],
        git_overridden: Callable[[], bool] = lambda: False,
    ) -> None:
        self._config = config
        self._launch_id = launch_id
        self._git_overridden = git_overridden
        self._billing: Literal["clear", "blocked", "unknown"] = "unknown"
        self._billing_problems: tuple[ProviderProblem, ...] = ()
        self._other_sign_in = False
        self._login: Literal["unknown", "signed_in", "signed_out"] = "unknown"
        self._version = ""
        self._last_checked_at: str | None = None
        self._git_probe_state: Literal["ready", "missing"] = (
            "ready" if git_available() else "missing"
        )
        self._git_version = ""

    def observe_billing(self, problems: Sequence[str]) -> None:
        """The launch-time billing preflight (non-fatal): empty means clear."""
        self._billing = "blocked" if problems else "clear"
        self._billing_problems = tuple(_classify_billing_problem(p) for p in problems)
        self._last_checked_at = _now()

    def observe_sign_in_choice(self, allowed: bool) -> None:
        """The user's `chat.other_sign_in` choice, read once at launch: when allowed, native
        sessions start on whatever sign-in Claude Code is set up with."""
        self._other_sign_in = allowed

    def observe_account(self, account: Mapping[str, object] | None) -> None:
        """The `initialize` reply's account block from a real session start. Only a first-party
        account moves login to signed_in; anything else leaves it as it was, since a session
        that never reached `initialize` reports through observe_start_error instead."""
        if account is not None and account.get("apiProvider") == "firstParty":
            self._login = "signed_in"
        self._last_checked_at = _now()

    def observe_start_error(self, code: str) -> None:
        if code == "not_logged_in":
            self._login = "signed_out"
        elif code == "billing":
            self._billing = "blocked"
        self._last_checked_at = _now()

    def observe_version(self, version: str, *, checked_at: str | None = None) -> None:
        self._version = version
        self._last_checked_at = checked_at or _now()

    def observe_git(self, version: str, *, checked_at: str | None = None) -> None:
        self._git_version = version
        self._git_probe_state = "ready" if version else "missing"
        self._last_checked_at = checked_at or _now()

    def _cli_state(self) -> Literal["ready", "missing", "unusable", "config_dir_override"]:
        if self._config.claude_config_dir_set:
            return "config_dir_override"
        # A shim is recorded as cli=None too (config.load keeps no path to a .cmd wrapper), so
        # the problem code is judged before the missing path or a real shim reads "missing".
        if self._config.cli_problem == "batch_shim":
            return "unusable"
        if self._config.cli is None or self._config.cli_problem == "not_found":
            return "missing"
        return "ready"

    def _current_git_state(self) -> Literal["ready", "missing", "overridden"]:
        # The injected `git_overridden` (never, by default) decides this; overridden always wins,
        # since a scoped-out Git means the app must act as if Git were absent regardless of what the
        # last `git --version` probe found.
        if self._git_overridden():
            return "overridden"
        return self._git_probe_state

    def _problems(self) -> tuple[ProviderProblem, ...]:
        found: list[ProviderProblem] = []
        if self._config.claude_config_dir_set:
            found.append("config_dir_override")
        elif self._config.cli_problem in ("not_found", "batch_shim"):
            found.append(self._config.cli_problem)
        found.extend(self._billing_problems)
        seen: set[ProviderProblem] = set()
        ordered: list[ProviderProblem] = []
        for problem in found:
            if problem not in seen:
                seen.add(problem)
                ordered.append(problem)
        return tuple(ordered)

    def may_spawn(self) -> bool:
        """The single predicate every spawn of claude.exe must check first."""
        return self._cli_state() == "ready"

    @property
    def billing(self) -> Literal["clear", "blocked", "unknown"]:
        """The native-session billing state: a block is lifted while the user allows other
        sign-in methods. Model connections read `billing_preflight_blocked` instead."""
        if self._other_sign_in and self._billing == "blocked":
            return "clear"
        return self._billing

    @property
    def billing_preflight_blocked(self) -> bool:
        """Conflicting launch settings, independently of runtime subscription refusals."""
        return bool(self._billing_problems)

    @property
    def other_sign_in(self) -> bool:
        return self._other_sign_in

    @property
    def cli_state(self) -> Literal["ready", "missing", "unusable", "config_dir_override"]:
        return self._cli_state()

    @property
    def version(self) -> str:
        """The observed CLI version, with no store or filesystem read."""
        return self._version

    @property
    def git_state(self) -> Literal["ready", "missing", "overridden"]:
        """What the identity probes read: the launch-time probe until a `git` check re-runs it,
        and never "ready" while the scope override is set."""
        return self._current_git_state()

    @property
    def launch_id(self) -> str:
        return self._launch_id

    @property
    def browser_kind(self) -> str:
        """The launch-time browser discovery outcome, or "" when none was found."""
        return self._config.browser_kind

    def capabilities(
        self,
        active: ActiveWorkspace | None,
        storage: StorageReadiness,
        *,
        mode: Literal["normal", "recovery"] = "normal",
        root_available: bool = True,
    ) -> Capabilities:
        cli_source = self._config.cli_source
        source: Literal["override", "bundled", "installed", ""] = (
            cli_source if cli_source in ("override", "bundled", "installed") else ""
        )
        provider = ProviderReadiness(
            cli=self._cli_state(),
            version=self._version,
            login=self._login,
            billing=self.billing,
            last_checked_at=self._last_checked_at,
            can_start=(
                self._cli_state() == "ready"
                and self.billing != "blocked"
                and storage.state == "ready"
            ),
            problems=self._problems(),
            source=source,
            version_status=version_status(self._version),
        )
        return Capabilities(
            app={"mode": mode},
            workspace=_workspace_json(active, root_available=root_available),
            provider={"claude": provider},
            git=GitReadiness(state=self._current_git_state(), version=self._git_version),
            storage=storage,
            features={},
            launch={"id": self._launch_id},
        )


def _workspace_json(
    active: ActiveWorkspace | None, *, root_available: bool
) -> Mapping[str, object]:
    if active is None:
        return {
            "active": None,
            "chat_only": True,
            "selected_id": None,
            "selected_by": "builtin",
            "restart_required": False,
            "shared_scratch": False,
        }
    workspace = active.workspace
    scratch = workspace.kind == "scratch"
    if scratch:
        # None is the expected, no-restart-needed state for the implicit scratch fallback.
        restart_required = (
            active.selected_id is not None and active.selected_id != workspace.workspace_id
        )
    else:
        # A vault active with no persisted selection (None) is itself a mismatch between the
        # in-memory active workspace and the persisted one, not the "nothing selected yet"
        # baseline that None means for scratch.
        restart_required = active.selected_id != workspace.workspace_id
    return {
        "active": {
            "id": workspace.workspace_id,
            "kind": workspace.kind,
            "name": workspace.name,
            "policy": workspace.policy,
            "layout": workspace.layout,
            "source": workspace.source,
            "aliased": workspace.root.aliased,
            "root_available": root_available,
        },
        "chat_only": active.chat_only,
        "selected_id": active.selected_id,
        "selected_by": active.selected_by,
        "restart_required": restart_required,
        "shared_scratch": scratch,
    }


def _settings_summary(settings: EffectiveSettings | None) -> Mapping[str, object] | None:
    if settings is None:
        return None
    document = settings.document
    return {
        "version": document.version,
        "revision": document.revision,
        "read_only": document.read_only,
        "reason": document.reason,
    }


def _workspace_summary(
    workspace: Workspace, *, include_paths: bool, include_identity: bool
) -> Mapping[str, object]:
    return {
        "id": workspace.workspace_id,
        "kind": workspace.kind,
        "policy": workspace.policy,
        "layout": workspace.layout,
        "name": workspace.name if include_identity else "<redacted>",
        "root": workspace.root.display if include_paths else "<redacted>",
    }


def _os_name(platform: str) -> Literal["windows", "macos", "linux"]:
    if platform == "win32":
        return "windows"
    if platform == "darwin":
        return "macos"
    return "linux"


def diagnostics_document(
    readiness: Readiness,
    *,
    storage: StorageReadiness,
    settings: EffectiveSettings | None,
    workspaces: Sequence[Workspace],
    data_dir: Path,
    log_path: Path,
    include_paths: bool,
    include_identity: bool,
) -> dict[str, object]:
    """Versions, readiness, storage and settings-parse state as one JSON-safe dict.

    `storage` is the caller's own already-computed StorageReadiness (the same AppState-derived
    read used everywhere else); this function does no filesystem I/O of its own. `data_dir` and
    `log_path` contribute only their path strings, shown when `include_paths`. Never includes an
    environment value: every other field comes from `readiness`'s already-observed state or the
    caller's own already-loaded `settings`/`workspaces`, with names and roots redacted per
    `include_identity`/`include_paths`.
    """
    caps = readiness.capabilities(None, storage)
    provider = caps.provider["claude"]
    document: dict[str, object] = {
        "generated_at": _now(),
        "launch_id": caps.launch["id"],
        "provider": {
            "claude": {
                "cli": provider.cli,
                "version": provider.version,
                "login": provider.login,
                "billing": provider.billing,
                "can_start": provider.can_start,
                "problems": list(provider.problems),
                "last_checked_at": provider.last_checked_at,
                "source": provider.source,
                "version_status": provider.version_status,
                "tested": [CLI_TESTED_MIN, CLI_TESTED_MAX],
            }
        },
        "git": {"state": caps.git.state, "version": caps.git.version},
        "storage": {"state": storage.state, "reason": storage.reason, "private": storage.private},
        "platform": {
            "os": _os_name(sys.platform),
            "browser": readiness.browser_kind or "none",
            "keyring": protection.keyring_state(),
        },
        "settings": _settings_summary(settings),
        "workspaces": [
            _workspace_summary(w, include_paths=include_paths, include_identity=include_identity)
            for w in workspaces
        ],
    }
    if include_paths:
        document["data_dir"] = str(data_dir)
        document["log_path"] = str(log_path)
    return document


# --- redaction ----------------------------------------------------------------------------

_SECRET_MARKERS = ("token=", "Bearer ", "sk-ant-", "ANTHROPIC_", "OAUTH")
_EXPORT_SECRET_MARKERS = tuple(marker.casefold() for marker in _SECRET_MARKERS)
_CREDENTIAL_NAME = (
    r"(?:api[_-]?key|(?:api|access|refresh|auth|oauth|bot|app|session)[_-]?token|"
    r"client[_-]?secret|private[_-]?key|token|password|passwd|secret|authorization|"
    r"cookie|set[_-]?cookie)"
)
_CREDENTIAL_KEY_RE = re.compile(rf"(?:^|[._-]){_CREDENTIAL_NAME}$", re.IGNORECASE)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    rf"(?<![a-z0-9]){_CREDENTIAL_NAME}[\"']?\s*[:=]", re.IGNORECASE
)
# Do not retry a long dotted scheme at each word boundary inside that same scheme.
_URL_USERINFO_RE = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://[^/\s?#@\"<>]*@", re.IGNORECASE)
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,}")
# A drive-letter path (C:\Users\name, c:/Users/name, C:\\Users\\name once JSON-escaped) or a
# UNC path (\\server\share\Users\name, \\localhost\C$\Users\name), any mix of \ and /, any
# case, any repeated-backslash depth. Only the one path segment after "Users" is generalised,
# matching redact()'s per-root behaviour of substituting a path prefix, not a whole line.
_USER_PATH_RE = re.compile(
    r"(?:[A-Za-z]:|[\\/]{2,}[^\\/\s]+[\\/]+[^\\/\s]+)[\\/]+Users[\\/]+[^\\/\s]+",
    re.IGNORECASE,
)
# A macOS or Linux home (/Users/name, /home/name, /var/home/name), or the folder where Linux
# desktops mount removable drives under the user name (/media/name, /run/media/name). The
# lookbehind excludes a preceding word character, dot or hyphen, so /usr/share/x and a name
# inside another component stay as they are; it does not exclude `>` or `~`, so
# `<workspace>/media/x` or `~/media/x` also matches and is redacted too -- over-redaction, never
# a miss.
_POSIX_USER_PATH_RE = re.compile(
    r"(?<![\w.-])(?:/var)?/home/[^/\s\"']+|(?<![\w.-])/Users/[^/\s\"']+"
    r"|(?<![\w.-])(?:/run)?/media/[^/\s\"']+"
)


def _looks_secret(line: str) -> bool:
    """Token shapes used by provider metadata admission, with its existing error contract."""
    return any(marker in line for marker in _SECRET_MARKERS) or bool(_LONG_TOKEN_RE.search(line))


def is_credential_key(key: str) -> bool:
    """Match credential fields, including the sealed model-connection bundle."""
    return key.lower() == "chat.llm_connections" or bool(_CREDENTIAL_KEY_RE.search(key))


def redact_secrets(text: str) -> tuple[str, int]:
    """Drop credential-bearing lines without changing included path spellings."""
    kept_lines = []
    count = 0
    for line in text.splitlines(keepends=True):
        # Export filtering is broader than the provider metadata admission grammar.
        if (
            any(marker in line.casefold() for marker in _EXPORT_SECRET_MARKERS)
            or _CREDENTIAL_ASSIGNMENT_RE.search(line)
            or _URL_USERINFO_RE.search(line)
            or _LONG_TOKEN_RE.search(line)
        ):
            count += 1
        else:
            kept_lines.append(line)
    return "".join(kept_lines), count


def _root_pattern(root_path: str) -> re.Pattern[str]:
    """A regex matching `root_path` spelled with any mix of `\\`, `/` or a JSON-escaped `\\\\`
    as its separators, case-insensitively -- so `C:\\VIKI`, `c:/VIKI` and `C:\\\\VIKI` (the
    literal text of a JSON-encoded path) all match the same root."""
    segments = [s for s in re.split(r"[\\/]+", root_path.strip("\\/")) if s]
    if not segments:
        return re.compile("(?!)")  # never matches: a blank/degenerate root
    body = r"[\\/]+".join(re.escape(segment) for segment in segments)
    return re.compile(body, re.IGNORECASE)


def redact(text: str, *, roots: Sequence[tuple[str, str]]) -> tuple[str, int]:
    """Replace each known root with its placeholder (longest path first, so a nested root such
    as data_dir-under-home is not shadowed by the shorter one; matched case-insensitively and
    with any mix of `\\`, `/` or a JSON-escaped `\\\\` as the separator), generalise any
    remaining Windows user path -- drive-letter or UNC form, either separator, any case -- and
    then any macOS or Linux home path (`/Users/<name>`, `/home/<name>`, `/var/home/<name>`) or
    removable-media mount (`/media/<name>`, `/run/media/<name>`) to `<user>`, on every OS, then
    drop every line that looks like a secret. Returns the redacted text and how many
    replacements/drops were made."""
    count = 0
    working = text
    for root_path, placeholder in sorted(roots, key=lambda pair: len(pair[0]), reverse=True):
        if not root_path:
            continue
        working, replaced = _root_pattern(root_path).subn(placeholder, working)
        count += replaced
    for user_path in (_USER_PATH_RE, _POSIX_USER_PATH_RE):
        working, replaced = user_path.subn("<user>", working)
        count += replaced
    working, dropped = redact_secrets(working)
    return working, count + dropped


# --- provider/git checks ---------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    check: CheckName
    status: Literal["ok", "failed"]
    result: Mapping[str, object]
    checked_at: str
    duration_ms: int


class CheckUnsupported(Exception):
    """The check name is unknown, or provider_cli was asked for while spawning is refused."""


class CheckBusy(Exception):
    """Another check is already running; the caller answers 409 check_in_progress."""


class ProviderChecks:
    """The two non-spawning checks the shell may run one at a time: `provider_cli` (gated on
    `Readiness.may_spawn()`, so it never starts claude.exe while CLAUDE_CONFIG_DIR is set or
    the CLI is missing/unusable) and `git` (never spawns claude.exe at all). Every probe is
    bounded by `timeout_s` and never lets an exception escape as a 500: a timeout or a raised
    exception both come back as `status: "failed"`, an empty version, and `_busy` cleared; a
    raised exception is logged as its exception type name only, never a traceback or message
    (hard rule 8)."""

    def __init__(
        self,
        *,
        readiness: Readiness,
        cli_version: Callable[[], str],
        git_version: Callable[[], str],
        timeout_s: float = 15.0,
    ) -> None:
        self._readiness = readiness
        self._cli_version = cli_version
        self._git_version = git_version
        self._timeout_s = timeout_s
        self._busy = False

    async def run(self, check: str) -> CheckResult:
        name: CheckName
        probe: Callable[[], str]
        observe: Callable[..., None]
        if check == "provider_cli":
            if not self._readiness.may_spawn():
                raise CheckUnsupported(check)
            name = "provider_cli"
            probe = self._cli_version
            observe = self._readiness.observe_version
        elif check == "git":
            name, probe, observe = "git", self._git_version, self._readiness.observe_git
        else:
            raise CheckUnsupported(check)
        if self._busy:
            raise CheckBusy()
        self._busy = True
        started = time.monotonic()
        try:
            version = await asyncio.wait_for(asyncio.to_thread(probe), self._timeout_s)
        except TimeoutError:
            version = ""
        except Exception as exc:
            logger.warning("check %s failed: %s", check, type(exc).__name__)
            version = ""
        finally:
            self._busy = False
        checked_at = _now()
        observe(version, checked_at=checked_at)
        duration_ms = int((time.monotonic() - started) * 1000)
        return CheckResult(
            check=name,
            status="ok" if version else "failed",
            result={"version": version},
            checked_at=checked_at,
            duration_ms=duration_ms,
        )
