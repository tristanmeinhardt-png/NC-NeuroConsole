"""Deterministic SI-unit 3D rigid-body and cloth physics for NC."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from nc_runtime_support import (
    FixedStepClock,
    IdentifierPool,
    ImageAsset,
    ModelAsset,
    NCConfigurationError,
    add,
    clamp,
    cross3,
    dot,
    finite_number,
    integer_at_least,
    invoke_nc_callback,
    length,
    length_squared,
    load_image_asset,
    load_model_asset,
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


def _component_multiply(a: Sequence[float], b: Sequence[float]) -> list[float]:
    return [a[index] * b[index] for index in range(3)]


def _inverse_inertia_term(body: "Body3D", relative: Sequence[float], axis: Sequence[float]) -> float:
    crossed = cross3(relative, axis)
    rotated = _component_multiply(crossed, body._inverse_inertia)
    return dot(cross3(rotated, relative), axis)


@dataclass(frozen=True)
class Material3D:
    restitution: float = 0.1
    static_friction: float = 0.6
    dynamic_friction: float = 0.45

    @classmethod
    def from_options(cls, raw: Any = None) -> "Material3D":
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


class Body3D:
    def __init__(
        self,
        world: "World3D",
        identifier: str,
        shape: str,
        *,
        position: Sequence[float],
        mass: float,
        body_type: str,
        radius: float | None = None,
        size: Sequence[float] | None = None,
        plane_normal: Sequence[float] | None = None,
        plane_offset: float = 0.0,
        options: dict[str, Any] | None = None,
    ):
        opts = dict(options or {})
        body_type = str(body_type or "dynamic").strip().lower()
        if body_type not in {"dynamic", "static", "kinematic"}:
            raise NCConfigurationError("body_type must be 'dynamic', 'static', or 'kinematic'")
        if shape == "plane" and body_type != "static":
            raise NCConfigurationError("Plane bodies must be static")
        self.world = world
        self.id = str(identifier)
        self.shape = str(shape)
        self.body_type = body_type
        self.radius = radius
        self.size = list(size) if size is not None else None
        self.plane_normal = list(plane_normal) if plane_normal is not None else None
        self.plane_offset = float(plane_offset)
        self._position = list(position)
        self._velocity = optional_vector(opts.get("velocity"), 3, "velocity", [0.0, 0.0, 0.0])
        self._force = [0.0, 0.0, 0.0]
        self._rotation = optional_vector(opts.get("rotation"), 3, "rotation", [0.0, 0.0, 0.0])
        self._angular_velocity = optional_vector(
            opts.get("angular_velocity"), 3, "angular_velocity", [0.0, 0.0, 0.0]
        )
        self._torque = [0.0, 0.0, 0.0]
        self.sensor = bool(opts.get("sensor", False))
        self.gravity_scale = finite_number(opts.get("gravity_scale", 1.0), "gravity_scale")
        self.linear_damping = positive_number(opts.get("linear_damping", 0.02), "linear_damping", allow_zero=True)
        self.angular_damping = positive_number(opts.get("angular_damping", 0.02), "angular_damping", allow_zero=True)
        self.drag_coefficient = positive_number(
            opts.get("drag_coefficient", 0.0), "drag_coefficient", allow_zero=True
        )
        self.drag_area = positive_number(opts.get("drag_area", 0.0), "drag_area", allow_zero=True)
        self.material = Material3D.from_options(opts.get("material"))
        self.model_asset: ModelAsset | None = None
        self.model_scale = optional_vector(opts.get("model_scale"), 3, "model_scale", [1.0, 1.0, 1.0])
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
        self._inertia = self._calculate_inertia(actual_mass) if body_type == "dynamic" else [math.inf] * 3
        self._inverse_inertia = [
            1.0 / component if math.isfinite(component) and component > 0.0 else 0.0
            for component in self._inertia
        ]

        model_path = opts.get("model")
        if model_path:
            self.set_model(model_path, opts.get("model_scale"))

    def _calculate_inertia(self, mass: float) -> list[float]:
        if self.shape == "sphere":
            radius = float(self.radius or 0.5)
            value = 0.4 * mass * radius * radius
            return [value, value, value]
        dimensions = self.size or [1.0, 1.0, 1.0]
        x, y, z = dimensions
        return [
            mass * (y * y + z * z) / 12.0,
            mass * (x * x + z * z) / 12.0,
            mass * (x * x + y * y) / 12.0,
        ]

    def _half_extents(self) -> list[float]:
        if self.shape == "sphere":
            radius = float(self.radius or 0.0)
            return [radius, radius, radius]
        return [component * 0.5 for component in (self.size or [0.0, 0.0, 0.0])]

    def _velocity_at(self, point: Sequence[float]) -> list[float]:
        relative = subtract(point, self._position)
        return add(self._velocity, cross3(self._angular_velocity, relative))

    def _apply_impulse_internal(self, impulse: Sequence[float], point: Sequence[float]) -> None:
        if self.body_type != "dynamic":
            return
        self._velocity = add(self._velocity, scale(impulse, self._inverse_mass))
        relative = subtract(point, self._position)
        angular_impulse = cross3(relative, impulse)
        self._angular_velocity = add(
            self._angular_velocity, _component_multiply(angular_impulse, self._inverse_inertia)
        )

    @nc_callable
    def position(self) -> list[float]:
        return list(self._position)

    @nc_callable
    def set_position(self, value: Any) -> "Body3D":
        self._position = vector(value, 3, "position")
        return self

    @nc_callable
    def velocity(self) -> list[float]:
        return list(self._velocity)

    @nc_callable
    def set_velocity(self, value: Any) -> "Body3D":
        self._velocity = vector(value, 3, "velocity")
        return self

    @nc_callable
    def rotation(self) -> list[float]:
        return list(self._rotation)

    @nc_callable
    def set_rotation(self, radians_xyz: Any) -> "Body3D":
        self._rotation = vector(radians_xyz, 3, "rotation")
        return self

    @nc_callable
    def angular_velocity(self) -> list[float]:
        return list(self._angular_velocity)

    @nc_callable
    def set_angular_velocity(self, value: Any) -> "Body3D":
        self._angular_velocity = vector(value, 3, "angular_velocity")
        return self

    @nc_callable
    def mass(self) -> float:
        return self._mass

    @nc_callable
    def apply_force(self, force: Any, point: Any = None) -> "Body3D":
        if self.body_type != "dynamic":
            return self
        force_vector = vector(force, 3, "force")
        self._force = add(self._force, force_vector)
        if point is not None:
            application_point = vector(point, 3, "point")
            self._torque = add(
                self._torque,
                cross3(subtract(application_point, self._position), force_vector),
            )
        return self

    @nc_callable
    def apply_torque(self, torque: Any) -> "Body3D":
        if self.body_type == "dynamic":
            self._torque = add(self._torque, vector(torque, 3, "torque"))
        return self

    @nc_callable
    def apply_impulse(self, impulse: Any, point: Any = None) -> "Body3D":
        application_point = self._position if point is None else vector(point, 3, "point")
        self._apply_impulse_internal(vector(impulse, 3, "impulse"), application_point)
        return self

    @nc_callable
    def set_model(self, path: Any, model_scale: Any = None) -> "Body3D":
        self.model_asset = load_model_asset(path, self.world.base_dir)
        if model_scale is not None:
            scale_value = vector(model_scale, 3, "model_scale")
            if any(component <= 0.0 for component in scale_value):
                raise NCConfigurationError("model_scale values must be greater than zero")
            self.model_scale = scale_value
        return self

    @nc_callable
    def clear_model(self) -> "Body3D":
        self.model_asset = None
        return self

    @nc_callable
    def info(self) -> dict[str, Any]:
        result = {
            "id": self.id,
            "shape": self.shape,
            "body_type": self.body_type,
            "position": self.position(),
            "velocity": self.velocity(),
            "rotation": self.rotation(),
            "angular_velocity": self.angular_velocity(),
            "mass": None if math.isinf(self._mass) else self._mass,
            "sensor": self.sensor,
            "material": self.material.info(),
            "model": self.model_asset.to_dict() if self.model_asset else None,
            "model_scale": list(self.model_scale),
            "color": self.color,
            "user_data": self.user_data,
        }
        if self.shape == "sphere":
            result["radius"] = self.radius
        elif self.shape == "box":
            result["size"] = list(self.size or [])
            result["collision_basis"] = "axis_aligned"
        elif self.shape == "plane":
            result["normal"] = list(self.plane_normal or [])
            result["offset"] = self.plane_offset
        return result


@dataclass
class Contact3D:
    first: Body3D
    second: Body3D
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


class DistanceJoint3D:
    def __init__(
        self,
        first: Body3D,
        second: Body3D,
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
    def set_enabled(self, enabled: Any) -> "DistanceJoint3D":
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


def _sphere_sphere(first: Body3D, second: Body3D) -> Contact3D | None:
    delta = subtract(second._position, first._position)
    distance_sq = length_squared(delta)
    combined = float(first.radius or 0.0) + float(second.radius or 0.0)
    if distance_sq >= combined * combined:
        return None
    if distance_sq <= _EPSILON:
        normal, distance = [1.0, 0.0, 0.0], 0.0
    else:
        distance = math.sqrt(distance_sq)
        normal = scale(delta, 1.0 / distance)
    penetration = combined - distance
    point = add(first._position, scale(normal, float(first.radius or 0.0) - penetration * 0.5))
    return Contact3D(first, second, normal, penetration, point)


def _box_bounds(body: Body3D) -> tuple[list[float], list[float]]:
    half = body._half_extents()
    return subtract(body._position, half), add(body._position, half)


def _box_box(first: Body3D, second: Body3D) -> Contact3D | None:
    first_min, first_max = _box_bounds(first)
    second_min, second_max = _box_bounds(second)
    overlaps = [
        min(first_max[axis], second_max[axis]) - max(first_min[axis], second_min[axis])
        for axis in range(3)
    ]
    if any(overlap <= 0.0 for overlap in overlaps):
        return None
    axis_index = min(range(3), key=lambda axis: overlaps[axis])
    normal = [0.0, 0.0, 0.0]
    normal[axis_index] = 1.0 if second._position[axis_index] >= first._position[axis_index] else -1.0
    point = [
        (max(first_min[axis], second_min[axis]) + min(first_max[axis], second_max[axis])) * 0.5
        for axis in range(3)
    ]
    return Contact3D(first, second, normal, overlaps[axis_index], point)


def _sphere_box(sphere: Body3D, box: Body3D) -> Contact3D | None:
    box_min, box_max = _box_bounds(box)
    closest = [clamp(sphere._position[axis], box_min[axis], box_max[axis]) for axis in range(3)]
    delta = subtract(closest, sphere._position)
    distance_sq = length_squared(delta)
    radius = float(sphere.radius or 0.0)
    if distance_sq > radius * radius:
        return None
    if distance_sq > _EPSILON:
        distance = math.sqrt(distance_sq)
        normal = scale(delta, 1.0 / distance)
        penetration = radius - distance
        point = closest
    else:
        distances: list[tuple[float, int, float]] = []
        for axis in range(3):
            distances.append((sphere._position[axis] - box_min[axis], axis, -1.0))
            distances.append((box_max[axis] - sphere._position[axis], axis, 1.0))
        face_distance, axis_index, sign = min(distances, key=lambda item: item[0])
        normal = [0.0, 0.0, 0.0]
        # Contact normals point from the first body toward the second. For a
        # sphere already inside a box this is opposite the nearest exit face.
        normal[axis_index] = -sign
        penetration = radius + face_distance
        point = list(sphere._position)
        point[axis_index] = box_max[axis_index] if sign > 0.0 else box_min[axis_index]
    return Contact3D(sphere, box, normal, penetration, point)


def _plane_body(plane: Body3D, body: Body3D) -> Contact3D | None:
    normal = list(plane.plane_normal or [0.0, 0.0, 1.0])
    projected_radius = (
        float(body.radius or 0.0)
        if body.shape == "sphere"
        else dot(body._half_extents(), [abs(component) for component in normal])
    )
    signed_distance = dot(normal, body._position) - plane.plane_offset
    penetration = projected_radius - signed_distance
    if penetration <= 0.0:
        return None
    point = subtract(body._position, scale(normal, projected_radius - penetration * 0.5))
    return Contact3D(plane, body, normal, penetration, point)


def _collide(first: Body3D, second: Body3D) -> Contact3D | None:
    if first.shape == "plane":
        if second.shape == "plane":
            return None
        return _plane_body(first, second)
    if second.shape == "plane":
        contact = _plane_body(second, first)
        if contact is None:
            return None
        return Contact3D(first, second, scale(contact.normal, -1.0), contact.penetration, contact.point)
    if first.shape == "sphere" and second.shape == "sphere":
        return _sphere_sphere(first, second)
    if first.shape == "sphere":
        return _sphere_box(first, second)
    if second.shape == "sphere":
        contact = _sphere_box(second, first)
        if contact is None:
            return None
        return Contact3D(first, second, scale(contact.normal, -1.0), contact.penetration, contact.point)
    return _box_box(first, second)


def _resolve_contact(contact: Contact3D) -> None:
    first, second = contact.first, contact.second
    inverse_mass_sum = first._inverse_mass + second._inverse_mass
    if inverse_mass_sum <= 0.0 or first.sensor or second.sensor:
        return
    relative_first = subtract(contact.point, first._position)
    relative_second = subtract(contact.point, second._position)
    relative_velocity = subtract(second._velocity_at(contact.point), first._velocity_at(contact.point))
    normal_speed = dot(relative_velocity, contact.normal)
    if normal_speed < 0.0:
        denominator = (
            inverse_mass_sum
            + _inverse_inertia_term(first, relative_first, contact.normal)
            + _inverse_inertia_term(second, relative_second, contact.normal)
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
                tangent_denominator = (
                    inverse_mass_sum
                    + _inverse_inertia_term(first, relative_first, tangent)
                    + _inverse_inertia_term(second, relative_second, tangent)
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


@dataclass
class _ClothParticle:
    position: list[float]
    previous: list[float]
    inverse_mass: float
    force: list[float]


@dataclass(frozen=True)
class _ClothConstraint:
    first: int
    second: int
    rest_length: float
    stiffness: float


class Cloth3D:
    """Position-based cloth surface; not a generic soft-body implementation."""

    def __init__(self, world: "World3D", identifier: str, raw_options: Any = None):
        options = options_dict(raw_options, "cloth options")
        self.world = world
        self.id = str(identifier)
        self.columns = integer_at_least(options.get("columns", 16), "columns", 2)
        self.rows = integer_at_least(options.get("rows", 10), "rows", 2)
        self.size = optional_vector(options.get("size"), 2, "size", [3.0, 2.0])
        if any(component <= 0.0 for component in self.size):
            raise NCConfigurationError("cloth size values must be greater than zero")
        self.origin = optional_vector(options.get("origin"), 3, "origin", [-1.5, 0.0, 3.0])
        self.orientation = str(options.get("orientation", "vertical")).strip().lower()
        if self.orientation not in {"vertical", "horizontal"}:
            raise NCConfigurationError("cloth orientation must be 'vertical' or 'horizontal'")
        self.total_mass = positive_number(options.get("mass", 1.0), "mass")
        self.structural_stiffness = clamp(
            finite_number(options.get("structural_stiffness", options.get("stiffness", 0.95)), "structural_stiffness"),
            0.0,
            1.0,
        )
        self.shear_stiffness = clamp(
            finite_number(options.get("shear_stiffness", 0.8), "shear_stiffness"), 0.0, 1.0
        )
        self.bend_stiffness = clamp(
            finite_number(options.get("bend_stiffness", 0.35), "bend_stiffness"), 0.0, 1.0
        )
        self.damping = clamp(finite_number(options.get("damping", 0.015), "damping"), 0.0, 0.99)
        self.solver_iterations = integer_at_least(options.get("solver_iterations", 8), "solver_iterations", 1)
        self.substeps = integer_at_least(options.get("substeps", 2), "substeps", 1)
        self.thickness = positive_number(options.get("thickness", 0.015), "thickness", allow_zero=True)
        self.drag_coefficient = positive_number(
            options.get("drag_coefficient", 1.2), "drag_coefficient", allow_zero=True
        )
        self.wind = optional_vector(options.get("wind"), 3, "wind", [0.0, 0.0, 0.0])
        self.color = str(options.get("color", "#ef4444"))
        self.texture_asset: ImageAsset | None = None
        self.user_data = options.get("user_data")
        self._particles: list[_ClothParticle] = []
        self._constraints: list[_ClothConstraint] = []
        self._triangles: list[list[int]] = []
        self._build_grid()
        self._apply_pin_spec(options.get("pinned", "top_corners"))
        if options.get("texture"):
            self.set_texture(options["texture"])

    def _index(self, column: int, row: int) -> int:
        return row * self.columns + column

    def _grid_position(self, column: int, row: int) -> list[float]:
        x = self.size[0] * column / (self.columns - 1)
        y = self.size[1] * row / (self.rows - 1)
        if self.orientation == "vertical":
            return add(self.origin, [x, 0.0, -y])
        return add(self.origin, [x, y, 0.0])

    def _add_constraint(self, first: int, second: int, stiffness: float) -> None:
        rest = length(subtract(self._particles[second].position, self._particles[first].position))
        self._constraints.append(_ClothConstraint(first, second, rest, stiffness))

    def _build_grid(self) -> None:
        inverse_mass = (self.columns * self.rows) / self.total_mass
        for row in range(self.rows):
            for column in range(self.columns):
                position = self._grid_position(column, row)
                self._particles.append(
                    _ClothParticle(list(position), list(position), inverse_mass, [0.0, 0.0, 0.0])
                )
        for row in range(self.rows):
            for column in range(self.columns):
                current = self._index(column, row)
                if column + 1 < self.columns:
                    self._add_constraint(current, self._index(column + 1, row), self.structural_stiffness)
                if row + 1 < self.rows:
                    self._add_constraint(current, self._index(column, row + 1), self.structural_stiffness)
                if column + 1 < self.columns and row + 1 < self.rows:
                    self._add_constraint(current, self._index(column + 1, row + 1), self.shear_stiffness)
                    self._add_constraint(self._index(column + 1, row), self._index(column, row + 1), self.shear_stiffness)
                    self._triangles.append([current, self._index(column + 1, row), self._index(column, row + 1)])
                    self._triangles.append([
                        self._index(column + 1, row),
                        self._index(column + 1, row + 1),
                        self._index(column, row + 1),
                    ])
                if column + 2 < self.columns:
                    self._add_constraint(current, self._index(column + 2, row), self.bend_stiffness)
                if row + 2 < self.rows:
                    self._add_constraint(current, self._index(column, row + 2), self.bend_stiffness)

    def _apply_pin_spec(self, spec: Any) -> None:
        if spec is None or spec is False or str(spec).lower() == "none":
            return
        indices: list[int] = []
        if isinstance(spec, str):
            name = spec.strip().lower()
            if name == "top":
                indices = [self._index(column, 0) for column in range(self.columns)]
            elif name in {"top_corners", "corners"}:
                indices = [self._index(0, 0), self._index(self.columns - 1, 0)]
            else:
                raise NCConfigurationError("pinned must be 'none', 'top', 'top_corners', or a list")
        elif isinstance(spec, (list, tuple)):
            for item in spec:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    column, row = int(item[0]), int(item[1])
                    if not (0 <= column < self.columns and 0 <= row < self.rows):
                        raise NCConfigurationError(f"Pinned cloth coordinate is outside the grid: {item!r}")
                    indices.append(self._index(column, row))
                else:
                    index = int(item)
                    if not (0 <= index < len(self._particles)):
                        raise NCConfigurationError(f"Pinned cloth index is outside the grid: {index}")
                    indices.append(index)
        else:
            raise NCConfigurationError("pinned must be a string or list")
        for index in indices:
            self._particles[index].inverse_mass = 0.0

    def _aerodynamic_forces(self, delta: float) -> list[list[float]]:
        forces = [[0.0, 0.0, 0.0] for _ in self._particles]
        if self.drag_coefficient <= 0.0:
            return forces
        for triangle in self._triangles:
            particles = [self._particles[index] for index in triangle]
            edge_a = subtract(particles[1].position, particles[0].position)
            edge_b = subtract(particles[2].position, particles[0].position)
            normal_area = cross3(edge_a, edge_b)
            double_area = length(normal_area)
            if double_area <= _EPSILON:
                continue
            surface_normal = scale(normal_area, 1.0 / double_area)
            velocities = [
                scale(subtract(particle.position, particle.previous), 1.0 / max(delta, _EPSILON))
                for particle in particles
            ]
            average_velocity = scale(add(add(velocities[0], velocities[1]), velocities[2]), 1.0 / 3.0)
            relative_wind = subtract(self.wind, average_velocity)
            normal_speed = dot(relative_wind, surface_normal)
            pressure = 0.5 * self.world.air_density * self.drag_coefficient * normal_speed * abs(normal_speed)
            triangle_force = scale(surface_normal, pressure * double_area * 0.5 / 3.0)
            for index in triangle:
                forces[index] = add(forces[index], triangle_force)
        return forces

    def _solve_constraints(self) -> None:
        for constraint in self._constraints:
            first = self._particles[constraint.first]
            second = self._particles[constraint.second]
            inverse_sum = first.inverse_mass + second.inverse_mass
            if inverse_sum <= 0.0:
                continue
            delta = subtract(second.position, first.position)
            distance = length(delta)
            if distance <= _EPSILON:
                continue
            correction = scale(
                delta,
                constraint.stiffness * (distance - constraint.rest_length) / (distance * inverse_sum),
            )
            if first.inverse_mass > 0.0:
                first.position = add(first.position, scale(correction, first.inverse_mass))
            if second.inverse_mass > 0.0:
                second.position = subtract(second.position, scale(correction, second.inverse_mass))

    def _collide_particle(self, particle: _ClothParticle, body: Body3D) -> None:
        if particle.inverse_mass <= 0.0 or body.sensor:
            return
        if body.shape == "plane":
            normal = body.plane_normal or [0.0, 0.0, 1.0]
            signed = dot(normal, particle.position) - body.plane_offset
            if signed < self.thickness:
                particle.position = add(particle.position, scale(normal, self.thickness - signed))
            return
        if body.shape == "sphere":
            delta = subtract(particle.position, body._position)
            minimum = float(body.radius or 0.0) + self.thickness
            distance_sq = length_squared(delta)
            if distance_sq < minimum * minimum:
                normal = normalized(delta, [0.0, 0.0, 1.0])
                particle.position = add(body._position, scale(normal, minimum))
            return
        box_min, box_max = _box_bounds(body)
        expanded_min = [value - self.thickness for value in box_min]
        expanded_max = [value + self.thickness for value in box_max]
        if all(expanded_min[axis] <= particle.position[axis] <= expanded_max[axis] for axis in range(3)):
            choices: list[tuple[float, int, float]] = []
            for axis in range(3):
                choices.append((particle.position[axis] - expanded_min[axis], axis, -1.0))
                choices.append((expanded_max[axis] - particle.position[axis], axis, 1.0))
            _distance, axis_index, sign = min(choices, key=lambda item: item[0])
            particle.position[axis_index] = (
                expanded_max[axis_index] if sign > 0.0 else expanded_min[axis_index]
            )

    def _step(self, delta: float) -> None:
        sub_delta = delta / self.substeps
        particle_mass = self.total_mass / len(self._particles)
        for _ in range(self.substeps):
            aerodynamic = self._aerodynamic_forces(sub_delta)
            for index, particle in enumerate(self._particles):
                if particle.inverse_mass <= 0.0:
                    particle.previous = list(particle.position)
                    particle.force = [0.0, 0.0, 0.0]
                    continue
                velocity = scale(subtract(particle.position, particle.previous), 1.0 - self.damping)
                acceleration = add(
                    self.world._gravity,
                    scale(add(particle.force, aerodynamic[index]), 1.0 / particle_mass),
                )
                next_position = add(add(particle.position, velocity), scale(acceleration, sub_delta * sub_delta))
                particle.previous = list(particle.position)
                particle.position = next_position
                particle.force = [0.0, 0.0, 0.0]
            for _iteration in range(self.solver_iterations):
                self._solve_constraints()
                for particle in self._particles:
                    for body in self.world.bodies:
                        self._collide_particle(particle, body)

    @nc_callable
    def set_wind(self, velocity: Any) -> "Cloth3D":
        self.wind = vector(velocity, 3, "wind")
        return self

    @nc_callable
    def apply_force(self, force: Any) -> "Cloth3D":
        force_vector = vector(force, 3, "force")
        share = scale(force_vector, 1.0 / len(self._particles))
        for particle in self._particles:
            if particle.inverse_mass > 0.0:
                particle.force = add(particle.force, share)
        return self

    @nc_callable
    def pin(self, column: Any, row: Any) -> "Cloth3D":
        column_value, row_value = int(column), int(row)
        if not (0 <= column_value < self.columns and 0 <= row_value < self.rows):
            raise NCConfigurationError("Cloth pin coordinate is outside the grid")
        self._particles[self._index(column_value, row_value)].inverse_mass = 0.0
        return self

    @nc_callable
    def unpin(self, column: Any, row: Any) -> "Cloth3D":
        column_value, row_value = int(column), int(row)
        if not (0 <= column_value < self.columns and 0 <= row_value < self.rows):
            raise NCConfigurationError("Cloth pin coordinate is outside the grid")
        self._particles[self._index(column_value, row_value)].inverse_mass = len(self._particles) / self.total_mass
        return self

    @nc_callable
    def set_texture(self, path: Any) -> "Cloth3D":
        self.texture_asset = load_image_asset(path, self.world.base_dir)
        return self

    @nc_callable
    def vertices(self) -> list[list[float]]:
        return [list(particle.position) for particle in self._particles]

    @nc_callable
    def triangles(self) -> list[list[int]]:
        return [list(triangle) for triangle in self._triangles]

    @nc_callable
    def info(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "cloth",
            "columns": self.columns,
            "rows": self.rows,
            "size": list(self.size),
            "mass": self.total_mass,
            "wind": list(self.wind),
            "color": self.color,
            "texture": self.texture_asset.to_dict() if self.texture_asset else None,
            "vertices": self.vertices(),
            "triangles": self.triangles(),
            "pinned": [
                index for index, particle in enumerate(self._particles) if particle.inverse_mass <= 0.0
            ],
            "user_data": self.user_data,
        }


class World3D:
    def __init__(self, raw_options: Any = None, *, base_dir: str = "."):
        options = options_dict(raw_options)
        self.base_dir = str(base_dir or ".")
        self._gravity = optional_vector(
            options.get("gravity"), 3, "gravity", [0.0, 0.0, -STANDARD_GRAVITY]
        )
        self.fixed_step = positive_number(options.get("fixed_step", 1.0 / 120.0), "fixed_step")
        if self.fixed_step > 0.1:
            raise NCConfigurationError("fixed_step cannot be greater than 0.1 seconds")
        self.solver_iterations = integer_at_least(
            options.get("solver_iterations", 8), "solver_iterations", 1
        )
        self.air_density = positive_number(options.get("air_density", 1.225), "air_density", allow_zero=True)
        self.max_bodies = integer_at_least(options.get("max_bodies", 1500), "max_bodies", 1)
        self.max_cloths = integer_at_least(options.get("max_cloths", 32), "max_cloths", 1)
        self.time = 0.0
        self.step_count = 0
        self.bodies: list[Body3D] = []
        self.cloths: list[Cloth3D] = []
        self.joints: list[DistanceJoint3D] = []
        self._body_identifiers = IdentifierPool("body3d_")
        self._cloth_identifiers = IdentifierPool("cloth3d_")
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
        **shape_values: Any,
    ) -> Body3D:
        if len(self.bodies) >= self.max_bodies:
            raise NCConfigurationError(f"World body limit reached ({self.max_bodies})")
        identifier = str(options.get("id") or self._body_identifiers.allocate())
        if any(body.id == identifier for body in self.bodies):
            raise NCConfigurationError(f"Body id already exists: {identifier}")
        body = Body3D(
            self,
            identifier,
            shape,
            position=optional_vector(position, 3, "position", [0.0, 0.0, 0.0]),
            mass=finite_number(mass, "mass"),
            body_type=body_type,
            options=options,
            **shape_values,
        )
        self.bodies.append(body)
        return body

    @nc_callable
    def sphere(self, radius: Any = 0.5, mass: Any = 1.0, position: Any = None, raw_options: Any = None) -> Body3D:
        options = options_dict(raw_options)
        return self._create_body(
            "sphere",
            mass,
            position,
            str(options.get("body_type", "dynamic")),
            options,
            radius=positive_number(radius, "radius"),
        )

    @nc_callable
    def box(self, size: Any = None, mass: Any = 1.0, position: Any = None, raw_options: Any = None) -> Body3D:
        options = options_dict(raw_options)
        dimensions = optional_vector(size, 3, "size", [1.0, 1.0, 1.0])
        if any(component <= 0.0 for component in dimensions):
            raise NCConfigurationError("size values must be greater than zero")
        return self._create_body(
            "box",
            mass,
            position,
            str(options.get("body_type", "dynamic")),
            options,
            size=dimensions,
        )

    @nc_callable
    def static_box(self, size: Any, position: Any = None, raw_options: Any = None) -> Body3D:
        options = options_dict(raw_options)
        options["body_type"] = "static"
        return self.box(size, 0.0, position, options)

    @nc_callable
    def plane(self, normal: Any = None, offset: Any = 0.0, raw_options: Any = None) -> Body3D:
        options = options_dict(raw_options)
        plane_normal = normalized(
            optional_vector(normal, 3, "normal", [0.0, 0.0, 1.0]),
            [0.0, 0.0, 1.0],
        )
        if length_squared(plane_normal) <= _EPSILON:
            raise NCConfigurationError("plane normal cannot be zero")
        return self._create_body(
            "plane",
            0.0,
            scale(plane_normal, finite_number(offset, "offset")),
            "static",
            options,
            plane_normal=plane_normal,
            plane_offset=finite_number(offset, "offset"),
        )

    @nc_callable
    def cloth(self, raw_options: Any = None) -> Cloth3D:
        if len(self.cloths) >= self.max_cloths:
            raise NCConfigurationError(f"World cloth limit reached ({self.max_cloths})")
        options = options_dict(raw_options, "cloth options")
        identifier = str(options.get("id") or self._cloth_identifiers.allocate())
        if any(cloth.id == identifier for cloth in self.cloths):
            raise NCConfigurationError(f"Cloth id already exists: {identifier}")
        cloth = Cloth3D(self, identifier, options)
        self.cloths.append(cloth)
        return cloth

    @nc_callable
    def distance_joint(
        self,
        first: Body3D,
        second: Body3D,
        rest_length: Any = None,
        stiffness: Any = 250.0,
        damping: Any = 20.0,
    ) -> DistanceJoint3D:
        if first not in self.bodies or second not in self.bodies:
            raise NCConfigurationError("Joint bodies must belong to this world")
        if rest_length is None:
            rest_length = length(subtract(second._position, first._position))
        joint = DistanceJoint3D(
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
    def set_gravity(self, value: Any) -> "World3D":
        self._gravity = vector(value, 3, "gravity")
        return self

    @nc_callable
    def add_force_rule(self, callback: Any) -> "World3D":
        if callback not in self._force_rules:
            self._force_rules.append(callback)
        return self

    @nc_callable
    def on_fixed_step(self, callback: Any) -> "World3D":
        if callback not in self._fixed_step_callbacks:
            self._fixed_step_callbacks.append(callback)
        return self

    @nc_callable
    def on_collision(self, callback: Any) -> "World3D":
        if callback not in self._collision_callbacks:
            self._collision_callbacks.append(callback)
        return self

    @nc_callable
    def remove(self, item: Any) -> bool:
        if item in self.bodies:
            self.bodies.remove(item)
            self.joints = [joint for joint in self.joints if joint.first is not item and joint.second is not item]
            return True
        if item in self.cloths:
            self.cloths.remove(item)
            return True
        return False

    @nc_callable
    def body(self, identifier: Any) -> Body3D | None:
        wanted = str(identifier)
        return next((body for body in self.bodies if body.id == wanted), None)

    @nc_callable
    def cloth_by_id(self, identifier: Any) -> Cloth3D | None:
        wanted = str(identifier)
        return next((cloth for cloth in self.cloths if cloth.id == wanted), None)

    @nc_callable
    def clear(self) -> "World3D":
        self.bodies.clear()
        self.cloths.clear()
        self.joints.clear()
        self.time = 0.0
        self.step_count = 0
        self._clock.reset()
        return self

    def _apply_air_drag(self, body: Body3D) -> None:
        speed_squared = length_squared(body._velocity)
        if speed_squared <= _EPSILON or body.drag_coefficient <= 0.0 or body.drag_area <= 0.0:
            return
        magnitude = 0.5 * self.air_density * body.drag_coefficient * body.drag_area * speed_squared
        body._force = add(body._force, scale(normalized(body._velocity), -magnitude))

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
            body._velocity = add(body._velocity, scale(body._force, body._inverse_mass * delta))
            body._angular_velocity = add(
                body._angular_velocity,
                scale(_component_multiply(body._torque, body._inverse_inertia), delta),
            )
            body._velocity = scale(body._velocity, math.exp(-body.linear_damping * delta))
            body._angular_velocity = scale(
                body._angular_velocity, math.exp(-body.angular_damping * delta)
            )
        for body in tuple(self.bodies):
            if body.body_type in {"dynamic", "kinematic"}:
                body._position = add(body._position, scale(body._velocity, delta))
                body._rotation = add(body._rotation, scale(body._angular_velocity, delta))

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

        for cloth in tuple(self.cloths):
            cloth._step(delta)
        for body in self.bodies:
            body._force = [0.0, 0.0, 0.0]
            body._torque = [0.0, 0.0, 0.0]
        self.time += delta
        self.step_count += 1

    @nc_callable
    def step(self, delta: Any = None) -> "World3D":
        value = self.fixed_step if delta is None else positive_number(delta, "delta")
        if value > 0.1:
            raise NCConfigurationError(
                "A single physics step cannot exceed 0.1 seconds; use simulate(seconds)"
            )
        self._step_once(value)
        return self

    @nc_callable
    def simulate(self, seconds: Any) -> "World3D":
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
        return {
            "dimension": 3,
            "units": {"length": "metre", "mass": "kilogram", "time": "second"},
            "time": self.time,
            "step_count": self.step_count,
            "gravity": list(self._gravity),
            "fixed_step": self.fixed_step,
            "bodies": [body.info() for body in self.bodies],
            "cloths": [cloth.info() for cloth in self.cloths],
            "joints": [joint.info() for joint in self.joints],
        }

    @nc_callable
    def app(self, raw_options: Any = None) -> Any:
        from nc_physics3d_app import Physics3DApplication

        return Physics3DApplication(self, options_dict(raw_options), base_dir=self.base_dir)


def create_nc_module(interpreter: Any) -> dict[str, Any]:
    base_dir = str(getattr(interpreter, "_base_dir_current", ".") or ".")

    @nc_callable
    def world(raw_options: Any = None) -> World3D:
        return World3D(raw_options, base_dir=str(getattr(interpreter, "_base_dir_current", base_dir)))

    @nc_callable
    def material(raw_options: Any = None) -> Material3D:
        return Material3D.from_options(raw_options)

    @nc_callable
    def app(world_value: World3D, raw_options: Any = None) -> Any:
        if not isinstance(world_value, World3D):
            raise NCConfigurationError("physics3d.app expects a physics3d world")
        return world_value.app(raw_options)

    @nc_callable
    def model(path: Any) -> dict[str, Any]:
        return load_model_asset(path, str(getattr(interpreter, "_base_dir_current", base_dir))).to_dict()

    @nc_callable
    def image(path: Any) -> dict[str, Any]:
        return load_image_asset(path, str(getattr(interpreter, "_base_dir_current", base_dir))).to_dict()

    return {
        "world": world,
        "material": material,
        "app": app,
        "model": model,
        "image": image,
        "standard_gravity": STANDARD_GRAVITY,
        "units": {"length": "metre", "mass": "kilogram", "time": "second"},
        "features": {
            "rigid_bodies": True,
            "cloth": True,
            "generic_soft_bodies": False,
            "liquids": False,
            "gases": False,
        },
    }
