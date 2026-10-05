# Smoke brief: two independent fixes, then a review

This repo is the end-to-end smoke target for the team workflow. It is small and
synthetic on purpose: a 5,500-line copy of the netflow repo is not a repeatable
smoke test and burns quota re-discovering the same wiring.

Two bugs live in two **separate** modules, so two fixers can run in parallel
without ever touching the same file. A third task (the test suite) depends on
both, so it must not start until they finish.

## Task A — `pkg/durations.py`

`humanise(seconds)` must render a duration as `1h2m3s` style text:
- drop any zero component except when the whole value is zero (`0` -> `"0s"`)
- keep units in order h, m, s
- `humanise(3661)`  -> `"1h1m1s"`
- `humanise(90)`   -> `"1m30s"`
- `humanise(0)`    -> `"0s"`
- `humanise(7200)` -> `"2h"`

Gate: `python3 -m pytest -q tests/test_durations.py` exits 0.
Path you may touch: `pkg/durations.py` only.

## Task B — `pkg/normalise.py`

`slugify(text)` must turn arbitrary text into a lower-case hyphen slug:
- collapse runs of non-alphanumerics into a single `-`
- strip leading/trailing `-`
- `slugify("  Hello, World!  ")` -> `"hello-world"`
- `slugify("HTTP  Status--200")` -> `"http-status-200"`
- `slugify("___")`               -> `""`

Gate: `python3 -m pytest -q tests/test_normalise.py` exits 0.
Path you may touch: `pkg/normalise.py` only.

## Task C — full suite (depends on A and B)

`python3 -m pytest -q` exits 0 over the whole repo, and
`python3 -m compileall -q pkg` exits 0.

Path you may touch: `tests/` only.

## Rules for every task

- Do not touch a path outside the one you are given.
- Do not add dependencies; stdlib only.
- Never write a secret into any file.
- When you are blocked or done, write your status to `DONE.md` and finish anyway
  (that is the self-wake contract: the next agent reads the file, not your reply).
