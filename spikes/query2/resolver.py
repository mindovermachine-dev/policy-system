#!/usr/bin/env python3
"""Free-text -> Capability entity resolution, two strategies compared.

Per q-approach4.md §10.5: the literature "mildly vindicates" Candidate A's
own unresolved critique of itself -- entity-linking research finds hybrid
(embedding + lexical) beats either alone, but pure fuzzy-string matching is
often *sufficient* on curated (not web-scraped) vocabularies, which this
graph's 68 capability names are. §10.5 and the mining pass's own "next
steps" call for running that comparison as a cheap head-to-head test on the
four known NL-mapping cases (H3/H8/H9/H11, extended here to also cover
H6/M3) before committing to an embeddings pipeline.

No embedding model is available in this environment without changing shared
infrastructure (the local Ollama server needs `--embeddings` at startup,
which this spike declined to do rather than touch a shared service for a
one-off comparison -- see mining-pass.md's own discipline about not making
infra changes lightly). scikit-learn's TF-IDF + cosine similarity is used
here as the "beyond lexical substring" comparison point instead: it captures
word overlap/weighting across the full name+description text the same way a
resolver reaching for semantic similarity would, without needing a network
call or a GPU. It is explicitly *not* a dense neural embedding, and that
distinction is preserved in how results are reported, not glossed over.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query1"))

from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: E402
from sklearn.metrics.pairwise import cosine_similarity  # noqa: E402


@dataclass
class ResolverHit:
    capability_id: str
    capability_name: str
    score: float


# Measured gap, not a hypothetical one: the bake-off
# (experiment_resolver_bakeoff.py) found neither lexical substring nor
# TF-IDF resolves a bare acronym that never appears in the graph's own text
# ("MFA" against a description that says "authentication" but never "MFA";
# "PII" never appearing verbatim either) -- both need the term *expanded*
# first. A closed, curated glossary for the small, stable set of compliance
# acronyms this domain actually uses is a cheaper, more deterministic fix
# than reaching for embeddings to solve a problem that's really "the
# vocabulary was abbreviated," not "the vocabulary is semantically distant."
# Kept intentionally small -- this is the same "closed library" discipline
# q-approach4.md's Candidate A uses, not an attempt to cover every possible
# acronym in the domain.
ACRONYM_GLOSSARY: dict[str, str] = {
    "mfa": "multi-factor authentication",
    "pii": "personal identifiable information personal data",
    "sbom": "software bill of materials",
    "dpia": "data protection impact assessment",
    "sso": "single sign-on authentication",
    "siem": "security information and event management logging",
}


def expand_acronyms(text: str) -> str:
    """Append expansions for recognized acronyms; never rewrite the input.
    An earlier version replaced every hyphen in the whole query before
    tokenizing (to normalize "multi-factor" style glossary keys), which had
    a real, measured side effect: it also split apart non-acronym compounds
    like "rate-limiting" into "rate limiting," reintroducing the false-
    positive word-overlap matches the hyphenated form correctly avoided (see
    experiment_resolver_bakeoff.py's H9 rows). Fixed by normalizing only for
    the *lookup* (checking each whitespace-delimited word against the
    glossary after stripping its own hyphens) and leaving the original text
    -- hyphens and all -- untouched in what actually gets matched against
    the catalog.
    """
    normalized_words = [w.replace("-", "") for w in text.lower().split()]
    expansions = [ACRONYM_GLOSSARY[w] for w in normalized_words if w in ACRONYM_GLOSSARY]
    if not expansions:
        return text
    return text + " " + " ".join(expansions)


class LexicalResolver:
    """Substring match over name AND description (extends
    query_mechanism_v1.EntityResolver.resolve_capability, which only checks
    name -- widened here since free-text scenario phrases like "doesn't
    encrypt data at rest" are far more likely to hit a description than a
    short capability name).
    """

    def __init__(self, capabilities: list[tuple[str, str, str]]):
        self.capabilities = capabilities  # (id, name, description)

    def resolve(self, text: str, top_k: int = 3) -> list[ResolverHit]:
        t = text.strip().lower()
        hits = []
        for cid, name, desc in self.capabilities:
            haystack = f"{name} {desc}".lower()
            if t in haystack or any(word in haystack for word in t.split() if len(word) > 3):
                # score: fraction of query words found, favors more specific matches
                words = [w for w in t.split() if len(w) > 3] or [t]
                score = sum(1 for w in words if w in haystack) / len(words)
                if t in haystack:
                    score += 1.0  # exact phrase hit ranks above word-overlap-only
                hits.append(ResolverHit(cid, name, score))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]


class TfidfResolver:
    def __init__(self, capabilities: list[tuple[str, str, str]]):
        self.capabilities = capabilities
        corpus = [f"{name} {desc}" for _, name, desc in capabilities]
        self.vectorizer = TfidfVectorizer(stop_words="english")
        self.matrix = self.vectorizer.fit_transform(corpus)

    def resolve(self, text: str, top_k: int = 3, min_score: float = 0.08) -> list[ResolverHit]:
        vec = self.vectorizer.transform([text])
        sims = cosine_similarity(vec, self.matrix)[0]
        ranked = sorted(
            ((score, i) for i, score in enumerate(sims) if score >= min_score),
            reverse=True,
        )[:top_k]
        return [
            ResolverHit(self.capabilities[i][0], self.capabilities[i][1], float(score))
            for score, i in ranked
        ]


class CapabilityResolver:
    """The resolver actually used by the catalog lookups: expand known
    acronyms, try lexical substring first (per §10.5, sufficient for this
    curated a vocabulary in the common case and free), fall back to TF-IDF
    cosine only when lexical finds nothing (per the bake-off: TF-IDF alone
    isn't strictly better -- it missed "doesn't encrypt data at rest" that
    lexical caught via plain substring containment of "encrypt" in
    "encryption" -- so this is lexical-first-with-fallback, not a wholesale
    replacement).
    """

    def __init__(self, capabilities: list[tuple[str, str, str]]):
        self.lexical = LexicalResolver(capabilities)
        self.tfidf = TfidfResolver(capabilities)

    def resolve(self, text: str, top_k: int = 5) -> list[ResolverHit]:
        expanded = expand_acronyms(text)
        hits = self.lexical.resolve(expanded, top_k=top_k)
        if not hits:
            hits = self.tfidf.resolve(expanded, top_k=top_k)
        return hits


if __name__ == "__main__":
    from falkordb import FalkorDB

    from catalog import compile_catalog

    db = FalkorDB(host="localhost", port=6379)
    g = db.select_graph("policy_system")
    cat = compile_catalog(g)

    lex = LexicalResolver(cat.all_capabilities)
    tfidf = TfidfResolver(cat.all_capabilities)

    for query in ["MFA", "multi-factor authentication", "SBOM", "rate limiting"]:
        print(f"\n{query!r}")
        print("  lexical:", lex.resolve(query))
        print("  tfidf:  ", tfidf.resolve(query))
