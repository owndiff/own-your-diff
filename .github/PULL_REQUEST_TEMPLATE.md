## Summary

-

## Verification

- [ ] `ruff check .`
- [ ] `pytest`
- [ ] Tested a real git repository or added focused tests for the changed behavior.

## Security and Privacy

- [ ] No secrets, private source code, local absolute paths, or personal machine details are included.
- [ ] Generated `.owndiff/` artifacts are not committed.
- [ ] User-controlled repository content is treated as untrusted input.

## OwnDiff Gate

- [ ] For risky code changes, `.owndiff/ownership-gate.json` allows the push/MR action.
- [ ] Not applicable because this change is docs, tests, config, or low-risk maintenance.
