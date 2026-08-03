# Graph Ingestion Spike - Command Line Interface

**Overview**: This spike provides a CLI to demonstrate end-to-end regulation ingestion from EU regulations into a graph database.

---

## Usage

```bash
cd docs/spikes/graph-ingestion

python run_spike.py [OPTIONS]
```

---

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `--max-chunks N` | 5 | Maximum number of article chunks to process (for faster demo) |
| `--ollama-url URL` | `http://localhost:11434` | Ollama API endpoint |
| `--regulation TYPE` | cra | Which regulation to process: `cra` or `nis2` |

---

## Examples

### Quick Demo (5 articles, CRA)
```bash
python run_spike.py
```

### Fast Demo (2 articles only)
```bash
python run_spike.py --max-chunks 2
```

### Process NIS2 Regulation
```bash
python run_spike.py --regulation nis2 --max-chunks 3
```

### Custom Ollama URL
```bash
OLLAMA_URL=http://ollama.internal:11434 python run_spike.py --max-chunks 5
```

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success - pipeline completed with data inserted |
| 1 | Error - missing dependencies (file not found, Ollama/FalkorDB unavailable) |

---

## Prerequisites Check

Before running the spike, verify these services are available:

```bash
# 1. Ollama
curl http://localhost:11434/api/tags | grep qwen3-coder-next

# Should return JSON with model name

# 2. FalkorDB (if you want graph integration)
redis-cli PING

# Should return "PONG"

# Verify GRAPH module loaded:
redis-cli MODULE LIST | grep graph
```

---

## What Happens When You Run It?

1. **Load** - Reads EU regulation HTML from `eu-cra/` or `eu-nis2/` directory
2. **Chunk** - Parses article structure using BeautifulSoup4 (no external API)
3. **Extract** - Calls Ollama for each article to extract obligations
4. **Insert** - Writes nodes and edges to FalkorDB graph database

The script shows progress, timing, and results in human-readable output.

---

## Sample Output

```
======================================================================
  Graph Ingestion Spike - Starting
======================================================================

Configuration:
  Regulation: CRA
  Max chunks to process: 3
  Ollama URL: http://localhost:11434

======================================================================
  Step 1: Loading Regulation
======================================================================

✅ Loaded L_202402847EN.000101.fmx.xml.html (767,807 bytes)

======================================================================
  Step 2: Chunks by Article
======================================================================

✅ Chunked into 71 articles in 0.15s

Processing 3 of 71 articles...

[1/3] Processing art_Article 1...
  ✅ Extracted 3 obligations in 24.3s

[2/3] Processing art_Article 2...
  ✅ Extracted 4 obligations in 25.1s

[3/3] Processing art_Article 3...
  ✅ Extracted 2 obligations in 23.8s

======================================================================
  Step 4: Graph Database Insertion
======================================================================

✅ Created regulation node with ID: 12345
✅ Inserted 9/9 obligations successfully

======================================================================
  Spike Complete!
======================================================================

Pipeline Execution Summary:
  Regulation: CRA
  Articles processed: 3 of 71
  Objections extracted: 9
  Objects inserted into graph: 9

🎉 Spike demonstration completed successfully!
```

---

## Integration with CI/CD

This spike is designed to be **run once** for demonstration/validation, not as part of automated tests. For CI/CD, use the unit tests instead:

```bash
# CI test step (no external services required)
pytest test/test_real_eu_cra_chunking.py -v
pytest test/test_extractor.py -v --ignore=test/test_graph_integration.py

# Spike is run manually by developers for validation
python run_spike.py --max-chunks 2
```

---

## Troubleshooting

### "Cannot connect to Ollama"

**Fix**:
```bash
curl http://localhost:11434/api/tags   # Verify Ollama running
ollama pull qwen3-coder-next:q8_0    # Pull required model
```

### "FalkorDB connection failed"

**Fix**:
```bash
podman run --rm -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
sleep 30
redis-cli MODULE LIST | grep graph
```

### No obligations extracted

**Potential causes**:
- Ollama timeout (reduce `--max-chunks` or use faster model)
- Model not loaded (`ollama list`)
- HTML parsing issue (check EU regulation file exists)

---

## Extending the Spike

To process all articles instead of `--max-chunks`:

```python
# In run_spike.py, change:
chunks_to_process = min(args.max_chunks, len(chunks))

# To:
chunks_to_process = len(chunks)  # Process ALL articles
```

Note: CRA has 71 articles, NIS2 has 46 - processing all may take several minutes.