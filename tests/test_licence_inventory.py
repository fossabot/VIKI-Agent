"""Tests for `licence_inventory.collect`/`write`: the compiled server's licence facts."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import sys
import sysconfig
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import licence_inventory  # noqa: E402

_RUNTIME_LICENCE_TEXT = "Nuitka Runtime Library Exception, Version 1.0"


def test_collect_defaults_to_the_real_stdlib_callables() -> None:
    assert licence_inventory.collect.__kwdefaults__ == {
        "distributions": importlib.metadata.distributions,
        "base_prefix": Path(sys.base_prefix),
        "stdlib": Path(sysconfig.get_path("stdlib")),
        "python_version": platform.python_version(),
        "platform_name": sys.platform,
    }


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _distribution(
    root: Path,
    name: str,
    version: str,
    *,
    metadata_extra: str = "",
    files: dict[str, str | bytes] | None = None,
) -> importlib.metadata.PathDistribution:
    """A real `PathDistribution` backed by on-disk files under `root` (a stand-in site-packages
    directory). `importlib.metadata`'s `.files` only reports a RECORD entry whose target exists
    on disk (confirmed against the real stdlib, not assumed), so every path in `files` is
    written for real rather than merely listed.
    """
    dist_info = root / f"{name}-{version}.dist-info"
    _write(
        dist_info / "METADATA",
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{metadata_extra}",
    )
    record_lines = [f"{name}-{version}.dist-info/METADATA,,"]
    for rel_path, content in (files or {}).items():
        _write(root / rel_path, content)
        record_lines.append(f"{rel_path},,")
    _write(dist_info / "RECORD", "\n".join(record_lines) + "\n")
    return importlib.metadata.PathDistribution(dist_info)


def _base_prefix(tmp_path: Path, text: str = "CPython licence text") -> Path:
    base = tmp_path / "base_prefix"
    _write(base / "LICENSE.txt", text)
    return base


def _nuitka(root: Path, *, runtime_licence: bool = True) -> importlib.metadata.PathDistribution:
    files: dict[str, str | bytes] = {}
    if runtime_licence:
        files["nuitka-4.2.2.dist-info/licenses/LICENSE-RUNTIME.txt"] = _RUNTIME_LICENCE_TEXT
    return _distribution(root, "nuitka", "4.2.2", files=files)


def test_collect_reports_licence_files_fields_and_classifiers_for_a_distribution(
    tmp_path: Path,
) -> None:
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    dist.mkdir()
    certifi = _distribution(
        site_packages,
        "certifi",
        "2026.7.22",
        metadata_extra=(
            "License-Expression: MPL-2.0 AND MIT\n"
            "Classifier: License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)\n"
            "Classifier: Programming Language :: Python :: 3\n"
        ),
        files={
            # PEP 639 location (any depth under licenses/) ...
            "certifi-2026.7.22.dist-info/licenses/LICENSE.txt": "MPL text",
            # ... plus the legacy top-level dist-info convention.
            "certifi-2026.7.22.dist-info/COPYING": "legacy notice text",
        },
    )

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages), certifi],
        base_prefix=_base_prefix(tmp_path),
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="win32",
    )

    [entry] = result["distributions"]
    assert entry["key"] == "pypi:certifi@2026.7.22"
    assert entry["name"] == "certifi"
    assert entry["version"] == "2026.7.22"
    assert entry["license_expression"] == "MPL-2.0 AND MIT"
    assert entry["license"] is None
    assert entry["classifiers"] == [
        "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)"
    ]
    assert entry["files"] == {
        "certifi-2026.7.22.dist-info/licenses/LICENSE.txt": "MPL text",
        "certifi-2026.7.22.dist-info/COPYING": "legacy notice text",
    }
    assert entry["embedded"] == []


def test_collect_excludes_viki_agent_and_nuitka_and_sorts_the_rest_by_key(
    tmp_path: Path,
) -> None:
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    dist.mkdir()
    viki_agent = _distribution(site_packages, "viki-agent", "1.0.0")
    zeta = _distribution(site_packages, "zeta", "1.0")
    alpha = _distribution(site_packages, "Alpha", "2.0")  # mixed-case name

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages), viki_agent, zeta, alpha],
        base_prefix=_base_prefix(tmp_path),
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="win32",
    )

    assert [entry["key"] for entry in result["distributions"]] == [
        "pypi:alpha@2.0",
        "pypi:zeta@1.0",
    ]
    assert result["nuitka"]["version"] == "4.2.2"


def test_collect_summarises_a_cyclonedx_sbom_and_keeps_others_with_null_components(
    tmp_path: Path,
) -> None:
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    dist.mkdir()
    cyclonedx = json.dumps(
        {
            "bomFormat": "CycloneDX",
            "components": [
                {
                    "name": "openssl",
                    "version": "4.0.2",
                    "licenses": [{"license": {"id": "Apache-2.0"}}],
                },
                {
                    "name": "some-crate",
                    "version": "1.2.3",
                    "licenses": [{"expression": "MIT OR Apache-2.0"}],
                },
            ],
        }
    )
    other_format = json.dumps({"bomFormat": "SPDX", "components": [{"name": "ignored"}]})
    cryptography = _distribution(
        site_packages,
        "cryptography",
        "50.0.1",
        files={
            "cryptography-50.0.1.dist-info/sboms/cryptography.cdx.json": cyclonedx,
            "cryptography-50.0.1.dist-info/sboms/other.json": other_format,
        },
    )

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages), cryptography],
        base_prefix=_base_prefix(tmp_path),
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="win32",
    )

    [entry] = result["distributions"]
    embedded = {item["file"]: item for item in entry["embedded"]}
    cdx = embedded["cryptography-50.0.1.dist-info/sboms/cryptography.cdx.json"]
    assert cdx["components"] == [
        {"name": "openssl", "version": "4.0.2", "licences": ["Apache-2.0"]},
        {"name": "some-crate", "version": "1.2.3", "licences": ["MIT OR Apache-2.0"]},
    ]
    assert embedded["cryptography-50.0.1.dist-info/sboms/other.json"]["components"] is None


def test_collect_reads_the_cpython_licence_from_base_prefix(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    dist.mkdir()

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages)],
        base_prefix=_base_prefix(tmp_path, "BASE PREFIX LICENCE"),
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="win32",
    )

    assert result["python"] == {"version": "3.14.7", "licence": "BASE PREFIX LICENCE"}


def test_collect_falls_back_to_the_stdlib_licence(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    dist.mkdir()
    base_prefix = tmp_path / "base_prefix"
    base_prefix.mkdir()
    stdlib = tmp_path / "stdlib"
    _write(stdlib / "LICENSE.txt", "STDLIB LICENCE")

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages)],
        base_prefix=base_prefix,
        stdlib=stdlib,
        python_version="3.14.7",
        platform_name="win32",
    )

    assert result["python"]["licence"] == "STDLIB LICENCE"


def test_collect_raises_when_no_cpython_licence_is_found(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    dist.mkdir()
    base_prefix = tmp_path / "base_prefix"
    base_prefix.mkdir()

    with pytest.raises(RuntimeError, match="LICENSE.txt"):
        licence_inventory.collect(
            dist,
            distributions=lambda: [_nuitka(site_packages)],
            base_prefix=base_prefix,
            stdlib=tmp_path / "stdlib",
            python_version="3.14.7",
            platform_name="win32",
        )


def test_collect_raises_when_nuitka_is_not_installed(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()

    with pytest.raises(RuntimeError, match="nuitka is not installed"):
        licence_inventory.collect(
            dist,
            distributions=lambda: [],
            base_prefix=_base_prefix(tmp_path),
            stdlib=tmp_path / "stdlib",
            python_version="3.14.7",
            platform_name="win32",
        )


def test_collect_raises_when_nuitka_has_no_license_runtime_txt(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    dist.mkdir()
    nuitka = _nuitka(site_packages, runtime_licence=False)

    with pytest.raises(RuntimeError, match="LICENSE-RUNTIME.txt"):
        licence_inventory.collect(
            dist,
            distributions=lambda: [nuitka],
            base_prefix=_base_prefix(tmp_path),
            stdlib=tmp_path / "stdlib",
            python_version="3.14.7",
            platform_name="win32",
        )


def test_collect_builds_the_nuitka_entry_with_inline_copy_licences(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    dist.mkdir()
    nuitka = _nuitka(site_packages)
    _write(
        site_packages / "nuitka" / "build" / "inline_copy" / "zlib" / "LICENSE",
        "zlib licence text",
    )
    _write(
        site_packages / "nuitka" / "build" / "inline_copy" / "python_hacl" / "LICENSE.txt",
        "python_hacl licence text",
    )

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [nuitka],
        base_prefix=_base_prefix(tmp_path),
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="win32",
    )

    assert result["nuitka"] == {
        "version": "4.2.2",
        "files": {
            "nuitka-4.2.2.dist-info/licenses/LICENSE-RUNTIME.txt": _RUNTIME_LICENCE_TEXT,
            "nuitka/build/inline_copy/zlib/LICENSE": "zlib licence text",
            "nuitka/build/inline_copy/python_hacl/LICENSE.txt": "python_hacl licence text",
        },
    }


def test_collect_attributes_a_nested_native_file_by_its_distributions_top_level_part(
    tmp_path: Path,
) -> None:
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    numpy = _distribution(site_packages, "numpy", "2.5.2", files={"numpy/__init__.py": ""})
    _write(dist / "numpy" / "core" / "_multiarray_umath.pyd", b"compiled")

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages), numpy],
        base_prefix=_base_prefix(tmp_path),
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="win32",
    )

    assert result["unattributed"] == []


def test_collect_attributes_a_root_native_file_by_an_exact_record_match(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    extra = _distribution(site_packages, "some-pkg", "1.0", files={"support.dll": b"x"})
    _write(dist / "support.dll", b"compiled")

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages), extra],
        base_prefix=_base_prefix(tmp_path),
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="win32",
    )

    assert result["unattributed"] == []


def test_collect_attributes_a_root_native_file_whose_abi_tag_nuitka_dropped(
    tmp_path: Path,
) -> None:
    """Nuitka copies an extension module with its ABI tag dropped, so the file this
    project ships as `_pywhispercpp.pyd` was installed as `_pywhispercpp.cp314-win_amd64.pyd`.
    """
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    pywhispercpp = _distribution(
        site_packages,
        "pywhispercpp",
        "1.5.1",
        files={"_pywhispercpp.cp314-win_amd64.pyd": b"x"},
    )
    _write(dist / "_pywhispercpp.pyd", b"compiled, tag dropped")

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages), pywhispercpp],
        base_prefix=_base_prefix(tmp_path),
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="win32",
    )

    assert result["unattributed"] == []


def test_collect_attributes_a_root_native_file_flattened_from_a_dist_libs_folder(
    tmp_path: Path,
) -> None:
    """Nuitka flattens a wheel's `<dist>.libs/` folder (auditwheel's convention for a
    bundled shared library, e.g. `pillow.libs/libjpeg-31e2ca52.so.62.4.0`) into the dist root,
    so a root-level file is attributed when its exact name matches that nested RECORD entry."""
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    pillow = _distribution(
        site_packages,
        "pillow",
        "12.3.0",
        files={"pillow.libs/libjpeg-31e2ca52.so.62.4.0": b"x"},
    )
    _write(dist / "libjpeg-31e2ca52.so.62.4.0", b"flattened by nuitka")

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages), pillow],
        base_prefix=_base_prefix(tmp_path),
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="linux",
    )

    assert result["unattributed"] == []


def test_collect_does_not_attribute_a_root_native_file_by_stem_alone(tmp_path: Path) -> None:
    """The `<dist>.libs/` match above is by exact file name only. A file that merely shares a
    stem with a nested RECORD entry (the stem-prefix fallback `_matches_known_root` applies to
    root-level RECORD entries and CPython names would wrongly pass) must stay unattributed."""
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    pillow = _distribution(
        site_packages,
        "pillow",
        "12.3.0",
        files={"pillow.libs/libfoo-1a2b3c4d.so.1.2.3": b"x"},
    )
    _write(dist / "libfoo-1a2b3c4d.so.9", b"different file, same stem")

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages), pillow],
        base_prefix=_base_prefix(tmp_path),
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="linux",
    )

    assert result["unattributed"] == ["libfoo-1a2b3c4d.so.9"]


def test_collect_attributes_a_cpython_dll_present_in_base_prefix(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    base_prefix = _base_prefix(tmp_path)
    _write(base_prefix / "python314.dll", b"real cpython dll")
    _write(dist / "python314.dll", b"copied by nuitka")

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages)],
        base_prefix=base_prefix,
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="win32",
    )

    assert result["unattributed"] == []


def test_collect_attributes_a_cpython_shared_library_under_base_prefix_lib(
    tmp_path: Path,
) -> None:
    """`base_prefix/lib` is where CPython's own shared library lives on Linux and macOS
    (`libpython3.14.so.1.0`), copied to the dist root the same way Windows' `python314.dll` is
    (the test above)."""
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    base_prefix = _base_prefix(tmp_path)
    _write(base_prefix / "lib" / "libpython3.14.so.1.0", b"real cpython shared library")
    _write(dist / "libpython3.14.so.1.0", b"copied by nuitka")

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages)],
        base_prefix=base_prefix,
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="linux",
    )

    assert result["unattributed"] == []


def test_collect_does_not_attribute_a_root_native_file_by_a_same_named_stdlib_module(
    tmp_path: Path,
) -> None:
    """On Windows, paths are case-insensitive, so `base_prefix / "lib"` (what
    `_cpython_extension_names` scans) resolves to the very same directory as the stdlib `Lib`
    folder. Its plain `.py` module names must not let `_matches_known_root`'s stem-prefix rule
    count a root native file named after a stdlib module (`lzma.dll`) as CPython's own."""
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    base_prefix = _base_prefix(tmp_path)
    _write(base_prefix / "Lib" / "lzma.py", "stdlib module source, not a licence file")
    _write(dist / "lzma.dll", b"compiled, unrelated to the stdlib lzma module")

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages)],
        base_prefix=base_prefix,
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="win32",
    )

    assert result["unattributed"] == ["lzma.dll"]


