#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Greffier installer: per-user installation for GNOME on Wayland (Fedora
# first). Run `packaging/install.sh --help` for the options. Running it again
# upgrades an existing installation in place.
#
# What goes where (details in docs/INSTALL.md):
#   ~/.local/share/greffier/venv                   Python virtualenv (--system-site-packages)
#   ~/.local/bin/{greffier,greffier-daemon,greffierctl}   launchers
#   ~/.config/systemd/user/<SYSTEMD_UNIT>          systemd user unit
#   ~/.local/share/dbus-1/services/<DBUS_NAME>.service    D-Bus activation
#   ~/.local/share/{applications,metainfo,icons/hicolor}  desktop integration
#   ~/.local/share/gnome-shell/extensions/<EXTENSION_UUID>
#   ~/.local/share/greffier/{uninstall.sh,installed-files.txt}
#
# sudo is used for one thing only: `dnf install` of missing system packages,
# after asking (or straight away with --yes).
#
# Environment variables (tests and unusual setups):
#   GREFFIER_PYTHON            interpreter for the virtualenv (default /usr/bin/python3)
#   GREFFIER_INSTALL_SKIP_PIP  "1": create the virtualenv without pip and do not
#                              install the Python package (offline packaging tests)
#   GREFFIER_OS_RELEASE        os-release file to read (default /etc/os-release)
#   NO_COLOR                   disable colours

set -Eeuo pipefail

# --------------------------------------------------------------------------- #
# Identifiers: keep in sync with greffier/const.py (docs/ARCHITECTURE.md §9).
# tests/test_packaging_templates.py checks that they match.
# --------------------------------------------------------------------------- #
APP_ID="io.github.gds_42.Greffier"
DAEMON_ID="${APP_ID}.Daemon"
DBUS_NAME="${DAEMON_ID}"
SYSTEMD_UNIT="app-${DAEMON_ID}.service"
EXTENSION_UUID="greffier@gds-42.github.io"
PROJECT_URL="https://github.com/GDS-42/gnome-wayland-speech-to-text"
DEFAULT_SHORTCUT="Super+Alt+D"

# Fedora packages providing PyGObject, GTK 4, libadwaita, GStreamer (+ GstApp),
# PipeWire capture and the wl-copy clipboard fallback.
FEDORA_PACKAGES=(python3-gobject gtk4 libadwaita gstreamer1 gstreamer1-plugins-base
                 gstreamer1-plugins-good pipewire-gstreamer wl-clipboard)
WRAPPER_MARKER="greffier-install-wrapper"
ENTRY_POINTS=(greffier greffier-daemon greffierctl)

# --------------------------------------------------------------------------- #
# Output helpers (French messages, colours only on a terminal)
# --------------------------------------------------------------------------- #
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

WARNINGS=0
ERRORS=0
step() { printf '\n%s==> %s%s\n' "${C_BOLD}${C_BLUE}" "$*" "${C_RESET}"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn() { WARNINGS=$((WARNINGS + 1)); printf '  %s⚠ %s%s\n' "${C_YELLOW}" "$*" "${C_RESET}"; }
fail() { ERRORS=$((ERRORS + 1)); printf '%s✗ %s%s\n' "${E_RED}" "$*" "${E_RESET}" >&2; }
die()  { printf '%s✗ %s%s\n' "${E_RED}" "$*" "${E_RESET}" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# shellcheck disable=SC2317  # invoked through trap
on_unexpected_error() {
  printf '%s✗ Erreur inattendue (ligne %s) : %s%s\n' "${E_RED}" "$1" "$2" "${E_RESET}" >&2
  printf '  Relancez avec « bash -x %s » pour plus de détails, ou signalez le problème :\n  %s/issues\n' \
    "$0" "${PROJECT_URL}" >&2
}
trap 'on_unexpected_error "$LINENO" "$BASH_COMMAND"' ERR

TMP_DIR=""
# shellcheck disable=SC2317  # invoked through trap
cleanup() { if [ -n "${TMP_DIR}" ]; then rm -rf -- "${TMP_DIR}"; fi; }
trap cleanup EXIT

usage() {
  cat <<EOF
Utilisation : packaging/install.sh [options]

Installe Greffier pour l'utilisateur courant, dans votre dossier personnel.
Aucun droit administrateur n'est nécessaire, sauf pour installer les paquets
système manquants (sudo dnf install, après confirmation).
Relancer le script met à jour une installation existante.

Options :
  -y, --yes          installer sans confirmation les paquets système manquants
      --no-deps      ne pas vérifier ni installer les paquets système
      --no-extension ne pas installer l'extension GNOME Shell
      --no-autostart ne pas lancer Greffier à l'ouverture de session (le service
                     démarre alors à la demande : raccourci, application)
      --model ID     télécharger ce modèle une fois le service démarré
                     (ex. parakeet-tdt-0.6b-v3-int8, voir « greffierctl models list »)
      --dev          installation « éditable » de ce dépôt (développement)
  -n, --dry-run      afficher ce qui serait fait, sans rien modifier
  -h, --help         afficher cette aide

Variables d'environnement :
  GREFFIER_PYTHON=/chemin/python3   interpréteur à utiliser (défaut : /usr/bin/python3)
  GREFFIER_INSTALL_SKIP_PIP=1       ne pas lancer pip (tests hors ligne)
  NO_COLOR=1                        sans couleurs

Désinstallation : packaging/uninstall.sh (ou ~/.local/share/greffier/uninstall.sh)
EOF
}

# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #
YES=0 NO_DEPS=0 NO_EXTENSION=0 NO_AUTOSTART=0 DEV=0 DRY_RUN=0 MODEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -y|--yes) YES=1 ;;
    --no-deps) NO_DEPS=1 ;;
    --no-extension) NO_EXTENSION=1 ;;
    --no-autostart) NO_AUTOSTART=1 ;;
    --dev) DEV=1 ;;
    -n|--dry-run) DRY_RUN=1 ;;
    --model)
      [ $# -ge 2 ] || die "L'option --model attend un identifiant de modèle (voir « greffierctl models list »)."
      MODEL="$2"; shift ;;
    --model=*) MODEL="${1#*=}" ;;
    *) printf '%s✗ Option inconnue : %s%s\n\n' "${E_RED}" "$1" "${E_RESET}" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
