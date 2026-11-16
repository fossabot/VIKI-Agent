"""Record the server's third-party licence facts at compile time (ADR-054, ADR-055).

``scripts/compile.py`` calls `write` right after it confirms Nuitka produced the standalone
binary. The JSON it writes is the one source of truth `desktop/build/notices.mjs` later reads to
compose ``THIRD-PARTY-NOTICES.txt``: which Python distributions are in the server, what licence
texts each one ships, what each one's embedded CycloneDX SBOM says about bundled native code, the
CPython and Nuitka runtime licences, and which native files under the compiled tree trace to none
of the above (``unattributed``) and so need a reviewed row before a release can ship.

Stdlib only: this runs inside the build environment right after Nuitka exits, with no guarantee
any project dependency is importable there.
"""

from __future__ import annotations

import importlib.metadata
import json
import platform
import re
import sys
import sysconfig
from collections.abc import Callable, Iterable
from pathlib import Path

SCHEMA = 1

_EXCLUDED_DISTRIBUTIONS = frozenset({"viki-agent", "nuitka"})
_INLINE_COPY_LIBS = ("zlib", "zstd", "python_hacl")

# Hoisted out of `collect`'s signature (ruff B008: a default must not itself call a function).
_STDLIB = Path(sysconfig.get_path("stdlib"))

# Matches a dist-info top-level file that carries licence text under the legacy (pre PEP 639)
# convention, where such files sat directly in the dist-info folder instead of its `licenses/`.
_LICENCE_FILENAME = re.compile(r"(?i)^(licen[cs]e|copying|notice)")

# A file Nuitka would have copied as a standalone native module or shared library: a Windows
# `.pyd`/`.dll`, a macOS `.dylib`, or an SOVERSION-suffixed `.so` such as `libgomp.so.1`.
_NATIVE_FILE = re.compile(r"(?i)\.(pyd|dll|dylib)$|\.so(\.\d+)*$")


def _pep503(name: str) -> str:
    """Normalise a distribution name per PEP 503, for comparisons and inventory keys."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _key(name: str, version: str) -> str:
    return f"pypi:{_pep503(name)}@{version}"


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _licence_files(dist: importlib.metadata.Distribution) -> dict[str, str]:
    """This distribution's licence texts: RECORD entries under its dist-info ``licenses/``
    (PEP 639) plus top-level dist-info files matching `_LICENCE_FILENAME` (the legacy layout).
    """
    files: dict[str, str] = {}
    for entry in dist.files or ():
        parts = entry.parts
        if len(parts) < 2 or not parts[0].endswith(".dist-info"):
            continue
        under_licenses_dir = len(parts) >= 3 and parts[1] == "licenses"
        legacy_top_level = len(parts) == 2 and _LICENCE_FILENAME.match(parts[1])
        if not (under_licenses_dir or legacy_top_level):
            continue
        location = Path(entry.locate())
        if not location.is_file():
            continue
        files[str(entry)] = _decode(location.read_bytes())
    return files


def _licence_names(licenses: Iterable[dict[str, object]]) -> list[str]:
    """The licence ids/names/expressions a CycloneDX component's ``licenses`` array lists."""
    names: list[str] = []
    for item in licenses:
        if "expression" in item:
            names.append(str(item["expression"]))
            continue
        license_ = item.get("license")
        if isinstance(license_, dict):
            name = license_.get("id") or license_.get("name")
            if name is not None:
                names.append(str(name))
    return names


def _embedded_sboms(dist: importlib.metadata.Distribution) -> list[dict[str, object]]:
    """One entry per ``<dist-info>/sboms/*.json`` file this distribution ships.

    A CycloneDX document is summarised to each component's name, version and licences; anything
    else (a different SBOM format, or a file that fails to parse as JSON) keeps the file on
    record with ``components: null`` rather than being silently dropped.
    """
    embedded: list[dict[str, object]] = []
    for entry in dist.files or ():
        parts = entry.parts
        if len(parts) != 3 or not parts[0].endswith(".dist-info") or parts[1] != "sboms":
            continue
        if not parts[2].endswith(".json"):
            continue
        location = Path(entry.locate())
        if not location.is_file():
            continue
        try:
            bom = json.loads(location.read_bytes().decode("utf-8"))
        except ValueError:
            bom = {}
        components: list[dict[str, object]] | None = None
        if isinstance(bom, dict) and bom.get("bomFormat") == "CycloneDX":
            components = [
                {
                    "name": component.get("name"),
                    "version": component.get("version"),
                    "licences": _licence_names(component.get("licenses") or ()),
                }
                for component in bom.get("components") or ()
            ]
        embedded.append({"file": str(entry), "components": components})
    return embedded


