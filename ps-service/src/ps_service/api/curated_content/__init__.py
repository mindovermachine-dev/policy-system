"""``ps_service.api.curated_content`` -- packaged copy of ``curated-content/catalog.json``.

CHANGES.md MA3's fix: the repo-root ``curated-content/`` tree never reaches the
``ps-service`` container image's build context (``Dockerfile``'s ``.dockerignore``
is an allow-list of exactly five paths, none of which is ``curated-content/``),
so a packaged, ``importlib.resources``-readable copy of ``catalog.json`` lives
here instead -- mirroring ``ps_service/mcp_interface/ps-domain-concepts.md``'s
own already-packaged-data-file precedent exactly.

``catalog.json`` in this directory is written by ``ps_service.export.
catalog_writer.write_catalog_json``'s ``packaged_copy_path`` parameter on every
curation run (the same computed JSON string as the repo-root, git-tracked
copy) -- never hand-edited independently of that run. This subpackage holds
no Python logic of its own; ``ps_service.api.catalog`` is the sole reader.
"""

from __future__ import annotations
