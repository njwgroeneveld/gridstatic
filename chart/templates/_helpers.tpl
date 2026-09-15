{{/*
Name of the Secret holding the database connection. This chart creates no Secrets
of its own: what never passes through here cannot end up in the Helm release
secret or in `helm get values`.
*/}}
{{- define "gridstatic.databaseSecret" -}}
{{- if .Values.database.existingSecret -}}
{{ .Values.database.existingSecret }}
{{- else -}}
{{- fail "database.existingSecret is empty. Create a Secret with the key 'url' first -- install.sh does this for you, or follow 'The database secret' in the README -- and set database.existingSecret to its name. Use the Supabase session pooler (port 5432 on aws-0-<region>.pooler.supabase.com); the direct connection is IPv6-only. Never point it at a database another gridstatic stack is using." -}}
{{- end -}}
{{- end -}}

{{- define "gridstatic.telegramSecret" -}}
{{- if .Values.telegram.existingSecret -}}
{{ .Values.telegram.existingSecret }}
{{- else -}}
{{- fail "telegram.enabled is true but telegram.existingSecret is empty. Create a Secret with 'bot_token' and 'chat_id', or set telegram.enabled to false." -}}
{{- end -}}
{{- end -}}
