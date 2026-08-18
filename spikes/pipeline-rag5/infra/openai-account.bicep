// © 2026 Cartman ApS. All rights reserved.
// Cognitive Services (Azure OpenAI) account + two deployments, deployed at
// resource-group scope. Consumed as a module by main.bicep. Mirrors the
// account created ad hoc for spikes/pipeline-rag5/ingest.py + map_graph.py
// (verified live via `az cognitiveservices account show` /
// `deployment show` on 2026-08-17) -- not a from-scratch design.

@description('Azure region for the Cognitive Services account.')
param location string

@description('Name of the Cognitive Services (Azure OpenAI) account.')
param accountName string

@description('Account-level SKU for the Cognitive Services resource.')
param accountSkuName string

@description('Chat deployment name.')
param chatDeploymentName string

@description('Chat model name.')
param chatModelName string

@description('Chat model version.')
param chatModelVersion string

@description('Chat deployment SKU (rate-limit tier).')
param chatSkuName string

@description('Chat deployment capacity, in units of 1K TPM.')
@minValue(1)
@maxValue(1000)
param chatCapacity int

@description('Content-safety policy applied to the chat deployment.')
param chatRaiPolicyName string

@description('Model version-upgrade behavior for the chat deployment.')
param chatVersionUpgradeOption string

@description('Embedding deployment name.')
param embeddingDeploymentName string

@description('Embedding model name.')
param embeddingModelName string

@description('Embedding model version.')
param embeddingModelVersion string

@description('Embedding deployment SKU (rate-limit tier).')
param embeddingSkuName string

@description('Embedding deployment capacity, in units of 1K TPM.')
@minValue(1)
param embeddingCapacity int

@description('Content-safety policy applied to the embedding deployment.')
param embeddingRaiPolicyName string

@description('Model version-upgrade behavior for the embedding deployment.')
param embeddingVersionUpgradeOption string

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: accountName
  location: location
  kind: 'OpenAI'
  sku: {
    name: accountSkuName
  }
  properties: {
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
  }
}

resource chatDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: chatDeploymentName
  sku: {
    name: chatSkuName
    capacity: chatCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: chatModelName
      version: chatModelVersion
    }
    raiPolicyName: chatRaiPolicyName
    versionUpgradeOption: chatVersionUpgradeOption
  }
}

// Cognitive Services deployments on the same account can conflict if
// created in parallel -- explicit dependsOn forces sequential creation
// (observed real-world Bicep/ARM behavior for this resource type, not a
// stylistic choice).
resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: embeddingDeploymentName
  sku: {
    name: embeddingSkuName
    capacity: embeddingCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModelName
      version: embeddingModelVersion
    }
    raiPolicyName: embeddingRaiPolicyName
    versionUpgradeOption: embeddingVersionUpgradeOption
  }
  dependsOn: [
    chatDeployment
  ]
}

output endpoint string = account.properties.endpoint
output accountName string = account.name
