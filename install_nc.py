"""Cross-platform NC installer for Python 3.12 and newer."""

from __future__ import annotations

import argparse
import os
import platform
import re
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
COMMAND_FOLDER_NAME = "NC-bin"
VERSION_FILE_NAME = "version.txt"


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


def _distribution_version(source: Path) -> str:
    match = re.search(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", (source / "nc.py").read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise InstallError("Could not determine the NC version from nc.py")
    return match.group(1).strip()


def _safe_version_name(version: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._+-]+", "_", str(version).strip())
    return name.strip(" .") or "unknown"


def _version_key(version: str) -> tuple:
    """Compare common NC versions without depending on packaging libraries."""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-.]?(.*))?$", str(version).strip())
    if not match:
        return (0, 0, 0, 0, ((1, str(version)),))
    suffix = match.group(4) or ""
    tokens = []
    for token in re.split(r"[.-]+", suffix):
        if not token:
            continue
        tokens.append((0, int(token)) if token.isdigit() else (1, token.lower()))
    # A release without a suffix sorts after alpha/beta/rc versions.
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 1 if not suffix else 0, tuple(tokens))


def _installed_version(target: Path) -> str | None:
    marker = target / VERSION_FILE_NAME
    if marker.is_file():
        value = marker.read_text(encoding="utf-8", errors="replace").strip()
        if value:
            return value
    try:
        return _distribution_version(target)
    except Exception:
        return None


def _select_install_target(requested: Path | None, version: str, mode: str) -> Path:
    """Choose overwrite or the required ``NC (version)`` side-by-side path."""
    if requested is not None:
        if mode != "ask":
            raise InstallError("--overwrite/--additional cannot be combined with an explicit --target")
        return requested.expanduser().resolve()

    primary = _default_target().resolve()
    if not primary.exists():
        return primary

    old_version = _installed_version(primary)
    if old_version == version:
        return primary

    if mode == "overwrite":
        return primary
    if mode == "additional":
        return primary.parent / f"{primary.name} ({_safe_version_name(version)})"

    if not sys.stdin.isatty():
        old_label = old_version or "unbekannte Version"
        raise InstallError(
            f"{primary} already contains {old_label}. Use --overwrite to replace it "
            f"or --additional to install {version} beside it."
        )

    old_label = old_version or "unbekannte Version"
    print(f"An older NC installation was found: {old_label}")
    print(f"New version: {version}")
    answer = input("[O]verwrite the old installation or [A]dd a side-by-side installation? [A/o]: ").strip().lower()
    if answer.startswith("o"):
        return primary
    return primary.parent / f"{primary.name} ({_safe_version_name(version)})"


def _write_version_marker(target: Path, version: str) -> None:
    (target / VERSION_FILE_NAME).write_text(str(version).strip() + "\n", encoding="utf-8")


def _newest_installed_target(current_target: Path) -> Path:
    candidates = [current_target.resolve(), _default_target().resolve()]
    home = Path.home()
    candidates.extend(
        path.resolve()
        for path in home.glob(f"{INSTALL_FOLDER_NAME} (*)")
        if path.is_dir()
    )
    unique: dict[str, Path] = {}
    for candidate in candidates:
        if candidate.is_dir():
            unique[os.path.normcase(os.fspath(candidate))] = candidate
    available = [(path, _installed_version(path)) for path in unique.values()]
    available = [(path, version) for path, version in available if version]
    if not available:
        return current_target.resolve()
    return max(available, key=lambda item: _version_key(str(item[1])))[0]


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


def _ensure_windows_user_path_first(folder: Path) -> bool:
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
            entries = [entry for entry in entries if os.path.normcase(os.path.normpath(os.path.expandvars(entry))) != wanted]
            entries.insert(0, os.fspath(folder))
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


def _write_windows_dispatcher(path: Path, script_name: str) -> None:
    path.write_text(
        "@echo off\n"
        "setlocal\n"
        "set \"NC_COMMAND_ROOT=%~dp0\"\n"
        "set \"NC_TARGET=\"\n"
        "set \"NC_SELECTOR=%~1\"\n"
        "if \"%NC_SELECTOR:~0,2%\"==\"--\" if exist \"%USERPROFILE%\\NC (%NC_SELECTOR:~2%)\\version.txt\" set \"NC_TARGET=%USERPROFILE%\\NC (%NC_SELECTOR:~2%)\"\n"
        "if \"%NC_SELECTOR:~0,2%\"==\"--\" if exist \"%USERPROFILE%\\NC\\version.txt\" for /f \"usebackq delims=\" %%V in (\"%USERPROFILE%\\NC\\version.txt\") do if /i \"%%V\"==\"%NC_SELECTOR:~2%\" set \"NC_TARGET=%USERPROFILE%\\NC\"\n"
        "if not defined NC_TARGET if exist \"%NC_COMMAND_ROOT%latest.txt\" set /p NC_TARGET=<\"%NC_COMMAND_ROOT%latest.txt\"\n"
        "if not defined NC_TARGET set \"NC_TARGET=%USERPROFILE%\\NC\"\n"
        "if not exist \"%NC_TARGET%\\.venv\\Scripts\\python.exe\" (\n"
        "  echo NC version target not found: %NC_TARGET% 1>&2\n"
        "  exit /b 1\n"
        ")\n"
        f'"%NC_TARGET%\\.venv\\Scripts\\python.exe" "%NC_TARGET%\\{script_name}" %*\n'
        "set \"NC_RESULT=%errorlevel%\"\n"
        "endlocal & exit /b %NC_RESULT%\n",
        encoding="utf-8",
        newline="\r\n",
    )


