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
    """Do 3.10 para cima o próprio Python tem a lista; antes disso, na unha.

    A primeira versão deste guarda caiu no CI pelo defeito que ele existe para
    pegar: passava aqui no 3.9 e falhava lá no 3.11, onde ``os`` e ``io`` são
    módulos *congelados* e o ``spec.origin`` deles é a palavra "frozen" em vez
    de um caminho -- então a conta de "mora na pasta da biblioteca padrão?" dava
    não, e a suíte acusava o CI de não instalar o ``os``.

    Quem não importa de jeito nenhum conta como de fora, que é exatamente o caso
    perseguido aqui: o yt_dlp ausente no runner.
    """
    nomes = getattr(sys, "stdlib_module_names", None)
    if nomes is not None:
        return raiz in nomes
    if raiz in sys.builtin_module_names:
        return True
    try:
        spec = importlib.util.find_spec(raiz)
    except (ImportError, ValueError):
        return False
    if spec is None:
        return False
    origem = spec.origin or ""
    if origem in ("frozen", "built-in"):
        return True
    if not origem:
        return False
    # Pasta, e não arquivo: ``asyncio`` e ``json`` são pacotes, e o origin deles
    # aponta para um ``__init__.py`` uma pasta abaixo da biblioteca padrão.
    raiz_padrao = os.path.dirname(os.__file__)
    if not origem.startswith(raiz_padrao + os.sep):
        return False
    dentro = origem[len(raiz_padrao) + 1:]
    # Em algumas máquinas o site-packages mora dentro da própria pasta da
    # biblioteca padrão, e aí "começa com" não bastaria.
    return not dentro.startswith(("site-packages", "dist-packages"))


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


# ------------------------------------- o manifesto nunca anda para trás


def test_nenhum_workflow_escreve_uma_versao_mais_antiga_no_manifesto():
    """Duas publicações próximas terminam em ordem qualquer.

    O manifesto é o que a caixa dele consulta para saber se há novidade. Se a
    última a escrever for a mais antiga, a tela passa a oferecer um downgrade
    chamando-o de atualização -- e o updater instala, porque a versão é
    diferente da que está rodando.

    O image.yml já se protegia na chave "system"; o release.yml não se protegia
    na chave da aplicação, e numa noite de quatro versões isso deixa de ser
    teórico.
    """
    for fluxo, chave in (("release.yml", 'dados.get("version", "")'),
                         ("image.yml", '(dados.get("system") or {}).get("version", "")')):
        texto = _ler(os.path.join(FLUXOS, fluxo))
        assert "def como_tupla(" in texto, fluxo
        assert chave in texto, fluxo
        assert "não sobrescrevo" in texto, fluxo


def test_so_a_imagem_mais_nova_e_construida():
    """Uma imagem leva perto de uma hora; quatro tags numa noite punham quatro
    no ar ao mesmo tempo, e as três primeiras já estavam obsoletas ao nascer."""
    texto = _ler(os.path.join(FLUXOS, "image.yml"))
    bloco = texto[texto.index("concurrency:"):]
    bloco = bloco[:bloco.index("jobs:")]
    assert "group: image" in bloco
    assert "cancel-in-progress: true" in bloco
