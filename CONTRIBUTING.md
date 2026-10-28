# Contribuer à Greffier

Merci de votre intérêt ! Greffier est développé en français d'abord, mais
les contributions en anglais sont tout aussi bienvenues
(*English is fine everywhere: issues, pull requests, commits*).

- **Un bogue ?** Ouvrez un ticket avec la sortie de `greffierctl doctor`,
  votre version de Fedora/GNOME et un extrait du journal
  (`journalctl --user -u app-io.github.greffier.Greffier.Daemon.service -b`).
- **Une idée ?** Ouvrez d'abord un ticket pour en discuter, surtout si elle
  touche à l'architecture ([docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).
- **Une traduction, un modèle, un correctif ?** Lisez la suite.

## Environnement de développement

Prérequis : les paquets système du [README](README.md#prérequis), plus
`git`, `make`, `ShellCheck` et `nodejs` (pour vérifier l'extension) :

```bash
sudo dnf install git make ShellCheck nodejs desktop-file-utils appstream gettext
git clone https://github.com/greffier/greffier.git && cd greffier
make dev            # .venv avec --system-site-packages + pip install -e '.[dev]'
```

Le virtualenv **doit** voir les paquets système (`--system-site-packages`) :
PyGObject, GTK 4 et GStreamer viennent de la distribution, pas de PyPI.

Deux façons de lancer votre code :

- **Au premier plan**, sans rien installer :
  `make run-daemon` (journaux détaillés ; arrête d'abord le service installé)
  et, dans un autre terminal, `make run-gui`. L'extension GNOME Shell ne fait
  confiance qu'au service lancé par systemd : au premier plan, l'insertion
  passe par le portail ou le presse-papiers.
- **Comme un utilisateur** : `./packaging/install.sh --dev` installe le
  service, les fichiers `.desktop` et l'extension, avec une installation
  « éditable » qui pointe sur votre dépôt. Après une modification :
  `systemctl --user restart app-io.github.greffier.Greffier.Daemon.service`.
  L'extension, elle, n'est rechargée qu'à la reconnexion (Wayland) ; pour
  itérer plus vite, lancez une session imbriquée :
  `dbus-run-session gnome-shell --devkit --wayland` (GNOME 49+) ou
  `dbus-run-session -- gnome-shell --nested --wayland` (GNOME 48).

## Tests

```bash
make test           # rapides : ni modèle réel, ni affichage (ce que fait la CI)
make test-all       # tout, y compris les tests « slow » et « gui »
make lint           # ruff + shellcheck + node --check sur l'extension
make validate       # desktop-file-validate + appstreamcli validate
```

- Chaque test tourne avec `GREFFIER_HOME` dans un dossier temporaire : vos
  réglages et modèles ne sont jamais touchés.
- Les tests `slow` utilisent de vrais modèles : extrayez les archives
  sherpa-onnx dans un dossier et indiquez-le avec `GREFFIER_TEST_DL=/chemin`
  (ils sont ignorés sinon). `tools/e2e_smoke.py` fait un essai de bout en
  bout du service réel avec un fichier WAV à la place du micro.
- Les tests `gui` demandent un affichage (session Wayland, ou
  `GDK_BACKEND=broadway` avec `gtk4-broadwayd`).
- Les tests d'empaquetage (`tests/test_packaging_*.py`) installent Greffier
  dans un `HOME` temporaire. Les outils de session (`systemctl`, `gsettings`,
  `gnome-extensions`…) y sont remplacés par des bouchons, et pip est sauté
  grâce à `GREFFIER_INSTALL_SKIP_PIP=1` : ils sont rapides et ne touchent ni
  à votre session ni au réseau.

La CI GitHub ([.github/workflows/ci.yml](.github/workflows/ci.yml)) lance le
lint, puis les tests sur Ubuntu 24.04 et dans un conteneur Fedora ; une
étiquette `v*` produit en plus la wheel, le sdist et l'archive de
l'extension.

## Style de code

- **Python** : 3.11+, `ruff` (configuration dans `pyproject.toml`, lignes de
  100 caractères). Annotations de types partout, `from __future__ import
  annotations`. Les signatures publiques sont décrites dans
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §6 : si vous en changez une,
  mettez le document à jour dans le même commit.
- **Fil d'exécution** : tout l'état et tout D-Bus sur le fil principal GLib ;
  le travail long dans un fil dédié, résultat renvoyé par `GLib.idle_add`.
- **Chaînes** : le code source est en anglais, les chaînes visibles sont en
  anglais et entourées de `_()` (`from greffier.i18n import _`), la
  traduction française vient du fichier `.po`. Commentaires en anglais.
- **Shell** (`packaging/*.sh`) : bash, `set -Eeuo pipefail`, zéro
  avertissement `shellcheck`, messages en français, toute modification de
  fichier passe par les fonctions qui respectent `--dry-run`.
- **Extension GNOME Shell** : modules ES (GNOME 45+), pas de dépendance
  externe, rien de bloquant dans `enable()`/`disable()`, tout est défait dans
  `disable()` (règles de relecture d'extensions.gnome.org).
- **Identifiants** : tout dérive de `APP_ID` dans `greffier/const.py` ; les
  fichiers de `data/` et les scripts doivent suivre (des tests le vérifient).

## Traductions

Les traductions sont de simples fichiers gettext `.po` dans
`greffier/data/locale/` (`fr.po`, …), lus directement au démarrage : aucune
compilation n'est nécessaire.

```bash
make pot                                    # extraire les chaînes → greffier/data/locale/greffier.pot
make update-po                              # fusionner dans chaque .po existant
cp greffier/data/locale/greffier.pot greffier/data/locale/es.po   # nouvelle langue
GREFFIER_LANG=es make run-gui               # vérifier le résultat
```

Éditez les `.po` avec Poedit ou l'Éditeur de traduction de GNOME. Le
français est la langue de référence : il doit rester complet. Les entrées
« fuzzy » sont ignorées à l'exécution. Laissez les marqueurs comme `{n}` ou
`{path}` intacts. Les chaînes de l'extension GNOME Shell se trouvent dans
`gnome-extension/greffier@greffier.github.io/` (voir `i18n.js`).

## Ajouter un modèle au catalogue

Le catalogue `greffier/data/catalog.json` décrit chaque modèle : archive,
taille, **SHA-256**, dossier racine et correspondance rôle → fichier pour sa
*famille* de moteur (tableau dans [ARCHITECTURE.md](docs/ARCHITECTURE.md) §5).
Seules les familles prises en charge par `greffier/stt` peuvent être
ajoutées sans code.

1. Générez l'entrée à partir de l'archive (le script la télécharge, calcule
   taille et SHA-256, détecte la racine et les fichiers) :

   ```bash
   python tools/catalog_tool.py entry https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/<archive>.tar.bz2 …
   ```

   (`python tools/catalog_tool.py --help` pour les options : famille,
   langues, nom, licence…)
2. Ajoutez-la à `models`, complétez les descriptions `fr` et `en`, la
   licence et la page d'origine, puis **incrémentez `revision`** en tête du
   fichier : c'est elle qui fait gagner le nouveau catalogue chez les
   utilisateurs (il est récupéré chaque jour depuis la branche `main`).
3. Vérifiez : `make test`, puis `greffierctl models install <id>` et une
   vraie dictée. Indiquez dans la demande de fusion la mémoire utilisée et la
   vitesse mesurée.

Ne proposez que des modèles dont la licence permet la redistribution et
l'usage local, et dont l'archive est hébergée de façon pérenne (de
préférence les publications de sherpa-onnx sur GitHub).

## Commits et demandes de fusion

- Messages au format [Conventional Commits](https://www.conventionalcommits.org/fr/),
  en anglais et à l'impératif : `type(portée): résumé`.
  Types : `feat`, `fix`, `docs`, `i18n`, `test`, `refactor`, `perf`,
  `build` (empaquetage, CI), `chore`. Portées usuelles : `daemon`, `ui`,
  `cli`, `extension`, `models`, `stt`, `audio`, `desktop`, `text`,
  `packaging`.
  Exemples : `fix(desktop): restore the clipboard after a failed paste`,
  `i18n(fr): translate the onboarding page`.
- Un changement logique par commit ; `make lint test` doit passer à chaque
  étape.
- Ajoutez une ligne à la section « Non publié » de
  [CHANGELOG.md](CHANGELOG.md) pour tout changement visible.
- Une demande de fusion explique le *pourquoi*, renvoie au ticket, et
  contient des tests quand c'est possible.

## Publier une version (mainteneurs)

1. Mettre à jour `__version__` dans `greffier/__init__.py`, `version-name`
   dans `gnome-extension/…/metadata.json`, la balise `<release>` du fichier
   metainfo et `CHANGELOG.md` (date).
2. `git tag -s vX.Y.Z && git push --tags` : la CI vérifie que l'étiquette
   correspond à la version, puis publie la wheel, le sdist, l'archive de
   l'extension et `SHA256SUMS` sur la page des versions GitHub.
3. Envoyer l'archive de l'extension sur extensions.gnome.org.

## Licence

En contribuant, vous acceptez que votre contribution soit publiée sous la
licence du projet, la [GPL v3 ou ultérieure](LICENSE).
