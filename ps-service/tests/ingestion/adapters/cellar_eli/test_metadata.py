"""Tests for ps_service.ingestion.adapters.cellar_eli.metadata.

Fixture XHTML snippets are small, hand-trimmed, but structurally realistic
against real Cellar/ELI markup (`eli-container`/`eli-main-title`/
`eli-subdivision`/`eli-title` classes, `art_NN`/`art_NN.tit_1` id
convention) — see `spikes/cellar1/LEARNINGS.md` for the real structure
these mirror.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

import pytest

from ps_service.ingestion.adapters.cellar_eli.metadata import (
    _BASE_ACT_VERSION,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _base_celex,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _base_celex_or_none,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _date_role,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _find_effective_date_from_rdf,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _instrument_type_from_celex,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _own_subjects,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _parse_reification_blocks,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _parse_subject_blocks,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _pick_effective_date,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _resolve_predicate_candidates,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    extract_metadata,
)
from ps_service.ingestion.adapters.errors import CellarParseError

_TRANSPOSITION_FIXTURE = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-main-title">Directive (EU) 2022/2555 of the European Parliament
and of the Council</div>
<div class="eli-subdivision" id="art_41">
<div class="eli-title" id="art_41.tit_1">Article 41 Transposition</div>
<div id="041.001">1. By 17 October 2024, Member States shall adopt and publish the measures
necessary to comply with this Directive.</div>
</div>
</div>
</body>
</html>
"""

_ENTRY_INTO_FORCE_FIXTURE = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-main-title">Regulation (EU) 2024/2847 of the European Parliament
and of the Council</div>
<div class="eli-subdivision" id="art_71">
<div class="eli-title" id="art_71.tit_1">Article 71 Entry into force and application</div>
<div id="071.001">This Regulation shall enter into force on the twentieth day following
publication. It shall apply from 11 December 2027.</div>
</div>
</div>
</body>
</html>
"""

_NO_DATE_HEADING_FIXTURE = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-main-title">Some Regulation</div>
<div class="eli-subdivision" id="art_1">
<div class="eli-title" id="art_1.tit_1">Article 1 Subject matter</div>
<div id="001.001">This Regulation establishes rules.</div>
</div>
</div>
</body>
</html>
"""


def _rdf_root(rdf: bytes) -> ET.Element:
    """Parse an RDF/XML fixture *without* stripping namespaces.

    Subject-block discovery (`_parse_subject_blocks`) depends on the
    namespace-qualified `rdf:about` attribute, so this must not run
    `_strip_namespace` first.
    """
    return ET.fromstring(rdf)  # noqa: S314 — fixture XML authored in-repo, not untrusted


# RDF/XML fixtures below mirror the real, live-captured NIS2 shape quoted in
# SLICE_0_FINDINGS.md §2(a)/(b)/(c): subject IRI
# `http://publications.europa.eu/resource/oj/JOL_2022_333_R_0002`, predicate
# `cdm:date_transposition` (local name `date_transposition`) asserted twice
# with real values `2024-10-18`/`2024-10-17`, and `resource_legal_id_celex`
# `32022L2555` on that same subject. The plain
# `http://publications.europa.eu/resource/celex/32022L2555` subject is real
# too (SLICE_0_FINDINGS.md §2(c)) but, in real data, carries neither
# predicate — here it is deliberately given a conflicting `date_transposition`
# value to prove subject-scoped grouping, not flattening.
_RDF_TWO_SUBJECTS_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/oj/JOL_2022_333_R_0002">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32022L2555</j.0:resource_legal_id_celex>
<j.0:date_transposition rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-10-18</j.0:date_transposition>
<j.0:date_transposition rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-10-17</j.0:date_transposition>
</rdf:Description>
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32022L2555">
<j.0:date_transposition rdf:datatype="http://www.w3.org/2001/XMLSchema#date">1999-01-01</j.0:date_transposition>
</rdf:Description>
</rdf:RDF>
"""

# Models fact #2's shape (PLAN_REVISED.md §0/§2.4): the CELEX's own subject
# asserts `resource_legal_id_celex` but no entry-into-force date; a distinct,
# unrelated-looking subject (no `resource_legal_id_celex` of its own) asserts
# the target predicate -- the Tier-2 widen case. (Live data since Slice 0
# suggests `32025R0038` may actually resolve via Tier 1 -- see
# PLAN_REVISED.md's "Changes ... post-Slice-0" note -- but the algorithm is
# documented as tier-agnostic, so this fixture is kept as a deliberate,
# synthetic Tier-2 exerciser regardless of which tier real data hits.)
_RDF_WIDEN_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32025R0038">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32025R0038</j.0:resource_legal_id_celex>
</rdf:Description>
<rdf:Description rdf:about="http://publications.europa.eu/resource/oj/L_202500038">
<j.0:date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2025-02-04</j.0:date_entry-into-force>
</rdf:Description>
</rdf:RDF>
"""

# Same as _RDF_WIDEN_FIXTURE, plus a 3rd subject that asserts an unrelated
# CELEX's own id (`32019R0881`) AND a same-named target predicate -- the
# direct regression fixture for BLOCKING #1's contamination risk (PLAN_REVISED.md
# §2.3/§2.4). That 3rd subject's value must never be picked up.
_RDF_WIDEN_WITH_CONTAMINATION_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32025R0038">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32025R0038</j.0:resource_legal_id_celex>
</rdf:Description>
<rdf:Description rdf:about="http://publications.europa.eu/resource/oj/L_202500038">
<j.0:date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2025-02-04</j.0:date_entry-into-force>
</rdf:Description>
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32019R0881">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32019R0881</j.0:resource_legal_id_celex>
<j.0:date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2019-06-27</j.0:date_entry-into-force>
</rdf:Description>
</rdf:RDF>
"""

# No subject asserts the target predicate on an admissible (own or
# non-excluded) subject -- the only subject that has the predicate at all
# also asserts a different CELEX's own id, so it is excluded from widening.
_RDF_NO_MATCH_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32025R0038">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32025R0038</j.0:resource_legal_id_celex>
</rdf:Description>
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32019R0881">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32019R0881</j.0:resource_legal_id_celex>
<j.0:date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2019-06-27</j.0:date_entry-into-force>
</rdf:Description>
</rdf:RDF>
"""