if [ -n "${MODEL}" ] && ! [[ "${MODEL}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  die "Identifiant de modèle invalide : « ${MODEL} »."
fi
SKIP_PIP="${GREFFIER_INSTALL_SKIP_PIP:-0}"

# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #
if [ -z "${HOME:-}" ]; then die "La variable HOME n'est pas définie."; fi
xdg_dir() {  # $1 = value of an XDG variable, $2 = default; relative values are ignored (XDG spec)
  case "$1" in /*) printf '%s' "$1" ;; *) printf '%s' "$2" ;; esac
}
SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
DATA_HOME="$(xdg_dir "${XDG_DATA_HOME:-}" "${HOME}/.local/share")"
CONFIG_HOME="$(xdg_dir "${XDG_CONFIG_HOME:-}" "${HOME}/.config")"
BIN_DIR="${HOME}/.local/bin"
APP_DATA="${DATA_HOME}/greffier"
VENV="${APP_DATA}/venv"
VENV_BIN="${VENV}/bin"
MANIFEST="${APP_DATA}/installed-files.txt"
UNIT_DIR="${CONFIG_HOME}/systemd/user"
DBUS_DIR="${DATA_HOME}/dbus-1/services"
APPS_DIR="${DATA_HOME}/applications"
METAINFO_DIR="${DATA_HOME}/metainfo"
ICONS_DIR="${DATA_HOME}/icons/hicolor"
EXT_SRC="${SRC_DIR}/gnome-extension/${EXTENSION_UUID}"
EXT_DIR="${DATA_HOME}/gnome-shell/extensions/${EXTENSION_UUID}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "${SRC_DIR}/greffier/__init__.py" 2>/dev/null || true)"
VERSION="${VERSION:-?}"

# --------------------------------------------------------------------------- #
# Actions (all modifications go through these so that --dry-run is honest)
# --------------------------------------------------------------------------- #
run() {
  if [ "${DRY_RUN}" = 1 ]; then
    printf '    %s[simulation]%s' "${C_DIM}" "${C_RESET}"
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

INSTALLED=()   # every file written, for the manifest used by uninstall.sh

ensure_tmp() {
  if [ -z "${TMP_DIR}" ]; then TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/greffier-install.XXXXXX")"; fi
}

sed_escape() { printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'; }

# install_file SRC DEST [MODE]: copy SRC, replacing @BINDIR@ and, in desktop
# files, the bare greffier commands of Exec=/TryExec= by absolute paths (GNOME
# Shell's PATH does not always contain ~/.local/bin). A copy (not a move) gives
# the new file the SELinux label of its destination directory.
install_file() {
  local src="$1" dest="$2" mode="${3:-0644}" bindir
  INSTALLED+=("${dest}")
  if [ "${DRY_RUN}" = 1 ]; then
    printf '    %s[simulation]%s installer %s\n' "${C_DIM}" "${C_RESET}" "${dest}"
    return 0
  fi
  ensure_tmp
  bindir="$(sed_escape "${VENV_BIN}")"
  case "${src}" in
    *.service|*.desktop)
      sed -e "s|@BINDIR@|${bindir}|g" \
          -e "s|^Exec=greffier|Exec=${bindir}/greffier|" \
          -e "s|^TryExec=greffier|TryExec=${bindir}/greffier|" \
          "${src}" > "${TMP_DIR}/render"
      if grep -q '@[A-Z_]*@' "${TMP_DIR}/render"; then
        die "Variable de modèle non remplacée dans ${src}."
      fi
      install -D -m "${mode}" "${TMP_DIR}/render" "${dest}" ;;
    *)
      install -D -m "${mode}" "${src}" "${dest}" ;;
  esac
}

# write_file DEST MODE < content
write_file() {
  local dest="$1" mode="$2"
  if [ "${DRY_RUN}" = 1 ]; then
    cat >/dev/null
    printf '    %s[simulation]%s écrire %s\n' "${C_DIM}" "${C_RESET}" "${dest}"
    return 0
  fi
  ensure_tmp
  cat > "${TMP_DIR}/content"
  install -D -m "${mode}" "${TMP_DIR}/content" "${dest}"
}

# copy_tree SRC DEST: copy a directory without development leftovers.
copy_tree() {
  local src="$1" dest="$2"
  if [ "${DRY_RUN}" = 1 ]; then
    printf '    %s[simulation]%s copier %s/ → %s/\n' "${C_DIM}" "${C_RESET}" "${src}" "${dest}"
    return 0
  fi
  mkdir -p -- "${dest}"
  tar -C "${src}" \
      --exclude='.git' --exclude='.git*' --exclude='.venv' --exclude='node_modules' \
      --exclude='.eslint*' --exclude='.prettier*' --exclude='.editorconfig' \
      --exclude='build' --exclude='dist' --exclude='*.egg-info' --exclude='__pycache__' \
      --exclude='.pytest_cache' --exclude='.ruff_cache' --exclude='*.zip' \
      --exclude='*~' --exclude='*.swp' --exclude='*.orig' --exclude='*.rej' \
      -cf - . | tar -C "${dest}" -xf -
}

ask_yes() {  # ask_yes "question" → 0 when the user accepts (default: yes)
  local answer=""
  [ -t 0 ] || return 1
  printf '  %s ' "$1"
  read -r answer || return 1
  case "${answer}" in ""|[oOyY]*) return 0 ;; *) return 1 ;; esac
}

# The installer refuses to go on, except in a simulation where we want to see the whole plan.
fatal() {
  if [ "${DRY_RUN}" = 1 ]; then warn "$* (simulation : on continue)"; else die "$*"; fi
}

session_bus_ok() { [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] || [ -S "${XDG_RUNTIME_DIR:-/nonexistent}/bus" ]; }
systemd_user_ok() { have systemctl && systemctl --user show-environment >/dev/null 2>&1; }

# Edit a GVariant string array such as "['a', 'b']" or "@as []".
# gv_list add|remove ITEM CURRENT → prints the new array.
gv_list() {
  "${PY}" - "$1" "$2" "$3" <<'PYEOF'
import ast
import sys

op, item, current = sys.argv[1:4]
current = current.strip()
if current.startswith("@as"):
    current = current[3:].strip()
values = [v for v in ast.literal_eval(current or "[]") if v != item]
if op == "add":
    values.append(item)
quoted = ("'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'" for v in values)
print("[" + ", ".join(quoted) + "]")
PYEOF
}

# --------------------------------------------------------------------------- #
# 1. Pre-flight checks
# --------------------------------------------------------------------------- #
printf '%sInstallation de Greffier %s%s — dictée vocale locale pour GNOME\n' "${C_BOLD}" "${VERSION}" "${C_RESET}"
if [ "${DRY_RUN}" = 1 ]; then
  printf '%sMode simulation : rien ne sera modifié.%s\n' "${C_YELLOW}" "${C_RESET}"
fi

step "Vérifications préalables"
if [ "$(id -u)" -eq 0 ]; then
  if [ -n "${SUDO_USER:-}" ]; then
    die "N'utilisez pas sudo : Greffier s'installe dans votre dossier personnel. Relancez « $0 » sans sudo ; le mot de passe ne sera demandé que pour dnf, si des paquets manquent."
  fi
  warn "Exécution en tant que root : Greffier sera installé pour root (${HOME})."
fi
if [ ! -f "${SRC_DIR}/pyproject.toml" ] || [ ! -d "${SRC_DIR}/greffier" ]; then
  die "Dépôt Greffier introuvable autour de ce script (${SRC_DIR}). Lancez packaging/install.sh depuis un clone complet du dépôt."
fi
case "${VENV_BIN}" in
  *[[:space:]\'\"\\]*) die "Le chemin « ${VENV_BIN} » contient des espaces ou des guillemets, ce que les fichiers systemd et D-Bus ne permettent pas ici." ;;
esac
ok "Sources : ${SRC_DIR}"

# --- Distribution ---
OS_RELEASE="${GREFFIER_OS_RELEASE:-/etc/os-release}"
[ -r "${OS_RELEASE}" ] || OS_RELEASE=/usr/lib/os-release
OS_ID="" OS_LIKE="" OS_NAME="Linux"
if [ -r "${OS_RELEASE}" ]; then
  # shellcheck source=/dev/null
  OS_ID="$(. "${OS_RELEASE}" && printf '%s' "${ID:-}")"
  # shellcheck source=/dev/null
  OS_LIKE="$(. "${OS_RELEASE}" && printf '%s' "${ID_LIKE:-}")"
  # shellcheck source=/dev/null
  OS_NAME="$(. "${OS_RELEASE}" && printf '%s' "${PRETTY_NAME:-${NAME:-Linux}}")"
fi
case " ${OS_ID} ${OS_LIKE} " in
  *" fedora "*) OS_FAMILY=fedora ;;
  *" debian "*|*" ubuntu "*) OS_FAMILY=debian ;;
  *" arch "*) OS_FAMILY=arch ;;
  *" suse "*|*" opensuse "*|*opensuse-*) OS_FAMILY=suse ;;
  *) OS_FAMILY=other ;;
esac
OSTREE=0
if [ -e /run/ostree-booted ]; then OSTREE=1; fi
if [ -e /run/.toolboxenv ] || [ -e /run/.containerenv ]; then
  warn "Vous semblez être dans un conteneur (toolbox, distrobox…) : installez Greffier depuis l'hôte, sinon le service et l'extension ne seront pas vus par votre session."
fi
if [ "${OS_FAMILY}" = fedora ]; then
  ok "Distribution : ${OS_NAME}"
else
  warn "Distribution : ${OS_NAME}. Greffier est testé sur Fedora ; l'installation continue, avec des indications de paquets approximatives."
fi

# --- Graphical session (informational only) ---
case "${XDG_SESSION_TYPE:-}" in
  wayland) ok "Session Wayland" ;;
  x11) warn "Session X11 : Greffier est conçu pour GNOME sous Wayland ; certaines fonctions peuvent manquer." ;;
  *) warn "Aucune session graphique détectée (connexion SSH ?) : Greffier fonctionnera à la prochaine ouverture de session GNOME." ;;
esac
case ":${XDG_CURRENT_DESKTOP:-}:" in
  *:GNOME:*) ok "Bureau GNOME" ;;
  ::) ;;
  *) warn "Bureau « ${XDG_CURRENT_DESKTOP} » : Greffier vise GNOME (raccourci, extension) ; ailleurs, seul le service fonctionnera." ;;
esac
GNOME_MAJOR=""
if have gnome-shell; then
  GNOME_MAJOR="$(gnome-shell --version 2>/dev/null | sed -n 's/[^0-9]*\([0-9][0-9]*\).*/\1/p' | head -n 1 || true)"
  if [ -n "${GNOME_MAJOR}" ]; then
    if [ "${GNOME_MAJOR}" -ge 48 ]; then
      ok "GNOME Shell ${GNOME_MAJOR}"
    else
      warn "GNOME Shell ${GNOME_MAJOR} : le mode « appuyer pour parler » demande GNOME 48 ou plus récent (le mode bascule fonctionne)."
    fi
  fi
fi

# --- Python ---
if [ -n "${GREFFIER_PYTHON:-}" ]; then
  PY="${GREFFIER_PYTHON}"
elif [ -x /usr/bin/python3 ]; then
  PY=/usr/bin/python3   # the distribution's Python, the one PyGObject is built for
else
  PY="$(command -v python3 || true)"
fi
if [ -z "${PY}" ] || ! have "${PY}"; then
  die "Python 3 est introuvable${PY:+ (${PY})}. Sur Fedora : sudo dnf install python3"
fi
# PyGObject is compiled for one interpreter. If python3 points to another one
# (alternatives, a newer Python installed next to the system one…), use the
# versioned interpreter that can actually import it.
# Probes import GObject libraries; GStreamer would create its registry in
# ~/.cache, so they run with a throw-away cache (keeps --dry-run side-effect free).
# (TMP_DIR is created here, outside any $(…) subshell, so the EXIT trap removes it.)
if [ -z "${TMP_DIR}" ]; then TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/greffier-install.XXXXXX")"; fi
probe_env() { XDG_CACHE_HOME="${TMP_DIR}/probe-cache" "$@"; }
gi_importable() { probe_env "$1" -c 'import gi' >/dev/null 2>&1; }
if [ -z "${GREFFIER_PYTHON:-}" ] && ! gi_importable "${PY}"; then
  for minor in 15 14 13 12 11; do
    candidate="/usr/bin/python3.${minor}"
    if [ -x "${candidate}" ] && [ "${candidate}" != "${PY}" ] && gi_importable "${candidate}"; then
      info "${PY} ne trouve pas PyGObject ; utilisation de ${candidate}, pour lequel il est installé."
      PY="${candidate}"
      break
    fi
  done
fi
PY_VER="$("${PY}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! "${PY}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  fatal "Python ${PY_VER} (${PY}) est trop ancien : Greffier demande Python 3.11 ou plus récent."
fi
ok "Python ${PY_VER} (${PY})"

# --- System packages ---
check_gi() {  # prints the missing GObject namespaces (empty = all good)
  probe_env "${PY}" - 2>/dev/null <<'PYEOF'
import sys

try:
    import gi
except Exception:
    print("gi")
    sys.exit(0)
missing = []
for namespace, version in (("Gtk", "4.0"), ("Adw", "1"), ("Gst", "1.0"), ("GstApp", "1.0")):
    try:
        gi.require_version(namespace, version)
        __import__("gi.repository." + namespace)
    except Exception:
        missing.append(namespace)
print(" ".join(missing))
PYEOF
}

describe_gi() {  # "Gtk Adw" → human-readable list
  local out="" ns
  for ns in $1; do
    case "${ns}" in
      gi) out+="PyGObject (python3-gobject), " ;;
      Gtk) out+="GTK 4 (gtk4), " ;;
      Adw) out+="libadwaita, " ;;
      Gst) out+="GStreamer (gstreamer1), " ;;
      GstApp) out+="GstApp (gstreamer1-plugins-base), " ;;
    esac
  done
  printf '%s' "${out%, }"
}

