# SPDX-License-Identifier: GPL-3.0-or-later
# Greffier: developer and packaging tasks. `make help` lists the targets.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

VENV          ?= .venv
PYTHON        ?= $(VENV)/bin/python
SYSTEM_PYTHON ?= $(if $(wildcard /usr/bin/python3),/usr/bin/python3,python3)
# Python for tasks that only need the standard library (works before `make dev`).
PY_ANY        := $(if $(wildcard $(PYTHON)),$(PYTHON),$(SYSTEM_PYTHON))
RUFF          ?= $(if $(wildcard $(VENV)/bin/ruff),$(VENV)/bin/ruff,ruff)
# STRICT=1 (CI): a missing lint/validation tool is an error instead of a skip.
STRICT        ?= 0
INSTALL_FLAGS   ?=
UNINSTALL_FLAGS ?=
PYTEST_ARGS     ?=
DAEMON_ARGS     ?= --verbose --replace

APP_ID   := io.github.greffier.Greffier
UNIT     := app-$(APP_ID).Daemon.service
UUID     := greffier@greffier.github.io
EXT_SRC  := gnome-extension/$(UUID)
DIST     := dist
EXT_BUILD := build/extension/$(UUID)
EXT_ZIP  := $(DIST)/$(UUID).shell-extension.zip
POT      := greffier/data/locale/greffier.pot
VERSION  := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' greffier/__init__.py)
ISSUES   := https://github.com/greffier/greffier/issues

