# NC Physics API

## Units

| Quantity | Unit |
|---|---|
| position, size, radius, distance | metre (`m`) |
| time | second (`s`) |
| mass | kilogram (`kg`) |
| force | newton (`N`) |
| torque | newton-metre (`N m`) |
| impulse | newton-second (`N s`) |
| linear velocity | metre per second (`m/s`) |
| angular values | radians, radians per second |

The default gravity magnitude is `9.80665 m/s^2`.

NC expressions do not need keyword arguments. Extensible features therefore
use an options dictionary.

## Physics 2D

```nc
import physics2d

let world = physics2d.world({
  "gravity": [0, -9.80665],
  "fixed_step": 0.008333333333333333,
  "solver_iterations": 10
})

let ball = world.circle(0.5, 1.2, [0, 3], {
  "id": "ball",
  "velocity": [1, 0],
  "linear_damping": 0.02,
  "material": {"friction": 0.5, "restitution": 0.7},
  "color": "#38bdf8"
})

let floor = world.static_box(12, 1, [0, -2])
world.simulate(1)
print ball.position(), ball.velocity()
```

### World creation

- `physics2d.world(options)`
- `world.circle(radius, mass, position, options)`
- `world.box(width, height, mass, position, options)`
- `world.static_box(width, height, position, options)`
- `world.polygon(vertices, mass, position, options)`; vertices must be convex
- `world.distance_joint(first, second, rest_length, stiffness, damping)`

World options: `gravity`, `fixed_step`, `solver_iterations`, `air_density`, and
`max_bodies`.

Body options: `id`, `body_type`, `velocity`, `angle`, `angular_velocity`,
`gravity_scale`, `linear_damping`, `angular_damping`, `drag_coefficient`,
`drag_area`, `sensor`, `material`, `image`, `image_size`, `color`, and
`user_data`.

### Body methods

- `position()`, `set_position(value)`
- `velocity()`, `set_velocity(value)`
- `angle()`, `set_angle(radians)`
- `angular_velocity()`, `set_angular_velocity(value)`
- `mass()`
- `apply_force(force, point)`
- `apply_torque(torque)`
- `apply_impulse(impulse, point)`
- `set_image(path, world_size)`, `clear_image()`
- `info()`

Supported images are PNG, JPG/JPEG, WebP, and SVG. `world_size` is measured in
metres and is independent of the source pixel size.

## Physics 3D

```nc
import physics3d

let world = physics3d.world()
let ground = world.plane([0, 0, 1], 0)
let crate = world.box([1, 1, 1], 5, [0, 0, 4], {
  "model": "models/crate.glb",
  "model_scale": [1, 1, 1]
})
world.simulate(1)
```

### World creation

- `physics3d.world(options)`
- `world.sphere(radius, mass, position, options)`
- `world.box(size_xyz, mass, position, options)`
- `world.static_box(size_xyz, position, options)`
- `world.plane(normal, offset, options)`
- `world.distance_joint(first, second, rest_length, stiffness, damping)`
- `world.cloth(options)`

3D bodies expose the same force, impulse, mass, position, velocity, material,
and callback concepts as 2D. Rotation and angular velocity are XYZ lists in
radians. `set_model(path, scale)` accepts GLB, glTF, and OBJ. GLB/glTF loading
uses `panda3d-gltf`; OBJ has a built-in geometry loader.

## Cloth

```nc
let cloth = world.cloth({
  "columns": 24,
  "rows": 15,
  "size": [3.8, 2.2],
  "origin": [-2, 0, 4.8],
  "orientation": "vertical",
  "pinned": "top",
  "mass": 0.9,
  "wind": [0, 5.5, 0.4],
  "structural_stiffness": 0.96,
  "shear_stiffness": 0.82,
  "bend_stiffness": 0.3,
  "damping": 0.015,
  "solver_iterations": 8,
  "substeps": 2,
  "thickness": 0.015,
  "drag_coefficient": 1.2,
  "texture": "textures/flag.png"
})
```

`pinned` accepts `"none"`, `"top"`, `"top_corners"`, a list of flat particle
indices, or a list of `[column, row]` coordinates.

Cloth methods:

- `set_wind(velocity)`
- `apply_force(force)`
- `pin(column, row)`, `unpin(column, row)`
- `set_texture(path)`
- `vertices()`, `triangles()`, `info()`

This is cloth-surface physics only. There are no liquid, gas, or generic
soft-body factory functions in this version.

## Simulation and callbacks

Both worlds support:

- `step(delta)`; omit `delta` to use `fixed_step`
- `simulate(seconds)`
- `advance(real_seconds)` for real-time accumulators
- `gravity()`, `set_gravity(value)`
- `add_force_rule(callback)`
- `on_fixed_step(callback)`
- `on_collision(callback)`
- `body(id)`, `remove(item)`, `clear()`, `snapshot()`
- `app(options)`

A force rule may return a 2D or 3D force vector:

```nc
fn wind_force(body, world, delta):
  ret [4, 0]

world.add_force_rule(wind_force)
```

A collision callback receives `first`, `second`, IDs, contact normal,
penetration, and contact point.

## Rendering

`physics2d.app` uses PySide6. `physics3d.app` uses Panda3D. Both renderers call
the fixed-step accumulator and never substitute display FPS for simulation
time. Headless worlds work without either renderer.