distro_hint() {
  case "${OS_FAMILY}" in
    debian) info "Debian/Ubuntu : sudo apt install python3-gi python3-venv gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-pipewire wl-clipboard" ;;
    arch)   info "Arch Linux : sudo pacman -S --needed python-gobject gtk4 libadwaita gstreamer gst-plugins-base gst-plugins-good gst-plugin-pipewire wl-clipboard" ;;
    suse)   info "openSUSE : sudo zypper install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 typelib-1_0-Gst-1_0 gstreamer-plugins-base gstreamer-plugins-good gstreamer-plugin-pipewire wl-clipboard" ;;
    *)      info "Paquets nécessaires : PyGObject, GTK 4, libadwaita, GStreamer (base, good, PipeWire) avec leurs fichiers d'introspection, wl-clipboard." ;;
  esac
}

install_fedora_packages() {
  local cmd=(dnf install -y "$@")
  if [ "$(id -u)" -ne 0 ]; then cmd=(sudo "${cmd[@]}"); fi
  if [ "${OSTREE}" = 1 ]; then
    warn "Fedora Atomic (Silverblue…) : installez-les avec « rpm-ostree install $* » puis redémarrez."
    return 0
  fi
  if [ "${YES}" = 1 ] || { [ "${DRY_RUN}" = 0 ] && ask_yes "Les installer maintenant avec « ${cmd[*]} » ? [O/n]"; }; then
    run "${cmd[@]}" || die "dnf a échoué. Installez les paquets ci-dessus puis relancez ce script."
    if [ "${DRY_RUN}" = 0 ]; then ok "Paquets système installés."; fi
  else
    info "Installez-les avec : ${cmd[*]}"
    info "(ou relancez ce script avec --yes)"
  fi
}

