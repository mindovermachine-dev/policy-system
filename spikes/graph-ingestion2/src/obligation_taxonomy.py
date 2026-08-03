#!/usr/bin/env python3
"""Obligation Taxonomy for Regulatory Obligations.

This module provides a pre-defined list of obligation types that can be matched
across different regulations. The taxonomy enables consistent obligation
classification and canonical ID generation regardless of which regulation
contains the obligation.
"""

import hashlib
import re
from typing import List, Dict, Optional

# Obligation definitions with matching keywords and metadata
OBLIGATION_TAXONOMY = [
    {
        'name': 'Risk Management',
        'keywords': ['risk assessment', 'risk analysis', 'risk evaluation', 'risk management'],
        'category': 'organizational',
        'description': 'Systematic process of identifying, analyzing, and evaluating risks'
    },
    {
        'name': 'Security Controls',
        'keywords': ['security measure', 'technical security', 'security control', 'protective measure'],
        'category': 'technical',
        'description': 'Measures implemented to protect systems and data from security threats'
    },
    {
        'name': 'Access Control',
        'keywords': ['access control', 'authentication', 'authorization', 'user access', 'permission management'],
        'category': 'technical',
        'description': 'Mechanisms to ensure only authorized individuals can access resources'
    },
    {
        'name': 'Data Protection',
        'keywords': ['data protection', 'data security', 'confidentiality', 'data safeguard'],
        'category': 'technical',
        'description': 'Measures to protect personal and sensitive data from unauthorized access or disclosure'
    },
    {
        'name': 'Encryption',
        'keywords': ['encryption', 'encrypt', 'cryptography', 'cipher', 'secure transmission'],
        'category': 'technical',
        'description': 'Process of converting information into secure format to prevent unauthorized access'
    },
    {
        'name': 'Incident Response',
        'keywords': ['incident response', 'security incident', 'breach response', 'emergency procedure'],
        'category': 'organizational',
        'description': 'Processes and procedures for detecting, responding to, and recovering from security incidents'
    },
    {
        'name': 'Logging and Monitoring',
        'keywords': ['logging', 'monitoring', 'audit trail', 'security event logging', 'detection system'],
        'category': 'technical',
        'description': 'Recording and analysis of security-relevant events for monitoring and forensics'
    },
    {
        'name': 'Training and Awareness',
        'keywords': ['training', 'awareness program', 'employee education', 'security awareness'],
        'category': 'organizational',
        'description': 'PROGRAMS to ensure personnel understand their security responsibilities'
    },
    {
        'name': 'Vulnerability Management',
        'keywords': ['vulnerability', 'patch management', 'software update', 'defect remediation'],
        'category': 'technical',
        'description': 'Process of identifying, classifying, and remedying vulnerabilities'
    },
    {
        'name': 'Backup and Recovery',
        'keywords': ['backup', 'recovery', 'disaster recovery', 'data restoration', 'business continuity'],
        'category': 'technical',
        'description': 'Processes for creating copies of data and restoring systems after disruption'
    },
    {
        'name': 'Physical Security',
        'keywords': ['physical security', 'facility security', 'access control', 'surveillance'],
        'category': 'organizational',
        'description': 'Measures to protect physical assets and infrastructure from unauthorized access'
    },
    {
        'name': 'Supplier Management',
        'keywords': ['supplier management', 'third party', 'vendor assessment', 'supply chain security'],
        'category': 'organizational',
        'description': 'Processes for managing security risks associated with external suppliers'
    },
    {
        'name': 'Policy Development',
        'keywords': ['policy development', 'policy creation', 'procedural documentation'],
        'category': 'organizational',
        'description': 'Creation and maintenance of organizational policies and procedures'
    },
    {
        'name': 'Audit and Compliance',
        'keywords': ['audit', 'compliance check', 'regulatory compliance', 'verification process'],
        'category': 'organizational',
        'description': 'Processes for verifying adherence to policies, standards, and regulations'
    },
    {
        'name': 'Configuration Management',
        'keywords': ['configuration management', 'secure configuration', 'system hardening', 'baseline control'],
        'category': 'technical',
        'description': 'Maintaining secure system configurations through standardized setups'
    }
]


