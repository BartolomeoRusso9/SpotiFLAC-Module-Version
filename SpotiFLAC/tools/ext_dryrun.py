"""Extension dry-run — validates an extension you're developing *without*
installing it into your real ~/.spotiflac/extensions, contacting any
registry, or making a real download request.

Checks performed (each recorded as a pass/fail in the returned report):
  - manifest.json exists and parses as JSON
  - required fields present (name, version, runtime)
  - the entry point file the manifest points at actually exists
  - Python: the entry point imports cleanly and exposes exactly one
    BaseProvider subclass — the same requirement
    extensions/python_provider.py enforces at real load time
  - JavaScript: `node --check` on the entry point (syntax only, never
    executed) if `node` is on PATH, plus a plain-text check that it calls
    `registerExtension(...)` somewhere (extensions/_bridge.js's contract)

Used by `spotiflac --ext-dry-run PATH` (see launcher.py); also usable
directly:

    from SpotiFLAC.tools.ext_dryrun import dry_run
    report = dry_run("./my-provider")
    print(report.summary())
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class DryRunReport:
    target: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(CheckResult(name, ok, detail))

    def summary(self) -> str:
        lines = [f"Dry run: {self.target}", ""]
        for c in self.checks:
            icon = "✅" if c.ok else "❌"
            lines.append(f"  {icon} {c.name}" + (f" — {c.detail}" if c.detail else ""))
        lines.append("")
        lines.append("PASSED" if self.passed else "FAILED")
        return "\n".join(lines)


_REQUIRED_MANIFEST_FIELDS = ("name", "version", "runtime")


def _validate_manifest(manifest: dict, report: DryRunReport) -> str | None:
    """Returns the runtime string if the manifest is usable, else None."""
    missing = [f for f in _REQUIRED_MANIFEST_FIELDS if not manifest.get(f)]
    if missing:
        report.add(
            "manifest required fields",
            False,
            f"missing: {', '.join(missing)}",
        )
        return None
    report.add("manifest required fields", True)

    runtime = manifest["runtime"]
    if runtime not in ("python", "javascript"):
        report.add("manifest runtime", False, f"unknown runtime '{runtime}'")
        return None
    report.add("manifest runtime", True, runtime)
    return runtime


def _validate_python_entry(entry_point: Path, report: DryRunReport) -> None:
    import importlib.util

    if not entry_point.exists():
        report.add("entry point exists", False, str(entry_point))
        return
    report.add("entry point exists", True, str(entry_point))

    module_name = f"_spotiflac_dryrun_{entry_point.stem}"
    spec = importlib.util.spec_from_file_location(module_name, entry_point)
    if spec is None or spec.loader is None:
        report.add("entry point imports", False, "could not create module spec")
        return
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        report.add("entry point imports", False, f"{type(exc).__name__}: {exc}")
        return
    report.add("entry point imports", True)

    from SpotiFLAC.core.base import BaseProvider

    candidates = [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, BaseProvider)
        and value is not BaseProvider
    ]
    if len(candidates) != 1:
        report.add(
            "exposes exactly one BaseProvider subclass",
            False,
            f"found {len(candidates)}: {[c.__name__ for c in candidates]}",
        )
    else:
        report.add(
            "exposes exactly one BaseProvider subclass", True, candidates[0].__name__
        )


def _validate_javascript_entry(entry_point: Path, report: DryRunReport) -> None:
    if not entry_point.exists():
        report.add("entry point exists", False, str(entry_point))
        return
    report.add("entry point exists", True, str(entry_point))

    text = entry_point.read_text(encoding="utf-8", errors="replace")
    report.add(
        "calls registerExtension()",
        "registerExtension(" in text,
        "" if "registerExtension(" in text else "no registerExtension(...) call found",
    )

    node = shutil.which("node")
    if node is None:
        report.add(
            "node --check (syntax)",
            True,
            "skipped: node not on PATH — install Node.js to enable this check",
        )
        return

    proc = subprocess.run(
        [node, "--check", str(entry_point)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    report.add(
        "node --check (syntax)",
        proc.returncode == 0,
        proc.stderr.strip()[:300] if proc.returncode != 0 else "",
    )


def _dry_run_manifest_and_entry(
    manifest: dict, ext_root: Path, report: DryRunReport
) -> None:
    runtime = _validate_manifest(manifest, report)
    if runtime is None:
        return
    entry_point = ext_root / manifest.get("entryPoint", "index.js")
    if runtime == "python":
        _validate_python_entry(entry_point, report)
    else:
        _validate_javascript_entry(entry_point, report)


def _dry_run_directory(path: Path) -> DryRunReport:
    report = DryRunReport(target=str(path))
    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        report.add("manifest.json exists", False, str(manifest_path))
        return report
    report.add("manifest.json exists", True)

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        report.add("manifest.json parses as JSON", False, str(exc))
        return report
    report.add("manifest.json parses as JSON", True)

    _dry_run_manifest_and_entry(manifest, path, report)
    return report


def _dry_run_zip(path: Path) -> DryRunReport:
    report = DryRunReport(target=str(path))

    if not zipfile.is_zipfile(path):
        report.add("valid ZIP archive", False)
        return report
    report.add("valid ZIP archive", True)

    tmp_dir = Path(tempfile.mkdtemp(prefix="spotiflac-ext-dryrun-"))
    try:
        from SpotiFLAC.extensions.manager import ExtensionManager

        mgr = ExtensionManager(ext_dir=tmp_dir, auto_install_downloads=False)
        try:
            installed = mgr.install_from_file(path)
        except Exception as exc:
            report.add("installs into a scratch directory", False, str(exc))
            return report
        report.add("installs into a scratch directory", True, installed.name)

        _dry_run_manifest_and_entry(installed.manifest, installed.ext_dir, report)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return report


def dry_run(path: str | Path) -> DryRunReport:
    """Validates the extension at `path` (a source directory, or a packaged
    .spotiflac-ext/.sflx ZIP) without installing it for real or touching
    the network.
    """
    p = Path(path)
    if not p.exists():
        report = DryRunReport(target=str(p))
        report.add("path exists", False, str(p))
        return report

    if p.is_dir():
        return _dry_run_directory(p)
    return _dry_run_zip(p)
