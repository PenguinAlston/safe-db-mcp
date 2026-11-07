## Summary

Describe the change and the trust boundary it affects.

## Security impact

- Threat or failure mode addressed:
- Does this change alter accepted SQL syntax, authorization, approval, transaction state, or result handling?
- New residual risks:

## Validation

- [ ] `ruff check src tests`
- [ ] `pytest -q`
- [ ] Security regression tests added/updated
- [ ] Live database integration tests run (list engine/server and driver versions), or explicitly state they were not run
- [ ] Documentation updated (`README.md`, `SECURITY.md`, `AGENTS.md`, relevant docs)

## Secret/data hygiene

- [ ] No credentials, DB URLs, production SQL, customer data, or sensitive result sets are included
- [ ] API and MCP processes still have no database credentials
- [ ] Worker fencing-token and audit atomicity guarantees are preserved
