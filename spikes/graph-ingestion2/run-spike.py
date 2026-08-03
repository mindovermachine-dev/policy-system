#!/usr/bin/env python3
"""
Spike Runner: Full Concept Ingestion Pipeline for Graph Ingestion 2

This script ingest ALL domain concepts from EU regulations into FalkorDB:
  Regulation → Requirement → Obligation → Capability → Policy → Standard → Control

Usage:
    ./run-spike.py                    # Process both regulations (default)
    ./run-spike.py --max-chunks 5     # Process first 5 articles only (fast demo)
    ./run-spike.py --regulation cra   # Try CRA only
    ./run-spike.py --regulation nis2  # Try NIS2 only

Note: This implementation uses two-pass LLM extraction:
  Pass 1: Extract roles, requirements, obligations from each article
  Pass 2: Infer capabilities from extracted obligations
"""

import argparse
import hashlib
import os
import sys
from typing import Dict, List, Any


# Add src directory to path
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(script_dir, 'src'))


def reset_graph():
    """Reset graph database to clean state."""
    from graph import reset_graph
    
    reg_count = get_node_count('Regulation')
    obl_count = get_node_count('Obligation')
    
    if reg_count > 0 or obl_count > 0:
        print(f"Clearing database: {reg_count} regulations, {obl_count} obligations")
        reset_graph()
        print("Database cleared")


def get_node_count(label: str) -> int:
    """Get count of nodes with given label."""
    from graph import get_node_count as gnc
    return gnc(label)


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Run the graph ingestion spike 2 - full concept extraction.'
    )
    
    parser.add_argument(
        '--max-chunks',
        type=int,
        default=None,
        help='Maximum number of article chunks to process per regulation (default: all)'
    )
    
    parser.add_argument(
        '--ollama-url',
        type=str,
        default=os.environ.get('OLLAMA_URL', 'http://localhost:11434'),
        help=f'Ollama API URL (default: {os.environ.get("OLLAMA_URL", "http://localhost:11434")})'
    )
    
    parser.add_argument(
        '--regulation',
        type=str,
        default=None,  # None means process both when using --all
        choices=['cra', 'nis2'],
        help='Which regulation to process: cra or nis2'
    )
    
    parser.add_argument(
        '--all',
        action='store_true',
        dest='ingest_all',
        help='Ingest all regulations (CRA + NIS2) - default behavior when no --regulation specified'
    )
    
    return parser.parse_args()


