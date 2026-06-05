{{- define "hfao.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "hfao.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "hfao.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "hfao.labels" -}}
helm.sh/chart: {{ include "hfao.chart" . }}
{{ include "hfao.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "hfao.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hfao.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "hfao.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "hfao.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "hfao.imageTag" -}}
{{- default .Chart.AppVersion .Values.image.tag -}}
{{- end -}}

{{- define "hfao.ingestImageTag" -}}
{{- default .Chart.AppVersion .Values.imageIngest.tag -}}
{{- end -}}

{{/*
Shared env block for both cockpit and ingest pods.
Reads HFAO Appendix A env vars from `.Values`.
*/}}
{{- define "hfao.envCommon" -}}
- name: HFAO_PROJECT
  value: {{ .Values.project | quote }}
- name: HFAO_ENVIRONMENT
  value: {{ .Values.environment | quote }}
- name: HFAO_BACKEND
  value: {{ .Values.hot.backend | quote }}
{{- if .Values.hot.clickhouseDsn }}
- name: HFAO_CLICKHOUSE_DSN
  value: {{ .Values.hot.clickhouseDsn | quote }}
{{- end }}
{{- if .Values.control.dsn }}
- name: HFAO_CONTROL_PLANE_DSN
  value: {{ .Values.control.dsn | quote }}
{{- end }}
{{- if .Values.redis.url }}
- name: HFAO_REDIS_URL
  value: {{ .Values.redis.url | quote }}
{{- end }}
{{- if .Values.bodies.path }}
- name: HFAO_BODIES_PATH
  value: {{ .Values.bodies.path | quote }}
{{- end }}
{{- if .Values.hfBucket }}
- name: HFAO_HF_BUCKET
  value: {{ .Values.hfBucket | quote }}
{{- end }}
- name: HFAO_JUDGE_PROVIDER
  value: {{ .Values.judge.provider | quote }}
- name: HFAO_JUDGE_MODEL
  value: {{ .Values.judge.model | quote }}
{{- if .Values.oidc.issuerUrl }}
- name: HFAO_OIDC_ISSUER_URL
  value: {{ .Values.oidc.issuerUrl | quote }}
- name: HFAO_OIDC_CLIENT_ID
  value: {{ .Values.oidc.clientId | quote }}
{{- if .Values.oidc.clientSecretRef }}
- name: HFAO_OIDC_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.oidc.clientSecretRef }}
      key: oidc-client-secret
{{- end }}
{{- end }}
{{- if .Values.mcp.readOnly }}
- name: HFAO_MCP_READ_ONLY
  value: "true"
{{- end }}
{{- if .Values.apiKeysSecretRef }}
- name: ANTHROPIC_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.apiKeysSecretRef }}
      key: anthropic-api-key
      optional: true
- name: OPENAI_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.apiKeysSecretRef }}
      key: openai-api-key
      optional: true
- name: HF_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.apiKeysSecretRef }}
      key: hf-token
      optional: true
{{- end }}
{{- end -}}
