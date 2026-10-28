# Greffier

**Dictée vocale locale et privée pour GNOME sous Wayland.**
Appuyez sur un raccourci, parlez, le texte s'écrit dans l'application où se
trouve le curseur. La reconnaissance vocale tourne entièrement sur votre
machine : aucun son ni aucun texte ne quitte votre ordinateur.

*English version below: [Greffier in English](#english).*

![Greffier : la fenêtre principale](docs/screenshots/main.png)

> **État du projet : version 0.1.0, première publication.** Greffier est
> utilisable au quotidien sur Fedora Workstation (GNOME 48 à 50), mais c'est
> une première version : attendez-vous à quelques angles vifs et
> [signalez-les](https://github.com/greffier/greffier/issues) !

## Pourquoi Greffier ?

- **Local et privé** : la voix est transcrite par
  [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) sur votre processeur.
  Pas de compte, pas de nuage, pas de télémétrie.
- **Rapide** : avec le modèle par défaut (NVIDIA Parakeet TDT 0.6B v3), une
  minute de parole est transcrite en quelques secondes sur un processeur à
  4 cœurs.
- **Pensé pour le français** : ponctuation et majuscules automatiques,
  commandes vocales en français (« à la ligne », « ouvrez les guillemets »…),
  typographie française en option (espaces fines insécables).
- **Intégré à GNOME** : raccourci visible dans Paramètres › Clavier,
  notifications, extension GNOME Shell optionnelle pour un retour visuel et
  une insertion du texte instantanée.

## Fonctionnalités

- Dictée **bascule** (un appui pour commencer, un autre pour insérer) ou
  **appuyer pour parler** (GNOME 48 ou plus récent).
- Insertion du texte dans n'importe quelle application, y compris les
  terminaux, avec les accents et quelle que soit la disposition du clavier.
- Mode **continu** optionnel : le texte est inséré à chaque pause pendant que
  vous parlez ; arrêt automatique sur silence en option.
- **Commandes vocales** et **remplacements** personnalisés, pour les noms
  propres ou les termes que le modèle écrit mal.
- Plusieurs **modèles** au choix, téléchargés et vérifiés à la demande, mis à
  jour sans nouvelle version de l'application.
- **Historique** des dernières dictées (en mémoire seulement, par défaut).
- Une application GTK 4 / libadwaita, une commande `greffierctl` et un
  service en arrière-plan qui continue de fonctionner fenêtre fermée.

## Modèles disponibles

| Modèle | Téléchargement | Mémoire | Langues | Licence |
|---|---|---|---|---|
| **NVIDIA Parakeet TDT 0.6B v3** (recommandé) | ~487 Mo | ~900 Mo | 25 langues européennes, détection automatique | CC-BY-4.0 |
| NVIDIA Canary 180M Flash (machines modestes) | 154 Mo | ~450 Mo | français, anglais, allemand, espagnol | CC-BY-4.0 |
| Cohere Transcribe | 1,7 Go | ~3,5 Go | 14 langues | Apache-2.0 |
| Qwen3-ASR 0.6B | 879 Mo | ~1,6 Go | 30 langues, détection automatique | Apache-2.0 |
| OpenAI Whisper large-v3 turbo | 564 Mo | ~1,6 Go | 99 langues | MIT |

Parakeet v3 transcrit environ dix fois plus vite que le temps réel sur un
processeur à 4 cœurs (une minute de parole en ~6 secondes) ; c'est le
meilleur compromis précision / vitesse pour le français. Canary convient aux
machines avec peu de mémoire, Cohere vise la précision maximale, Whisper le
plus grand nombre de langues.

## Prérequis

- **Fedora Workstation 42, 43 ou 44** (GNOME 48 à 50, session Wayland). Les
  autres distributions récentes avec GNOME devraient fonctionner, mais ne
  sont pas testées.
- Python 3.11 ou plus récent (celui de la distribution), PipeWire.
- Environ 1 Go de disque et 1 Go de mémoire libre pour le modèle par défaut.
- Paquets système (installés par le script si vous le souhaitez) :
  `python3-gobject gtk4 libadwaita gstreamer1 gstreamer1-plugins-base
  gstreamer1-plugins-good pipewire-gstreamer wl-clipboard`.
  Optionnel : `ydotool` (dernier recours pour la saisie).

## Installation

```bash
git clone https://github.com/greffier/greffier.git
cd greffier
./packaging/install.sh
```

Le script installe Greffier **pour votre utilisateur uniquement** (dans
`~/.local`), sans droits administrateur. S'il manque des paquets système, il
les liste et propose de les installer avec `sudo dnf install` (c'est le seul
usage de `sudo`). Il installe ensuite l'extension GNOME Shell : **sous
Wayland, déconnectez-vous puis reconnectez-vous une fois** pour qu'elle soit
chargée. Greffier fonctionne déjà sans elle en attendant.

Options utiles : `--yes` (aucune question), `--model canary-180m-flash-int8`
(télécharger un modèle tout de suite), `--no-extension`, `--no-autostart`,
`--dry-run` (voir ce qui serait fait) ; `./packaging/install.sh --help` pour
la liste complète. Détails (fichiers installés, mise à jour, installation
pour le développement) : [docs/INSTALL.md](docs/INSTALL.md).

**Mise à jour** : `git pull` puis relancez `./packaging/install.sh`.

## Utilisation

1. Ouvrez **Greffier** depuis la vue des applications. Au premier lancement,
   choisissez un modèle : il est téléchargé (une seule fois) puis vérifié.
2. Placez le curseur là où vous voulez écrire, appuyez sur
   **Super+Alt+D** (touche Windows + Alt + D), parlez, puis appuyez de
   nouveau : le texte est inséré (`greffierctl cancel` annule un
   enregistrement en cours).
3. Le raccourci se change dans Greffier ou dans Paramètres › Clavier ›
   Raccourcis personnalisés. Le mode **appuyer pour parler** (maintenir la
   touche pendant que vous parlez) demande GNOME 48 ou plus récent.

### Commandes vocales

Dites-les comme des mots isolés, entre deux phrases :

| Vous dites | Greffier écrit |
|---|---|
| « à la ligne », « retour à la ligne », « nouvelle ligne » | un retour à la ligne |
| « point à la ligne » | `.` puis un retour à la ligne |
| « nouveau paragraphe » | une ligne vide |
| « point d'interrogation », « point d'exclamation » | `?`, `!` |
| « point-virgule », « deux points » | `;`, `:` |
| « ouvrez les guillemets » … « fermez les guillemets » | `«` … `»` |
| « ouvrez la parenthèse » … « fermez la parenthèse » | `(` … `)` |

La ponctuation courante (virgules, points) est ajoutée par le modèle
lui-même. Les phrases ordinaires ne sont pas touchées : « aller à la ligne
suivante » reste tel quel. En anglais : « new line », « new paragraph ». Les
commandes se désactivent dans les préférences.

### En ligne de commande

```bash
greffierctl toggle                       # démarrer / arrêter la dictée
greffierctl status                       # état du service et du modèle
greffierctl models list                  # modèles disponibles et installés
greffierctl models install canary-180m-flash-int8
greffierctl models use canary-180m-flash-int8
greffierctl doctor                       # diagnostic de l'installation
greffierctl config set text.french_typography true
```

## Comment le texte est-il inséré ?

Wayland interdit à une application d'écrire dans les fenêtres des autres,
c'est une bonne chose pour la sécurité. Greffier passe donc par le
presse-papiers puis simule le raccourci « coller », en restaurant ensuite
votre presse-papiers. Trois chemins, du meilleur au plus modeste :

1. **L'extension GNOME Shell** (recommandée, installée par le script) :
   insertion invisible et instantanée, indicateur dans la barre supérieure,
   Ctrl+Maj+V automatique dans les terminaux.
2. **Le portail « Bureau à distance »** de GNOME : GNOME vous demande une
   fois l'autorisation de contrôler le clavier ; Greffier mémorise votre
   accord.
3. **Le presse-papiers seul** (`wl-copy`) : le texte est copié et une
   notification vous invite à le coller avec Ctrl+V.

## Mises à jour des modèles

Le catalogue des modèles est intégré à l'application et actualisé une fois
par jour depuis ce dépôt GitHub, ce qui permet d'ajouter des modèles ou de
corriger un lien sans nouvelle version de Greffier. Chaque archive est
vérifiée par **SHA-256** avant d'être installée ; un téléchargement
interrompu reprend là où il s'était arrêté. Greffier vous signale les mises à
jour, l'ancienne version reste utilisable jusqu'à ce que la nouvelle soit
complète.

## Vie privée

- Le son du micro n'est jamais enregistré sur le disque ni envoyé sur le
  réseau ; il est transcrit en mémoire puis oublié.
- L'historique des dictées reste en mémoire, sauf si vous activez sa
  sauvegarde (`~/.local/state/greffier/history.jsonl`).
- **Seules connexions réseau** : le téléchargement des modèles et du
  catalogue depuis GitHub (`github.com`, `raw.githubusercontent.com`),
  désactivable (`greffierctl config set model.check_updates false`). Aucune
  télémétrie.

## Désinstallation

```bash
~/.local/share/greffier/uninstall.sh            # garde réglages et modèles
~/.local/share/greffier/uninstall.sh --purge    # efface aussi modèles, réglages, historique
```

(ou `./packaging/uninstall.sh` depuis le dépôt). Le raccourci clavier et
l'extension sont retirés ; les paquets système ne sont pas touchés.

## Documentation

- [Installation détaillée](docs/INSTALL.md)
- [Dépannage](docs/TROUBLESHOOTING.md) : raccourci sans effet, texte non
  inséré, micro, téléchargements, mémoire…
- [Architecture](docs/ARCHITECTURE.md) (en anglais)
- [Contribuer](CONTRIBUTING.md) : environnement de développement, tests,
  traductions, ajout d'un modèle
- [Journal des modifications](CHANGELOG.md)

## Contribuer

Les rapports de bogues, traductions, idées et correctifs sont les bienvenus.
Lisez [CONTRIBUTING.md](CONTRIBUTING.md) ; pour un bogue, joignez la sortie
de `greffierctl doctor`.

## Licence et remerciements

Greffier est un logiciel libre sous licence
**[GNU GPL v3 ou ultérieure](LICENSE)**.

Il s'appuie sur le travail de :

- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (k2-fsa, Apache-2.0),
  le moteur de reconnaissance, et ses exports ONNX des modèles ;
- **NVIDIA** pour Parakeet TDT 0.6B v3 et Canary 180M Flash (CC-BY-4.0) ;
- **Cohere Labs** pour Cohere Transcribe (Apache-2.0) ;
- l'équipe **Qwen** d'Alibaba pour Qwen3-ASR (Apache-2.0) ;
- **OpenAI** pour Whisper (MIT) ;
- [Silero](https://github.com/snakers4/silero-vad) pour la détection de la
  parole (Silero VAD, MIT) ;
- GNOME, GTK, libadwaita, GStreamer et PipeWire.

Les modèles ne font pas partie de Greffier : ils sont téléchargés à la
demande (exports ONNX publiés par sherpa-onnx) et restent sous la licence de
leur auteur, indiquée dans le tableau ci-dessus. Les licences CC-BY-4.0 et
Apache-2.0 demandent de créditer l'auteur si vous redistribuez le modèle
lui-même.

---

<a id="english"></a>

## English

**Greffier is a local, private voice-dictation tool for GNOME on Wayland**
(Fedora first). Press a shortcut, speak, and the text appears in the focused
application. Speech recognition runs entirely on your machine with
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx); the default model,
NVIDIA Parakeet TDT 0.6B v3, transcribes about ten times faster than real
time on a 4-core CPU, with punctuation and capitalisation. Built for French
speakers first (voice commands, French typography); the interface is
available in French and English.

> **Status: 0.1.0, first public release.** Daily-driver quality on Fedora
> Workstation (GNOME 48–50), but expect rough edges.

**Install** (per user, no root except an optional `sudo dnf install` of
missing system packages):

```bash
git clone https://github.com/greffier/greffier.git && cd greffier
./packaging/install.sh        # --help for options; log out/in once for the extension
```

**Use**: open Greffier, pick a model (Parakeet v3 recommended: ~487 MB
download, ~900 MB RAM), then press **Super+Alt+D**, speak, press it again.
Push-to-talk needs GNOME 48+. Voice commands: "new line", "new paragraph"
(and many more in French). CLI: `greffierctl toggle | status | models list |
models install <id> | doctor`.

**Models**: Parakeet TDT 0.6B v3 (CC-BY-4.0), Canary 180M Flash (154 MB,
450 MB RAM, CC-BY-4.0), Cohere Transcribe (1.7 GB, 3.5 GB RAM, Apache-2.0),
Qwen3-ASR 0.6B (879 MB, 1.6 GB RAM, Apache-2.0), Whisper large-v3 turbo
(564 MB, 1.6 GB RAM, MIT). The catalog is refreshed daily from this
repository and every archive is SHA-256-verified.

**Text insertion on Wayland**: through the optional GNOME Shell extension
(recommended), else the RemoteDesktop portal (asks once for consent), else
the clipboard only.

**Privacy**: audio never leaves the machine and is never written to disk; the
only network traffic is model and catalog downloads from GitHub.

**Uninstall**: `~/.local/share/greffier/uninstall.sh [--purge]`.
Troubleshooting (French): [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md);
architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); contributing:
[CONTRIBUTING.md](CONTRIBUTING.md).

**License**: GPL-3.0-or-later. Models keep their own licenses (see above);
thanks to k2-fsa/sherpa-onnx, NVIDIA, Cohere, Qwen, OpenAI and Silero.
