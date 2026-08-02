"""Deterministic SI-unit 2D physics for NC.

The builtin solver intentionally focuses on transparent, reproducible rigid
body mechanics.  It supports circles and convex polygons (boxes are polygons),
impulses, friction, restitution, gravity, drag, torque, distance springs, and
custom force rules.  Rendering is a separate optional layer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from nc_runtime_support import (
    FixedStepClock,
    IdentifierPool,
    ImageAsset,
    NCConfigurationError,
    add,
    clamp,
    dot,
    finite_number,
    invoke_nc_callback,
    length,
    length_squared,
    load_image_asset,
    nc_callable,
    normalized,
    optional_vector,
    options_dict,
    positive_number,
    scale,
    subtract,
    vector,
)


STANDARD_GRAVITY = 9.80665
_EPSILON = 1e-10


def _cross(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _cross_scalar_vector(value: float, v: Sequence[float]) -> list[float]:
    return [-value * v[1], value * v[0]]


def _rotate(v: Sequence[float], angle: float) -> list[float]:
    cosine = math.cos(angle)
    sine = math.sin(angle)
    return [v[0] * cosine - v[1] * sine, v[0] * sine + v[1] * cosine]


def _perpendicular(v: Sequence[float]) -> list[float]:
    return [-v[1], v[0]]


def _project(vertices: Iterable[Sequence[float]], axis: Sequence[float]) -> tuple[float, float]:
    values = [dot(vertex, axis) for vertex in vertices]
    return min(values), max(values)


def _convex_vertices(raw_vertices: Any) -> list[list[float]]:
    if not isinstance(raw_vertices, (list, tuple)) or len(raw_vertices) < 3:
        raise NCConfigurationError("vertices must contain at least three 2D points")
    vertices = [vector(item, 2, f"vertices[{index}]") for index, item in enumerate(raw_vertices)]

    signed_area = 0.0
    for index, current in enumerate(vertices):
        following = vertices[(index + 1) % len(vertices)]
        signed_area += _cross(current, following)
    if abs(signed_area) <= _EPSILON:
        raise NCConfigurationError("polygon vertices must enclose a non-zero area")
    if signed_area < 0.0:
        vertices.reverse()

    direction = 0
    count = len(vertices)
    for index in range(count):
        a = vertices[index]
        b = vertices[(index + 1) % count]
        c = vertices[(index + 2) % count]
        turn = _cross(subtract(b, a), subtract(c, b))
        if abs(turn) <= _EPSILON:
            continue
        sign = 1 if turn > 0.0 else -1
        if direction == 0:
            direction = sign
        elif sign != direction:
            raise NCConfigurationError(
                "polygon must be convex and its vertices must follow the outside edge"
            )
    if direction == 0:
        raise NCConfigurationError("polygon vertices cannot all be collinear")
    return vertices


@dataclass(frozen=True)
class Material2D:
    restitution: float = 0.1
    static_friction: float = 0.6
    dynamic_friction: float = 0.45

    @classmethod
    def from_options(cls, raw: Any = None) -> "Material2D":
        options = options_dict(raw, "material")
        restitution = clamp(finite_number(options.get("restitution", 0.1), "restitution"), 0.0, 1.0)
        static_friction = positive_number(
            options.get("static_friction", options.get("friction", 0.6)),
            "static_friction",
            allow_zero=True,
        )
        dynamic_friction = positive_number(
            options.get("dynamic_friction", min(static_friction, 0.45)),
            "dynamic_friction",
            allow_zero=True,
        )
        if dynamic_friction > static_friction:
            raise NCConfigurationError("dynamic_friction cannot exceed static_friction")
        return cls(restitution, static_friction, dynamic_friction)

    @nc_callable
    def info(self) -> dict[str, float]:
        return {
            "restitution": self.restitution,
            "static_friction": self.static_friction,
            "dynamic_friction": self.dynamic_friction,
        }


class Body2D:
    """A circle or convex polygon rigid body."""

    def __init__(
        self,
        world: "World2D",
        identifier: str,
        shape: str,
        *,
        position: Sequence[float],
        mass: float,
        body_type: str,
        radius: float | None = None,
        vertices: list[list[float]] | None = None,
        options: dict[str, Any] | None = None,
    ):
        opts = dict(options or {})
        body_type = str(body_type or "dynamic").strip().lower()
        if body_type not in {"dynamic", "static", "kinematic"}:
            raise NCConfigurationError("body_type must be 'dynamic', 'static', or 'kinematic'")
        self.world = world
        self.id = str(identifier)
        self.shape = str(shape)
        self.body_type = body_type
        self._position = list(position)
        self._velocity = optional_vector(opts.get("velocity"), 2, "velocity", [0.0, 0.0])
        self._force = [0.0, 0.0]
        self._angle = finite_number(opts.get("angle", 0.0), "angle")
        self._angular_velocity = finite_number(opts.get("angular_velocity", 0.0), "angular_velocity")
        self._torque = 0.0
        self.radius = radius
        self.local_vertices = vertices
        self.sensor = bool(opts.get("sensor", False))
        self.gravity_scale = finite_number(opts.get("gravity_scale", 1.0), "gravity_scale")
        self.linear_damping = positive_number(opts.get("linear_damping", 0.02), "linear_damping", allow_zero=True)
        self.angular_damping = positive_number(opts.get("angular_damping", 0.02), "angular_damping", allow_zero=True)
        self.drag_coefficient = positive_number(
            opts.get("drag_coefficient", 0.0), "drag_coefficient", allow_zero=True
        )
        self.drag_area = positive_number(opts.get("drag_area", 0.0), "drag_area", allow_zero=True)
        self.material = Material2D.from_options(opts.get("material"))
        self.image_asset: ImageAsset | None = None
        self.image_size = optional_vector(opts.get("image_size"), 2, "image_size", [0.0, 0.0])
        self.color = str(
            opts.get(
                "color",
                "#8ecae6" if body_type == "dynamic" else "#64748b",
            )
        )
        self.user_data = opts.get("user_data")

        actual_mass = positive_number(mass, "mass", allow_zero=(body_type != "dynamic"))
        if body_type == "dynamic" and actual_mass <= 0.0:
            raise NCConfigurationError("A dynamic body must have mass greater than zero")
        self._mass = actual_mass if body_type == "dynamic" else math.inf
        self._inverse_mass = 1.0 / actual_mass if body_type == "dynamic" else 0.0
        self._inertia = self._calculate_inertia(actual_mass) if body_type == "dynamic" else math.inf
        self._inverse_inertia = 1.0 / self._inertia if math.isfinite(self._inertia) and self._inertia > 0 else 0.0

        image_path = opts.get("image")
        if image_path:
            self.set_image(image_path, opts.get("image_size"))

    def _calculate_inertia(self, mass: float) -> float:
        if self.shape == "circle":
            assert self.radius is not None
            return 0.5 * mass * self.radius * self.radius
        vertices = self.local_vertices or []
        min_x = min(vertex[0] for vertex in vertices)
        max_x = max(vertex[0] for vertex in vertices)
        min_y = min(vertex[1] for vertex in vertices)
        max_y = max(vertex[1] for vertex in vertices)
        width = max_x - min_x
        height = max_y - min_y
        return mass * (width * width + height * height) / 12.0

    def _world_vertices(self) -> list[list[float]]:
        if self.shape == "circle":
            return []
        return [add(self._position, _rotate(vertex, self._angle)) for vertex in self.local_vertices or []]

    def _support(self, direction: Sequence[float]) -> list[float]:
        if self.shape == "circle":
            assert self.radius is not None
            return add(self._position, scale(normalized(direction, [1.0, 0.0]), self.radius))
        return max(self._world_vertices(), key=lambda vertex: dot(vertex, direction))

    def _velocity_at(self, point: Sequence[float]) -> list[float]:
        relative = subtract(point, self._position)
        return add(self._velocity, _cross_scalar_vector(self._angular_velocity, relative))

    def _apply_impulse_internal(self, impulse: Sequence[float], point: Sequence[float]) -> None:
        if self.body_type != "dynamic":
            return
        self._velocity = add(self._velocity, scale(impulse, self._inverse_mass))
        relative = subtract(point, self._position)
        self._angular_velocity += _cross(relative, impulse) * self._inverse_inertia

    @nc_callable
    def position(self) -> list[float]:
        return list(self._position)

    @nc_callable
    def set_position(self, value: Any) -> "Body2D":
        self._position = vector(value, 2, "position")
        return self

    @nc_callable
    def velocity(self) -> list[float]:
        return list(self._velocity)

    @nc_callable
    def set_velocity(self, value: Any) -> "Body2D":
        self._velocity = vector(value, 2, "velocity")
        return self

    @nc_callable
    def angle(self) -> float:
        return self._angle

    @nc_callable
    def set_angle(self, radians: Any) -> "Body2D":
        self._angle = finite_number(radians, "angle")
        return self

    @nc_callable
    def angular_velocity(self) -> float:
        return self._angular_velocity

    @nc_callable
    def set_angular_velocity(self, value: Any) -> "Body2D":
        self._angular_velocity = finite_number(value, "angular_velocity")
        return self

    @nc_callable
    def mass(self) -> float:
        return self._mass

    @nc_callable
    def apply_force(self, force: Any, point: Any = None) -> "Body2D":
        if self.body_type != "dynamic":
            return self
        force_vector = vector(force, 2, "force")
        self._force = add(self._force, force_vector)
        if point is not None:
            application_point = vector(point, 2, "point")
            self._torque += _cross(subtract(application_point, self._position), force_vector)
        return self

    @nc_callable
    def apply_torque(self, torque: Any) -> "Body2D":
        if self.body_type == "dynamic":
            self._torque += finite_number(torque, "torque")
        return self

    @nc_callable
    def apply_impulse(self, impulse: Any, point: Any = None) -> "Body2D":
        application_point = self._position if point is None else vector(point, 2, "point")
        self._apply_impulse_internal(vector(impulse, 2, "impulse"), application_point)
        return self

    @nc_callable
    def set_image(self, path: Any, world_size: Any = None) -> "Body2D":
        self.image_asset = load_image_asset(path, self.world.base_dir)
        if world_size is not None:
            size = vector(world_size, 2, "image_size")
            if size[0] <= 0.0 or size[1] <= 0.0:
                raise NCConfigurationError("image_size values must be greater than zero")
            self.image_size = size
        elif self.image_size == [0.0, 0.0]:
            if self.shape == "circle":
                diameter = 2.0 * float(self.radius or 0.5)
                self.image_size = [diameter, diameter]
            else:
                vertices = self.local_vertices or []
                self.image_size = [
                    max(v[0] for v in vertices) - min(v[0] for v in vertices),
                    max(v[1] for v in vertices) - min(v[1] for v in vertices),
                ]
        return self

    @nc_callable
    def clear_image(self) -> "Body2D":
        self.image_asset = None
        return self

    @nc_callable
    def info(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "shape": self.shape,
            "body_type": self.body_type,
            "position": self.position(),
            "velocity": self.velocity(),
            "angle": self._angle,
            "angular_velocity": self._angular_velocity,
            "mass": None if math.isinf(self._mass) else self._mass,
            "sensor": self.sensor,
            "material": self.material.info(),
            "image": self.image_asset.to_dict() if self.image_asset else None,
            "color": self.color,
            "user_data": self.user_data,
        }


@dataclass
class Contact2D:
    first: Body2D
    second: Body2D
    normal: list[float]
    penetration: float
    point: list[float]

    def event(self) -> dict[str, Any]:
        return {
            "first": self.first,
            "second": self.second,
            "first_id": self.first.id,
            "second_id": self.second.id,
            "normal": list(self.normal),
            "penetration": self.penetration,
            "point": list(self.point),
        }


class DistanceJoint2D:
    def __init__(
        self,
        first: Body2D,
        second: Body2D,
        rest_length: float,
        stiffness: float,
        damping: float,
    ):
        if first.world is not second.world:
            raise NCConfigurationError("Both joint bodies must belong to the same world")
        self.first = first
        self.second = second
        self.rest_length = positive_number(rest_length, "rest_length", allow_zero=True)
        self.stiffness = positive_number(stiffness, "stiffness", allow_zero=True)
        self.damping = positive_number(damping, "damping", allow_zero=True)
        self.enabled = True

    def _apply(self) -> None:
        if not self.enabled:
            return
        delta = subtract(self.second._position, self.first._position)
        distance = length(delta)
        if distance <= _EPSILON:
            return
        direction = scale(delta, 1.0 / distance)
        relative_speed = dot(subtract(self.second._velocity, self.first._velocity), direction)
        magnitude = self.stiffness * (distance - self.rest_length) + self.damping * relative_speed
        force = scale(direction, magnitude)
        self.first.apply_force(force)
        self.second.apply_force(scale(force, -1.0))

    @nc_callable
    def set_enabled(self, enabled: Any) -> "DistanceJoint2D":
        self.enabled = bool(enabled)
        return self

    @nc_callable
    def info(self) -> dict[str, Any]:
        return {
            "type": "distance",
            "first_id": self.first.id,
            "second_id": self.second.id,
            "rest_length": self.rest_length,
            "stiffness": self.stiffness,
            "damping": self.damping,
            "enabled": self.enabled,
        }


def _polygon_axes(vertices: Sequence[Sequence[float]]) -> list[list[float]]:
    axes: list[list[float]] = []
    for index, current in enumerate(vertices):
        following = vertices[(index + 1) % len(vertices)]
        edge = subtract(following, current)
        axes.append(normalized(_perpendicular(edge), [1.0, 0.0]))
    return axes


def _polygon_polygon(first: Body2D, second: Body2D) -> Contact2D | None:
    first_vertices = first._world_vertices()
    second_vertices = second._world_vertices()
    smallest_overlap = math.inf
    smallest_axis: list[float] | None = None
    for axis in _polygon_axes(first_vertices) + _polygon_axes(second_vertices):
        first_min, first_max = _project(first_vertices, axis)
        second_min, second_max = _project(second_vertices, axis)
        overlap = min(first_max, second_max) - max(first_min, second_min)
        if overlap <= 0.0:
            return None
        if overlap < smallest_overlap:
            smallest_overlap = overlap
            smallest_axis = list(axis)
    assert smallest_axis is not None
    if dot(subtract(second._position, first._position), smallest_axis) < 0.0:
        smallest_axis = scale(smallest_axis, -1.0)
    support_first = first._support(smallest_axis)
    support_second = second._support(scale(smallest_axis, -1.0))
    point = scale(add(support_first, support_second), 0.5)
    return Contact2D(first, second, smallest_axis, smallest_overlap, point)


def _circle_circle(first: Body2D, second: Body2D) -> Contact2D | None:
    delta = subtract(second._position, first._position)
    distance_sq = length_squared(delta)
    combined = float(first.radius or 0.0) + float(second.radius or 0.0)
    if distance_sq >= combined * combined:
        return None
    if distance_sq <= _EPSILON:
        normal = [1.0, 0.0]
        distance = 0.0
    else:
        distance = math.sqrt(distance_sq)
        normal = scale(delta, 1.0 / distance)
    penetration = combined - distance
    point = add(first._position, scale(normal, float(first.radius or 0.0) - penetration * 0.5))
    return Contact2D(first, second, normal, penetration, point)


def _circle_polygon(circle: Body2D, polygon: Body2D) -> Contact2D | None:
    vertices = polygon._world_vertices()
    axes = _polygon_axes(vertices)
    closest = min(vertices, key=lambda vertex: length_squared(subtract(vertex, circle._position)))
    vertex_axis = normalized(subtract(closest, circle._position), [1.0, 0.0])
    axes.append(vertex_axis)
    smallest_overlap = math.inf
    smallest_axis: list[float] | None = None
    radius = float(circle.radius or 0.0)
    for axis in axes:
        polygon_min, polygon_max = _project(vertices, axis)
        circle_center = dot(circle._position, axis)
        circle_min, circle_max = circle_center - radius, circle_center + radius
        overlap = min(circle_max, polygon_max) - max(circle_min, polygon_min)
        if overlap <= 0.0:
            return None
        if overlap < smallest_overlap:
            smallest_overlap = overlap
            smallest_axis = list(axis)
    assert smallest_axis is not None
    if dot(subtract(polygon._position, circle._position), smallest_axis) < 0.0:
        smallest_axis = scale(smallest_axis, -1.0)
    point = add(circle._position, scale(smallest_axis, radius - smallest_overlap * 0.5))
    return Contact2D(circle, polygon, smallest_axis, smallest_overlap, point)


def _collide(first: Body2D, second: Body2D) -> Contact2D | None:
    if first.shape == "circle" and second.shape == "circle":
        return _circle_circle(first, second)
    if first.shape == "circle":
        return _circle_polygon(first, second)
    if second.shape == "circle":
        contact = _circle_polygon(second, first)
        if contact is None:
            return None
        return Contact2D(first, second, scale(contact.normal, -1.0), contact.penetration, contact.point)
    return _polygon_polygon(first, second)


def _resolve_contact(contact: Contact2D) -> None:
    first, second = contact.first, contact.second
    inverse_mass_sum = first._inverse_mass + second._inverse_mass
    if inverse_mass_sum <= 0.0 or first.sensor or second.sensor:
        return

    relative_first = subtract(contact.point, first._position)
    relative_second = subtract(contact.point, second._position)
    relative_velocity = subtract(second._velocity_at(contact.point), first._velocity_at(contact.point))
    normal_speed = dot(relative_velocity, contact.normal)
    if normal_speed < 0.0:
        first_cross = _cross(relative_first, contact.normal)
        second_cross = _cross(relative_second, contact.normal)
        denominator = (
            inverse_mass_sum
            + first_cross * first_cross * first._inverse_inertia
            + second_cross * second_cross * second._inverse_inertia
        )
        if denominator > _EPSILON:
            restitution = min(first.material.restitution, second.material.restitution)
            normal_impulse_size = -(1.0 + restitution) * normal_speed / denominator
            normal_impulse = scale(contact.normal, normal_impulse_size)
            first._apply_impulse_internal(scale(normal_impulse, -1.0), contact.point)
            second._apply_impulse_internal(normal_impulse, contact.point)

            relative_velocity = subtract(second._velocity_at(contact.point), first._velocity_at(contact.point))
            tangent = subtract(relative_velocity, scale(contact.normal, dot(relative_velocity, contact.normal)))
            if length_squared(tangent) > _EPSILON:
                tangent = normalized(tangent)
                first_tangent_cross = _cross(relative_first, tangent)
                second_tangent_cross = _cross(relative_second, tangent)
                tangent_denominator = (
                    inverse_mass_sum
                    + first_tangent_cross * first_tangent_cross * first._inverse_inertia
                    + second_tangent_cross * second_tangent_cross * second._inverse_inertia
                )
                if tangent_denominator > _EPSILON:
                    tangent_impulse_size = -dot(relative_velocity, tangent) / tangent_denominator
                    static_friction = math.sqrt(
                        first.material.static_friction * second.material.static_friction
                    )
                    dynamic_friction = math.sqrt(
                        first.material.dynamic_friction * second.material.dynamic_friction
                    )
                    if abs(tangent_impulse_size) > normal_impulse_size * static_friction:
                        tangent_impulse_size = clamp(
                            tangent_impulse_size,
                            -normal_impulse_size * dynamic_friction,
                            normal_impulse_size * dynamic_friction,
                        )
                    tangent_impulse = scale(tangent, tangent_impulse_size)
                    first._apply_impulse_internal(scale(tangent_impulse, -1.0), contact.point)
                    second._apply_impulse_internal(tangent_impulse, contact.point)

    correction_size = max(contact.penetration - 0.001, 0.0) * 0.7 / inverse_mass_sum
    correction = scale(contact.normal, correction_size)
    if first.body_type == "dynamic":
        first._position = subtract(first._position, scale(correction, first._inverse_mass))
    if second.body_type == "dynamic":
        second._position = add(second._position, scale(correction, second._inverse_mass))


class World2D:
    """A deterministic two-dimensional physics world using SI units."""

    def __init__(self, raw_options: Any = None, *, base_dir: str = "."):
        options = options_dict(raw_options)
        self.base_dir = str(base_dir or ".")
        self._gravity = optional_vector(
            options.get("gravity"), 2, "gravity", [0.0, -STANDARD_GRAVITY]
        )
        self.fixed_step = positive_number(options.get("fixed_step", 1.0 / 120.0), "fixed_step")
        if self.fixed_step > 0.1:
            raise NCConfigurationError("fixed_step cannot be greater than 0.1 seconds")
        self.solver_iterations = max(1, int(options.get("solver_iterations", 8)))
        self.air_density = positive_number(options.get("air_density", 1.225), "air_density", allow_zero=True)
        self.max_bodies = max(1, int(options.get("max_bodies", 2000)))
        self.time = 0.0
        self.step_count = 0
        self.bodies: list[Body2D] = []
        self.joints: list[DistanceJoint2D] = []
        self._identifiers = IdentifierPool("body2d_")
        self._force_rules: list[Any] = []
        self._fixed_step_callbacks: list[Any] = []
        self._collision_callbacks: list[Any] = []
        self._clock = FixedStepClock(self.fixed_step)

    def _create_body(
        self,
        shape: str,
        mass: Any,
        position: Any,
        body_type: str,
        options: dict[str, Any],
        *,
        radius: float | None = None,
        vertices: list[list[float]] | None = None,
    ) -> Body2D:
        if len(self.bodies) >= self.max_bodies:
            raise NCConfigurationError(f"World body limit reached ({self.max_bodies})")
        identifier = str(options.get("id") or self._identifiers.allocate())
        if any(body.id == identifier for body in self.bodies):
            raise NCConfigurationError(f"Body id already exists: {identifier}")
        body = Body2D(
            self,
            identifier,
            shape,
            position=optional_vector(position, 2, "position", [0.0, 0.0]),
            mass=finite_number(mass, "mass"),
            body_type=body_type,
            radius=radius,
            vertices=vertices,
            options=options,
        )
        self.bodies.append(body)
        return body

    @nc_callable
    def circle(self, radius: Any = 0.5, mass: Any = 1.0, position: Any = None, raw_options: Any = None) -> Body2D:
        options = options_dict(raw_options)
        radius_value = positive_number(radius, "radius")
        body_type = str(options.get("body_type", "dynamic"))
        return self._create_body(
            "circle", mass, position, body_type, options, radius=radius_value
        )

    @nc_callable
    def box(
        self,
        width: Any = 1.0,
        height: Any = 1.0,
        mass: Any = 1.0,
        position: Any = None,
        raw_options: Any = None,
    ) -> Body2D:
        options = options_dict(raw_options)
        width_value = positive_number(width, "width")
        height_value = positive_number(height, "height")
        half_width, half_height = width_value * 0.5, height_value * 0.5
        vertices = [
            [-half_width, -half_height],
            [half_width, -half_height],
            [half_width, half_height],
            [-half_width, half_height],
        ]
        return self._create_body(
            "polygon",
            mass,
            position,
            str(options.get("body_type", "dynamic")),
            options,
            vertices=vertices,
        )

    @nc_callable
    def polygon(self, vertices: Any, mass: Any = 1.0, position: Any = None, raw_options: Any = None) -> Body2D:
        options = options_dict(raw_options)
        return self._create_body(
            "polygon",
            mass,
            position,
            str(options.get("body_type", "dynamic")),
            options,
            vertices=_convex_vertices(vertices),
        )

    @nc_callable
    def static_box(self, width: Any, height: Any, position: Any = None, raw_options: Any = None) -> Body2D:
        options = options_dict(raw_options)
        options["body_type"] = "static"
        return self.box(width, height, 0.0, position, options)

    @nc_callable
    def distance_joint(
        self,
        first: Body2D,
        second: Body2D,
        rest_length: Any = None,
        stiffness: Any = 250.0,
        damping: Any = 20.0,
    ) -> DistanceJoint2D:
        if first not in self.bodies or second not in self.bodies:
            raise NCConfigurationError("Joint bodies must belong to this world")
        if rest_length is None:
            rest_length = length(subtract(second._position, first._position))
        joint = DistanceJoint2D(
            first,
            second,
            finite_number(rest_length, "rest_length"),
            finite_number(stiffness, "stiffness"),
            finite_number(damping, "damping"),
        )
        self.joints.append(joint)
        return joint

    @nc_callable
    def gravity(self) -> list[float]:
        return list(self._gravity)

    @nc_callable
    def set_gravity(self, gravity: Any) -> "World2D":
        self._gravity = vector(gravity, 2, "gravity")
        return self

    @nc_callable
    def add_force_rule(self, callback: Any) -> "World2D":
        if callback not in self._force_rules:
            self._force_rules.append(callback)
        return self

    @nc_callable
    def on_fixed_step(self, callback: Any) -> "World2D":
        if callback not in self._fixed_step_callbacks:
            self._fixed_step_callbacks.append(callback)
        return self

    @nc_callable
    def on_collision(self, callback: Any) -> "World2D":
        if callback not in self._collision_callbacks:
            self._collision_callbacks.append(callback)
        return self

    @nc_callable
    def remove(self, body: Body2D) -> bool:
        if body not in self.bodies:
            return False
        self.bodies.remove(body)
        self.joints = [joint for joint in self.joints if joint.first is not body and joint.second is not body]
        return True

    @nc_callable
    def body(self, identifier: Any) -> Body2D | None:
        wanted = str(identifier)
        return next((body for body in self.bodies if body.id == wanted), None)

    @nc_callable
    def clear(self) -> "World2D":
        self.bodies.clear()
        self.joints.clear()
        self.time = 0.0
        self.step_count = 0
        self._clock.reset()
        return self

    def _apply_air_drag(self, body: Body2D) -> None:
        speed_squared = length_squared(body._velocity)
        if speed_squared <= _EPSILON or body.drag_coefficient <= 0.0 or body.drag_area <= 0.0:
            return
        direction = normalized(body._velocity)
        magnitude = 0.5 * self.air_density * body.drag_coefficient * body.drag_area * speed_squared
        body._force = add(body._force, scale(direction, -magnitude))

    def _step_once(self, delta: float) -> None:
        for callback in tuple(self._fixed_step_callbacks):
            invoke_nc_callback(callback, self, delta)
        for joint in tuple(self.joints):
            joint._apply()
        for body in tuple(self.bodies):
            if body.body_type != "dynamic":
                continue
            body._force = add(body._force, scale(self._gravity, body._mass * body.gravity_scale))
            self._apply_air_drag(body)
            for callback in tuple(self._force_rules):
                result = invoke_nc_callback(callback, body, self, delta)
                if result is not None:
                    body.apply_force(result)
            acceleration = scale(body._force, body._inverse_mass)
            body._velocity = add(body._velocity, scale(acceleration, delta))
            body._angular_velocity += body._torque * body._inverse_inertia * delta
            body._velocity = scale(body._velocity, math.exp(-body.linear_damping * delta))
            body._angular_velocity *= math.exp(-body.angular_damping * delta)

        for body in tuple(self.bodies):
            if body.body_type in {"dynamic", "kinematic"}:
                body._position = add(body._position, scale(body._velocity, delta))
                body._angle += body._angular_velocity * delta

        reported_pairs: set[tuple[str, str]] = set()
        for _ in range(self.solver_iterations):
            for first_index, first in enumerate(self.bodies):
                for second in self.bodies[first_index + 1 :]:
                    if first.body_type == "static" and second.body_type == "static":
                        continue
                    contact = _collide(first, second)
                    if contact is None:
                        continue
                    pair = tuple(sorted((first.id, second.id)))
                    if pair not in reported_pairs:
                        reported_pairs.add(pair)
                        event = contact.event()
                        for callback in tuple(self._collision_callbacks):
                            invoke_nc_callback(callback, event)
                    _resolve_contact(contact)

        for body in self.bodies:
            body._force = [0.0, 0.0]
            body._torque = 0.0
        self.time += delta
        self.step_count += 1

    @nc_callable
    def step(self, delta: Any = None) -> "World2D":
        value = self.fixed_step if delta is None else positive_number(delta, "delta")
        if value > 0.1:
            raise NCConfigurationError(
                "A single physics step cannot exceed 0.1 seconds; use simulate(seconds)"
            )
        self._step_once(value)
        return self

    @nc_callable
    def simulate(self, seconds: Any) -> "World2D":
        duration = positive_number(seconds, "seconds", allow_zero=True)
        full_steps = int(duration / self.fixed_step)
        remainder = duration - full_steps * self.fixed_step
        for _ in range(full_steps):
            self._step_once(self.fixed_step)
        if remainder > _EPSILON:
            self._step_once(remainder)
        return self

    @nc_callable
    def advance(self, real_seconds: Any) -> int:
        count, _alpha = self._clock.consume(finite_number(real_seconds, "real_seconds"))
        for _ in range(count):
            self._step_once(self.fixed_step)
        return count

    @nc_callable
    def snapshot(self) -> dict[str, Any]:
        body_rows: list[dict[str, Any]] = []
        for body in self.bodies:
            item = body.info()
            if body.shape == "circle":
                item["radius"] = body.radius
            else:
                item["vertices"] = body._world_vertices()
            item["image_size"] = list(body.image_size)
            body_rows.append(item)
        return {
            "dimension": 2,
            "units": {"length": "metre", "mass": "kilogram", "time": "second"},
            "time": self.time,
            "step_count": self.step_count,
            "gravity": list(self._gravity),
            "fixed_step": self.fixed_step,
            "bodies": body_rows,
            "joints": [joint.info() for joint in self.joints],
        }

    @nc_callable
    def app(self, raw_options: Any = None) -> Any:
        from nc_physics2d_app import Physics2DApplication

        return Physics2DApplication(self, options_dict(raw_options), base_dir=self.base_dir)


def create_nc_module(interpreter: Any) -> dict[str, Any]:
    base_dir = str(getattr(interpreter, "_base_dir_current", ".") or ".")

    @nc_callable
    def world(raw_options: Any = None) -> World2D:
        return World2D(raw_options, base_dir=str(getattr(interpreter, "_base_dir_current", base_dir)))

    @nc_callable
    def material(raw_options: Any = None) -> Material2D:
        return Material2D.from_options(raw_options)

    @nc_callable
    def app(world_value: World2D, raw_options: Any = None) -> Any:
        if not isinstance(world_value, World2D):
            raise NCConfigurationError("physics2d.app expects a physics2d world")
        return world_value.app(raw_options)

    @nc_callable
    def image(path: Any) -> dict[str, Any]:
        return load_image_asset(path, str(getattr(interpreter, "_base_dir_current", base_dir))).to_dict()

    return {
        "world": world,
        "material": material,
        "app": app,
        "image": image,
        "standard_gravity": STANDARD_GRAVITY,
        "units": {"length": "metre", "mass": "kilogram", "time": "second"},
    }
