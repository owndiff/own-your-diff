#!/usr/bin/env bash
set -euo pipefail

asset="${1:-}"
if [ -z "$asset" ]; then
  echo "usage: scripts/build_linux_release_binary.sh <asset-name>" >&2
  exit 2
fi

python_version="${OWNDIFF_PYTHON_VERSION:-3.11.15}"
python_prefix="${OWNDIFF_PYTHON_PREFIX:-/opt/owndiff-python-${python_version}}"
build_jobs="${OWNDIFF_BUILD_JOBS:-2}"
repo_root="${GITHUB_WORKSPACE:-$(git rev-parse --show-toplevel)}"
log_dir="${OWNDIFF_BUILD_LOG_DIR:-${RUNNER_TEMP:-/tmp}/owndiff-linux-build-logs}"
build_dir=""

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "Linux release build failed; logs are in ${log_dir}" >&2
  fi
  if [ -n "$build_dir" ]; then
    rm -rf "$build_dir"
  fi
  exit "$status"
}
trap cleanup EXIT

export DEBIAN_FRONTEND="${DEBIAN_FRONTEND:-noninteractive}"
mkdir -p "$log_dir"

echo "Installing Ubuntu build dependencies..."
apt-get update >"${log_dir}/apt-update.log" 2>&1
apt-get install -y --no-install-recommends \
  build-essential \
  ca-certificates \
  curl \
  git \
  libbz2-dev \
  libffi-dev \
  liblzma-dev \
  libncursesw5-dev \
  libreadline-dev \
  libsqlite3-dev \
  libssl-dev \
  tk-dev \
  uuid-dev \
  xz-utils \
  zlib1g-dev \
  >"${log_dir}/apt-install.log" 2>&1

if [ ! -x "${python_prefix}/bin/python3.11" ]; then
  echo "Building Python ${python_version} with shared-library support..."
  build_dir="$(mktemp -d)"
  cd "$build_dir"
  curl -fsSLO "https://www.python.org/ftp/python/${python_version}/Python-${python_version}.tgz"
  tar -xzf "Python-${python_version}.tgz"
  cd "Python-${python_version}"
  ./configure --prefix="$python_prefix" --enable-shared --with-ensurepip=install >"${log_dir}/python-configure.log" 2>&1
  make -j "$build_jobs" >"${log_dir}/python-make.log" 2>&1
  make install >"${log_dir}/python-install.log" 2>&1
else
  echo "Using existing Python ${python_version} at ${python_prefix}."
fi

cd "$repo_root"
export LD_LIBRARY_PATH="${python_prefix}/lib:${LD_LIBRARY_PATH:-}"

echo "Installing OwnDiff build dependencies..."
"${python_prefix}/bin/python3.11" -m venv .venv-linux-release
. .venv-linux-release/bin/activate

python -m pip install --upgrade pip >"${log_dir}/pip-upgrade.log" 2>&1
python -m pip install -e '.[build]' >"${log_dir}/pip-install.log" 2>&1

echo "Running OpenClaw release validation..."
python scripts/ci_openclaw_flow.py

echo "Building ${asset}..."
python scripts/build_binary.py --name "$asset"

echo "Smoke testing ${asset}..."
"./dist/${asset}" --version
"./dist/${asset}" run --help
"./dist/${asset}" install-agent-rules --help
"./dist/${asset}" quiz-web --help

echo "Writing ${asset}.sha256..."
sha256sum "./dist/${asset}" > "./dist/${asset}.sha256"
