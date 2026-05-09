<!--
Thanks for contributing! Before opening this PR:
- Read CONTRIBUTING.md and the engineering-style rules in CLAUDE.md.
- Sign your commits with `git commit -s` (DCO required).
- Run `pytest tests --ignore=tests/test_sdk_contract.py -q` and `python scripts/check_file_line_budget.py`.
-->

## Summary

<!-- One or two sentences. What changes, and why. -->

## Type of change

- [ ] Bug fix
- [ ] New feature / new agent
- [ ] Refactor (no behavior change)
- [ ] Docs / tooling
- [ ] Security fix
- [ ] Other (describe):

## Checklist

- [ ] I read `CONTRIBUTING.md` and the relevant sections of `CLAUDE.md`.
- [ ] My commits are signed off (`-s`) per the DCO.
- [ ] I added or updated tests where it made sense.
- [ ] I ran the full test suite locally and it passed.
- [ ] I ran `python scripts/check_file_line_budget.py` and it passed.
- [ ] If I changed a money path, I used integer cents only.
- [ ] If I added an outbound URL, it goes through `core/url_security.py`.
- [ ] If I added a hosted-service call, it goes through `core/hosted_client.py` and degrades gracefully when `AZTEA_HOSTED_API_URL` is unset.
- [ ] If I changed a function signature, I updated every caller in this PR.
- [ ] I did not introduce a hardcoded `aztea.ai` URL in core/server/agents code.

## Test plan

<!-- How did you verify this works? Commands run, manual tests, screenshots. -->

## Notes for reviewers

<!-- Anything non-obvious — design decisions, trade-offs, edge cases worth a second look. -->