step "Dépendances système"
MISSING_GI="$(check_gi)" || MISSING_GI="gi"
if [ "${NO_DEPS}" = 1 ]; then
  if [ -n "${MISSING_GI}" ]; then
    warn "Introuvable avec ${PY} : $(describe_gi "${MISSING_GI}"). Vérification ignorée (--no-deps), mais Greffier ne démarrera pas sans."
  else
    ok "PyGObject, GTK 4, libadwaita et GStreamer sont disponibles (--no-deps : aucun paquet installé)."
  fi
else
  if [ "${OS_FAMILY}" = fedora ] && have rpm; then
    MISSING_RPMS=()
    for pkg in "${FEDORA_PACKAGES[@]}"; do
      rpm -q --quiet "${pkg}" || MISSING_RPMS+=("${pkg}")
    done
    if [ "${#MISSING_RPMS[@]}" -eq 0 ]; then
      ok "Tous les paquets Fedora nécessaires sont installés."
    else
      info "Paquets Fedora manquants : ${MISSING_RPMS[*]}"
      install_fedora_packages "${MISSING_RPMS[@]}"
    fi
  elif [ -n "${MISSING_GI}" ]; then
    info "Il manque : $(describe_gi "${MISSING_GI}"). Installez l'équivalent pour votre distribution (à titre indicatif) :"
    distro_hint
  fi
  if [ "${DRY_RUN}" = 0 ]; then
    MISSING_GI="$(check_gi)" || MISSING_GI="gi"
  fi
  if [ -n "${MISSING_GI}" ]; then
    fatal "Toujours introuvable avec ${PY} : $(describe_gi "${MISSING_GI}"). Installez les paquets indiqués puis relancez (ou --no-deps pour passer outre)."
  else
    ok "PyGObject, GTK 4, libadwaita et GStreamer sont disponibles."
  fi
  if ! have wl-copy; then
    warn "wl-copy (paquet wl-clipboard) est absent : sans l'extension GNOME, la copie dans le presse-papiers ne fonctionnera pas."
  fi
