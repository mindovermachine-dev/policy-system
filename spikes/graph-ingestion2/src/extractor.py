#!/usr/bin/env python3
"""LLM-based multi-stage extraction component WITH ID GENERATION FIX.

Extracts all domain concepts from regulatory text chunks using LLM in two passes:
1. Primary pass: Extract Roles, Requirements, Obligations
2. Secondary pass: Infer Capabilities from obligations

Requires Ollama API accessible at environment variable OLLAMA_URL.
"""

import os
import json
import re
import hashlib


def generate_role_id(reg_prefix: str, role_name: str) -> str:
    """Generate role ID from regulation prefix and role name."""
    slug = re.sub(r'[^a-z0-9]', '_', role_name.lower().strip()[:24])
    return f"{reg_prefix}_role_{slug}"


def generate_obligation_id(reg_prefix: str, source_ref: str, text_hash: str) -> str:
    """Generate obligation ID from regulation + source + content hash.
    
    DEPRECATED: This generates regulation-prefixed IDs. Use canonical IDs instead
    for cross-regulation consistency.
    """
    numbers = ''.join(re.findall(r'\d+', source_ref))
    return f"{reg_prefix}_obl_{numbers}_{text_hash}"


def generate_canonical_obligation_id(obligation_text: str) -> str:
    """Generate a canonical obligation ID based on the obligation text content.
    
    This enables cross-regulation identification of similar obligations without
    regulation prefixes (e.g., no more 'CRA_obl_123_xyz' - instead we get
    'obl_risk_management_abc123').
    
    Args:
        obligation_text: The full text of the obligation
        
    Returns:
        Canonical obligation ID string
    """
    try:
        from obligation_taxonomy import generate_canonical_obligation_id as taxonomy_gen
        return taxonomy_gen(obligation_text)
    except (ImportError, AttributeError):
        # Fallback if obligation_taxonomy not available
        text_hash = hashlib.md5(obligation_text.encode()).hexdigest()[:8]
        return f"obl_generic_{text_hash}"


def generate_canonical_capability_id(capability_name: str, related_obligation_text: str) -> str:
    """Generate a canonical capability ID based on capability name and obligation context.
    
    This enables cross-regulation identification of similar capabilities.
    
    Args:
        capability_name: Name of the capability
        related_obligation_text: Text of the related obligation (for additional uniqueness)
        
    Returns:
        Canonical capability ID string
    """
    try:
        from obligation_taxonomy import generate_canonical_capability_id as taxonomy_gen
        return taxonomy_gen(capability_name, related_obligation_text)
    except (ImportError, AttributeError):
        # Fallback if obligation_taxonomy not available  
        combined_hash_input = f"{capability_name}:{related_obligation_text[:100]}"
        text_hash = hashlib.md5(combined_hash_input.encode()).hexdigest()[:8]
        return f"cap_generic_{text_hash}"


def _create_primary_extraction_prompt(chunk_text: str) -> str:
    """Create the prompt for multi-stage LLM extraction."""
    return f"""
You are a regulatory compliance expert analyzing EU legislation.

Extract ALL domain concepts from the following regulatory text. A regulation typically defines:
- ROLES (actors with duties)
- REQUIREMENTS (conditions that must be true)
- OBLIGATIONS (duties assigned to roles)

Return your response as valid JSON with this structure:
{{
  "roles": [
    {{
      "name": "Role name (e.g., Manufacturer, Operator)",
      "description": "Brief description of the role",
      "source_ref": "Article X.Y where this role is defined"
    }}
  ],
  "requirements": [
    {{
      "text": "Full requirement text extracted exactly from source",
      "type": "requirement|prohibition|recommendation",
      "source_ref": "Article X.Y where this appears in original document"
    }}
  ],
  "obligations": [
    {{
      "id": "auto-generated-by-pycode-will-overwrite-this",
      "text": "Full obligation text extracted exactly from source",
      "type": "requirement|prohibition|recommendation",  
      "confidence": 0.95,
      "source_ref": "Article X.Y where this appears in original document",
      "role_id": "reg_prefix_role_slug"  // ID of role having this duty (from roles array above)
    }}
  ]
}}

Rules:
1. Extract ALL roles, requirements, and obligations you find
2. If no elements found for a category, return empty array (NOT an error)
3. Role types: 'requirement' = 'shall', 'prohibition' = 'must not', 'recommendation' = 'should'
4. Confidence must be a float between 0.0 and 1.0
5. Only include items with confidence >= 0.90
6. source_ref should reference the article/section from the original content

Input text:
{chunk_text}
"""


