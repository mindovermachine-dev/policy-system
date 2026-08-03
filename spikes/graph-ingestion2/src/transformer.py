#!/usr/bin/env python3
"""Transformer Component - Generate Policy Chain.

Transforms extracted concepts (capabilities) into organizational policy chain:
  Capability → Policy → Standard → Control

This creates the complete compliance chain: regulatory obligations are addressed
through technical capabilities, which are governed by organizational policies,
implemented via standards, verified through controls.
"""

import datetime


def generate_canonical_policy_id(capability_name: str, capability_type: str) -> str:
    """Generate a canonical policy ID based on capability name and type.
    
    This enables cross-regulation identification of similar policies.
    No timestamp or regulation prefix - purely content-based.
    
    Args:
        capability_name: Name of the capability
        capability_type: Type ('technical' or 'organizational')
        
    Returns:
        Canonical policy ID string (e.g., "pol_data_encryption_technical")
    """
    import re
    import hashlib
    
    # Create a slug from capability name
    cap_slug = re.sub(r'[^a-z0-9]', '_', capability_name.lower())[:32]
    type_slug = re.sub(r'[^a-z0-9]', '_', capability_type.lower())[:16]
    
    return f"pol_{cap_slug}_{type_slug}"


def generate_canonical_standard_id(policy_id: str) -> str:
    """Generate a canonical standard ID based on policy ID.
    
    Args:
        policy_id: The parent policy ID
        
    Returns:
        Canonical standard ID string (e.g., "std_data_encryption_technical_v1")
    """
    # Extract the capability name part from policy_id (after 'pol_')
    parts = policy_id.replace('pol_', '').split('_')
    # Remove type suffix if present
    if len(parts) >= 2 and parts[-1] in ['technical', 'organizational']:
        cap_name = '_'.join(parts[:-1])
    else:
        cap_name = '_'.join(parts)
    
    return f"std_{cap_name}_v1"


def generate_canonical_control_id(standard_id: str, control_type: str) -> str:
    """Generate a canonical control ID based on standard and type.
    
    Args:
        standard_id: The parent standard ID
        control_type: Type ('automated' or 'manual')
        
    Returns:
        Canonical control ID string (e.g., "ctrl_data_encryption_auto")
    """
    # Extract capability name from standard_id
    parts = standard_id.replace('std_', '').split('_v')[0]
    ctrl_type_abbr = 'auto' if control_type == 'automated' else 'manual'
    
    return f"ctrl_{parts}_{ctrl_type_abbr}"


def transform_capabilities_to_policy_chain(capabilities: list, regulation_short: str) -> dict:
    """Transform extracted capabilities into complete policy chain.
    
    For each capability, generate:
    - Policy (governs the capability)
    - Standard (implements the policy)
    - Control (validates the standard)
    
    Carries explicit parent references for exact-lookup edge creation.
    
    Args:
        capabilities: List of capabilities from extraction
        regulation_short: Short code for regulation (e.g., 'CRA', 'NIS2')
        
    Returns:
        {
            'policies': [...],  # With 'capability_id' field
            'standards': [...], # With 'policy_id' field  
            'controls': [...]   # With 'standard_id' field
        }
    """
    policies = []
    standards = []
    controls = []
    
    for capability in capabilities:
        cap_name = capability.get('name', 'Unknown Capability')
        cap_type = capability.get('type', 'technical')
        cap_id = capability.get('id', '')  # Use canonical ID from extractor
        
        # ========== POLICY ==========
        policy_id = generate_canonical_policy_id(cap_name, cap_type)
        policy = {
            'id': policy_id,
            'title': f"{cap_name} Policy",
            'description': f"Organization commits to maintaining {cap_name.lower()} capability "
                          f"to address regulatory obligations under {regulation_short}.",
            'owner_id': 'security-team',  # Default, can be overridden
            'status': 'approved',
            'version': '1.0',
            'capability_id': cap_id  # EXPLICIT PARENT REFERENCE (AC-5)
        }
        policies.append(policy)
        
        # ========== STANDARD ==========
        standard_id = generate_canonical_standard_id(policy_id)
        standard = {
            'id': standard_id,
            'title': f"{cap_name} Implementation Standard",
            'description': _generate_standard_description(cap_name, regulation_short),
            'implementation_status': 'implemented',  # Auto-generated = implemented for spike
            'version': '1.0',
            'policy_id': policy_id  # EXPLICIT PARENT REFERENCE (AC-5)
        }
        standards.append(standard)
        
        # ========== CONTROL ==========
        control_type = 'automated' if capability.get('type') == 'technical' else 'manual'
        control_id = generate_canonical_control_id(standard_id, control_type)
        
        control = {
            'id': control_id,
            'type': control_type,
            'title': f"Verify {cap_name} Implementation",
            'description': _generate_control_description(cap_name, control_type),
            'implementation_status': 'implemented',
            'execution_frequency': 'daily' if control_type == 'automated' else 'quarterly',
            'standard_id': standard_id  # EXPLICIT PARENT REFERENCE (AC-5)
        }
        controls.append(control)
    
    return {
        'policies': policies,
        'standards': standards,
        'controls': controls
    }