fi
if ! have ydotool; then
  info "Optionnel : ydotool (sudo dnf install ydotool) sert de dernier recours pour la saisie au clavier si ni l'extension ni le portail ne sont disponibles."
fi

# --- Model id ---
if [ -n "${MODEL}" ]; then
  if KNOWN_MODELS="$("${PY}" - "${SRC_DIR}/greffier/data/catalog.json" "${MODEL}" 2>/dev/null <<'PYEOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    models = json.load(fh).get("models", [])
ids = [m.get("id", "") for m in models if m.get("kind", "asr") == "asr"]
print(", ".join(ids))
sys.exit(0 if sys.argv[2] in ids else 1)
PYEOF
  )"; then
    ok "Modèle demandé : ${MODEL}"
  else
    warn "Modèle « ${MODEL} » absent du catalogue intégré (${KNOWN_MODELS:-catalogue illisible}). On essaiera quand même : le catalogue en ligne est peut-être plus récent."
  fi
fi

# --------------------------------------------------------------------------- #
# 2. Python virtualenv and package
# --------------------------------------------------------------------------- #
step "Environnement Python (${VENV})"
venv_matches() {
  [ -x "${VENV}/bin/python" ] || return 1
  grep -qi '^include-system-site-packages *= *true' "${VENV}/pyvenv.cfg" 2>/dev/null || return 1
  [ "$("${VENV}/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" = "${PY_VER}" ]
}
CREATE_VENV=1
if [ -e "${VENV}" ]; then
  if venv_matches; then
    CREATE_VENV=0
    ok "Environnement existant réutilisé (mise à jour)."
  else
    warn "Environnement existant incompatible (Python mis à jour ou installation interrompue) : il est recréé."
    run rm -rf -- "${VENV}"
  fi
fi
if [ "${CREATE_VENV}" = 1 ]; then
  VENV_ARGS=(-m venv --system-site-packages)
  if [ "${SKIP_PIP}" = 1 ]; then VENV_ARGS+=(--without-pip); fi
  run mkdir -p -- "${APP_DATA}"
  run "${PY}" "${VENV_ARGS[@]}" "${VENV}" \
    || die "Impossible de créer l'environnement virtuel (sur Debian/Ubuntu : sudo apt install python3-venv)."
  ok "Environnement virtuel créé (avec les paquets système : PyGObject, GStreamer…)."
fi

pip_failed() {
  die "pip n'a pas pu installer Greffier. Vérifiez la connexion à Internet (proxy : variables HTTPS_PROXY/PIP_INDEX_URL) et l'espace disque, puis relancez. Voir docs/TROUBLESHOOTING.md."
}
if [ "${SKIP_PIP}" = 1 ]; then
  warn "GREFFIER_INSTALL_SKIP_PIP=1 : pip n'est pas lancé, le paquet Python n'est pas installé (mode test)."
else
  PIP=("${VENV}/bin/python" -m pip --disable-pip-version-check)
  run "${PIP[@]}" install --quiet --upgrade pip \
    || warn "pip n'a pas pu être mis à jour (hors ligne ?) ; on continue avec la version fournie."
  if [ "${DEV}" = 1 ]; then
    info "Installation éditable depuis ${SRC_DIR} (numpy, sherpa-onnx…), cela peut prendre une minute."
    run "${PIP[@]}" install --editable "${SRC_DIR}" || pip_failed
  else
    # Build from a clean copy: no build/ or *.egg-info left in the checkout,
    # and no stale module from an earlier build ends up in the wheel.
    STAGE="${SRC_DIR}"
    if [ "${DRY_RUN}" = 0 ]; then
      ensure_tmp
      STAGE="${TMP_DIR}/src"
      copy_tree "${SRC_DIR}" "${STAGE}"
      # The feedback sounds are committed; regenerate them (pure stdlib) if missing.
      if ! compgen -G "${STAGE}/greffier/data/sounds/*.wav" >/dev/null && [ -f "${STAGE}/tools/make_sounds.py" ]; then
        "${PY}" "${STAGE}/tools/make_sounds.py" --out "${STAGE}/greffier/data/sounds" >/dev/null \
          || warn "Les sons n'ont pas pu être générés (tools/make_sounds.py) : Greffier fonctionnera sans."
      fi
    fi
    info "Installation de Greffier et de ses dépendances (numpy, sherpa-onnx…), cela peut prendre une minute."
    run "${PIP[@]}" install --upgrade-strategy only-if-needed "${STAGE}" || pip_failed
  fi
  if [ "${DRY_RUN}" = 0 ]; then
    for name in "${ENTRY_POINTS[@]}"; do
      [ -x "${VENV_BIN}/${name}" ] || die "La commande ${VENV_BIN}/${name} n'a pas été créée par pip."
    done
    if ! "${VENV_BIN}/python" -c 'import sherpa_onnx' >/dev/null 2>&1; then
      warn "Le module sherpa_onnx ne se charge pas avec Python ${PY_VER} : la reconnaissance vocale ne fonctionnera pas. Voir docs/TROUBLESHOOTING.md."
    fi
  fi
  if [ "${DEV}" = 1 ]; then
    ok "Greffier ${VERSION} installé en mode développement (le service exécute le code de ${SRC_DIR})."
  else
    ok "Greffier ${VERSION} installé dans l'environnement virtuel."
  fi
fi

# --------------------------------------------------------------------------- #
# 3. Launchers in ~/.local/bin
# --------------------------------------------------------------------------- #
step "Commandes (${BIN_DIR})"
for name in "${ENTRY_POINTS[@]}"; do
  dest="${BIN_DIR}/${name}"
  if [ -e "${dest}" ] || [ -L "${dest}" ]; then
    if ! grep -qs "${WRAPPER_MARKER}" "${dest}"; then
      warn "${dest} existe déjà et ne vient pas de cet installateur : renommé en ${dest}.avant-greffier"
      run mv -f -- "${dest}" "${dest}.avant-greffier"
    fi
  fi
  INSTALLED+=("${dest}")
  write_file "${dest}" 0755 <<EOF
#!/bin/sh
# ${WRAPPER_MARKER}: generated by Greffier's packaging/install.sh, do not edit.
# Re-run the installer to update it; uninstall.sh removes it.
if [ ! -x '${VENV_BIN}/${name}' ]; then
  echo "Greffier : installation incomplète (${VENV_BIN}/${name} introuvable)." >&2
  echo "Relancez packaging/install.sh depuis le dépôt Greffier." >&2
  exit 127
fi
exec '${VENV_BIN}/${name}' "\$@"
EOF
done
ok "greffier, greffier-daemon et greffierctl → ${VENV_BIN}"
case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *) warn "${BIN_DIR} n'est pas dans votre PATH. Ajoutez à ~/.bashrc : export PATH=\"\$HOME/.local/bin:\$PATH\" (les raccourcis et le menu fonctionnent quand même)." ;;
esac

