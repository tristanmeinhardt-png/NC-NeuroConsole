# Changelog

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