# Two widened subjects (neither the CELEX's own, neither excluded) both
# assert the target predicate -- PLAN_REVISED.md §2.3's "more than one
# surviving subject" pooling branch, a documented secondary rule distinct
# from Tier 1's same-subject tie-break.
_RDF_WIDEN_MULTIPLE_MATCHES_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32022L2555">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32022L2555</j.0:resource_legal_id_celex>
</rdf:Description>
<rdf:Description rdf:about="http://publications.europa.eu/resource/oj/JOL_2022_333_R_0002">
<j.0:date_transposition rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-10-17</j.0:date_transposition>
</rdf:Description>
<rdf:Description rdf:about="http://publications.europa.eu/resource/oj/JOL_2022_333_R_0003">
<j.0:date_transposition rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-11-01</j.0:date_transposition>
</rdf:Description>
</rdf:RDF>
"""

# Real, live-confirmed shape for 32025R0038 (PLAN_REVISED.md §10 item 9/10 and
# "Changes ... post-Slice-0"): the own subject asserts both
# `resource_legal_id_celex` and the date under the *non-generalized*
# `resource_legal_date_entry-into-force` name -- Cellar's `notice=non-inferred`
# fetch profile never emits the bare `date_entry-into-force` name for a
# regulation. This is the direct regression fixture for the §10 item 9 fix:
# without both names in the `regulation` predicate tuple, this fixture's date
# would never resolve.
_RDF_REGULATION_RESOURCE_LEGAL_PREDICATE_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/oj/L_202500038">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32025R0038</j.0:resource_legal_id_celex>
<j.0:resource_legal_date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2025-02-04</j.0:resource_legal_date_entry-into-force>
</rdf:Description>
</rdf:RDF>
"""

# Directive own subject asserting only the non-generalized
# `directive_date_transposition` name -- proves dispatch checks both names in
# the `directive` predicate tuple, not just the bare `date_transposition` one.
_RDF_DIRECTIVE_ALT_PREDICATE_NAME_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/oj/JOL_2022_333_R_0002">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32022L2555</j.0:resource_legal_id_celex>
<j.0:directive_date_transposition rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-10-17</j.0:directive_date_transposition>
</rdf:Description>
</rdf:RDF>
"""

# `extract_metadata`'s regulation-CELEX fixture (Slice 5): own subject
# asserts both `resource_legal_id_celex` and `date_entry-into-force` --
# used wherever a test needs `extract_metadata` to succeed for a regulation
# CELEX without exercising the Tier-2 widen path specifically.
_RDF_REGULATION_OWN_SUBJECT_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32024R2847">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32024R2847</j.0:resource_legal_id_celex>
<j.0:date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2027-12-11</j.0:date_entry-into-force>
</rdf:Description>
</rdf:RDF>
"""

# No subject at all -- a placeholder for tests where `extract_metadata`
# raises before ever reaching RDF parsing (an unsupported CELEX type code),
# or is deliberately used to prove no admissible subject means
# `CellarParseError` (Slice 5's replacement for the old heading-based
# unresolvable-date test).
_RDF_EMPTY_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
</rdf:RDF>
"""


# --- Post-Slice-10, Defect 1/2 fixtures (§11, §12, §13) --------------------

# CRA-shaped (PLAN_REVISED.md's "Defect 1 -- live evidence", CRA section):
# 4 `owl:Axiom` blocks, `rdf:nodeID`-keyed, all `owl:annotatedSource` =
# `oj/L_202402847`, all `owl:annotatedProperty` =
# `cdm#resource_legal_date_entry-into-force`. Values/tokens reproduce the
# live A0/A2/A3/A10 blocks verbatim.
_RDF_CRA_AXIOM_BLOCKS_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:owl="http://www.w3.org/2002/07/owl#"
    xmlns:j.2="http://publications.europa.eu/ontology/annotation#">
<rdf:Description rdf:nodeID="A0">
<j.2:type_of_date>{MA|u:MA}</j.2:type_of_date>
<j.2:comment_on_date>{MA/PART|u:MP} {V|u:V} {ART|u:A} 71.2</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2026-09-11</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/L_202402847"/>
<rdf:type rdf:resource="http://www.w3.org/2002/07/owl#Axiom"/>
</rdf:Description>
<rdf:Description rdf:nodeID="A2">
<j.2:type_of_date>{MA|u:MA}</j.2:type_of_date>
<j.2:comment_on_date>{V|u:V} {ART|u:A} 71.2</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2027-12-11</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/L_202402847"/>
<rdf:type rdf:resource="http://www.w3.org/2002/07/owl#Axiom"/>
</rdf:Description>
<rdf:Description rdf:nodeID="A3">
<j.2:type_of_date>{MA|u:MA}</j.2:type_of_date>
<j.2:comment_on_date>{MA/PART|u:MP} {V|u:V} {ART|u:A} 71.2</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2026-06-11</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/L_202402847"/>
<rdf:type rdf:resource="http://www.w3.org/2002/07/owl#Axiom"/>
</rdf:Description>
<rdf:Description rdf:nodeID="A10">
<j.2:type_of_date>{EV|u:EV}</j.2:type_of_date>
<j.2:comment_on_date>{DATPUB|u:V} +20 {V|u:V} {ART|u:A} 71.1</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-12-10</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/L_202402847"/>
<rdf:type rdf:resource="http://www.w3.org/2002/07/owl#Axiom"/>
</rdf:Description>
</rdf:RDF>
"""

# CRA's own subject block, reproducing the same 4 values as
# `_RDF_CRA_AXIOM_BLOCKS_FIXTURE` (same subject IRI, same predicate),
# plus the 4 `owl:Axiom` blocks together with it -- one full document, used
# by `_pick_effective_date`-level tests that need both `_parse_subject_blocks`
# and `_parse_reification_blocks` output from the same real-shaped document.
_RDF_CRA_REIFIED_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:owl="http://www.w3.org/2002/07/owl#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#"
    xmlns:j.2="http://publications.europa.eu/ontology/annotation#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/oj/L_202402847">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32024R2847</j.0:resource_legal_id_celex>
