"""Toda chave que uma tela manda ligar precisa ter interruptor em alguma tela.

Três telas do modo avançado diziam, em texto:

* Ajustes de hardware — "Ligue security.allow_hardware_control em Configurações
  › Desenvolvedor";
* Serviços — "Ligue security.allow_service_control em Configurações";
* Arquivos — "A escrita está desligada nas Configurações
  (security.allow_file_write)".

E a aba Desenvolvedor gravava exatamente três coisas: ``security.allow_shell``,
``ui.dock_terminal`` e ``logging.level``. Nenhuma das três chaves citadas. O
efeito não era cosmético: a tela de Hardware inteira ficava só-leitura para
sempre (ventoinha, governor, LEDs, HDMI, Wi-Fi, GPU), e a de Serviços nunca
mostrava Iniciar/Parar/Reiniciar. O texto apontava para um lugar que não existia
-- e o jeito de sair disso era editar o config.yaml na mão e reiniciar o
serviço, que é justamente o que este projeto promete que ninguém precisa fazer.

O mesmo vale para o botão de reiniciar a placa: ``POST /api/system/power``
existia desde o primeiro build avançado, duas telas mandavam reiniciar, e nada
no ``web/`` chamava.

Este teste não confere a aparência da tela. Ele confere a promessa: se um texto
cita uma chave, alguma tela grava essa chave.
"""

from __future__ import annotations

import os
import re

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(RAIZ, "web")

#: ``security.allow_shell`` aparece com aspas dentro do próprio saveValues; a
#: busca aceita as duas formas de citar em JS.
ESCRITA = re.compile(r"""['"]([a-z_]+(?:\.[a-z_]+)+)['"]\s*:""")


def _fontes():
    for pasta, _, arquivos in os.walk(WEB):
        for nome in sorted(arquivos):
            if nome.endswith(".js"):
                caminho = os.path.join(pasta, nome)
                with open(caminho, "r", encoding="utf-8") as arquivo:
                    yield os.path.relpath(caminho, RAIZ), arquivo.read()


def _chaves_gravadas():
    """Toda chave pontuada que aparece dentro de um ``saveValues({...})``."""
    gravadas = set()
    for _, texto in _fontes():
        for trecho in re.findall(r"saveValues\(\{(.*?)\}", texto, re.S):
            gravadas.update(ESCRITA.findall(trecho))
        for trecho in re.findall(r"api\.put\(\s*'/settings'\s*,\s*\{values:\s*\{(.*?)\}", texto, re.S):
            gravadas.update(ESCRITA.findall(trecho))
    return gravadas


def test_as_chaves_citadas_nas_telas_tem_interruptor():
    citadas = set()
    for caminho, texto in _fontes():
        for chave in re.findall(r"security\.allow_[a-z_]+", texto):
            citadas.add(chave)
    # As quatro que os textos citam hoje. A lista não é fixa de propósito: uma
    # tela nova que cite uma chave nova entra sozinha nesta conta.
    assert "security.allow_hardware_control" in citadas
    assert "security.allow_service_control" in citadas

    gravadas = _chaves_gravadas()
    faltando = sorted(c for c in citadas if c not in gravadas)
    assert not faltando, (
        "estas telas mandam ligar chaves que nenhuma tela grava: %s" % ", ".join(faltando)
    )


def test_o_avanco_das_tres_chaves_esta_na_aba_desenvolvedor():
    caminho = os.path.join(WEB, "views", "settings.js")
    with open(caminho, "r", encoding="utf-8") as arquivo:
        texto = arquivo.read()
    inicio = texto.index("function developerSection()")
    fim = texto.index("function render()", inicio)
    aba = texto[inicio:fim]
    for chave in (
        "security.allow_hardware_control",
        "security.allow_service_control",
        "security.allow_file_write",
    ):
        assert chave in aba, "%s não está na aba Desenvolvedor" % chave


def test_existe_um_botao_que_reinicia_a_placa():
    chamadas = [caminho for caminho, texto in _fontes() if "/system/power" in texto]
    assert chamadas, "nenhuma tela chama POST /api/system/power"


def test_os_avisos_trancados_levam_para_o_interruptor():
    """Ler "ligue em Configurações" e ter que procurar a aba é meio conserto."""
    for arquivo in ("tuning.js", "services.js", "files.js"):
        caminho = os.path.join(WEB, "views", arquivo)
        with open(caminho, "r", encoding="utf-8") as fonte:
            texto = fonte.read()
        assert "#/settings/developer" in texto, "%s não leva para o interruptor" % arquivo
