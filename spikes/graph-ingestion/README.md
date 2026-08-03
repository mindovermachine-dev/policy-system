# Spike: Regulation Ingestion into Graph Database

---

## 🚀 Run the Spike (End-to-End Demonstration)

**Run this to demonstrate the pipeline working with real data and services.**

### Prerequisites

1. **Python 3.9+**
2. **Ollama** running locally with `qwen3-coder-next:q8_0` model
3. **FalkorDB** running on localhost:6379 (optional - for graph integration)

### Quick Start

```bash
cd docs/spikes/graph-ingestion

# Install dependencies
pip install -r requirements.txt

# Verify Ollama is available
curl http://localhost:11434/api/tags | grep qwen3-coder-next

# If model not present, pull it:
ollama pull qwen3-coder-next:q8_0

# Start FalkorDB (optional - for graph integration)
podman run --rm -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
sleep 30
redis-cli MODULE LIST | grep graph

# Run the spike!
python run_spike.py
```

### Command Line Options

```bash
usage: python run_spike.py [-h] [--max-chunks N] [--ollama-url URL] [--regulation TYPE]
                           [--all]

Options:
  --max-chunks N        Max articles to process per regulation (default: all, use `--max-chunks 5` for fast demo)
  --ollama-url URL      Ollama endpoint (default: http://localhost:11434)
  --regulation TYPE     cra or nis2 (default: cra)
  --all                 Ingest both CRA and NIS2 regulations
```

**Examples:**
```bash
python run_spike.py                    # Process ALL CRA articles (71) - full ingestion
python run_spike.py --max-chunks 5     # Process first 5 articles only (fast demo, ~3 min)
python run_spike.py --regulation nis2  # Try NIS2 regulation instead
python run_spike.py --all              # Ingest both regulations (CRA + NIS2) - full ingestion
```

---

## 📚 Documentation

| File | Purpose |
|------|---------|
| **README.md** | This file - spike overview and how-to-run |
| `run_spike.py` | ✅ Execute the end-to-end pipeline demonstration |
| `HOW-TESTED.md` | ⚙️ Unit testing for component verification (not the spike itself) |
| `CLI.md` | 📋 Command-line interface reference |
| `data-model.md` | 💾 Data model and graph schema specifications |
| `graph-data-schema.md` | 🗃 Graph database schema details |

---

## ✅ What's Working (Spike Status)

| Component | Tests | Status |
|-----------|-------|--------|
| Chunker (article-level HTML parsing) | 5/5 | ✅ Complete |
| LLM Extractor (Ollama with retries & truncation) | 5/5 | ✅ Complete |
| Graph Integration (FalkorDB insertion) | 3/3 | ✅ Complete |
| End-to-End Pipeline | 1/1 | ✅ Complete |

**Total: 14/14 tests passing** (all with real services, no mocks)

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
│ extractor    │ Call Ollama API for each chunk
└──────┬───────┘
       │ List of obligations
       ▼
┌──────────────┐
│ graph.py     │ Insert to FalkorDB
└──────┬───────┘
       │ Graph nodes + CONTAINS edges
```

---

## 🧪 Unit Testing (For Developers)

**Note**: These are for component verification during development, NOT the spike demonstration.

```bash
# Run chunker tests only (no external dependencies)
pytest test/test_real_eu_cra_chunking.py -v

# Run extractor tests (requires Ollama running)
export OLLAMA_URL=http://localhost:11434
pytest test/test_extractor.py -v
```

**See `HOW-TESTED.md` for complete testing instructions.**

---

## 🎯 Spike Goals Achieved

- ✅ **Article-level chunking** from EU EUR-Lex HTML works correctly (CRA: 71, NIS2: 46 articles)
- ✅ **LLM obligation extraction** with confidence scoring ≥0.90
- ✅ **Retry logic** handles transient Ollama failures
- ✅ **Text truncation** prevents timeouts on long articles
- ✅ **FalkorDB integration** inserts nodes and edges correctly
- ✅ **Idempotent operations** using MERGE for safe re-runs

---

## 📝 Files

```
docs/spikes/graph-ingestion/
├── README.md                    # This file - spike overview
├── run_spike.py                 # ✨ EXECUTE THE SPIKE HERE (end-to-end demo)
├── HOW-TESTED.md                # Unit tests for developers (not the spike demo)
├── CLI.md                       # Command-line interface reference
├── data-model.md                # Data model specifications
├── graph-data-schema.md         # Graph database schema
├── requirements.txt             # Python dependencies
└── src/                         # Pipeline components
    ├── chunker.py               # Article-level HTML parsing
    ├── extractor.py             # LLM obligation extraction with Ollama
    └── graph.py                 # FalkorDB integration
```

---

## 🚦 Next Steps

After the spike, production implementation would need:

- Async/batched processing for performance (currently sequential ~25s per obligation)
- Centralized configuration management
- Comprehensive audit logging of LLM interactions
- Batch graph operations to reduce network round-trips
- Business IDs instead of ephemeral database IDs

But for now: **Run `python run_spike.py` to see it working!** 🎉