<j.0:resource_legal_date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2026-09-11</j.0:resource_legal_date_entry-into-force>
<j.0:resource_legal_date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2027-12-11</j.0:resource_legal_date_entry-into-force>
<j.0:resource_legal_date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2026-06-11</j.0:resource_legal_date_entry-into-force>
<j.0:resource_legal_date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-12-10</j.0:resource_legal_date_entry-into-force>
</rdf:Description>
<rdf:Description rdf:nodeID="A0">
<j.2:type_of_date>{MA|u:MA}</j.2:type_of_date>
<j.2:comment_on_date>{MA/PART|u:MP} {V|...} {ART|...} 71.2</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2026-09-11</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/L_202402847"/>
</rdf:Description>
<rdf:Description rdf:nodeID="A2">
<j.2:type_of_date>{MA|u:MA}</j.2:type_of_date>
<j.2:comment_on_date>{V|...} {ART|...} 71.2</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2027-12-11</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/L_202402847"/>
</rdf:Description>
<rdf:Description rdf:nodeID="A3">
<j.2:type_of_date>{MA|u:MA}</j.2:type_of_date>
<j.2:comment_on_date>{MA/PART|u:MP} {V|...} {ART|...} 71.2</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2026-06-11</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/L_202402847"/>
</rdf:Description>
<rdf:Description rdf:nodeID="A10">
<j.2:type_of_date>{EV|u:EV}</j.2:type_of_date>
<j.2:comment_on_date>{DATPUB|...} +20 {V|...} {ART|...} 71.1</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-12-10</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/L_202402847"/>
</rdf:Description>
</rdf:RDF>
"""

# GDPR's 2 `owl:Axiom` blocks (MA-without-MA/PART + EV) and NIS2's 2
# (ADOPTION/APPLICATION, no `type_of_date` at all -- a different vocabulary,
# `fd_361` not `fd_335`) together in one document, proving token extraction
# generalizes across both vocabularies (PLAN_REVISED.md §8 item 11).
_RDF_GDPR_NIS2_AXIOM_BLOCKS_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:owl="http://www.w3.org/2002/07/owl#"
    xmlns:j.2="http://publications.europa.eu/ontology/annotation#">
<rdf:Description rdf:nodeID="G_A3">
<j.2:type_of_date>{MA|u:MA}</j.2:type_of_date>
<j.2:comment_on_date>{V|u:V} {ART|u:A} 99</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2018-05-25</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/JOL_2016_119_R_0001"/>
</rdf:Description>
<rdf:Description rdf:nodeID="G_A4">
<j.2:type_of_date>{EV|u:EV}</j.2:type_of_date>
<j.2:comment_on_date>{DATPUB|u:V} +20 {V|u:V} {ART|u:A} 99</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2016-05-24</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/JOL_2016_119_R_0001"/>
</rdf:Description>
<rdf:Description rdf:nodeID="N_A0">
<j.2:comment_on_date>{ADOPTION|u:AD} {V|u:V} {ART|u:A} 41.1</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-10-17</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#directive_date_transposition"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/JOL_2022_333_R_0002"/>
</rdf:Description>
<rdf:Description rdf:nodeID="N_A8">
<j.2:comment_on_date>{APPLICATION|u:AP} {V|u:V} {ART|u:A} 41.1</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-10-18</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#directive_date_transposition"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/JOL_2022_333_R_0002"/>
</rdf:Description>
</rdf:RDF>
"""

# A plain `rdf:Description` with unrelated children, plus one with only
# `annotatedSource` present (missing `annotatedProperty`/`annotatedTarget`)
# -- neither is Axiom-shaped, must not produce a false-positive reification.
_RDF_NOT_AXIOM_SHAPED_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:owl="http://www.w3.org/2002/07/owl#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32024R2847">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32024R2847</j.0:resource_legal_id_celex>
<j.0:title>Some unrelated title text</j.0:title>
</rdf:Description>
<rdf:Description rdf:nodeID="PARTIAL">
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/L_202402847"/>
</rdf:Description>
</rdf:RDF>
"""

# 32019R0881-shaped (PLAN_REVISED.md's "Selection-rule validation against
# 32019R0881"): exactly EV + MA/PART, NO general MA at all -- the exact case
# a naive "any MA beats EV" rule would break. Domain-correct value is EV
# (2019-06-27), not the later MA/PART value (2021-06-28).
_RDF_32019R0881_REIFIED_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:owl="http://www.w3.org/2002/07/owl#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#"
    xmlns:j.2="http://publications.europa.eu/ontology/annotation#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/oj/JOL_2019_151_R_0002">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32019R0881</j.0:resource_legal_id_celex>
<j.0:resource_legal_date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2019-06-27</j.0:resource_legal_date_entry-into-force>
<j.0:resource_legal_date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2021-06-28</j.0:resource_legal_date_entry-into-force>
</rdf:Description>
<rdf:Description rdf:nodeID="A3">
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2019-06-27</owl:annotatedTarget>
<j.2:comment_on_date>{DATPUB|...} +20 {V|...} {ART|...} 69.1</j.2:comment_on_date>
<j.2:type_of_date>{EV|u:EV}</j.2:type_of_date>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/JOL_2019_151_R_0002"/>
</rdf:Description>
<rdf:Description rdf:nodeID="A4">
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2021-06-28</owl:annotatedTarget>
<j.2:comment_on_date>{MA/PART|u:MP} {V|...} {ART|...} 69.2</j.2:comment_on_date>
<j.2:type_of_date>{MA|u:MA}</j.2:type_of_date>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/JOL_2019_151_R_0002"/>
</rdf:Description>
</rdf:RDF>
"""

# NIS2-shaped directive fixture with reification, deliberately constructed
# with APPLICATION's subject-block value and Axiom block listed BEFORE
# ADOPTION's, to prove selection is by tag, not fixture/document order or
# "always the minimum".
_RDF_NIS2_REIFIED_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:owl="http://www.w3.org/2002/07/owl#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#"
    xmlns:j.2="http://publications.europa.eu/ontology/annotation#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/oj/JOL_2022_333_R_0002">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32022L2555</j.0:resource_legal_id_celex>
