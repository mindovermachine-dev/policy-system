{{/*
Expand the name of the chart.
*/}}
{{- define "policy-system.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to
this (by the DNS naming spec).
*/}}
{{- define "policy-system.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "policy-system.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "policy-system.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "policy-system.selectorLabels" -}}
app.kubernetes.io/name: {{ include "policy-system.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
LiteLLM chat-model string for PS_LLMINTERFACE_MODEL: "<provider>/<name>".
name is .Values.llm.model when non-empty, else the provider's shipped default.
Any provider other than "azure" is treated as ollama, mirroring the env
branch ps-service-deployment.yaml had before issue #103.
*/}}
{{- define "policy-system.llmModel" -}}
{{- if eq .Values.llm.provider "azure" -}}
azure/{{ .Values.llm.model | default "gpt-5.4-mini" }}
{{- else -}}
ollama/{{ .Values.llm.model | default "phi3:mini" }}
{{- end -}}
{{- end }}

{{/*
LiteLLM embedding-model string for PS_LLMINTERFACE_EMBED_MODEL — same rule as
policy-system.llmModel, driven by .Values.llm.embedModel.
*/}}
{{- define "policy-system.llmEmbedModel" -}}
{{- if eq .Values.llm.provider "azure" -}}
azure/{{ .Values.llm.embedModel | default "text-embedding-3-large" }}
{{- else -}}
ollama/{{ .Values.llm.embedModel | default "nomic-embed-text" }}
{{- end -}}
{{- end }}

{{/*
Name of the durable (Premium SSD, Retain) StorageClass for FalkorDB's PVC
(AC-BI-014, issue #111). Computed once here so falkordb-storageclass.yaml and
falkordb-pvc.yaml never duplicate the literal name string.
*/}}
{{- define "policy-system.falkordbStorageClassName" -}}
{{- printf "%s-falkordb-durable" (include "policy-system.fullname" .) -}}
{{- end }}

{{/*
Name of the durable (Premium SSD, Retain) StorageClass for Authentik's
hand-rolled Postgres PVC (AC-BI-008, issue #129). Mirrors
policy-system.falkordbStorageClassName exactly, so
authentik-postgres-storageclass.yaml and authentik-postgres-pvc.yaml never
duplicate the literal name string.
*/}}
{{- define "policy-system.authentikPostgresStorageClassName" -}}
{{- printf "%s-authentik-postgres-durable" (include "policy-system.fullname" .) -}}
{{- end }}

{{/*
Name of the Secret consumed by the upstream `authentik` dependency's own
`authentik.existingSecret.secretName` value (AC-BI-010, issue #129 S3). This
chart never generates this Secret's contents itself -- scripts/deploy-ps.sh (a
later slice) provisions it from Key-Vault-sourced values, matching the
`llm.existingSecret`/authentik-postgres-credentials consume-only convention
already used elsewhere in this chart.

IMPORTANT: this helper's output cannot be embedded as live `{{ }}` template
syntax inside values.yaml/values-prod.yaml -- Helm never templates values
files, and the upstream chart's own `authentik.secret.name` helper
(charts/authentik/templates/_helpers.tpl) substitutes
`authentik.existingSecret.secretName` verbatim with no `tpl` re-evaluation
(embedding template syntax there breaks `helm template` outright with a YAML
parse error, verified empirically -- see IMPL_SLICE_3.md). This helper exists
as the single documented definition of the pattern; values-prod.yaml's own
comment cites it and hardcodes its *resolved* literal output for this repo's
one fixed Helm release name ("policy-system", scripts/deploy-ps.sh's
HELM_RELEASE_NAME), so any other consumer of this exact Secret name (e.g. a
later slice's deploy-ps.sh) computes the identical string.
*/}}
{{- define "policy-system.authentikCredentialsSecretName" -}}
{{- printf "%s-authentik-credentials" (include "policy-system.fullname" .) -}}
{{- end }}
