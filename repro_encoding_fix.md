# Repro: cp932 encoding error with Unicode output

## Issue

On Windows with a Japanese locale, the julia-mcp server crashes when Julia outputs
characters outside the cp932 (Shift-JIS) range (e.g. `✗`, `✓`, `→`).

**Error message:**
```
MCP server 'julia': Error executing tool julia_eval: 'cp932' codec can't encode character '\u2717' in position 0: illegal multibyte sequence
```

**Root cause:** `raw.decode()` in `_execute_raw` used the system default encoding (`cp932`)
instead of UTF-8. Same issue in the log file `open()` call.

**Fix applied:** `server.py` lines 158 and 203 — explicit `utf-8` encoding.

## Steps to reproduce (before fix)

Run this in a fresh session:

```python
# julia_eval tool call:
print('\u2717')
```

Expected: error as above.

## Steps to verify fix (after restarting Copilot)

Run this:

```python
# julia_eval tool call:
print('\u2717')  # ✗
```

Expected output: `✗` with no error.

Also try:
```julia
println("✓ success → done ✗ fail")
```

Expected output: `✓ success → done ✗ fail` with no error.