<j.0:directive_date_transposition rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-10-18</j.0:directive_date_transposition>
<j.0:directive_date_transposition rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-10-17</j.0:directive_date_transposition>
</rdf:Description>
<rdf:Description rdf:nodeID="N_A8">
<j.2:comment_on_date>{APPLICATION|u:AP} {V|...} {ART|...} 41.1</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-10-18</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#directive_date_transposition"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/JOL_2022_333_R_0002"/>
</rdf:Description>
<rdf:Description rdf:nodeID="N_A0">
<j.2:comment_on_date>{ADOPTION|u:AD} {V|...} {ART|...} 41.1</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-10-17</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#directive_date_transposition"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/JOL_2022_333_R_0002"/>
</rdf:Description>
</rdf:RDF>
"""

# GDPR's live, reified, cross-subject ("related resource") shape (Slice 14,
# post-Slice-10 Defect 1 evidence, GDPR section): the CELEX's own subject
# (`celex/32016R0679`) asserts only `resource_legal_id_celex` -- no date --
# so Tier 1 is empty and `_resolve_predicate_candidates` must widen to the
# related `oj/JOL_2016_119_R_0001` subject (§2.3 Tier 2), which carries both
# `resource_legal_date_entry-into-force` values, each reified (`MA`, no
# `MA/PART` -> `2018-05-25`; `EV` -> `2016-05-24`). This is the fixture
# `test_extract_metadata_resolves_regulation_entry_into_force_date_from_
# related_resource` now uses -- under the old blind-earliest-wins logic this
# would have resolved `2016-05-24` (EV, domain-WRONG); the role-priority fix
# must resolve the general `MA` value, `2018-05-25`.
_RDF_GDPR_REIFIED_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:owl="http://www.w3.org/2002/07/owl#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#"
    xmlns:j.2="http://publications.europa.eu/ontology/annotation#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32016R0679">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32016R0679</j.0:resource_legal_id_celex>
</rdf:Description>
<rdf:Description rdf:about="http://publications.europa.eu/resource/oj/JOL_2016_119_R_0001">
<j.0:resource_legal_date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2018-05-25</j.0:resource_legal_date_entry-into-force>
<j.0:resource_legal_date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2016-05-24</j.0:resource_legal_date_entry-into-force>
</rdf:Description>
<rdf:Description rdf:nodeID="G_A3">
<j.2:type_of_date>{MA|u:MA}</j.2:type_of_date>
<j.2:comment_on_date>{V|u:V} {ART|u:A} 99</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2018-05-25</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/JOL_2016_119_R_0001"/>
</rdf:Description>
<rdf:Description rdf:nodeID="G_A4">
<j.2:type_of_date>{EV|u:EV}</j.2:type_of_date>
<j.2:comment_on_date>{DATPUB|u:V} +20 {V|u:V} {ART|u:A} 99</j.2:comment_on_date>
<owl:annotatedTarget rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2016-05-24</owl:annotatedTarget>
<owl:annotatedProperty rdf:resource="http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"/>
<owl:annotatedSource rdf:resource="http://publications.europa.eu/resource/oj/JOL_2016_119_R_0001"/>
</rdf:Description>
</rdf:RDF>
"""

# CRA's live consolidated-expression shape (Defect 2's direct regression
# fixture): the only admissible subject asserts `resource_legal_id_celex`
# in UNREDUCED consolidated form (`02024R2847-20241120`), not the base-act
# form (`32024R2847`).
_RDF_CONSOLIDATED_CELEX_OWN_SUBJECT_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/02024R2847-20241120">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">02024R2847-20241120</j.0:resource_legal_id_celex>
<j.0:resource_legal_date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2024-11-20</j.0:resource_legal_date_entry-into-force>
</rdf:Description>
</rdf:RDF>
"""

# `_RDF_WIDEN_WITH_CONTAMINATION_FIXTURE`'s foreign subject, plus a 2nd
# foreign-looking subject asserting a garbage (non-CELEX-shaped)
# `resource_legal_id_celex` value alongside the target predicate -- must not
# crash `_base_celex_or_none`-based normalization and must not be picked up.
_RDF_WIDEN_WITH_UNPARSEABLE_FOREIGN_VALUE_FIXTURE = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32025R0038">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32025R0038</j.0:resource_legal_id_celex>
</rdf:Description>
<rdf:Description rdf:about="http://publications.europa.eu/resource/oj/GARBAGE_0001">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">not-a-celex</j.0:resource_legal_id_celex>
<j.0:date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">1999-01-01</j.0:date_entry-into-force>
</rdf:Description>
</rdf:RDF>
"""


def test_subject_blocks_group_predicate_values_by_subject_iri() -> None:
    blocks = _parse_subject_blocks(_rdf_root(_RDF_TWO_SUBJECTS_FIXTURE))

    assert blocks["http://publications.europa.eu/resource/oj/JOL_2022_333_R_0002"][
        "date_transposition"
    ] == ["2024-10-18", "2024-10-17"]
    assert blocks["http://publications.europa.eu/resource/celex/32022L2555"][
        "date_transposition"
    ] == ["1999-01-01"]


def test_own_subject_is_identified_via_resource_legal_id_celex() -> None:
    blocks = _parse_subject_blocks(_rdf_root(_RDF_TWO_SUBJECTS_FIXTURE))

    own = _own_subjects(blocks, "32022L2555")

    assert own == {"http://publications.europa.eu/resource/oj/JOL_2022_333_R_0002"}


def test_own_subject_tier_wins_when_it_has_matches() -> None:
    blocks = _parse_subject_blocks(_rdf_root(_RDF_TWO_SUBJECTS_FIXTURE))

    result = _resolve_predicate_candidates(blocks, "32022L2555", ("date_transposition",))

    subject_iri = "http://publications.europa.eu/resource/oj/JOL_2022_333_R_0002"
    assert result == [
        (subject_iri, "date_transposition", "2024-10-18"),
        (subject_iri, "date_transposition", "2024-10-17"),
    ]


def test_widens_to_related_subject_when_own_subject_has_no_matches() -> None:
    blocks = _parse_subject_blocks(_rdf_root(_RDF_WIDEN_FIXTURE))

    result = _resolve_predicate_candidates(blocks, "32025R0038", ("date_entry-into-force",))

    assert result == [
        (
            "http://publications.europa.eu/resource/oj/L_202500038",
            "date_entry-into-force",
            "2025-02-04",
        )
    ]