def _distribution_entry(dist: importlib.metadata.Distribution) -> dict[str, object]:
    name = dist.name
    version = dist.version
    metadata = dist.metadata
    classifiers = [
        value for value in metadata.get_all("Classifier") or () if value.startswith("License")
    ]
    return {
        "key": _key(name, version),
        "name": name,
        "version": version,
        "license_expression": metadata.get("License-Expression"),
        "license": metadata.get("License"),
        "classifiers": classifiers,
        "files": _licence_files(dist),
        "embedded": _embedded_sboms(dist),
    }


def _cpython_licence(base_prefix: Path, stdlib: Path) -> str:
    for candidate in (base_prefix / "LICENSE.txt", stdlib / "LICENSE.txt"):
        if candidate.is_file():
            return _decode(candidate.read_bytes())
    raise RuntimeError(f"no CPython LICENSE.txt under {base_prefix} or {stdlib}")


def _inline_copy_licences(dist: importlib.metadata.Distribution) -> dict[str, str]:
    """Licence files for the zlib/zstd/python_hacl copies Nuitka links into its runtime."""
    files: dict[str, str] = {}
    inline_copy = Path(dist.locate_file("nuitka/build/inline_copy"))  # type: ignore[arg-type]
    for name in _INLINE_COPY_LIBS:
        folder = inline_copy / name
        if not folder.is_dir():
            continue
        for child in sorted(folder.iterdir()):
            if child.is_file() and _LICENCE_FILENAME.match(child.name):
                files[f"nuitka/build/inline_copy/{name}/{child.name}"] = _decode(child.read_bytes())
    return files


def _nuitka_entry(all_distributions: list[importlib.metadata.Distribution]) -> dict[str, object]:
    nuitka = next(
        (dist for dist in all_distributions if _pep503(dist.name) == "nuitka"),
        None,
    )
    if nuitka is None:
        raise RuntimeError("nuitka is not installed")
    files = _licence_files(nuitka)
    files.update(_inline_copy_licences(nuitka))
    if not any(Path(path).name == "LICENSE-RUNTIME.txt" for path in files):
        raise RuntimeError("nuitka is installed without a LICENSE-RUNTIME.txt")
    return {"version": nuitka.version, "files": files}


def _cpython_extension_names(base_prefix: Path, stdlib: Path) -> set[str]:
    """Filenames Nuitka could have copied straight out of the CPython install itself, rather
    than out of a pip-installed distribution: the interpreter's own DLLs/extension modules,
    plus `base_prefix/lib`'s files (not recursive), which is where CPython's own shared
    library lives on Linux and macOS (`libpython3.14.so.1.0`).

    Kept to names matching `_NATIVE_FILE` only: on Windows, paths are case-insensitive, so
    `base_prefix / "lib"` resolves to the very same directory as the stdlib `Lib` folder, whose
    plain `.py` module names (``lzma.py``, ``uuid.py``, ``socket.py``) would otherwise join this
    set and let `_matches_known_root`'s stem-prefix rule count a same-named root native file
    (``lzma.dll``) as CPython's own.
    """
    names: set[str] = set()
    for directory in (
        base_prefix,
        base_prefix / "DLLs",
        stdlib / "lib-dynload",
        base_prefix / "lib",
    ):
        if directory.is_dir():
            names.update(
                child.name
                for child in directory.iterdir()
                if child.is_file() and _NATIVE_FILE.search(child.name)
            )
    return names


