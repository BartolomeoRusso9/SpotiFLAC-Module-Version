"""Tests for the extension scaffold + dry-run tools (--ext-scaffold /
--ext-dry-run). End-to-end: scaffold a real extension skeleton to a temp
dir, then dry-run it — no mocking of the tools' own logic, only of
whatever external environment quirk would make a test flaky (node's
presence is checked, not assumed).
"""

from __future__ import annotations

import json
import shutil
import zipfile

import pytest

from SpotiFLAC.tools.ext_dryrun import dry_run
from SpotiFLAC.tools.ext_scaffold import scaffold_extension


def test_scaffold_python_extension_creates_expected_files(tmp_path):
    target = scaffold_extension("my-provider", runtime="python", output_dir=tmp_path)
    assert target == tmp_path / "my-provider"
    assert (target / "manifest.json").exists()
    assert (target / "my_provider.py").exists()
    assert (target / "README.md").exists()

    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["name"] == "my-provider"
    assert manifest["runtime"] == "python"
    assert manifest["entryPoint"] == "my_provider.py"


def test_scaffold_javascript_extension_creates_expected_files(tmp_path):
    target = scaffold_extension("my-js-ext", runtime="javascript", output_dir=tmp_path)
    assert (target / "manifest.json").exists()
    assert (target / "index.js").exists()

    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["runtime"] == "javascript"
    assert manifest["entryPoint"] == "index.js"
    assert "registerExtension(" in (target / "index.js").read_text()


def test_scaffold_rejects_unknown_runtime(tmp_path):
    with pytest.raises(ValueError):
        scaffold_extension("x", runtime="rust", output_dir=tmp_path)


def test_scaffold_refuses_to_overwrite_existing_directory(tmp_path):
    scaffold_extension("dup", runtime="python", output_dir=tmp_path)
    with pytest.raises(FileExistsError):
        scaffold_extension("dup", runtime="python", output_dir=tmp_path)


def test_dry_run_passes_for_a_freshly_scaffolded_python_extension(tmp_path):
    target = scaffold_extension("good-py", runtime="python", output_dir=tmp_path)
    report = dry_run(target)
    assert report.passed, report.summary()
    names = {c.name for c in report.checks}
    assert "exposes exactly one BaseProvider subclass" in names


def test_dry_run_passes_for_a_freshly_scaffolded_js_extension(tmp_path):
    target = scaffold_extension("good-js", runtime="javascript", output_dir=tmp_path)
    report = dry_run(target)
    assert report.passed, report.summary()
    names = {c.name for c in report.checks}
    assert "calls registerExtension()" in names


def test_dry_run_fails_on_missing_manifest(tmp_path):
    empty_dir = tmp_path / "no-manifest"
    empty_dir.mkdir()
    report = dry_run(empty_dir)
    assert not report.passed
    assert report.checks[0].name == "manifest.json exists"
    assert report.checks[0].ok is False


def test_dry_run_fails_on_invalid_json(tmp_path):
    d = tmp_path / "bad-json"
    d.mkdir()
    (d / "manifest.json").write_text("{not valid json", encoding="utf-8")
    report = dry_run(d)
    assert not report.passed


def test_dry_run_fails_when_python_entry_has_no_provider_subclass(tmp_path):
    target = scaffold_extension("no-provider", runtime="python", output_dir=tmp_path)
    (target / "no_provider.py").write_text(
        "# no BaseProvider subclass here\nx = 1\n", encoding="utf-8"
    )
    report = dry_run(target)
    assert not report.passed
    failing = [c for c in report.checks if not c.ok]
    assert any("BaseProvider" in c.name for c in failing)


def test_dry_run_fails_on_python_syntax_error(tmp_path):
    target = scaffold_extension("broken-py", runtime="python", output_dir=tmp_path)
    (target / "broken_py.py").write_text("def broken(:\n", encoding="utf-8")
    report = dry_run(target)
    assert not report.passed
    failing_names = {c.name for c in report.checks if not c.ok}
    assert "entry point imports" in failing_names


def test_dry_run_reports_missing_registerextension_call(tmp_path):
    target = scaffold_extension(
        "js-no-register", runtime="javascript", output_dir=tmp_path
    )
    (target / "index.js").write_text("function foo() {}\n", encoding="utf-8")
    report = dry_run(target)
    assert not report.passed
    reg_check = next(c for c in report.checks if c.name == "calls registerExtension()")
    assert reg_check.ok is False


def test_dry_run_reports_node_syntax_errors(tmp_path):
    import shutil as _shutil

    if _shutil.which("node") is None:
        pytest.skip("node not installed in this environment")
    target = scaffold_extension(
        "js-syntax-error", runtime="javascript", output_dir=tmp_path
    )
    (target / "index.js").write_text(
        "function broken( {\nregisterExtension({});\n", encoding="utf-8"
    )
    report = dry_run(target)
    assert not report.passed
    syntax_check = next(c for c in report.checks if c.name == "node --check (syntax)")
    assert syntax_check.ok is False


def test_dry_run_missing_path_reports_cleanly(tmp_path):
    report = dry_run(tmp_path / "does-not-exist")
    assert not report.passed
    assert report.checks[0].name == "path exists"


def test_dry_run_on_a_packaged_zip(tmp_path):
    """Package a scaffolded extension into a .spotiflac-ext ZIP (the real
    distribution format) and dry-run *that*, exercising the
    ExtensionManager.install_from_file() path rather than the plain-
    directory one.
    """
    target = scaffold_extension("zipped-py", runtime="python", output_dir=tmp_path)
    zip_path = tmp_path / "zipped-py.spotiflac-ext"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in target.iterdir():
            zf.write(f, arcname=f.name)
    shutil.rmtree(target)  # prove the dry-run doesn't need the source dir anymore

    report = dry_run(zip_path)
    assert report.passed, report.summary()
    names = {c.name for c in report.checks}
    assert "installs into a scratch directory" in names