# --------------------------------------------------------------------------- #
# 4. Service, D-Bus activation and desktop integration files
# --------------------------------------------------------------------------- #
step "Intégration au bureau"
need_file() { [ -f "$1" ] || die "Fichier manquant dans le dépôt : $1"; }
need_file "${SRC_DIR}/data/systemd/${SYSTEMD_UNIT}"
need_file "${SRC_DIR}/data/dbus/${DBUS_NAME}.service"
need_file "${SRC_DIR}/data/${DAEMON_ID}.desktop"

install_file "${SRC_DIR}/data/systemd/${SYSTEMD_UNIT}" "${UNIT_DIR}/${SYSTEMD_UNIT}"
install_file "${SRC_DIR}/data/dbus/${DBUS_NAME}.service" "${DBUS_DIR}/${DBUS_NAME}.service"
install_file "${SRC_DIR}/data/${DAEMON_ID}.desktop" "${APPS_DIR}/${DAEMON_ID}.desktop"
ok "Service systemd, activation D-Bus et fichier .desktop du service"

GUI_DESKTOP=0
if [ -f "${SRC_DIR}/data/${APP_ID}.desktop" ]; then
  install_file "${SRC_DIR}/data/${APP_ID}.desktop" "${APPS_DIR}/${APP_ID}.desktop"
  GUI_DESKTOP=1
  ok "Lanceur de l'application (${APP_ID}.desktop)"
  if [ -f "${SRC_DIR}/data/dbus/${APP_ID}.service" ]; then
    install_file "${SRC_DIR}/data/dbus/${APP_ID}.service" "${DBUS_DIR}/${APP_ID}.service"
  fi
else
  warn "data/${APP_ID}.desktop absent du dépôt : pas d'entrée « Greffier » dans la vue des applications."
fi
if [ -f "${SRC_DIR}/data/${APP_ID}.metainfo.xml" ]; then
  install_file "${SRC_DIR}/data/${APP_ID}.metainfo.xml" "${METAINFO_DIR}/${APP_ID}.metainfo.xml"
  ok "Métadonnées AppStream"
fi
ICON_COUNT=0
if [ -d "${SRC_DIR}/data/icons/hicolor" ]; then
  while IFS= read -r -d '' icon; do
    install_file "${icon}" "${ICONS_DIR}/${icon#"${SRC_DIR}/data/icons/hicolor/"}"
    ICON_COUNT=$((ICON_COUNT + 1))
  done < <(find "${SRC_DIR}/data/icons/hicolor" -type f \( -name '*.svg' -o -name '*.png' \) -print0 | sort -z)
fi
if [ "${ICON_COUNT}" -gt 0 ]; then
  ok "${ICON_COUNT} icône(s)"
else
  warn "Aucune icône dans data/icons/hicolor : une icône générique sera affichée."
fi
install_file "${SRC_DIR}/packaging/uninstall.sh" "${APP_DATA}/uninstall.sh" 0755

