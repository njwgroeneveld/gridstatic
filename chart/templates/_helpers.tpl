{{- define "gridstatic.databaseUrl" -}}
{{- if .Values.database.url -}}
{{ .Values.database.url }}
{{- else -}}
{{- fail "database.url is leeg. Maak een eigen Supabase-project aan (of pak een andere Postgres) en vul de directe connectiestring in: poort 5432, sslmode=require. Wijs dit nooit naar een database waarin al een andere gridstatic-stack werkt." -}}
{{- end -}}
{{- end -}}
