# Installer Greffier

Greffier s'installe **pour votre utilisateur**, dans votre dossier
personnel, avec le script `packaging/install.sh`. Aucun droit administrateur
n'est nécessaire, sauf pour installer d'éventuels paquets système manquants
(`sudo dnf install`, après confirmation).

## Installation rapide (Fedora Workstation 42 à 44)

```bash
git clone https://github.com/GDS-42/gnome-wayland-speech-to-text.git
cd gnome-wayland-speech-to-text
./packaging/install.sh
```

Le script :

1. vérifie votre système (Fedora, session Wayland, GNOME, Python 3.11+) et
   la présence de PyGObject, GTK 4, libadwaita et GStreamer ; il propose
   d'installer ce qui manque parmi `python3-gobject gtk4 libadwaita
   gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good
   pipewire-gstreamer wl-clipboard` ;
2. crée un environnement Python dans `~/.local/share/greffier/venv`
   (il réutilise les paquets système pour PyGObject) et y installe Greffier,
   numpy et sherpa-onnx (une cinquantaine de Mo depuis PyPI) ;
3. installe les commandes `greffier`, `greffierctl` et `greffier-daemon`
   dans `~/.local/bin`, le lanceur de l'application, les icônes, le service
   systemd et le fichier d'activation D-Bus ;
4. active et démarre le service (il démarrera ensuite avec chaque session) ;
5. installe et active l'extension GNOME Shell. **Sous Wayland, il faut se
   déconnecter puis se reconnecter une fois** pour qu'elle soit chargée.

Ouvrez ensuite **Greffier** depuis la vue des applications pour choisir et
télécharger un modèle, puis dictez avec **Super+Alt+D**.

## Options

| Option | Effet |
|---|---|
| `-y`, `--yes` | installer les paquets système manquants sans poser de question |
| `--no-deps` | ne pas vérifier ni installer les paquets système |
| `--no-extension` | ne pas installer l'extension GNOME Shell |
| `--no-autostart` | ne pas lancer le service avec la session : il démarre à la demande (raccourci, application, `greffierctl`) grâce à l'activation D-Bus |
| `--model ID` | télécharger ce modèle à la fin, par ex. `--model parakeet-tdt-0.6b-v3-int8` |
| `--dev` | installation « éditable » : le service exécute le code du dépôt (développement) |
| `-n`, `--dry-run` | afficher tout ce qui serait fait, sans rien modifier |
| `-h`, `--help` | aide |

Identifiants des modèles : `parakeet-tdt-0.6b-v3-int8` (recommandé),
`canary-180m-flash-int8`, `cohere-transcribe-14-lang-int8`,
`qwen3-asr-0.6b-int8`, `whisper-turbo-int8` (`greffierctl models list` pour
la liste à jour).

Variables d'environnement reconnues :

| Variable | Usage |
|---|---|
| `GREFFIER_PYTHON` | interpréteur Python à utiliser (défaut : `/usr/bin/python3`, celui pour lequel PyGObject est compilé) |
| `HTTPS_PROXY`, `PIP_INDEX_URL`… | proxy ou miroir PyPI pour pip |
| `NO_COLOR=1` | sortie sans couleurs |
| `GREFFIER_INSTALL_SKIP_PIP=1` | pour les tests : ne lance pas pip (rien n'est installé dans l'environnement Python) |
| `GREFFIER_OS_RELEASE` | pour les tests : fichier `os-release` à lire à la place de `/etc/os-release` |

## Ce qui est installé, et où

| Emplacement | Contenu |
|---|---|
| `~/.local/share/greffier/venv/` | environnement Python (Greffier, numpy, sherpa-onnx) |
| `~/.local/bin/greffier`, `greffierctl`, `greffier-daemon` | petits lanceurs vers l'environnement Python |
| `~/.config/systemd/user/app-io.github.gds_42.Greffier.Daemon.service` | service utilisateur systemd |
| `~/.local/share/dbus-1/services/io.github.gds_42.Greffier.Daemon.service` | activation D-Bus du service |
| `~/.local/share/applications/io.github.gds_42.Greffier.desktop` | lanceur de l'application |
| `~/.local/share/applications/io.github.gds_42.Greffier.Daemon.desktop` | entrée cachée qui nomme le service (notifications, portails) |
| `~/.local/share/metainfo/`, `~/.local/share/icons/hicolor/` | métadonnées AppStream et icônes |
| `~/.local/share/gnome-shell/extensions/greffier@gds-42.github.io/` | extension GNOME Shell |
| `~/.local/share/greffier/uninstall.sh`, `installed-files.txt` | désinstallateur et liste des fichiers installés |

Créés ensuite par Greffier lui-même : réglages (`~/.config/greffier/`),
modèles (`~/.local/share/greffier/models/`), cache et téléchargements
partiels (`~/.cache/greffier/`), historique s'il est sauvegardé et accord du
portail (`~/.local/state/greffier/`).

Le service tourne sous systemd avec quelques protections (pas d'élévation de
privilèges, fichiers créés lisibles par vous seul) ; leur justification est
détaillée dans le fichier du service, `data/systemd/`.

## Mettre à jour

```bash
cd gnome-wayland-speech-to-text
git pull
./packaging/install.sh
```

Le script met à jour l'installation existante et redémarre le service. Il
recrée l'environnement Python s'il ne correspond plus (par exemple après une
mise à niveau de Fedora qui change la version de Python). Vos réglages et
modèles ne sont pas touchés.

## Désinstaller

```bash
~/.local/share/greffier/uninstall.sh             # programme seulement
~/.local/share/greffier/uninstall.sh --purge     # + réglages, modèles, cache, historique
```

Le désinstallateur arrête et désactive le service, retire le raccourci
clavier « Greffier » des Paramètres GNOME, désactive et supprime
l'extension, puis les fichiers listés ci-dessus. Avec `--purge`, il affiche
la place occupée par vos données avant de les effacer. `--yes` évite les
questions, `--dry-run` montre ce qui serait supprimé. Les paquets système
ne sont jamais désinstallés.

## Fedora Atomic (Silverblue, Kinoite…)

Les paquets système nécessaires sont déjà présents dans l'image GNOME de
Silverblue, sauf parfois `wl-clipboard`. Le script ne lance pas `dnf` sur
ces systèmes : il indique la commande `rpm-ostree install …` à exécuter
(suivie d'un redémarrage). Lancez le script depuis l'hôte, pas dans une
boîte `toolbox`.

## Autres distributions

Greffier vise Fedora, mais le script fonctionne ailleurs : il avertit, puis
continue. Installez d'abord l'équivalent des paquets Fedora (à titre
indicatif) :

```bash
# Debian 13, Ubuntu 24.04 et suivantes
sudo apt install python3-gi python3-venv gir1.2-gtk-4.0 gir1.2-adw-1 \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-pipewire wl-clipboard
# Arch Linux
sudo pacman -S --needed python-gobject gtk4 libadwaita gstreamer gst-plugins-base \
  gst-plugins-good gst-plugin-pipewire wl-clipboard
# openSUSE Tumbleweed
sudo zypper install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 \
  typelib-1_0-Gst-1_0 gstreamer-plugins-base gstreamer-plugins-good \
  gstreamer-plugin-pipewire wl-clipboard
```

Le raccourci et l'extension supposent GNOME ; sur un autre bureau, seuls le
service, l'application et `greffierctl` fonctionnent (associez
`greffierctl toggle` à un raccourci de votre bureau).

## Installation pour le développement

Voir [CONTRIBUTING.md](../CONTRIBUTING.md) : `make dev` crée un
environnement `.venv` dans le dépôt, et `./packaging/install.sh --dev` fait
tourner le service installé directement sur le code du dépôt.

## En cas de problème

Voir [TROUBLESHOOTING.md](TROUBLESHOOTING.md), et lancez
`greffierctl doctor`. Le script s'arrête au premier problème bloquant avec
un message explicatif ; `./packaging/install.sh --dry-run` montre ce qu'il
ferait, et `bash -x ./packaging/install.sh` le détail de chaque commande.
