# Building and contributing

## Run from source

```bash
python -m venv .venv
```
```bash
.venv/Scripts/python -m pip install -r requirements.txt
```
```bash
.venv/Scripts/python gui.py --demo
```

`--demo` uses made-up data and connects to nothing. Drop it to use a real server, or add `--web` to open the app in a browser at http://127.0.0.1:8550. On macOS and Linux use `.venv/bin/python`.

| File | What it is |
|---|---|
| `app.py` | The engine (connect, scan, apply) and the command line |
| `gui.py` | The desktop app (Flet). UI only, no deletion logic |
| `demo.py` | The synthetic server behind `--demo` |
| `test_app.py` | Self-check with fakes: `python test_app.py` |
| `testenv/` | A real Mattermost test server in Docker ([testenv/README.md](../testenv/README.md)) |
| `DESIGN.md`, `mockup/` | The design system and the approved screens |

## Test

```bash
.venv/Scripts/python test_app.py
```

Any change to how things are deleted should also pass against the real test server: reset, seed, `verify.py --pre`, purge, `verify.py --post`. See [testenv/README.md](../testenv/README.md).

## Build the desktop app

```bash
.venv/Scripts/python -m pip install "flet[all]==1.1.2"
```
```bash
.venv/Scripts/flet build windows
```

Use `macos` or `linux` on those systems; each platform builds only on itself. The first build downloads Flutter. App name, version, icon and what goes into the bundle are set in `pyproject.toml`.

On Windows you also need:
- Visual Studio Build Tools with **Desktop development with C++**.
- **Developer Mode** on (Settings → System → For developers), because Flutter needs symbolic links.
- No spaces in the Flutter SDK or `PUB_CACHE` paths. If your user folder has a space, put Flutter on `PATH` from somewhere like `D:\tools\flutter` and set `PUB_CACHE=D:\tools\pub-cache`.

### Portable Windows exe

The single-file `Binday-portable.exe` is built with PyInstaller through `flet pack`, so it needs no Flutter or Visual Studio:

```bash
.venv/Scripts/python -m pip install pyinstaller
```
```bash
.venv/Scripts/flet pack gui.py -y -n Binday-portable -i assets/icon.ico --add-data "assets:assets" --product-name Binday
```

The exe lands in `dist/`.

## Release

[`.github/workflows/build.yml`](../.github/workflows/build.yml) builds Windows, macOS and Linux. Run it from the Actions tab for test builds, or push a tag such as `v1.2.0` to build all three and publish a GitHub Release with `Binday-windows-x64.zip`, `Binday-portable.exe`, `Binday-macos.zip` and `Binday-linux-x64.tar.gz`. Bump `version` in `pyproject.toml` first.

## Brand assets

The icon, logos, banners and social preview come from `assets/brand/make_brand.py`, and the README screenshots from `assets/readme/capture.ps1`:

```bash
.venv/Scripts/python -m pip install pillow fonttools
```
```bash
.venv/Scripts/python assets/brand/make_brand.py
```
```bash
powershell -ExecutionPolicy Bypass -File assets/readme/capture.ps1
```

Brand rules (name, mark, voice) are in [DESIGN.md](../DESIGN.md#brand).