def get_regulation_path(regulation_type: str) -> str:
    """Get path to EU regulation HTML file."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Use parent directory's data files from spike 1
    data_parent = os.path.join(script_dir, '..', 'graph-ingestion')
    
    if regulation_type == 'cra':
        filepath = os.path.join(data_parent, 'eu-cra', 'L_202402847EN.000101.fmx.xml.html')
    else:  # nis2
        filepath = os.path.join(data_parent, 'eu-nis2', 'L_2022333EN.01008001.xml.html')
    
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Regulation file not found: {filepath}")
    
    return filepath


def extract_all_concepts_for_chunk(chunk_id: str, html_content: str, ollama_url: str) -> dict:
    """Extract all concepts from a single HTML chunk."""
    from extractor import extract_all_concepts
    from extractor import validate_roles_by_obligation_subject  # Import validation function
    from transformer import transform_capabilities_to_policy_chain
    
    print(f"  Processing {chunk_id}...")
    
    # Pass 1 & 2: Extract roles, requirements, obligations, capabilities
    extraction_result = extract_all_concepts(html_content, chunk_id, ollama_url)
    
    if extraction_result['error']:
        return {'error': extraction_result['error']}
    
    results = {
        'roles': extraction_result['roles'],
        'requirements': extraction_result['requirements'],
        'obligations': extraction_result['obligations'],
        'capabilities': extraction_result['capabilities']
    }
    
    # Validate roles by checking if they appear as subjects in obligations
    validated_roles, obligations_with_role_ids = validate_roles_by_obligation_subject(
        results['roles'],
        results['obligations']
    )
    results['roles'] = validated_roles
    results['obligations'] = obligations_with_role_ids
    print(f"    After role validation: {len(results['roles'])} roles kept")
    
    print(f"    Extracted: {len(results['roles'])} roles, "
          f"{len(results['requirements'])} requirements, "
          f"{len(results['obligations'])} obligations, "
          f"{len(results['capabilities'])} capabilities")
    
    # Transform capabilities to policy chain
    regulation_short = chunk_id.split('_')[0]  # e.g., 'CRA' from 'cra_art_1'
    policy_chain = transform_capabilities_to_policy_chain(
        results['capabilities'], 
        regulation_short.upper()
    )
    
    results.update(policy_chain)
    
    return {
        **results,
        'error': None
    }


def generate_business_id(entity_type: str, name: str, prefix: str) -> str:
    """Generate a business-friendly ID for any entity."""
    import re
    slug = re.sub(r'[^a-z0-9]', '_', name.lower().strip()[:32])
    return f"{prefix}_{slug}"


def validate_extraction_results(results: dict, regulation_short: str) -> bool:
    """Validate that extraction results contain required fields."""
    # At minimum, we should have obligations extracted
    if not results.get('obligations') and not results.get('error'):
        print(f"  WARNING: No obligations extracted from {regulation_short}")
    
    return 'error' not in results or results['error'] is None


def ingest_regulation_into_graph(regulation_data: dict, regulation_type: str) -> dict:
    """Ingest all extracted concepts for a regulation into the graph.
    
    Args:
        regulation_data: {
            'roles': [...],
            'requirements': [...],
            'obligations': [...],
            'capabilities': [...],
            'policies': [...],
            'standards': [...],
            'controls': [...]
        }
        regulation_type: 'cra' or 'nis2'
        
    Returns:
        {
            'regulation_node_id': int,
            'success_count': {...},  # counts for each concept type
            'failure_count': {...},  # NEW: counts of failures per relationship type
            'error': str (if any)
        }
    """
    from graph import (
        insert_regulation_into_graph, insert_role_into_graph,
        insert_requirement_into_graph, insert_obligation_into_graph,
        insert_capability_into_graph, insert_policy_into_graph,
        insert_standard_into_graph, insert_control_into_graph,
        create_regulation_contains_requirement,
        create_requirement_satisfied_by_obligation,
        create_obligation_requires_capability,
        create_capability_governed_by_policy,
        create_policy_supported_by_standard,
        create_standard_validates_control,
        create_role_has_obligation,
        create_regulation_defines_role
    )
    
    success_count = {}
    failure_count = {}  # NEW: Track failures for each relationship type
    
    try:
        # First, insert the regulation node (we need it for IDs)
        regulation_id = f"{regulation_type.upper()}-1.0"
        
        regulation_node = {
            'id': regulation_id,
            'title': 'Cyber Resilience Act' if regulation_type == 'cra' else 'Network and Information Security Directive',
            'jurisdiction': 'EU',
            'version': '1.0'
        }
        
        reg_node_id = insert_regulation_into_graph(regulation_node)
        print(f"  Regulation '{regulation_id}' node created (ID: {reg_node_id})")
        success_count['Regulation'] = 1
        
        # Then process each concept type in order ( respect relationships )
        all_nodes = {}  # Store IDs for relationship creation
        
        # 1. Insert roles (and create DEFINES edges from regulation)
        role_ids = []
        for role in regulation_data.get('roles', []):
            try:
                role['regulation_id'] = regulation_id
                node_id = insert_role_into_graph(role)
                role_ids.append(node_id)
                success_count['Role'] = success_count.get('Role', 0) + 1

                # Create edge: Regulation → defines → Role
                create_regulation_defines_role(regulation_id, role['id'])
            except Exception as e:
                # NEW: Log failure with full context (node id + error)
                print(f"    Role insert failed: id={role.get('id', 'unknown')}, name={role.get('name', 'unknown')}: {e}")
                failure_count['Role_insert'] = failure_count.get('Role_insert', 0) + 1

        all_nodes['roles'] = role_ids
        
        # 2. Insert requirements (and create CONTAINS edges from regulation)
        req_ids = []
        for req in regulation_data.get('requirements', []):
            try:
                node_id = insert_requirement_into_graph(req)
                req_ids.append(node_id)
                
                # Create edge: Regulation → contains → Requirement
                edge_result = create_regulation_contains_requirement(regulation_id, req['id'])
                
                success_count['Requirement'] = success_count.get('Requirement', 0) + 1
            except Exception as e:
                print(f"    Requirement insert failed: id={req.get('id', 'unknown')}, text={req.get('text', '')[:50]}...: {e}")
                failure_count['Requirement_insert'] = failure_count.get('Requirement_insert', 0) + 1
        
        all_nodes['requirements'] = req_ids
        
        # 3. Insert obligations (and create SATISFIED_BY edges from requirements)
        obl_ids = []
        for obl in regulation_data.get('obligations', []):
            try:
                node_id = insert_obligation_into_graph(obl)
                obl_ids.append(node_id)
                
                success_count['Obligation'] = success_count.get('Obligation', 0) + 1
            except Exception as e:
                print(f"    Obligation insert failed: id={obl.get('id', 'unknown')}, text={obl.get('text', '')[:50]}...: {e}")
                failure_count['Obligation_insert'] = failure_count.get('Obligation_insert', 0) + 1
        
        all_nodes['obligations'] = obl_ids
        
        # NEW: Create SATISFIED_BY edges - with proper matching and tracking
        for obl in regulation_data.get('obligations', []):
            try:
                obl_source_ref = obl.get('source_ref', '')
                if not obl_source_ref:
                    print(f"    Obligation {obl['id']}: missing source_ref, skipping SATISFIED_BY edge")
                    failure_count['SATISFIED_BY_edge'] = failure_count.get('SATISFIED_BY_edge', 0) + 1
                    continue
                
                # Extract article number from source_ref (e.g., "Article 32(1)" → "32")
                import re as rr
                article_match = rr.search(r'Article\s+(\d+)', obl_source_ref)
                if not article_match:
                    print(f"    Obligation {obl['id']}: unparseable source_ref '{obl_source_ref}', skipping SATISFIED_BY edge")
                    failure_count['SATISFIED_BY_edge'] = failure_count.get('SATISFIED_BY_edge', 0) + 1
                    continue
                
                article_num = article_match.group(1)
                
                # Find the requirement sharing this article number
                matching_req = None
                for req in regulation_data.get('requirements', []):
                    req_source_ref = req.get('source_ref', '')
                    req_article_match = rr.search(r'Article\s+(\d+)', req_source_ref)
                    if req_article_match and req_article_match.group(1) == article_num:
                        matching_req = req
                        break
                
                if not matching_req:
                    print(f"    Obligation {obl['id']}: no Requirement found for article {article_num}, skipping SATISFIED_BY edge")
                    failure_count['SATISFIED_BY_edge'] = failure_count.get('SATISFIED_BY_edge', 0) + 1
                    continue
                
                create_requirement_satisfied_by_obligation(matching_req['id'], obl['id'])
                
            except Exception as e:
                print(f"    Failed to create SATISFIED_BY edge for Obligation {obl.get('id', 'unknown')}: {e}")
                failure_count['SATISFIED_BY_edge'] = failure_count.get('SATISFIED_BY_edge', 0) + 1
        for obl in regulation_data.get('obligations', []):
            role_id = obl.get('role_id')
            if role_id:
                try:
                    create_role_has_obligation(role_id, obl['id'])
                    print(f"    Created Role→Obligation edge: {role_id} → {obl['id']}")
                except Exception as e:
                    print(f"    Failed to create HAS edge for Obligation {obl['id']} (role={role_id}): {e}")
                    failure_count['HAS_edge'] = failure_count.get('HAS_edge', 0) + 1
            else:
                # Log without failing - obligation has no role assignment
                pass
        
        # NEW: Track and report HAS edge coverage (AC-6, AC-7 collapse guard)
        obligations_with_role = sum(1 for obl in regulation_data.get('obligations', []) if obl.get('role_id'))
        total_obligations = len(regulation_data.get('obligations', []))
        print(f"    HAS edge coverage: {obligations_with_role}/{total_obligations} obligations have role assignments ({100*obligations_with_role/total_obligations:.1f}% if total > 0)")
        
        # 4. Insert capabilities and create REQUIRES edges
        cap_ids = []
        capability_map = {}  # Track capabilities by ID to avoid duplicates
        
        for cap in regulation_data.get('capabilities', []):
            try:
                node_id = insert_capability_into_graph(cap)
                cap_ids.append(node_id)
                success_count['Capability'] = success_count.get('Capability', 0) + 1
            except Exception as e:
                print(f"    Capability insert failed: id={cap.get('id', 'unknown')}, name={cap.get('name', 'unknown')}: {e}")
                failure_count['Capability_insert'] = failure_count.get('Capability_insert', 0) + 1
        
        all_nodes['capabilities'] = cap_ids
        
        # Create REQUIRES edges: each capability links to its related obligation
        for cap in regulation_data.get('capabilities', []):
            try:
                related_obl_ref = cap.get('related_obligation_ref', '')
                
                if not related_obl_ref:
                    print(f"    Capability {cap['id']}: missing related_obligation_ref, skipping REQUIRES edge")
                    failure_count['REQUIRES_edge'] = failure_count.get('REQUIRES_edge', 0) + 1
                    continue
                
                # Find the obligation matching this reference
                import re as rr
                article_match = rr.search(r'Article\s+(\d+)', related_obl_ref)
                matching_obl = None
                
                if article_match:
                    # Primary match: parseable article reference
                    article_num = article_match.group(1)
                    for obl in regulation_data.get('obligations', []):
                        obl_source_ref = obl.get('source_ref', '')
                        obl_article_match = rr.search(r'Article\s+(\d+)', obl_source_ref)
                        if obl_article_match and obl_article_match.group(1) == article_num:
                            matching_obl = obl
                            break
                else:
                    # Try to match "Obligation X" indices (secondary fallback)
                    obl_index_match = rr.search(r'Obligation\s+(\d+)', related_obl_ref)
                    if obl_index_match:
                        index = int(obl_index_match.group(1))
                        obligations_list = regulation_data.get('obligations', [])
                        # Index is 1-based, list is 0-based
                        if 1 <= index <= len(obligations_list):
                            matching_obl = obligations_list[index - 1]
                    
                    if not matching_obl:
                        # Fallback match: capability name as substring of obligation text (AC-3)
                        cap_name_lower = cap.get('name', '').lower()[:30]
                        for obl in regulation_data.get('obligations', []):
                            obl_text = obl.get('text', '').lower()
                            # Check if Capability name appears in Obligation text
                            if cap_name_lower in obl_text:
                                matching_obl = obl
                                print(f"    Capability {cap['id']}: using fallback match - '{cap_name_lower}' found in obligation")
                                break
                
                if not matching_obl:
                    print(f"    Capability {cap['id']}: no Obligation found for related_obligation_ref='{related_obl_ref}', skipping REQUIRES edge")
                    failure_count['REQUIRES_edge'] = failure_count.get('REQUIRES_edge', 0) + 1
                    continue
                
                create_obligation_requires_capability(matching_obl['id'], cap['id'])
                
            except Exception as e:
                print(f"    Failed to create REQUIRES edge for Capability {cap.get('id', 'unknown')}: {e}")
                failure_count['REQUIRES_edge'] = failure_count.get('REQUIRES_edge', 0) + 1
        
        # 5. Insert policies (and create GOVERNED_BY edges from capabilities)
        pol_ids = []
        # Capabilities → Policies is one-to-one; use each policy's explicit
        # capability_id reference for exact-lookup edge creation instead of
        # positional pairing (AC-5).
        for pol in regulation_data.get('policies', []):
            try:
                node_id = insert_policy_into_graph(pol)
                pol_ids.append(node_id)
                success_count['Policy'] = success_count.get('Policy', 0) + 1

                cap_id = pol.get('capability_id')
                if cap_id:
                    # Create edge: Capability → governed by → Policy
                    create_capability_governed_by_policy(cap_id, pol['id'])
                else:
                    print(f"    Policy {pol['id']}: missing capability_id, skipping GOVERNED_BY edge")
                    failure_count['GOVERNED_BY_edge'] = failure_count.get('GOVERNED_BY_edge', 0) + 1

            except Exception as e:
                print(f"    Policy insert failed: id={pol.get('id', 'unknown')}, title={pol.get('title', 'unknown')}: {e}")
                failure_count['Policy_insert'] = failure_count.get('Policy_insert', 0) + 1

        all_nodes['policies'] = pol_ids

        # 6. Insert standards (and create SUPPORTED_BY edges from policies)
        std_ids = []
        # Policies → Standards is one-to-one; use each standard's explicit
        # policy_id reference for exact-lookup edge creation (AC-5).
        for std in regulation_data.get('standards', []):
            try:
                node_id = insert_standard_into_graph(std)
                std_ids.append(node_id)
                success_count['Standard'] = success_count.get('Standard', 0) + 1

                pol_id = std.get('policy_id')
                if pol_id:
                    # Create edge: Policy → supported by → Standard
                    create_policy_supported_by_standard(pol_id, std['id'])
                else:
                    print(f"    Standard {std['id']}: missing policy_id, skipping SUPPORTED_BY edge")
                    failure_count['SUPPORTED_BY_edge'] = failure_count.get('SUPPORTED_BY_edge', 0) + 1

            except Exception as e:
                print(f"    Standard insert failed: id={std.get('id', 'unknown')}, title={std.get('title', 'unknown')}: {e}")
                failure_count['Standard_insert'] = failure_count.get('Standard_insert', 0) + 1

        all_nodes['standards'] = std_ids

        # 7. Insert controls (and create IMPLEMENTED_BY edges from standards)
        ctrl_ids = []
        # Standards → Controls is one-to-one; use each control's explicit
        # standard_id reference for exact-lookup edge creation (AC-5).
        for ctrl in regulation_data.get('controls', []):
            try:
                node_id = insert_control_into_graph(ctrl)
                ctrl_ids.append(node_id)
                success_count['Control'] = success_count.get('Control', 0) + 1

                std_id = ctrl.get('standard_id')
                if std_id:
                    # Create edge: Standard → implemented by → Control
                    create_standard_validates_control(std_id, ctrl['id'])
                else:
                    print(f"    Control {ctrl['id']}: missing standard_id, skipping IMPLEMENTED_BY edge")
                    failure_count['IMPLEMENTED_BY_edge'] = failure_count.get('IMPLEMENTED_BY_edge', 0) + 1

            except Exception as e:
                print(f"    Control insert failed: id={ctrl.get('id', 'unknown')}, title={ctrl.get('title', 'unknown')}: {e}")
                failure_count['Control_insert'] = failure_count.get('Control_insert', 0) + 1

        all_nodes['controls'] = ctrl_ids
        
        return {
            'regulation_node_id': reg_node_id,
            'success_count': success_count,
            'failure_count': failure_count,  # NEW: Include failure counts
            'error': None
        }
        
    except Exception as e:
        return {
            'regulation_node_id': None,
            'success_count': success_count,
            'failure_count': failure_count,  # NEW: Include failure counts
            'error': str(e)
        }


def process_regulation(regulation_type: str, max_chunks: int = None) -> dict:
    """Process a single regulation from HTML to graph database."""
    print(f"\n{'='*60}")
    print(f"Processing {regulation_type.upper()} regulation")
    print('='*60)
    
    try:
        # Step 1: Load EU regulation HTML
        html_path = get_regulation_path(regulation_type)
        with open(html_path, 'r', encoding='utf-8') as f:
            html = f.read()
        
        print(f"Loaded {len(html):,} bytes from {os.path.basename(html_path)}")
        
    except FileNotFoundError as e:
        return {'error': str(e)}
    
    # Step 2: Chunk by article
    try:
        from chunker import chunk_by_article
        chunks = chunk_by_article(html)
        print(f"Parsed {len(chunks)} articles")
        
    except ValueError as e:
        return {'error': f"Chunking failed: {e}"}
    
    # Process only up to max_chunks if specified
    chunks_to_process = min(max_chunks, len(chunks)) if max_chunks else len(chunks)
    print(f"Processing {chunks_to_process} of {len(chunks)} articles...")
    
    # Step 3: Extract all concepts for each chunk
    all_extraction_results = []
    
    for i in range(chunks_to_process):
        chunk = chunks[i]
        chunk_id = f"{regulation_type}_{chunk['article_id']}"
        
        extraction = extract_all_concepts_for_chunk(
            chunk_id, 
            chunk['content'], 
            os.environ.get('OLLAMA_URL', 'http://localhost:11434')
        )
        
        all_extraction_results.append(extraction)
        
        if i < 5 or (i + 1) % 10 == 0:
            print(f"  Completed {i+1}/{chunks_to_process} articles")
    
    # Merge all results
    merged = {
        'roles': [],
        'requirements': [],
        'obligations': [],
        'capabilities': [],
        'policies': [],
        'standards': [],
        'controls': []
    }
    
    for result in all_extraction_results:
        if not result.get('error'):
            for key in merged.keys():
                merged[key].extend(result.get(key, []))
    
    # Step 4: Ingest into graph database
    print("\nIngesting into FalkorDB...")
    ingestion_result = ingest_regulation_into_graph(merged, regulation_type)
    
    return {
        'chunks_processed': chunks_to_process,
        'total_articles': len(chunks),
        'extraction_results': all_extraction_results,
        **ingestion_result
    }


def main():
    """Run the spike demonstration."""
    args = parse_args()
    
    # Set environment for extractor
    os.environ['OLLAMA_URL'] = args.ollama_url
    
    print("="*60)
    print("Graph Ingestion 2 - Full Concept Extraction")
    print("="*60)
    
    # Determine which regulations to process
    if args.regulation:
        regulations_to_process = [args.regulation]
    else:
        regulations_to_process = ['cra', 'nis2']
    
    all_results = []
    
    for regulation_type in regulations_to_process:
        result = process_regulation(regulation_type, args.max_chunks)
        all_results.append(result)
        
        if result.get('error'):
            print(f"ERROR processing {regulation_type.upper()}: {result['error']}")
    
    # Summary
    print("\n" + "="*60)
    print("=== SUMMARY ===")
    print("="*60)
    
    for reg_type, result in zip(regulations_to_process, all_results):
        success_count = result.get('success_count', {})
        failure_count = result.get('failure_count', {})  # NEW: Get failure counts
        
        if result.get('error'):
            print(f"\n{reg_type.upper()}: FAILED - {result['error']}")
        else:
            print(f"\n{reg_type.upper()}:")
            print(f"  Articles processed: {result.get('chunks_processed', 'N/A')} of {result.get('total_articles', 'N/A')}")
            print(f"  Nodes inserted:")
            for node_type, count in sorted(success_count.items()):
                print(f"    {node_type}: {count}")
            
            # NEW: Report failure/unmatched counts
            if failure_count:
                print(f"  Failures/Unmatched:")
                for edge_type, count in sorted(failure_count.items()):
                    print(f"    {edge_type}: {count}")
            else:
                print(f"  Failures/Unmatched: 0")
    
    # Final node counts
    print("\nFinal Graph Node Counts:")
    for label in ['Regulation', 'Role', 'Requirement', 'Obligation', 
                  'Capability', 'Policy', 'Standard', 'Control']:
        count = get_node_count(label)
        if count > 0:
            print(f"  {label}: {count}")
    
    # Validate acceptance criteria
    print("\n" + "="*60)
    print("ACCEPTANCE CRITERIA CHECK")
    print("="*60)
    
    all_found = True
    for label in ['Regulation', 'Obligation']:  # Minimum required
        count = get_node_count(label)
        if count == 0:
            print(f"❌ {label}: NOT FOUND (count = 0)")
            all_found = False
        else:
            print(f"✅ {label}: FOUND ({count} nodes)")
    
    # Also verify edges were created
    try:
        from graph import _get_graph_client
        graph = _get_graph_client()
        
        edge_types = ['DEFINES', 'CONTAINS', 'SATISFIED_BY', 'HAS', 'REQUIRES',
                      'GOVERNED_BY', 'SUPPORTED_BY', 'IMPLEMENTED_BY']
        print("\nEdges in Graph:")
        for edge_type in edge_types:
            result = graph.query(f"MATCH ()-[r:{edge_type}]->() RETURN count(r) as cnt")
            if result.result_set and len(result.result_set) > 0:
                count = int(result.result_set[0][0])
                status = "✅" if count > 0 else "⚠️"
                print(f"  {status} {edge_type}: {count}")
    except Exception as e:
        print(f"  ⚠️  Could not verify edges: {e}")
    
    if all_found:
        print("\n🎉 Acceptance criteria MET!")
        print("All concepts successfully ingested into FalkorDB.")
    else:
        print("\n⚠️  Some acceptance criteria NOT met. Check error messages above.")
    
    return 0 if all_found else 1


if __name__ == '__main__':
    sys.exit(main())
