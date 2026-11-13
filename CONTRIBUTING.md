# Contributing to Binday

Thanks for helping. Bug reports, fixes and small improvements are all welcome.

## Before you start

- **Bugs:** [open a bug report](https://github.com/zaryabmustafa/binday/issues/new?template=bug_report.yml). Include your Mattermost version, how it's installed, and what the Results screen or `binday.log` says.
- **Ideas:** [open a feature request](https://github.com/zaryabmustafa/binday/issues/new?template=feature_request.yml) before writing code for anything bigger than a small fix, so we can agree on the approach first.
- **Security problems:** don't open an issue; see [SECURITY.md](SECURITY.md).

Some things are left out on purpose. Please don't send pull requests that:

- delete anything without a dry run, a review and the typed `DELETE` confirmation,
- remove the re-check that happens right before each item is deleted,
- delete files on disk before Mattermost's own `mmctl` delete has succeeded,
- add telemetry or a self-updater.

## Build and test

```bash
python -m venv .venv
```
```bash
.venv/Scripts/python -m pip install -r requirements.txt
```
```bash
.venv/Scripts/python test_app.py
```
```bash
.venv/Scripts/python gui.py --demo
```

Any change to what gets deleted must also pass against the real test server in [testenv/](testenv/README.md): reset, seed, `verify.py --pre`, purge, `verify.py --post`. More in [docs/development.md](docs/development.md).

## Pull requests

- Keep each pull request to one change, and say what it fixes and how you tested it.
- Run `python test_app.py` before you push; for deletion changes, include the `verify.py --post` output.
- UI changes follow [DESIGN.md](DESIGN.md): every screen uses the shared frame, colours come from the theme tokens, and red is only for deleting. Include light and dark screenshots.
- Keep dependencies small. New ones need a good reason.
- Use LF line endings.

By contributing, you agree that your work is released under the [MIT License](LICENSE).