def generate_canonical_obligation_id(obligation_text: str) -> str:
    """Generate a canonical obligation ID based on the obligation text content.
    
    This enables cross-regulation identification of similar obligations without
    regulation prefixes (e.g., no more 'CRA_obl_123_xyz' - instead we get
    'obl_risk_management_abc123').
    
    Phase 8 fix: Use SHA-256 with longer digest over full normalized text to prevent collisions.
    
    Args:
        obligation_text: The full text of the obligation
        
    Returns:
        Canonical obligation ID string
    """
    import re as rr
    
    # Normalize text: lowercase, collapse whitespace, strip punctuation for hashing
    normalized_text = re.sub(r'\s+', ' ', obligation_text.strip().lower())
    
    # Phase 8 fix: Use SHA-256 with longer digest (12 hex chars = 48 bits) to prevent collisions
    text_hash = hashlib.sha256(normalized_text.encode()).hexdigest()[:12]
    
    # Find matching obligation type from taxonomy
    matched_type = _find_matching_obligation_type(obligation_text)
    
    if matched_type:
        # Use the matched obligation type in the ID
        slug = rr.sub(r'[^a-z0-9]', '_', matched_type.lower())[:24]
        return f"obl_{slug}_{text_hash}"
    else:
        # Fallback: use general category and text hash
        return f"obl_generic_{text_hash}"


def generate_canonical_capability_id(capability_name: str, related_obligation_text: str) -> str:
    """Generate a canonical capability ID based on capability name and obligation context.
    
    This enables cross-regulation identification of similar capabilities.
    
    Phase 8 fix: Use SHA-256 with longer digest over normalized text to prevent collisions.
    
    Args:
        capability_name: Name of the capability
        related_obligation_text: Text of the related obligation (for additional uniqueness)
        
    Returns:
        Canonical capability ID string
    """
    import re as rr
    
    # Normalize text for more robust comparison
    normalized_cap_name = re.sub(r'\s+', ' ', capability_name.strip().lower())
    normalized_obl_text = re.sub(r'\s+', ' ', related_obligation_text[:200].strip().lower())
    
    # Phase 8 fix: Use SHA-256 with longer digest (12 hex chars = 48 bits)
    combined_hash_input = f"{normalized_cap_name}:{normalized_obl_text}"
    text_hash = hashlib.sha256(combined_hash_input.encode()).hexdigest()[:12]
    
    slug = re.sub(r'[^a-z0-9]', '_', normalized_cap_name)[:24]
    return f"cap_{slug}_{text_hash}"


def _find_matching_obligation_type(obligation_text: str) -> Optional[str]:
    """Find the best matching obligation type from taxonomy.
    
    Uses keyword matching against obligation taxonomy to find the most specific
    obligation category for a given obligation text.
    
    Args:
        obligation_text: Text of the obligation to classify
        
    Returns:
        Name of matched obligation type, or None if no match found
    """
    import re as rr
    
    text_lower = obligation_text.lower()
    
    best_match = None
    best_score = 0
    
    for obligation_type in OBLIGATION_TAXONOMY:
        score = 0
        matched_keywords = []
        
        for keyword in obligation_type['keywords']:
            # Simple word boundary matching
            pattern = r'\b' + rr.escape(keyword) + r'\b'
            if rr.search(pattern, text_lower):
                score += 1
                matched_keywords.append(keyword)
        
        if score > best_score:
            best_score = score
            best_match = obligation_type['name']
    
    # Only return a match if we found at least one keyword
    if best_score > 0:
        return best_match
    
    return None


def get_obligation_by_name(obligation_name: str):
    """Retrieve an obligation type definition by its name.
    
    Args:
        obligation_name: Exact obligation type name
        
    Returns:
        Obligation dict if found, None otherwise
    """
    for obligation_type in OBLIGATION_TAXONOMY:
        if obligation_type['name'].lower() == obligation_name.lower():
            return obligation_type.copy()
    return None


def get_all_obligation_types() -> list[dict]:
    """Get all obligation types from the taxonomy.
    
    Returns:
        Copy of the full obligation taxonomy
    """
    return [obligation_type.copy() for obligation_type in OBLIGATION_TAXONOMY]


# Example usage and testing
if __name__ == '__main__':
    print("Testing Obligation Taxonomy...")
    
    # Test obligations from various regulations
    test_obligations = [
        "The manufacturer shall implement risk assessment procedures to identify security vulnerabilities.",
        "Operators must establish access control systems to restrict unauthorized access to personal data.",
        "All security events shall be logged and retained for a minimum of 12 months.",
        "The manufacturer shall conduct regular vulnerability assessments and apply necessary patches.",
        "An organization shall maintain backup procedures to ensure data recovery after disruption.",
    ]
    
    for obligation in test_obligations:
        print(f"\nObligation: {obligation[:70]}...")
        
        # Generate canonical ID
        from obligation_taxonomy import generate_canonical_obligation_id
        canonical_id = generate_canonical_obligation_id(obligation)
        print(f"  Canonical ID: {canonical_id}")
        
        # Find matching type
        matched_type = _find_matching_obligation_type(obligation)
        if matched_type:
            print(f"  Matched type: {matched_type}")
        else:
            print(f"  No specific match found")
