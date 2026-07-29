# Contributing to Myrmex

Myrmex is alpha software focused on OpenCode. Keep changes scoped, preserve
contracts, and add tests for behavior changes.

Before opening a pull request:

1. install the development dependency with python3 -m pip install -r requirements-dev.txt;
2. run ./scripts/run-tests.sh;
3. run ./scripts/check-package.py;
4. inspect git diff --check, generated files, and sensitive data.

Do not commit local state, browser profiles, secrets, release archives, or
external source checkouts. Commits and pushes require explicit authorization
when performed through Myrmex.
