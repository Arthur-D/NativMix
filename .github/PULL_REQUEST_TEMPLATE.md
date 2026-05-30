## Summary

<!-- What problem does this PR solve? Link issues with "Fixes #123" or "Closes #123" when applicable. -->

## Changes

<!-- Brief description of your approach. Keep the diff focused — one topic per PR when possible. -->

## Test plan

- [ ] `pytest -q` passes
- [ ] (optional) `ruff check lib/`
- [ ] (optional) `mypy lib/`
- [ ] Manual testing performed:
  - **OS / desktop:**
  - **Hardware:** USB / MIDI / none
  - **What you verified:**

## Checklist

- [ ] I read [CONTRIBUTING.md](../CONTRIBUTING.md) and followed the project rules (threads/signals, XDG paths, no silent `except Exception: pass`, etc.)
- [ ] GUI slots use `@_slot_guard` where applicable; `QPushButton.clicked` slots accept `checked: bool = False`
- [ ] No version bump unless explicitly requested by a maintainer
- [ ] User-facing text in issues/docs is English (code comments stay English)

## Notes for reviewers

<!-- Breaking changes, follow-up work, maintainer-only steps (packaging, hardware validation), screenshots, etc. -->
