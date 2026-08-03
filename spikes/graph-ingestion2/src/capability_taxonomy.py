#!/usr/bin/env python3
"""Capability Taxonomy for Inferred Capabilities.

This module provides a pre-defined list of capabilities that can be inferred from obligations.
The taxonomy enables consistent capability classification across regulations.
"""

# Capability definitions with matching keywords and metadata
CAPABILITY_TAXONOMY = [
    {
        'name': 'Data Encryption',
        'keywords': ['encryption', 'encrypt', 'cipher', 'aes', 'rsa', 'encode', 'cryptography'],
        'type': 'technical',
        'description': 'Capacity to protect data through cryptographic methods, both at rest and in transit'
    },
    {
        'name': 'Access Control',
        'keywords': ['access control', 'authentication', 'authorization', 'rbac', 'permission', 'principle of least privilege'],
        'type': 'technical',
        'description': 'Capacity to manage who can access what resources based on identity and permissions'
    },
    {
        'name': 'Security Logging',
        'keywords': ['logging', 'log', 'audit trail', 'event log', 'security event', 'monitor', 'detection'],
        'type': 'technical',
        'description': 'Capacity to record security-relevant events for monitoring and forensic analysis'
    },
    {
        'name': 'Risk Assessment',
        'keywords': ['risk assessment', 'risk analysis', 'vulnerability', 'threat modeling', 'impact assessment'],
        'type': 'organizational',
        'description': 'Capacity to systematically identify, analyze, and evaluate security risks'
    },
    {
        'name': 'Incident Response',
        'keywords': ['incident response', 'security incident', 'breach notification', 'contingency plan', 'emergency'],
        'type': 'organizational',
        'description': 'Capacity to detect, respond to, and recover from security incidents'
    },
    {
        'name': 'Secure Configuration',
        'keywords': ['secure configuration', 'hardening', 'baseline configuration', 'configuration management', 'default settings'],
        'type': 'technical',
        'description': 'Capacity to establish and maintain secure system configurations'
    },
    {
        'name': 'Vulnerability Management',
        'keywords': ['vulnerability', 'patching', 'update', 'software update', 'security patch', 'fix'],
        'type': 'technical',
        'description': 'Capacity to identify, prioritize, and remediate security vulnerabilities'
    },
    {
        'name': 'Backup and Recovery',
        'keywords': ['backup', 'recovery', 'restore', 'disaster recovery', 'business continuity'],
        'type': 'technical',
        'description': 'Capacity to create copies of data and restore systems after disruption'
    },
    {
        'name': 'Personnel Security',
        'keywords': ['personnel security', 'background check', 'training', 'awareness', 'competence'],
        'type': 'organizational',
        'description': 'Capacity to ensure personnel are trustworthy, trained, and competent for their roles'
    },
    {
        'name': 'Supply Chain Security',
        'keywords': ['supply chain', 'third party', 'vendor', 'supplier', 'outsourcing'],
        'type': 'organizational',
        'description': 'Capacity to manage security risks from external suppliers and partners'
    },
    {
        'name': 'Cryptographic Key Management',
        'keywords': ['key management', 'key rotation', 'key lifecycle', 'crypto key'],
        'type': 'technical',
        'description': 'Capacity to securely generate, store, distribute, and rotate cryptographic keys'
    },
    {
        'name': 'Network Security',
        'keywords': ['network security', 'firewall', 'intrusion detection', 'ids', 'network segmentation'],
        'type': 'technical',
        'description': 'Capacity to protect network infrastructure from unauthorized access and attacks'
    },
    {
        'name': 'Physical Security',
        'keywords': ['physical security', 'access control', 'surveillance', 'cctv', 'facility'],
        'type': 'organizational',
        'description': 'Capacity to protect physical assets and infrastructure from unauthorized access'
    },
    {
        'name': 'Data Minimization',
        'keywords': ['data minimization', 'purpose limitation', 'data collection', 'delete data', 'retention'],
        'type': 'technical',
        'description': 'Capacity to collect and retain only the minimum necessary data for intended purpose'
    },
    {
        'name': 'Privacy Protection',
        'keywords': ['privacy', 'personal data', 'anonymization', 'pseudonymization', 'data subjects'],
        'type': 'technical',
        'description': 'Capacity to protect personal data and respect data subject rights'
    }
]


def find_matching_capabilities(obligation_text: str) -> list[dict]:
    """Find capabilities that match the obligation text.
    
    Uses keyword matching against capability taxonomy to infer capabilities from obligations.
    Each capability receives a score based on keyword matches.
    
    Args:
        obligation_text: Text of the obligation to analyze
        
    Returns:
        List of matching capabilities with scores, sorted by relevance
    """
    import re
    
    text_lower = obligation_text.lower()
    
    scored_capabilities = []
    
    for capability in CAPABILITY_TAXONOMY:
        score = 0
        matched_keywords = []
        
        for keyword in capability['keywords']:
            # Simple word boundary matching
            pattern = r'\b' + re.escape(keyword) + r'\b'
            if re.search(pattern, text_lower):
                score += 1
                matched_keywords.append(keyword)
        
        if score > 0:
            scored_capabilities.append({
                'name': capability['name'],
                'description': capability['description'],
                'type': capability['type'],
                'score': score,
                'matched_keywords': matched_keywords
            })
    
    # Sort by score (descending) then by name for consistency
    scored_capabilities.sort(key=lambda x: (-x['score'], x['name']))
    
    return scored_capabilities


def get_capability_by_name(capability_name: str):
    """Retrieve a capability definition by its name.
    
    Args:
        capability_name: Exact capability name
        
    Returns:
        Capability dict if found, None otherwise
    """
    for cap in CAPABILITY_TAXONOMY:
        if cap['name'].lower() == capability_name.lower():
            return cap.copy()
    return None


def get_all_capabilities() -> list[dict]:
    """Get all capabilities from the taxonomy.
    
    Returns:
        Copy of the full capability taxonomy
    """
    return [cap.copy() for cap in CAPABILITY_TAXONOMY]


# Example usage and testing
if __name__ == '__main__':
    print("Testing Capability Taxonomy...")
    
    # Test obligations
    test_obligations = [
        "The manufacturer shall implement data encryption to protect customer information.",
        "Operators must establish access control systems to restrict unauthorized access.",
        "All security events shall be logged and retained for a minimum of 12 months.",
        "Manufacturers shall conduct regular risk assessments to identify vulnerabilities.",
    ]
    
    for obligation in test_obligations:
        print(f"\nObligation: {obligation[:60]}...")
        matches = find_matching_capabilities(obligation)
        
        if matches:
            print("匹配的能力:")
            for match in matches[:3]:  # Show top 3
                print(f"  - {match['name']} (score: {match['score']}, keywords: {match['matched_keywords']})")
        else:
            print("  No matching capabilities found")
