#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Greffier uninstaller: removes what packaging/install.sh installed for the
# current user. Self-contained on purpose: install.sh copies it to
# ~/.local/share/greffier/uninstall.sh so that it works without the checkout.
#
#   uninstall.sh            remove the program, keep settings, models, history
#   uninstall.sh --purge    also remove settings, models, caches and history
#
# Never uses sudo: system packages installed with dnf are left alone.

set -Eeuo pipefail

# Identifiers: keep in sync with greffier/const.py and packaging/install.sh.
APP_ID="io.github.greffier.Greffier"
DAEMON_ID="${APP_ID}.Daemon"
DBUS_NAME="${DAEMON_ID}"
DBUS_PATH="/io/github/greffier/Greffier/Daemon"
DBUS_IFACE="${APP_ID}.Daemon1"
SYSTEMD_UNIT="app-${DAEMON_ID}.service"
EXTENSION_UUID="greffier@greffier.github.io"
# GNOME custom shortcut created by the daemon (greffier.desktop.HotkeyManager).
MEDIA_KEYS_SCHEMA="org.gnome.settings-daemon.plugins.media-keys"
SHORTCUT_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/greffier/"
WRAPPER_MARKER="greffier-install-wrapper"
ENTRY_POINTS=(greffier greffier-daemon greffierctl)

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != dumb ]; then
  C_BOLD=$'\e[1m' C_BLUE=$'\e[34m' C_GREEN=$'\e[32m' C_YELLOW=$'\e[33m' C_DIM=$'\e[2m' C_RESET=$'\e[0m'
else
  C_BOLD="" C_BLUE="" C_GREEN="" C_YELLOW="" C_DIM="" C_RESET=""
fi
if [ -t 2 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != dumb ]; then
  E_RED=$'\e[1;31m' E_RESET=$'\e[0m'
else
  E_RED="" E_RESET=""
fi
step() { printf '\n%s==> %s%s\n' "${C_BOLD}${C_BLUE}" "$*" "${C_RESET}"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn() { printf '  %s⚠ %s%s\n' "${C_YELLOW}" "$*" "${C_RESET}"; }
die()  { printf '%s✗ %s%s\n' "${E_RED}" "$*" "${E_RESET}" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
# shellcheck disable=SC2317  # invoked through trap
on_unexpected_error() {
  printf '%s✗ Erreur inattendue (ligne %s) : %s%s\n' "${E_RED}" "$1" "$2" "${E_RESET}" >&2
}
trap 'on_unexpected_error "$LINENO" "$BASH_COMMAND"' ERR

usage() {
  cat <<EOF
Utilisation : uninstall.sh [options]

Désinstalle Greffier pour l'utilisateur courant : service, commandes, extension
GNOME Shell, raccourci clavier et environnement Python. Par défaut, vos
réglages, modèles téléchargés et l'historique sont conservés.

Options :
      --purge     supprimer aussi les réglages, les modèles, le cache et l'historique
  -y, --yes       ne poser aucune question
  -n, --dry-run   afficher ce qui serait fait, sans rien supprimer
  -h, --help      afficher cette aide

Les paquets système (python3-gobject, gtk4…) ne sont jamais désinstallés.
EOF
}

PURGE=0 YES=0 DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --purge) PURGE=1 ;;
    -y|--yes) YES=1 ;;
    -n|--dry-run) DRY_RUN=1 ;;
    *) printf '%s✗ Option inconnue : %s%s\n\n' "${E_RED}" "$1" "${E_RESET}" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ -z "${HOME:-}" ]; then die "La variable HOME n'est pas définie."; fi
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
  die "N'utilisez pas sudo : relancez « $0 » en tant qu'utilisateur normal."
