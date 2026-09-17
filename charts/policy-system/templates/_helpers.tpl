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
