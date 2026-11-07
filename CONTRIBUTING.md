# Contributing

Please read [AGENTS.md](AGENTS.md) and [SECURITY.md](SECURITY.md) before changing security-sensitive code.

Install development dependencies with `pip install -e ".[dev]"`. Run `ruff check src tests` and `pytest -q` before submitting a pull request.

Add regression tests for both the reported behavior and relevant rejection/race/failure paths. Unit tests are not live database integration tests. State exactly which database engines and driver versions you tested.

Never commit credentials, database URLs, customer data, production SQL, or sensitive result sets. Do not log SQL, bind values, URLs, secrets, or driver exception strings.

Changes to SQL parsing, database adapters, worker claims, reviewer approvals, connection pooling, or result handling require a security-focused explanation in the pull request and updates to `README.md` and/or `SECURITY.md` where appropriate.
