# Repository instructions

## Bias toward simplicity

- Prefer fewer features, settings, and flags.
- Do not add configurability for cases that do not exist yet. A hardcoded value
  that is easy to edit is better than a flag that is easy to forget.
- Prefer small, simple, individual processes over compound, complex,
  configurable ones.

## Ground assumptions

- Write for the inputs that actually occur.
- State important assumptions in a comment or in the response instead of
  defending against every hypothetical edge case.
- Prefer failing loudly on unexpected input over silently handling it.

## Keep implementations scoped

- Implement what was requested, then stop. Do not add adjacent improvements or
  generalize beyond the request.
- If an instruction is wrong, incomplete, or based on a false assumption, say
  so plainly before or alongside the work.
- Silently correct harmless mistakes such as typos, stale line numbers, and
  off-by-one paths when the intended request is clear.

## Comments

- Keep comments short and explain why, not what.
- Do not restate the code or document every negative case.

## Git

- Leave changes unstaged. Do not run `git add`.
- If a command stages changes as a side effect, unstage them before finishing.

## Technical limitations

- Prefer optimization or additional compute over reducing output quality.
- Start with code-level optimizations that preserve output quality. If those
  are insufficient, consider faster implementation languages or more CPU and
  memory before compromising the result.

## Documentation check

- After changing files, check whether `README.md` still accurately describes
  affected pipeline layout and stages, file contracts, script usage, and
  league or scoring assumptions.
- Update the affected README sections when they are stale. If the README is not
  affected, say so briefly in the final response.