fi
xdg_dir() { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s' "$2" ;; esac; }
DATA_HOME="$(xdg_dir "${XDG_DATA_HOME:-}" "${HOME}/.local/share")"
CONFIG_HOME="$(xdg_dir "${XDG_CONFIG_HOME:-}" "${HOME}/.config")"
CACHE_HOME="$(xdg_dir "${XDG_CACHE_HOME:-}" "${HOME}/.cache")"
STATE_HOME="$(xdg_dir "${XDG_STATE_HOME:-}" "${HOME}/.local/state")"
BIN_DIR="${HOME}/.local/bin"
APP_DATA="${DATA_HOME}/greffier"
VENV="${APP_DATA}/venv"
MANIFEST="${APP_DATA}/installed-files.txt"
UNIT_DIR="${CONFIG_HOME}/systemd/user"
DBUS_FILE="${DATA_HOME}/dbus-1/services/${DBUS_NAME}.service"
APPS_DIR="${DATA_HOME}/applications"
ICONS_DIR="${DATA_HOME}/icons/hicolor"
EXT_DIR="${DATA_HOME}/gnome-shell/extensions/${EXTENSION_UUID}"
# User data (docs/ARCHITECTURE.md §7), removed only with --purge.
PURGE_DIRS=("${CONFIG_HOME}/greffier" "${APP_DATA}" "${CACHE_HOME}/greffier" "${STATE_HOME}/greffier")
PY="$(command -v python3 || true)"

run() {
  if [ "${DRY_RUN}" = 1 ]; then
    printf '    %s[simulation]%s' "${C_DIM}" "${C_RESET}"
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}
remove() {  # remove PATH… (files, symlinks or directories), quietly skipping missing ones
  local p
  for p in "$@"; do
    if [ -e "${p}" ] || [ -L "${p}" ]; then run rm -rf -- "${p}"; fi
  done
}
session_bus_ok() { [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] || [ -S "${XDG_RUNTIME_DIR:-/nonexistent}/bus" ]; }
systemd_user_ok() { have systemctl && systemctl --user show-environment >/dev/null 2>&1; }
size_of() { du -sh -- "$1" 2>/dev/null | cut -f1 || true; }

# Remove ITEM from a GVariant string array such as "['a', 'b']" or "@as []".
gv_list_remove() {
  if [ -n "${PY}" ]; then
    "${PY}" - "$1" "$2" <<'PYEOF'
import ast
import sys

item, current = sys.argv[1:3]
current = current.strip()
if current.startswith("@as"):
    current = current[3:].strip()
values = [v for v in ast.literal_eval(current or "[]") if v != item]
quoted = ("'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'" for v in values)
print("[" + ", ".join(quoted) + "]")
PYEOF
  else
    printf '%s' "$2" | sed -e "s|, *'$1'||g" -e "s|'$1', *||g" -e "s|'$1'||g"
  fi
}

# --------------------------------------------------------------------------- #
# Confirmation
# --------------------------------------------------------------------------- #
printf '%sDésinstallation de Greffier%s\n' "${C_BOLD}" "${C_RESET}"
if [ "${DRY_RUN}" = 1 ]; then
  printf '%sMode simulation : rien ne sera supprimé.%s\n' "${C_YELLOW}" "${C_RESET}"
fi
info "Seront supprimés : le service, les commandes, l'extension GNOME Shell, le raccourci"
info "clavier « Greffier » et l'environnement Python (${VENV})."
if [ "${PURGE}" = 1 ]; then
  printf '\n  %s--purge : ces données seront aussi effacées définitivement :%s\n' "${C_BOLD}" "${C_RESET}"
  for d in "${PURGE_DIRS[@]}"; do
    if [ -e "${d}" ]; then info "${d}  ($(size_of "${d}"))"; fi
  done
  if [ -d "${APP_DATA}/models" ]; then
    info "dont modèles téléchargés : $(size_of "${APP_DATA}/models")"
  fi
else
  info "Vos réglages, modèles téléchargés et l'historique sont conservés (--purge pour les effacer)."
fi
if [ -n "${GREFFIER_HOME:-}${GREFFIER_MODELS_DIR:-}" ]; then
  warn "GREFFIER_HOME/GREFFIER_MODELS_DIR sont définis : les données qui s'y trouvent ne sont pas touchées."
fi
if [ "${YES}" = 0 ] && [ "${DRY_RUN}" = 0 ]; then
  [ -t 0 ] || die "Pas de terminal pour confirmer : relancez avec --yes."
  printf '\n  Continuer ? [o/N] '
  read -r answer || answer=""
  case "${answer}" in [oOyY]*) ;; *) echo "  Abandon, rien n'a été supprimé."; exit 0 ;; esac
