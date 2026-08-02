"""Direct, event-driven ``ui.app`` API for NC programs.

The model classes are usable without PySide6.  Qt is imported only by
``NCApplication.run()``, which keeps headless tests and server programs clean.
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass
from typing import Any

from nc_runtime_support import (
    IdentifierPool,
    NCConfigurationError,
    NCDependencyError,
    ImageAsset,
    finite_number,
    invoke_nc_callback,
    load_image_asset,
    nc_callable,
    options_dict,
    positive_number,
)


def _style_sheet(style: dict[str, Any]) -> str:
    rules: list[str] = []
    mapping = {
        "color": "color",
        "background": "background-color",
        "border": "border",
        "border_radius": "border-radius",
        "padding": "padding",
        "margin": "margin",
        "font_size": "font-size",
    }
    pixel_keys = {"border_radius", "padding", "margin", "font_size"}
    for key, css_name in mapping.items():
        if key not in style:
            continue
        value = style[key]
        if key in pixel_keys and isinstance(value, (int, float)):
            value = f"{value}px"
        rules.append(f"{css_name}: {value}")
    if style.get("bold"):
        rules.append("font-weight: 700")
    return "; ".join(rules)


class UIElement:
    def __init__(self, identifier: str, raw_options: Any = None):
        options = options_dict(raw_options)
        self.id = str(options.get("id") or identifier)
        self.visible = bool(options.get("visible", True))
        self.enabled = bool(options.get("enabled", True))
        self.style = options_dict(options.get("style"), "style")
        self.width = int(options.get("width", 0) or 0)
        self.height = int(options.get("height", 0) or 0)
        self.tooltip = str(options.get("tooltip", ""))
        self._qt_widget: Any = None

    def _refresh_common(self) -> None:
        widget = self._qt_widget
        if widget is None:
            return
        widget.setVisible(self.visible)
        widget.setEnabled(self.enabled)
        widget.setToolTip(self.tooltip)
        widget.setStyleSheet(_style_sheet(self.style))
        if self.width > 0:
            widget.setMinimumWidth(self.width)
        if self.height > 0:
            widget.setMinimumHeight(self.height)

    @nc_callable
    def set_visible(self, value: Any) -> "UIElement":
        self.visible = bool(value)
        self._refresh_common()
        return self

    @nc_callable
    def set_enabled(self, value: Any) -> "UIElement":
        self.enabled = bool(value)
        self._refresh_common()
        return self

    @nc_callable
    def set_style(self, raw_style: Any) -> "UIElement":
        self.style.update(options_dict(raw_style, "style"))
        self._refresh_common()
        return self

    @nc_callable
    def set_tooltip(self, value: Any) -> "UIElement":
        self.tooltip = str(value)
        self._refresh_common()
        return self


class UILabel(UIElement):
    def __init__(self, identifier: str, text: Any = "", raw_options: Any = None):
        super().__init__(identifier, raw_options)
        self._text = str(text)

    @nc_callable
    def text(self) -> str:
        return self._text

    @nc_callable
    def set_text(self, value: Any) -> "UILabel":
        self._text = str(value)
        if self._qt_widget is not None:
            self._qt_widget.setText(self._text)
        return self


class UIButton(UILabel):
    def __init__(self, identifier: str, text: Any = "Button", raw_options: Any = None):
        super().__init__(identifier, text, raw_options)
        self._click_callbacks: list[Any] = []

    @nc_callable
    def on_click(self, callback: Any) -> "UIButton":
        if callback not in self._click_callbacks:
            self._click_callbacks.append(callback)
        return self

    @nc_callable
    def click(self) -> "UIButton":
        for callback in tuple(self._click_callbacks):
            invoke_nc_callback(callback, self)
        return self


class UIInput(UIElement):
    def __init__(self, identifier: str, placeholder: Any = "", raw_options: Any = None):
        options = options_dict(raw_options)
        super().__init__(identifier, options)
        self._value = str(options.get("value", ""))
        self.placeholder = str(placeholder)
        self.password = bool(options.get("password", False))
        self._change_callbacks: list[Any] = []
        self._submit_callbacks: list[Any] = []

    @nc_callable
    def value(self) -> str:
        return self._value

    @nc_callable
    def set_value(self, value: Any) -> "UIInput":
        self._value = str(value)
        if self._qt_widget is not None and self._qt_widget.text() != self._value:
            self._qt_widget.setText(self._value)
        return self

    @nc_callable
    def on_change(self, callback: Any) -> "UIInput":
        if callback not in self._change_callbacks:
            self._change_callbacks.append(callback)
        return self

    @nc_callable
    def on_submit(self, callback: Any) -> "UIInput":
        if callback not in self._submit_callbacks:
            self._submit_callbacks.append(callback)
        return self


class UICheckbox(UIElement):
    def __init__(self, identifier: str, text: Any = "", checked: Any = False, raw_options: Any = None):
        super().__init__(identifier, raw_options)
        self._text = str(text)
        self._checked = bool(checked)
        self._change_callbacks: list[Any] = []

    @nc_callable
    def checked(self) -> bool:
        return self._checked

    @nc_callable
    def set_checked(self, value: Any) -> "UICheckbox":
        self._checked = bool(value)
        if self._qt_widget is not None and self._qt_widget.isChecked() != self._checked:
            self._qt_widget.setChecked(self._checked)
        return self

    @nc_callable
    def on_change(self, callback: Any) -> "UICheckbox":
        if callback not in self._change_callbacks:
            self._change_callbacks.append(callback)
        return self


class UIImage(UIElement):
    def __init__(self, identifier: str, path: Any, base_dir: str, raw_options: Any = None):
        super().__init__(identifier, raw_options)
        self.base_dir = str(base_dir)
        self.asset = load_image_asset(path, base_dir)
        self.keep_aspect = bool(options_dict(raw_options).get("keep_aspect", True))

    @nc_callable
    def set_source(self, path: Any) -> "UIImage":
        self.asset = load_image_asset(path, self.base_dir)
        if self._qt_widget is not None:
            self._qt_widget._nc_refresh_image()
        return self

    @nc_callable
    def source(self) -> str:
        return self.asset.path


class UIProgress(UIElement):
    def __init__(self, identifier: str, value: Any = 0.0, raw_options: Any = None):
        options = options_dict(raw_options)
        super().__init__(identifier, options)
        self.minimum = finite_number(options.get("minimum", 0.0), "minimum")
        self.maximum = finite_number(options.get("maximum", 100.0), "maximum")
        if self.maximum <= self.minimum:
            raise NCConfigurationError("progress maximum must be greater than minimum")
        self._value = finite_number(value, "value")

    @nc_callable
    def value(self) -> float:
        return self._value

    @nc_callable
    def set_value(self, value: Any) -> "UIProgress":
        self._value = finite_number(value, "value")
        if self._qt_widget is not None:
            normal = (self._value - self.minimum) / (self.maximum - self.minimum)
            self._qt_widget.setValue(round(max(0.0, min(1.0, normal)) * 1000))
        return self


class UISlider(UIProgress):
    def __init__(self, identifier: str, value: Any = 0.0, raw_options: Any = None):
        super().__init__(identifier, value, raw_options)
        self._change_callbacks: list[Any] = []

    @nc_callable
    def on_change(self, callback: Any) -> "UISlider":
        if callback not in self._change_callbacks:
            self._change_callbacks.append(callback)
        return self


class UIChoice(UIElement):
    def __init__(self, identifier: str, choices: Any, raw_options: Any = None):
        options = options_dict(raw_options)
        super().__init__(identifier, options)
        if not isinstance(choices, (list, tuple)):
            raise NCConfigurationError("choices must be a list")
        self.choices = [str(value) for value in choices]
        self._value = str(options.get("value", self.choices[0] if self.choices else ""))
        self._change_callbacks: list[Any] = []

    @nc_callable
    def value(self) -> str:
        return self._value

    @nc_callable
    def set_value(self, value: Any) -> "UIChoice":
        text = str(value)
        if text not in self.choices:
            raise NCConfigurationError(f"Unknown choice: {text}")
        self._value = text
        if self._qt_widget is not None:
            self._qt_widget.setCurrentText(text)
        return self

    @nc_callable
    def on_change(self, callback: Any) -> "UIChoice":
        if callback not in self._change_callbacks:
            self._change_callbacks.append(callback)
        return self


class UITable(UIElement):
    def __init__(self, identifier: str, rows: Any = None, raw_options: Any = None):
        options = options_dict(raw_options)
        super().__init__(identifier, options)
        self.headers = [str(value) for value in options.get("headers", [])]
        self.rows: list[list[Any]] = []
        self.set_rows(rows or [])

    @nc_callable
    def set_rows(self, rows: Any) -> "UITable":
        if not isinstance(rows, (list, tuple)):
            raise NCConfigurationError("table rows must be a list")
        self.rows = [list(row) if isinstance(row, (list, tuple)) else [row] for row in rows]
        if self._qt_widget is not None:
            self._qt_widget._nc_refresh_table()
        return self

    @nc_callable
    def append_row(self, row: Any) -> "UITable":
        self.rows.append(list(row) if isinstance(row, (list, tuple)) else [row])
        if self._qt_widget is not None:
            self._qt_widget._nc_refresh_table()
        return self


class UICanvas(UIElement):
    def __init__(self, identifier: str, raw_options: Any = None):
        super().__init__(identifier, raw_options)
        self.commands: list[dict[str, Any]] = []
        self.background = str(options_dict(raw_options).get("background", "#111827"))

    def _append(self, command: dict[str, Any]) -> "UICanvas":
        self.commands.append(command)
        if self._qt_widget is not None:
            self._qt_widget.update()
        return self

    @nc_callable
    def clear(self) -> "UICanvas":
        self.commands.clear()
        if self._qt_widget is not None:
            self._qt_widget.update()
        return self

    @nc_callable
    def line(self, start: Any, end: Any, color: Any = "#ffffff", width: Any = 1.0) -> "UICanvas":
        return self._append({"type": "line", "start": list(start), "end": list(end), "color": str(color), "width": float(width)})

    @nc_callable
    def rectangle(self, position: Any, size: Any, color: Any = "#ffffff", fill: Any = True) -> "UICanvas":
        return self._append({"type": "rectangle", "position": list(position), "size": list(size), "color": str(color), "fill": bool(fill)})

    @nc_callable
    def circle(self, centre: Any, radius: Any, color: Any = "#ffffff", fill: Any = True) -> "UICanvas":
        return self._append({"type": "circle", "centre": list(centre), "radius": float(radius), "color": str(color), "fill": bool(fill)})

    @nc_callable
    def text(self, position: Any, value: Any, color: Any = "#ffffff") -> "UICanvas":
        return self._append({"type": "text", "position": list(position), "text": str(value), "color": str(color)})


class UISpacer(UIElement):
    pass


class UIContainer(UIElement):
    def __init__(self, app: "NCApplication", identifier: str, direction: str, raw_options: Any = None):
        super().__init__(identifier, raw_options)
        self.app = app
        self.direction = str(direction)
        self.children: list[UIElement] = []

    def _add(self, element: UIElement) -> Any:
        if any(existing.id == element.id for existing in self.app._all_elements()):
            raise NCConfigurationError(f"UI element id already exists: {element.id}")
        self.children.append(element)
        return element

    def _id(self, prefix: str, raw_options: Any) -> str:
        options = options_dict(raw_options)
        return str(options.get("id") or self.app._element_ids.allocate().replace("element_", prefix + "_"))

    @nc_callable
    def text(self, value: Any = "", raw_options: Any = None) -> UILabel:
        return self._add(UILabel(self._id("text", raw_options), value, raw_options))

    @nc_callable
    def button(self, value: Any = "Button", raw_options: Any = None) -> UIButton:
        return self._add(UIButton(self._id("button", raw_options), value, raw_options))

    @nc_callable
    def input(self, placeholder: Any = "", raw_options: Any = None) -> UIInput:
        return self._add(UIInput(self._id("input", raw_options), placeholder, raw_options))

    @nc_callable
    def checkbox(self, value: Any = "", checked: Any = False, raw_options: Any = None) -> UICheckbox:
        return self._add(UICheckbox(self._id("checkbox", raw_options), value, checked, raw_options))

    @nc_callable
    def image(self, path: Any, raw_options: Any = None) -> UIImage:
        return self._add(UIImage(self._id("image", raw_options), path, self.app.base_dir, raw_options))

    @nc_callable
    def slider(self, value: Any = 0.0, raw_options: Any = None) -> UISlider:
        return self._add(UISlider(self._id("slider", raw_options), value, raw_options))

    @nc_callable
    def progress(self, value: Any = 0.0, raw_options: Any = None) -> UIProgress:
        return self._add(UIProgress(self._id("progress", raw_options), value, raw_options))

    @nc_callable
    def choice(self, choices: Any, raw_options: Any = None) -> UIChoice:
        return self._add(UIChoice(self._id("choice", raw_options), choices, raw_options))

    @nc_callable
    def table(self, rows: Any = None, raw_options: Any = None) -> UITable:
        return self._add(UITable(self._id("table", raw_options), rows, raw_options))

    @nc_callable
    def canvas(self, raw_options: Any = None) -> UICanvas:
        return self._add(UICanvas(self._id("canvas", raw_options), raw_options))

    @nc_callable
    def spacer(self, raw_options: Any = None) -> UISpacer:
        return self._add(UISpacer(self._id("spacer", raw_options), raw_options))

    @nc_callable
    def row(self, raw_options: Any = None) -> "UIContainer":
        return self._add(UIContainer(self.app, self._id("row", raw_options), "row", raw_options))

    @nc_callable
    def column(self, raw_options: Any = None) -> "UIContainer":
        return self._add(UIContainer(self.app, self._id("column", raw_options), "column", raw_options))


class UIWindow(UIContainer):
    def __init__(
        self,
        app: "NCApplication",
        identifier: str,
        title: Any,
        width: Any,
        height: Any,
        raw_options: Any = None,
    ):
        super().__init__(app, identifier, "column", raw_options)
        self.title = str(title)
        self.window_width = max(200, int(width))
        self.window_height = max(120, int(height))
        self._close_callbacks: list[Any] = []
        self._key_callbacks: list[Any] = []

    @nc_callable
    def set_title(self, value: Any) -> "UIWindow":
        self.title = str(value)
        if self._qt_widget is not None:
            self._qt_widget.setWindowTitle(self.title)
        return self

    @nc_callable
    def resize(self, width: Any, height: Any) -> "UIWindow":
        self.window_width = max(200, int(width))
        self.window_height = max(120, int(height))
        if self._qt_widget is not None:
            self._qt_widget.resize(self.window_width, self.window_height)
        return self

    @nc_callable
    def on_close(self, callback: Any) -> "UIWindow":
        if callback not in self._close_callbacks:
            self._close_callbacks.append(callback)
        return self

    @nc_callable
    def on_key(self, callback: Any) -> "UIWindow":
        if callback not in self._key_callbacks:
            self._key_callbacks.append(callback)
        return self


@dataclass
class UITimer:
    interval_ms: int
    callback: Any
    repeat: bool = True
    _qt_timer: Any = None

    @nc_callable
    def start(self) -> "UITimer":
        if self._qt_timer is not None:
            self._qt_timer.start(self.interval_ms)
        return self

    @nc_callable
    def stop(self) -> "UITimer":
        if self._qt_timer is not None:
            self._qt_timer.stop()
        return self


class NCApplication:
    def __init__(self, title: Any = "NC App", *, base_dir: str = ".", interpreter: Any = None):
        self.title = str(title)
        self.base_dir = str(base_dir or ".")
        self.interpreter = interpreter
        self.windows: list[UIWindow] = []
        self.timers: list[UITimer] = []
        self._window_ids = IdentifierPool("window_")
        self._element_ids = IdentifierPool("element_")
        self._qt_application: Any = None
        self._last_error = ""

    def _all_elements(self) -> list[UIElement]:
        result: list[UIElement] = []

        def visit(element: UIElement) -> None:
            result.append(element)
            if isinstance(element, UIContainer):
                for child in element.children:
                    visit(child)

        for window in self.windows:
            visit(window)
        return result

    def _dispatch(self, callback: Any, *args: Any) -> None:
        try:
            invoke_nc_callback(callback, *args)
        except Exception as error:
            self._last_error = "".join(traceback.format_exception_only(type(error), error)).strip()
            print(f"NC UI callback error: {self._last_error}", file=sys.stderr)

    @nc_callable
    def window(
        self,
        title: Any = None,
        width: Any = 800,
        height: Any = 600,
        raw_options: Any = None,
    ) -> UIWindow:
        options = options_dict(raw_options)
        identifier = str(options.get("id") or self._window_ids.allocate())
        if any(window.id == identifier for window in self.windows):
            raise NCConfigurationError(f"Window id already exists: {identifier}")
        window = UIWindow(
            self,
            identifier,
            self.title if title is None else title,
            width,
            height,
            options,
        )
        self.windows.append(window)
        return window

    @nc_callable
    def timer(self, interval_ms: Any, callback: Any, repeat: Any = True) -> UITimer:
        interval = int(positive_number(interval_ms, "interval_ms"))
        timer = UITimer(interval, callback, bool(repeat))
        self.timers.append(timer)
        return timer

    @nc_callable
    def last_error(self) -> str:
        return self._last_error

    @nc_callable
    def quit(self) -> None:
        if self._qt_application is not None:
            self._qt_application.quit()

    @nc_callable
    def run(self) -> int:
        if not self.windows:
            self.window(self.title, 800, 600)
        try:
            from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer
            from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
            from PySide6.QtWidgets import (
                QApplication,
                QCheckBox,
                QComboBox,
                QHBoxLayout,
                QLabel,
                QLineEdit,
                QMainWindow,
                QProgressBar,
                QPushButton,
                QSlider,
                QSpacerItem,
                QTableWidget,
                QTableWidgetItem,
                QVBoxLayout,
                QWidget,
                QSizePolicy,
            )
        except Exception as error:
            raise NCDependencyError(
                "ui.app needs PySide6. Run the NC installer or install PySide6 in the NC environment."
            ) from error

        controller = self

        class CanvasWidget(QWidget):
            def __init__(self, model: UICanvas):
                super().__init__()
                self.model = model
                model._qt_widget = self
                self.setMinimumSize(model.width or 240, model.height or 160)
                model._refresh_common()

            def paintEvent(self, _event):
                painter = QPainter(self)
                painter.setRenderHint(QPainter.Antialiasing, True)
                painter.fillRect(self.rect(), QColor(self.model.background))
                for command in self.model.commands:
                    color = QColor(command.get("color", "#ffffff"))
                    kind = command.get("type")
                    if kind == "line":
                        painter.setPen(QPen(color, float(command.get("width", 1.0))))
                        start = command["start"]
                        end = command["end"]
                        painter.drawLine(QLineF(start[0], start[1], end[0], end[1]))
                    elif kind == "rectangle":
                        painter.setPen(QPen(color, 1.0))
                        painter.setBrush(color if command.get("fill", True) else Qt.NoBrush)
                        position = command["position"]
                        size = command["size"]
                        painter.drawRect(QRectF(position[0], position[1], size[0], size[1]))
                    elif kind == "circle":
                        centre = command["centre"]
                        radius = command["radius"]
                        painter.setPen(QPen(color, 1.0))
                        painter.setBrush(color if command.get("fill", True) else Qt.NoBrush)
                        painter.drawEllipse(QPointF(centre[0], centre[1]), radius, radius)
                    elif kind == "text":
                        painter.setPen(color)
                        position = command["position"]
                        painter.drawText(QPointF(position[0], position[1]), command["text"])

        class ImageLabel(QLabel):
            def __init__(self, model: UIImage):
                super().__init__()
                self.model = model
                model._qt_widget = self
                self.setAlignment(Qt.AlignCenter)
                self._nc_refresh_image()
                model._refresh_common()

            def _nc_refresh_image(self):
                pixmap = QPixmap(self.model.asset.path)
                target_width = self.model.width or pixmap.width()
                target_height = self.model.height or pixmap.height()
                if self.model.keep_aspect:
                    pixmap = pixmap.scaled(target_width, target_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                else:
                    pixmap = pixmap.scaled(target_width, target_height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                self.setPixmap(pixmap)

        class TableWidget(QTableWidget):
            def __init__(self, model: UITable):
                super().__init__()
                self.model = model
                model._qt_widget = self
                self._nc_refresh_table()
                model._refresh_common()

            def _nc_refresh_table(self):
                column_count = max(len(self.model.headers), max((len(row) for row in self.model.rows), default=0), 1)
                self.setColumnCount(column_count)
                self.setRowCount(len(self.model.rows))
                if self.model.headers:
                    self.setHorizontalHeaderLabels(self.model.headers + [""] * (column_count - len(self.model.headers)))
                for row_index, row in enumerate(self.model.rows):
                    for column_index in range(column_count):
                        value = row[column_index] if column_index < len(row) else ""
                        self.setItem(row_index, column_index, QTableWidgetItem(str(value)))
                self.horizontalHeader().setStretchLastSection(True)

        def build_element(model: UIElement) -> QWidget | None:
            if isinstance(model, UIContainer):
                widget = QWidget()
                model._qt_widget = widget
                layout = QHBoxLayout(widget) if model.direction == "row" else QVBoxLayout(widget)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(8)
                for child in model.children:
                    if isinstance(child, UISpacer):
                        layout.addItem(QSpacerItem(1, 1, QSizePolicy.Expanding, QSizePolicy.Expanding))
                        continue
                    child_widget = build_element(child)
                    if child_widget is not None:
                        layout.addWidget(child_widget)
                model._refresh_common()
                return widget
            if isinstance(model, UIButton):
                widget = QPushButton(model._text)
                model._qt_widget = widget
                widget.clicked.connect(
                    lambda _checked=False, item=model: [controller._dispatch(callback, item) for callback in tuple(item._click_callbacks)]
                )
            elif isinstance(model, UILabel):
                widget = QLabel(model._text)
                model._qt_widget = widget
                widget.setWordWrap(True)
            elif isinstance(model, UIInput):
                widget = QLineEdit(model._value)
                model._qt_widget = widget
                widget.setPlaceholderText(model.placeholder)
                if model.password:
                    widget.setEchoMode(QLineEdit.Password)

                def input_changed(value: str, item: UIInput = model):
                    item._value = value
                    for callback in tuple(item._change_callbacks):
                        controller._dispatch(callback, value, item)

                def input_submitted(item: UIInput = model):
                    for callback in tuple(item._submit_callbacks):
                        controller._dispatch(callback, item._value, item)

                widget.textChanged.connect(input_changed)
                widget.returnPressed.connect(input_submitted)
            elif isinstance(model, UICheckbox):
                widget = QCheckBox(model._text)
                model._qt_widget = widget
                widget.setChecked(model._checked)

                def checkbox_changed(state: int, item: UICheckbox = model):
                    item._checked = bool(state)
                    for callback in tuple(item._change_callbacks):
                        controller._dispatch(callback, item._checked, item)

                widget.stateChanged.connect(checkbox_changed)
            elif isinstance(model, UIImage):
                widget = ImageLabel(model)
            elif isinstance(model, UISlider):
                widget = QSlider(Qt.Horizontal)
                model._qt_widget = widget
                widget.setRange(0, 1000)
                model.set_value(model._value)

                def slider_changed(raw_value: int, item: UISlider = model):
                    item._value = item.minimum + (item.maximum - item.minimum) * raw_value / 1000.0
                    for callback in tuple(item._change_callbacks):
                        controller._dispatch(callback, item._value, item)

                widget.valueChanged.connect(slider_changed)
            elif isinstance(model, UIProgress):
                widget = QProgressBar()
                model._qt_widget = widget
                widget.setRange(0, 1000)
                model.set_value(model._value)
            elif isinstance(model, UIChoice):
                widget = QComboBox()
                model._qt_widget = widget
                widget.addItems(model.choices)
                if model._value in model.choices:
                    widget.setCurrentText(model._value)

                def choice_changed(value: str, item: UIChoice = model):
                    item._value = value
                    for callback in tuple(item._change_callbacks):
                        controller._dispatch(callback, value, item)

                widget.currentTextChanged.connect(choice_changed)
            elif isinstance(model, UITable):
                widget = TableWidget(model)
            elif isinstance(model, UICanvas):
                widget = CanvasWidget(model)
            else:
                return None
            model._refresh_common()
            return widget

        class MainWindow(QMainWindow):
            def __init__(self, model: UIWindow):
                super().__init__()
                self.model = model
                model._qt_widget = self
                self.setWindowTitle(model.title)
                self.resize(model.window_width, model.window_height)
                root = QWidget()
                layout = QVBoxLayout(root)
                layout.setContentsMargins(12, 12, 12, 12)
                layout.setSpacing(8)
                for child in model.children:
                    if isinstance(child, UISpacer):
                        layout.addStretch(1)
                    else:
                        child_widget = build_element(child)
                        if child_widget is not None:
                            layout.addWidget(child_widget)
                self.setCentralWidget(root)
                model._refresh_common()

            def keyPressEvent(self, event):
                payload = {
                    "type": "press",
                    "key": event.text() or event.key(),
                    "code": int(event.key()),
                    "auto_repeat": bool(event.isAutoRepeat()),
                }
                for callback in tuple(self.model._key_callbacks):
                    controller._dispatch(callback, payload, self.model)
                super().keyPressEvent(event)

            def keyReleaseEvent(self, event):
                payload = {
                    "type": "release",
                    "key": event.text() or event.key(),
                    "code": int(event.key()),
                    "auto_repeat": bool(event.isAutoRepeat()),
                }
                for callback in tuple(self.model._key_callbacks):
                    controller._dispatch(callback, payload, self.model)
                super().keyReleaseEvent(event)

            def closeEvent(self, event):
                for callback in tuple(self.model._close_callbacks):
                    controller._dispatch(callback, self.model)
                super().closeEvent(event)

        existing_application = QApplication.instance()
        owns_application = existing_application is None
        application = existing_application or QApplication(sys.argv[:1])
        application.setApplicationName(self.title)
        self._qt_application = application
        for window_model in self.windows:
            window_widget = MainWindow(window_model)
            window_model._qt_widget = window_widget
            if window_model.visible:
                window_widget.show()
        for timer_model in self.timers:
            timer = QTimer(application)
            timer_model._qt_timer = timer

            def fire(item: UITimer = timer_model):
                controller._dispatch(item.callback, item)
                if not item.repeat and item._qt_timer is not None:
                    item._qt_timer.stop()

            timer.timeout.connect(fire)
            timer.start(timer_model.interval_ms)
        if owns_application:
            return int(application.exec())
        return 0


def create_app(title: Any = "NC App", *, base_dir: str = ".", interpreter: Any = None) -> NCApplication:
    return NCApplication(title, base_dir=base_dir, interpreter=interpreter)
