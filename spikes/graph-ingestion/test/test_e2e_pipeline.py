#!/usr/bin/env python3
"""End-to-End Integration Test: Full pipeline from EU regulation HTML to graph DB.

Tests the complete pipeline:
  eu-cra HTML → chunk_by_article() → extract_obligations() → insert into FalkorDB

TDD Approach: Tests first, then implementation until green.
No fallbacks - errors must surface explicitly.
"""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from chunker import chunk_by_article
from extractor import extract_obligations
from graph import insert_regulation_into_graph, insert_obligation_into_graph, create_contains_edge


def _reset_falkordb_graph():
    """Check if graph has data, reset if needed."""
    from redisgraph import Graph
    
    graph = Graph('policy_system', host='localhost', port=6379)
    
    # Check existing node counts
    reg_count = graph.query("MATCH (r:Regulation) RETURN count(*)").result_set[0][0]
    obl_count = graph.query("MATCH (o:Obligation) RETURN count(*)").result_set[0][0]
    
    if reg_count > 0 or obl_count > 0:
        print(f"⚠️  Graph has existing data: {reg_count} regulations, {obl_count} obligations")
        print("🔧 Resetting graph to empty state...")
        
        # Delete all nodes and edges
        result = graph.delete()
        
        if not isinstance(result, (bytes, str)) or 'OK' not in str(result).upper():
            raise RuntimeError(f"Failed to delete graph: {result}")
        
        # Verify reset
        reg_count_after = graph.query("MATCH (r:Regulation) RETURN count(*)").result_set[0][0]
        obl_count_after = graph.query("MATCH (o:Obligation) RETURN count(*)").result_set[0][0]
        
        if reg_count_after > 0 or obl_count_after > 0:
            raise RuntimeError(f"Graph reset failed - still has {reg_count_after} regs, {obl_count_after} obligations")
        
        print("✅ Graph reset to empty state")


# Add at module level for clarity
print = print  # Keep reference


def get_cra_html_path():
    """Get path to EU CRA HTML file."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    cra_path = os.path.join(project_root, 'eu-cra', 'L_202402847EN.000101.fmx.xml.html')
    
    if not os.path.exists(cra_path):
        raise FileNotFoundError(f"CRA HTML file not found: {cra_path}")
    
    return cra_path


def test_full_pipeline_cra_to_graph():
    """Test end-to-end pipeline from EU CRA HTML to graph database."""
    # Reset graph if it has data (to ensure clean state)
    _reset_falkordb_graph()
    
    print("Starting fresh with empty graph...")
    
    # Step 1: Read EU CRA HTML
    cra_path = get_cra_html_path()
    
    with open(cra_path, 'r', encoding='utf-8') as f:
        html = f.read()
    
    print(f"Loaded EU CRA HTML ({len(html)} bytes)")
    
    # Step 2: Chunk by article
    chunks = chunk_by_article(html)
    print(f"Chunked into {len(chunks)} articles")
    
    assert len(chunks) > 0, "Expected at least one article chunk"
    assert all('article_id' in c and 'content' in c for c in chunks), \
        "All chunks must have article_id and content"
    
    # Step 3: Extract obligations from first few chunks only (for faster test)
    # Use single mid-range article for faster testing
    chunk_indices = [10] if len(chunks) > 10 else [len(chunks) // 2]
    
    print(f"Processing {len(chunk_indices)} chunks (sample article {chunk_indices[0]+1})...")
    
    all_obligations = []
    
    for i in chunk_indices:
        chunk = chunks[i]
        print(f"Extracting obligations from article {i+1}/{len(chunks)}...")
        
        result = extract_obligations(chunk['content'])
        obligations = result.get('obligations', [])
        
        # Add source reference to each obligation
        for obl in obligations:
            obl['source_ref'] = chunk['article_id']
        
        all_obligations.extend(obligations)
    
    print(f"Extracted {len(all_obligations)} total obligations from sampled chunks")
    
    assert len(all_obligations) > 0, (
        "Expected at least one obligation from EU CRA"
    )
    
    # Step 4: Insert into graph database
    # First create a regulation node (idempotent with MERGE)
    regulation = {
        'id': 'CRA-1.0',
        'title': 'Cyber Resilience Act',
        'jurisdiction': 'EU'
    }
    
    reg_result = insert_regulation_into_graph(regulation)
    reg_node_id = reg_result['node_id']
    print(f"Created regulation node with ID: {reg_node_id}")
    
    # Insert each obligation and create CONTAINS edges (idempotent with MERGE)
    for obl in all_obligations:
        # Generate ID from source_ref and text hash (for fresh load, no idempotency needed)
        source_ref_clean = obl.get('source_ref', 'unknown').replace('art_', '')
        obl_id = "CRA_" + source_ref_clean + "_" + str(hash(obl["text"]))[:8]