fi

# --------------------------------------------------------------------------- #
# 1. Stop the service and prevent re-activation
# --------------------------------------------------------------------------- #
step "Arrêt du service"
remove "${DBUS_FILE}"
if session_bus_ok && have gdbus; then
  run gdbus call --session --timeout 5 --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.ReloadConfig >/dev/null 2>&1 || true
fi
if systemd_user_ok; then
  run systemctl --user disable --now --quiet "${SYSTEMD_UNIT}" 2>/dev/null || true
fi
# Also covers a daemon started by hand (make run-daemon) or a missing systemd.
if session_bus_ok && have gdbus && [ "${DRY_RUN}" = 0 ]; then
  owner="$(gdbus call --session --timeout 5 --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.NameHasOwner "${DBUS_NAME}" 2>/dev/null || true)"
  case "${owner}" in
    *true*) gdbus call --session --timeout 5 --dest "${DBUS_NAME}" --object-path "${DBUS_PATH}" \
              --method "${DBUS_IFACE}.Quit" >/dev/null 2>&1 || true ;;
  esac
fi
for wants in "${UNIT_DIR}"/*.wants/"${SYSTEMD_UNIT}"; do
  remove "${wants}"
done
remove "${UNIT_DIR}/${SYSTEMD_UNIT}"
if systemd_user_ok; then
  run systemctl --user daemon-reload || true
  run systemctl --user reset-failed "${SYSTEMD_UNIT}" >/dev/null 2>&1 || true
fi
ok "Service arrêté et désactivé."

# --------------------------------------------------------------------------- #
# 2. GNOME custom shortcut
# --------------------------------------------------------------------------- #
step "Raccourci clavier"
if have gsettings && gsettings writable "${MEDIA_KEYS_SCHEMA}" custom-keybindings >/dev/null 2>&1; then
  current="$(gsettings get "${MEDIA_KEYS_SCHEMA}" custom-keybindings 2>/dev/null || echo '[]')"
  case "${current}" in
    *"'${SHORTCUT_PATH}'"*)
      new="$(gv_list_remove "${SHORTCUT_PATH}" "${current}")"
      run gsettings set "${MEDIA_KEYS_SCHEMA}" custom-keybindings "${new}"
      ;;
  esac
  run gsettings reset-recursively "${MEDIA_KEYS_SCHEMA}.custom-keybinding:${SHORTCUT_PATH}" 2>/dev/null || true
  ok "Raccourci « Greffier » supprimé des Paramètres › Clavier."
else
  info "Paramètres GNOME indisponibles (gsettings) : rien à faire."
fi

# --------------------------------------------------------------------------- #
# 3. GNOME Shell extension
# --------------------------------------------------------------------------- #
step "Extension GNOME Shell"
if have gnome-extensions; then
  run gnome-extensions disable "${EXTENSION_UUID}" >/dev/null 2>&1 || true
fi
if have gsettings && gsettings writable org.gnome.shell enabled-extensions >/dev/null 2>&1; then
  current="$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo '[]')"
  case "${current}" in
    *"'${EXTENSION_UUID}'"*)
      run gsettings set org.gnome.shell enabled-extensions "$(gv_list_remove "${EXTENSION_UUID}" "${current}")" ;;
  esac
fi
if [ -d "${EXT_DIR}" ]; then
  remove "${EXT_DIR}"
  ok "Extension désactivée et supprimée (elle reste chargée jusqu'à la prochaine connexion)."
else
  info "Extension non installée."
fi

# --------------------------------------------------------------------------- #
# 4. Program files
# --------------------------------------------------------------------------- #
step "Fichiers du programme"
if [ -f "${MANIFEST}" ]; then
  while IFS= read -r path; do
    case "${path}" in "${HOME}"/*) ;; *) continue ;; esac   # never outside $HOME
    case "${path}" in */../*|*/./*) continue ;; esac
    case "${path}" in "${BIN_DIR}"/*) continue ;; esac      # launchers: checked below
    if [ -f "${path}" ] || [ -L "${path}" ]; then run rm -f -- "${path}"; fi
  done < "${MANIFEST}"
fi
# Known locations, in case the manifest is missing (older or interrupted install).
remove "${APPS_DIR}/${APP_ID}.desktop" "${APPS_DIR}/${DAEMON_ID}.desktop" \
       "${DATA_HOME}/metainfo/${APP_ID}.metainfo.xml" \
       "${CONFIG_HOME}/autostart/${APP_ID}.desktop" "${CONFIG_HOME}/autostart/${DAEMON_ID}.desktop"
for icon in "${ICONS_DIR}"/*/apps/"${APP_ID}".svg "${ICONS_DIR}"/*/apps/"${APP_ID}".png \
            "${ICONS_DIR}"/*/apps/"${APP_ID}"-symbolic.svg; do
  remove "${icon}"
