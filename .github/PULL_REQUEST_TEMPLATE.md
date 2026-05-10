## Summary

What does this PR change?

## Why

Link to the issue this addresses, or describe the problem if there's no issue yet.

## How

Brief description of the approach. Note any design decisions worth flagging for review.

## Testing

- [ ] `ruff check . && ruff format --check . && mypy src/p4net && pytest` all pass.
- [ ] If this touches integration code paths, the relevant marker suite (`integration`, `requires_p4c`, `requires_bmv2`) was run locally.
- [ ] New code has unit tests; coverage on touched modules has not regressed.
- [ ] CHANGELOG `[Unreleased]` updated if user-visible.

## Notes for the reviewer

Anything specific you want eyes on.
