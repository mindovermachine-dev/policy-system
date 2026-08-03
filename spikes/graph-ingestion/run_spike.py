#!/usr/bin/env python3
"""
Spike Runner: End-to-End Regulation Ingestion Pipeline

This script demonstrates the complete pipeline:
  EU regulation HTML → chunk_by_article() → extract_obligations() → insert into FalkorDB

Usage:
    ./run_spike.py                    # Process first 5 articles of CRA
    ./run_spike.py --max-chunks 10    # Process more articles (default regulation)
    ./run_spike.py --regulation nis2  # Try NIS2 instead of CRA
    ./run_spike.py --all              # Ingest both CRA and NIS2 regulations
"""

import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from chunker import chunk_by_article
from extractor import extract_obligations


def reset_graph():
    """Reset graph database to clean state."""
    from redisgraph import Graph
    
    graph = Graph('policy_system', host='localhost', port=6379)
    
    reg_count = graph.query("MATCH (r:Regulation) RETURN count(*)").result_set[0][0]
    obl_count = graph.query("MATCH (o:Obligation) RETURN count(*)").result_set[0][0]
    
    if reg_count > 0 or obl_count > 0:
        print(f"Clearing database: {reg_count} regulations, {obl_count} obligations")
        result = graph.delete()
        print("Database cleared")


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Run the graph ingestion spike with real EU regulations.'
    )
    
    parser.add_argument(
        '--max-chunks',
        type=int,
        default=None,  # None means process all articles
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
        default='cra',
        choices=['cra', 'nis2'],
        help='Which regulation to process: cra (Cyber Resilience Act) or nis2 (Network and Information Security Directive)'
    )
    
    parser.add_argument(
        '--all',
        action='store_true',
        help='Ingest both CRA and NIS2 regulations'
    )
    
    return parser.parse_args()


