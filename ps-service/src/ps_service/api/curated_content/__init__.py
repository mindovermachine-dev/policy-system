"""Packaged copy of the curated catalog, ``catalog.json``, readable through ``importlib.resources``.

The repository-root ``curated-content/`` tree never reaches the ``ps-service`` container image's
build context, so a packaged copy lives here and is found the same way from a wheel, an image or
a checkout. A curation run writes this file with the same JSON as the repository-root copy; it
is never edited by hand independently of that run. This subpackage holds no Python logic;
``ps_service.api.catalog`` is its only reader.
"""

from __future__ import annotations
