# Installing NC

## Requirements

- 64-bit Python 3.12 or newer
- Windows 10/11, a current Linux distribution, or a current macOS release
- internet access during the first dependency installation

The full NC 1.2 runtime uses current Qt/PySide6 and Panda3D builds. Windows
Vista, 7, 8, and 8.1 cannot run the full Python 3.12-based distribution. The
included `.bat` file is a Windows command-script format alternative; it cannot
remove that Python/Qt operating-system limitation. A separate reduced legacy
runtime would require its own older Python dependency set and support policy.

## Windows

Run either:

```text
install_nc.cmd
install_nc.bat
```

The installer uses the Python Launcher (`py -3.12`) when available. It installs
to `%USERPROFILE%\NC` and adds that folder to the user PATH. Open a new Command
Prompt or PowerShell window after installation.

## Linux

```text
chmod +x install_nc.sh
./install_nc.sh
```

The installer creates `~/.local/bin/nc` and `~/.local/bin/ncw`. If necessary,
it adds `~/.local/bin` to `~/.profile`.

## macOS

Double-click `Install_NC.command` or run:

```text
chmod +x Install_NC.command
./Install_NC.command
```

The command wrappers are stored in `~/.local/bin`. The installer can add that
folder to `.profile` and `.zprofile`.

## Common options

```text
python install_nc.py --target /custom/path
python install_nc.py --skip-dependencies
python install_nc.py --no-path
python install_nc.py --no-self-test
```

The installer copies distribution-owned files atomically. It merges rather
than deletes `standart_imports`, so user modules remain intact. Dependencies
live only inside `~/NC/.venv`.
