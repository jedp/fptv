#!/usr/bin/env bash
set -euo pipefail

# tvh-atsc-rescan.sh
#
# Goals:
#  1) Find ATSC OTA network UUID by name
#  2) (Optional) delete existing muxes for that network
#  3) Create muxes for ATSC RF channels (2–36, or 14–36)
#  4) Force scan all muxes
#  5) Poll until scanning settles
#  6) (Optional) trigger service mapping

BASE_URL="${BASE_URL:-http://localhost:9981}"
NET_NAME="${NET_NAME:-ATSC OTA}"

# Assumes fptv.service has been created
# and credentials are stored in /etc/fptv-tvheadend-api.env
# See README.md for more information.
TVH_USER="${TVH_USER:-}"
TVH_PASS="${TVH_PASS:-}"

# Alternatively, use ~/.netrc and set USE_NETRC=1
USE_NETRC="${USE_NETRC:-0}"

# What RF range to add:
# Post-repack, most US markets are UHF; start with 14–36 for speed, or 2–36 for completeness.
RF_START="${RF_START:-14}"
RF_END="${RF_END:-36}"

# Whether to delete existing muxes for this network first:
WIPE_EXISTING_MUXES="${WIPE_EXISTING_MUXES:-0}"

# Modulation string as tvheadend reports it for ATSC:
MODULATION="${MODULATION:-VSB/8}"

# Polling
SLEEP_SECS="${SLEEP_SECS:-2}"
TIMEOUT_SECS="${TIMEOUT_SECS:-600}" # 10 minutes

curl_tvh() {
  # All API calls are Digest-auth’d for your setup.
  if [[ "$USE_NETRC" == "1" ]]; then
    curl -sS --digest --netrc "$@"
  else
    [[ -n "$TVH_USER" && -n "$TVH_PASS" ]] || {
      echo "Set TVH_USER and TVH_PASS, or set USE_NETRC=1 and configure ~/.netrc" >&2
      exit 2
    }
    curl -sS --digest -u "${TVH_USER}:${TVH_PASS}" "$@"
  fi
}

rf_to_freq_hz() {
  local rf="$1"
  if (( rf >= 14 )); then
    echo $((473000000 + (rf - 14) * 6000000))
  elif (( rf >= 7 )); then
    echo $((177000000 + (rf - 7) * 6000000))
  else
    case "$rf" in
      2) echo 57000000 ;;
      3) echo 63000000 ;;
      4) echo 69000000 ;;
      5) echo 79000000 ;;
      6) echo 85000000 ;;
      *) return 1 ;;
    esac
  fi
}

require_jq() {
  command -v jq >/dev/null || { echo "jq is required (sudo apt install jq)"; exit 2; }
}

get_network_uuid() {
  # tvheadend grid endpoints usually return { entries: [...] }
  # Fields vary slightly by version; try common ones.
  curl_tvh "${BASE_URL}/api/mpegts/network/grid?limit=9999" | \
    jq -r --arg name "$NET_NAME" '
      .entries[]
      | select((.networkname? == $name) or (.name? == $name))
      | .uuid
    ' | head -n 1
}

list_muxes_for_network() {
  local net_uuid="$1"
  curl_tvh "${BASE_URL}/api/mpegts/mux/grid?limit=99999" | \
    jq -r --arg net "$net_uuid" '
      .entries[]
      | select(.network_uuid? == $net or .network? == $net)
      | .uuid
    '
}

delete_mux_uuid() {
  local mux_uuid="$1"
  # Some builds expose /api/idnode/delete, others /api/mpegts/mux/delete.
  # Try idnode/delete first; fall back if needed.
  if curl_tvh -o /dev/null -w "%{http_code}" -X POST \
      --data-urlencode "uuid=${mux_uuid}" \
      "${BASE_URL}/api/idnode/delete" | grep -q '^200$'; then
    return 0
  fi

  # Fallback:
  curl_tvh -o /dev/null -X POST \
    --data-urlencode "uuid=${mux_uuid}" \
    "${BASE_URL}/api/mpegts/mux/delete" || true
}

create_mux_atsc() {
  local net_uuid="$1"
  local freq_hz="$2"

  # tvheadend 4.3 commonly accepts mpegts/mux/create with class inferred,
  # but some builds require an explicit "class".
  #
  # We'll try without class first, then fall back to a class-discovery approach.
  local rc
  rc="$(curl_tvh -o /dev/null -w "%{http_code}" -X POST \
    --data-urlencode "network_uuid=${net_uuid}" \
    --data-urlencode "frequency=${freq_hz}" \
    --data-urlencode "modulation=${MODULATION}" \
    "${BASE_URL}/api/mpegts/mux/create")"

  if [[ "$rc" == "200" ]]; then
    return 0
  fi

  # Fallback: attempt to discover an ATSC-T mux class.
  # (Class names vary; we search for something containing 'atsc' and 't'.)
  local mux_class
  mux_class="$(
    curl_tvh "${BASE_URL}/api/mpegts/mux/class" | \
      jq -r '
        .entries? // .[]? // empty
        | .class? // .id? // empty
      ' | grep -i atsc | grep -i -E '(^|[^a-z])t([^a-z]|$)' | head -n 1 || true
  )"

  if [[ -z "$mux_class" ]]; then
    echo "Could not auto-detect ATSC-T mux class; mpegts/mux/create returned HTTP ${rc}" >&2
    echo "Tip: open ${BASE_URL}/api/mpegts/mux/class and look for an ATSC-T class; then set MUX_CLASS env var." >&2
    return 1
  fi

  curl_tvh -o /dev/null -X POST \
    --data-urlencode "class=${mux_class}" \
    --data-urlencode "network_uuid=${net_uuid}" \
    --data-urlencode "frequency=${freq_hz}" \
    --data-urlencode "modulation=${MODULATION}" \
    "${BASE_URL}/api/mpegts/mux/create" >/dev/null
}

