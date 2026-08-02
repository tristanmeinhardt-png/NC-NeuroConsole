# `ui.app` API

`ui.app` creates ordinary application windows directly from NC code. UI objects
exist before a renderer starts, so they can be tested without opening a window.
PySide6 is imported only by `app.run()`.

```nc
import ui

let app = ui.app("Counter")
let window = app.window("Counter", 640, 360)
let output = window.text("0")
let button = window.button("Add 1")

fn add_one():
  output.set_text(str(int(output.text()) + 1))

button.on_click(add_one)
app.run()
```

## Application

- `ui.app(title)`
- `app.window(title, width, height, options)`
- `app.timer(interval_ms, callback, repeat)`
- `app.last_error()`
- `app.quit()`
- `app.run()`

## Layout and elements

A window, row, or column can create:

- `text(value, options)`
- `button(value, options)`
- `input(placeholder, options)`
- `checkbox(value, checked, options)`
- `image(path, options)`
- `slider(value, options)`
- `progress(value, options)`
- `choice(values, options)`
- `table(rows, options)`
- `canvas(options)`
- `spacer(options)`
- `row(options)`, `column(options)`

Common options include `id`, `visible`, `enabled`, `width`, `height`,
`tooltip`, and `style`. Style fields include `color`, `background`, `border`,
`border_radius`, `padding`, `margin`, `font_size`, and `bold`.

## Events

- button: `on_click(callback)`
- input: `on_change(callback)`, `on_submit(callback)`
- checkbox, slider, choice: `on_change(callback)`
- window: `on_key(callback)`, `on_close(callback)`

A callback may ignore event values by declaring fewer parameters. A button
handler can therefore be `fn save():` or `fn save(button):`.

## State updates

- text/button: `text()`, `set_text(value)`
- input/choice: `value()`, `set_value(value)`
- checkbox: `checked()`, `set_checked(value)`
- progress/slider: `value()`, `set_value(value)`
- image: `source()`, `set_source(path)`
- table: `set_rows(rows)`, `append_row(row)`
- all elements: `set_visible`, `set_enabled`, `set_style`, `set_tooltip`

Canvas commands are `line`, `rectangle`, `circle`, `text`, and `clear`.
