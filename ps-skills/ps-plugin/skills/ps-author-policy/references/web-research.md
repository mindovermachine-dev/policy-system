# Web research and citations (ps-author-policy)

Read on demand from `SKILL.md`'s Process immediately before drafting S-001's `procedure`, S-004's `verification_notes` or C-002's `execution_method`. Every Guardrail in `SKILL.md` still applies.

Applies immediately before drafting three specific fields -- S-001's
`procedure`, C-002's `execution_method`, and S-004's `verification_notes`
-- each genuinely "how" content, unlike every other criterion's field.

1. **Offer, never require.** Before drafting the field, offer the user a
   web-research lookup using the host's own web-search/browsing
   capability -- **never a PS Service tool call** (no MCP tool call is
   made for the offer itself). If the user declines, or the offer isn't
   taken up, proceed on the user's own drafting as normal -- identical to
   the plain field-by-field flow above, with nothing appended to the
   value.
2. **If research is used, cite it -- honestly.** When the user accepts
   and web research is actually incorporated into the drafted value,
   append a literal `Sources: <url>[, <url>...]` line to the end of that
   field's value, separated from the drafted text by a blank line, before
   calling the same `update-standard-draft`/`update-control-draft` used
   in the field-by-field loop above. List **only the URL(s) actually
   retrieved by that lookup** -- never fabricate a URL, and never
   construct or guess one from memory or plausibility. If research was
   declined or not used, the `Sources:` line is never appended -- the
   suffix is only ever present when it names real, retrieved sources.
3. Neither `update-standard-draft` nor `update-control-draft` requires or
   rejects a `Sources:` suffix -- the tool round-trips the field's value
   either way. The discipline above (append only when research was used,
   only real retrieved URLs, never fabricated, never required) is this
   skill's own, not the tool's.
