"""Table-driven tests of greffier.text: what users actually see typed."""

from __future__ import annotations

import pytest

from greffier.config import Replacement, TextConfig
from greffier.text import apply_commands, french_typography, normalise_whitespace, postprocess
from greffier.text.postprocess import NBSP, NNBSP, normalise_language

PLAIN = TextConfig(voice_commands=True, french_typography=False)
TYPO = TextConfig(voice_commands=True, french_typography=True)
RAW = TextConfig(voice_commands=False, french_typography=False)

# (input, expected) with voice commands on, French, no typography.
FR_COMMANDS = [
    # line breaks, with the punctuation the model put around the command
    ("Bonjour. À la ligne. Comment ça va ?", "Bonjour.\nComment ça va ?"),
    ("Bonjour, à la ligne, comment ça va ?", "Bonjour,\nComment ça va ?"),
    ("Bonjour. Retour à la ligne. Merci.", "Bonjour.\nMerci."),
    ("Bonjour, nouvelle ligne, comment ça va ?", "Bonjour,\nComment ça va ?"),
    ("Bonjour nouvelle ligne", "Bonjour\n"),
    ("Merci. Nouveau paragraphe. Cordialement.", "Merci.\n\nCordialement."),
    ("À la ligne.", "\n"),
    ("À la ligne. À la ligne. Fin.", "\n\nFin."),
    ("Bonjour. A la ligne. Merci.", "Bonjour.\nMerci."),  # accent dropped by the model
    ("BONJOUR. À LA LIGNE. MERCI.", "BONJOUR.\nMERCI."),
    ("Bonjour. À la ligne ! Merci.", "Bonjour.\nMerci."),
    ("Bonjour, point à la ligne, merci.", "Bonjour.\nMerci."),
    ("Premier point. Nouveau paragraphe, et ensuite.", "Premier point.\n\nEt ensuite."),
    # ordinary prose must stay untouched
    ("Il faut passer à la ligne suivante.", "Il faut passer à la ligne suivante."),
    ("Va à la ligne.", "Va à la ligne."),
    ("Il trace une nouvelle ligne.", "Il trace une nouvelle ligne."),
    ("Je commence un nouveau paragraphe.", "Je commence un nouveau paragraphe."),
    ("Il y a la ligne trois.", "Il y a la ligne trois."),
    ("C'est le retour à la ligne.", "C'est le retour à la ligne."),
    ("On ouvre une nouvelle ligne de métro.", "On ouvre une nouvelle ligne de métro."),
    # punctuation commands
    ("Tu viens point d'interrogation", "Tu viens ?"),
    ("Tu viens, point d'interrogation.", "Tu viens ?"),
    ("Tu viens, point d'interrogation, je pars.", "Tu viens ? Je pars."),
    ("Tu viens point d’interrogation", "Tu viens ?"),  # typographic apostrophe
    ("C'est génial, point d'exclamation.", "C'est génial !"),
    ("Quoi ? Point d'exclamation.", "Quoi ?!"),
    ("J'arrive, point-virgule, je pars.", "J'arrive ; je pars."),
    ("J'arrive, point virgule, je pars.", "J'arrive ; je pars."),
    ("Voici la liste, deux points. Le premier.", "Voici la liste : Le premier."),
    ("Voici la liste. Deux-points.", "Voici la liste :"),
    ("C'est un point d'interrogation.", "C'est un point d'interrogation."),
    ("Il manque un point-virgule.", "Il manque un point-virgule."),
    ("les deux points communs", "les deux points communs"),
    ("J'ai marqué deux points.", "J'ai marqué deux points."),
    ("Il y a deux points à voir.", "Il y a deux points à voir."),
    # quotes and parentheses
    ("Il a dit, ouvrez les guillemets, je viens, fermez les guillemets.",
     "Il a dit « je viens »."),
    ("Il a dit ouvrez les guillemets je viens fermez les guillemets.",
     "Il a dit « je viens »."),
    ("Ouvrir les guillemets. Ça va. Fermer les guillemets.", "« Ça va. »"),
    ("Ouvrez les guillemets, oui, fermez les guillemets, dit-il.", "« oui », dit-il."),
    ("Ouvrir la parenthèse, entre nous, fermer la parenthèse, c'est fini.",
     "(entre nous), c'est fini."),
    ("Le prix ouvrez la parenthèse hors taxes fermez la parenthèse est bas.",
     "Le prix (hors taxes) est bas."),
]

