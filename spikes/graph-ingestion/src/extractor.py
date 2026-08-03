#!/usr/bin/env python3
"""LLM-based obligation extraction component.

Extracts obligations from regulatory text chunks using LLM.
Requires Ollama API accessible at environment variable OLLAMA_URL.
"""

import os
import json
import re
import socket


def _create_extraction_prompt(chunk_text: str) -> str:
    """Create the prompt for LLM obligation extraction."""
    return f"""
You are a regulatory compliance expert analyzing EU legislation.

Extract all obligations from the following text. An obligation is a statement that specifies what must, must not, or should be done by a regulated entity.

Return your response as valid JSON with this structure:
{{
  "obligations": [
    {{
      "id": "unique_id_generated_here",
      "type": "requirement|prohibition|recommendation",
      "text": "The full obligation text extracted exactly from source",
      "confidence": 0.95,
      "source_ref": "Article X.Y where this appears in original document"
    }}
  ]
}}

Rules:
1. Only include obligations with confidence >= 0.90
2. If no clear obligations found, return empty obligations array (NOT an error)
3. Type should be: 'requirement' for 'shall', 'prohibition' for 'must not', 'recommendation' for 'should'
4. Confidence must be a float between 0.0 and 1.0
5. source_ref should reference the article/section header from the original content

Input text:
{chunk_text}
"""


def _truncate_for_extraction(text: str, max_chars: int = 5000) -> str:
    """Truncate text for LLM extraction if too long.
    
    Ollama requests can timeout with very long inputs. This function truncates
    while preserving important regulatory structure.
    """
    if len(text) <= max_chars:
        return text
    
    # Try to preserve article headers but truncate content
    lines = text.split('\n')
    result_lines = []
    char_count = 0
    
    for line in lines:
        if line.strip().startswith('Article ') or line.strip().startswith(('shall', 'must not', 'should')):
            # Always keep article headers and key obligation keywords
            result_lines.append(line)
            char_count += len(line) + 1
        elif char_count < max_chars - 500:
            result_lines.append(line)
            char_count += len(line) + 1
    
    return '\n'.join(result_lines)


def _clean_json_response(response_text: str) -> str:
    """Remove markdown code block wrappers from LLM responses."""
    # Remove triple-backtick code blocks with optional language specifier
    cleaned = re.sub(r'^```[a-z]*\n', '', response_text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r'```\s*$', '', cleaned, flags=re.IGNORECASE)
    
    return cleaned.strip()


def extract_obligations(chunk_text: str, max_retries: int = 2) -> dict:
    """Extract obligations from regulatory text chunk.
    
    Args:
        chunk_text: Text content from a single chunk
        max_retries: Number of retry attempts on timeout (default 2)
        
    Returns:
        {
            'obligations': [
                {
                    'id': str,           # Generated unique ID
                    'type': str,         # 'requirement'/'prohibition'/'recommendation'
                    'text': str,         # Extracted obligation text
                    'confidence': float, # 0.0-1.0 (must be >=0.90 for success)
                    'source_ref': str    # Source reference (e.g., 'Article 1')
                }
            ],
            'error': str           # Present if extraction failed (should not happen on success)
        }
        
    Raises:
        ConnectionError if LLM service unavailable after retries
    """
    # Check environment for Ollama URL
    ollama_url = os.environ.get('OLLAMA_URL', 'http://localhost:11434')
    
    # Truncate if too long to avoid timeouts
    truncated_text = _truncate_for_extraction(chunk_text, max_chars=5000)
    if len(chunk_text) > 5000:
        print(f"Truncated {len(chunk_text)} chars -> {len(truncated_text)} chars")
    
    # Build prompt with truncated text
    prompt = _create_extraction_prompt(truncated_text)
    
    # Call Ollama API directly (no fallback library to avoid hiding errors)
    import urllib.request
    import urllib.error
    
    last_error = None
    
    for attempt in range(max_retries + 1):
        try:
            request_data = json.dumps({
                # Using qwen3-coder-next:q8_0 for higher accuracy inference
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
                
        except urllib.error.URLError as e:
            last_error = e
            if attempt < max_retries:
                import time
                time.sleep(2)  # Wait before retry
                print(f"LLM request timeout (attempt {attempt+1}/{max_retries+1}), retrying...")
                continue
            
            # No fallback - error must surface after all retries
            raise ConnectionError(
                f"Cannot connect to Ollama at {ollama_url} after {max_retries + 1} attempts: {e}"
            )
        except socket.timeout as e:
            last_error = e
            if attempt < max_retries:
                import time
                time.sleep(2)
                print(f"LLM request timeout (attempt {attempt+1}/{max_retries+1}), retrying...")
                continue
            
            raise ConnectionError(
                f"Ollama request timed out after {max_retries + 1} attempts: {e}"
            )
        else:
            # Success
            break
    
    # Parse the response text for obligations
    response_text = result.get('response', '')
    cleaned_response = _clean_json_response(response_text)
    
    try:
        parsed = json.loads(cleaned_response)
        obligations = parsed.get('obligations', [])
        
    except json.JSONDecodeError:
        # LLM returned malformed JSON - this is a failure, not fallback territory
        raise ValueError(f"LLM returned invalid JSON: {response_text[:200]}")
    
    return {
        'obligations': obligations,
        'error': None if obligations else "No obligations extracted"
    }