def _create_capability_inference_prompt(obligations: list) -> str:
    """Create the prompt for capability inference."""
    obligations_text = "\n\n".join([
        f"Obligation {i+1}: [{obl['type']}] {obl['text']} (confidence: {obl.get('confidence', 'N/A')})"
        for i, obl in enumerate(obligations)
    ])
    
    return f"""
You are a regulatory compliance expert analyzing EU legislation.

Based on the following extracted obligations, identify what CAPABILITIES must exist to fulfill each obligation.
A capability is a technical or organizational capacity needed to meet the duty.

Return your response as valid JSON with this structure:
{{
  "capabilities": [
    {{
      "name": "Capability name (e.g., Data Encryption, Access Control)",
      "description": "Brief description of what this capability enables",
      "type": "technical|organizational",  
      "related_obligation_ref": "Article X.Y where the related obligation is defined"
    }}
  ]
}}

Rules:
1. Each capability should be a reusable capacity (not a one-off task)
2. Type: 'technical' for technical solutions, 'organizational' for process/procedure
3. Related obligation ref MUST be the article reference where the related obligation is defined (e.g., "Article 1.1", "Article 5(2)(a)")
4. Do NOT use indices like "Obligation 1" - always use actual article references
5. Return empty array if no clear capabilities can be identified

Extracted obligations:
{obligations_text}
"""


def _truncate_for_extraction(text: str, max_chars: int = 5000) -> str:
    """Truncate text for LLM extraction if too long."""
    if len(text) <= max_chars:
        return text
    
    lines = text.split('\n')
    result_lines = []
    char_count = 0
    
    for line in lines:
        if line.strip().startswith('Article ') or any(
            keyword in line.lower() 
            for keyword in ['shall', 'must not', 'should', 'may', 'will']
        ):
            result_lines.append(line)
            char_count += len(line) + 1
        elif char_count < max_chars - 500:
            result_lines.append(line)
            char_count += len(line) + 1
    
    return '\n'.join(result_lines)


def validate_roles_by_obligation_subject(roles: list, obligations: list) -> tuple:
    """Validate roles and assign role_ids to obligations.
    
    A role is only valid if its name appears as the subject in at least one obligation,
    following patterns like "The Manufacturer shall..." or "Manufacturer shall...".
    
    This validation ensures that Role nodes are grounded in actual regulatory text
    - they must appear as actors who have duties assigned, not just mentioned.
    
    Args:
        roles: List of extracted role dictionaries with 'name' field
        obligations: List of extracted obligation dictionaries with 'text' and 'source_ref'
        
    Returns:
        Tuple of (validated_roles, obligations_with_role_ids)
    """
    if not roles or not obligations:
        return roles, obligations
    
    valid_role_names = set()
    duty_verbs = ['shall', 'must', 'should']
    
    # First pass: find which roles appear as subjects in obligations
    for obl in obligations:
        OblText = obl.get('text', '')
        source_ref = obl.get('source_ref', '')
        combined_text = f"{OblText} {source_ref}".lower()
        
        # Split by duty verbs to examine what's before each verb
        for verb in duty_verbs:
            if verb not in combined_text:
                continue
            
            # EverythingBeforeVerb is the subject area we care about
            parts_before_verb = combined_text.split(verb)[0]
            
            for role in roles:
                role_name_original = role.get('name', '')
                role_name_lower = role_name_original.lower().strip()
                
                # Skip if already validated
                if role_name_original in valid_role_names:
                    continue
                
                # Check if role name appears in the subject area (before a duty verb)
                # This handles: "The Manufacturer shall", "Manufacturer, Distributor shall"
                if role_name_lower in parts_before_verb:
                    valid_role_names.add(role_name_original)
                    break  # Role validated, move to next obligation
    
    # Filter roles to keep only those validated by obligation subjects
    validated_roles = [r for r in roles if r.get('name') in valid_role_names]
    
    # Second pass: assign role_ids to obligations that have validated roles
    for obl in obligations:
        OblText = obl.get('text', '')
        source_ref = obl.get('source_ref', '')
        combined_text = f"{OblText} {source_ref}".lower()
        
        # Find which role this obligation belongs to (from VALIDATED roles only)
        for role in validated_roles:
            role_name_lower = role.get('name', '').lower().strip()
            
            # Check if role name appears as subject (before duty verbs) in obligation text
            for verb in duty_verbs:
                if verb not in combined_text:
                    continue
                # Everything before the verb is the subject area
                parts_before_verb = combined_text.split(verb)[0]
                if role_name_lower in parts_before_verb:
                    obl['role_id'] = role.get('id')
                    break  # Found, move to next obligation
    
    # Log drop statistics
    dropped_count = len(roles) - len(validated_roles)
    if dropped_count > 0:
        print(f"    Role validation: dropped {dropped_count} of {len(roles)} roles "
              f"(only {len(validated_roles)} appeared as obligation subjects)")
    
    # Log role_id assignment statistics
    obligations_with_role = sum(1 for obl in obligations if 'role_id' in obl and obl['role_id'])
    print(f"    Role-Obligation linking: {obligations_with_role}/{len(obligations)} obligations have role assignments")
    
    return validated_roles, obligations



