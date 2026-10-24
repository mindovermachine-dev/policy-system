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
    "governance": {
      "policy": {
        "id": "pol_patching",
        "title": "Patch Management Policy",
        "status": "approved"
      },
      "acknowledgment_required": true,
      "acknowledgment_text": "Splitting changes what policy 'Patch Management Policy' (pol_patching) covers: capability 'cap_a_vuln_remediation' stays governed by it, the split capability 'cap_x_vuln_patch' starts with no governing policy, and the 2 obligation(s) whose REQUIRES edge moves to 'cap_x_vuln_patch' are no longer covered by it. The policy is `approved`: its content and version do not change."
    },
    "obligations_moving": [
      "obl_patch_window",
      "obl_patch_testing"
    ],
    "skipped_missing_endpoints": [],
    "state_digest": "3f9a1c0e5b7d2a4f6e8c0b1d3a5f7e9c1b3d5f7a9c0e2b4d6f8a0c2e4b6d8f0a"
  },
  "acknowledgment_required": true,
  "message": "Splitting changes what policy 'Patch Management Policy' (pol_patching) covers: capability 'cap_a_vuln_remediation' stays governed by it, the split capability 'cap_x_vuln_patch' starts with no governing policy, and the 2 obligation(s) whose REQUIRES edge moves to 'cap_x_vuln_patch' are no longer covered by it. The policy is `approved`: its content and version do not change."
}