done
for name in "${ENTRY_POINTS[@]}"; do
  launcher="${BIN_DIR}/${name}"
  if [ -L "${launcher}" ]; then
    case "$(readlink -- "${launcher}")" in "${VENV}"/*) remove "${launcher}" ;; esac
  elif [ -f "${launcher}" ]; then
    if grep -qs "${WRAPPER_MARKER}" "${launcher}"; then
      remove "${launcher}"
    else
      warn "${launcher} n'a pas été créé par l'installateur de Greffier : laissé en place."
    fi
  fi
done
remove "${VENV}" "${APP_DATA}/uninstall.sh" "${MANIFEST}"
if [ -d "${APP_DATA}" ] && [ "${DRY_RUN}" = 0 ]; then
  rmdir -- "${APP_DATA}" 2>/dev/null || true   # only if nothing else (models) is left
fi
if have update-desktop-database && [ -d "${APPS_DIR}" ]; then
  run update-desktop-database -q "${APPS_DIR}" || true
fi
if [ -f "${ICONS_DIR}/icon-theme.cache" ] && have gtk-update-icon-cache; then
  run gtk-update-icon-cache -q -t -f "${ICONS_DIR}" || true
fi
ok "Programme, commandes et fichiers d'intégration supprimés."

# --------------------------------------------------------------------------- #
# 5. User data (--purge)
# --------------------------------------------------------------------------- #
if [ "${PURGE}" = 1 ]; then
  step "Données personnelles (--purge)"
  for d in "${PURGE_DIRS[@]}"; do
    if [ -e "${d}" ]; then
      remove "${d}"
      ok "Supprimé : ${d}"
    fi
  done
fi

step "Terminé"
if [ "${DRY_RUN}" = 1 ]; then
  info "Simulation : rien n'a été supprimé."
  exit 0
fi
info "Greffier a été désinstallé."
if [ "${PURGE}" = 0 ]; then
  kept=()
  for d in "${PURGE_DIRS[@]}"; do
    if [ -e "${d}" ]; then kept+=("${d} ($(size_of "${d}"))"); fi
  done
  if [ "${#kept[@]}" -gt 0 ]; then
    info "Conservés (réglages, modèles, historique) :"
    for k in "${kept[@]}"; do info "  ${k}"; done
    info "Pour les effacer aussi : relancez avec --purge."
  fi
fi
info "Les paquets système installés avec dnf (python3-gobject, gtk4…) n'ont pas été touchés."
