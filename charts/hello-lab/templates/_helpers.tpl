{{/*
Expand the name of the chart.
*/}}
{{- define "hello-lab.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name (max 63 chars, DNS-1123).
If the release name already contains the chart name, don't repeat it.
*/}}
{{- define "hello-lab.fullname" -}}
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
Chart name and version as used by the chart label.
*/}}
{{- define "hello-lab.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels (recommended Kubernetes labels).
*/}}
{{- define "hello-lab.labels" -}}
helm.sh/chart: {{ include "hello-lab.chart" . }}
{{ include "hello-lab.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: k8s-helm-lab
environment: {{ .Values.app.environment | quote }}
{{- end }}

{{/*
Selector labels. These must never change after the first install
(Deployment selectors are immutable).
*/}}
{{- define "hello-lab.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hello-lab.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of the service account to use.
*/}}
{{- define "hello-lab.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "hello-lab.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
