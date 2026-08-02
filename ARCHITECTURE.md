# NC 1.2 Architecture

## Processing chain

Every language feature is considered across the complete NC chain:

```text
source -> logical-line folding -> parser -> Stmt representation
       -> safe expression analysis -> interpreter -> native runtime module
       -> output / renderer / external effect
```

NC 1.2 does not introduce new physics-specific syntax. Physics and direct UI
are ordinary builtin modules. This avoids coupling simulation behavior to the
parser and keeps the language grammar backward compatible.

## Core and native modules

`nc.py` remains the compatibility core for the existing NC language. New large
systems are not appended to that file. They use these boundaries:

- `nc_module_registry.py`: lazy builtin-module discovery.
- `nc_runtime_support.py`: safe callable markers, callback adaptation,
  validation, resources, vectors, identifiers, and fixed-step clock.
- `nc_physics2d.py`: headless 2D state and solver.
- `nc_physics2d_app.py`: optional PySide6 rendering and input.
- `nc_physics3d.py`: headless 3D state, solver, and cloth.
- `nc_physics3d_app.py`: optional Panda3D rendering and model loading.
- `nc_ui_app.py`: direct event-driven UI object model and lazy Qt adapter.
- `nc_diagnostics.py`: source registry, classification, relation analysis, and
  formatting.

Builtin modules are loaded on first import. A console-only program therefore
does not import PySide6 or Panda3D.

## Physics state and rendering are separate

Physics uses metres, kilograms, seconds, radians, newtons, newton-metres, and
newton-seconds. Images use pixels and models use their asset coordinate system.
A renderer converts between those spaces explicitly.

This separation guarantees that resizing a window, changing monitor DPI, or
replacing a detailed model does not change mass, gravity, collision geometry,
or simulation timing.

Visible resources never silently become collision geometry:

- a 2D body has a circle or convex polygon collider and may have an image;
- a 3D body has a sphere, box, or plane collider and may have a model;
- cloth owns a simulation grid and may have an image texture.

## Time model and determinism

`world.step()` executes one physics step. `world.simulate(seconds)` uses the
world's fixed step and a final remainder. Real-time renderers call
`world.advance(real_seconds)`, which feeds a bounded accumulator and executes
zero or more fixed steps.

Given the same platform, Python build, starting state, callback results, and
step sequence, the headless solvers are deterministic. Rendering frame rate is
not used as the physics time step.

Default standard gravity is exactly `9.80665 m/s^2`.

## Solver scope

The builtin 2D solver supports circles and convex polygons with separating-axis
collision detection and angular impulse response.

The builtin 3D solver supports spheres, planes, and axis-aligned box contacts.
Boxes can rotate visually and carry angular state, but builtin box collision is
currently axis-aligned. Arbitrarily oriented box collision is reserved for a
later compatible backend; the public NC API will not need to change.

The cloth solver is position-based and includes structural, shear, and bending
constraints. Cloth collides with rigid bodies, but the current cloth-to-rigid
coupling is one-way: rigid bodies affect cloth, while cloth does not yet transfer
equal-and-opposite momentum back to rigid bodies. This limitation is explicit
so programs do not mistake an approximation for exact two-way coupling.

## Custom physics

Custom behavior is introduced through world callbacks rather than parser
special cases:

- `add_force_rule(callback)` can return an additional force for each body;
- `on_fixed_step(callback)` runs before every simulation step;
- `on_collision(callback)` receives a stable collision-event dictionary.

NC callbacks may declare fewer arguments than an event provides. This permits
simple zero-argument UI handlers while retaining richer event payloads when
needed. Declaring more arguments than an event provides is an error.

## Diagnostics

The parser registers in-memory source text before parsing. Errors are classified
only at the diagnostic boundary, so legacy parser/runtime exceptions can retain
their behavior while receiving stable codes and source excerpts.

`NCMultiError` performs a relation pass. For example, an indented line following
a missing block colon is marked as a consequence of `NC-S1001`, rather than
presented as an unrelated second mistake.

NC functions retain their definition source, line, and base directory. Runtime
errors therefore preserve the real source location and build an NC-level call
stack without exposing a Python traceback in normal operation.

## Compatibility policy

- Existing NC syntax is unchanged.
- New modules use ordinary `import` statements.
- Existing `ui.window`, TWIN, MathNC, NCE, server, and compatibility helpers
  remain available.
- Refactoring of older compatibility layers must be behavior-preserving and
  guarded by regression tests; it is intentionally separate from the physics
  feature implementation.