def test_widen_excludes_subject_asserting_a_different_resource_legal_id_celex() -> None:
    blocks = _parse_subject_blocks(_rdf_root(_RDF_WIDEN_WITH_CONTAMINATION_FIXTURE))

    result = _resolve_predicate_candidates(blocks, "32025R0038", ("date_entry-into-force",))
    values = [value for _, _, value in result]

    assert values == ["2025-02-04"]
    assert "2019-06-27" not in values


def test_returns_empty_when_no_admissible_subject_matches() -> None:
    blocks = _parse_subject_blocks(_rdf_root(_RDF_NO_MATCH_FIXTURE))

    result = _resolve_predicate_candidates(blocks, "32025R0038", ("date_entry-into-force",))

    assert result == []


def test_widen_pools_values_from_all_surviving_subjects_when_more_than_one_matches() -> None:
    """§2.3's documented secondary rule: not a silent reuse of Tier 1's scope.

    Not one of PLAN_REVISED.md §8 item 2's enumerated tests, but exercises a
    branch of `_resolve_predicate_candidates` (the "more than one surviving
    widened subject" pooling case) that item 2's list otherwise leaves
    uncovered — item 3's test of the same scenario (a later slice) exercises
    `_pick_effective_date` directly, not this function's own pooling
    behavior.
    """
    blocks = _parse_subject_blocks(_rdf_root(_RDF_WIDEN_MULTIPLE_MATCHES_FIXTURE))

    result = _resolve_predicate_candidates(blocks, "32022L2555", ("date_transposition",))
    values = [value for _, _, value in result]

    assert sorted(values) == ["2024-10-17", "2024-11-01"]


def test_pick_effective_date_returns_earliest_of_duplicate_values() -> None:
    """Untagged duplicate values (no reification) -- earliest-wins fallback."""
    subject_iri = "http://publications.europa.eu/resource/oj/JOL_2022_333_R_0002"
    candidates = [
        (subject_iri, "date_transposition", "2024-10-18"),
        (subject_iri, "date_transposition", "2024-10-17"),
    ]

    result = _pick_effective_date(candidates, {}, "directive")

    assert result == date(2024, 10, 17)


def test_pick_effective_date_returns_none_for_empty_candidates() -> None:
    assert _pick_effective_date([], {}, "regulation") is None


def test_pick_effective_date_returns_the_single_value_when_unambiguous() -> None:
    candidates = [
        (
            "http://publications.europa.eu/resource/oj/L_202500038",
            "date_entry-into-force",
            "2025-02-04",
        )
    ]

    assert _pick_effective_date(candidates, {}, "regulation") == date(2025, 2, 4)


def test_tie_break_applies_only_within_one_subjects_values_not_across_two_matching_subjects() -> (
    None
):
    """Two surviving Tier-2 subjects, each with one distinct, non-duplicate, untagged value.

    Proves PLAN_REVISED.md §2.3's pooled-then-tie-break secondary rule is a
    real application of the *same* `_pick_effective_date` tie-break (§3)
    applied to `_resolve_predicate_candidates`'s already-pooled output --
    not a silently different rule -- once two distinct, non-excluded
    subjects both assert the target predicate. Neither value is reified, so
    this exercises the earliest-wins fallback, not role-priority.
    """
    blocks = _parse_subject_blocks(_rdf_root(_RDF_WIDEN_MULTIPLE_MATCHES_FIXTURE))
    candidates = _resolve_predicate_candidates(blocks, "32022L2555", ("date_transposition",))

    resolved = _pick_effective_date(candidates, {}, "directive")

    assert resolved == date(2024, 10, 17)


def test_find_effective_date_uses_transposition_predicates_for_directive_instrument_type() -> None:
    blocks = _parse_subject_blocks(_rdf_root(_RDF_TWO_SUBJECTS_FIXTURE))

    result = _find_effective_date_from_rdf(blocks, {}, "32022L2555", "directive")

    assert result == date(2024, 10, 17)


def test_find_effective_date_uses_entry_into_force_predicate_for_regulation_instrument_type() -> (
    None
):
    """MUST use `resource_legal_date_entry-into-force`, not bare `date_entry-into-force`.

    Regression test for PLAN_REVISED.md §10 item 9: Cellar's adopted
    `notice=non-inferred` fetch profile never emits the bare
    `date_entry-into-force` name for a regulation -- only this
    non-generalized name, confirmed live for `32025R0038`.
    """
    blocks = _parse_subject_blocks(_rdf_root(_RDF_REGULATION_RESOURCE_LEGAL_PREDICATE_FIXTURE))

    result = _find_effective_date_from_rdf(blocks, {}, "32025R0038", "regulation")

    assert result == date(2025, 2, 4)


def test_find_effective_date_checks_both_directive_predicate_names() -> None:
    blocks = _parse_subject_blocks(_rdf_root(_RDF_DIRECTIVE_ALT_PREDICATE_NAME_FIXTURE))

    result = _find_effective_date_from_rdf(blocks, {}, "32022L2555", "directive")

    assert result == date(2024, 10, 17)


# --- Slice 14 (post-Slice-10): NIS2/CRA/GDPR extract_metadata-level fixtures
# now carry realistic `owl:Axiom` reification, so the tests below exercise the
# REAL role-priority path (`_pick_effective_date` grouping by `_date_role`),
# not the untagged earliest-wins fallback the original Slice 5 fixtures
# happened to pass under "by luck" (no reification data at all). NIS2 is
# switched from `_RDF_TWO_SUBJECTS_FIXTURE` (no reification) to
# `_RDF_NIS2_REIFIED_FIXTURE` (same own-subject/Tier-1 shape and same two
# duplicate values, now each tagged `ADOPTION`/`APPLICATION` per the real live
# `fd_361` vocabulary) -- the resolved value is unchanged (`2024-10-17`,
# selected by tag here, not by numeric minimum) but the fixture is no longer
# reification-blind.


def test_extract_metadata_returns_title_from_eli_main_title() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE, _RDF_NIS2_REIFIED_FIXTURE, "32022L2555")

    assert (
        metadata.title == "Directive (EU) 2022/2555 of the European Parliament and of the Council"
    )