def _matches_known_root(name: str, stem: str, candidates: set[str]) -> bool:
    """Whether a root-level file is explained by one of `candidates` (RECORD entries or
    CPython-provided filenames), matching exactly or, since Nuitka drops a copied extension
    module's ABI tag (`_pywhispercpp.pyd` from `_pywhispercpp.cp314-win_amd64.pyd`), by the
    tag-dropped file's stem prefixing a candidate.
    """
    if name in candidates:
        return True
    prefix = f"{stem}."
    return any(candidate.startswith(prefix) for candidate in candidates)


def _unattributed(
    dist_dir: Path,
    all_distributions: list[importlib.metadata.Distribution],
    base_prefix: Path,
    stdlib: Path,
) -> list[str]:
    """Native files under `dist_dir` that trace to no installed distribution and no CPython
    file: a gap a reviewed table row must close before a release can ship (fail-closed).

    Nuitka flattens a wheel's `<dist>.libs/` folder (auditwheel/delocate's convention for a
    bundled shared library, e.g. `pillow.libs/libjpeg-31e2ca52.so.62.4.0`) into the dist root,
    dropping the folder. A root-level file is therefore also attributed when its exact file
    name equals the final path component of any installed distribution's RECORD entry, at any
    depth. That check matches by exact name only, never by the stem-prefix fallback that
    `_matches_known_root` applies below, so a file that merely shares a stem with a RECORD
    entry cannot pass.
    """
    top_level_parts: set[str] = set()
    root_entries: set[str] = set()
    record_file_names: set[str] = set()
    for dist in all_distributions:
        for entry in dist.files or ():
            parts = entry.parts
            if not parts:
                continue
            top_level_parts.add(parts[0])
            if len(parts) == 1:
                root_entries.add(str(entry))
            record_file_names.add(parts[-1])

    cpython_names = _cpython_extension_names(base_prefix, stdlib)

    unattributed: list[str] = []
    for path in sorted(p for p in dist_dir.rglob("*") if p.is_file()):
        if not _NATIVE_FILE.search(path.name):
            continue
        relative = path.relative_to(dist_dir)
        parts = relative.parts
        if len(parts) > 1:
            attributed = parts[0] in top_level_parts
        else:
            attributed = _matches_known_root(relative.name, relative.stem, root_entries)
            if not attributed:
                attributed = relative.name in record_file_names
        if not attributed:
            attributed = _matches_known_root(relative.name, relative.stem, cpython_names)
        if not attributed:
            unattributed.append(relative.as_posix())
    return unattributed


def collect(
    dist_dir: Path,
    *,
    distributions: Callable[
        [], Iterable[importlib.metadata.Distribution]
    ] = importlib.metadata.distributions,
    base_prefix: Path = Path(sys.base_prefix),
    stdlib: Path = _STDLIB,
    python_version: str = platform.python_version(),
    platform_name: str = sys.platform,
) -> dict:
    """The licence inventory for a compiled `dist_dir`: the JSON `desktop/build/notices.mjs` reads.

    `distributions`, `base_prefix`, `stdlib`, `python_version` and `platform_name` are
    injectable for tests; production leaves them at their real stdlib defaults.
    """
    all_distributions = list(distributions())
    kept = [dist for dist in all_distributions if _pep503(dist.name) not in _EXCLUDED_DISTRIBUTIONS]
    kept.sort(key=lambda dist: _key(dist.name, dist.version))

    return {
        "schema": SCHEMA,
        "platform": platform_name,
        "python": {
            "version": python_version,
            "licence": _cpython_licence(base_prefix, stdlib),
        },
        "nuitka": _nuitka_entry(all_distributions),
        "distributions": [_distribution_entry(dist) for dist in kept],
        "unattributed": _unattributed(dist_dir, all_distributions, base_prefix, stdlib),
    }


def write(dist_dir: Path, out_file: Path, **kw: object) -> Path:
    """Write `collect`'s inventory for `dist_dir` to `out_file` as UTF-8 JSON and return it.

    `newline="\\n"` keeps the file LF-only: `write_text`'s default `newline=None` would
    otherwise translate "\\n" to `os.linesep`, giving CRLF on Windows and making a Windows
    build's inventory differ byte-for-byte from one built on Linux.
    """
    data = collect(dist_dir, **kw)  # type: ignore[arg-type]
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(
        json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return out_file
