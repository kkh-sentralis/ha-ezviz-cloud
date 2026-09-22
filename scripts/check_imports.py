#!/usr/bin/env python3
"""Verifie qu'aucun module standard n'est utilise sans etre importe.

Deux bugs de cette nature ont atteint la production -- `io` puis `json`, tous
deux introduits en manipulant les imports par remplacement de texte. Python ne
les signale qu'a l'execution, et seulement sur le chemin de code concerne : ici
une vignette, la une ouverture de flux.

    python3 scripts/check_imports.py
"""

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent.parent / "custom_components" / "ezviz"


def main() -> int:
    stdlib = set(sys.stdlib_module_names)
    failures = 0
    for path in sorted(ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {(a.asname or a.name).split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported |= {a.asname or a.name for a in node.names}

        # Noms locaux : une variable nommee `json` masquerait le module.
        assigned = {
            target.id
            for node in ast.walk(tree)
            for target in ast.walk(node)
            if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store)
        }

        # Modules standard employes en prefixe : json.loads, io.BytesIO...
        used = {
            node.value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        }

        if missing := sorted((used & stdlib) - imported - assigned):
            failures += 1
            print(f"{path.name}: utilise sans import -> {', '.join(missing)}")

    if failures:
        print(f"\n{failures} fichier(s) en defaut")
        return 1
    print("Tous les modules standard utilises sont importes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
