#!/usr/bin/env python3
"""Integration test for graph insertion component.

This test creates nodes and edges in real FalkorDB with GRAPH module.
NO MOCKS - actual connections to real services.

To run this test:
1. Start FalkorDB: podman run --rm -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
2. Ensure redisgraph Python package is installed: pip install redisgraph
3. Run: pytest docs/spikes/graph-ingestion/test/test_graph_integration.py -v

Expected behaviors:
- Tests FAIL if FalkorDB not running or GRAPH module not loaded
- This demonstrates the tech actually works with real services
"""

import sys

# Add src to path
sys.path.insert(0, '/Users/tma/repos/steward/docs/spikes/graph-ingestion/src')

from graph import (
    insert_regulation_into_graph,
    insert_obligation_into_graph,
    create_contains_edge
)


def test_insert_regulation_node():
    """Insert a regulation node into real FalkorDB."""
    result = insert_regulation_into_graph({
        'id': 'test-reg-1',
        'title': 'Test Regulation 1',
        'jurisdiction': 'EU'
    })
    
    assert 'node_id' in result
    assert isinstance(result['node_id'], int)


def test_insert_obligation_node():
    """Insert an obligation node into real FalkorDB."""
    result = insert_obligation_into_graph({
        'id': 'test-obl-1',
        'type': 'requirement',
        'text': 'Implement encryption for data at rest',
        'confidence': 0.95,
        'source_ref': 'Article 1'
    })
    
    assert 'node_id' in result
    assert isinstance(result['node_id'], int)


def test_create_contains_edge():
    """Create edge from regulation to obligation."""
    # First create nodes
    r_result = insert_regulation_into_graph({
        'id': 'test-reg-2',
        'title': 'Test Regulation 2',
        'jurisdiction': 'EU'
    })
    
    o_result = insert_obligation_into_graph({
        'id': 'test-obl-2',
        'type': 'requirement',
        'text': 'Maintain access logs',
        'confidence': 0.90,
        'source_ref': 'Article 5'
    })
    
    # Create edge
    result = create_contains_edge(r_result['node_id'], o_result['node_id'])
    
    assert result.get('edge') is True


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
