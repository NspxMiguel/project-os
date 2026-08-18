"""O yt-dlp da caixa dele entra uma vez e nunca mais é tocado.

Quem instala é o ``01-run.sh`` da imagem, no dia em que a imagem é construída.
Quem atualiza seria o updater, que roda ``pip install --upgrade -r
requirements.txt`` -- e o yt-dlp não está no requirements.txt, de propósito: ele
é opcional e a caixa boota sem ele. O resultado é um órfão. O baixador fica
parado na versão daquele dia, para sempre.

Isso importaria pouco em quase qualquer dependência. Nesta importa muito: o
trabalho do yt-dlp é perseguir mudança do YouTube, e por isso ele lança versão
quase toda semana. Medido nesta máquina em 17/08/2026: o yt-dlp instalado é o
2025.10.14, **307 dias** atrás.

E o erro que aparece quando isso morde não fala de idade -- fala de extração de
player. Ninguém liga uma coisa na outra sozinho.

Os dois lados do conserto, amarrados aqui: a tela passa a dizer a idade (sem
rede: o número da versão do yt-dlp *é* a data), e a atualização do app passa a
renovar os extras que a imagem instalou.
"""

from __future__ import annotations

import datetime
import io
import os
import re

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ler(*partes):
    return io.open(os.path.join(RAIZ, *partes), encoding="utf-8").read()


# ------------------------------------------------------ a data dentro da versão


@pytest.mark.parametrize("versao,esperado", [
    ("2026.7.4", (2026, 7, 4)),
    ("2025.10.14", (2025, 10, 14)),
    ("2025.09.07", (2025, 9, 7)),
    # As de desenvolvimento levam sufixo; os três primeiros campos continuam a data.
    ("2025.9.7.232816.dev0", (2025, 9, 7)),
])
def test_o_numero_da_versao_e_a_data(versao, esperado):
    from project_os.apps.birdtunes import sources

    assert sources._dia_da_versao(versao) == datetime.date(*esperado)


@pytest.mark.parametrize("versao", ["", None, "1.2", "sei lá", "2026.13.99", "a.b.c"])
def test_o_que_nao_e_data_nao_vira_data(versao):
    from project_os.apps.birdtunes import sources

    assert sources._dia_da_versao(versao) is None


# ------------------------------------------------------------------- a idade


def _estado(monkeypatch, versao, hoje, tem=True):
    from project_os.apps.birdtunes import sources

    monkeypatch.setattr(sources, "ytdlp_version", lambda: versao)
    monkeypatch.setattr(sources, "available", lambda: tem)
    return sources.ytdlp_state(hoje=hoje)


def test_um_baixador_de_ontem_esta_em_paz(monkeypatch):
    estado = _estado(monkeypatch, "2026.8.16", datetime.date(2026, 8, 17))
    assert estado["age_days"] == 1
    assert estado["stale"] is False


def test_o_de_trezentos_dias_nao(monkeypatch):
    """O caso real desta máquina."""
    estado = _estado(monkeypatch, "2025.10.14", datetime.date(2026, 8, 17))
    assert estado["age_days"] == 307
    assert estado["stale"] is True
    assert estado["version"] == "2025.10.14"
    assert estado["released"] == "2025-10-14"


def test_a_fronteira_e_de_noventa_dias(monkeypatch):
    from project_os.apps.birdtunes import sources

    assert sources.VELHO_DEPOIS_DE_DIAS == 90
    hoje = datetime.date(2026, 8, 17)
    noventa = hoje - datetime.timedelta(days=90)
    assert _estado(monkeypatch, "%d.%d.%d" % (noventa.year, noventa.month, noventa.day), hoje)["stale"] is False
    um_a_mais = hoje - datetime.timedelta(days=91)
    assert _estado(monkeypatch, "%d.%d.%d" % (um_a_mais.year, um_a_mais.month, um_a_mais.day), hoje)["stale"] is True


def test_sem_saber_a_data_nao_inventa_alarme(monkeypatch):
    """Um "sei lá" honesto é melhor que um aviso que a pessoa não pode conferir."""
    estado = _estado(monkeypatch, "sei lá", datetime.date(2026, 8, 17))
    assert estado["age_days"] is None
    assert estado["stale"] is False


def test_sem_yt_dlp_nao_ha_baixador_velho(monkeypatch):
    """A tela sem yt-dlp já tem o aviso dela; dois avisos seriam ruído."""
    estado = _estado(monkeypatch, "", datetime.date(2026, 8, 17), tem=False)
    assert estado["available"] is False
    assert estado["stale"] is False


