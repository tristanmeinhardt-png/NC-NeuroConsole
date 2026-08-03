# NC 1.2.0 Alpha 2

NC 1.2.0 Alpha 2 extends Alpha 1 with horizontal terminal controls, button
preparation blocks, version-aware installations, native packaging options,
and fixes for relative targets and console physics executables.

## Language changes

```nc
button "A" button "B"
print "A" print "B"
input "Name" input "City"
(sound) = checkmark "Sound" (music) = checkmark "Music"
```

Use left/right and Enter in an interactive terminal. Buttons can prepare
variables before their action:

```nc
button "Add":
  let message = "Added"
  counter = counter + 1
  set counter = counter + 1
  action:
    print message
    print counter
```

## Installation versions

If `~/NC` already contains another version, the installer asks whether to
overwrite it or install beside it as `NC (version)`. Automation can choose
explicitly with `--overwrite` or `--additional`.

The central `nc`/`ncw` commands select the highest installed version. An exact
version can be selected with `nc --1.2.0-alpha.2 program.nc`.

## Conversion

- Windows/Linux/macOS: `nc program.nc --exe`
- macOS: `nc program.nc --dmg` (native macOS `hdiutil` required)
- Linux: `nc program.nc --appimage` (native `appimagetool` required)

The console executable includes the NC runtime and main source file. External
imports and assets need to be bundled with the project as well.

## Required installation files

- `nc.py`, `nc_console.py`, `nc_twin_run.py`, `t_windows.py`
- `nc_runtime_support.py`, `nc_module_registry.py`, `nc_diagnostics.py`
- `nc_physics2d.py`, `nc_physics2d_app.py`
- `nc_physics3d.py`, `nc_physics3d_app.py`
- `nc_ui_app.py`, `nc_server.py`
- `requirements.txt`
- `install_nc.py` and the platform installer for Windows, Linux, or macOS
- `nc.cmd`, `nc.bat`, `ncw.cmd`, `ncw.bat`
- `standart_imports/`

The examples, tests, and documentation are included in the Alpha 2 archive.
