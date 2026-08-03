#!/usr/bin/env python3
"""FalkorDB graph insertion component.

Integrates with FalkorDB (Redis Graph API) to insert nodes and edges.
Requires:
- FalkorDB running on localhost:6379 with GRAPH.QUERY command available

This is a SPIKE - technology test, not production code.
"""

import os


def _get_graph_client():
    """Get FalkorDB client connected to real database.
    
    Returns:
        Graph object for executing commands
        
    Raises:
        ConnectionError if connection fails (no fallback)
    """
    from redisgraph import Graph
    
    try:
        # Initialize with host/port directly
        graph = Graph('policy_system', host='localhost', port=6379)
        
        # Verify GRAPH.QUERY command is available
        result = graph.query("RETURN 1 as test")
        if not result.result_set or result.result_set[0][0] != 1:
            raise RuntimeError(f"GRAPH.QUERY command returned unexpected result: {result.result_set}")
        
        return graph
    except Exception as e:
        raise ConnectionError(
            f"FalkorDB connection failed at localhost:6379. "
            f"Is FalkorDB running with GRAPH module? Error: {e}"
        ) from e


def insert_regulation_into_graph(regulation: dict) -> dict:
    """Insert a regulation node into the graph database.
    
    Uses MERGE for idempotent insertion - will update existing nodes or create new ones.
    
    Args:
        regulation: {
            'id': str,
            'title': str,
            'jurisdiction': str
        }
        
    Returns:
        {'node_id': int} on success
        
    Raises:
        ConnectionError if FalkorDB not accessible
        RuntimeError if node creation fails
    """
    graph = _get_graph_client()
    
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
    
    if not result.result_set or len(result.result_set) == 0:
        raise RuntimeError("Failed to create regulation node - no result returned")
    
    return {'node_id': result.result_set[0][0]}


def insert_obligation_into_graph(obligation: dict) -> dict:
    """Insert an obligation node into the graph database.
    
    Args:
        obligation: {
            'id': str,
            'type': str,         # 'requirement'/'prohibition'/'recommendation'
            'text': str,
            'confidence': float,
            'source_ref': str
        }
        
    Returns:
        {'node_id': int} on success
        
    Raises:
        ConnectionError if FalkorDB not accessible
        RuntimeError if node creation fails
    """
    graph = _get_graph_client()
    
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
            'confidence': float(obligation['confidence']),
            'source_ref': obligation.get('source_ref', '')
        }
    }
    
    result = graph.query(query, params=params)
    
    if not result.result_set or len(result.result_set) == 0:
        raise RuntimeError("Failed to create obligation node - no result returned")
    
    return {'node_id': result.result_set[0][0]}


def create_contains_edge(regulation_node_id: int, obligation_node_id: int) -> dict:
    """Create 'contains' edge from regulation to obligation.
    
    Args:
        regulation_node_id: Node ID of the regulation
        obligation_node_id: Node ID of the obligation
        
    Returns:
        {'edge': True} on success
        
    Raises:
        ConnectionError if FalkorDB not accessible
        RuntimeError if edge creation fails
    """
    graph = _get_graph_client()
    
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
    
    result = graph.query(query, params=params)
    
    if not result.result_set or len(result.result_set) == 0:
        raise RuntimeError("Failed to create contains edge - no result returned")
    
    edge_created = result.result_set[0][0]
    
    if edge_created is not True and edge_created != "true":
        raise RuntimeError(f"Unexpected result from edge creation: {edge_created}")
    
    return {'edge': True}
