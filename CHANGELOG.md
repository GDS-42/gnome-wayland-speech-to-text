# Journal des modifications

Toutes les évolutions notables de Greffier sont consignées ici.
Format : [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) ;
numérotation : [versionnage sémantique](https://semver.org/lang/fr/).
Les nouveaux modèles et les corrections du catalogue arrivent par le
catalogue en ligne et ne figurent pas forcément ici.

## [Non publié]

## [0.1.0] - 2026-10-28

Première version publique.

### Ajouté

- Service de dictée en arrière-plan (`greffier-daemon`), service utilisateur
  systemd activable par D-Bus, démarré avec la session GNOME.
- Reconnaissance vocale locale avec sherpa-onnx ; modèle par défaut NVIDIA
  Parakeet TDT 0.6B v3, et au choix Canary 180M Flash, Cohere Transcribe,
  Qwen3-ASR 0.6B et Whisper large-v3 turbo. Détection de la parole Silero VAD
  pour découper les longues dictées et s'arrêter sur un silence.
- Catalogue de modèles actualisé chaque jour, téléchargements reprenables et
  vérifiés par SHA-256, mises à jour sans nouvelle version de l'application.
- Raccourci global Super+Alt+D (raccourci personnalisé GNOME) en mode
  bascule ; mode « appuyer pour parler » par le portail GlobalShortcuts
  (GNOME 48+).
- Insertion du texte sous Wayland : extension GNOME Shell (recommandée),
  portail RemoteDesktop ou presse-papiers ; Ctrl+Maj+V dans les terminaux ;
  restauration du presse-papiers.
- Commandes vocales françaises (« à la ligne », « nouveau paragraphe »,
  « ouvrez les guillemets »…), remplacements personnalisés, typographie
  française optionnelle.
- Application GTK 4 / libadwaita : premiers pas, choix et téléchargement des
  modèles, préférences, historique, diagnostic.
- Extension GNOME Shell : indicateur dans la barre supérieure, incrustation
  pendant l'enregistrement, insertion sans vol de focus.
- Outil en ligne de commande `greffierctl` (`toggle`, `status`, `models`,
  `config`, `history`, `devices`, `doctor`…).
- Interface en français et en anglais.
- Installation par utilisateur (`packaging/install.sh`) et désinstallation
  (`uninstall.sh`, `--purge`), intégration continue Ubuntu et Fedora.

[Non publié]: https://github.com/GDS-42/gnome-wayland-speech-to-text/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/GDS-42/gnome-wayland-speech-to-text/releases/tag/v0.1.0
