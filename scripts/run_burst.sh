#!/usr/bin/env bash
set -euo pipefail

: "${WALLET_BASE_URL:=https://wallet-app-fast-api.onrender.com}"
: "${JWT_SECRET:?Set JWT_SECRET to the deployment JWT signing secret}"
: "${TEST_SEED_KEY:?Set TEST_SEED_KEY to the deployment test seed key}"
: "${READINESS_RETRY_SECONDS:=30}"
: "${READINESS_MAX_ATTEMPTS:=6}"

for command in curl jq openssl uuidgen xargs; do
  command -v "$command" >/dev/null || { echo "Missing required command: $command" >&2; exit 1; }
done

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

check_ready() {
  local url="$WALLET_BASE_URL/ready" response_file status_code body attempt
  echo 'Checking whether the API and database are ready before starting the test cases.'
  echo 'A sleeping free Render service can take about a minute to wake up. Retrying every 30 seconds if needed.'

  for attempt in $(seq 1 "$READINESS_MAX_ATTEMPTS"); do
    response_file="$(mktemp "$work_dir/ready.XXXXXX")"
    printf 'Readiness attempt %s/%s: GET %s\n' "$attempt" "$READINESS_MAX_ATTEMPTS" "$url"
    status_code="$(curl --silent --show-error --connect-timeout 10 --max-time 20 \
      --output "$response_file" --write-out '%{http_code}' "$url")" || status_code="000"
    body="$(<"$response_file")"
    if [[ "$status_code" == "200" ]]; then
      printf 'Readiness response: HTTP %s %s\n' "$status_code" "$body"
      echo 'Readiness check passed: API and database are available. Starting test cases.'
      return
    fi

    if (( attempt < READINESS_MAX_ATTEMPTS )); then
      printf 'Service is not ready yet (HTTP %s). It may be waking up; retrying in %s seconds.\n' \
        "$status_code" "$READINESS_RETRY_SECONDS"
      sleep "$READINESS_RETRY_SECONDS"
    fi
  done

  echo "Service did not become ready after $READINESS_MAX_ATTEMPTS attempts: $url" >&2
  echo 'Check the Render deployment logs and database status, then rerun this script.' >&2
  exit 1
}

jwt_for() {
  local user_id="$1" header payload signature
  header="$(printf '%s' '{"alg":"HS256","typ":"JWT"}' | b64url)"
  payload="$(printf '{"sub":"%s"}' "$user_id" | b64url)"
  signature="$(printf '%s' "$header.$payload" | openssl dgst -sha256 -hmac "$JWT_SECRET" -binary | b64url)"
  printf '%s.%s.%s' "$header" "$payload" "$signature"
}

api_post() {
  local path="$1" token="$2" payload="$3" url response_file status_code body
  url="$WALLET_BASE_URL$path"
  response_file="$(mktemp "$work_dir/response.XXXXXX")"
  status_code="$(curl --silent --show-error --output "$response_file" --write-out '%{http_code}' \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    -X POST "$url" --data "$payload")" || return 1
  body="$(<"$response_file")"
  if (( status_code < 200 || status_code >= 300 )); then
    echo "Request failed: POST $url returned HTTP $status_code: $body" >&2
    return 1
  fi
  printf '%s' "$body"
}

create_wallet() {
  local user_id="$1"
  api_post "/wallets" "$(jwt_for "$user_id")" '{}' | jq -r '.id'
}

fund_wallet() {
  local wallet_id="$1" amount="$2" url payload response_file status_code body
  url="$WALLET_BASE_URL/internal/test/wallets/$wallet_id/fund"
  payload="{\"amount_paise\":$amount}"
  response_file="$(mktemp "$work_dir/response.XXXXXX")"
  status_code="$(curl --silent --show-error --output "$response_file" --write-out '%{http_code}' -X POST \
    -H 'Content-Type: application/json' -H "X-Test-Seed-Key: $TEST_SEED_KEY" \
    "$url" --data "$payload")" || return 1
  body="$(<"$response_file")"
  if (( status_code < 200 || status_code >= 300 )); then
    echo "Request failed: POST $url returned HTTP $status_code: $body" >&2
    return 1
  fi
}

get_balance() {
  local user_id="$1" wallet_id="$2" url response_file status_code body
  url="$WALLET_BASE_URL/wallets/$wallet_id"
  response_file="$(mktemp "$work_dir/response.XXXXXX")"
  status_code="$(curl --silent --show-error --output "$response_file" --write-out '%{http_code}' \
    -H "Authorization: Bearer $(jwt_for "$user_id")" \
    "$url")" || return 1
  body="$(<"$response_file")"
  if (( status_code < 200 || status_code >= 300 )); then
    echo "Request failed: GET $url returned HTTP $status_code: $body" >&2
    return 1
  fi
  jq -r '.balance_paise' <<<"$body"
}

cat <<'EOF'

Wallet Transfer API - Concurrency Burst Harness
-----------------------------------------------
This harness calls the deployed HTTP API only. It proves that wallet creation,
idempotency, balance conservation, and no-overdraft rules hold under contention.

EOF
echo '{"event":"burst_started"}'
check_ready

cat <<'EOF'

TC1 - Concurrent wallet get-or-create
What:     Send 50 simultaneous POST /wallets requests for one new authenticated user.
Why:      Proves the unique user-to-wallet rule is race-free under concurrency.
Expected: All 50 responses return the same single wallet ID.
EOF

