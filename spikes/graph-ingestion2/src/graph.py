#!/usr/bin/env python3
"""FalkorDB graph insertion component for all 8 domain concepts.

Integrates with FalkorDB (Redis Graph API) to insert nodes and edges.
Supports the complete compliance chain:
  Regulation → Requirement → Obligation → Capability → Policy → Standard → Control

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
    try:
        from redisgraph import Graph
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


def _insert_node(graph, label: str, properties: dict) -> int:
    """Insert a node with given label and properties.
    
    Uses MERGE for idempotent insertion.
    
    Args:
        graph: Graph client
        label: Node label (e.g., 'Regulation', 'Obligation')
        properties: Node properties
        
    Returns:
        Node ID on success
        
    Raises:
        RuntimeError if node creation fails
    """
    # Build property string for Cypher
    prop_string = ', '.join([
        f"{k}: ${k}" 
        for k in properties.keys()
    ])
    
    query = f"""
    MERGE (n:{label} {{id: $id}})
    ON CREATE SET n += $properties
    ON MATCH SET n.last_seen = timestamp()
    RETURN id(n) as node_id
    """
    
    params = {
        'id': properties.get('id'),
        'properties': {k: v for k, v in properties.items() if k != 'id'}
    }
    
    result = graph.query(query, params=params)
    
    if not result.result_set or len(result.result_set) == 0:
        raise RuntimeError(f"Failed to create node with label '{label}' - no result returned")
    
    return int(result.result_set[0][0])


def _create_edge(graph, from_label: str, to_label: str, 
                from_id: str, to_id: str, edge_type: str) -> dict:
    """Create an edge between two nodes.
    
    Uses MERGE for idempotent edge creation (safe to re-run pipeline).
    
    Args:
        graph: Graph client
        from_label: Source node label
        to_label: Target node label
        from_id: Source node ID (business ID)
        to_id: Target node ID (business ID)
        edge_type: Type of relationship (e.g., 'CONTAINS', 'FULFILLS')
        
    Returns:
        {'edge': True} on success
        
    Raises:
        RuntimeError if edge creation fails
    """
    query = f"""
    MATCH (from:{from_label} {{id: $from_id}}), (to:{to_label} {{id: $to_id}})
    MERGE (from)-[:{edge_type}]->(to)
    RETURN true as edge_created
    """
    
    params = {
        'from_id': from_id,
        'to_id': to_id
    }
    
    result = graph.query(query, params=params)
    
    if not result.result_set or len(result.result_set) == 0:
        raise RuntimeError(f"Failed to create {edge_type} edge - no result returned")
    
    return {'edge': True}


# ==================== INSERT FUNCTIONS FOR ALL CONCEPTS ====================

def insert_regulation_into_graph(regulation: dict) -> int:
    """Insert a regulation node into the graph database.
    
    Args:
        regulation: {
            'id': str,                    # e.g., 'CRA-1.0'
            'title': str,
            'jurisdiction': str (optional),
            'effective_date': str (optional),  # ISO 8601
            'version': str (optional),
            'status': str (optional)       # 'active'|'superseded'|'vacated'
        }
        
    Returns:
        Node ID on success
        
    Raises:
        ConnectionError if FalkorDB not accessible
        RuntimeError if node creation fails
    """
    graph = _get_graph_client()
    
    # Required fields
    node_id = _insert_node(graph, 'Regulation', {
        'id': regulation['id'],
        'title': regulation['title']
    })
    
    # Update with optional properties using SET
    if len(regulation) > 2:  # Has optional properties
        prop_updates = []
        for key in ['jurisdiction', 'effective_date', 'version', 'status']:
            if key in regulation:
                prop_updates.append(f"r.{key} = ${key}")
        
        if prop_updates:
            query = f"""
            MATCH (r:Regulation {{id: $id}})
            SET {', '.join(prop_updates)}
            RETURN id(r) as node_id
            """
            
            params = {'id': regulation['id']}
            for key in ['jurisdiction', 'effective_date', 'version', 'status']:
                if key in regulation:
                    params[key] = regulation[key]
            
            graph.query(query, params=params)
    
    return node_id


def insert_role_into_graph(role: dict) -> int:
    """Insert a role node into the graph database.
    
    Phase 10 fix: Persist source_ref for provenance traceability.
    
    Args:
        role: {
            'id': str,
            'name': str,
            'description': str (optional),
            'source_ref': str (optional),   # Article where defined
            'regulation_id': str            # Link to regulation
        }
        
    Returns:
        Node ID on success
        
    Raises:
        ConnectionError if FalkorDB not accessible
        RuntimeError if node creation fails
    """
    graph = _get_graph_client()

    props = {
        'id': role['id'],
        'name': role['name']
    }

    # Phase 10: Persist source_ref for provenance traceability (AC-10)
    if 'source_ref' in role:
        props['source_ref'] = role['source_ref']

    # Persist regulation_id so Role provenance doesn't depend solely on the
    # `defines` edge (was previously accepted by this function but dropped).
    if 'regulation_id' in role:
        props['regulation_id'] = role['regulation_id']

    return _insert_node(graph, 'Role', props)


def insert_requirement_into_graph(requirement: dict) -> int:
    """Insert a requirement node into the graph database.
    
    Args:
        requirement: {
            'id': str,
            'text': str,
            'source_ref': str,          # Article reference
            'type': str                 # 'requirement'|'prohibition'|'recommendation'
        }
        
    Returns:
        Node ID on success
        
    Raises:
        ConnectionError if FalkorDB not accessible
        RuntimeError if node creation fails
    """
    graph = _get_graph_client()
    
    return _insert_node(graph, 'Requirement', {
        'id': requirement['id'],
        'text': requirement['text'],
        'source_ref': requirement.get('source_ref', ''),
        'type': requirement.get('type', 'requirement')
    })


def insert_obligation_into_graph(obligation: dict) -> int:
    """Insert an obligation node into the graph database.
    
    Args:
        obligation: {
            'id': str,
            'text': str,
            'source_ref': str,          # Article reference
            'type': str,                # 'requirement'|'prohibition'|'recommendation'
            'confidence': float,        # 0.0-1.0 (default 0.95)
            'obligation_type': str      # optional: 'technical'|'organizational'
            'role_id': str             # NEW (AC-6): ID of role having this duty
        }
        
    Returns:
        Node ID on success
        
    Raises:
        ConnectionError if FalkorDB not accessible
        RuntimeError if node creation fails
    """
    graph = _get_graph_client()
    
    props = {
        'id': obligation['id'],
        'text': obligation['text'],
        'source_ref': obligation.get('source_ref', ''),
        'type': obligation.get('type', 'requirement')
    }
    
    if 'confidence' in obligation:
        props['confidence'] = float(obligation['confidence'])
    
    # NEW (AC-6): Persist role_id on Obligation node for HAS edge creation
    if 'role_id' in obligation:
        props['role_id'] = obligation['role_id']
    
    return _insert_node(graph, 'Obligation', props)


def insert_capability_into_graph(capability: dict) -> int:
    """Insert a capability node into the graph database.
    
    Args:
        capability: {
            'id': str,
            'name': str,
            'description': str (optional),
            'type': str,                # 'technical'|'organizational'
            'status': str (optional)    # 'active'|'deprecated'
            'related_obligation_ref': str (optional)  # NEW: Reference to related obligation
        }
        
    Returns:
        Node ID on success
        
    Raises:
        ConnectionError if FalkorDB not accessible
        RuntimeError if node creation fails
    """
    graph = _get_graph_client()
    
    props = {
        'id': capability['id'],
        'name': capability['name']
    }
    # NEW: Persist related_obligation_ref if present (for AC-3 edge matching)
    if 'related_obligation_ref' in capability:
        props['related_obligation_ref'] = capability['related_obligation_ref']
    
    return _insert_node(graph, 'Capability', props)


def insert_policy_into_graph(policy: dict) -> int:
    """Insert a policy node into the graph database.
    
    Args:
        policy: {
            'id': str,
            'title': str,
            'description': str (optional),
            'owner_id': str (optional),
            'status': str,              # 'draft'|'approved'|'deprecated'
            'version': str (optional)
        }
        
    Returns:
        Node ID on success
        
    Raises:
        ConnectionError if FalkorDB not accessible
        RuntimeError if node creation fails
    """
    graph = _get_graph_client()
    
    return _insert_node(graph, 'Policy', {
        'id': policy['id'],
        'title': policy['title'],
        'status': policy.get('status', 'approved')
    })


def insert_standard_into_graph(standard: dict) -> int:
    """Insert a standard node into the graph database.
    
    Args:
        standard: {
            'id': str,
            'title': str,
            'description': str (optional),
            'implementation_status': str,  # 'draft'|'implemented'|'reviewed'|'deprecated'
            'version': str (optional)
        }
        
    Returns:
        Node ID on success
        
    Raises:
        ConnectionError if FalkorDB not accessible
        RuntimeError if node creation fails
    """
    graph = _get_graph_client()
    
    return _insert_node(graph, 'Standard', {
        'id': standard['id'],
        'title': standard['title'],
        'implementation_status': standard.get('implementation_status', 'implemented')
    })


def insert_control_into_graph(control: dict) -> int:
    """Insert a control node into the graph database.
    
    Args:
        control: {
            'id': str,
            'type': str,                # 'automated'|'manual'
            'title': str,
            'description': str (optional),
            'implementation_status': str,  # 'planned'|'implemented'|'reviewed'|'deprecated'
            'execution_frequency': str (optional)
        }
        
    Returns:
        Node ID on success
        
    Raises:
        ConnectionError if FalkorDB not accessible
        RuntimeError if node creation fails
    """
    graph = _get_graph_client()
    
    return _insert_node(graph, 'Control', {
        'id': control['id'],
        'type': control.get('type', 'automated'),
        'title': control['title'],
        'implementation_status': control.get('implementation_status', 'implemented')
    })


# ==================== EDGE CREATION FUNCTIONS ====================

def create_regulation_contains_requirement(regulation_id: str, requirement_id: str) -> dict:
    """Create CONTAINS edge from regulation to requirement."""
    graph = _get_graph_client()
    return _create_edge(graph, 'Regulation', 'Requirement',
                       regulation_id, requirement_id, 'CONTAINS')


def create_regulation_defines_role(regulation_id: str, role_id: str) -> dict:
    """Create DEFINES edge from regulation to role.

    Was previously missing entirely: Role nodes had no edge back to their
    defining Regulation, leaving them unprovenanced per the domain model's
    Provenance & Traceability principle.
    """
    graph = _get_graph_client()
    return _create_edge(graph, 'Regulation', 'Role',
                       regulation_id, role_id, 'DEFINES')


def create_requirement_satisfied_by_obligation(requirement_id: str, obligation_id: str) -> dict:
    """Create SATISFIED_BY edge from requirement to obligation."""
    graph = _get_graph_client()
    return _create_edge(graph, 'Requirement', 'Obligation',
                       requirement_id, obligation_id, 'SATISFIED_BY')


def create_obligation_requires_capability(obligation_id: str, capability_id: str) -> dict:
    """Create REQUIRES edge from obligation to capability."""
    graph = _get_graph_client()
    return _create_edge(graph, 'Obligation', 'Capability',
                       obligation_id, capability_id, 'REQUIRES')


def create_capability_governed_by_policy(capability_id: str, policy_id: str) -> dict:
    """Create GOVERNED_BY edge from capability to policy."""
    graph = _get_graph_client()
    return _create_edge(graph, 'Capability', 'Policy',
                       capability_id, policy_id, 'GOVERNED_BY')


def create_policy_supported_by_standard(policy_id: str, standard_id: str) -> dict:
    """Create SUPPORTED_BY edge from policy to standard."""
    graph = _get_graph_client()
    return _create_edge(graph, 'Policy', 'Standard',
                       policy_id, standard_id, 'SUPPORTED_BY')


def create_role_has_obligation(role_id: str, obligation_id: str) -> dict:
    """Create Role → has → Obligation edge.
    
    Args:
        role_id: Business ID of Role node
        obligation_id: Business ID of Obligation node
        
    Returns:
        {'edge': True} on success
        
    Raises:
        RuntimeError if edge creation fails
    """
    graph = _get_graph_client()
    
    return _create_edge(graph, 'Role', 'Obligation',
                       role_id, obligation_id, 'HAS')


def create_standard_validates_control(standard_id: str, control_id: str) -> dict:
    """Create IMPLEMENTED_BY edge from standard to control.
    
    RENAMED FROM VALIDATES TO IMPLEMENTED_BY per AC-5 requirements.
    Both domain-concepts.md and the actual source of truth use IMPLEMENTED_BY.
    """
    graph = _get_graph_client()
    return _create_edge(graph, 'Standard', 'Control',
                       standard_id, control_id, 'IMPLEMENTED_BY')


# ==================== UTILITY FUNCTIONS ====================

def reset_graph() -> None:
    """Reset the entire graph database.
    
    WARNING: This deletes all data in the policy_system graph!
    """
    graph = _get_graph_client()
    graph.delete()


def get_node_count(label: str) -> int:
    """Get count of nodes with given label."""
    graph = _get_graph_client()
    result = graph.query(f"MATCH (n:{label}) RETURN count(*)")
    if result.result_set and len(result.result_set) > 0:
        return int(result.result_set[0][0])
    return 0


def get_all_nodes(label: str) -> list:
    """Get all nodes with given label."""
    graph = _get_graph_client()
    result = graph.query(f"MATCH (n:{label}) RETURN n")
    
    nodes = []
    if result.result_set:
        for row in result.result_set:
            node = row[0]
            nodes.append({
                'id': node.id,
                'labels': list(node.labels),
                'properties': dict(node.properties)
            })
    
    return nodes


def get_relationships(start_id: str, end_id: str, edge_type: str) -> list:
    """Get relationships between two nodes of specific type."""
    graph = _get_graph_client()
    result = graph.query(f"""
        MATCH (a {{id: $start}})-[r:{edge_type}]->(b {{id: $end}})
        RETURN r
    """, params={'start': start_id, 'end': end_id})
    
    relationships = []
    if result.result_set:
        for row in result.result_set:
            rel = row[0]
            relationships.append({
                'id': rel.id,
                'type': rel.type,
                'properties': dict(rel.properties)
            })
    
    return relationships


if __name__ == '__main__':
    # Test basic functionality
    print("Testing Graph Integration...")
    
    try:
        graph = _get_graph_client()
        
        # Insert a test regulation
        reg_id = insert_regulation_into_graph({
            'id': 'TEST-1.0',
            'title': 'Test Regulation'
        })
        print(f"Inserted regulation with ID: {reg_id}")
        
        # Check node counts
        print(f"Regulations in graph: {get_node_count('Regulation')}")
        print(f"Total nodes:\n{get_all_nodes('Regulation')}")
        
    except ConnectionError as e:
        print(f"FalkorDB connection failed: {e}")
    except Exception as e:
        print(f"Error: {e}")
