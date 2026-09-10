{{/*
De naam van het Secret met de databaseverbinding. Deze chart maakt zelf geen
Secrets: wat hier niet doorheen loopt, kan niet in het Helm-release-secret of in
`helm get values` terechtkomen.
*/}}
{{- define "gridstatic.databaseSecret" -}}
{{- if .Values.database.existingSecret -}}
{{ .Values.database.existingSecret }}
{{- else -}}
{{- fail "database.existingSecret is leeg. Maak eerst een Secret met de sleutel 'url' aan:\n\n  kubectl -n <namespace> create secret generic gridstatic-db --from-literal=url='postgresql://postgres.PROJECTREF:WACHTWOORD@aws-0-REGIO.pooler.supabase.com:5432/postgres?sslmode=require'\n\nen zet database.existingSecret op gridstatic-db. Pak in het Supabase-dashboard de session pooler (poort 5432, host aws-0-<regio>.pooler.supabase.com); de directe verbinding is IPv6-only. Wijs dit nooit naar een database waarin al een andere gridstatic-stack werkt." -}}
{{- end -}}
{{- end -}}

{{- define "gridstatic.telegramSecret" -}}
{{- if .Values.telegram.existingSecret -}}
{{ .Values.telegram.existingSecret }}
{{- else -}}
{{- fail "telegram.enabled staat op true maar telegram.existingSecret is leeg. Maak een Secret met 'bot_token' en 'chat_id', of zet telegram.enabled op false." -}}
{{- end -}}
{{- end -}}
