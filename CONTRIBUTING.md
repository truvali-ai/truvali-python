# Contributing to truvali

Thank you for helping to improve the official Python SDK for the Truvalı platform. This document describes how to set up a development environment, which checks a change must pass and which conventions the code follows.

For a security problem, do not open an issue or a pull request; follow [SECURITY.md](SECURITY.md).

## Development setup

You need Python 3.10 or newer and git.

```bash
git clone https://github.com/truvali-ai/truvali-python.git
cd truvali-python
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

On Windows, activate the environment with `.venv\Scripts\activate`.

The `dev` extra installs the tools the checks use: `ruff`, `mypy`, `pytest`, `build`, `twine` and the type stubs for `requests`.

## Checks

CI runs the following commands on Python 3.10, 3.11, 3.12 and 3.13 for every push to `main` and every pull request. Run them locally, from the repository root, before you open a pull request:

```bash
ruff check .
ruff format --check .
mypy
pytest
python -m build
twine check --strict dist/*
```

| Command | What it checks |
| --- | --- |
| `ruff check .` | Lint rules, import order and Google-style docstrings |
| `ruff format --check .` | Formatting; run `ruff format .` to apply it |
| `mypy` | Strict type checking of `src` and `tests` |
| `pytest` | The test suite |
| `python -m build` | That the source distribution and the wheel build |
| `twine check --strict dist/*` | That the distributions and the README are valid for PyPI |

`python -m build` writes to `dist/`, which git ignores. All six commands must pass.

## Code conventions

- **English only.** Names, docstrings, messages and documentation are written in English.
- **Plain ASCII in source files.** Do not use non-ASCII characters in `.py` files. In documentation, the brand is written "Truvalı" in prose and `truvali` for package, module and code names.
- **Docstrings everywhere.** Every module, class, function and method has a Google-style docstring, tests and private helpers included. Describe the purpose, the arguments (`Args:`), the return value (`Returns:` or `Yields:`) and the exceptions raised (`Raises:`).
- **No comments in code.** Explanations belong in docstrings. This includes directive comments: do not add `# type: ignore`, `# noqa` or similar. If a check fails, change the code so that it passes.
- **Type hints and mypy strict.** Annotate every function, method and attribute. The package ships `py.typed`, so the annotations are part of the public API.
- **Line length 100**, formatted by `ruff format`.
- **Never log sensitive data.** Do not log request or response bodies, query strings, tokens, API secrets, webhook secrets or personal data, and do not put them into exception messages. Log the method, path, status, request id and timing only.
- **Keep the public API in `truvali.__all__`.** A name that users may import is exported from `truvali/__init__.py` and listed in `__all__`. Everything else is private; private modules, functions and methods start with an underscore. Argument errors of public methods raise `InvalidArgumentError`, not a plain `ValueError`, so that `except TruvaliError` catches every error the SDK raises on purpose.
- **Dependencies.** The only runtime dependency is `requests`. Please discuss a new dependency in an issue first.
- **Compatibility.** Code must run on every supported Python version (3.10 to 3.13).

## Tests

- Tests live in `tests/` and run offline. No test may call a real server; HTTP responses come from the scripted adapter and the fake clock in `tests/fakes.py`.
- Use fake data only: `https://truvali.example.com` as the base URL, `example.com` e-mail addresses, made-up refs, keys and secrets. Never use real credentials, real customer data or real names.
- Add or update tests for every change in behaviour, including the failure paths.
- Tests follow the same conventions as the package: type hints, docstrings and no comments.

## Pull requests

1. For a larger change, open an issue first and describe what you want to do.
2. Create a branch from `main` and keep the change focused on one topic.
3. Add tests and update the documentation (`README.md` and the docstrings) when behaviour changes.
4. Add an entry to `CHANGELOG.md` under "Unreleased", in the fitting group ("Added", "Changed", "Deprecated", "Removed", "Fixed" or "Security").
5. Run all checks locally.
6. Open the pull request and describe what changes and why. Point out anything that changes the public API.

A maintainer reviews the pull request. CI must pass before it is merged.

By contributing, you agree that your contribution is licensed under the [Apache License 2.0](LICENSE).

## Release process (maintainers)

Releases are published by the `release` workflow when a version tag is pushed.

1. Set the new version in `src/truvali/_version.py`.
2. In `CHANGELOG.md`, move the "Unreleased" entries under a heading with the new version and the release date, for example `## [0.2.0] - 2027-01-15`.
3. Commit both changes on `main` and wait for the `check` workflow to pass.
4. Create and push the tag `vX.Y.Z`, for example:

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

5. The `release` workflow verifies that the tag matches the version in `src/truvali/_version.py`, builds the distributions, runs `twine check --strict` and publishes them to TestPyPI.
6. Check the release on TestPyPI. A maintainer then approves the deployment to the `pypi` environment on GitHub.
7. After the approval, the workflow publishes the same distributions to PyPI.

If the tag does not match the package version, the workflow fails before anything is built or published. A version that has been published cannot be published again: fix the problem and release a new version.
