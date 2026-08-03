# Changelog

## 1.2.0 Alpha 2

- Added horizontal console rows for `button`, `checkmark`, `print`, and
  `input`, navigated with left/right arrow keys.
- Added button preparation statements before `action:` using `let`, `set`, and
  plain assignment.
- Added side-by-side version installation as `NC (version)`.
- Added central version-aware `nc`/`ncw` dispatchers and exact version
  selectors.
- Added native `--dmg` and `--appimage` packaging entry points with platform
  checks.
- Fixed relative target lookup from the NC installation directory.
- Fixed console EXE packaging to include the main NC source and suppress
  physics windows in console executables.
- Removed the Windows installer's hard-coded `py -3.12` selection.

## 1.2.0 Alpha 1

- Added lazy native-module registry and shared runtime validation layer.
- Added deterministic `physics2d` rigid-body world and PySide6 renderer.
- Added deterministic `physics3d` rigid-body world and Panda3D renderer.
- Added dedicated 3D cloth physics; explicitly excluded liquids, gases, and
  generic soft bodies from this release.
- Added PNG/JPG/WebP/SVG images and GLB/glTF/OBJ model resources with separate
  visual and collision geometry.
- Added direct event-driven `ui.app` API.
- Added structured English diagnostics, error codes, source markers, help,
  cause relationships, import chains, and NC call stacks.
- Removed duplicate parser-error printing from embedded runs.
- Fixed `nc --no-log` reading `text` before the target was loaded.
- Replaced misleading first-letter name suggestions with edit-distance matches.
- Removed hard-coded `C:\Users\meinh` paths from launch and inspection tools.
- Added Windows, Linux, and macOS installers and portable `nc`/`ncw` launchers.
- Made PyInstaller executable path detection cross-platform.
- Added headless tests and runnable examples.