creation_user="$(uuidgen | tr '[:upper:]' '[:lower:]')"
echo "Making 50 concurrent calls to POST $WALLET_BASE_URL/wallets ..."
for index in $(seq 1 50); do
  (api_post '/wallets' "$(jwt_for "$creation_user")" '{}' >"$work_dir/create-$index.json") &
done
wait
creation_ids="$(jq -r '.id' "$work_dir"/create-*.json | sort -u)"
[[ "$(printf '%s\n' "$creation_ids" | wc -l | tr -d ' ')" == "1" ]] || { echo 'wallet creation failed' >&2; exit 1; }
echo "{\"event\":\"concurrent_wallet_creation_passed\",\"requests\":50,\"wallet_id\":\"$creation_ids\"}"

cat <<'EOF'

TC2 - Idempotent transfer retry storm
What:     Fund one sender, then send the identical transfer request 30 times concurrently.
Why:      Proves retries do not cause a second debit or credit.
Expected: One transfer ID, sender balance 9250 paise, recipient balance 750 paise.
EOF

sender_user="$(uuidgen | tr '[:upper:]' '[:lower:]')"
recipient_user="$(uuidgen | tr '[:upper:]' '[:lower:]')"
sender_wallet="$(create_wallet "$sender_user")"
recipient_wallet="$(create_wallet "$recipient_user")"
fund_wallet "$sender_wallet" 10000
idempotency_key="retry-storm-$(uuidgen)"
transfer_payload="{\"from\":\"$sender_wallet\",\"to\":\"$recipient_wallet\",\"amount_paise\":750,\"idempotency_key\":\"$idempotency_key\"}"
echo "Making 30 concurrent calls to POST $WALLET_BASE_URL/transfers with one idempotency key ..."
for index in $(seq 1 30); do
  (api_post '/transfers' "$(jwt_for "$sender_user")" "$transfer_payload" >"$work_dir/retry-$index.json") &
done
wait
transfer_ids="$(jq -r '.id' "$work_dir"/retry-*.json | sort -u)"
[[ "$(printf '%s\n' "$transfer_ids" | wc -l | tr -d ' ')" == "1" ]] || { echo 'idempotency test failed' >&2; exit 1; }
[[ "$(get_balance "$sender_user" "$sender_wallet")" == "9250" ]]
[[ "$(get_balance "$recipient_user" "$recipient_wallet")" == "750" ]]
echo "{\"event\":\"idempotency_storm_passed\",\"requests\":30,\"transfer_id\":\"$transfer_ids\"}"

cat <<'EOF'

TC3 - Conservation under bidirectional contention
What:     Create six funded wallets and submit 200 unique transfers in both directions.
Why:      Forces competing locks while proving no money is created, lost, or overdrawn.
Expected: All requests finish completed or declined; total remains 30000 paise; no negative balance.
EOF

wallet_users=()
wallet_ids=()
for index in $(seq 0 5); do
  wallet_users[$index]="$(uuidgen | tr '[:upper:]' '[:lower:]')"
  wallet_ids[$index]="$(create_wallet "${wallet_users[$index]}")"
  fund_wallet "${wallet_ids[$index]}" 5000
done

echo "Making 200 POST $WALLET_BASE_URL/transfers calls in batches of 12 ..."
for index in $(seq 0 199); do
  source_index=$((index % 6))
  if (( index % 2 == 0 )); then
    destination_index=$(((source_index + 1) % 6))
  else
    destination_index=$(((source_index + 5) % 6))
  fi
  if (( index % 5 == 0 )); then amount=20000; else amount=125; fi
  payload="{\"from\":\"${wallet_ids[$source_index]}\",\"to\":\"${wallet_ids[$destination_index]}\",\"amount_paise\":$amount,\"idempotency_key\":\"contention-$(uuidgen)\"}"
  (
    api_post '/transfers' "$(jwt_for "${wallet_users[$source_index]}")" "$payload" \
      >"$work_dir/contention-$index.json"
  ) &
  if (( (index + 1) % 12 == 0 )); then
    wait
  fi
done
wait

completed="$(jq -r 'select(.status == "completed") | .id' "$work_dir"/contention-*.json | wc -l | tr -d ' ')"
declined="$(jq -r 'select(.status == "declined") | .id' "$work_dir"/contention-*.json | wc -l | tr -d ' ')"
[[ $((completed + declined)) == 200 ]] || { echo 'contention requests did not return final states' >&2; exit 1; }
total=0
negative_wallets=0
echo "Reading final balances from GET $WALLET_BASE_URL/wallets/{wallet_id} ..."
for index in $(seq 0 5); do
  balance="$(get_balance "${wallet_users[$index]}" "${wallet_ids[$index]}")"
  total=$((total + balance))
  (( balance >= 0 )) || negative_wallets=$((negative_wallets + 1))
done
[[ "$total" == "30000" && "$negative_wallets" == "0" ]] || {
  echo "conservation failed: total=$total negative_wallets=$negative_wallets" >&2
  exit 1
}
echo "{\"event\":\"conservation_contention_passed\",\"requests\":200,\"completed\":$completed,\"declined\":$declined,\"total_before\":30000,\"total_after\":$total,\"negative_wallets\":$negative_wallets}"

cat <<'EOF'

All test cases passed.
The API preserved the core money invariants under concurrent HTTP requests.
EOF
echo '{"event":"burst_passed"}'