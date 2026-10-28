"""GTK 4 / libadwaita user interface of Greffier.

The interface is a pure D-Bus client of ``greffier-daemon`` (see
``greffier/data/daemon-interface.xml``): it never touches the configuration
file, the models or the microphone itself. Closing the window never stops
dictation.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Adw", "1")