def test_ninguem_sai_para_a_rede_para_saber_disso():
    """Igual ao bit do MAC: o dado já está na mão e não desatualiza."""
    fonte = _ler("project_os", "apps", "birdtunes", "sources.py")
    corpo = fonte[fonte.index("def ytdlp_state("):]
    corpo = corpo[:corpo.index("#: Every shape of YouTube link")]
    for proibido in ("urlopen", "requests", "httpx", "socket"):
        assert proibido not in corpo, proibido


# --------------------------------------------------------------------- a tela


def test_o_compat_conta_a_idade():
    fonte = _ler("project_os", "apps", "birdtunes", "app.py")
    assert '"ytdlp": sources.ytdlp_state(),' in fonte


def test_o_painel_so_avisa_quando_e_velho_e_existe():
    painel = _ler("project_os", "apps", "birdtunes", "web", "panel.js")
    corpo = painel[painel.index("function baixadorVelho()"):]
    corpo = corpo[:corpo.index("function addView()")]
    assert "!est.stale" in corpo and "!est.available" in corpo
    assert "pip install -U yt-dlp" in corpo, "o aviso diz o que fazer, não só o que houve"


def test_o_aviso_esta_nas_duas_linguas():
    painel = _ler("project_os", "apps", "birdtunes", "web", "panel.js")
    pt = _ler("web", "lib", "strings-pt.js")
    for chave in ("bt.import.ytdlp.old", "bt.import.ytdlp.old.why"):
        assert "'%s':" % chave in painel, chave
        assert "'%s':" % chave in pt, chave


# ------------------------------------------------- e a atualização conserta


def test_a_atualizacao_renova_o_que_a_imagem_instalou():
    fonte = _ler("project_os", "api", "updates.py")
    assert "updates.refresh_extras(on_line=_job.say)" in fonte
    depois = fonte[fonte.index("install_requirements(on_line=_job.say)"):]
    assert depois.index("refresh_extras") > 0, "primeiro o código novo, depois os extras"


def test_nenhum_extra_da_imagem_fica_orfao():
    """Se a imagem passar a instalar mais uma coisa, ou ela entra no
    requirements.txt (e o updater a renova) ou entra em EXTRAS_DA_IMAGEM. Este
    teste existe porque o yt-dlp ficou fora dos dois por meses.
    """
    from project_os.core import updates

    script = _ler("image", "stage-project-os", "00-project-os", "01-run.sh")
    linha = re.search(r"pip install --no-cache-dir \\\n\s+(.+?) \|\|", script, re.S)
    assert linha, "não achei a linha de extras no script da imagem"
    da_imagem = set(n.strip().lower() for n in linha.group(1).split() if n.strip())

    no_requirements = set()
    for crua in _ler("requirements.txt").splitlines():
        crua = crua.split("#")[0].strip()
        if crua:
            no_requirements.add(re.split(r"[<>=\[]", crua)[0].strip().lower())

    cobertos = no_requirements | set(n.lower() for n in updates.EXTRAS_DA_IMAGEM)
    orfaos = sorted(da_imagem - cobertos)
    assert not orfaos, (
        "a imagem instala %s e nada nunca atualiza: ou entra no requirements.txt, "
        "ou entra em updates.EXTRAS_DA_IMAGEM" % ", ".join(orfaos))


def test_o_yt_dlp_e_um_deles():
    from project_os.core import updates

    assert "yt-dlp" in updates.EXTRAS_DA_IMAGEM


def test_renovar_nao_instala_o_que_nao_estava_la(monkeypatch):
    """Numa caixa sem yt-dlp, uma atualização de rotina não decide instalar."""
    from project_os.core import updates

    monkeypatch.setattr(updates, "_pacotes_instalados", lambda python: ["fastapi", "uvicorn"])
    chamou = []
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: chamou.append(a) or None)
    ditos = []
    assert updates.refresh_extras(root=RAIZ, on_line=ditos.append) == 0
    assert chamou == [], "não chamou pip"
    assert any("nothing to refresh" in d for d in ditos)


def test_mas_renova_o_que_estava(monkeypatch):
    from project_os.core import updates

    monkeypatch.setattr(updates, "_pacotes_instalados", lambda python: ["yt-dlp", "fastapi"])

    class _Falso(object):
        stdout = None

        def __init__(self, argv, **kwargs):
            self.argv = argv
            visto.append(argv)

        def wait(self):
            return 0

    visto = []
    monkeypatch.setattr(updates.subprocess, "Popen", _Falso)
    assert updates.refresh_extras(root=RAIZ) == 0
    assert visto and visto[0][-1] == "yt-dlp"
    assert "--upgrade" in visto[0]
    assert "casttube" not in visto[0], "só o que está instalado"
