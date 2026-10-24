---
type: fixed
expect:
  x_id: cap_x_vuln_patch
---

{
  "preview": {
    "x_id": "cap_x_vuln_patch",
    "x_name": "Patch known vulnerabilities",
    "a_id": "cap_a_vuln_remediation",
    "a_name": "Vulnerability remediation",
    "marker_resolves_to": "cap_a_vuln_remediation",
    "source_baseline": "dora_baseline",
    "source_instruments": [
      "32022R2554"
    ],
    "embedding_available": true,
    "edges": {
      "requires": {
        "moves": [
          "obl_patch_window",
          "obl_patch_testing"
        ],
        "shared": [
          "obl_patch_inventory"
        ],
        "added": [],
        "stays": [
          "obl_remediate_findings"
        ]
      },
      "covers": {
        "moves": [],
        "shared": [],
        "added": [],
        "stays": [
          "std_vuln_scanning"
        ]
      },
      "mitigated_by": {
        "moves": [
          "risk_unpatched_systems"
        ],
        "shared": [],
        "added": [],
        "stays": []
      }
    },
    "governance": null,
    "obligations_moving": [
      "obl_patch_window",
      "obl_patch_testing"
    ],
    "skipped_missing_endpoints": [],
    "state_digest": "3f9a1c0e5b7d2a4f6e8c0b1d3a5f7e9c1b3d5f7a9c0e2b4d6f8a0c2e4b6d8f0a"
  },
  "pending_approval_id": "ap_1",
  "approval_url": "https://ps.example/approvals/ap_1#code",
  "expires_at": "2026-10-24T18:00:00+00:00"
}
