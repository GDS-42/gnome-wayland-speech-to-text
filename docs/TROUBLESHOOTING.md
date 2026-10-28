# Dépannage

Commencez toujours par :

```bash
greffierctl doctor
```

Il vérifie le service, le modèle, le micro, l'extension, les portails et les
outils (`wl-copy`, `ydotool`), et dit quoi faire pour chaque point en
erreur. Dans les commandes ci-dessous, `UNIT` désigne le service
`app-io.github.greffier.Greffier.Daemon.service` :

```bash
UNIT=app-io.github.greffier.Greffier.Daemon.service
```

## Sommaire

- [Le raccourci ne fait rien](#le-raccourci-ne-fait-rien)
- [Le texte n'est pas inséré](#le-texte-nest-pas-inséré)
- [Pas de son, micro introuvable](#pas-de-son-micro-introuvable)
- [Le téléchargement d'un modèle échoue](#le-téléchargement-dun-modèle-échoue)
- [Greffier utilise trop de mémoire](#greffier-utilise-trop-de-mémoire)
- [Utiliser une carte graphique NVIDIA (CUDA)](#utiliser-une-carte-graphique-nvidia-cuda)
- [Après une mise à niveau de Fedora](#après-une-mise-à-niveau-de-fedora)
- [Journaux](#journaux)
- [Signaler un problème](#signaler-un-problème)

## Le raccourci ne fait rien

1. **Le service répond-il ?** Lancez `greffierctl toggle` dans un terminal,
   parlez, relancez-le.
   - Si la dictée fonctionne ainsi, le problème vient du raccourci (point 2).
   - Sinon, regardez l'état du service : `systemctl --user status $UNIT`
     et les dernières lignes du journal : `journalctl --user -u $UNIT -b -n 50`.
     Un service en échec se relance avec `systemctl --user restart $UNIT`.
2. **Le raccourci existe-t-il ?** Ouvrez *Paramètres › Clavier › Raccourcis
   clavier › Raccourcis personnalisés* : une entrée « Greffier » doit
   apparaître avec la touche Super+Alt+D et une commande `gdbus call …
   Toggle`.
   - Si elle manque, ouvrez Greffier une fois : le service la recrée. Vous
     pouvez aussi choisir une autre touche dans Greffier ›
     Préférences, ou `greffierctl config set shortcut.accelerator '<Super><Alt>d'`.
   - Si GNOME signale un conflit, une autre application utilise déjà cette
     combinaison : choisissez-en une autre.
3. **Mode « appuyer pour parler »** : il demande GNOME 48 ou plus récent et
   passe par le portail des raccourcis globaux. GNOME affiche une fenêtre de
   confirmation la première fois. Si vous l'avez fermée sans valider,
   repassez en mode bascule (`greffierctl config set shortcut.mode toggle`)
   puis de nouveau en « appuyer pour parler » dans les préférences : Greffier
   redemande alors le raccourci au portail. Le mode bascule, lui, fonctionne
   sur toutes les versions de GNOME.
4. Le service n'est pas lancé et vous aviez installé avec `--no-autostart` ?
   C'est normal : il démarre au premier usage (raccourci, application). Pour
   le lancer avec la session : `systemctl --user enable --now $UNIT`.

## Le texte n'est pas inséré

La transcription apparaît dans l'historique de Greffier (ou dans
`greffierctl history`) mais rien ne s'écrit ? Voyez quel chemin est utilisé
avec `greffierctl status`, puis :

- **L'extension vient d'être installée** : sous Wayland, GNOME Shell ne
  charge une nouvelle extension qu'à l'ouverture de session.
  **Déconnectez-vous puis reconnectez-vous.** Vérifiez ensuite :
  `gnome-extensions info greffier@greffier.github.io` doit afficher
  `State: ACTIVE` (ou `ENABLED`). Sinon, activez-la dans l'application
  *Extensions* ; vérifiez aussi que l'interrupteur général « Extensions »
  n'est pas désactivé.
  Si GNOME la déclare incompatible (`OUT OF DATE`), votre version de GNOME
  est plus récente que l'extension : mettez Greffier à jour.
- **Sans l'extension**, GNOME affiche une fois une demande
  d'autorisation « contrôle à distance » (portail *RemoteDesktop*).
  Acceptez-la : Greffier la mémorise. Si vous l'avez refusée, effacez
  l'accord mémorisé puis redémarrez le service, la demande réapparaîtra :

  ```bash
  rm -f ~/.local/state/greffier/remote-desktop.token
  systemctl --user restart $UNIT
  ```

- **Terminaux** : ils collent avec Ctrl+Maj+V. Avec l'extension, Greffier
  le détecte tout seul. Sans elle, collez à la main ou, si vous dictez
  surtout dans un terminal :
  `greffierctl config set injection.paste_shortcut ctrl-shift-v`
  (attention : dans LibreOffice, Ctrl+Maj+V ouvre le « Collage spécial »).
- **Le texte est seulement copié** (notification « texte copié ») : aucun
  moyen de simuler le clavier n'est disponible. Installez l'extension, ou
  acceptez le portail, ou en dernier recours `ydotool` (`sudo dnf install
  ydotool`, puis démarrez son service `ydotoold` comme l'indique sa
  documentation ; `greffierctl doctor` dit s'il est joignable).
- **Applications Flatpak** : le collage fonctionne comme ailleurs. Si une
  application refuse le collage (certains champs protégés, applications qui
  interceptent Ctrl+V), essayez la saisie touche par touche :
  `greffierctl config set injection.method type` — elle ne sait produire que
  les caractères de votre disposition de clavier.
- **Le presse-papiers** : Greffier remet votre texte précédent après le
  collage ; une image copiée auparavant n'est pas restaurée. Désactivable
  avec `greffierctl config set injection.restore_clipboard false`.
- `wl-copy` manque (`rpm -q wl-clipboard`) : `sudo dnf install wl-clipboard`.

Pour tester l'insertion : Greffier › Préférences › Système ›
« Relancer l’assistant de configuration », puis utilisez la zone d'essai de l'assistant.

## Pas de son, micro introuvable

- `greffierctl devices` liste les micros vus par Greffier ; choisissez le
  bon dans Greffier › Préférences (« Micro »), ou laissez « par défaut ».
- *Paramètres › Son › Entrée* : vérifiez le périphérique choisi, le volume,
  et que le niveau bouge quand vous parlez. Une touche « micro coupé » du
  clavier ou du casque peut aussi être active.
- *Paramètres › Confidentialité et sécurité › Microphone* : l'accès au
  microphone doit être autorisé.
- Testez PipeWire sans Greffier : `pw-record --rate 16000 /tmp/essai.wav`,
  parlez, Ctrl+C, puis `pw-play /tmp/essai.wav`.
- Le greffon GStreamer pour PipeWire doit être installé :
  `rpm -q pipewire-gstreamer gstreamer1-plugins-good` (sinon
  `sudo dnf install pipewire-gstreamer gstreamer1-plugins-good`).
- Casque Bluetooth : en profil « haute fidélité » (A2DP) le micro est coupé ;
  passez au profil « casque » (HSP/HFP) dans *Paramètres › Son*.

## Le téléchargement d'un modèle échoue

- **Reprise** : un téléchargement interrompu (coupure réseau, mise en
  veille, Ctrl+C) reprend là où il s'était arrêté. Relancez simplement
  `greffierctl models install <id>` ou le bouton dans Greffier. Les fichiers
  partiels sont dans `~/.cache/greffier/downloads/` ; pour repartir de zéro :
  `rm -r ~/.cache/greffier/downloads`.
- **Somme de contrôle (SHA-256) incorrecte** : le fichier reçu n'est pas
  celui du catalogue (proxy qui modifie les fichiers, téléchargement
  corrompu). Il est supprimé automatiquement ; réessayez. Si l'erreur
  persiste, signalez-la : l'archive en ligne a peut-être changé.
- **Espace disque** : il faut la place de l'archive *et* du modèle extrait
  (Parakeet v3 : ~490 Mo + ~670 Mo ; Cohere : ~1,7 Go + ~2,9 Go). Vérifiez avec
  `df -h ~/.cache ~/.local/share`.
- **Réseau filtré** : Greffier doit joindre `github.com`,
  `objects.githubusercontent.com`, `release-assets.githubusercontent.com`
  (archives des modèles) et `raw.githubusercontent.com` (catalogue).
- **Proxy** : le service lit les variables `HTTPS_PROXY`/`https_proxy`, pas
  les réglages de proxy de GNOME. Comme il tourne sous systemd, donnez-les
  au gestionnaire de services de votre session :

  ```bash
  systemctl --user edit $UNIT
  # ajoutez :
  #   [Service]
  #   Environment=HTTPS_PROXY=http://proxy.exemple.fr:3128
  systemctl --user restart $UNIT
  ```

  Pour l'installation elle-même (pip), exportez `HTTPS_PROXY` avant de lancer
  `packaging/install.sh` (ou `PIP_INDEX_URL` pour un miroir PyPI).
- **Le catalogue ne se met pas à jour** : sans réseau, Greffier utilise le
  catalogue intégré ou la dernière copie téléchargée
  (`~/.cache/greffier/catalog.json`) ; cela n'empêche pas de dicter.

## Greffier utilise trop de mémoire

Un modèle chargé occupe de la mémoire tant que le service tourne :
~900 Mo pour Parakeet v3, ~450 Mo pour Canary, ~1,6 Go pour Whisper et
Qwen3-ASR, ~3,5 Go pour Cohere.

- **Prendre un modèle plus léger** :
  `greffierctl models install canary-180m-flash-int8` puis
  `greffierctl models use canary-180m-flash-int8` (français, anglais,
  allemand, espagnol).
- **Libérer la mémoire après une période d'inactivité**, par exemple
  10 minutes : `greffierctl config set model.unload_after 10`. La dictée
  suivante démarre aussitôt, le modèle se recharge pendant que vous parlez.
- **Ne pas charger le modèle à l'ouverture de session** :
  `greffierctl config set model.preload false`.
- Supprimer les modèles inutilisés : `greffierctl models list`, puis
  `greffierctl models remove <id>`.

## Utiliser une carte graphique NVIDIA (CUDA)

Ce n'est **pas pris en charge officiellement** : la version de sherpa-onnx
installée par défaut depuis PyPI ne fonctionne que sur le processeur, ce qui
suffit largement avec Parakeet v3. Pour essayer quand même, avec le pilote
NVIDIA et CUDA installés, remplacez sherpa-onnx dans l'environnement de
Greffier par une version compilée pour CUDA (voir la
[documentation de sherpa-onnx](https://k2-fsa.github.io/sherpa/onnx/python/install.html)),
puis :

```bash
greffierctl config set model.provider cuda
systemctl --user restart $UNIT
```

Revenir au processeur : `greffierctl config set model.provider cpu`.
Relancer `packaging/install.sh` conserve votre version de sherpa-onnx tant
qu'elle reste compatible.

## Après une mise à niveau de Fedora

Une nouvelle version de Fedora apporte souvent une nouvelle version de
Python, et l'environnement de Greffier doit être recréé : relancez
simplement `./packaging/install.sh` depuis le dépôt (il le détecte et le
refait ; vos réglages et modèles sont conservés).

Si pip tente alors de *compiler* sherpa-onnx et échoue, c'est qu'aucune
version précompilée n'existe encore pour ce Python. Installez la version
précédente de Python fournie par Fedora (par exemple
`sudo dnf install python3.13`) et choisissez-la :
`GREFFIER_PYTHON=/usr/bin/python3.13 ./packaging/install.sh`
(PyGObject doit alors être disponible pour cette version, sinon attendez
la publication d'une version compatible de sherpa-onnx).

## Journaux

```bash
journalctl --user -u $UNIT -b            # service, depuis le démarrage
journalctl --user -u $UNIT -f            # en direct
journalctl -b /usr/bin/gnome-shell | grep -i greffier    # extension
```

Pour des journaux détaillés, arrêtez le service puis lancez-le au premier plan
(Ctrl+C pour arrêter, puis `systemctl --user start $UNIT`) :

```bash
systemctl --user stop $UNIT
greffier-daemon --verbose
```

Lancé ainsi depuis un terminal, le service n'est pas reconnu par l'extension
GNOME Shell (elle n'accepte que le service démarré par systemd) : le texte est
alors inséré par le portail de bureau à distance ou seulement copié.

L'interface affiche ses messages si vous la lancez depuis un terminal :
`greffier`.

## Signaler un problème

Ouvrez un ticket sur <https://github.com/greffier/greffier/issues> avec :

- la sortie de `greffierctl doctor` (ou `greffierctl doctor --json`) ;
- votre version de Fedora et de GNOME ;
- les lignes utiles de `journalctl --user -u $UNIT -b` ;
- ce que vous avez fait, ce que vous attendiez, ce qui s'est passé.

Vérifiez que le journal ne contient pas de texte dicté que vous préférez
garder pour vous avant de le partager.