EN_COMMANDS = [
    ("Hello. New line. How are you?", "Hello.\nHow are you?"),
    ("Hello, new line, how are you?", "Hello,\nHow are you?"),
    ("Thanks. New paragraph. Best regards.", "Thanks.\n\nBest regards."),
    ("Start a new line.", "Start a new line."),
    ("Draw the new line here.", "Draw the new line here."),
]


@pytest.mark.parametrize(("text", "expected"), FR_COMMANDS)
def test_french_commands(text: str, expected: str) -> None:
    assert postprocess(text, PLAIN, "fr") == expected


@pytest.mark.parametrize(("text", "expected"), EN_COMMANDS)
def test_english_commands(text: str, expected: str) -> None:
    assert postprocess(text, PLAIN, "en") == expected


@pytest.mark.parametrize(
    ("text", "language", "expected"),
    [
        ("Hello. New line. Bye.", "fr", "Hello. New line. Bye."),  # EN table not active
        ("Bonjour. À la ligne. Merci.", "en", "Bonjour. À la ligne. Merci."),
        ("Bonjour. À la ligne. Hello. New line. Bye.", "auto",
         "Bonjour.\nHello.\nBye."),
        ("Bonjour. À la ligne. Merci.", None, "Bonjour.\nMerci."),  # None = auto
        ("Bonjour. À la ligne. Merci.", "fr_FR", "Bonjour.\nMerci."),
        ("Hallo. À la ligne. Danke.", "de", "Hallo. À la ligne. Danke."),  # no table
    ],
)
def test_command_tables_by_language(text: str, language: str | None, expected: str) -> None:
    assert postprocess(text, PLAIN, language) == expected


def test_commands_disabled() -> None:
    text = "Bonjour. À la ligne. Tu viens, point d'interrogation."
    assert postprocess(text, RAW, "fr") == text


def test_quotes_with_typography() -> None:
    text = "Il a dit, ouvrez les guillemets, je viens, fermez les guillemets."
    assert postprocess(text, TYPO, "fr") == f"Il a dit «{NBSP}je viens{NBSP}»."


def test_punctuation_command_with_typography() -> None:
    assert postprocess("Tu viens, point d'interrogation.", TYPO, "fr") == f"Tu viens{NNBSP}?"


# --------------------------------------------------------------------------- #
# Replacements
# --------------------------------------------------------------------------- #

REPLACEMENTS = [
    Replacement("greffier", "Greffier"),
    Replacement("été", "ÉTÉ"),
    Replacement("mon adresse mail", "moi@exemple.fr"),
    Replacement("mon adresse", "rue de la Paix"),
    Replacement("c plus plus", "C++"),
    Replacement("   ", "ignored"),  # blank entries are skipped
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Le greffier arrive.", "Le Greffier arrive."),
        ("GREFFIER et Greffier.", "Greffier et Greffier."),  # case-insensitive
        ("Les greffiers arrivent.", "Les greffiers arrivent."),  # whole words only
        ("L'été est là.", "L'ÉTÉ est là."),  # elided article
        ("C'est peut-être été.", "C'est peut-être ÉTÉ."),  # not inside "peut-être"
        ("Un étés", "Un étés"),  # accented letters are word characters
        ("Envoie à mon adresse mail.", "Envoie à moi@exemple.fr."),  # longest first
        ("Envoie à Mon  Adresse.", "Envoie à rue de la Paix."),  # flexible spaces
        ("J'aime le c plus plus.", "J'aime le C++."),
    ],
)
def test_replacements(text: str, expected: str) -> None:
    cfg = TextConfig(voice_commands=True, replacements=REPLACEMENTS)
    assert postprocess(text, cfg, "fr") == expected


def test_first_replacement_wins_for_duplicates() -> None:
    cfg = TextConfig(replacements=[Replacement("Gnome", "GNOME"), Replacement("gnome", "x")])
    assert postprocess("gnome", cfg, "fr") == "GNOME"


