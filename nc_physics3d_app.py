"""Optional Panda3D renderer for :mod:`nc_physics3d` worlds."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

from nc_runtime_support import (
    NCDependencyError,
    NCResourceError,
    invoke_nc_callback,
    nc_callable,
    optional_vector,
)


def _rgba(value: Any) -> tuple[float, float, float, float]:
    text = str(value or "#ffffff").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) not in {6, 8}:
        return 1.0, 1.0, 1.0, 1.0
    try:
        channels = [int(text[index : index + 2], 16) / 255.0 for index in range(0, len(text), 2)]
    except ValueError:
        return 1.0, 1.0, 1.0, 1.0
    if len(channels) == 3:
        channels.append(1.0)
    return channels[0], channels[1], channels[2], channels[3]


class Physics3DApplication:
    def __init__(self, world: Any, options: dict[str, Any], *, base_dir: str = "."):
        self.world = world
        self.base_dir = str(base_dir or ".")
        self.title = str(options.get("title", "NC Physics 3D"))
        self.width = max(320, int(options.get("width", 1100)))
        self.height = max(240, int(options.get("height", 760)))
        self.background = _rgba(options.get("background", "#0b1020"))
        self.camera_position = optional_vector(
            options.get("camera_position"), 3, "camera_position", [8.0, -12.0, 7.0]
        )
        self.camera_target = optional_vector(
            options.get("camera_target"), 3, "camera_target", [0.0, 0.0, 1.5]
        )
        self.auto_step = bool(options.get("auto_step", True))
        self.show_debug_grid = bool(options.get("show_debug_grid", True))
        self._key_callbacks: list[Any] = []
        self._close_callbacks: list[Any] = []
        self._base: Any = None

    @nc_callable
    def set_camera(self, position: Any, target: Any = None) -> "Physics3DApplication":
        self.camera_position = optional_vector(position, 3, "camera_position", self.camera_position)
        if target is not None:
            self.camera_target = optional_vector(target, 3, "camera_target", self.camera_target)
        if self._base is not None:
            self._base.camera.setPos(*self.camera_position)
            self._base.camera.lookAt(*self.camera_target)
        return self

    @nc_callable
    def on_key(self, callback: Any) -> "Physics3DApplication":
        if callback not in self._key_callbacks:
            self._key_callbacks.append(callback)
        return self

    @nc_callable
    def on_close(self, callback: Any) -> "Physics3DApplication":
        if callback not in self._close_callbacks:
            self._close_callbacks.append(callback)
        return self

    @nc_callable
    def stop(self) -> None:
        if self._base is not None:
            self._base.userExit()

    @staticmethod
    def _obj_geometry(path: str, panda: dict[str, Any]) -> Any:
        Geom = panda["Geom"]
        GeomNode = panda["GeomNode"]
        GeomTriangles = panda["GeomTriangles"]
        GeomVertexData = panda["GeomVertexData"]
        GeomVertexFormat = panda["GeomVertexFormat"]
        GeomVertexWriter = panda["GeomVertexWriter"]

        positions: list[tuple[float, float, float]] = []
        normals: list[tuple[float, float, float]] = []
        texcoords: list[tuple[float, float]] = []
        faces: list[list[tuple[int, int | None, int | None]]] = []
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as error:
            raise NCResourceError(f"Could not read OBJ model: {path}: {error}") from error
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if parts[0] == "v" and len(parts) >= 4:
                positions.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif parts[0] == "vn" and len(parts) >= 4:
                normals.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif parts[0] == "vt" and len(parts) >= 3:
                texcoords.append((float(parts[1]), float(parts[2])))
            elif parts[0] == "f" and len(parts) >= 4:
                face: list[tuple[int, int | None, int | None]] = []
                for token in parts[1:]:
                    fields = token.split("/")
                    vertex_index = int(fields[0])
                    texture_index = int(fields[1]) if len(fields) > 1 and fields[1] else None
                    normal_index = int(fields[2]) if len(fields) > 2 and fields[2] else None
                    face.append((vertex_index, texture_index, normal_index))
                faces.append(face)
        if not positions or not faces:
            raise NCResourceError(f"OBJ model contains no renderable faces: {path}")

        data = GeomVertexData("nc_obj", GeomVertexFormat.getV3n3t2(), Geom.UHStatic)
        vertex_writer = GeomVertexWriter(data, "vertex")
        normal_writer = GeomVertexWriter(data, "normal")
        texture_writer = GeomVertexWriter(data, "texcoord")
        triangles = GeomTriangles(Geom.UHStatic)
        output_index = 0

        def resolve_index(index: int, values: Sequence[Any]) -> int:
            return index - 1 if index > 0 else len(values) + index

        for face in faces:
            for triangle_offset in range(1, len(face) - 1):
                triangle = [face[0], face[triangle_offset], face[triangle_offset + 1]]
                triangle_positions = [positions[resolve_index(item[0], positions)] for item in triangle]
                edge_a = [triangle_positions[1][axis] - triangle_positions[0][axis] for axis in range(3)]
                edge_b = [triangle_positions[2][axis] - triangle_positions[0][axis] for axis in range(3)]
                generated_normal = [
                    edge_a[1] * edge_b[2] - edge_a[2] * edge_b[1],
                    edge_a[2] * edge_b[0] - edge_a[0] * edge_b[2],
                    edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0],
                ]
                magnitude = math.sqrt(sum(component * component for component in generated_normal)) or 1.0
                generated_normal = [component / magnitude for component in generated_normal]
                for item, position in zip(triangle, triangle_positions):
                    vertex_writer.addData3(*position)
                    if item[2] is not None and normals:
                        normal_writer.addData3(*normals[resolve_index(item[2], normals)])
                    else:
                        normal_writer.addData3(*generated_normal)
                    if item[1] is not None and texcoords:
                        texture_writer.addData2(*texcoords[resolve_index(item[1], texcoords)])
                    else:
                        texture_writer.addData2(0.0, 0.0)
                    triangles.addVertex(output_index)
                    output_index += 1
                triangles.closePrimitive()
        geometry = Geom(data)
        geometry.addPrimitive(triangles)
        node = GeomNode("nc_obj")
        node.addGeom(geometry)
        return node

    @staticmethod
    def _box_geometry(size: Sequence[float], panda: dict[str, Any]) -> Any:
        x, y, z = [component * 0.5 for component in size]
        vertices = [
            (-x, -y, -z), (x, -y, -z), (x, y, -z), (-x, y, -z),
            (-x, -y, z), (x, -y, z), (x, y, z), (-x, y, z),
        ]
        faces = [
            (0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
            (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7),
        ]
        return Physics3DApplication._indexed_mesh(vertices, faces, panda, "nc_box")

    @staticmethod
    def _sphere_geometry(radius: float, panda: dict[str, Any], segments: int = 24, rings: int = 14) -> Any:
        vertices: list[tuple[float, float, float]] = []
        for ring in range(rings + 1):
            latitude = math.pi * ring / rings
            for segment in range(segments):
                longitude = 2.0 * math.pi * segment / segments
                vertices.append((
                    radius * math.sin(latitude) * math.cos(longitude),
                    radius * math.sin(latitude) * math.sin(longitude),
                    radius * math.cos(latitude),
                ))
        faces: list[tuple[int, int, int, int]] = []
        for ring in range(rings):
            for segment in range(segments):
                following = (segment + 1) % segments
                first = ring * segments + segment
                second = ring * segments + following
                third = (ring + 1) * segments + following
                fourth = (ring + 1) * segments + segment
                faces.append((first, second, third, fourth))
        return Physics3DApplication._indexed_mesh(vertices, faces, panda, "nc_sphere")

    @staticmethod
    def _indexed_mesh(
        vertices: Sequence[Sequence[float]],
        faces: Iterable[Sequence[int]],
        panda: dict[str, Any],
        name: str,
    ) -> Any:
        Geom = panda["Geom"]
        GeomNode = panda["GeomNode"]
        GeomTriangles = panda["GeomTriangles"]
        GeomVertexData = panda["GeomVertexData"]
        GeomVertexFormat = panda["GeomVertexFormat"]
        GeomVertexWriter = panda["GeomVertexWriter"]
        data = GeomVertexData(name, GeomVertexFormat.getV3n3(), Geom.UHStatic)
        vertex_writer = GeomVertexWriter(data, "vertex")
        normal_writer = GeomVertexWriter(data, "normal")
        triangles = GeomTriangles(Geom.UHStatic)
        output_index = 0
        for face in faces:
            for offset in range(1, len(face) - 1):
                triangle_indices = [face[0], face[offset], face[offset + 1]]
                points = [vertices[index] for index in triangle_indices]
                edge_a = [points[1][axis] - points[0][axis] for axis in range(3)]
                edge_b = [points[2][axis] - points[0][axis] for axis in range(3)]
                normal = [
                    edge_a[1] * edge_b[2] - edge_a[2] * edge_b[1],
                    edge_a[2] * edge_b[0] - edge_a[0] * edge_b[2],
                    edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0],
                ]
                magnitude = math.sqrt(sum(component * component for component in normal)) or 1.0
                normal = [component / magnitude for component in normal]
                for point in points:
                    vertex_writer.addData3(*point)
                    normal_writer.addData3(*normal)
                    triangles.addVertex(output_index)
                    output_index += 1
                triangles.closePrimitive()
        geometry = Geom(data)
        geometry.addPrimitive(triangles)
        node = GeomNode(name)
        node.addGeom(geometry)
        return node

    @staticmethod
    def _cloth_geometry(cloth: Any, panda: dict[str, Any]) -> Any:
        Geom = panda["Geom"]
        GeomNode = panda["GeomNode"]
        GeomTriangles = panda["GeomTriangles"]
        GeomVertexData = panda["GeomVertexData"]
        GeomVertexFormat = panda["GeomVertexFormat"]
        GeomVertexWriter = panda["GeomVertexWriter"]
        vertices = cloth.vertices()
        data = GeomVertexData("nc_cloth", GeomVertexFormat.getV3n3t2(), Geom.UHDynamic)
        vertex_writer = GeomVertexWriter(data, "vertex")
        normal_writer = GeomVertexWriter(data, "normal")
        texture_writer = GeomVertexWriter(data, "texcoord")
        triangles = GeomTriangles(Geom.UHDynamic)
        output_index = 0
        for triangle in cloth.triangles():
            points = [vertices[index] for index in triangle]
            edge_a = [points[1][axis] - points[0][axis] for axis in range(3)]
            edge_b = [points[2][axis] - points[0][axis] for axis in range(3)]
            normal = [
                edge_a[1] * edge_b[2] - edge_a[2] * edge_b[1],
                edge_a[2] * edge_b[0] - edge_a[0] * edge_b[2],
                edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0],
            ]
            magnitude = math.sqrt(sum(component * component for component in normal)) or 1.0
            normal = [component / magnitude for component in normal]
            for index, point in zip(triangle, points):
                column = index % cloth.columns
                row = index // cloth.columns
                vertex_writer.addData3(*point)
                normal_writer.addData3(*normal)
                texture_writer.addData2(
                    column / max(1, cloth.columns - 1),
                    1.0 - row / max(1, cloth.rows - 1),
                )
                triangles.addVertex(output_index)
                output_index += 1
            triangles.closePrimitive()
        geometry = Geom(data)
        geometry.addPrimitive(triangles)
        node = GeomNode("nc_cloth")
        node.addGeom(geometry)
        return node

    @nc_callable
    def run(self) -> int:
        if os.environ.get("NC_DISABLE_GRAPHICS") == "1":
            print("NC: physics3d window disabled for this console executable.")
            return 0
        try:
            from direct.showbase.ShowBase import ShowBase
            from panda3d.core import (
                AmbientLight,
                ClockObject,
                DirectionalLight,
                Geom,
                GeomNode,
                GeomTriangles,
                GeomVertexData,
                GeomVertexFormat,
                GeomVertexWriter,
                LineSegs,
                NodePath,
                Texture,
                WindowProperties,
                loadPrcFileData,
            )
        except Exception as error:
            raise NCDependencyError(
                "physics3d.app needs Panda3D. Run the NC installer or install panda3d and panda3d-gltf."
            ) from error

        loadPrcFileData("", f"window-title {self.title}")
        loadPrcFileData("", f"win-size {self.width} {self.height}")
        loadPrcFileData("", "sync-video true")
        panda = {
            "Geom": Geom,
            "GeomNode": GeomNode,
            "GeomTriangles": GeomTriangles,
            "GeomVertexData": GeomVertexData,
            "GeomVertexFormat": GeomVertexFormat,
            "GeomVertexWriter": GeomVertexWriter,
        }
        controller = self
        frame_clock = ClockObject.getGlobalClock()

        class NCPhysicsShowBase(ShowBase):
            def __init__(self):
                super().__init__()
                self.disableMouse()
                self.setBackgroundColor(*controller.background)
                properties = WindowProperties()
                properties.setTitle(controller.title)
                properties.setSize(controller.width, controller.height)
                self.win.requestProperties(properties)
                self.camera.setPos(*controller.camera_position)
                self.camera.lookAt(*controller.camera_target)
                self.body_nodes: dict[str, NodePath] = {}
                self.cloth_nodes: dict[str, NodePath] = {}
                self._build_lights()
                if controller.show_debug_grid:
                    self._build_grid()
                self._sync_body_nodes()
                self.taskMgr.add(self._update_world, "nc_physics3d_update")
                for key in (
                    "escape", "space", "arrow_up", "arrow_down", "arrow_left", "arrow_right",
                    "w", "a", "s", "d", "q", "e",
                ):
                    self.accept(key, self._key, [key, "press"])
                    self.accept(key + "-up", self._key, [key, "release"])

            def _build_lights(self):
                ambient = AmbientLight("nc_ambient")
                ambient.setColor((0.35, 0.38, 0.45, 1.0))
                self.render.setLight(self.render.attachNewNode(ambient))
                sun = DirectionalLight("nc_sun")
                sun.setColor((0.9, 0.88, 0.82, 1.0))
                sun_node = self.render.attachNewNode(sun)
                sun_node.setHpr(-35, -55, 0)
                self.render.setLight(sun_node)

            def _build_grid(self):
                lines = LineSegs("nc_grid")
                lines.setThickness(1.0)
                for coordinate in range(-10, 11):
                    shade = 0.35 if coordinate == 0 else 0.16
                    lines.setColor(shade, shade + 0.03, shade + 0.08, 1.0)
                    lines.moveTo(coordinate, -10, 0)
                    lines.drawTo(coordinate, 10, 0)
                    lines.moveTo(-10, coordinate, 0)
                    lines.drawTo(10, coordinate, 0)
                self.render.attachNewNode(lines.create())

            def _primitive_node(self, body: Any) -> NodePath:
                if body.shape == "sphere":
                    return NodePath(controller._sphere_geometry(float(body.radius), panda))
                if body.shape == "box":
                    return NodePath(controller._box_geometry(body.size, panda))
                # The physics plane is infinite; its visual grid is rendered separately.
                return NodePath("nc_plane")

            def _model_node(self, body: Any) -> NodePath:
                asset = body.model_asset
                assert asset is not None
                if asset.format == "obj":
                    node = NodePath(controller._obj_geometry(asset.path, panda))
                else:
                    try:
                        node = self.loader.loadModel(asset.path)
                    except Exception as error:
                        raise NCResourceError(f"Could not load 3D model: {asset.path}: {error}") from error
                    if node is None or node.isEmpty():
                        raise NCResourceError(
                            f"Could not load {asset.format.upper()} model '{asset.path}'. "
                            "Install panda3d-gltf for GLB/glTF support."
                        )
                node.setScale(*body.model_scale)
                return node

            def _sync_body_nodes(self):
                known = {body.id for body in controller.world.bodies}
                for identifier in list(self.body_nodes):
                    if identifier not in known:
                        self.body_nodes.pop(identifier).removeNode()
                for body in controller.world.bodies:
                    if body.shape == "plane":
                        continue
                    node = self.body_nodes.get(body.id)
                    if node is None:
                        node = self._model_node(body) if body.model_asset else self._primitive_node(body)
                        node.reparentTo(self.render)
                        node.setColor(*_rgba(body.color))
                        self.body_nodes[body.id] = node
                    node.setPos(*body._position)
                    node.setHpr(*[math.degrees(value) for value in body._rotation])

            def _sync_cloth_nodes(self):
                known = {cloth.id for cloth in controller.world.cloths}
                for identifier in list(self.cloth_nodes):
                    if identifier not in known:
                        self.cloth_nodes.pop(identifier).removeNode()
                for cloth in controller.world.cloths:
                    old_node = self.cloth_nodes.pop(cloth.id, None)
                    if old_node is not None:
                        old_node.removeNode()
                    node = NodePath(controller._cloth_geometry(cloth, panda))
                    node.reparentTo(self.render)
                    node.setTwoSided(True)
                    node.setColor(*_rgba(cloth.color))
                    if cloth.texture_asset is not None:
                        texture = self.loader.loadTexture(cloth.texture_asset.path)
                        if texture is not None:
                            filter_value = getattr(
                                Texture,
                                "FT_linear_mipmap_linear",
                                getattr(Texture, "FTLinearMipmapLinear", None),
                            )
                            if filter_value is not None:
                                texture.setMinfilter(filter_value)
                            node.setColor(1.0, 1.0, 1.0, 1.0)
                            node.setTexture(texture, 1)
                    self.cloth_nodes[cloth.id] = node

            def _key(self, key: str, event_type: str):
                payload = {"key": key, "type": event_type}
                for callback in tuple(controller._key_callbacks):
                    invoke_nc_callback(callback, payload)
                if key == "escape" and event_type == "press":
                    self.userExit()

            def _update_world(self, task):
                if controller.auto_step:
                    controller.world.advance(frame_clock.getDt())
                self._sync_body_nodes()
                self._sync_cloth_nodes()
                return task.cont

            def userExit(self):
                for callback in tuple(controller._close_callbacks):
                    invoke_nc_callback(callback, controller)
                super().userExit()

        base = NCPhysicsShowBase()
        self._base = base
        base.run()
        return 0
