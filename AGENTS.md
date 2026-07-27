# AGENTS.md

## Cursor Cloud specific instructions

### What this repo is
This repo is a collection of **FunPay Cardinal plugins** (single-file Python modules
in `plugins/`). There is no standalone app here — plugins are loaded by the external
host **FunPayCardinal** (`sidor0912/FunPayCardinal`). Real plugins use the host's
module-level format: constants `NAME`, `VERSION`, `DESCRIPTION`, `CREDITS`, `UUID`
(valid UUID4), `SETTINGS_PAGE`, `BIND_TO_DELETE`, plus `BIND_TO_*` handler lists.

Note: `plugins/exampleplugins.py` and `.cursorrules` describe an older `Plugin`-class
template. That template is NOT a runnable Cardinal plugin (it lacks the required
constants) and the host skips it on load — don't treat it as the real plugin format.

### Environment (provisioned by the update script)
- Python venv: `~/fpc-venv` (host deps installed: `requests`, `pytelegrambotapi`,
  `beautifulsoup4`, `lxml`, etc.).
- Host source cloned to `~/FunPayCardinal` so plugins can import `cardinal`, `tg_bot`,
  `FunPayAPI`, `Utils`. The host is NOT part of this repo.
- Use `~/fpc-venv/bin/python` directly, or `source ~/fpc-venv/bin/activate`.

### Lint / compile (no linter configured in repo)
```
~/fpc-venv/bin/python -m py_compile plugins/*.py
```

### Load / validate plugins (the closest thing to "running" them)
Plugins are loaded by the host BEFORE any FunPay auth, so they can be validated
offline (no golden_key / Telegram token needed). Run from the host repo root with
this repo's plugins copied into `~/FunPayCardinal/plugins/`:
```
cp plugins/*.py ~/FunPayCardinal/plugins/
cd ~/FunPayCardinal && ~/fpc-venv/bin/python plugin_load_test.py
```
`plugin_load_test.py` uses the host's real `Cardinal.load_plugin` / `is_plugin` /
`is_uuid_valid` to import each plugin, validate its metadata, and enumerate its
registered `BIND_TO_*` handlers. It exits non-zero if a non-template plugin fails.
If the harness file is missing (e.g. host was re-cloned), it just re-runs the host's
static loader over `./plugins/*.py`; recreate it or inline the loader loop.

### Running the full host end-to-end
Running FunPayCardinal for real requires a FunPay `golden_key` and (optionally) a
Telegram bot token in `~/FunPayCardinal/configs/_main.cfg`. Without valid creds the
host's account init retries indefinitely (it does not crash). For plugin development,
prefer the offline load/validate flow above.

### Gotchas
- The loader uses hardcoded relative paths (`plugins/...`), so always run plugin
  loading with cwd = `~/FunPayCardinal`.
- Plugins persist data under `storage/plugins/<UUID>/` relative to cwd.
- A plugin file whose first line comment contains `noplug` is intentionally skipped.
