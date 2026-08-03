#!/usr/bin/env python3
"""Unit tests for chunker processing REAL EU regulation data.

Tests process actual EU regulations (CRA and NIS2) from eu-cra/eu-nis2 folders.
TDD Approach: Tests first, then implementation until green.
No fallbacks - errors must surface explicitly.
"""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from chunker import chunk_by_article


def get_eu_regulations():
    """Get paths to all EU regulation HTML files."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    
    regulations = []
    
    # Find all HTML files in eu-cra and eu-nis2 folders
    for folder_name in ['eu-cra', 'eu-nis2']:
        folder_path = os.path.join(project_root, folder_name)
        if not os.path.exists(folder_path):
            continue
        
        for file_name in os.listdir(folder_path):
            if file_name.endswith('.html'):
                regulations.append(os.path.join(folder_path, file_name))
    
    return regulations


def test_chunk_eu_regulations_exists():
    """Verify EU regulation files exist."""
    regulations = get_eu_regulations()
    
    assert len(regulations) >= 2, (
        f"Expected at least 2 EU regulations (eu-cra and eu-nis2 folders), "
        f"found {len(regulations)}"
    )


def test_chunk_both_regulations():
    """Verify both EU regulations (CRA and NIS2) can be chunked."""
    regulations = get_eu_regulations()
    
    for reg_path in regulations:
        with open(reg_path, 'r', encoding='utf-8') as f:
            html = f.read()
        
        chunks = chunk_by_article(html)
        
        # Verify each regulation was processed
        assert len(chunks) > 0, (
            f"Failed to chunk {os.path.basename(reg_path)}"
        )
        
        # Check article ID format (should be 'art_Article X')
        for chunk in chunks:
            assert chunk['article_id'].startswith('art_'), (
                f"Invalid article_id: {chunk['article_id']}"
            )


def test_chunk_different_regulations_different_article_counts():
    """Verify regulations with different article counts are handled correctly."""
    cra_path = get_eu_regulations()[0]  # One regulation
    
    with open(cra_path, 'r', encoding='utf-8') as f:
        html = f.read()
    
    chunks = chunk_by_article(html)
    
    # Count articles in the HTML to verify accuracy
    expected_articles = html.count('class="oj-ti-art"')
    
    assert len(chunks) == expected_articles, (
        f"Expected {expected_articles} chunks for this regulation, got {len(chunks)}"
    )


def test_chunk_multiple_regulations_structure():
    """Verify chunking preserves regulatory article structure across multiple regulations."""
    regulations = get_eu_regulations()
    
    for reg_path in regulations:
        with open(reg_path, 'r', encoding='utf-8') as f:
            html = f.read()
        
        chunks = chunk_by_article(html)
        
        # Test: Each chunk has substantial content (>100 chars)
        for i, chunk in enumerate(chunks):
            assert len(chunk['content']) > 100, (
                f"Chunk {i} from {os.path.basename(reg_path)} has insufficient content "
                f"({len(chunk['content'])} chars)"
            )


def test_chunk_returns_list_of_dicts():
    """Verify chunker returns list of dictionaries for each regulation."""
    regulations = get_eu_regulations()
    
    for reg_path in regulations:
        with open(reg_path, 'r', encoding='utf-8') as f:
            html = f.read()
        
        chunks = chunk_by_article(html)
        
        assert isinstance(chunks, list), "chunk_by_article must return a list"
        assert all(isinstance(c, dict) for c in chunks), (
            "All items must be dictionaries"
        )
        assert len(chunks) > 0, "No chunks produced for regulation"


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
