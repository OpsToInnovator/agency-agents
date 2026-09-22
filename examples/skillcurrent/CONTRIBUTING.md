# Contributing to SkillCurrent

SkillCurrent is in a small beta. Bug reports and fixes are welcome; please
open an issue before starting anything larger, so we can agree it fits.

## Set up and run the tests

```bash
cd examples/skillcurrent
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The browser test of the beta form runs when Node and the `playwright`
package are available and is skipped otherwise. CI runs everything on
Linux and macOS (`.github/workflows/skillcurrent-tests.yml`).

## Rules the code keeps

- **Standard library only.** No runtime dependencies. Tests may use pytest.
- **Additive schema changes only.** Add columns and tables, bump
  `SCHEMA_VERSION` in `store.py`, and add a migration test. Never drop or
  rewrite data a team has collected.
- **Every behaviour change has a test.** A bug fix starts with a test that
  fails without it.
- **No claim beyond the evidence.** Messages, docs, the beta page and the
  ads say what the tool does and nothing more. A check result is evidence
  about bytes, never a promise about how an agent will behave.
- **The beta page's walkthrough is a test.** If you change CLI output that
  the page shows, `tests/test_beta_measurement.py` will tell you; update
  the page too.
- **No third-party requests** from the beta page or the web UI.
- **Security-sensitive changes** (auth, `--public`, the installer's write
  paths) get a line in `SECURITY.md` if the model changes.

## Pull requests

Keep them small and focused, describe what changed and how you tested it,
and make sure `pytest` passes. By contributing you agree your work is
released under the licence that covers this directory.

## Security problems

Don't open a public issue. See [SECURITY.md](SECURITY.md).
