"""Review kinds the `company_merge` tests share.

`UNREGISTERED_KIND` is a label with no spec: a stored review carrying it must be refused, naming it.
It is never the label of a real node kind, so a test never mistakes it for a kind the product knows.
"""

from __future__ import annotations

UNREGISTERED_KIND = "Unregistered"
