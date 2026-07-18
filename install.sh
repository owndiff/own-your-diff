#!/usr/bin/env sh
set -eu

repo="${OWNDIFF_REPO:-owndiff/own-your-diff}"
version="${OWNDIFF_VERSION:-latest}"
bin_dir="${OWNDIFF_BIN_DIR:-/usr/local/bin}"
binary_name="${OWNDIFF_BINARY_NAME:-owndiff}"

usage() {
  cat <<'EOF'
Install OwnDiff's standalone executable for macOS or Linux.

Environment:
  OWNDIFF_VERSION=v0.0.1        Install a specific release tag. Default: latest.
  OWNDIFF_BIN_DIR=$HOME/bin     Install directory. Default: /usr/local/bin.
  OWNDIFF_REPO=owner/repo       Release repository. Default: owndiff/own-your-diff.
  OWNDIFF_DOWNLOAD_URL=file:///tmp/owndiff
                                Override the release URL. Intended for CI/test harnesses.
  OWNDIFF_DRY_RUN=1             Print detected asset and URL without installing.
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
if [ -n "${OWNDIFF_DOWNLOAD_URL:-}" ]; then
  url="$OWNDIFF_DOWNLOAD_URL"
elif [ "$version" = "latest" ]; then
  url="https://github.com/${repo}/releases/latest/download/${asset}"
else
  url="https://github.com/${repo}/releases/download/${version}/${asset}"
fi

target="${bin_dir}/${binary_name}"

if [ "${OWNDIFF_DRY_RUN:-}" = "1" ]; then
  echo "asset=${asset}"
  echo "url=${url}"
  echo "target=${target}"
  exit 0
fi

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT INT TERM
tmp_file="${tmp_dir}/${binary_name}"

if command -v curl >/dev/null 2>&1; then
  curl -fL "$url" -o "$tmp_file"
elif command -v wget >/dev/null 2>&1; then
  wget -O "$tmp_file" "$url"
else
  echo "error: curl or wget is required to download ${url}" >&2
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