def _clean_json_response(response_text: str) -> str:
    """Remove markdown code block wrappers from LLM responses."""
    cleaned = re.sub(r'^```[a-z]*\n', '', response_text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r'```\s*$', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _call_llm(prompt: str, ollama_url: str) -> dict:
    """Call Ollama API and parse response."""
    import urllib.request
    import socket
    
    max_retries = 2
    
    for attempt in range(max_retries + 1):
        try:
            request_data = json.dumps({
                'model': 'qwen3-coder-next:q8_0',
                'prompt': prompt,
                'stream': False
            }).encode('utf-8')
            
            req = urllib.request.Request(
                f'{ollama_url}/api/generate',
                data=request_data,
                headers={'Content-Type': 'application/json'}
            )
            
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
                
        except (urllib.error.URLError, socket.timeout) as e:
            if attempt < max_retries:
                import time
                time.sleep(2)
                print(f"  LLM request timeout (attempt {attempt+1}/{max_retries+1}), retrying...")
                continue
            
            return {'error': f"Cannot connect to Ollama after {max_retries + 1} attempts: {e}"}
        
        else:
            # Success - parse response
            break
    
    response_text = result.get('response', '')
    cleaned_response = _clean_json_response(response_text)
    
    try:
        parsed = json.loads(cleaned_response)
        return {'parsed': parsed, 'error': None}
    except json.JSONDecodeError as e:
        return {'error': f"LLM returned invalid JSON: {e}"}


def extract_all_concepts(chunk_text: str, chunk_id: str = "primary", ollama_url: str = None) -> dict:
    """Extract all domain concepts from regulatory text chunk using two-pass strategy.
    
    Pass 1: Extract roles, requirements, and obligations in a single structured response
    Pass 2: Infer capabilities from the extracted obligations
    
    Args:
        chunk_text: Text content from a single chunk
        chunk_id: Identifier for this chunk (for logging)
        ollama_url: Ollama API URL (default: from environment or http://localhost:11434)
        
    Returns:
        {
            'roles': [...],
            'requirements': [...],
            'obligations': [...],  
            'capabilities': [...],
            'error': str  # Present if extraction failed
        }
    """
    ollama_url = ollama_url or os.environ.get('OLLAMA_URL', 'http://localhost:11434')
    
    # Truncate if too long to avoid timeouts
    truncated_text = _truncate_for_extraction(chunk_text, max_chars=5000)
    
    print(f"  Extracting concepts from chunk {chunk_id}...")
    
    # ========== PASS 1: Extract Roles, Requirements, Obligations ==========
    prompt1 = _create_primary_extraction_prompt(truncated_text)
    
    result1 = _call_llm(prompt1, ollama_url)
    
    if result1['error']:
        return {
            'roles': [], 
            'requirements': [], 
            'obligations': [], 
            'capabilities': [], 
            'error': f"Pass 1 failed: {result1['error']}"
        }
    
    parsed1 = result1.get('parsed', {})
    roles = parsed1.get('roles', [])
    requirements = parsed1.get('requirements', [])
    obligations = parsed1.get('obligations', [])
    
    # ========== ROLE VALIDATION (AC-6) ==========
    # Filter roles to keep only those that appear as subjects in obligations
    # AND assign role_ids to obligations that have matching roles
    validated_roles, obligations = validate_roles_by_obligation_subject(roles, obligations)
    role_count_before = len(roles)
    roles = validated_roles
    print(f"    Roles: {role_count_before} raw → {len(roles)} validated")
    
    # Generate IDs for all extracted concepts
    article_num = re.search(r'Article\s+(\d+)', chunk_id)
    article_number = article_num.group(1) if article_num else '0'
    reg_prefix = chunk_id.split('_')[0].upper()
    
    # Store old role_id to new role_id mapping for later updates
    role_name_to_id_map = {}
    for role in roles:
        role['id'] = generate_role_id(reg_prefix, role.get('name', 'unknown'))
        role_name_to_id_map[role.get('name', '').lower()] = role['id']
    
    # Generate IDs for requirements  
    # NEW: Use article-number-based ID for idempotency (AC-2)
    article_num_match = re.search(r'Article\s+(\d+)', chunk_id)
    article_number = article_num_match.group(1) if article_num_match else '0'
    reg_prefix = chunk_id.split('_')[0].upper()
    
    for req in requirements:
        source_ref = req.get('source_ref', '')
        # Extract only leading article number (e.g., "Article 32(1)(a)" → "32")
        artikel_num = re.search(r'Article\s+(\d+)', source_ref)
        article_number_for_id = artikel_num.group(1) if artikel_num else article_number
        req['id'] = f"{reg_prefix}_req_art_{article_number_for_id}"
    
    # Generate IDs for obligations (overwrite any LLM-generated ones)
    # NEW: Use canonical obligation IDs (content-based, regulation-independent) - AC-4
    for obl in obligations:
        source_ref = obl.get('source_ref', '')
        obligation_text = obl.get('text', '')
        # Canonical ID based on content, not regulation prefix
        obl['id'] = generate_canonical_obligation_id(obligation_text)
        
        # Assign role_id by matching obligation text to extracted roles
        OblText = obl.get('text', '')
        combined_text = f"{OblText} {source_ref}".lower()
        
        # Find which role this obligation belongs to
        for role in roles:
            role_name_lower = role.get('name', '').lower().strip()
            
            # Check if role name appears as subject (before duty verbs) in obligation text
            duty_verbs = ['shall', 'must', 'should']
            for verb in duty_verbs:
                if verb not in combined_text:
                    continue
                # Everything before the verb is the subject area
                parts_before_verb = combined_text.split(verb)[0]
                if role_name_lower in parts_before_verb:
                    obl['role_id'] = role.get('id')
                    break  # Found, move to next obligation
    
    print(f"    Roles: {len(roles)}, Requirements: {len(requirements)}, Obligations: {len(obligations)}")
    
    # FIX (AC-6): Re-assign role_ids to obligations after role IDs were regenerated
    for obl in obligations:
        if 'role_id' not in obl or not obl['role_id']:
            continue
        # Extract role name from old ID format: reg_prefix_role_name -> role_name
        # Role ID is like "CRA_role_manufacturer", extract "manufacturer"
        old_role_id = obl.get('role_id', '')
        # Find the new role ID matching this role name
        for role in roles:
            if role['name'].lower() in old_role_id.lower():
                obl['role_id'] = role['id']
                print(f"    Fixed role_id: {old_role_id} → {role['id']} for Obligation")
                break
    
    # ========== PASS 2: Infer Capabilities from Obligations ==========
    if not obligations:
        return {
            'roles': roles,
            'requirements': requirements,
            'obligations': obligations,
            'capabilities': [],
            'error': None
        }
    
    # Build obligations text for capability inference prompt
    prompt2 = _create_capability_inference_prompt(obligations)
    
    result2 = _call_llm(prompt2, ollama_url)
    
    if result2['error']:
        return {
            'roles': roles,
            'requirements': requirements,
            'obligations': obligations,
            'capabilities': [],
            'error': f"Capability inference failed: {result2['error']}"
        }
    
    parsed2 = result2.get('parsed', {})
    capabilities = parsed2.get('capabilities', [])
    
    # Add IDs to capabilities - use canonical IDs based on content, not regulation prefix (AC-4)
    for cap in capabilities:
        capability_name = cap.get('name', 'unknown')
        related_obl_ref = cap.get('related_obligation_ref', '')
        related_obligation_text = ''
        
        # Find the text of the related obligation from obligations list
        for obl in obligations:
            if obl.get('source_ref', '') == related_obl_ref or related_obl_ref in obl.get('text', '')[:50]:
                related_obligation_text = obl.get('text', '')
                break
        
        cap['id'] = generate_canonical_capability_id(capability_name, related_obligation_text)
    
    return {
        'roles': roles,
        'requirements': requirements,
        'obligations': obligations,
        'capabilities': capabilities,
        'error': None
    }


if __name__ == '__main__':
    # Test with sample text
    test_text = """
    <p class="oj-ti-art">Article 1 General provisions</p>
    <p>Manufacturers shall ensure products meet requirements.</p>
    <p>The manufacturer shall implement cyber-risk management.</p>
    """
    
    result = extract_all_concepts(test_text, "cra_art_1")
    
    print("\nRoles:")
    for role in result.get('roles', []):
        print(f"  {role.get('id')}: {role.get('name')}")
    
    print("\nRequirements:")
    for req in result.get('requirements', []):
        print(f"  {req.get('id')}: {req.get('text', 'N/A')[:50]}...")
    
    print("\nObligations:")
    for obl in result.get('obligations', []):
        print(f"  {obl.get('id')}: {obl.get('type')} - {obl.get('text', 'N/A')[:50]}...")
    
    print("\nCapabilities:")
    for cap in result.get('capabilities', []):
        print(f"  {cap}")
