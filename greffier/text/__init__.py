"""Text post-processing: voice commands, user replacements, typography."""

from greffier.text.commands import apply_commands
from greffier.text.postprocess import (
    apply_replacements,
    french_typography,
    normalise_whitespace,
    postprocess,
)

__all__ = [
    "apply_commands",
    "apply_replacements",
    "french_typography",
    "normalise_whitespace",
    "postprocess",
]