def _write_posix_dispatcher(path: Path, script_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\nset -eu\n"
        'COMMAND_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'TARGET=""\n'
        'if [ "$#" -gt 0 ]; then\n'
        '  case "$1" in\n'
        '    --*) CANDIDATE=${1#--}; if [ -f "$HOME/NC ($CANDIDATE)/version.txt" ]; then TARGET="$HOME/NC ($CANDIDATE)"; elif [ -f "$HOME/NC/version.txt" ] && [ "$(cat "$HOME/NC/version.txt")" = "$CANDIDATE" ]; then TARGET="$HOME/NC"; fi ;;\n'
        '  esac\n'
        'fi\n'
        'if [ -z "$TARGET" ] && [ -f "$COMMAND_ROOT/latest.txt" ]; then TARGET=$(cat "$COMMAND_ROOT/latest.txt"); fi\n'
        'if [ -z "$TARGET" ]; then TARGET="$HOME/NC"; fi\n'
        'PYTHON="$TARGET/.venv/bin/python"\n'
        'if [ ! -x "$PYTHON" ]; then echo "NC version target not found: $TARGET" >&2; exit 1; fi\n'
        'exec "$PYTHON" "$TARGET/' + script_name + '" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    path.chmod(0o755)


def _install_commands(target: Path, python: Path, version: str, update_path: bool) -> None:
    command_folder = Path.home() / COMMAND_FOLDER_NAME
    command_folder.mkdir(parents=True, exist_ok=True)
    newest_target = _newest_installed_target(target)
    (command_folder / "latest.txt").write_text(os.fspath(newest_target) + "\n", encoding="utf-8")
    if os.name == "nt":
        _write_windows_dispatcher(command_folder / "nc.cmd", "nc_console.py")
        _write_windows_dispatcher(command_folder / "ncw.cmd", "nc_twin_run.py")
        _write_windows_dispatcher(command_folder / "nc.bat", "nc_console.py")
        _write_windows_dispatcher(command_folder / "ncw.bat", "nc_twin_run.py")
        if update_path and _ensure_windows_user_path_first(command_folder):
            print("Added the NC command folder first in the Windows user PATH.")
        return
    user_bin = Path.home() / ".local" / "bin"
    _write_posix_dispatcher(user_bin / "nc", "nc_console.py")
    _write_posix_dispatcher(user_bin / "ncw", "nc_twin_run.py")
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
    parser.add_argument("--target", type=Path, default=None, help="Explicit installation folder")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--overwrite", action="store_true", help="Replace the existing default NC installation")
    mode.add_argument("--additional", action="store_true", help="Install beside the old version as NC (version)")
    parser.add_argument("--skip-dependencies", action="store_true", help="Do not install Python packages")
    parser.add_argument("--no-path", action="store_true", help="Do not add the command folder to the user PATH")
    parser.add_argument("--no-self-test", action="store_true", help="Skip the post-installation self-test")
    args = parser.parse_args(argv)

    try:
        _require_supported_python()
        source = Path(__file__).resolve().parent
        version = _distribution_version(source)
        install_mode = "overwrite" if args.overwrite else "additional" if args.additional else "ask"
        target = _select_install_target(args.target, version, install_mode)
        print(f"Installing NC from {source}")
        print(f"Version: {version}")
        print(f"Destination: {target}")
        _copy_distribution(source, target)
        _write_version_marker(target, version)
        python = _create_or_update_environment(target, bool(args.skip_dependencies))
        _install_commands(target, python, version, update_path=not args.no_path)
        if not args.no_self_test:
            print("Running NC self-test...")
            _self_test(target, python)
        print("NC installation completed successfully.")
        print("Open a new terminal, then run: nc <file.nc> or ncw <file.nc>")
        print(f"The default nc command now selects the newest installed version; use nc --{version} for this version explicitly.")
        return 0
    except (InstallError, OSError, subprocess.CalledProcessError) as error:
        print(f"NC INSTALL ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
