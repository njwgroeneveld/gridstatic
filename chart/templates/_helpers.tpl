{{/*
Name of the Secret holding the Hyperliquid key. This chart creates no Secrets of
its own: what never passes through here cannot end up in the Helm release secret
or in `helm get values`.
*/}}
{{- define "gridstatic.hyperliquidSecret" -}}
{{- if .Values.hyperliquid.existingSecret -}}
{{ .Values.hyperliquid.existingSecret }}
{{- else -}}
{{- fail "hyperliquid.existingSecret is empty. Create a Secret with the keys 'private_key' and 'wallet_address' first -- install.sh does this for you, or follow 'Manual installation' in docs/operations.md -- and set hyperliquid.existingSecret to its name. hyperliquid.testnet is true by default: use a testnet key to try the bot." -}}
{{- end -}}
{{- end -}}

{{- define "gridstatic.telegramSecret" -}}
{{- if .Values.telegram.existingSecret -}}
{{ .Values.telegram.existingSecret }}
{{- else -}}
{{- fail "telegram.enabled is true but telegram.existingSecret is empty. Create a Secret with 'bot_token' and 'chat_id', or set telegram.enabled to false." -}}
{{- end -}}
{{- end -}}
