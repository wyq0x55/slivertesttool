"""Build the offline runtime ZIP from a clean x64 Windows CI checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = {
    ".env.example", "requirements.txt", "manage.py", "run_web.py", "run_worker.py",
    "run_collab.py", "scripts/lm_backup.py", "deployment/runtime.py",
    "deployment/Manage-Silver.ps1", "docs/windows-deployment.md",
}
FRONTEND_ASSETS = (
    "vendor/univer/univer.full.umd.js", "vendor/univer/univer.full.umd.css",
    "vendor/univer/esm/univer-core.js", "vendor/collab/collab.umd.js",
    "vendor/sbs/sbs-editor.umd.js", "vendor/sbs/onig.wasm", "js/lanmatrix/editor_qualification.js",
    "js/lanmatrix/ut_tasks.js",
)


def tracked_files() -> set[str]:
    result = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return set(result.decode("utf-8").rstrip("\0").split("\0"))


def write_zip(destination: Path, wheels: Path, manifest: dict) -> None:
    static = ROOT / "app/static"
    esm = static / "vendor/univer/esm"
    files = RUNTIME_FILES | {
        name for name in tracked_files() if name.startswith(("app/", "silver_cli/"))
        and not name.startswith(esm.relative_to(ROOT).as_posix() + "/")
    }
    # Vite replaces its ESM output, including hashed chunks. It also creates
    # ignored gzip sidecars for the current build.
    generated = {path.relative_to(ROOT).as_posix() for path in esm.rglob("*") if path.is_file()}
    generated.update(path.relative_to(ROOT).as_posix() for path in static.rglob("*.gz")
                     if path.with_suffix("").relative_to(ROOT).as_posix() in files | generated)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        for name in sorted(files | generated):
            archive.write(ROOT / name, name)
        for wheel in sorted(wheels.glob("*.whl")):
            archive.write(wheel, f"wheels/{wheel.name}")
        archive.writestr("release.json", json.dumps(manifest, indent=2) + "\n")


def run(*command: str, cwd: Path = ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_id")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if (sysconfig.get_platform() != "win-amd64"
            or sys.implementation.name != "cpython" or sys.version_info[:3] != (3, 10, 18)):
        parser.error("Build on x64 Windows with CPython 3.10.18.")
    # Windows platform markers in the frozen requirements use this environment
    # variable on CPython 3.10, even if the build shell was started without it.
    os.environ.setdefault("PROCESSOR_ARCHITECTURE", "AMD64")
    if not re.fullmatch(r"preview|v\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?", args.release_id):
        parser.error("Use a vMAJOR.MINOR.PATCH tag, an optional prerelease suffix, or preview.")
    for name in FRONTEND_ASSETS:
        if not (ROOT / "app/static" / name).is_file():
            parser.error(f"Missing frontend asset {name}. Run the production frontend build first.")

    version = re.search(r'^__version__ = "([^"\n]+)"', (ROOT / "app/__init__.py").read_text("utf-8"), re.M)[1]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    release = args.release_id if args.release_id != "preview" else f"preview-{revision[:12]}"
    manifest = {"release": release, "app_version": version, "revision": revision}
    args.output.mkdir(parents=True, exist_ok=True)
    filename = f"silver-test-platform-{release}-windows-x64.zip"
    destination = args.output / filename
    with tempfile.TemporaryDirectory(prefix="silver-release-") as temporary:
        stage = Path(temporary)
        wheels = stage / "wheels"
        run(sys.executable, "-m", "pip", "download", "--only-binary=:all:",
            "--dest", str(wheels), "-r", "requirements.txt")
        package = stage / filename
        write_zip(package, wheels, manifest)
        extracted = stage / "extracted"
        with ZipFile(package) as archive:
            archive.extractall(extracted)
        venv = stage / "venv"
        run(sys.executable, "-m", "venv", str(venv))
        python = str(venv / "Scripts/python.exe")
        run(python, "-m", "pip", "install", "--no-index", "--find-links=wheels",
            "-r", "requirements.txt", cwd=extracted)
        subprocess.run([python, "-c", "import run_web, run_worker, run_collab; import app.collab.server"],
                       cwd=extracted, env=os.environ | {"SECRET_KEY": "windows-package-import-smoke"},
                       check=True)
        # Python 3.10 does not have hashlib.file_digest.
        digest = hashlib.sha256()
        with package.open("rb") as source, destination.open("wb") as target:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                target.write(chunk)
        destination.with_suffix(".zip.sha256").write_text(f"{digest.hexdigest()}  {filename}\n", encoding="ascii")
    print(destination)


if __name__ == "__main__":
    main()
