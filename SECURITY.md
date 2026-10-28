# Sécurité / Security

## Signaler une vulnérabilité

Merci de **ne pas** ouvrir de ticket public. Utilisez les *security advisories*
privés de GitHub (onglet **Security › Report a vulnerability** du dépôt). Nous
répondons sous une semaine et publions un correctif avant toute divulgation.

Please do **not** open a public issue: use GitHub's private security advisories
(**Security › Report a vulnerability**).

## Modèle de menace (résumé)

Greffier entend ce que vous dites et peut écrire dans n'importe quelle fenêtre :
ces deux capacités sont protégées ainsi.

| Ce qui est protégé | Comment |
|---|---|
| Votre voix et vos textes | Tout est traité localement ; aucune donnée n'est envoyée. Le micro n'est ouvert que pendant une dictée (l'indicateur de GNOME le montre). L'historique reste en mémoire, sauf option contraire ; s'il est enregistré, le fichier est en mode 0600. |
| Les modèles téléchargés | HTTPS uniquement (redirections comprises), taille et **SHA-256** vérifiées, extraction refusant chemins absolus, `..`, liens et fichiers spéciaux, installation atomique. Le catalogue distant est validé avant d'être mis en cache. |
| L'injection de texte et le presse-papiers | L'extension GNOME Shell n'accepte que les appels du service Greffier démarré par systemd (nom D-Bus **et** cgroup `app-io.github.greffier.Greffier.Daemon.service`). Elle n'expose aucune méthode de lecture du presse-papiers. Le service ne peut pas être remplacé sur le bus pendant qu'il tourne, et il ne parle à l'extension que si celle-ci appartient bien au processus `gnome-shell`. |
| Le portail de bureau à distance | Consentement explicite de GNOME la première fois ; la session est fermée après quelques secondes d'inactivité. |

Limites assumées : comme tout service de bureau, Greffier fait confiance aux
programmes **non isolés** de votre propre session (ils peuvent déjà lire vos
fichiers et enregistrer le micro). Les applications Flatpak isolées n'ont pas
accès à son interface D-Bus, sauf si elles disposent d'un accès complet au bus
de session — elles ne sont alors plus réellement isolées. Greffier ne lit jamais
le clavier (pas d'accès à `/dev/input`).
