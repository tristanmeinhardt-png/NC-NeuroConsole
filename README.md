# NeuroConsole (NC) 1.2.0 Alpha 2

NC is an indentation-based programming language and runtime for readable
programs, UI applications, simulations, local tools, and web endpoints. This
release adds the first native real-world physics architecture, a direct UI API,
structured diagnostics, and cross-platform installation.

## What is new

- `physics2d`: deterministic SI-unit rigid-body simulation with circles,
  convex polygons, boxes, impulses, forces, torque, friction, restitution,
  air drag, distance joints, custom force rules, images, and a PySide6 view.
- `physics3d`: deterministic SI-unit rigid-body simulation with spheres, boxes,
  planes, impulses, forces, torque, friction, restitution, air drag, joints,
  GLB/glTF/OBJ visual models, and a Panda3D view.
- 3D cloth physics: pinned cloth grids, structural/shear/bending constraints,
  damping, aerodynamic wind, rigid-body collision, and optional image texture.
- `ui.app`: windows programmed directly in NC with layouts, text, buttons,
  input, checkboxes, choices, sliders, progress, images, tables, canvas, timers,
  keyboard events, and callbacks.
- English structured diagnostics with error codes, source excerpts, column
  markers, help, import chains, NC call stacks, and related/cascading errors.
- Installers for Windows, Linux, and macOS. They create an isolated Python
  environment and install `nc` and `ncw` commands.
- Horizontal console rows such as `button "A" button "B"`,
  `print "A" print "B"`, `input "Name" input "City"`, and paired checkmarks.
- Button preparation statements before `action:` using `let`, `set`, or plain
  assignment such as `counter = counter + 1`.
- Version-aware installation as `NC` or `NC (version)`. The default `nc`
  command selects the newest installed version; `nc --1.2.0-alpha.2` selects
  one exact version.
- Standalone packaging with `--exe`, macOS `--dmg`, and Linux `--appimage`.

This alpha intentionally does **not** include generic soft bodies, liquids, or
gases. Cloth is the only deformable-matter system in this version.

## Install

Python 3.12 or newer (64-bit) must already be installed.

- Windows: run `install_nc.cmd` or `install_nc.bat`.
- Linux: run `chmod +x install_nc.sh && ./install_nc.sh`.
- macOS: double-click `Install_NC.command`, or run it from Terminal.

The default installation folder is `~/NC` on every platform. The installer
preserves `~/NC/standart_imports`, installs dependencies into `~/NC/.venv`,
adds the command folder to the user PATH, and runs a self-test.

After opening a new terminal:

```text
nc program.nc
ncw graphical_program.nc
nc --version
```

If another NC version is already installed, the installer asks whether to
overwrite it or install the new version beside it. For scripts and CI, use:

```text
python install_nc.py --overwrite
python install_nc.py --additional
nc --1.2.0-alpha.2 program.nc
```

## Horizontal console syntax

```nc
button "Play" button "Settings" button "Exit"
print "Score:" print score
input "Name" input "Country"
(sound) = checkmark "Sound" (music) = checkmark "Music"
```

In an interactive terminal, use left/right and Enter. A selected input is
stored in its named variable or in `__last_input__`; a selected inline button
is stored in `__last_button__`.

Button actions can prepare values before `action:`:

```nc
let counter = 0
button "Add":
  let message = "Added"
  counter = counter + 1
  set counter = counter + 1
  action:
    print message
    print counter
```

## Standalone conversion

```text
nc program.nc --exe
```

The generated executable contains the NC runtime and the main `.nc` source,
so NC does not need to be installed on the target computer. External imports
and assets still need to be included in the project bundle. On native build
systems, use `nc program.nc --dmg` on macOS or `nc program.nc --appimage` on
Linux. NC rejects those package options on the wrong operating system.

## First examples

```text
ncw examples/physics2d_balls.nc
ncw examples/physics3d_cloth.nc
ncw examples/physics3d_arena.nc
ncw examples/ui_counter.nc
```

## Verify the source tree

```text
python -m unittest discover -s tests -v
```

The tests run headlessly. GUI imports are lazy, so physics and language tests
do not require a display server.

## Documentation

- `ARCHITECTURE.md`: runtime boundaries, processing chain, determinism, and
  compatibility rules.
- `PHYSICS_API.md`: complete first-version physics API and units.
- `UI_APP_API.md`: direct UI programming API.
- `DIAGNOSTICS.md`: error-code families and formatting rules.
- `INSTALLING.md`: installation behavior and platform support.

## Accuracy statement

NC uses classical mechanics, SI units, finite fixed time steps, floating-point
numbers, and iterative constraint/contact solvers. That can produce controlled,
repeatable, physically meaningful simulations, but no finite computer
simulation reproduces reality with literal 100% mathematical accuracy. The
time step, solver iteration count, collision approximation, and material
parameters determine the numerical error.
