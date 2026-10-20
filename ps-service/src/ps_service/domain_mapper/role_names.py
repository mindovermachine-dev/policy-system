"""Deterministic normalisation of a Role name, applied before `identity.role_id`.

The extraction model names the same actor in several spellings ("Manufacturer",
"manufacturers", "The manufacturer"). `role_id` hashes the name it is given, so each spelling
would become its own Role. `normalise_role_name` removes the spellings that differ only in
whitespace, a leading article or a plural final word, so they reach `role_id` as one name.

The rules are small and explicit, with no inflection library, and they are intentionally
limited: a name that differs in more than that ("Manufacturers of products with digital
elements") is NOT collapsed here; two such Roles are surfaced by the Role near-miss review.
Pure and deterministic: no I/O, no logging.
"""

from __future__ import annotations

__all__ = ["normalise_role_name", "role_name_key"]

_ARTICLES = frozenset({"the", "a", "an"})
_MIN_SINGULARISED_LENGTH = 4  # shorter words ("gas", "bus") are left alone
_KEPT_ENDINGS = ("ss", "us", "is")  # "business", "status", "analysis" are already singular


def _singular(word: str) -> str:
    """Singularise `word` with the explicit rules, or return it unchanged.

    `ies` becomes `y`, `sses` becomes `ss`, and a trailing `s` is dropped unless the word ends
    `ss`, `us` or `is`. A word shorter than four characters and an all-uppercase word (an
    acronym such as `DNS`) are never changed.
    """
    lowered = word.lower()
    if len(word) < _MIN_SINGULARISED_LENGTH or word.isupper() or not lowered.endswith("s"):
        return word
    if lowered.endswith("ies"):
        return f"{word[:-3]}{'Y' if word[-3:-2].isupper() else 'y'}"
    if lowered.endswith("sses"):
        return word[:-2]
    if lowered.endswith(_KEPT_ENDINGS):
        return word
    return word[:-1]


def normalise_role_name(raw: str) -> str:
    """Return `raw` without surrounding or repeated whitespace, one leading article and a plural.

    Whitespace runs collapse to one space; one leading article (`the`, `a`, `an`, any case) is
    dropped when something follows it; only the last word is singularised. The original casing
    is kept, so the result is a display name; compare names through `role_name_key`.

    Args:
        raw: A Role name as the extraction model wrote it.

    Returns:
        The normalised display name; an empty string for a blank `raw`. Idempotent.
    """
    words = raw.split()
    if len(words) > 1 and words[0].lower() in _ARTICLES:
        words = words[1:]
    if words:
        words[-1] = _singular(words[-1])
    return " ".join(words)


def role_name_key(raw: str) -> str:
    """Return the case-insensitive comparison key of `raw`'s normalised name.

    Args:
        raw: A Role name as the extraction model wrote it.

    Returns:
        The casefolded `normalise_role_name(raw)`; two spellings of one actor share a key.
    """
    return normalise_role_name(raw).casefold()
