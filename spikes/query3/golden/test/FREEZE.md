# Test-set freeze log

The test set is **empty and locked**. It must not be populated by anyone who
has seen the matchers it will be used to grade. See
[`../README.md`](../README.md) — "Authoring the test set."

When the test set is authored, record the freeze here. A freeze entry is the
only thing that makes a later test-set score interpretable: without it, a
pass could reflect a matcher quietly tuned after the questions were seen.

## Freeze entries

(no entries — test set not yet authored)

<!-- Template for the first freeze entry:

### Freeze 1 — YYYY-MM-DD

- **Authored by:** <name>  (must NOT be the author of clarifier.py / the
  catalog templates being graded)
- **Cold-authored:** <yes — confirm the author had no sight of the matchers>
- **Question count:** <n>
- **Matchers live at freeze:** <git SHA or file list of clarifier.py,
  catalog templates, query_mechanism_vN.py as they existed at freeze time>
- **Answer computation:** confirm every golden answer was produced via
  harness/compute_answer.py (independent path), not by running the mechanism.
- **Locked:** <date> — after this date, test/questions.jsonl is read-only
  except to ADD genuinely new cold questions (which get their own entry).

-->
