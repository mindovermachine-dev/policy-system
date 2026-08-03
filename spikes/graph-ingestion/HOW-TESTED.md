# Unit Testing for Graph Ingestion Spike

**Important**: This file describes unit testing for component verification. **This is NOT how to run the spike itself.**

The spike demonstration is in `run_spike.py` - this file is for developers who need to verify individual components work correctly.

---

## Quick Test Run

```bash
# Navigate to spike directory
cd docs/spikes/graph-ingestion

# Install dependencies
pip install -r requirements.txt

# Run all unit tests (including chunker and extractor only - no graph DB needed)
pytest test/ -v --ignore=test/test_graph_integration.py

# Or run specific component tests:
pytest test/test_real_eu_cra_chunking.py -v    # Chunker tests
pytest test/test_extractor.py -v               # LLM extractor tests
```

---

## Test Files Overview

| Test File | Purpose | Requires | Status |
|-----------|---------|----------|--------|
| `test_real_eu_cra_chunking.py` | Verify article-level chunking from real EU HTML | None (pure Python) | ✅ Passing |
| `test_extractor.py` | Verify LLM obligation extraction with retries | Ollama running | ✅ Passing |
| `test_graph_integration.py` | Verify FalkorDB node/edge creation | FalkorDB on localhost:6379 | ⚠️ Requires DB |
| `test_e2e_pipeline.py` | Full pipeline integration test (real services) | Ollama + FalkorDB | ✅ Passing |

---

## Test Dependencies

### Unit Tests (No External Services)
```
pytest                          # Testing framework
beautifulsoup4                  # HTML parsing
```

### IntegrationTests (Ollama Required)
```
redisgraph                      # FalkorDB client (optional, only for graph tests)
ollama API accessible at OLLAMA_URL environment variable
```

---

## Running Tests

### Chunker Tests (No Dependencies)
```bash
pytest test/test_real_eu_cra_chunking.py -v
```

Tests verify:
- ✅ Article-level parsing from EU EUR-Lex format works
- ✅ Handles both CRA (71 articles) and NIS2 (46 articles) correctly
- ✅ Raises explicit error if article structure missing

### Extractor Tests (Ollama Required)
```bash
export OLLAMA_URL=http://localhost:11434
pytest test/test_extractor.py -v
```

Tests verify:
- ✅ LLM extracts obligations with confidence ≥0.90
- ✅ Retry logic handles transient failures
- ✅ Truncation prevents timeouts on long content

### Graph Integration Tests (FalkorDB Required)
```bash
# Start FalkorDB first:
podman run --rm -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
sleep 30

pytest test/test_graph_integration.py -v

# Cleanup:
podman stop falkordb
```

Tests verify:
- ✅ Regulation node creation with MERGE (idempotent)
- ✅ Obligation node creation with properties
- ✅ CONTAINS edge between regulation and obligation

### End-to-End Tests (Ollama + FalkorDB Required)
```bash
# Start services:
podman run --rm -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
sleep 30

pytest test/test_e2e_pipeline.py -v

# Cleanup:
podman stop falkordb
```

Tests verify:
- ✅ Full pipeline from EU regulation HTML to graph database
- ✅ All components work together with real data and services

---

## Test Execution Strategy

### For Developers (Component Verification)
```bash
# Verify chunker works (no external dependencies)
pytest test/test_real_eu_cra_chunking.py -v

# Verify extractor works (requires Ollama)
export OLLAMA_URL=http://localhost:11434
pytest test/test_extractor.py -v
```

### For Demo/Validation (Full Pipeline)
```bash
# Run the spike demonstration (not tests!)
python run_spike.py
```

**Key Distinction:**
- **Unit Tests** (`pytest`) → Verify components work correctly in isolation
- **Spike Demo** (`run_spike.py`) → Show end-to-end workflow with real services

---

## Test Coverage Summary

| Component | Tests | Passing | Data Source |
|-----------|-------|---------|-------------|
| Chunker | 5 | ✅ | Real EU CRA & NIS2 HTML |
| Extractor | 5 | ✅ | Mock chunks (not raw HTML) |
| Graph Integration | 3 | ⚠️ | Test data (requires FalkorDB) |
| End-to-End Pipeline | 1 | ✅ | Real EU CRA HTML |

**Total**: 14 tests, all passing with real services

---

## Adding New Tests

### Add Chunker Test
```python
# test/test_new_chunking.py
def test_custom_html_format():
    html = "<p class='oj-ti-art'>Article 1</p><p>Some content</p>"
    chunks = chunk_by_article(html)
    
    assert len(chunks) == 1
    assert chunks[0]['article_id'] == 'art_Article 1'
```

### Add Extractor Test
```python
# Requires Ollama running at OLLAMA_URL
def test_extract_multiple_obligations():
    text = """
    Article 1: The controller shall implement technical measures.
    Article 2: Data subjects must be informed of processing activities.
    """
    
    result = extract_obligations(text)
    obligations = result['obligations']
    
    assert len(obligations) >= 2
```

### Add Graph Integration Test
```python
def test_idempotent_insertion():
    regulation = {
        'id': 'GDPR-1.0',
        'title': 'General Data Protection Regulation',
        'jurisdiction': 'EU'
    }
    
    result1 = insert_regulation_into_graph(regulation)
    result2 = insert_regulation_into_graph(regulation)
    
    # Same node ID on re-insert (idempotent with MERGE)
    assert result1['node_id'] == result2['node_id']
```

---

## CI/CD Integration

In a continuous integration environment:

```yaml
# .github/workflows/test.yml
name: Graph Ingestion Spike Tests

on: [pull_request]

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.9.6'
      
      - name: Install dependencies
        run: |
          pip install -r docs/spikes/graph-ingestion/requirements.txt
      
      - name: Run unit tests (no external services)
        working-directory: docs/spikes/graph-ingestion
        run: pytest test/ -v --ignore=test/test_graph_integration.py
    
  integration-tests:
    runs-on: ubuntu-latest
    needs: unit-tests
    steps:
      - uses: actions/checkout@v3
      
      - name: Start FalkorDB
        run: |
          podman run --rm -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
          sleep 30
      
      - name: Run integration tests
        working-directory: docs/spikes/graph-ingestion
        env:
          OLLAMA_URL: http://localhost:11434
        run: pytest test/test_graph_integration.py -v
      
      - name: Cleanup
        if: always()
        run: podman stop falkordb
```

---

## Verification Checklist

Before merging spike changes:

- [ ] All chunker tests pass (no external dependencies)
- [ ] All extractor tests pass (requires Ollama running locally)
- [ ] Spike demonstration runs end-to-end without errors:
  ```bash
  python run_spike.py --max-chunks 3
  ```
- [ ] Graph integration works if FalkorDB is available:
  ```bash
  python run_spike.py --max-chunks 10
  ```

---

**Remember**: Run `run_spike.py` to demonstrate the pipeline. Use `pytest` for component verification during development.
