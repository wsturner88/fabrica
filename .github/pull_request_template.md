## Summary

<!-- What does this change, and why? Link the issue: Fixes #123 -->

## Checklist

- [ ] Each test file I touched or affected passes when run on its own (`python tests/test_<name>.py`), using temp paths for `BAMBU_MCP_CONFIG` / `BAMBU_MCP_CACHE`; nothing was run against a real printer
- [ ] New or changed behaviour has tests
- [ ] Docs updated, and `CHANGELOG.md` has a line under *Unreleased* for user-visible changes
- [ ] No personal info, real IP addresses, host names, serial numbers, access codes or API keys in code, tests, docs, screenshots or logs
- [ ] No new dependency (or it was discussed in an issue) and no copied third-party code
- [ ] New REST routes / MCP tools have a `read` or `control` scope in `app/security.py`
- [ ] UI changes: status shown in words (not colour alone), touch-friendly, checked at phone width and 1024x600, screenshots attached
- [ ] I agree my contribution is licensed under the project's MIT licence
