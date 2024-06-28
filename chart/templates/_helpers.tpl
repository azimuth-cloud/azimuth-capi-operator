{{/*
Expand the name of the chart.
*/}}
{{- define "azimuth-capi-operator.name" -}}
{{- .Chart.Name | lower | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified name for a chart-level resource.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "azimuth-capi-operator.fullname" -}}
{{- if contains .Chart.Name .Release.Name }}
{{- .Release.Name | lower | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name .Chart.Name | lower | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Selector labels for a chart-level resource.
*/}}
{{- define "azimuth-capi-operator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "azimuth-capi-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Labels for a chart-level resource.
*/}}
{{- define "azimuth-capi-operator.labels" -}}
helm.sh/chart: {{
  printf "%s-%s" .Chart.Name .Chart.Version |
    replace "+" "_" |
    lower |
    trunc 63 |
    trimSuffix "-" |
    trimSuffix "_" |
    trimSuffix "."
}}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{ include "azimuth-capi-operator.selectorLabels" . }}
{{- end }}

{{/*
Produces the metadata for a CRD.
*/}}
{{- define "azimuth-capi-operator.crd.metadata" }}
metadata:
  labels: {{ include "azimuth-capi-operator.labels" . | nindent 4 }}
  {{- if .Values.crds.keep }}
  annotations:
    helm.sh/resource-policy: keep
  {{- end }}
{{- end }}

{{/*
Loads a CRD from the specified file and merges in the metadata.
*/}}
{{- define "azimuth-capi-operator.crd" }}
{{- $ctx := index . 0 }}
{{- $path := index . 1 }}
{{- $crd := $ctx.Files.Get $path | fromYaml }}
{{- $metadata := include "azimuth-capi-operator.crd.metadata" $ctx | fromYaml }}
{{- merge $crd $metadata | toYaml }}
{{- end }}
