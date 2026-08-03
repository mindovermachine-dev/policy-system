#!/usr/bin/env python3
"""In-memory graph implementation for testing.

This provides a mock graph client that simulates FalkorDB behavior
without requiring an actual database connection.
"""

import hashlib


class TestGraph:
    """Mock graph client for testing."""
    
    def __init__(self):
        self.nodes = {}  # id -> {labels, properties}
        self.edges = []  # [(from_id, to_id, relationship_type)]
        self.current_node_id = 0
        
    def query(self, query: str, params: dict = None) -> 'TestResult':
        """Execute a Cypher-like query.
        
        Supports:
        - CREATE (n:Label {props})
        - MATCH (...), (...) WHERE id(n) = $var AND id(m) = $var CREATE (n)-[:REL]->(m)
        - RETURN ... as alias
        """
        result = TestResult()
        
        if 'MATCH' in query and 'CREATE' in query:
            self._execute_match_create(query, params, result)
        elif 'CREATE' in query:
            self._execute_create(query, params, result)
        else:
            raise NotImplementedError(f"Query type not supported: {query}")
        
        return result
    
    def _execute_create(self, query: str, params: dict, result: 'TestResult'):
        """Parse CREATE (n:Label {props}) and execute."""
        # Extract node definition from query
        import re
        
        # Match pattern like: CREATE (r:Regulation {id: $id, title: $title})
        match = re.search(r'CREATE\s+\((\w+):(\w+)\s*\{([^}]+)\}\)', query)
        
        if not match:
            raise ValueError(f"Could not parse CREATE statement: {query}")
        
        var_name = match.group(1)
        label = match.group(2)
        props_str = match.group(3)
        
        # Parse properties
        props = {}
        for prop_match in re.finditer(r'(\w+):\s*\$(\w+)', props_str):
            key = prop_match.group(1)
            param_name = prop_match.group(2)
            
            if params and param_name in params:
                props[key] = params[param_name]
        
        # Create node with unique ID
        self.current_node_id += 1
        
        self.nodes[self.current_node_id] = {
            'labels': [label],
            'properties': props,
            'var_name': var_name
        }
        
        # Set result for RETURN id(n) as node_id
        if 'RETURN' in query and 'id(' in query:
            result.result_set = [[self.current_node_id]]
    
    def _execute_match_create(self, query: str, params: dict, result: 'TestResult'):
        """Parse MATCH ... CREATE ... pattern."""
        import re
        
        # Extract WHERE clause conditions
        where_match = re.search(r'WHERE\s+(.+)', query)
        
        if not where_match:
            raise ValueError(f"Could not parse WHERE clause: {query}")
        
        where_clause = where_match.group(1)
        
        # Find node IDs from parameters
        regulation_id = None
        obligation_id = None
        
        if params:
            for key, value in params.items():
                if 'regulation' in key.lower():
                    regulation_id = value
                elif 'obligation' in key.lower():
                    obligation_id = value
        
        # Verify nodes exist
        if regulation_id not in self.nodes:
            raise RuntimeError(f"Regulation node {regulation_id} not found")
        
        if obligation_id not in self.nodes:
            raise RuntimeError(f"Obligation node {obligation_id} not found")
        
        # Create edge
        edge_type_match = re.search(r'CREATE\s+\([^)]+\)-\[:([^\]]+)\]->\(', query)
        
        if edge_type_match:
            rel_type = edge_type_match.group(1)
            self.edges.append({
                'from': regulation_id,
                'to': obligation_id,
                'type': rel_type
            })
        
        # Set result
        if 'RETURN' in query and 'true' in query:
            result.result_set = [[True]]


class TestResult:
    """Mock result object."""
    
    def __init__(self):
        self.result_set = []
