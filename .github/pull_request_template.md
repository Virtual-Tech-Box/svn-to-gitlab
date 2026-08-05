## What this changes

<!-- What breaks without this change? Describe the problem, not just the diff. -->

## Why this approach

<!-- Alternatives you rejected, and why. Skip for trivial fixes. -->

## Testing

<!-- How you verified it. For conversion changes, name the fixture case you added. -->

- [ ] `pytest` passes locally
- [ ] Added or updated tests covering this change
- [ ] End-to-end tests still pass (`pytest tests/test_e2e.py`)

## Checklist

- [ ] Errors added here carry an actionable `remedy`
- [ ] No credential can reach a log, report or stored git remote via this path
- [ ] Conversion output stays deterministic (identical input, identical commit SHAs)
- [ ] Anything skipped or narrowed is reported to the operator, not silent
- [ ] Docs updated if behaviour or configuration changed
