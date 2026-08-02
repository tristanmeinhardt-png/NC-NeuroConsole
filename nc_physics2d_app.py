"""Optional PySide6 renderer for :mod:`nc_physics2d` worlds."""

from __future__ import annotations

import math
import sys
import time
from typing import Any

from nc_runtime_support import (
    NCConfigurationError,
    NCDependencyError,
    finite_number,
    invoke_nc_callback,
    nc_callable,
    optional_vector,
    positive_number,
)


class Physics2DApplication:
    """Real-time view kept separate from the deterministic physics world."""

    def __init__(self, world: Any, options: dict[str, Any], *, base_dir: str = "."):
        self.world = world
        self.base_dir = str(base_dir or ".")
        self.title = str(options.get("title", "NC Physics 2D"))
        self.width = max(240, int(options.get("width", 1000)))
        self.height = max(180, int(options.get("height", 700)))
        self.pixels_per_metre = positive_number(
            options.get("pixels_per_metre", options.get("pixels_per_meter", 60.0)),
            "pixels_per_metre",
        )
        self.target_fps = max(10, min(240, int(options.get("target_fps", 60))))
        self.background = str(options.get("background", "#0b1020"))
        self.camera = optional_vector(options.get("camera"), 2, "camera", [0.0, 0.0])
        self.auto_step = bool(options.get("auto_step", True))
        self._key_callbacks: list[Any] = []
        self._mouse_callbacks: list[Any] = []
        self._close_callbacks: list[Any] = []
        self._window: Any = None
        self._qt_application: Any = None

    @nc_callable
    def set_camera(self, position: Any) -> "Physics2DApplication":
        self.camera = optional_vector(position, 2, "camera", [0.0, 0.0])
        if self._window is not None:
            self._window.update()
        return self

    @nc_callable
    def set_scale(self, pixels_per_metre: Any) -> "Physics2DApplication":
        self.pixels_per_metre = positive_number(pixels_per_metre, "pixels_per_metre")
        if self._window is not None:
            self._window.update()
        return self

    @nc_callable
    def on_key(self, callback: Any) -> "Physics2DApplication":
        if callback not in self._key_callbacks:
            self._key_callbacks.append(callback)
        return self

    @nc_callable
    def on_mouse(self, callback: Any) -> "Physics2DApplication":
        if callback not in self._mouse_callbacks:
            self._mouse_callbacks.append(callback)
        return self

    @nc_callable
    def on_close(self, callback: Any) -> "Physics2DApplication":
        if callback not in self._close_callbacks:
            self._close_callbacks.append(callback)
        return self

    @nc_callable
    def stop(self) -> None:
        if self._qt_application is not None:
            self._qt_application.quit()

    def _screen_to_world(self, x: float, y: float, width: int, height: int) -> list[float]:
        return [
            self.camera[0] + (x - width * 0.5) / self.pixels_per_metre,
            self.camera[1] - (y - height * 0.5) / self.pixels_per_metre,
        ]

    @nc_callable
    def run(self) -> int:
        try:
            from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
            from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
            from PySide6.QtWidgets import QApplication, QWidget
        except Exception as error:
            raise NCDependencyError(
                "physics2d.app needs PySide6. Run the NC installer or install PySide6 in the NC environment."
            ) from error

        controller = self

        class PhysicsCanvas(QWidget):
            def __init__(self):
                super().__init__()
                self.setWindowTitle(controller.title)
                self.resize(controller.width, controller.height)
                self.setFocusPolicy(Qt.StrongFocus)
                self._pixmaps: dict[str, Any] = {}
                self._last_time = time.perf_counter()
                self._timer = QTimer(self)
                self._timer.setTimerType(Qt.PreciseTimer)
                self._timer.timeout.connect(self._frame)
                self._timer.start(max(1, round(1000 / controller.target_fps)))

            def _frame(self):
                current = time.perf_counter()
                elapsed = current - self._last_time
                self._last_time = current
                if controller.auto_step:
                    controller.world.advance(elapsed)
                self.update()

            def _world_to_screen(self, position: Any) -> QPointF:
                return QPointF(
                    self.width() * 0.5 + (position[0] - controller.camera[0]) * controller.pixels_per_metre,
                    self.height() * 0.5 - (position[1] - controller.camera[1]) * controller.pixels_per_metre,
                )

            def _draw_body(self, painter: QPainter, body: Any):
                centre = self._world_to_screen(body._position)
                painter.save()
                painter.translate(centre)
                painter.rotate(-math.degrees(body._angle))
                asset = body.image_asset
                if asset is not None:
                    pixmap = self._pixmaps.get(asset.path)
                    if pixmap is None:
                        pixmap = QPixmap(asset.path)
                        self._pixmaps[asset.path] = pixmap
                    if not pixmap.isNull():
                        width = body.image_size[0] * controller.pixels_per_metre
                        height = body.image_size[1] * controller.pixels_per_metre
                        painter.drawPixmap(QRectF(-width / 2.0, -height / 2.0, width, height), pixmap, QRectF(pixmap.rect()))
                        painter.restore()
                        return

                color = QColor(body.color)
                if not color.isValid():
                    color = QColor("#8ecae6")
                painter.setBrush(color)
                painter.setPen(QPen(color.lighter(135), 1.25))
                if body.shape == "circle":
                    radius = float(body.radius) * controller.pixels_per_metre
                    painter.drawEllipse(QPointF(0.0, 0.0), radius, radius)
                    painter.setPen(QPen(QColor("#172033"), 1.5))
                    painter.drawLine(QPointF(0.0, 0.0), QPointF(radius, 0.0))
                else:
                    path = QPainterPath()
                    vertices = body.local_vertices or []
                    if vertices:
                        path.moveTo(
                            vertices[0][0] * controller.pixels_per_metre,
                            -vertices[0][1] * controller.pixels_per_metre,
                        )
                        for vertex in vertices[1:]:
                            path.lineTo(
                                vertex[0] * controller.pixels_per_metre,
                                -vertex[1] * controller.pixels_per_metre,
                            )
                        path.closeSubpath()
                        painter.drawPath(path)
                painter.restore()

            def paintEvent(self, _event):
                painter = QPainter(self)
                painter.setRenderHint(QPainter.Antialiasing, True)
                painter.fillRect(self.rect(), QColor(controller.background))
                for body in tuple(controller.world.bodies):
                    self._draw_body(painter, body)
                painter.setPen(QColor("#94a3b8"))
                painter.drawText(12, 22, f"{controller.world.time:.2f} s  |  {len(controller.world.bodies)} bodies")

            def keyPressEvent(self, event):
                payload = {
                    "type": "press",
                    "key": event.text() or event.key(),
                    "code": int(event.key()),
                    "auto_repeat": bool(event.isAutoRepeat()),
                }
                for callback in tuple(controller._key_callbacks):
                    invoke_nc_callback(callback, payload)
                super().keyPressEvent(event)

            def keyReleaseEvent(self, event):
                payload = {
                    "type": "release",
                    "key": event.text() or event.key(),
                    "code": int(event.key()),
                    "auto_repeat": bool(event.isAutoRepeat()),
                }
                for callback in tuple(controller._key_callbacks):
                    invoke_nc_callback(callback, payload)
                super().keyReleaseEvent(event)

            def _mouse_payload(self, event: Any, event_type: str) -> dict[str, Any]:
                position = event.position()
                return {
                    "type": event_type,
                    "button": int(event.button()),
                    "screen": [position.x(), position.y()],
                    "world": controller._screen_to_world(position.x(), position.y(), self.width(), self.height()),
                }

            def mousePressEvent(self, event):
                payload = self._mouse_payload(event, "press")
                for callback in tuple(controller._mouse_callbacks):
                    invoke_nc_callback(callback, payload)
                super().mousePressEvent(event)

            def mouseReleaseEvent(self, event):
                payload = self._mouse_payload(event, "release")
                for callback in tuple(controller._mouse_callbacks):
                    invoke_nc_callback(callback, payload)
                super().mouseReleaseEvent(event)

            def closeEvent(self, event):
                for callback in tuple(controller._close_callbacks):
                    invoke_nc_callback(callback, controller)
                super().closeEvent(event)

        existing = QApplication.instance()
        owns_application = existing is None
        application = existing or QApplication(sys.argv[:1])
        self._qt_application = application
        self._window = PhysicsCanvas()
        self._window.show()
        if owns_application:
            return int(application.exec())
        return 0
