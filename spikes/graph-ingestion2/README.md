# Spike: Full Concept Ingestion into Graph Database

---

## 🚀 Run the Spike (End-to-End Demonstration)

**Run this to demonstrate ingestion of ALL domain concepts from EU regulations into FalkorDB.**

### Prerequisites

1. **Python 3.9+**
2. **Ollama** running locally with `qwen3-coder-next:q8_0` model
3. **FalkorDB** running on localhost:6379 (optional - for graph integration)

### Quick Start

```bash
cd docs/spikes/graph-ingestion2

# Install dependencies
pip install -r requirements.txt

# Verify Ollama is available
curl http://localhost:11434/api/tags | grep qwen3-coder-next

# If model not present, pull it:
ollama pull qwen3-coder-next:q8_0

# Start FalkorDB (optional - for graph integration)
podman run --rm -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
sleep 30
redis-cli MODULE_LIST | grep graph

# Run the spike!
python run-spike.py
```

### Command Line Options

```bash
usage: python run-spike.py [-h] [--max-chunks N] [--ollama-url URL] [--regulation TYPE]
                           [--all]

Options:
  --max-chunks N        Max articles to process per regulation (default: all)
  --ollama-url URL      Ollama endpoint (default: http://localhost:11434)
  --regulation TYPE     cra or nis2 (default: both when using --all)
  --all                 Ingest both CRA and NIS2 regulations
```

**Examples:**
```bash
python run-spike.py                    # Process ALL articles from both regulations
python run-spike.py --max-chunks 5     # Process first 5 articles only (fast demo)
python run-spike.py --regulation nis2  # Try NIS2 regulation only
python run-spike.py --all              # Ingest both regulations (default behavior)
```

---

## 📚 Documentation

| File | Purpose |
|------|---------|
| **README.md** | This file - spike overview and how-to-run |
| `run-spike.py` | ✅ Execute the end-to-end pipeline demonstration |
| [`graph-ingestion-acceptance-criteria.md`](graph-ingestion-acceptance-criteria.md) | ✅ Acceptance criteria — baseline scope plus everything learned from running the spike against live data |
| [`docs/framework/policy-system-domain-concepts.md`](../../framework/policy-system-domain-concepts.md) | 💾 Canonical domain model and schema for all 8 domain concepts (data-model.md was merged into this doc and removed) |

---

## ✅ Acceptance Criteria

Moved to [`graph-ingestion-acceptance-criteria.md`](graph-ingestion-acceptance-criteria.md) — the spike is about learning,
so every acceptance criterion (the original baseline scope and everything
learned from actually running it against live data) lives in one place
rather than being split between this README and a separate fix doc.

---

## 🔍 How It Works

```
EU Regulation HTML
       │
       ▼
┌──────────────┐
│ chunker      │ Extract articles from EU EUR-Lex format
└──────┬───────┘
       │ List of chunks: [{article_id, content}]
       ▼
┌──────────────┐
│ extractor    │ Call Ollama API for each chunk (multi-stage extraction)
└──────┬───────┘
       │ {
         roles,
         requirements, 
         obligations,
         capabilities
       }
       ▼
┌──────────────┐
│ transformer  │ Generate policies, standards, controls from extracted data
└──────┬───────┘
       │ Complete concept set with relationships
       ▼
┌──────────────┐
│ graph.py     │ Insert to FalkorDB (all nodes + all edges)
└──────┬───────┘
       │ Graph nodes + relationships
```

---

## 📝 Files

```
docs/spikes/graph-ingestion2/
├── README.md                                    # This file - spike overview
├── run-spike.py                                 # ✨ EXECUTE THE SPIKE HERE (end-to-end demo)
├── graph-ingestion-acceptance-criteria.md       # ✅ Acceptance criteria — baseline scope plus everything learned from running the spike against live data
└── src/                                         # Pipeline components
    ├── chunker.py                               # Article-level HTML parsing (copy from spike 1)
    ├── extractor.py                             # LLM multi-stage extraction with Ollama
    ├── transformer.py                           # Generate policies, standards, controls from extracted data
    └── graph.py                                 # FalkorDB integration for all concepts and relationships
```

---

## 🎯 Spike Goals

- ✅ Ingest ALL 8 domain concepts into graph database
- ✅ Create comprehensive knowledge graph with all relationships  
- ✅ Support both CRA and NIS2 regulations (and extensible to others)
- ✅ Idempotent operations for safe re-runs
- ✅ Clear separation: extraction → transformation → ingestion
- ✅ **Phase 4 COMPLETE**: Canonical Obligation & Capability IDs (no regulation prefixes)
- ✅ **Phase 5 COMPLETE**: Policy Chain with parent references and edge type fix
- ✅ **Phase 6 COMPLETE**: Role validity filtering and HAS edge coverage tracking (note: EU regulations often use imperative/passive voice without explicit subjects before duty verbs, so some false-negative role filtering is expected per AC-6)

*End of Document*
