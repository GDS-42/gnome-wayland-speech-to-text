// SPDX-License-Identifier: GPL-3.0-or-later
// Tiny string table: the extension ships no gettext catalog. English is the
// source language; French is the main audience.

import GLib from 'gi://GLib';

const NBSP = ' ';

const TABLES = {
    fr: {
        'Dictation': 'Dictée',
        'Start dictation': 'Démarrer la dictée',
        'Stop dictation': 'Arrêter la dictée',
        'Cancel': 'Annuler',
        'Open Greffier': 'Ouvrir Greffier',
        'Preferences': 'Préférences',
        'Ready': 'Prêt',
        'Recording…': 'Enregistrement…',
        'Transcribing…': 'Transcription…',
        'Loading the speech model…': 'Chargement du modèle vocal…',
        'No speech model installed': 'Aucun modèle vocal installé',
        'Error': 'Erreur',
        'Service not running': 'Service arrêté',
        'Model: %s': `Modèle${NBSP}: %s`,
        'No model selected': 'Aucun modèle sélectionné',
        'Copied: press Ctrl+V to paste': `Copié${NBSP}: Ctrl+V pour coller`,
        'Greffier is not installed': 'Greffier n’est pas installé',
    },
};

function pickTable() {
    for (const name of GLib.get_language_names()) {
        const lang = name.split(/[_.@]/)[0].toLowerCase();
        if (lang === 'c' || lang === 'posix' || lang === 'en')
            return null;
        if (TABLES[lang])
            return TABLES[lang];
    }
    return null;
}

let table;

/**
 * Translate an English source string.
 *
 * @param {string} text English string
 * @returns {string} the translation, or `text`
 */
export function _(text) {
    if (table === undefined)
        table = pickTable();
    return table?.[text] ?? text;
}

/**
 * Translate and substitute `%s` placeholders in order.
 *
 * @param {string} text English string with `%s`
 * @param {...string} args values
 * @returns {string}
 */
export function format(text, ...args) {
    let i = 0;
    return _(text).replace(/%s/g, () => String(args[i++] ?? ''));
}
