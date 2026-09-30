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
`authentik.existingSecret.secretName` value (AC-BI-010, issue #129 S3).
As of issue #159, this chart generates this Secret's own contents itself
(templates/authentik-credentials-secret.yaml, via
policy-system.generateOrReuseSecretValue) whenever
`authentik.credentialsExistingSecret` is unset -- the escape hatch, when set,
points this helper (and therefore the rendered Secret name) at an
operator-managed Secret instead. Mirrors
policy-system.psPostgresSigningSecretName's exact
"operator-managed-name-or-chart-generates" shape.

IMPORTANT: this helper's output cannot be embedded as live `{{ }}` template
syntax inside values.yaml/values-prod.yaml -- Helm never templates values
files, and the upstream chart's own `authentik.secret.name` helper
(charts/authentik/templates/_helpers.tpl) substitutes
`authentik.existingSecret.secretName` verbatim with no `tpl` re-evaluation
(embedding template syntax there breaks `helm template` outright with a YAML
parse error, verified empirically -- see IMPL_SLICE_3.md). This helper exists
as the single documented definition of the pattern; values.yaml/
values-prod.yaml's own `authentik.authentik.existingSecret.secretName`
comments cite it and hardcode its *resolved* literal output for this repo's
one fixed Helm release name ("policy-system", scripts/deploy-ps-prod.sh's
HELM_RELEASE_NAME) -- kept in sync by hand with this helper's default output,
not by any automatic Helm wiring (see authentik-credentials-secret.yaml's own
fail() guard, issue #159, for the safety net when the two disagree).
*/}}
{{- define "policy-system.authentikCredentialsSecretName" -}}
{{- .Values.authentik.credentialsExistingSecret | default (printf "%s-authentik-credentials" (include "policy-system.fullname" .)) -}}
{{- end }}

{{/*
Name of the durable (Premium SSD, Retain) StorageClass for the single PS
Postgres server's PVC (AC-BI-006/AC-BI-008, issues #131/#130 Slice 5). Mirrors
policy-system.authentikPostgresStorageClassName exactly, so
ps-postgres-storageclass.yaml and ps-postgres-pvc.yaml never duplicate the
literal name string.
*/}}
{{- define "policy-system.psPostgresStorageClassName" -}}
{{- printf "%s-ps-postgres-durable" (include "policy-system.fullname" .) -}}
{{- end }}

{{/*
Names of the three PS Postgres credentials Secrets (issue #130 Slice 5):
admin (superuser; consumed only by the Postgres container), state role, signing
role. Each is "operator-managed name if psPostgres.<role>.existingSecret is
set, else the chart generates its own Secret" (ps-postgres-secret.yaml, issue
#159 lookup+randAlphaNum idiom) -- own Secret per role, never appended to
Authentik's or FalkorDB's (AC-BI-006's isolation extended to credentials).
*/}}
{{- define "policy-system.psPostgresAdminSecretName" -}}
{{- .Values.psPostgres.admin.existingSecret | default (printf "%s-ps-postgres-admin-credentials" (include "policy-system.fullname" .)) -}}
{{- end }}

{{- define "policy-system.psPostgresStateSecretName" -}}
{{- .Values.psPostgres.state.existingSecret | default (printf "%s-ps-postgres-state-credentials" (include "policy-system.fullname" .)) -}}
{{- end }}

{{- define "policy-system.psPostgresSigningSecretName" -}}
{{- .Values.psPostgres.signing.existingSecret | default (printf "%s-ps-postgres-signing-credentials" (include "policy-system.fullname" .)) -}}
{{- end }}

{{/*
Name of the Secret carrying PS_AUTHENTIK_API_TOKEN (issue #140) -- PS
Service's own service credential for calling Authentik's invitation-stage
API, consumed by ps-service-deployment.yaml's PS_AUTHENTIK_API_TOKEN env var.
Mirrors policy-system.psPostgresSigningSecretName's exact shape
("operator-managed name, or the chart renders one from a plain value") --
deliberately NOT policy-system.authentikCredentialsSecretName above, which is
a different Secret entirely (the upstream `authentik` dependency's own
consume-only credentials, provisioned externally by scripts/deploy-ps-prod.sh) --
this chart generates and owns this Secret's contents itself, same as
ps-postgres-secret.yaml does for Passkey Signing.
*/}}
{{- define "policy-system.authentikApiTokenSecretName" -}}
{{- .Values.psService.authentik.existingSecret | default (printf "%s-authentik-api-token" (include "policy-system.fullname" .)) -}}
{{- end }}

{{/*
Generates a Secret value once and makes it survive every subsequent `helm
upgrade` by reading it back from an already-installed Secret via `lookup`,
falling back to a freshly `randAlphaNum`-generated value when no live cluster
is reachable (`helm template`/`helm lint`/`helm unittest`, AC-BI-004) or the
named Secret/key does not exist yet (first install, AC-BI-001/AC-BI-002,
issue #159).

Re-implements the same lookup-then-generate idiom the bundled Authentik
dependency's own, unreferenced Bitnami common library implements at
charts/authentik-2026.8.3.tgz ->
authentik/charts/postgresql/charts/common/templates/_secrets.tpl's
common.secrets.passwords.manage (lines 103-110, 116-138) and
common.secrets.lookup (lines 164-175) -- NOT `include`-able from this chart's
own templates, because the postgresql sub-subchart it lives in is
dependency-condition-gated off in every profile this chart ships
(authentik.postgresql.enabled: false), and Helm excludes a
condition-disabled dependency's templates from the whole render's
named-template namespace entirely.

Usage:
{{ include "policy-system.generateOrReuseSecretValue" (dict "secretName" (include "some.secretName.helper" .) "key" "SOME_KEY" "length" 32 "context" $) }}

Params:
  - secretName - String - Required - name of the Secret to look up.
  - key        - String - Required - key inside that Secret's data to reuse if present.
  - length     - Int    - Required - length of a freshly generated value (randAlphaNum).
  - context    - Dict   - Required - the root context ($), for .Release.Namespace.
  - ignore     - List   - Optional - stored values to treat as absent (a retired placeholder that
                          must not survive an upgrade as the live credential, issue #165 F-1).
*/}}
{{- define "policy-system.generateOrReuseSecretValue" -}}
{{- $existing := lookup "v1" "Secret" .context.Release.Namespace .secretName -}}
{{- $stored := "" -}}
{{- if and $existing $existing.data (hasKey $existing.data .key) -}}
{{- $stored = index $existing.data .key | b64dec -}}
{{- end -}}
{{- if and $stored (not (has $stored (.ignore | default list))) -}}
{{- $stored -}}
{{- else -}}
{{- randAlphaNum (.length | int) -}}
{{- end -}}
{{- end -}}