def _generate_standard_description(capability_name: str, regulation_short: str) -> str:
    """Generate standard description based on capability."""
    descriptions = {
        'Data Encryption': """# Data Encryption Implementation Standard

## Objective
Ensure all data is encrypted at rest and in transit using approved cryptographic standards.

## Requirements
1. Use AES-256 encryption for data at rest
2. Implement TLS 1.3+ for data in transit  
3. Maintain cryptographic key management procedures

## References
{regulation_short} - Article on security measures""".format(regulation_short=regulation_short),
        
        'Access Control': """# Access Control Implementation Standard

## Objective
Ensure only authorized personnel can access systems and data.

## Requirements
1. Implement role-based access control (RBAC)
2. Enforce principle of least privilege
3. Require multi-factor authentication for sensitive systems
4. Review access rights quarterly

## References
{regulation_short} - Article on security measures""".format(regulation_short=regulation_short),
        
        'Security Logging': """# Security Logging Implementation Standard

## Objective
Ensure all security-relevant events are recorded for monitoring and forensics.

## Requirements
1. Log all authentication events
2. Log all access to sensitive data
3. Retain logs for minimum 12 months
4. Implement log integrity protection

## References
{regulation_short} - Article on logging requirements""".format(regulation_short=regulation_short),
        
        'Risk Assessment': """# Risk Assessment Implementation Standard

## Objective
Ensure systematic identification and evaluation of security risks.

## Requirements
1. Conduct risk assessments before system deployment
2. Perform annual risk reviews
3. Document all identified risks and mitigations
4. Maintain risk register

## References
{regulation_short} - Article on risk management""".format(regulation_short=regulation_short),
        
        'Incident Response': """# Incident Response Implementation Standard

## Objective
Ensure rapid detection, response, and recovery from security incidents.

## Requirements
1. Maintain incident response plan
2. Conduct annual incident response testing
3. Establish 24/7 monitoring capability
4. Document all incidents and post-incident reviews

## References
{regulation_short} - Article on incident response""".format(regulation_short=regulation_short),
        
        'Secure Configuration': """# Secure Configuration Implementation Standard

## Objective
Ensure systems are configured according to security best practices.

## Requirements
1. Implement baseline configuration standards
2. Disable unnecessary services and ports
3. Regular configuration audits
4. Automated configuration management

## References
{regulation_short} - Article on secure configurations""".format(regulation_short=regulation_short),
        
        'Vulnerability Management': """# Vulnerability Management Implementation Standard

## Objective
Ensure security vulnerabilities are identified and remediated promptly.

## Requirements
1. Scan systems weekly for known vulnerabilities
2. Patch critical vulnerabilities within 7 days
3. Test patches before deployment
4. Maintain vulnerability management documentation

## References
{regulation_short} - Article on vulnerability management""".format(regulation_short=regulation_short),
        
        'Capacity': """# Capacity Implementation Standard

## Objective
Ensure adequate capacity to fulfill {regulation_short} obligations.

## Requirements
1. Implement required capacity
2. Test capacity regularly
3. Maintain documentation

## References
{regulation_short} - Relevant articles""".format(regulation_short=regulation_short)
    }
    
    # Return specific description or generic fallback
    return descriptions.get(capability_name, 
                           f"# {capability_name} Implementation Standard\n\n## Objective\nEnsure compliance with {regulation_short} obligations.\n\n## Requirements\n1. Implement {capability_name.lower()}\n2. Test capabilities regularly\n3. Maintain documentation\n\n## References\n{regulation_short} - Relevant articles")


def _generate_control_description(capability_name: str, control_type: str) -> str:
    """Generate control description based on capability and type."""
    if control_type == 'automated':
        return f"""Automated verification that {capability_name.lower()} is properly implemented.
- Scans systems for proper configuration
- Logs all findings
- Generates compliance reports"""
    
    else:  # manual
        return f"""Manual review of {capability_name.lower()} implementation.
- Review documentation and procedures
- Interview personnel responsible for capability
- Verify implementation matches requirements"""


def transform_requirement_to_obligation(Requirement, regulation_short: str) -> dict:
    """Transform a requirement into an obligation with proper ID."""
    import hashlib
    
    text = Requirement.get('text', '')
    source_ref = Requirement.get('source_ref', 'unknown')
    text_hash = hashlib.md5(text[:50].encode()).hexdigest()[:8]
    
    # Extract numbers from source reference
    import re
    numbers = ''.join(re.findall(r'\d+', source_ref)) or '0'
    
    return {
        'id': f"{regulation_short}_obl_{numbers}_{text_hash}",
        'type': Requirement.get('type', 'requirement'),
        'text': text,
        'confidence': 0.95,
        'source_ref': source_ref
    }


if __name__ == '__main__':
    # Test with sample capabilities
    test_capabilities = [
        {
            'name': 'Data Encryption',
            'description': 'Capacity to encrypt data at rest and in transit',
            'type': 'technical'
        },
        {
            'name': 'Access Control', 
            'description': 'Capacity to manage who can access what resources',
            'type': 'technical'
        }
    ]
    
    result = transform_capabilities_to_policy_chain(test_capabilities, "CRA")
    
    print("Policy Chain Generation Results:")
    print(f"  Policies: {len(result['policies'])}")
    print(f"  Standards: {len(result['standards'])}")
    print(f"  Controls: {len(result['controls'])}")
    
    if result['policies']:
        print("\n  Sample Policy:")
        print(f"    ID: {result['policies'][0]['id']}")
        print(f"    Title: {result['policies'][0]['title']}")
