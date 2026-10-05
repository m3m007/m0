# lib/ — keys in, secrets out

Two modules every other part of this repo uses. Nothing here talks to the
network; they only hand out credentials and clean up after them.

## `keys.py` — get a key, keep it out of the file

```python
from lib import keys

api_key  = keys.token_plan_key()              # env var, else ~/.hermes/config.yaml
base_url = keys.base_url("openai")            # .../compatible-mode/v1
base_url = keys.base_url("anthropic")         # .../apps/anthropic
kimi     = keys.kimi_key()                    # backup lane, env only
print(keys.describe())                        # safe summary: source, length, 4-char hint
```

`redact(obj)` returns a **deep copy** with every secret replaced by
`<REDACTED>`. Use it on anything you are about to write to disk:

```python
from lib.keys import redact
json.dump(redact(litellm_kwargs), open("runs/request.json", "w"), indent=2)
```

It works on three levels, which is deliberate — pattern matching alone is
bypassable:

1. **Exact live-key match.** The actual key strings are held in process memory
   and replaced as substrings, including any 24-character slice of them. A key
   truncated by a log line still gets caught. No regex can miss this.
2. **Known credential shapes** — `sk-…` provider keys, `Bearer …`, and
   `api_key: <value>` / `x-api-key: <value>` pairs (with or without quotes).
3. **Dict keys named like secrets** (`api_key`, `authorization`, `token`,
   `password`, …) are replaced wholesale, whatever the value looks like.

Cycles, pydantic models, tuples and bytes are all handled, and the input object
is never mutated (there is a test for exactly that, because a "redacting"
function that writes through to its argument is worse than none).

`raw` strings in captured HTTP dumps are scrubbed too, including
`Authorization:`/`x-api-key:` header lines that arrive without a JSON key to
hang rule 3 on.

## `secret_scan.py` — the gate

```bash
python3 lib/secret_scan.py            # exit 0 clean, exit 1 leak found
python3 lib/secret_scan.py --verbose
bash .secrets-safe.sh                 # same thing, committable as a pre-commit hook
```

On a leak it prints **only** `FILE:LINE [rule]` — never the value — so running
the scanner cannot itself put a secret on a terminal, in CI output, or in a bug
report.

### Why it does not just `grep -F "$REAL_KEY"`
`grep` puts the key in a **process argument list** (readable by anyone with
`ps`) and often into shell history. Instead the scanner loads the live keys
in-process and does its own substring/window comparison. The key never leaves
the heap.

### Allowlist, and its one exception
Placeholders carrying an unmistakable tell (`fake`, `example`, `<REDACTED>`,
`${ENV_VAR}` references) are forgiven so documentation and tests can *describe*
a secret shape. But the live-key test runs **before** the allowlist, so the
owner's real key is flagged even if it happens to contain the word `example`.
That ordering is pinned by `test_scanner_real_key_beats_the_placeholder_allowlist`.

The allowlist deliberately contains nothing a random key body could plausibly
contain (no `"1234"`, no `"abcdef"`, no single letters). Test fixtures stay
clean by **assembling decoys at runtime from fragments**
(`tests/test_keys.py::decoy_key`) rather than by weakening the rules — verified
by `test_this_test_file_contains_no_whole_credential_literal`, which scans this
file's own source.

### Why the gate is trustworthy
`test_scanner_negative_control_is_not_vacuous` plants a decoy in a temp file,
runs the scanner as a subprocess, and asserts **exit 1**. Without it, "scan
passes" would be meaningless — an empty rule list also passes.

## The rule this repo runs on

> **Keys are env-only. Nothing secret is ever committed, logged, or printed —
> including by tools whose job is to find secrets.**

Practical consequences elsewhere in the repo:
- never pass a key in a curl argument (`-H "Authorization: Bearer $KEY"` puts it
  in `ps` and shell history — use the env-var form the shell expands at use time,
  or a config file);
- every `repro/` capture goes through `redact()` before it is written;
- `runs/` and `*.log` are gitignored, because logs are where leaks actually
  happen.
