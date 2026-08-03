#!/usr/bin/env python3
"""Unit tests for LLM-based obligation extraction component.

TDD Approach: Tests first, then implementation until green.
No fallbacks - errors must surface explicitly.
"""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from extractor import extract_obligations


def test_extract_single_obligation():
    """Extract a single obligation from regulatory text."""
    # Mock chunk content
    chunk_text = """
    Article 1 - Security Measures
    
    The responsible entity shall implement technical and organizational measures to ensure 
    appropriate levels of security. Where appropriate, the controller and processor shall 
    introduce security measures including encryption.
    """
    
    result = extract_obligations(chunk_text)
    
    # Should produce obligations
    assert len(result['obligations']) > 0
    assert any('security' in o['text'].lower() for o in result['obligations'])
    
    # Each obligation should have required fields
    for obligation in result['obligations']:
        assert 'type' in obligation, "Obligation missing 'type' field"
        assert 'confidence' in obligation, "Obligation missing 'confidence' field"
        assert 'source_ref' in obligation, "Obligation missing 'source_ref' field"
        assert obligation['confidence'] >= 0.90, f"Confidence below threshold: {obligation['confidence']}"


def test_extract_multiple_obligations():
    """Extract multiple obligations from regulatory text."""
    chunk_text = """
    Article 2 - Data Protection
    
    The controller shall implement technical measures to protect personal data.
    
    Processors shall maintain records of processing activities.
    
    Data subjects have the right to access their personal data.
    """
    
    result = extract_obligations(chunk_text)
    
    # The LLM correctly identifies obligations (not rights) - 3 sentences, 2 are obligations
    assert len(result['obligations']) >= 2, f"Expected at least 2 obligations, got {len(result['obligations'])}"


def test_extract_requirement_type():
    """Extract obligation type: requirement."""
    chunk_text = "The entity shall implement encryption for data at rest."
    
    result = extract_obligations(chunk_text)
    
    assert len(result['obligations']) > 0
    assert any(o['type'] == 'requirement' for o in result['obligations'])


def test_extract_low_confidence_fails():
    """Extract with low confidence should fail (no fallback)."""
    chunk_text = "This is vague text without clear obligations."
    
    result = extract_obligations(chunk_text)
    
    # Should NOT return low-confidence obligations
    assert len([o for o in result['obligations'] if o['confidence'] >= 0.90]) == 0


def test_extract_no_llm_response():
    """Handle LLM not responding (no fallback - error must surface explicitly)."""
    # Set invalid OLLAMA_URL to simulate connection failure
    import os
    original_url = os.environ.get('OLLAMA_URL')
    os.environ['OLLAMA_URL'] = 'http://invalid-host:9999'
    
    try:
        chunk_text = "Test obligation text."
        
        try:
            result = extract_obligations(chunk_text)
            assert False, "Expected ConnectionError for invalid Ollama URL"
        except ConnectionError as e:
            # Success - error surfaced explicitly
            pass
    finally:
        # Restore original env var
        if original_url is not None:
            os.environ['OLLAMA_URL'] = original_url
        else:
            del os.environ['OLLAMA_URL']


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