PY_SOURCES  = $(shell find greffier -name '*.py' -not -path '*/__pycache__/*' | LC_ALL=C sort)
UI_SOURCES  = $(wildcard greffier/ui/*.ui)
EXT_JS      = $(shell [ -d $(EXT_SRC) ] && find $(EXT_SRC) -name '*.js' -not -path '*/node_modules/*' | LC_ALL=C sort)
SHELL_SCRIPTS := packaging/install.sh packaging/uninstall.sh

# $(call need,tool): succeed if the tool exists; otherwise skip (exit 0 from the
# recipe line) or fail under STRICT=1.
need = command -v $(1) >/dev/null 2>&1 || { if [ "$(STRICT)" = 1 ]; then echo "✗ $(1) introuvable" >&2; exit 1; fi; echo "  (ignoré : $(1) introuvable)"; exit 0; }

.PHONY: help install uninstall dev test test-all lint lint-python lint-shell lint-js validate run-daemon run-gui \
        sounds pot update-po extension-zip dist clean

help: ## Afficher cette aide
	@echo "Greffier $(VERSION) — cibles disponibles :"
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -e 's/:.*## /\t/' | expand -t 16 | sed 's/^/  /'
	@echo
	@echo "Variables utiles : INSTALL_FLAGS='--dev --model parakeet-tdt-0.6b-v3-int8', PYTEST_ARGS='-k shortcut', STRICT=1"

# --------------------------------------------------------------------------- #
# Installation for the current user (see docs/INSTALL.md)
# --------------------------------------------------------------------------- #
install: ## Installer pour l'utilisateur courant (options : INSTALL_FLAGS=…)
	./packaging/install.sh $(INSTALL_FLAGS)

uninstall: ## Désinstaller (UNINSTALL_FLAGS=--purge pour tout effacer)
	./packaging/uninstall.sh $(UNINSTALL_FLAGS)

# --------------------------------------------------------------------------- #
# Development
# --------------------------------------------------------------------------- #
dev: ## Créer .venv (paquets système visibles) et installer Greffier en éditable + outils de dev
	@if [ ! -x "$(PYTHON)" ]; then \
	  echo "→ création de $(VENV) avec $(SYSTEM_PYTHON) (--system-site-packages pour PyGObject)"; \
	  $(SYSTEM_PYTHON) -m venv --system-site-packages $(VENV); \
	fi
	@if $(PYTHON) -m pip --version >/dev/null 2>&1; then \
	  $(PYTHON) -m pip install --disable-pip-version-check -e '.[dev]'; \
	elif command -v uv >/dev/null 2>&1; then \
	  uv pip install --python $(PYTHON) -e '.[dev]'; \
	else \
	  $(PYTHON) -m ensurepip --upgrade && $(PYTHON) -m pip install -e '.[dev]'; \
	fi

test: ## Tests rapides (sans modèle réel ni affichage)
	$(PYTHON) -m pytest -m "not slow and not gui" -q $(PYTEST_ARGS)

test-all: ## Tous les tests (les lents demandent des modèles téléchargés, voir CONTRIBUTING.md)
	$(PYTHON) -m pytest -q $(PYTEST_ARGS)

lint: lint-python lint-shell lint-js ## ruff + shellcheck + node --check sur l'extension (make -k lint : tout vérifier)

lint-python:
	@echo "→ ruff"
	@$(RUFF) check .

lint-shell:
	@echo "→ shellcheck"
	@$(call need,shellcheck); LC_ALL=C.UTF-8 shellcheck -x $(SHELL_SCRIPTS)

# GNOME 45+ extensions are ES modules: check each file as .mjs, whatever the Node version.
lint-js:
	@echo "→ node --check (extension)"
	@if [ -z "$(strip $(EXT_JS))" ]; then echo "  (aucun fichier JavaScript dans $(EXT_SRC))"; exit 0; fi; \
	$(call need,node); \
	tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	for f in $(EXT_JS); do \
	  cp "$$f" "$$tmp/check.mjs"; \
	  node --check "$$tmp/check.mjs" || { echo "✗ $$f" >&2; exit 1; }; \
	done; echo "  $(words $(EXT_JS)) fichier(s) OK"

validate: ## desktop-file-validate + appstreamcli validate --no-net
	@echo "→ desktop-file-validate"
	@$(call need,desktop-file-validate); desktop-file-validate data/*.desktop
	@echo "→ appstreamcli validate"
	@files="$(wildcard data/*.metainfo.xml)"; \
	if [ -z "$$files" ]; then echo "  (aucun fichier metainfo dans data/)"; exit 0; fi; \
	$(call need,appstreamcli); appstreamcli validate --no-net --explain $$files

run-daemon: ## Lancer le service au premier plan, journaux détaillés (remplace l'instance systemd)
	$(PYTHON) -m greffier.daemon.app $(DAEMON_ARGS)

run-gui: ## Lancer l'interface depuis le dépôt
	$(PYTHON) -c 'import sys; from greffier.ui.app import main; sys.exit(main())'

sounds: ## Régénérer greffier/data/sounds/*.wav
	$(PY_ANY) tools/make_sounds.py

# --------------------------------------------------------------------------- #
# Translations (plain .po files read at runtime, see docs/ARCHITECTURE.md §8)
# --------------------------------------------------------------------------- #
pot: ## Extraire les chaînes traduisibles dans greffier/data/locale/greffier.pot
	@mkdir -p $(dir $(POT))
	xgettext --from-code=UTF-8 --language=Python --add-comments=TRANSLATORS: \
	  --keyword=_ --keyword=N_ --keyword=ngettext:1,2 --keyword=pgettext:1c,2 \
	  --package-name=Greffier --package-version=$(VERSION) --msgid-bugs-address=$(ISSUES) \
	  --sort-by-file --output=$(POT) $(PY_SOURCES)
	@if [ -n "$(UI_SOURCES)" ]; then \
	  xgettext --from-code=UTF-8 --join-existing --add-comments=TRANSLATORS: \
	    --sort-by-file --output=$(POT) $(UI_SOURCES); \
	fi
	@echo "→ $(POT) : $$(grep -c '^msgid ' $(POT)) chaînes"

update-po: pot ## Fusionner le .pot dans chaque greffier/data/locale/*.po
	@for po in greffier/data/locale/*.po; do \
	  [ -e "$$po" ] || continue; \
	  msgmerge --quiet --update --backup=none --no-fuzzy-matching "$$po" $(POT); \
	  printf '%s : ' "$$po"; msgfmt --statistics -o /dev/null "$$po"; \
	done

# --------------------------------------------------------------------------- #
# Release artefacts
# --------------------------------------------------------------------------- #
extension-zip: ## Archive de l'extension pour extensions.gnome.org (dist/)
	@test -f $(EXT_SRC)/metadata.json || { echo "✗ $(EXT_SRC)/metadata.json introuvable" >&2; exit 1; }
	@rm -rf $(EXT_BUILD) && mkdir -p $(EXT_BUILD) $(DIST)
	@tar -C $(EXT_SRC) \
	  --exclude='.git*' --exclude='.eslint*' --exclude='.editorconfig' --exclude='node_modules' \
	  --exclude='package*.json' --exclude='*.zip' --exclude='*~' --exclude='*.swp' \
	  --exclude='*.orig' --exclude='*.rej' --exclude='*.pot' --exclude='gschemas.compiled' \
	  --exclude='__pycache__' -cf - . | tar -C $(EXT_BUILD) -xf -
	@if compgen -G "$(EXT_BUILD)/po/*.po" >/dev/null; then \
	  command -v msgfmt >/dev/null || { echo "✗ msgfmt (gettext) est nécessaire pour compiler po/" >&2; exit 1; }; \
	  domain=$$($(PY_ANY) -c 'import json,sys; print(json.load(open(sys.argv[1])).get("gettext-domain") or "$(UUID)")' $(EXT_BUILD)/metadata.json); \
	  for po in $(EXT_BUILD)/po/*.po; do \
	    lang=$$(basename "$$po" .po); mkdir -p "$(EXT_BUILD)/locale/$$lang/LC_MESSAGES"; \
	    msgfmt -o "$(EXT_BUILD)/locale/$$lang/LC_MESSAGES/$$domain.mo" "$$po"; \
	  done; \
	  rm -rf $(EXT_BUILD)/po; \
	fi
	@find $(EXT_BUILD) -type d -empty -delete
	@rm -f $(EXT_ZIP)
	@cd $(EXT_BUILD) && zip -qr -X $(CURDIR)/$(EXT_ZIP) .
	@echo "→ $(EXT_ZIP)"; unzip -l $(EXT_ZIP) | tail -n +4 | head -n -2 | awk '{print "   " $$4}'

dist: ## Paquets Python (wheel + sdist avec « build », sinon wheel avec pip) dans dist/
	@mkdir -p $(DIST)
	@if $(PY_ANY) -c 'import build' 2>/dev/null; then \
	  $(PY_ANY) -m build --outdir $(DIST) .; \
	else \
	  echo "  (module « build » absent : wheel seule, via pip)"; \
	  $(PY_ANY) -m pip wheel --no-deps --wheel-dir $(DIST) .; \
	fi
	@ls -1 $(DIST)

clean: ## Supprimer les fichiers générés (garde .venv)
	rm -rf build $(DIST) *.egg-info greffier.egg-info .pytest_cache .ruff_cache
	find . -path ./$(VENV) -prune -o -type d -name __pycache__ -exec rm -rf {} +
