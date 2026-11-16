"""Product wheels contain exactly the validated UI staged from a completed frontend build."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_product  # noqa: E402

from viki_agent import winproc  # noqa: E402


def _fixture(repo: Path) -> tuple[Path, Path]:
    source = repo / "frontend" / "dist"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_bytes(b"<script src='/assets/app-AbCd1234.js'></script>")
    (source / "assets" / "app-AbCd1234.js").write_bytes(b"console.log('app')")
    icon = repo / "frontend" / "public" / "favicon.ico"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"current-icon")
    (source / "favicon.ico").write_bytes(b"old-icon")
    return source, icon


def _write_wheel(stage: Path, out: Path, *, code: bytes = b"current code") -> Path:
    wheel_path = out / "viki_agent-0.1.0-py3-none-any.whl"
    with ZipFile(wheel_path, "w", ZIP_DEFLATED) as wheel:
        wheel.writestr("viki_agent/__main__.py", code)
        for file in stage.rglob("*"):
            if file.is_file():
                wheel.write(file, f"viki_agent/resources/ui/{file.relative_to(stage).as_posix()}")
    return wheel_path


def test_stage_copies_the_checked_ui_and_never_calls_winproc_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("stage must not run a process")

    monkeypatch.setattr(build_product.winproc, "run", boom)
    stage_dir = build_product.stage(tmp_path)

    assert stage_dir == tmp_path / "src" / "viki_agent" / "resources" / "ui"
    assert sorted(
        file.relative_to(stage_dir).as_posix() for file in stage_dir.rglob("*") if file.is_file()
    ) == ["assets/app-AbCd1234.js", "favicon.ico", "index.html"]
    assert (stage_dir / "favicon.ico").read_bytes() == b"current-icon"


def test_product_build_replaces_stale_ui_and_checks_wheel_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    stage = tmp_path / "src" / "viki_agent" / "resources" / "ui"
    stage.mkdir(parents=True)
    (stage / "stale.js").write_bytes(b"stale")
    out = tmp_path / "wheel-output"

    def fake_run(argv: list[str], *, cwd: Path, timeout: float) -> SimpleNamespace:
        assert argv[:5] == ["uv", "build", "--wheel", "--offline", "--out-dir"]
        assert cwd == tmp_path and timeout >= 60
        temporary = Path(argv[-1])
        assert temporary.parent == out and temporary.is_dir()
        with ZipFile(temporary / "viki_agent-0.1.0-py3-none-any.whl", "w", ZIP_DEFLATED) as wheel:
            wheel.write(stage, "viki_agent/resources/ui/")
            wheel.write(stage / "assets", "viki_agent/resources/ui/assets/")
            for file in stage.rglob("*"):
                if file.is_file():
                    wheel.write(
                        file, f"viki_agent/resources/ui/{file.relative_to(stage).as_posix()}"
                    )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_product.winproc, "run", fake_run)
    wheel = build_product.build(tmp_path, out)

    assert wheel == out / "viki_agent-0.1.0-py3-none-any.whl"
    assert sorted(
        file.relative_to(stage).as_posix() for file in stage.rglob("*") if file.is_file()
    ) == [
        "assets/app-AbCd1234.js",
        "favicon.ico",
        "index.html",
    ]
    assert (stage / "favicon.ico").read_bytes() == b"current-icon"


def test_product_build_accepts_copied_public_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _fixture(tmp_path)
    public = tmp_path / "frontend" / "public"
    for relative in ("brand/mark.png", "fonts/local.woff2", "fonts/local.ttf"):
        asset = public / relative
        asset.parent.mkdir(exist_ok=True)
        asset.write_bytes(relative.encode())
        copied = source / relative
        copied.parent.mkdir(exist_ok=True)
        copied.write_bytes(asset.read_bytes())
    stage = tmp_path / "src" / "viki_agent" / "resources" / "ui"

    def fake_run(argv: list[str], *, cwd: Path, timeout: float) -> SimpleNamespace:
        del cwd, timeout
        _write_wheel(stage, Path(argv[-1]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_product.winproc, "run", fake_run)
    build_product.build(tmp_path, tmp_path / "wheel-output")
    assert (stage / "brand" / "mark.png").read_bytes() == b"brand/mark.png"
    assert (stage / "fonts" / "local.woff2").read_bytes() == b"fonts/local.woff2"
    assert (stage / "fonts" / "local.ttf").read_bytes() == b"fonts/local.ttf"


@pytest.mark.parametrize("relative", ["licenses/libraries-dev.txt", "licenses/syncopate.txt"])
def test_product_build_packages_copied_third_party_license(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    source, _ = _fixture(tmp_path)
    notice = b"Synthetic third-party notice"
    public = tmp_path / "frontend" / "public" / relative
    public.parent.mkdir()
    public.write_bytes(notice)
    copied = source / relative
    copied.parent.mkdir()
    copied.write_bytes(notice)
    stage = tmp_path / "src" / "viki_agent" / "resources" / "ui"

    def fake_run(argv: list[str], *, cwd: Path, timeout: float) -> SimpleNamespace:
        del cwd, timeout
        _write_wheel(stage, Path(argv[-1]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_product.winproc, "run", fake_run)
    wheel = build_product.build(tmp_path, tmp_path / "wheel-output")
    assert (stage / relative).read_bytes() == notice
    with ZipFile(wheel) as archive:
        assert archive.read(f"viki_agent/resources/ui/{relative}") == notice


@pytest.mark.parametrize("relative", ["LICENSE.txt", "THIRD_PARTY_NOTICES.txt"])
def test_stage_keeps_product_legal_text_and_refuses_a_changed_copy(
    tmp_path: Path, relative: str
) -> None:
    source, _ = _fixture(tmp_path)
    text = b"Synthetic product legal text\n"
    public = tmp_path / "frontend" / "public" / relative
    public.write_bytes(text)
    copied = source / relative
    copied.write_bytes(text)

    stage = build_product.stage(tmp_path)
    assert (stage / relative).read_bytes() == text

    copied.write_bytes(b"changed legal text")
    with pytest.raises(ValueError, match="unexpected"):
        build_product.stage(tmp_path)
    assert (stage / relative).read_bytes() == text


@pytest.mark.parametrize(
    ("relative", "copied_bytes"),
    [
        ("licenses/libraries-dev.txt", b"changed"),
        ("licenses/syncopate.txt", b"changed"),
        ("licenses/other.txt", b"source"),
    ],
)
def test_product_build_refuses_changed_license_or_sibling_text(
    tmp_path: Path, relative: str, copied_bytes: bytes
) -> None:
    source, _ = _fixture(tmp_path)
    public = tmp_path / "frontend" / "public" / relative
    public.parent.mkdir()
    public.write_bytes(b"source")
    copied = source / relative
    copied.parent.mkdir()
    copied.write_bytes(copied_bytes)
    with pytest.raises(ValueError, match="unexpected"):
        build_product.build(tmp_path, tmp_path / "wheel-output")


def test_product_build_refuses_missing_index_reference(tmp_path: Path) -> None:
    source, _ = _fixture(tmp_path)
    (source / "index.html").write_text(
        '<script src="/assets/missing-AbCd1234.js"></script>', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="referenced"):
        build_product.build(tmp_path, tmp_path / "wheel-output")


def test_product_build_refuses_entry_without_javascript(tmp_path: Path) -> None:
    source, _ = _fixture(tmp_path)
    (source / "index.html").write_text("<main>Incomplete build</main>", encoding="utf-8")
    with pytest.raises(ValueError, match="JavaScript"):
        build_product.build(tmp_path, tmp_path / "wheel-output")


@pytest.mark.parametrize(
    "reference", ['"/assets/missing-AbCd1234.woff2"', "../fonts/missing.woff2"]
)
def test_product_build_refuses_missing_css_font(tmp_path: Path, reference: str) -> None:
    source, _ = _fixture(tmp_path)
    (source / "index.html").write_text(
        '<script src="/assets/app-AbCd1234.js"></script>'
        '<link rel="stylesheet" href="/assets/app-AbCd1234.css">',
        encoding="utf-8",
    )
    (source / "assets" / "app-AbCd1234.css").write_text(
        f"body{{font-face:url({reference})}}", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="referenced"):
        build_product.build(tmp_path, tmp_path / "wheel-output")


def test_product_build_accepts_relative_css_public_font(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _fixture(tmp_path)
    (source / "index.html").write_text(
        '<script src="/assets/app-AbCd1234.js"></script>'
        '<link rel="stylesheet" href="/assets/app-AbCd1234.css">',
        encoding="utf-8",
    )
    (source / "assets" / "app-AbCd1234.css").write_text(
        "@font-face{src:url(../fonts/local.woff2),url('../fonts/local.woff2')} "
        "body{background:url('data:image/svg+xml,%3Csvg%3E')} "
        "a{background:url(https://example.com/background.png)} "
        "/* url('/assets/not-a-real-file.woff2') */",
        encoding="utf-8",
    )
    public_font = tmp_path / "frontend" / "public" / "fonts" / "local.woff2"
    public_font.parent.mkdir()
    public_font.write_bytes(b"font")
    copied_font = source / "fonts" / "local.woff2"
    copied_font.parent.mkdir()
    copied_font.write_bytes(public_font.read_bytes())
    stage = tmp_path / "src" / "viki_agent" / "resources" / "ui"

    def fake_run(argv: list[str], *, cwd: Path, timeout: float) -> SimpleNamespace:
        del cwd, timeout
        _write_wheel(stage, Path(argv[-1]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_product.winproc, "run", fake_run)
    build_product.build(tmp_path, tmp_path / "wheel-output")
    assert (stage / "fonts" / "local.woff2").read_bytes() == b"font"


@pytest.mark.parametrize(
    ("relative", "copied_bytes"),
    [("brand/mark.png", b"different"), ("fonts/local.js.map", b"same")],
)
def test_product_build_refuses_mismatched_or_source_map_public_files(
    tmp_path: Path, relative: str, copied_bytes: bytes
) -> None:
    source, _ = _fixture(tmp_path)
    public = tmp_path / "frontend" / "public" / relative
    public.parent.mkdir(exist_ok=True)
    public.write_bytes(b"same")
    copied = source / relative
    copied.parent.mkdir(exist_ok=True)
    copied.write_bytes(copied_bytes)
    with pytest.raises(ValueError, match="unexpected"):
        build_product.build(tmp_path, tmp_path / "wheel-output")


def test_product_build_refuses_stage_ancestor_link_before_deletion(tmp_path: Path) -> None:
    _fixture(tmp_path)
    victim = tmp_path / "victim"
    (victim / "ui").mkdir(parents=True)
    sentinel = victim / "ui" / "sentinel"
    sentinel.write_bytes(b"keep")
    resources = tmp_path / "src" / "viki_agent" / "resources"
    resources.parent.mkdir(parents=True)
    if sys.platform == "win32":
        result = winproc.run(
            ["cmd", "/c", "mklink", "/J", str(resources), str(victim)], cwd=tmp_path
        )
        assert result.returncode == 0, result.stderr
    else:
        resources.symlink_to(victim, target_is_directory=True)

    with pytest.raises(ValueError, match="link"):
        build_product.build(tmp_path, tmp_path / "wheel-output")
    assert sentinel.read_bytes() == b"keep"


def test_product_build_requires_wheel_from_this_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    stage = tmp_path / "src" / "viki_agent" / "resources" / "ui"
    out = tmp_path / "wheel-output"
    out.mkdir()

    # Build once to create a wheel whose UI matches the staged files.
    def first_run(argv: list[str], *, cwd: Path, timeout: float) -> SimpleNamespace:
        del cwd, timeout
        _write_wheel(stage, Path(argv[-1]), code=b"old code")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_product.winproc, "run", first_run)
    build_product.build(tmp_path, out)
    stale = out / "viki_agent-0.1.0-py3-none-any.whl"
    with ZipFile(stale) as archive:
        assert archive.read("viki_agent/__main__.py") == b"old code"

    def no_artifact(argv: list[str], *, cwd: Path, timeout: float) -> SimpleNamespace:
        del argv, cwd, timeout
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_product.winproc, "run", no_artifact)
    with pytest.raises(RuntimeError, match="wheel"):
        build_product.build(tmp_path, out)
    with ZipFile(stale) as archive:
        assert archive.read("viki_agent/__main__.py") == b"old code"


@pytest.mark.parametrize("missing", ["index.html", "favicon.ico"])
def test_product_build_refuses_missing_inputs(tmp_path: Path, missing: str) -> None:
    source, icon = _fixture(tmp_path)
    (source / missing if missing == "index.html" else icon).unlink()
    with pytest.raises(ValueError, match="missing"):
        build_product.build(tmp_path, tmp_path / "wheel-output")


@pytest.mark.parametrize("unsafe", ["assets/private.js.map", "private.txt"])
def test_product_build_refuses_unexpected_dist_files(tmp_path: Path, unsafe: str) -> None:
    source, _ = _fixture(tmp_path)
    file = source / unsafe
    file.parent.mkdir(exist_ok=True)
    file.write_bytes(b"not-for-publication")
    with pytest.raises(ValueError, match="unexpected"):
        build_product.build(tmp_path, tmp_path / "wheel-output")


def test_product_build_refuses_symlink_input(tmp_path: Path) -> None:
    source, _ = _fixture(tmp_path)
    target = tmp_path / "private.txt"
    target.write_bytes(b"private")
    try:
        (source / "assets" / "private-AbCd1234.js").symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError, match="link"):
        build_product.build(tmp_path, tmp_path / "wheel-output")


@pytest.mark.parametrize("contents", [None, b"wrong"])
def test_product_build_refuses_incomplete_or_mismatched_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: bytes | None
) -> None:
    _fixture(tmp_path)
    out = tmp_path / "wheel-output"
    stage = tmp_path / "src" / "viki_agent" / "resources" / "ui"

    def fake_run(argv: list[str], *, cwd: Path, timeout: float) -> SimpleNamespace:
        del cwd, timeout
        with ZipFile(Path(argv[-1]) / "viki_agent-0.1.0-py3-none-any.whl", "w") as wheel:
            for file in stage.rglob("*"):
                if file.is_file() and (contents is not None or file.name != "favicon.ico"):
                    wheel.writestr(
                        f"viki_agent/resources/ui/{file.relative_to(stage).as_posix()}",
                        contents
                        if contents is not None and file.name == "favicon.ico"
                        else file.read_bytes(),
                    )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_product.winproc, "run", fake_run)
    with pytest.raises(RuntimeError, match="wheel UI"):
        build_product.build(tmp_path, out)


def test_product_build_reports_corrupt_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fixture(tmp_path)
    out = tmp_path / "wheel-output"

    def fake_run(argv: list[str], *, cwd: Path, timeout: float) -> SimpleNamespace:
        del cwd, timeout
        (Path(argv[-1]) / "viki_agent-0.1.0-py3-none-any.whl").write_bytes(b"not-a-wheel")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_product, "REPO", tmp_path)
    monkeypatch.setattr(build_product.winproc, "run", fake_run)
    assert build_product.main(["--out-dir", str(out)]) == 2
    assert "product build refused" in capsys.readouterr().err


@pytest.mark.parametrize(
    "relative", [".env.local", "starter/extra.md", "starter/state.json", "welcome.ja.md"]
)
def test_product_build_refuses_unexpected_resources_before_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    _fixture(tmp_path)
    resource = tmp_path / "src" / "viki_agent" / "resources" / relative
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_bytes(b"synthetic private resource")
    calls: list[object] = []
    monkeypatch.setattr(build_product.winproc, "run", lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError, match="resource"):
        build_product.build(tmp_path, tmp_path / "wheel-output")
    assert calls == []


def test_stage_accepts_only_manifested_non_ui_resources_without_requiring_the_full_starter(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    resources = tmp_path / "src" / "viki_agent" / "resources"
    for relative in (
        "welcome.en.md",
        "starter/AGENTS.template.md",
        "starter/dot-claude/settings.json",
    ):
        path = resources / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic public resource")
    assert build_product.stage(tmp_path).is_dir()


def test_source_resource_directory_link_is_refused_before_copy(tmp_path: Path) -> None:
    _fixture(tmp_path)
    resources = tmp_path / "src" / "viki_agent" / "resources"
    resources.mkdir(parents=True)
    target = tmp_path / "public-starter"
    target.mkdir()
    if sys.platform == "win32":
        result = winproc.run(
            ["cmd", "/c", "mklink", "/J", str(resources / "starter"), str(target)], cwd=tmp_path
        )
        assert result.returncode == 0, result.stderr
    else:
        (resources / "starter").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="link"):
        build_product.stage(tmp_path)


@pytest.mark.parametrize(
    "member",
    [
        "viki_agent/state.json",
        "viki_agent/resources/starter/extra.md",
        ".env.production",
        "viki_agent/private-note.md",
        "private-note.md",
        "another/private-note.md",
    ],
)
def test_product_build_refuses_injected_wheel_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str
) -> None:
    _fixture(tmp_path)
    stage = tmp_path / "src" / "viki_agent" / "resources" / "ui"

    def fake_run(argv: list[str], *, cwd: Path, timeout: float) -> SimpleNamespace:
        del cwd, timeout
        wheel = _write_wheel(stage, Path(argv[-1]))
        with ZipFile(wheel, "a") as archive:
            archive.writestr(member, b"synthetic private payload")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_product.winproc, "run", fake_run)
    with pytest.raises(ValueError, match="release|resource"):
        build_product.build(tmp_path, tmp_path / "wheel-output")
    assert not list((tmp_path / "wheel-output").glob("*.whl"))


def test_product_wheel_accepts_python_modules_and_distribution_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    stage = tmp_path / "src" / "viki_agent" / "resources" / "ui"

    def fake_run(argv: list[str], *, cwd: Path, timeout: float) -> SimpleNamespace:
        del cwd, timeout
        wheel = _write_wheel(stage, Path(argv[-1]))
        with ZipFile(wheel, "a") as archive:
            archive.writestr("viki_agent/helpers.py", b"# synthetic public module")
            archive.writestr("viki_agent-0.1.0.dist-info/METADATA", b"Name: viki-agent\n")
            archive.writestr(
                "viki_agent-0.1.0.dist-info/licenses/LICENSE.txt", b"Synthetic licence"
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_product.winproc, "run", fake_run)
    assert build_product.build(tmp_path, tmp_path / "wheel-output").is_file()