def get_regulation_path(regulation_type: str) -> str:
    """Get path to EU regulation HTML file."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    if regulation_type == 'cra':
        filepath = os.path.join(script_dir, 'eu-cra', 'L_202402847EN.000101.fmx.xml.html')
    else:  # nis2
        filepath = os.path.join(script_dir, 'eu-nis2', 'L_2022333EN.01008001.xml.html')
    
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Regulation file not found: {filepath}")
    
    return filepath


def insert_regulation_into_graph(regulation: dict) -> int:
    """Insert a regulation node into the graph database."""
    from redisgraph import Graph
    
    graph = Graph('policy_system', host='localhost', port=6379)
    
    query = """
    MERGE (r:Regulation {id: $id})
    ON CREATE SET r += $properties
    ON MATCH SET r.last_seen = timestamp()
    RETURN id(r) as node_id
    """
    
    params = {
        'id': regulation['id'],
        'properties': {
            'title': regulation['title'],
            'jurisdiction': regulation.get('jurisdiction', '')
        }
    }
    
    result = graph.query(query, params=params)
    return int(result.result_set[0][0])


def insert_obligation_into_graph(obligation: dict) -> int:
    """Insert an obligation node into the graph database."""
    from redisgraph import Graph
    
    graph = Graph('policy_system', host='localhost', port=6379)
    
    query = """
    MERGE (o:Obligation {id: $id})
    ON CREATE SET o += $properties
    ON MATCH SET o.last_seen = timestamp()
    RETURN id(o) as node_id
    """
    
    params = {
        'id': obligation['id'],
        'properties': {
            'type': obligation['type'],
            'text': obligation['text'],
            'confidence': float(obligation.get('confidence', 0)),
            'source_ref': obligation.get('source_ref', '')
        }
    }
    
    result = graph.query(query, params=params)
    return int(result.result_set[0][0])


def create_contains_edge(regulation_node_id: int, obligation_node_id: int):
    """Create 'contains' edge from regulation to obligation."""
    from redisgraph import Graph
    
    graph = Graph('policy_system', host='localhost', port=6379)
    
    query = """
    MATCH (r:Regulation), (o:Obligation)
    WHERE id(r) = $regulation_id AND id(o) = $obligation_id
    CREATE (r)-[:contains]->(o)
    RETURN true as edge_created
    """
    
    params = {
        'regulation_id': int(regulation_node_id),
        'obligation_id': int(obligation_node_id)
    }
    
    graph.query(query, params=params)


def main():
    """Run the spike demonstration."""
    args = parse_args()
    
    # Set environment for extractor
    os.environ['OLLAMA_URL'] = args.ollama_url
    
    print("Starting run")
    
    # Determine which regulations to process
    if args.all:
        regulations_to_process = ['cra', 'nis2']
    else:
        regulations_to_process = [args.regulation]
    
    all_regulations_data = []
    
    for regulation_type in regulations_to_process:
        print()  # Empty line between regulations
        
        # Step 1: Load EU regulation HTML
        try:
            html_path = get_regulation_path(regulation_type)
            with open(html_path, 'r', encoding='utf-8') as f:
                html = f.read()
            print(f"Initiating regulation {regulation_type.upper()}")
            print(f"Loaded {len(html):,} bytes from {os.path.basename(html_path)}")
        except FileNotFoundError as e:
            print(f"Error: {e}")
            continue  # Skip this regulation and try the next
        
        # Step 2: Chunk by article
        try:
            chunks = chunk_by_article(html)
            print(f"Parsed {len(chunks)} articles from regulation")
        except ValueError as e:
            print(f"Error parsing HTML: {e}")
            continue  # Skip this regulation and try the next
        
        # Step 3: Extract obligations from each chunk
        all_obligations = []
        # Default to processing all articles if --max-chunks not specified
        max_chunks = args.max_chunks if args.max_chunks is not None else len(chunks)
        chunks_to_process = min(max_chunks, len(chunks))
        
        for i in range(chunks_to_process):
            chunk = chunks[i]
            print(f"Extracting obligation from article {i+1}/{chunks_to_process} ({chunk['article_id']})...")
            
            result = extract_obligations(chunk['content'])
            obligations = result.get('obligations', [])
            
            if len(obligations) == 0:
                print(f"  No obligations extracted from this article")
            else:
                for obl in obligations:
                    obl['source_ref'] = chunk['article_id']
                    all_obligations.append(obl)
                
                # Print first obligation as sample
                sample = obligations[0]
                print(f"  Extracted {len(obligations)} obligations")
                print(f"    Sample: [{sample.get('type', 'unknown')}] {sample.get('text', '')[:60]}...")
        
        all_regulations_data.append({
            'type': regulation_type,
            'chunks_to_process': chunks_to_process,
            'total_articles': len(chunks),
            'obligations': all_obligations
        })
    
    print()  # Empty line before graph insertion
    
    # Step 4: Insert into graph database (only after all regulations processed)
    if all_regulations_data:
        try:
            reset_graph()
            total_successful_inserts = 0
            total_obligations = 0
            
            for reg_data in all_regulations_data:
                regulation_type = reg_data['type']
                all_obligations = reg_data['obligations']
                
                regulation = {
                    'id': f'{regulation_type.upper()}-1.0',
                    'title': 'Cyber Resilience Act' if regulation_type == 'cra' else 'Network and Information Security Directive',
                    'jurisdiction': 'EU'
                }
                
                reg_node_id = insert_regulation_into_graph(regulation)
                print(f"Regulation node created: {regulation['id']} (ID: {reg_node_id})")
                
                # Insert obligations for this regulation
                successful_inserts = 0
                for obl in all_obligations:
                    source_ref_clean = obl.get('source_ref', 'unknown').replace('art_', '')
                    text_hash = hashlib.md5(obl['text'][:50].encode()).hexdigest()[:8]
                    obl_id = f"{regulation_type.upper()}_{source_ref_clean}_{text_hash}"
                    
                    obl['id'] = obl_id
                    
                    try:
                        obl_node_id = insert_obligation_into_graph(obl)
                        create_contains_edge(reg_node_id, obl_node_id)
                        print(f"  Inserted obligation {successful_inserts+1}: [{obl.get('type', 'unknown')}] {obl.get('text', '')[:50]}...")
                        successful_inserts += 1
                    except Exception as e:
                        print(f"  Failed: {str(e)[:80]}")
                
                total_successful_inserts += successful_inserts
                total_obligations += len(all_obligations)
            
            # Final summary
            print()
            print("=== Summary ===")
            for reg_data in all_regulations_data:
                reg_type = reg_data['type']
                print(f"{reg_type.upper()}: {reg_data['chunks_to_process']} of {reg_data['total_articles']} articles, {len(reg_data['obligations'])} obligations extracted")
            print()
            print(f"Total obligations inserted into graph: {total_successful_inserts}")
            print()
            print("Spike complete!")
            
        except ConnectionError as e:
            print(f"\nGraph database connection failed: {e}")
            sys.exit(1)
    else:
        print("No regulations were successfully processed.")
        sys.exit(1)


if __name__ == '__main__':
    main()