def test_extract_metadata_sets_jurisdiction_status_and_source_type_constants() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE, _RDF_NIS2_REIFIED_FIXTURE, "32022L2555")

    assert metadata.jurisdiction == "EU"
    assert metadata.status == "active"
    assert metadata.source_type == "external"


def test_extract_metadata_version_equals_base_act_version_constant() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE, _RDF_NIS2_REIFIED_FIXTURE, "32022L2555")

    assert metadata.version == _BASE_ACT_VERSION


def test_extract_metadata_effective_date_is_a_real_date_object() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE, _RDF_NIS2_REIFIED_FIXTURE, "32022L2555")

    assert metadata.effective_date == date(2024, 10, 17)
    assert isinstance(metadata.effective_date, date)


def test_extract_metadata_resolves_directive_transposition_date_from_rdf_metadata() -> None:
    """Reproduces NIS2's real duplicate-value, reified shape (post-Slice-10:
    PLAN_REVISED.md's Defect 1 evidence, `fd_361` vocabulary).

    The RDF subject asserts `directive_date_transposition` twice
    (`2024-10-18`/`2024-10-17`), each wrapped in its own `owl:Axiom` block
    tagging it `APPLICATION`/`ADOPTION` respectively -- `extract_metadata`
    must resolve `2024-10-17` by role priority (`ADOPTION` beats
    `APPLICATION`), not merely because it is numerically the smaller value
    (the fixture's Axiom blocks are deliberately ordered `APPLICATION` before
    `ADOPTION` -- see `_RDF_NIS2_REIFIED_FIXTURE`'s own comment).
    """
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE, _RDF_NIS2_REIFIED_FIXTURE, "32022L2555")

    assert metadata.effective_date == date(2024, 10, 17)


def test_extract_metadata_resolves_regulation_entry_into_force_date_from_related_resource() -> None:
    """Reproduces GDPR's real, reified cross-subject shape (post-Slice-10,
    Defect 1's GDPR evidence -- switched from the unreified, single-value
    32025R0038-shaped `_RDF_WIDEN_FIXTURE` used pre-Slice-14).

    The CELEX's own subject (`celex/32016R0679`) asserts only
    `resource_legal_id_celex`, no date at all; the related subject
    (`oj/JOL_2016_119_R_0001`) -- discovered via `resource_legal_id_celex`,
    not the fetch URL -- carries BOTH `resource_legal_date_entry-into-force`
    values (`MA`=`2018-05-25`, `EV`=`2016-05-24`), each reified.
    `extract_metadata` must widen to find them (§2.3 Tier 2) AND resolve the
    `MA`-general value specifically (`2018-05-25`), not whichever happens to
    be earliest -- under the old blind-earliest-wins tie-break this would
    have resolved `2016-05-24` (EV), the exact GDPR regression
    IMPL_SLICE_10_LIVE.md confirmed live.
    """
    metadata = extract_metadata(_ENTRY_INTO_FORCE_FIXTURE, _RDF_GDPR_REIFIED_FIXTURE, "32016R0679")

    assert metadata.effective_date == date(2018, 5, 25)


def test_extract_metadata_raises_cellar_parse_error_when_no_matching_predicate_present() -> None:
    """Replaces the old heading-based unresolvable-date test.

    No subject in the RDF document asserts any of the regulation
    `effective_date` predicates -- `extract_metadata` must raise
    `CellarParseError` naming the identifier and the predicates searched,
    not the old "no Article heading matched" reason.
    """
    with pytest.raises(CellarParseError, match="32999R9999"):
        extract_metadata(_NO_DATE_HEADING_FIXTURE, _RDF_EMPTY_FIXTURE, "32999R9999")


def test_instrument_type_from_celex_maps_R_to_regulation() -> None:
    assert _instrument_type_from_celex("32016R0679") == "regulation"


def test_instrument_type_from_celex_maps_L_to_directive() -> None:
    assert _instrument_type_from_celex("32022L2555") == "directive"


def test_instrument_type_from_celex_raises_naming_unsupported_code() -> None:
    with pytest.raises(CellarParseError, match="'D'"):
        _instrument_type_from_celex("32014D0123")


def test_instrument_type_from_celex_raises_on_unparseable_identifier() -> None:
    with pytest.raises(CellarParseError):
        _instrument_type_from_celex("not-a-celex")


def test_extract_metadata_sets_instrument_type_regulation_for_R_celex() -> None:
    """CRA-shaped, reified fixture (post-Slice-10): the own subject asserts
    4 `resource_legal_date_entry-into-force` values (MA/PART, MA general, MA/
    PART, EV), each reified -- exercises the real multi-value regulation path,
    not the single-unambiguous-value shape `_RDF_REGULATION_OWN_SUBJECT_FIXTURE`
    (pre-Slice-10) carried.
    """
    metadata = extract_metadata(_ENTRY_INTO_FORCE_FIXTURE, _RDF_CRA_REIFIED_FIXTURE, "32024R2847")

    assert metadata.instrument_type == "regulation"


def test_extract_metadata_resolves_regulation_ma_general_date_not_earliest_ev_date() -> None:
    """Direct extract_metadata-level regression test for Defect 1 (CRA).

    `_RDF_CRA_REIFIED_FIXTURE` reproduces CRA's live 4-value shape exactly:
    `MA/PART=2026-09-11`, `MA(general)=2027-12-11`, `MA/PART=2026-06-11`,
    `EV=2024-12-10`. Under the old, reification-blind "earliest wins" logic
    this would have resolved `2024-12-10` (EV) -- the domain-WRONG value, and
    exactly IMPL_SLICE_10_LIVE.md's confirmed CRA regression. The
    role-priority fix must resolve the general `MA` value, `2027-12-11`,
    matching `ps-domain-concepts.md` line 530 and
    `test_pipeline_live.py::_GROUND_TRUTH["CRA"]`.
    """
    metadata = extract_metadata(_ENTRY_INTO_FORCE_FIXTURE, _RDF_CRA_REIFIED_FIXTURE, "32024R2847")

    assert metadata.effective_date == date(2027, 12, 11)


