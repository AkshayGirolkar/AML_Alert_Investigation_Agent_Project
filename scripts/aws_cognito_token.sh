#!/usr/bin/env bash
# Print a Cognito access token for the API (F11):
#   export API_TOKEN=$(COGNITO_USER=ana@example.com scripts/aws_cognito_token.sh)
# Reads the client id from Terraform outputs; asks for the password without echoing it.
set -euo pipefail
: "${COGNITO_USER:?set COGNITO_USER to the user email address}"
CLIENT_ID=${COGNITO_CLIENT_ID:-$(terraform -chdir=infra output -raw cognito_client_id)}
[ -n "$CLIENT_ID" ] || { echo "enable_cognito is off (no client id)" >&2; exit 1; }
if [ -z "${COGNITO_PASSWORD:-}" ]; then
  read -r -s -p "Password for $COGNITO_USER: " COGNITO_PASSWORD >&2
  echo >&2
fi
aws cognito-idp initiate-auth --region "${AWS_REGION:-us-east-1}" \
  --auth-flow USER_PASSWORD_AUTH --client-id "$CLIENT_ID" \
  --auth-parameters "USERNAME=$COGNITO_USER,PASSWORD=$COGNITO_PASSWORD" \
  --query 'AuthenticationResult.AccessToken' --output text