force_scan_mux() {
  local mux_uuid="$1"

  # Many builds support /api/mpegts/mux/scan (or /api/mpegts/mux/rescan).
  # If that 404s, fallback to idnode/save to set scan_state to PENDING.
  local code
  code="$(curl_tvh -o /dev/null -w "%{http_code}" -X POST \
    --data-urlencode "uuid=${mux_uuid}" \
    "${BASE_URL}/api/mpegts/mux/scan")" || true

  if [[ "$code" == "200" ]]; then
    return 0
  fi

  # Fallback: set scan_state via idnode/save.
  # Some builds use strings like "PENDING", others use integers. We'll try string first.
  code="$(curl_tvh -o /dev/null -w "%{http_code}" -X POST \
    --data-urlencode "uuid=${mux_uuid}" \
    --data-urlencode "scan_state=PENDING" \
    "${BASE_URL}/api/idnode/save")" || true

  if [[ "$code" == "200" ]]; then
    return 0
  fi

  # Last fallback: numeric scan_state=1 (common for PENDING)
  curl_tvh -o /dev/null -X POST \
    --data-urlencode "uuid=${mux_uuid}" \
    --data-urlencode "scan_state=1" \
    "${BASE_URL}/api/idnode/save" >/dev/null || true
}

count_mux_states() {
  local net_uuid="$1"
  # Returns counts of ACTIVE / PENDING / FAIL / OK / IDLE (as seen in your build).
  curl_tvh "${BASE_URL}/api/mpegts/mux/grid?limit=99999" | \
    jq -r --arg net "$net_uuid" '
      [ .entries[]
        | select(.network_uuid? == $net or .network? == $net)
        | (.scan_state? // .scan_status? // "UNKNOWN")
      ]
      | {
          ACTIVE:  (map(select(. == "ACTIVE"))  | length),
          PENDING: (map(select(. == "PENDING")) | length),
          OK:      (map(select(. == "OK"))      | length),
          FAIL:    (map(select(. == "FAIL"))    | length),
          IDLE:    (map(select(. == "IDLE"))    | length),
          TOTAL:   length
        }
    '
}

start_service_mapping() {
  # Endpoint names can vary slightly; try common one first.
  local code
  code="$(curl_tvh -o /dev/null -w "%{http_code}" -X POST \
    "${BASE_URL}/api/service/mapper/start")" || true
  [[ "$code" == "200" ]] && return 0

  # Fallback:
  curl_tvh -o /dev/null -X POST "${BASE_URL}/api/service/mapper/map" >/dev/null || true
}

main() {
  require_jq

  echo "Finding network UUID for: ${NET_NAME}"
  local net_uuid
  net_uuid="$(get_network_uuid)"
  [[ -n "$net_uuid" ]] || { echo "Network not found: ${NET_NAME}" >&2; exit 1; }
  echo "Network UUID: ${net_uuid}"

  if [[ "$WIPE_EXISTING_MUXES" == "1" ]]; then
    echo "Wiping existing muxes for network..."
    mapfile -t muxes < <(list_muxes_for_network "$net_uuid")
    for u in "${muxes[@]}"; do
      delete_mux_uuid "$u"
    done
    echo "Deleted ${#muxes[@]} muxes."
  fi

  echo "Creating ATSC muxes RF ${RF_START}..${RF_END} (modulation=${MODULATION})..."
  for ((rf=RF_START; rf<=RF_END; rf++)); do
    freq="$(rf_to_freq_hz "$rf")" || continue
    echo "  RF ${rf} -> ${freq} Hz"
    create_mux_atsc "$net_uuid" "$freq" || true
  done

  echo "Forcing scan on all muxes in network..."
  mapfile -t muxes < <(list_muxes_for_network "$net_uuid")
  for u in "${muxes[@]}"; do
    force_scan_mux "$u"
  done
  echo "Requested scan for ${#muxes[@]} muxes."

  echo "Polling scan progress (timeout ${TIMEOUT_SECS}s)..."
  local start now elapsed
  start="$(date +%s)"
  while true; do
    now="$(date +%s)"
    elapsed=$((now - start))
    if (( elapsed > TIMEOUT_SECS )); then
      echo "Timed out waiting for scan to settle." >&2
      count_mux_states "$net_uuid"
      exit 1
    fi

    states="$(count_mux_states "$net_uuid")"
    echo "$states"

    active="$(echo "$states" | jq -r '.ACTIVE + .PENDING')"
    if [[ "$active" == "0" ]]; then
      break
    fi
    sleep "$SLEEP_SECS"
  done

  echo "Scan settled."
  echo "Optionally starting service mapping..."
  start_service_mapping || true
  echo "Done."
}

main "$@"