def test_extract_metadata_sets_instrument_type_directive_for_L_celex() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE, _RDF_NIS2_REIFIED_FIXTURE, "32022L2555")

    assert metadata.instrument_type == "directive"


def test_extract_metadata_sets_celex_from_identifier() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE, _RDF_NIS2_REIFIED_FIXTURE, "32022L2555")

    assert metadata.celex == "32022L2555"


def test_extract_metadata_raises_before_building_node_for_unsupported_celex_code() -> None:
    with pytest.raises(CellarParseError):
        extract_metadata(_ENTRY_INTO_FORCE_FIXTURE, _RDF_EMPTY_FIXTURE, "32014D0123")


# --- Follow-on A1: consolidated-expression CELEX acceptance ---------------


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("32024R2847", "32024R2847"),
        ("32022L2555", "32022L2555"),
        ("02024R2847-20241120", "32024R2847"),
        ("02016R0679-20160504", "32016R0679"),
        ("02013R0575-20240709", "32013R0575"),
    ],
)
def test_base_celex_normalises_consolidated_and_passes_base_act_through(
    identifier: str, expected: str
) -> None:
    assert _base_celex(identifier) == expected


@pytest.mark.parametrize("identifier", ["garbage", "02024R2847", "not-a-celex"])
def test_base_celex_raises_on_unrecognised_identifier_form(identifier: str) -> None:
    with pytest.raises(CellarParseError):
        _base_celex(identifier)


def test_instrument_type_from_celex_accepts_consolidated_expression_form() -> None:
    assert _instrument_type_from_celex("02016R0679-20160504") == "regulation"


def test_extract_metadata_accepts_consolidated_celex_and_stores_base_act_celex() -> None:
    metadata = extract_metadata(
        _ENTRY_INTO_FORCE_FIXTURE, _RDF_REGULATION_OWN_SUBJECT_FIXTURE, "02024R2847-20241120"
    )

    assert metadata.celex == "32024R2847"
    assert metadata.instrument_type == "regulation"
    assert metadata.effective_date == date(2027, 12, 11)


# --- Slice 11 (Defect 1, §2.1b): _parse_reification_blocks -----------------


def test_parse_reification_blocks_associates_type_of_date_and_comment_on_date_with_the_reified_triple() -> (  # noqa: E501 — exact test name per PLAN_REVISED.md §8 item 11
    None
):
    """CRA-shaped fixture, byte-for-byte modeled on the live A0/A2/A3/A10 blocks.

    4 values, one subject (`oj/L_202402847`), one predicate
    (`resource_legal_date_entry-into-force`). Asserts the returned
    `_Reifications` map has the right token sets keyed by the exact
    `(subject_iri, predicate_name, value_text)` triple -- in particular that
    `2027-12-11` carries `MA` but NOT `MA/PART` (the domain-correct value).
    """
    reifications = _parse_reification_blocks(_rdf_root(_RDF_CRA_AXIOM_BLOCKS_FIXTURE))

    subject_iri = "http://publications.europa.eu/resource/oj/L_202402847"
    predicate = "resource_legal_date_entry-into-force"
    assert reifications[(subject_iri, predicate, "2026-09-11")] == {"MA", "MA/PART", "V", "ART"}
    assert reifications[(subject_iri, predicate, "2027-12-11")] == {"MA", "V", "ART"}
    assert reifications[(subject_iri, predicate, "2026-06-11")] == {"MA", "MA/PART", "V", "ART"}
    assert reifications[(subject_iri, predicate, "2024-12-10")] == {"EV", "DATPUB", "V", "ART"}


def test_parse_reification_blocks_extracts_ma_part_and_adoption_application_tokens() -> None:
    """GDPR (`fd_335` EV/MA vocabulary) + NIS2 (`fd_361` ADOPTION/APPLICATION) together.

    Proves token extraction generalizes across both vocabularies, and that
    a reification with no `type_of_date` element at all (NIS2's shape) is
    still parsed correctly from `comment_on_date` alone.
    """
    reifications = _parse_reification_blocks(_rdf_root(_RDF_GDPR_NIS2_AXIOM_BLOCKS_FIXTURE))

    gdpr_subject = "http://publications.europa.eu/resource/oj/JOL_2016_119_R_0001"
    gdpr_predicate = "resource_legal_date_entry-into-force"
    assert reifications[(gdpr_subject, gdpr_predicate, "2018-05-25")] == {"MA", "V", "ART"}
    assert reifications[(gdpr_subject, gdpr_predicate, "2016-05-24")] == {
        "EV",
        "DATPUB",
        "V",
        "ART",
    }

    nis2_subject = "http://publications.europa.eu/resource/oj/JOL_2022_333_R_0002"
    nis2_predicate = "directive_date_transposition"
    assert reifications[(nis2_subject, nis2_predicate, "2024-10-17")] == {"ADOPTION", "V", "ART"}
    assert reifications[(nis2_subject, nis2_predicate, "2024-10-18")] == {
        "APPLICATION",
        "V",
        "ART",
    }


def test_parse_reification_blocks_ignores_elements_that_are_not_axiom_shaped() -> None:
    """A plain `rdf:Description` and a partial (missing property/target) block.

    Proves no false positives from `annotatedSource`/`annotatedProperty`/
    `annotatedTarget` being only partially present.
    """
    reifications = _parse_reification_blocks(_rdf_root(_RDF_NOT_AXIOM_SHAPED_FIXTURE))

    assert reifications == {}


def test_parse_reification_blocks_returns_empty_for_a_document_with_no_axiom_blocks() -> None:
    """Models `32025R0038`'s and both consolidated-expression documents' real shape.

    Live-confirmed zero `owl:Axiom` blocks for these cases.
    """
    assert _parse_reification_blocks(_rdf_root(_RDF_WIDEN_FIXTURE)) == {}
    assert _parse_reification_blocks(_rdf_root(_RDF_EMPTY_FIXTURE)) == {}