# Manifest for uninstall.sh; drop files a previous version installed but this one does not.
if [ -f "${MANIFEST}" ]; then
  while IFS= read -r old; do
    case "${old}" in "${HOME}"/*) ;; *) continue ;; esac
    case "${old}" in */../*) continue ;; esac
    keep=0
    for new in "${INSTALLED[@]}"; do
      if [ "${new}" = "${old}" ]; then keep=1; break; fi
    done
    if [ "${keep}" = 0 ] && [ -f "${old}" ]; then run rm -f -- "${old}"; fi
  done < "${MANIFEST}"
fi
write_file "${MANIFEST}" 0644 < <(printf '%s\n' "${INSTALLED[@]}")

if [ "${DRY_RUN}" = 0 ] && have selinuxenabled && selinuxenabled && have restorecon; then
  restorecon -R -- "${UNIT_DIR}/${SYSTEMD_UNIT}" "${DBUS_DIR}" "${APPS_DIR}" "${VENV}" "${BIN_DIR}" >/dev/null 2>&1 || true
fi
if have update-desktop-database && [ -d "${APPS_DIR}" ]; then
  run update-desktop-database -q "${APPS_DIR}" || true
fi
if [ -d "${ICONS_DIR}" ] || [ "${DRY_RUN}" = 1 ]; then
  # Only refresh an existing cache: creating one would hide icons that other
  # applications later install here without updating it.
  if [ -f "${ICONS_DIR}/icon-theme.cache" ] && have gtk-update-icon-cache; then
    run gtk-update-icon-cache -q -t -f "${ICONS_DIR}" || true
  elif [ -d "${ICONS_DIR}" ]; then
    run touch "${ICONS_DIR}"
  fi
fi
# dbus-broker (Fedora) does not watch its service directories: ask it to rescan.
if session_bus_ok && have gdbus; then
  run gdbus call --session --timeout 5 --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.ReloadConfig >/dev/null 2>&1 || true
fi

# --------------------------------------------------------------------------- #
# 5. systemd user service
# --------------------------------------------------------------------------- #
step "Service en arrière-plan (${SYSTEMD_UNIT})"
SERVICE_STARTED=0
if systemd_user_ok; then
  run systemctl --user daemon-reload
  if [ "${NO_AUTOSTART}" = 1 ]; then
    if systemctl --user is-enabled --quiet "${SYSTEMD_UNIT}" 2>/dev/null; then
      run systemctl --user disable --quiet "${SYSTEMD_UNIT}"
    fi
    if systemctl --user is-active --quiet "${SYSTEMD_UNIT}" 2>/dev/null; then
      if run systemctl --user restart "${SYSTEMD_UNIT}"; then
        SERVICE_STARTED=1
        ok "Service redémarré avec la nouvelle version."
      else
        fail "Le service n'a pas redémarré : journalctl --user -u ${SYSTEMD_UNIT}"
      fi
    fi
    ok "Pas de démarrage automatique (--no-autostart) : le service sera lancé à la demande (raccourci, application, greffierctl)."
  else
    run systemctl --user enable --quiet "${SYSTEMD_UNIT}"
    # restart = start, or reload the new code after an upgrade
    if run systemctl --user restart "${SYSTEMD_UNIT}"; then
      SERVICE_STARTED=1
      ok "Service activé et démarré (il démarrera avec chaque session GNOME)."
    else
      fail "Le service n'a pas démarré. Dernières lignes du journal :"
      journalctl --user -u "${SYSTEMD_UNIT}" -n 15 --no-pager -o cat 2>/dev/null | sed 's/^/      /' >&2 || true
      info "Diagnostic : greffierctl doctor   •   journal complet : journalctl --user -u ${SYSTEMD_UNIT}"
    fi
  fi
else
  warn "Gestionnaire systemd de l'utilisateur injoignable (session SSH, conteneur ?) : le service sera chargé à la prochaine ouverture de session."
  if [ "${NO_AUTOSTART}" = 0 ]; then
    # What `systemctl --user enable` would do (WantedBy=graphical-session.target).
    wants="${UNIT_DIR}/graphical-session.target.wants"
    run mkdir -p -- "${wants}"
    run ln -sfn -- "${UNIT_DIR}/${SYSTEMD_UNIT}" "${wants}/${SYSTEMD_UNIT}"
    ok "Démarrage automatique activé pour les prochaines sessions."
  fi
fi

# --------------------------------------------------------------------------- #
# 6. GNOME Shell extension
# --------------------------------------------------------------------------- #
step "Extension GNOME Shell (${EXTENSION_UUID})"
EXT_STATUS="non installée"
EXT_RELOGIN=0
install_extension() {
  if [ "${NO_EXTENSION}" = 1 ]; then
    info "Ignorée (--no-extension). Greffier utilisera le portail RemoteDesktop et wl-copy pour insérer le texte."
    return 0
  fi
  if [ ! -f "${EXT_SRC}/metadata.json" ]; then
    info "L'extension n'est pas (encore) présente dans ce dépôt (${EXT_SRC}) : étape ignorée."
    info "Greffier fonctionne sans : l'insertion passera par le portail RemoteDesktop et wl-copy."
    return 0
  fi
  local upgrade=0 live=0 state="" domain lang po shell_versions
  if [ -d "${EXT_DIR}" ]; then upgrade=1; fi
  run rm -rf -- "${EXT_DIR}"
  copy_tree "${EXT_SRC}" "${EXT_DIR}"
  if [ "${DRY_RUN}" = 0 ]; then
    if compgen -G "${EXT_DIR}/schemas/*.gschema.xml" >/dev/null && have glib-compile-schemas; then
      glib-compile-schemas "${EXT_DIR}/schemas" || warn "glib-compile-schemas a échoué pour l'extension."
    fi
    if compgen -G "${EXT_DIR}/po/*.po" >/dev/null && have msgfmt; then
      domain="$("${PY}" -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("gettext-domain", ""))' "${EXT_DIR}/metadata.json" 2>/dev/null || true)"
      domain="${domain:-${EXTENSION_UUID}}"
      for po in "${EXT_DIR}"/po/*.po; do
        lang="$(basename "${po}" .po)"
        mkdir -p -- "${EXT_DIR}/locale/${lang}/LC_MESSAGES"
        msgfmt -o "${EXT_DIR}/locale/${lang}/LC_MESSAGES/${domain}.mo" "${po}" || warn "msgfmt a échoué pour ${po}."
      done
    fi
  fi
  EXT_STATUS="installée"
  ok "Fichiers copiés dans ${EXT_DIR}"

  if [ -n "${GNOME_MAJOR}" ]; then
    shell_versions="$("${PY}" -c 'import json, sys; print(" ".join(str(v).split(".")[0] for v in json.load(open(sys.argv[1], encoding="utf-8")).get("shell-version", [])))' "${EXT_SRC}/metadata.json" 2>/dev/null || true)"
    case " ${shell_versions} " in
      *" ${GNOME_MAJOR} "*) ;;
      *) warn "L'extension déclare GNOME ${shell_versions:-?} mais vous avez GNOME ${GNOME_MAJOR} : GNOME Shell risque de la refuser." ;;
    esac
  fi

  if have gnome-extensions && gnome-extensions info "${EXTENSION_UUID}" >/dev/null 2>&1 \
     && run gnome-extensions enable "${EXTENSION_UUID}"; then
    # The running Shell knows the extension (upgrade, or X11): enabled live.
    state="$(gnome-extensions info "${EXTENSION_UUID}" 2>/dev/null | sed -n 's/^ *State: *//p' | head -n 1 || true)"
    case "${state}" in ACTIVE|ENABLED) live=1 ;; esac
    ok "Extension activée."
  elif have gsettings && gsettings writable org.gnome.shell enabled-extensions >/dev/null 2>&1; then
    # Wayland: the running GNOME Shell only discovers new extensions at login.
    # Add it to the list it reads at startup.
    local current new
    current="$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo '[]')"
    case "${current}" in
      *"'${EXTENSION_UUID}'"*) ;;
      *) new="$(gv_list add "${EXTENSION_UUID}" "${current}")" \
           && run gsettings set org.gnome.shell enabled-extensions "${new}" ;;
    esac
    current="$(gsettings get org.gnome.shell disabled-extensions 2>/dev/null || echo '[]')"
    case "${current}" in
      *"'${EXTENSION_UUID}'"*) new="$(gv_list remove "${EXTENSION_UUID}" "${current}")" \
           && run gsettings set org.gnome.shell disabled-extensions "${new}" ;;
    esac
    ok "Extension activée pour la prochaine ouverture de session."
  else
    warn "Ni gnome-extensions ni gsettings ne sont disponibles : activez l'extension dans l'application « Extensions » après reconnexion."
  fi
  if have gsettings && [ "$(gsettings get org.gnome.shell disable-user-extensions 2>/dev/null || true)" = "true" ]; then
    warn "Les extensions sont désactivées globalement : réactivez-les dans l'application « Extensions »."
  fi
  # 1 = never loaded yet: log out/in once; 2 = old code still loaded until next login.
  if [ "${live}" = 0 ]; then
    EXT_RELOGIN=1
  elif [ "${upgrade}" = 1 ]; then
    EXT_RELOGIN=2
  fi
  return 0
}
install_extension

