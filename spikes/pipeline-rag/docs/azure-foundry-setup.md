# Azure AI Foundry setup for spikes/pipeline-rag


## Prerequisites

- Azure CLI installed and authenticated: `az login`, then confirm the right
  subscription with `az account show` / `az account set --subscription <id>`.
- Sufficient quota for the chat + embedding model deployments you intend to
  use (quota is region- and subscription-specific; check the Azure AI
  Foundry portal's quota page before provisioning if unsure).

## 1. Resource group

```bash
az group create \
  --name rg-policy-system-graphrag-spike \
  --location <region>   # check `az cognitiveservices model list --location <region>`
                         # and `usage list` before picking one with quota
```

## 2. Azure AI Foundry (Azure OpenAI) resource

```bash
az cognitiveservices account create \
  --name policy-system-graphrag-spike \
  --resource-group rg-policy-system-graphrag-spike \
  --kind OpenAI \
  --sku S0 \
  --location <region> \
  --custom-domain policy-system-graphrag-spike
```

## 3. Deploy a chat model and an embedding model

Pick deployment names you'll reuse as `--model`/`--embed-model` in
`ingest.py --backend azure`. Model/version availability depends on region
and subscription offer — check `az cognitiveservices model list --location
<region>` (not `list-models`, which needs an existing resource and is less
useful for picking a version up front) before picking one.

SKU availability varies by model and region — e.g. `gpt-5.4-mini` may only
offer `GlobalStandard`/`DataZoneStandard`, not plain `Standard`, depending
on where you deploy. Check `az cognitiveservices model list --location
<region>` for `model.skus` before assuming `Standard` is available for
whichever model you pick.

```bash
az cognitiveservices account deployment create \
  --name <resource-name> \
  --resource-group rg-policy-system-graphrag-spike \
  --deployment-name gpt-5.4-mini \
  --model-name gpt-5.4-mini \
  --model-version 2026-03-17 \
  --model-format OpenAI \
  --sku-capacity 10 \
  --sku-name GlobalStandard

az cognitiveservices account deployment create \
  --name <resource-name> \
  --resource-group rg-policy-system-graphrag-spike \
  --deployment-name text-embedding-3-large \
  --model-name text-embedding-3-large \
  --model-version 1 \
  --model-format OpenAI \
  --sku-capacity 10 \
  --sku-name Standard
```

## 4. Retrieve endpoint + key

```bash
az cognitiveservices account show \
  --name policy-system-graphrag-spike \
  --resource-group rg-policy-system-graphrag-spike \
  --query "properties.endpoint" -o tsv

az cognitiveservices account keys list \
  --name policy-system-graphrag-spike \
  --resource-group rg-policy-system-graphrag-spike \
  --query "key1" -o tsv
```

## 5. Set environment variables

LiteLLM's `azure/` provider (used by `ingest.py --backend azure`) reads
these directly:

```bash
export AZURE_API_KEY="<key1 from step 4>"
export AZURE_API_BASE="<endpoint from step 4>"
export AZURE_API_VERSION="<api version, e.g. 2024-10-21 -- check current supported version>"
```

`ingest.py` refuses to start against `--backend azure` if any of these are
unset, rather than silently falling back to Ollama.

## 6. Run

```bash
python spikes/pipeline-rag/ingest.py \
  --backend azure \
  --model gpt-5.4-mini \
  --embed-model text-embedding-3-large \
  --source all
```

`--model`/`--embed-model` here are the **deployment names** from step 3,
not the underlying model names — Azure OpenAI/Foundry routes by deployment,
not model name directly.

## Cost note

Every ingest run against this backend calls a billed Azure OpenAI/Foundry
endpoint per chunk (extraction) and per entity (embedding backfill). Check
the deployment's pricing tier before running the full CRA/NIS2/GDPR +
Engineering Practices set, especially repeatedly during iteration — prefer
iterating against the Ollama backend first (see `../README.md`) and only
switching to Azure once the schema/prompting is already working.

## Teardown

```bash
az group delete --name rg-policy-system-graphrag-spike --yes --no-wait
```

Deletes the resource group and everything in it. Not run automatically by
anything in this spike — run it explicitly once the spike concludes, if the
resource shouldn't persist.
