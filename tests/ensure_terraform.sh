#!/usr/bin/env bash
# Ensures a real HashiCorp Terraform CLI (>= 1.6) is installed in ~/.local/bin
# and shadows Google Cloud Shell's `/usr/bin/terraform` stub wrapper.

ensure_terraform() {
  export PATH="${HOME}/.local/bin:${PATH}"
  hash -r 2>/dev/null || true

  if command -v terraform >/dev/null 2>&1 && terraform version 2>/dev/null | grep -q '^Terraform v'; then
    return 0
  fi

  echo "    Terraform CLI not active (Cloud Shell stub detected); installing Terraform into ${HOME}/.local/bin..."
  mkdir -p "${HOME}/.local/bin"

  local os arch tf_version tmp_zip
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"
  case "${arch}" in
    x86_64|amd64) arch="amd64" ;;
    aarch64|arm64) arch="arm64" ;;
    *)
      echo "ERROR: Unsupported CPU architecture '${arch}' for automatic Terraform install." >&2
      return 1
      ;;
  esac

  tf_version="$(
    python3 -c '
import json, urllib.request
try:
    with urllib.request.urlopen("https://checkpoint-api.hashicorp.com/v1/check/terraform", timeout=5) as r:
        print(json.load(r)["current_version"])
except Exception:
    print("1.10.5")
'
  )"

  tmp_zip="$(mktemp /tmp/terraform-XXXXXX.zip)"
  curl -fsSL "https://releases.hashicorp.com/terraform/${tf_version}/terraform_${tf_version}_${os}_${arch}.zip" -o "${tmp_zip}"
  python3 - "${tmp_zip}" "${HOME}/.local/bin" <<'PY'
import os, sys, zipfile
zip_path, dest_dir = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zip_path) as zf:
    zf.extract("terraform", dest_dir)
os.chmod(os.path.join(dest_dir, "terraform"), 0o755)
PY
  rm -f "${tmp_zip}"

  if [[ -f "${HOME}/.bashrc" ]] && ! grep -q '\.local/bin' "${HOME}/.bashrc" 2>/dev/null; then
    printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "${HOME}/.bashrc"
  fi

  hash -r 2>/dev/null || true
  if ! terraform version 2>/dev/null | grep -q '^Terraform v'; then
    echo "ERROR: Terraform installation into ${HOME}/.local/bin/terraform failed verification." >&2
    return 1
  fi
  echo "    Installed $(terraform version | head -n 1)"
}
