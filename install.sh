#!/usr/bin/env sh
set -eu

repo="owndiff/own-your-diff"
version="${OWNDIFF_VERSION:-latest}"
bin_dir="${OWNDIFF_BIN_DIR:-/usr/local/bin}"
binary_name="${OWNDIFF_BINARY_NAME:-owndiff}"
local_asset="${OWNDIFF_LOCAL_ASSET:-}"

usage() {
  cat <<'EOF'
Install OwnDiff's standalone executable for macOS or Linux.

Environment:
  OWNDIFF_VERSION=v0.0.1        Install a specific release tag. Default: latest.
  OWNDIFF_BIN_DIR=$HOME/bin     Install directory. Default: /usr/local/bin.
  OWNDIFF_EXPECTED_SHA256=...   Expected SHA-256 digest. Overrides checksum file lookup.
  OWNDIFF_LOCAL_ASSET=/tmp/owndiff
                                Install this local executable path. Intended for CI/test harnesses.
  OWNDIFF_LOCAL_CHECKSUM=/tmp/owndiff.sha256
                                Local SHA-256 checksum file. Default: ${OWNDIFF_LOCAL_ASSET}.sha256.
  OWNDIFF_DRY_RUN=1             Print detected asset and source without installing.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

case "$(uname -s)" in
  Darwin) os="darwin" ;;
  Linux) os="linux" ;;
  *)
    echo "error: unsupported OS: $(uname -s). OwnDiff releases support macOS and Linux." >&2
    exit 2
    ;;
esac

case "$(uname -m)" in
  x86_64|amd64) arch="x86_64" ;;
  arm64|aarch64) arch="arm64" ;;
  *)
    echo "error: unsupported architecture: $(uname -m). OwnDiff releases support x86_64 and arm64." >&2
    exit 2
    ;;
esac

asset="owndiff-${os}-${arch}"
if [ -n "$local_asset" ]; then
  case "$local_asset" in
    /*) ;;
    *)
      echo "error: OWNDIFF_LOCAL_ASSET must be an absolute file path." >&2
      exit 2
      ;;
  esac
  if [ ! -f "$local_asset" ]; then
    echo "error: OWNDIFF_LOCAL_ASSET does not exist: ${local_asset}" >&2
    exit 2
  fi
  url=""
elif [ "$version" = "latest" ]; then
  url="https://github.com/${repo}/releases/latest/download/${asset}"
else
  case "$version" in
    v[0123456789]*)
      case "$version" in
        *[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-]*)
          echo "error: unsupported OWNDIFF_VERSION: ${version}" >&2
          exit 2
          ;;
      esac
      ;;
    *)
      echo "error: unsupported OWNDIFF_VERSION: ${version}. Use latest or a v-prefixed release tag." >&2
      exit 2
      ;;
  esac
  url="https://github.com/${repo}/releases/download/${version}/${asset}"
fi

checksum_url=""
checksum_path="${OWNDIFF_LOCAL_CHECKSUM:-}"
if [ -n "$local_asset" ]; then
  if [ -z "${OWNDIFF_EXPECTED_SHA256:-}" ]; then
    if [ -z "$checksum_path" ]; then
      checksum_path="${local_asset}.sha256"
    fi
    case "$checksum_path" in
      /*) ;;
      *)
        echo "error: OWNDIFF_LOCAL_CHECKSUM must be an absolute file path." >&2
        exit 2
        ;;
    esac
    if [ ! -f "$checksum_path" ]; then
      echo "error: checksum file does not exist: ${checksum_path}" >&2
      exit 2
    fi
  fi
else
  checksum_url="${url}.sha256"
fi

target="${bin_dir}/${binary_name}"

if [ "${OWNDIFF_DRY_RUN:-}" = "1" ]; then
  echo "asset=${asset}"
  if [ -n "$local_asset" ]; then
    echo "local_asset=${local_asset}"
  else
    echo "url=${url}"
  fi
  if [ -n "${OWNDIFF_EXPECTED_SHA256:-}" ]; then
    echo "checksum=provided"
  elif [ -n "$local_asset" ]; then
    echo "checksum_path=${checksum_path}"
  else
    echo "checksum_url=${checksum_url}"
  fi
  echo "target=${target}"
  exit 0
fi

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT INT TERM
tmp_file="${tmp_dir}/${binary_name}"
tmp_checksum="${tmp_dir}/${binary_name}.sha256"

download() {
  download_url="$1"
  output_path="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fL "$download_url" -o "$output_path"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$output_path" "$download_url"
  else
    echo "error: curl or wget is required to download ${download_url}" >&2
    exit 2
  fi
}

compute_sha256() {
  input_path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$input_path" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$input_path" | awk '{print $1}'
  else
    echo "error: sha256sum or shasum is required to verify the OwnDiff download." >&2
    exit 2
  fi
}

normalize_sha256() {
  printf '%s' "$1" | awk '{print $1}' | tr 'A-F' 'a-f'
}

if [ -n "${OWNDIFF_EXPECTED_SHA256:-}" ]; then
  expected_sha256="$(normalize_sha256 "$OWNDIFF_EXPECTED_SHA256")"
elif [ -n "$local_asset" ]; then
  expected_sha256="$(normalize_sha256 "$(cat "$checksum_path")")"
else
  download "$checksum_url" "$tmp_checksum"
  expected_sha256="$(normalize_sha256 "$(cat "$tmp_checksum")")"
fi

case "$expected_sha256" in
  ""|*[!0123456789abcdef]*)
    echo "error: invalid SHA-256 checksum for ${asset}." >&2
    exit 2
    ;;
esac

if [ "${#expected_sha256}" -ne 64 ]; then
  echo "error: invalid SHA-256 checksum length for ${asset}." >&2
  exit 2
fi

if [ -n "$local_asset" ]; then
  cp "$local_asset" "$tmp_file"
else
  download "$url" "$tmp_file"
fi

actual_sha256="$(normalize_sha256 "$(compute_sha256 "$tmp_file")")"
if [ "$actual_sha256" != "$expected_sha256" ]; then
  echo "error: checksum verification failed for ${asset}." >&2
  echo "expected: ${expected_sha256}" >&2
  echo "actual:   ${actual_sha256}" >&2
  exit 2
fi

chmod 0755 "$tmp_file"

if { [ -d "$bin_dir" ] || mkdir -p "$bin_dir" 2>/dev/null; } && [ -w "$bin_dir" ]; then
  install -m 0755 "$tmp_file" "$target"
elif command -v sudo >/dev/null 2>&1; then
  sudo mkdir -p "$bin_dir"
  sudo install -m 0755 "$tmp_file" "$target"
else
  echo "error: ${bin_dir} is not writable and sudo is not available." >&2
  echo "Set OWNDIFF_BIN_DIR to a writable directory on PATH and rerun this installer." >&2
  exit 2
fi

"$target" --version
echo "OwnDiff installed at ${target}"
