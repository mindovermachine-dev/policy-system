# 🚀 Graph Ingestion Spike - Quick Start

## Run the Spike (End-to-End)

```bash
cd docs/spikes/graph-ingestion

pip install -r requirements.txt

python run_spike.py     # Process first 5 articles of CRA
```

### That's it. Done.

The spike demonstrates:
1. ✅ Load EU regulation HTML (CRA or NIS2)
2. ✅ Parse into articles using article boundaries
3. ✅ Extract obligations from first 5 articles using Ollama
4. ✅ Insert nodes and edges into FalkorDB

## Process Both Regulations

```bash
python run_spike.py --all    # Ingest both CRA and NIS2 regulations
```

This will:
- Process first 5 articles from CRA
- Then process first 5 articles from NIS2
- Clear database between runs
- Show combined summary

---

## Prerequisites Checklist

Before running, verify:

```bash
# 1. Python dependencies installed
pip list | grep -E "beautifulsoup4|redisgraph"

# 2. Ollama running with correct model
curl http://localhost:11434/api/tags
# Should include qwen3-coder-next:q8_0

# 3. FalkorDB (optional - for graph integration)
redis-cli PING
# Should return "PONG"

redis-cli MODULE LIST | grep graph
# Should show "graph" in output
```

---

## Configure (Optional)

```bash
# Process more/fewer articles
python run_spike.py --max-chunks 3    # Faster demo

# Try NIS2 regulation instead of CRA
python run_spike.py --regulation nis2 --max-chunks 5

# Use custom Ollama URL
OLLAMA_URL=http://ollama.internal:11434 python run_spike.py
```

---

## Troubleshooting

### "Cannot connect to Ollama"
```bash
curl http://localhost:11434/api/tags  # Verify it's running
ollama pull qwen3-coder-next:q8_0   # Pull required model
```

### "FalkorDB connection failed"
```bash
podman run --rm -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
sleep 30
redis-cli MODULE LIST | grep graph
```

---

## Files Reference

| File | Purpose |
|------|---------|
| `run_spike.py` | ✅ Run the spike (end-to-end demo) |
| `HOW-TESTED.md` | ⚙️ Unit tests for component verification |
| `CLI.md` | 📋 Command-line options reference |

---

## Exit Code

- `0` = Success
- `1` = Error (missing Ollama/FalkorDB/file not found)