# --------------------------------------------------------------------------- #
# Whitespace
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bonjour  le   monde", "Bonjour le monde"),
        ("Bonjour , le monde .", "Bonjour, le monde."),
        ("  Bonjour.  ", "Bonjour."),
        ("Bonjour. \n  Merci.", "Bonjour.\nMerci."),
        ("A\n\n\nB", "A\n\n\nB"),  # dictated line breaks are kept
        ("\nBonjour", "\nBonjour"),
        ("Il utilise .NET aussi", "Il utilise .NET aussi"),
        ("( entre nous )", "(entre nous)"),
        ("Comment ça va ?", "Comment ça va ?"),  # French spacing from the model is kept
        ("Bonjour\tà\ttous", "Bonjour à tous"),
        ("Fin\r\nSuite", "Fin\nSuite"),
        (f"Note{NBSP} : x", f"Note{NBSP}: x"),
    ],
)
def test_normalise_whitespace(text: str, expected: str) -> None:
    assert normalise_whitespace(text) == expected


# --------------------------------------------------------------------------- #
# French typography
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Comment ça va ?", f"Comment ça va{NNBSP}?"),
        ("Comment ça va?", f"Comment ça va{NNBSP}?"),
        ("Super !", f"Super{NNBSP}!"),
        ("Oui ; non", f"Oui{NNBSP}; non"),
        ("Note : voici", f"Note{NBSP}: voici"),
        ("Note: voici", f"Note{NBSP}: voici"),
        ("Quoi ?!", f"Quoi{NNBSP}?!"),
        ("Il a dit « bonjour ».", f"Il a dit «{NBSP}bonjour{NBSP}»."),
        ("Il a dit «bonjour».", f"Il a dit «{NBSP}bonjour{NBSP}»."),
        ("Rendez-vous à 10:30.", "Rendez-vous à 10:30."),
        ("Un ratio de 16:9.", "Un ratio de 16:9."),
        ("Voir https://exemple.fr/page?q=1;r=2 ici", "Voir https://exemple.fr/page?q=1;r=2 ici"),
        ("Voir https://exemple.fr?", "Voir https://exemple.fr?"),
        ("Voir https://exemple.fr ?", f"Voir https://exemple.fr{NNBSP}?"),
        ("Si x != y alors", "Si x != y alors"),
        ("? au début", "? au début"),
        ("Bonjour !\nÇa va ?", f"Bonjour{NNBSP}!\nÇa va{NNBSP}?"),
    ],
)
def test_french_typography(text: str, expected: str) -> None:
    assert french_typography(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Comment ça va ? Très bien ! « Oui » : non; peut-être",
        "Il est 10:30 ; voir https://exemple.fr/?a=1 !",
        f"Déjà{NNBSP}? «{NBSP}fait{NBSP}»",
    ],
)
def test_typography_is_idempotent(text: str) -> None:
    once = postprocess(text, TYPO, "fr")
    assert postprocess(once, TYPO, "fr") == once
    assert french_typography(french_typography(text)) == french_typography(text)


def test_typography_only_for_french() -> None:
    assert postprocess("Really ?", TYPO, "en") == "Really ?"
    assert postprocess("Vraiment ?", TYPO, "auto") == f"Vraiment{NNBSP}?"


def test_typography_off_keeps_plain_spaces() -> None:
    assert postprocess("Comment ça va ? « Bien »", PLAIN, "fr") == "Comment ça va ? « Bien »"


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #


def test_empty_text() -> None:
    assert postprocess("", TYPO, "fr") == ""
    assert postprocess("   ", TYPO, "fr") == ""


def test_apply_commands_unknown_language_is_identity() -> None:
    assert apply_commands("Bonjour. À la ligne.", "xx") == "Bonjour. À la ligne."


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "auto"), ("", "auto"), ("auto", "auto"), ("FR", "fr"), ("fr-CA", "fr"),
     ("pt_BR", "pt")],
)
def test_normalise_language(value: str | None, expected: str) -> None:
    assert normalise_language(value) == expected


def test_full_pipeline() -> None:
    cfg = TextConfig(voice_commands=True, french_typography=True,
                     replacements=[Replacement("greffier", "Greffier")])
    text = ("Bonjour Marie, à la ligne, greffier marche bien, point d'exclamation. "
            "Nouveau paragraphe. Tu viens demain, point d'interrogation.")
    assert postprocess(text, cfg, "fr") == (
        f"Bonjour Marie,\nGreffier marche bien{NNBSP}!\n\nTu viens demain{NNBSP}?"
    )
