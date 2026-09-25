#!/usr/bin/env bash
#
# Install Troika D Lite on Ubuntu 24.04 LTS (and newer).
#
# This script is intentionally conservative: it installs packages through apt
# and puts a small launcher in ~/.local/bin. It does not use sudo for anything
# except the apt install step, and it never touches your recordings folder.
#
# Usage:
#   ./scripts/install.sh            # install dependencies and the launcher
#   ./scripts/install.sh --deps     # install apt dependencies only
#   ./scripts/install.sh --uninstall
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
APPLICATIONS_DIR="${HOME}/.local/share/applications"

# Packages Troika D Lite needs at runtime. Every one of them is in the standard
# Ubuntu archive, so no third-party repository is required.
APT_PACKAGES=(
  python3
  python3-gi
  python3-gi-cairo
  python3-dbus
  gir1.2-gtk-4.0
  gstreamer1.0-tools
  gstreamer1.0-plugins-base
  gstreamer1.0-plugins-good
  gstreamer1.0-plugins-bad
  gstreamer1.0-plugins-ugly
  gstreamer1.0-pipewire
  pipewire
  wireplumber
  xdg-desktop-portal
  pulseaudio-utils
)

log() { printf '\n==> %s\n' "$*"; }

install_dependencies() {
  log "Installing runtime dependencies with apt"
  sudo apt update
  sudo apt install -y "${APT_PACKAGES[@]}"

  if ! dpkg -l xdg-desktop-portal-gnome xdg-desktop-portal-kde \
      xdg-desktop-portal-wlr xdg-desktop-portal-hyprland >/dev/null 2>&1; then
    cat >&2 <<'WARN'

WARNING: no Wayland desktop portal backend was detected.
Screen recording needs one of:
  xdg-desktop-portal-gnome   (Ubuntu/GNOME default)
  xdg-desktop-portal-kde     (KDE Plasma)
  xdg-desktop-portal-wlr     (wlroots: Sway, River, ...)
  xdg-desktop-portal-hyprland

On stock Ubuntu 24.04 the GNOME backend is already present.
WARN
  fi
}

install_launcher() {
  log "Installing the launcher into ${BIN_DIR}"
  mkdir -p "${BIN_DIR}" "${APPLICATIONS_DIR}"

  ln -sf "${REPO_ROOT}/bin/troika-d-lite" "${BIN_DIR}/troika-d-lite"
  ln -sf "${REPO_ROOT}/bin/troika-selftest" "${BIN_DIR}/troika-selftest"

  # The desktop entry is copied rather than linked so the Exec= line resolves
  # through PATH like any other installed application.
  install -m 0644 "${REPO_ROOT}/data/troika-d-lite.desktop" \
    "${APPLICATIONS_DIR}/troika-d-lite.desktop"
  update-desktop-database "${APPLICATIONS_DIR}" >/dev/null 2>&1 || true

  case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *)
      cat >&2 <<EOF

NOTE: ${BIN_DIR} is not on your PATH. Add it with:

    echo 'export PATH="\$HOME/.local/bin:\$PATH"' >> ~/.bashrc
    export PATH="\$HOME/.local/bin:\$PATH"
EOF
      ;;
  esac
}

uninstall() {
  log "Removing the launcher and desktop entry"
  rm -f "${BIN_DIR}/troika-d-lite" "${BIN_DIR}/troika-selftest"
  rm -f "${APPLICATIONS_DIR}/troika-d-lite.desktop"
  update-desktop-database "${APPLICATIONS_DIR}" >/dev/null 2>&1 || true
  cat <<'EOF'

Troika D Lite's launcher has been removed. Recordings are untouched.
System packages installed by --deps were left in place; remove them with apt
if you no longer need them.
EOF
}

case "${1:-}" in
  --uninstall)
    uninstall
    ;;
  --deps)
    install_dependencies
    ;;
  "")
    install_dependencies
    install_launcher
    log "Done. Launch the recorder with: troika-d-lite"
    ;;
  *)
    echo "Unknown option: $1" >&2
    echo "Usage: $0 [--deps|--uninstall]" >&2
    exit 2
    ;;
esac