# --- Slice 12 (Defect 1, §3): _date_role + rewritten _pick_effective_date --


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (frozenset({"MA"}), "MA_GENERAL"),
        (frozenset({"MA", "MA/PART"}), "MA_PART"),
        (frozenset({"EV"}), "EV"),
        (frozenset[str](), None),
    ],
)
def test_date_role_classifies_regulation_ma_general_ev_and_ma_part(
    tokens: frozenset[str], expected: str | None
) -> None:
    assert _date_role("regulation", tokens) == expected


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (frozenset({"ADOPTION"}), "ADOPTION"),
        (frozenset({"APPLICATION"}), "APPLICATION"),
        (frozenset[str](), None),
    ],
)
def test_date_role_classifies_directive_adoption_and_application(
    tokens: frozenset[str], expected: str | None
) -> None:
    assert _date_role("directive", tokens) == expected


def test_pick_effective_date_selects_regulation_ma_general_over_ev_and_ma_part() -> None:
    """CRA-shaped fixture reproducing the exact live 4 values/roles.

    Must resolve to the MA-general value (`2027-12-11`), never `EV`
    (`2024-12-10`, the always-earliest value the old blind earliest-wins
    rule always picked) nor either `MA/PART` value.
    """
    root = _rdf_root(_RDF_CRA_REIFIED_FIXTURE)
    blocks = _parse_subject_blocks(root)
    reifications = _parse_reification_blocks(root)
    candidates = _resolve_predicate_candidates(
        blocks, "32024R2847", ("resource_legal_date_entry-into-force",)
    )

    result = _pick_effective_date(candidates, reifications, "regulation")

    assert result == date(2027, 12, 11)


def test_pick_effective_date_selects_ev_over_ma_part_when_no_ma_general_value_exists() -> None:
    """The regression guard for the corrected priority order (32019R0881-shaped).

    Exactly `EV` (2019-06-27) + `MA/PART` (2021-06-28), no general `MA` at
    all. Must select `EV`, NOT the later `MA/PART` value -- a naive "any MA
    beats EV" rule would pick 2021-06-28 here and silently break this
    already-passing case (see PLAN_REVISED.md's "Selection-rule validation
    against 32019R0881"). This is the single most important correctness
    check in this slice.
    """
    root = _rdf_root(_RDF_32019R0881_REIFIED_FIXTURE)
    blocks = _parse_subject_blocks(root)
    reifications = _parse_reification_blocks(root)
    candidates = _resolve_predicate_candidates(
        blocks, "32019R0881", ("resource_legal_date_entry-into-force",)
    )

    result = _pick_effective_date(candidates, reifications, "regulation")

    assert result == date(2019, 6, 27)


def test_pick_effective_date_selects_directive_adoption_over_application() -> None:
    """NIS2-shaped fixture, APPLICATION listed BEFORE ADOPTION in fixture order.

    Proves selection is by tag, not by document/fixture order and not by
    "always the minimum" (both happen to coincide with role-priority here,
    but the fixture order is deliberately reversed to prove it's not an
    accident of ordering).
    """
    root = _rdf_root(_RDF_NIS2_REIFIED_FIXTURE)
    blocks = _parse_subject_blocks(root)
    reifications = _parse_reification_blocks(root)
    candidates = _resolve_predicate_candidates(
        blocks, "32022L2555", ("directive_date_transposition",)
    )

    result = _pick_effective_date(candidates, reifications, "directive")

    assert result == date(2024, 10, 17)


def test_pick_effective_date_falls_back_to_earliest_when_no_candidate_has_a_recognized_role() -> (
    None
):
    """An untagged synthetic duplicate -- no reification for either value.

    Proves the original earliest-wins fallback still exists for this case.
    """
    subject_iri = "http://example.org/resource/oj/UNTAGGED_0001"
    candidates = [
        (subject_iri, "date_entry-into-force", "2025-06-01"),
        (subject_iri, "date_entry-into-force", "2025-01-01"),
    ]

    result = _pick_effective_date(candidates, {}, "regulation")

    assert result == date(2025, 1, 1)


# --- Slice 13 (Defect 2, §2.2/§2.3): _base_celex_or_none + normalized -------
# --- comparison --------------------------------------------------------


def test_base_celex_or_none_returns_none_instead_of_raising_for_an_unparseable_value() -> None:
    assert _base_celex_or_none("not-a-celex") is None
    # Still normalizes valid forms exactly like `_base_celex` does.
    assert _base_celex_or_none("02024R2847-20241120") == "32024R2847"
    assert _base_celex_or_none("32024R2847") == "32024R2847"


def test_own_subjects_matches_a_subject_asserting_the_unreduced_consolidated_expression_celex() -> (
    None
):
    """The direct regression test for Defect 2.

    A subject asserts `resource_legal_id_celex` in unreduced consolidated
    form (`02024R2847-20241120`); `own_id` is the normalized base-act form
    (`32024R2847`). Previously: excluded, misclassified as foreign. Now:
    recognized as own.
    """
    blocks = _parse_subject_blocks(_rdf_root(_RDF_CONSOLIDATED_CELEX_OWN_SUBJECT_FIXTURE))

    own = _own_subjects(blocks, "32024R2847")

    assert own == {"http://publications.europa.eu/resource/celex/02024R2847-20241120"}


def test_widen_still_excludes_a_foreign_subject_after_normalizing_both_sides() -> None:
    """Re-run/extension of the existing BLOCKING-#1 contamination test.

    Proves the normalize-both-sides change (Defect 2's fix) doesn't weaken
    the existing "unrelated cited/amending act" exclusion guard.
    """
    blocks = _parse_subject_blocks(_rdf_root(_RDF_WIDEN_WITH_CONTAMINATION_FIXTURE))

    result = _resolve_predicate_candidates(blocks, "32025R0038", ("date_entry-into-force",))
    values = [value for _, _, value in result]

    assert values == ["2025-02-04"]
    assert "2019-06-27" not in values


def test_widen_excludes_a_subject_asserting_an_unparseable_resource_legal_id_celex_value() -> None:
    """Defensive: a garbage (non-CELEX-shaped) foreign value must not crash resolution.

    Must not be treated as own, and must not raise an unhandled
    `CellarParseError` from inside the exclusion guard.
    """
    blocks = _parse_subject_blocks(_rdf_root(_RDF_WIDEN_WITH_UNPARSEABLE_FOREIGN_VALUE_FIXTURE))

    result = _resolve_predicate_candidates(blocks, "32025R0038", ("date_entry-into-force",))

    assert result == []
