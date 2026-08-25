# Universal rules

## Bias towards simplicity
Fewer features, settings, and flags. Don't build configurability for a case that
doesn't exist yet — agentic AI makes editing the source directly cheap, so a
hardcoded value that's easy to change beats a flag that's easy to forget.

Prefer individual, small, simple processes over compound, large, complex, configurable
processes.

## Ground assumptions, don't chase edge cases
Write for the inputs that actually occur. State the assumption in a comment or
in your reply rather than defending against every hypothetical. Prefer failing
loudly on unexpected input over silently handling it.

## Minimal implementations of instructions
Implement what was asked and stop. Don't widen scope, add adjacent
improvements, or generalize past the request.

But don't be a pushover about the instructions themselves:
- If they're wrong, missing information, or resting on an incorrect assumption,
  say so plainly — before or alongside the work, not after.
- If the error doesn't change what was meant (a typo, a wrong line number, an
  off-by-one in a path), just fix it silently and keep going.

## Comments
Simple and succinct. Explain why, not what. Skip comments that restate the code. 
Avoid writing comments for every negative case.

## Leave changes unstaged
When you finish working, leave your changes unstaged — don't `git add` them.
If a command stages as a side effect (e.g. `git mv`), unstage with
`git restore --staged` before finishing.

## Prefer optimization over compromising quality when presented with technical limitations
For example, if a Python script is running slowly, prefer re-writing key paths in Rust or increasing
CPU and memory over compromising the script's output. Obviously code-level (still Python) optimizations which don't
compromise output quality are still the gold standard..