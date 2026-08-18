"""Verde nesta máquina não estava valendo nada no repositório dele.

Medido em 17/08/2026: a suíte passava aqui (1135 testes) e o CI estava vermelho
havia **cinco commits**. A causa era uma linha só -- um teste importava
``yt_dlp`` direto, sem guarda, e o yt-dlp está instalado nesta máquina e não no
runner. O resultado é o pior formato de erro que existe: quem escreve o código
vê verde, quem olha o repositório vê vermelho, e ninguém dos dois está errado.

Pior: o workflow de release não rodava teste nenhum. Cinco versões foram
publicadas de uma árvore vermelha, e publicar aqui não é arquivar um `.tar.gz`
-- é entregar pelo updater, sozinho, na caixa que fica ligada na casa dele.

Os três invariantes deste arquivo:

1. tudo que a suíte importa e não é biblioteca padrão, o CI instala. Sem isso,
   "passou" pode significar "nem rodou";
2. a tag não publica sem a suíte passar;
3. o portão roda a suíte inteira, e não um pedaço escolhido a dedo.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import os
import re
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTES = os.path.join(RAIZ, "tests")
FLUXOS = os.path.join(RAIZ, ".github", "workflows")

# O nome que se instala e o nome que se importa quase nunca são o mesmo.
COMO_SE_IMPORTA = {
    "pyyaml": "yaml",
    "yt-dlp": "yt_dlp",
    "pychromecast": "pychromecast",
    "python-multipart": "multipart",
    "pytest-asyncio": "pytest_asyncio",
    "python-dotenv": "dotenv",
}

# Quem chega de carona em outro pacote, e por isso está no runner sem estar
# declarado. É pouco e é sabido: o fastapi não existe sem o starlette -- é dele
# que vêm o TestClient e as respostas que a suíte inspeciona.
VEM_JUNTO = {"starlette": "fastapi"}


def _ler(caminho):
    return io.open(caminho, encoding="utf-8").read()


def _e_da_biblioteca_padrao(raiz):
    """Sem ``sys.stdlib_module_names``, que só existe do 3.10 para cima.

    Quem não importa de jeito nenhum conta como de fora -- que é exatamente o
    caso que este arquivo persegue: o yt_dlp ausente no runner.
    """
    if raiz in sys.builtin_module_names:
        return True
    try:
        spec = importlib.util.find_spec(raiz)
    except (ImportError, ValueError):
        return False
    if spec is None or not spec.origin:
        return spec is not None and spec.origin is None
    return os.path.dirname(os.__file__) in os.path.dirname(spec.origin)


def _o_que_a_suite_importa():
    de_fora = {}
    for nome in sorted(os.listdir(TESTES)):
        if not nome.endswith(".py"):
            continue
        arvore = ast.parse(_ler(os.path.join(TESTES, nome)))
        for no in ast.walk(arvore):
            if isinstance(no, ast.Import):
                raizes = [a.name.split(".")[0] for a in no.names]
            elif isinstance(no, ast.ImportFrom) and no.module and not no.level:
                raizes = [no.module.split(".")[0]]
            else:
                continue
            for raiz in raizes:
                if raiz in ("project_os", "tests") or _e_da_biblioteca_padrao(raiz):
                    continue
                de_fora.setdefault(raiz, nome)
    return de_fora


def _o_que_o_ci_instala(fluxo):
    """As distribuições do comando ``pip install`` do workflow, já traduzidas."""
    texto = _ler(os.path.join(FLUXOS, fluxo))
    linha = [l for l in texto.splitlines() if "pip install" in l]
    assert linha, "%s não instala nada" % fluxo
    comando = linha[0]

    pacotes = set()
    if "requirements.txt" in comando:
        for crua in _ler(os.path.join(RAIZ, "requirements.txt")).splitlines():
            crua = crua.split("#")[0].strip()
            if crua:
                pacotes.add(re.split(r"[<>=\[]", crua)[0].strip().lower())
    for extra in re.findall(r"\.\[([^\]]+)\]", comando):
        for nome in extra.split(","):
            bloco = re.search(
                r"^%s\s*=\s*\[(.*?)\]" % re.escape(nome.strip()),
                _ler(os.path.join(RAIZ, "pyproject.toml")),
                re.S | re.M,
            )
            if bloco:
                for item in re.findall(r'"([^"]+)"', bloco.group(1)):
                    pacotes.add(re.split(r"[<>=\[]", item)[0].strip().lower())
    tem = set(COMO_SE_IMPORTA.get(p, p.replace("-", "_")) for p in pacotes)
    for carona, dono in VEM_JUNTO.items():
        if dono in tem:
            tem.add(carona)
    return tem


def test_o_ci_instala_tudo_que_a_suite_importa():
    precisa = _o_que_a_suite_importa()
    tem = _o_que_o_ci_instala("tests.yml")
    faltando = sorted(
        "%s (usado em %s)" % (raiz, arquivo)
        for raiz, arquivo in precisa.items()
        if raiz not in tem
    )
    assert not faltando, (
        "o CI não instala: %s.\nOu declara no pyproject e instala no workflow, "
        "ou o teste vira verde aqui e vermelho lá." % ", ".join(faltando)
    )


def test_o_yt_dlp_de_verdade_entra_no_runner():
    """Guardar com importorskip e não instalar é ficar verde por não rodar.

    São dois testes contra a biblioteca real: os nomes das categorias do
    SponsorBlock e a exceção de cancelar. Os dois valem justamente por falarem
    com o yt-dlp que existe, e não com o nosso dublê.
    """
    assert "yt_dlp" in _o_que_o_ci_instala("tests.yml")


def test_a_tag_nao_publica_sem_a_suite_passar():
    fluxo = _ler(os.path.join(FLUXOS, "release.yml"))
    assert re.search(r"^\s*pytest:\s*$", fluxo, re.M), "o release não tem trabalho de teste"
    publicar = fluxo[fluxo.index("\n  publish:"):]
    cabeca = publicar[:publicar.index("steps:")]
    assert "needs: pytest" in cabeca, "publish precisa esperar o pytest"


def test_e_o_portao_roda_a_suite_inteira():
    """Um portão que roda um arquivo escolhido a dedo não é portão."""
    fluxo = _ler(os.path.join(FLUXOS, "release.yml"))
    portao = fluxo[fluxo.index("  pytest:"):fluxo.index("\n  publish:")]
    assert "python -m pytest -q" in portao
    assert "tests/" not in portao, "sem escolher arquivo: a suíte inteira"
