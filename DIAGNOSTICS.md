# NC Diagnostics

Normal NC execution prints structured NC diagnostics and does not expose Python
tracebacks. Every diagnostic has a stable family code:

| Family | Meaning |
|---|---|
| `NC-S1xxx` | syntax and indentation |
| `NC-N2xxx` | name and scope resolution |
| `NC-M3xxx` | modules and imports |
| `NC-R4xxx` | runtime execution |
| `NC-T41xx` | invalid value or type |
| `NC-D5xxx` | dependencies and resources |
| `NC-P6xxx` | security or runtime policy |

Example:

```text
error NC-S1001: Missing ':' after if block header
  --> game.nc:12:16
   |
12 | if player.alive
   |                ^ expected ':'
13 |   player.move()
   |   ^ This indentation diagnostic is a consequence of the missing ':'.
   = help: Write `if player.alive:`
```

## Cause relationships

The multi-error formatter analyzes nearby diagnostics. An unexpected indent
directly after a missing block colon is linked to the missing colon and rendered
as a note. The parser still records both facts for tools, but the programmer is
shown which one to fix first.

## Runtime context

NC functions retain their definition source and line. A nested runtime failure
therefore includes an NC call stack:

```text
= NC call stack:
  at update_player (game.nc:41)
  at main (game.nc:88)
```

Imported modules add an import chain. Source excerpts work for local files and
in-memory code passed to `run_text`.

## Output ownership

The interpreter raises a structured exception but does not print it. The active
host (`nc`, `ncw`, or `nc_server`) formats it once. This prevents the duplicate
parse-error output produced by older builds.
