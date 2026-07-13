# Contributing

Thanks for looking. LLM OS is a small, security-positioned codebase; the bar
for a change is that it keeps the guarantees in [SECURITY.md](SECURITY.md)
true and provable.

## Setup

```bash
pip install -r requirements-dev.txt
pytest            # 228 tests, all with a mocked LLM — no Ollama needed
```

Runtime deps are pinned in `requirements.txt`; the full closure is in
`requirements.lock`. CI runs the suite on Linux, macOS and Windows.

## The one rule that matters

**A bug fix comes with a test that reproduces it.** Every test in `tests/`
opens with a docstring naming the real failure it guards — an audit chain
forking under concurrent writers, a model narrating a blocked action, a
relevance floor deleting a real answer. Write that test first, watch it fail,
then fix. This is how a solo project keeps a guarantee from silently rotting.

## Style

- Match the surrounding code; there is no separate house style.
- Keep the kernel's contract intact: the model routes, deterministic
  sandboxed code executes, every action is audited. A change that lets the
  model *do* something instead of *propose* it will be rejected.
- Touch only what the change needs.

## What gets merged fast

A confirmed bug with a failing test attached, a portability fix backed by the
CI matrix, or a docs correction. Larger refactors: open an issue first so we
agree on the seam before you write a week of code.
