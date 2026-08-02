"""Cross-platform NC installer for Python 3.12 and newer."""

from __future__ import annotations

import argparse
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


MINIMUM_PYTHON = (3, 12)
INSTALL_FOLDER_NAME = "NC"
COPY_EXTENSIONS = {".py", ".cmd", ".bat", ".sh", ".command", ".txt", ".md", ".nc"}
COPY_DIRECTORIES = {"examples", "tests", "standart_imports"}
SKIP_NAMES = {"__pycache__", ".git", ".venv", "nc_exe_build", "nc_twin_exe_build"}


class InstallError(RuntimeError):
    pass


def _require_supported_python() -> None:
    if sys.version_info < MINIMUM_PYTHON:
        required = ".".join(map(str, MINIMUM_PYTHON))
        current = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        raise InstallError(f"NC requires Python {required} or newer. Current Python: {current}")
    if sys.maxsize <= 2**32:
        raise InstallError("NC requires a 64-bit Python installation.")


def _default_target() -> Path:
    return Path.home() / INSTALL_FOLDER_NAME


def _venv_python(target: Path) -> Path:
    return target / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == destination.resolve():
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_distribution(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir(), key=lambda path: path.name.lower()):
        if item.name in SKIP_NAMES:
            continue
        if item.is_file() and item.suffix.lower() in COPY_EXTENSIONS:
            _copy_file_atomic(item, target / item.name)
        elif item.is_dir() and item.name in COPY_DIRECTORIES:
            destination_root = target / item.name
            destination_root.mkdir(parents=True, exist_ok=True)
            for child in item.rglob("*"):
                if any(part in SKIP_NAMES for part in child.parts):
                    continue
                relative = child.relative_to(item)
                if child.is_dir():
                    (destination_root / relative).mkdir(parents=True, exist_ok=True)
                elif child.suffix.lower() in COPY_EXTENSIONS or item.name == "standart_imports":
                    _copy_file_atomic(child, destination_root / relative)


def _create_or_update_environment(target: Path, skip_dependencies: bool) -> Path:
    environment = target / ".venv"
    if not _venv_python(target).is_file():
        print(f"Creating isolated Python environment: {environment}")
        venv.EnvBuilder(with_pip=True, clear=False, symlinks=(os.name != "nt")).create(environment)
    python = _venv_python(target)
    if not python.is_file():
        raise InstallError(f"The NC Python environment was not created correctly: {python}")
    if not skip_dependencies:
        requirements = target / "requirements.txt"
        if not requirements.is_file():
            raise InstallError(f"Missing dependency file: {requirements}")
        print("Installing or updating NC runtime dependencies...")
        subprocess.check_call([os.fspath(python), "-m", "pip", "install", "--upgrade", "pip"])
        subprocess.check_call(
            [os.fspath(python), "-m", "pip", "install", "--upgrade", "-r", os.fspath(requirements)]
        )
    return python


def _append_windows_user_path(folder: Path) -> bool:
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
        try:
            try:
                current, value_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, value_type = "", winreg.REG_EXPAND_SZ
            entries = [entry.strip() for entry in str(current).split(";") if entry.strip()]
            normalized = {os.path.normcase(os.path.normpath(os.path.expandvars(entry))) for entry in entries}
            wanted = os.path.normcase(os.path.normpath(os.fspath(folder)))
            if wanted in normalized:
                return False
            entries.append(os.fspath(folder))
            winreg.SetValueEx(key, "Path", 0, value_type, ";".join(entries))
            return True
        finally:
            winreg.CloseKey(key)
    except OSError as error:
        raise InstallError(f"Could not update the Windows user PATH: {error}") from error


def _write_posix_launcher(path: Path, python: Path, script: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "#!/bin/sh\nexec "
        + shlex.quote(os.fspath(python))
        + " "
        + shlex.quote(os.fspath(script))
        + ' "$@"\n'
    )
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _ensure_posix_path(bin_folder: Path) -> list[Path]:
    changed: list[Path] = []
    marker = "# Added by the NC installer"
    line = 'export PATH="$HOME/.local/bin:$PATH"'
    profile_candidates = [Path.home() / ".profile"]
    if platform.system() == "Darwin":
        profile_candidates.append(Path.home() / ".zprofile")
    for profile in profile_candidates:
        existing = profile.read_text(encoding="utf-8", errors="replace") if profile.exists() else ""
        if os.fspath(bin_folder) in os.environ.get("PATH", "").split(os.pathsep) or line in existing:
            continue
        separator = "" if not existing or existing.endswith("\n") else "\n"
        with profile.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(separator + marker + "\n" + line + "\n")
        changed.append(profile)
    return changed


def _install_commands(target: Path, python: Path, update_path: bool) -> None:
    if os.name == "nt":
        if update_path and _append_windows_user_path(target):
            print("Added the NC folder to the Windows user PATH.")
        return
    user_bin = Path.home() / ".local" / "bin"
    _write_posix_launcher(user_bin / "nc", python, target / "nc_console.py")
    _write_posix_launcher(user_bin / "ncw", python, target / "nc_twin_run.py")
    if update_path:
        changed = _ensure_posix_path(user_bin)
        for profile in changed:
            print(f"Added ~/.local/bin to PATH in {profile}")


def _self_test(target: Path, python: Path) -> None:
    check = (
        "import nc; "
        "env=nc.run_text('import physics2d\\nlet w = physics2d.world()\\nlet b = w.circle()\\nw.step()\\n', "
        "enable_ui=False, source_name='<installer-self-test>'); "
        "assert env['b'].position()[1] < 0"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.fspath(target)
    subprocess.check_call([os.fspath(python), "-c", check], cwd=target, env=environment)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install NC and its nc/ncw commands.")
    parser.add_argument("--target", type=Path, default=_default_target(), help="Installation folder (default: ~/NC)")
    parser.add_argument("--skip-dependencies", action="store_true", help="Do not install Python packages")
    parser.add_argument("--no-path", action="store_true", help="Do not add the command folder to the user PATH")
    parser.add_argument("--no-self-test", action="store_true", help="Skip the post-installation self-test")
    args = parser.parse_args(argv)

    try:
        _require_supported_python()
        source = Path(__file__).resolve().parent
        target = args.target.expanduser().resolve()
        print(f"Installing NC from {source}")
        print(f"Destination: {target}")
        _copy_distribution(source, target)
        python = _create_or_update_environment(target, bool(args.skip_dependencies))
        _install_commands(target, python, update_path=not args.no_path)
        if not args.no_self_test:
            print("Running NC self-test...")
            _self_test(target, python)
        print("NC installation completed successfully.")
        print("Open a new terminal, then run: nc <file.nc> or ncw <file.nc>")
        return 0
    except (InstallError, OSError, subprocess.CalledProcessError) as error:
        print(f"NC INSTALL ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
