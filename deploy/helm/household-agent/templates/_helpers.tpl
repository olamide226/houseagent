{{- define "household-agent.name" -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}

{{- define "household-agent.labels" -}}
app.kubernetes.io/name: household-agent
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "household-agent.selector" -}}
app.kubernetes.io/name: household-agent
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "household-agent.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{/* Configuration and secrets for a container: the ConfigMap, then the existing Secret. */}}
{{- define "household-agent.env" -}}
envFrom:
  - configMapRef:
      name: {{ include "household-agent.name" . }}
  - secretRef:
      name: {{ required "existingSecret names the Secret that holds DATABASE_URL, SESSION_SECRET and the provider keys" .Values.existingSecret }}
{{- end -}}

{{/* Runs as nobody on a read-only root; /tmp is the one writable place. */}}
{{- define "household-agent.securityContext" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 65534
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: [ALL]
{{- end -}}

{{/*
Codex's sign-in, for LLM_PROVIDER=codex_cli: one claim, mounted in both processes, because Codex
rewrites the file when it refreshes the sign-in and two copies would log each other out.
*/}}
{{- define "household-agent.codexHomeMount" -}}
{{- if .Values.codexHome.existingClaim }}
- {name: codex-home, mountPath: {{ .Values.codexHome.mountPath }}}
{{- end }}
{{- end -}}

{{- define "household-agent.codexHomeVolume" -}}
{{- if .Values.codexHome.existingClaim }}
- name: codex-home
  persistentVolumeClaim:
    claimName: {{ .Values.codexHome.existingClaim }}
{{- end }}
{{- end -}}

{{/* The pods run as nobody, who must be able to write the claim. */}}
{{- define "household-agent.codexHomeOwner" -}}
{{- if .Values.codexHome.existingClaim }}
securityContext:
  fsGroup: 65534
{{- end }}
{{- end -}}

{{- define "household-agent.scheduling" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* The paths of one ingress, each a Prefix match to the api Service. */}}
{{- define "household-agent.paths" -}}
{{- $root := index . 0 -}}
{{- range $i, $path := index . 1 }}
{{- if $i }}
{{ end -}}
- path: {{ $path }}
  pathType: Prefix
  backend:
    service:
      name: {{ include "household-agent.name" $root }}-api
      port:
        name: http
{{- end }}
{{- end -}}