# --------------------------------------------------------------------------- #
# 7. Optional model download
# --------------------------------------------------------------------------- #
if [ -n "${MODEL}" ]; then
  step "Modèle « ${MODEL} »"
  if [ "${SKIP_PIP}" = 1 ]; then
    warn "Mode test (GREFFIER_INSTALL_SKIP_PIP=1) : téléchargement du modèle ignoré."
  elif ! session_bus_ok; then
    warn "Pas de bus de session D-Bus : lancez plus tard « greffierctl models install ${MODEL} »."
  elif [ "${ERRORS}" -gt 0 ]; then
    warn "Le service ne fonctionne pas : téléchargement du modèle ignoré."
  else
    info "Téléchargement et vérification (SHA-256) en cours… (Ctrl+C pour interrompre : le téléchargement reprendra plus tard)"
    if run "${VENV_BIN}/greffierctl" models install "${MODEL}"; then
      ok "Modèle installé."
    else
      warn "Le modèle n'a pas pu être installé. Réessayez avec « greffierctl models install ${MODEL} » ou depuis l'application."
    fi
  fi
fi

# --------------------------------------------------------------------------- #
# 8. Summary
# --------------------------------------------------------------------------- #
if [ "${DRY_RUN}" = 1 ]; then
  step "Simulation terminée"
  info "Aucun fichier n'a été modifié. Relancez sans --dry-run pour installer."
  exit 0
fi

step "C'est prêt !"
if [ "${NO_AUTOSTART}" = 1 ]; then AUTOSTART_TXT="non, lancé à la demande"; else AUTOSTART_TXT="oui"; fi
cat <<EOF
  Greffier ${VERSION} est installé pour ${USER:-$(id -un)} :
    • programme        ${VENV}$([ "${DEV}" = 1 ] && printf ' (éditable : %s)' "${SRC_DIR}")
    • commandes        greffier, greffierctl, greffier-daemon (${BIN_DIR})
    • service          ${SYSTEMD_UNIT} (démarrage automatique : ${AUTOSTART_TXT})
    • extension GNOME  ${EXT_STATUS}

  Pour commencer :
    1. Ouvrez ${C_BOLD}Greffier${C_RESET} $([ "${GUI_DESKTOP}" = 1 ] && printf 'depuis la vue des applications (ou tapez « greffier »)' || printf 'avec la commande « greffier »')
       et choisissez un modèle (Parakeet v3 est recommandé pour le français).
    2. Placez le curseur dans n'importe quelle application, appuyez sur
       ${C_BOLD}${DEFAULT_SHORTCUT}${C_RESET} (touche Windows + Alt + D), parlez, puis appuyez à nouveau :
       le texte est inséré. Le raccourci se modifie dans Paramètres › Clavier.
    3. En cas de souci : ${C_BOLD}greffierctl doctor${C_RESET} et docs/TROUBLESHOOTING.md

  Mettre à jour : git pull, puis relancez packaging/install.sh
  Désinstaller  : ~/.local/share/greffier/uninstall.sh   (--purge supprime aussi
                  les modèles, les réglages et l'historique)
EOF
if [ "${SERVICE_STARTED}" = 0 ] && [ "${NO_AUTOSTART}" = 0 ] && [ "${ERRORS}" = 0 ]; then
  info ""
  info "Le service démarrera à la prochaine ouverture de session (ou dès le premier lancement de l'application)."
fi
if [ "${EXT_RELOGIN}" = 1 ]; then
  printf '\n  %s⚠ Extension GNOME : déconnectez-vous puis reconnectez-vous une fois.%s\n' "${C_YELLOW}${C_BOLD}" "${C_RESET}"
  info "Sous Wayland, GNOME Shell ne charge une nouvelle extension qu'à l'ouverture de session."
  info "D'ici là, Greffier fonctionne déjà, avec le portail et le presse-papiers."
elif [ "${EXT_RELOGIN}" = 2 ]; then
  printf '\n'
  info "La nouvelle version de l'extension sera chargée à votre prochaine connexion."
fi
if [ "${ERRORS}" -gt 0 ]; then
  printf '\n%s✗ Installation terminée avec %s erreur(s), voir ci-dessus.%s\n' "${E_RED}" "${ERRORS}" "${E_RESET}" >&2
  exit 1
fi
if [ "${WARNINGS}" -gt 0 ]; then
  printf '\n  %s(%s avertissement(s) ci-dessus.)%s\n' "${C_DIM}" "${WARNINGS}" "${C_RESET}"
fi
exit 0
