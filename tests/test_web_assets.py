"""The browser code has to at least parse.

There were no tests here, and it cost the whole interface: a rename swept
through the tree with sed and turned ``window.projectos = {...}`` into
``window.project-os = {...}``. That is a syntax error, so the module never ran
and every screen stayed on the spinner. Nothing in the Python suite noticed,
because none of it ever looks at a .js file.

This does not test behaviour -- it only asks node to parse each module. That is
the cheap half, and it is exactly the half that was missing.
"""

from __future__ import annotations

import base64
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _modules():
    found = []
    for folder in ("web", "project_os"):
        found.extend(sorted((ROOT / folder).rglob("*.js")))
    return found


def test_there_is_browser_code_to_check() -> None:
    assert len(_modules()) > 10


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(ROOT)))
def test_module_parses(path: pathlib.Path) -> None:
    """Parse only: imports are never resolved, so no file needs a server."""
    source = base64.b64encode(path.read_bytes()).decode("ascii")
    script = (
        "import('data:text/javascript;base64," + source + "')"
        ".then(() => {}, err => {"
        "  if (err instanceof SyntaxError) {"
        "    console.error(err.message); process.exit(1);"
        "  }"  # anything else is an unresolved import, which is not our business
        "})"
    )
    result = subprocess.run(
        [NODE, "-e", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    )
    assert result.returncode == 0, "%s: %s" % (
        path.relative_to(ROOT), result.stderr.decode("utf-8", "replace").strip()
    )


ESPECIFICADOR = re.compile(
    r"""(?:import\s+[^'"]*?from\s*|import\s*\(\s*)['"]([^'"]+)['"]""",
    re.S,
)


def _alvo(origem: pathlib.Path, spec: str):
    """O arquivo que um import aponta, ou None quando não é um caminho nosso."""
    if spec.startswith("./") or spec.startswith("../"):
        return (origem.parent / spec).resolve()
    if spec.startswith("/"):
        # O shell serve web/ na raiz, e é assim que os painéis de app chegam em
        # /lib/format.js: o caminho é absoluto na URL, não no disco.
        return (ROOT / "web" / spec.lstrip("/")).resolve()
    return None  # http(s), data:, bare specifier -- não é arquivo deste repo


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(ROOT)))
def test_todo_import_aponta_para_um_arquivo_que_existe(path: pathlib.Path) -> None:
    """Parsear não basta: um caminho errado só aparece na tela em branco.

    O ``test_module_parses`` acima ignora imports de propósito (ele roda o
    módulo sozinho, sem servidor). Só que o erro clássico aqui não é sintaxe, é
    caminho -- inclusive nos ``import()`` dinâmicos, que nem o navegador
    reclama até alguém abrir aquela tela.
    """
    fonte = path.read_text(encoding="utf-8")
    faltando = []
    for spec in ESPECIFICADOR.findall(fonte):
        if "${" in spec or spec.startswith("data:"):
            continue  # montado em tempo de execução
        alvo = _alvo(path, spec)
        if alvo is not None and not alvo.is_file():
            faltando.append(spec)
    assert not faltando, "%s importa o que não existe: %s" % (
        path.relative_to(ROOT), ", ".join(faltando),
    )


def test_the_shell_exposes_a_usable_global() -> None:
    """App panels reach the shell through one global, and it must be nameable.

    ``project-os`` is not a legal identifier -- that is how the break happened.
    """
    text = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "window.projectOs =" in text
    assert "window.project-os" not in text