def test_collect_reports_an_unattributed_native_file(tmp_path: Path) -> None:
    """A sentinel: if attribution ever degenerated into "everything is attributed", this is the
    case that would wrongly stop showing up here."""
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    _write(dist / "mystery" / "libfoo.so.6", b"nobody claims this")

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages)],
        base_prefix=_base_prefix(tmp_path),
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="win32",
    )

    assert result["unattributed"] == ["mystery/libfoo.so.6"]


def test_collect_reports_an_unattributed_root_native_file(tmp_path: Path) -> None:
    """The root-level counterpart of the sentinel above: a root file matching no RECORD entry,
    under either the existing root-entry rule or the dist-libs rule, stays unattributed."""
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    _write(dist / "libbar-99.so.1", b"nobody claims this either")

    result = licence_inventory.collect(
        dist,
        distributions=lambda: [_nuitka(site_packages)],
        base_prefix=_base_prefix(tmp_path),
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="linux",
    )

    assert result["unattributed"] == ["libbar-99.so.1"]


def test_write_produces_sorted_utf8_json_with_a_trailing_newline(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    dist.mkdir()
    out_file = tmp_path / "out" / "viki-agent-licences.json"

    returned = licence_inventory.write(
        dist,
        out_file,
        distributions=lambda: [_nuitka(site_packages)],
        base_prefix=_base_prefix(tmp_path),
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="win32",
    )

    assert returned == out_file
    text = out_file.read_text(encoding="utf-8")
    assert text.endswith("\n")
    data = json.loads(text)
    assert data["schema"] == 1
    assert data["platform"] == "win32"
    assert text == json.dumps(data, sort_keys=True, indent=2) + "\n"


def test_write_writes_lf_line_endings_even_on_windows(tmp_path: Path) -> None:
    """`Path.write_text`'s default `newline=None` translates every "\\n" to `os.linesep`,
    which is "\\r\\n" on Windows; the inventory is LF (`.gitattributes` keeps it), and a
    Windows-built inventory must byte-for-byte match one built on Linux."""
    site_packages = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    dist.mkdir()
    out_file = tmp_path / "out" / "viki-agent-licences.json"

    licence_inventory.write(
        dist,
        out_file,
        distributions=lambda: [_nuitka(site_packages)],
        base_prefix=_base_prefix(tmp_path),
        stdlib=tmp_path / "stdlib",
        python_version="3.14.7",
        platform_name="win32",
    )

    raw = out_file.read_bytes()
    assert b"\r" not in raw
