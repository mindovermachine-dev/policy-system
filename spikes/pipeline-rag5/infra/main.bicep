// © 2026 Cartman ApS. All rights reserved.
//
// Subscription-scope entry point: creates (or targets) the resource group,
// then deploys the Azure OpenAI account + two deployments this spike's
// ingest.py/map_graph.py depend on. Parameterizes exactly the two levers
// spikes/pipeline-rag5/docs/speedup-plan.md's Track B is about: chatCapacity
// and embeddingCapacity.
//
// Defaults below match the account as it actually exists today (verified
// via `az cognitiveservices account show` / `deployment show` /
// `usage list` on 2026-08-17) -- Track A's capacity=10 (10 RPM / 10K TPM).
// Track B is raising --chatCapacity at deploy time, not editing this file's
// defaults -- keep the checked-in default at the safe Track A value so a
// plain `az deployment sub create` never silently reintroduces a quota
// change. Confirmed subscription ceiling for GlobalStandard gpt-5.4-mini in
// swedencentral is capacity=1000 (1,000,000 TPM) -- no Microsoft
// quota-increase request needed up to that ceiling.
//
// Deploy (Track A / default, no confirmation needed beyond normal review):
//   az deployment sub create --location swedencentral \
//     --template-file main.bicep
//
// Deploy (Track B -- raise capacity; requires the explicit, separate
// confirmation speedup-plan.md calls for, do not run this as a side effect
// of "implementing the plan"):
//   az deployment sub create --location swedencentral \
//     --template-file main.bicep --parameters chatCapacity=100

targetScope = 'subscription'

@description('Azure region for the resource group and Cognitive Services account.')
param location string = 'swedencentral'

@description('Name of the resource group to create/target.')
param resourceGroupName string = 'rg-policy-system-graphrag-spike'

@description('Name of the Cognitive Services (Azure OpenAI) account.')
param accountName string = 'policy-system-graphrag-spike'

@description('Account-level SKU for the Cognitive Services resource.')
param accountSkuName string = 'S0'

@description('Chat deployment name.')
param chatDeploymentName string = 'gpt-5.4-mini'

@description('Chat model name.')
param chatModelName string = 'gpt-5.4-mini'

@description('Chat model version.')
param chatModelVersion string = '2026-03-17'

@description('Chat deployment SKU (rate-limit tier).')
param chatSkuName string = 'GlobalStandard'

@description('Chat deployment capacity, in units of 1K TPM (observed 1:1 ratio with RPM at capacity=10, i.e. 10 RPM / 10,000 TPM -- not confirmed to hold linearly all the way to 1000). Track A default is 10; raising this is Track B and needs its own explicit approval, not a default-file change. Subscription ceiling in swedencentral is 1000.')
@minValue(1)
@maxValue(1000)
param chatCapacity int = 10

@description('Content-safety policy applied to the chat deployment.')
param chatRaiPolicyName string = 'Microsoft.DefaultV2'

@description('Model version-upgrade behavior for the chat deployment.')
param chatVersionUpgradeOption string = 'OnceNewDefaultVersionAvailable'

@description('Embedding deployment name.')
param embeddingDeploymentName string = 'text-embedding-3-large'

@description('Embedding model name.')
param embeddingModelName string = 'text-embedding-3-large'

@description('Embedding model version.')
param embeddingModelVersion string = '1'

@description('Embedding deployment SKU (rate-limit tier) -- the account\'s actual current configuration uses "Standard" here, not "GlobalStandard" like the chat deployment; that asymmetry is real, not a typo.')
param embeddingSkuName string = 'Standard'

@description('Embedding deployment capacity, in units of 1K TPM.')
@minValue(1)
param embeddingCapacity int = 10

@description('Content-safety policy applied to the embedding deployment.')
param embeddingRaiPolicyName string = 'Microsoft.DefaultV2'

@description('Model version-upgrade behavior for the embedding deployment.')
param embeddingVersionUpgradeOption string = 'OnceNewDefaultVersionAvailable'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
}

module openai 'openai-account.bicep' = {
  name: 'openai-account-deploy'
  scope: rg
  params: {
    location: location
    accountName: accountName
    accountSkuName: accountSkuName
    chatDeploymentName: chatDeploymentName
    chatModelName: chatModelName
    chatModelVersion: chatModelVersion
    chatSkuName: chatSkuName
    chatCapacity: chatCapacity
    chatRaiPolicyName: chatRaiPolicyName
    chatVersionUpgradeOption: chatVersionUpgradeOption
    embeddingDeploymentName: embeddingDeploymentName
    embeddingModelName: embeddingModelName
    embeddingModelVersion: embeddingModelVersion
    embeddingSkuName: embeddingSkuName
    embeddingCapacity: embeddingCapacity
    embeddingRaiPolicyName: embeddingRaiPolicyName
    embeddingVersionUpgradeOption: embeddingVersionUpgradeOption
  }
}

output endpoint string = openai.outputs.endpoint
output accountName string = openai.outputs.accountName
