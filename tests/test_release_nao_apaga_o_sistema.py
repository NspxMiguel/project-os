"""Publicar a aplicação não pode apagar o anúncio do sistema.

O ``release/latest.json`` tem dois anúncios e dois donos. A chave de cima
(``version``/``url``/``sha256``) é o tarball da aplicação, escrita pelo
``release.yml`` a cada tag. A chave ``system`` é o rootfs A/B, de 880 MB,
escrita pelo ``image.yml`` -- que roda uma vez por imagem e leva mais de uma
hora.

O ``release.yml`` escrevia o manifesto inteiro com um ``cat >``, então cada
release de aplicação apagava o ``system``. O efeito não aparece no release nem
nos logs: o arquivo do rootfs continua publicado, só que ninguém mais fica
sabendo dele. O Pi lê o manifesto, não a lista de arquivos do release, e o botão
"Atualizar sistema" fica sem alvo até a próxima imagem.

O teste que existia para isso (``test_o_manifesto_de_sistema_aponta_para_um
_arquivo_que_existe``) pula quando o manifesto não anuncia sistema -- ou seja,
ficaria verde justamente no dia em que a chave desaparecesse.

Aqui o bloco de merge é extraído do YAML e executado de verdade, contra um
manifesto que já anuncia um sistema.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import textwrap

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE_YML = os.path.join(RAIZ, ".github", "workflows", "release.yml")
MANIFESTO = os.path.join(RAIZ, "release", "latest.json")

FALSOS = {
    "steps.v.outputs.version": "9.9.9",
    "steps.v.outputs.tag": "v9.9.9",
    "steps.pack.outputs.name": "project-os-9.9.9.tar.gz",
    "steps.pack.outputs.sha": "a" * 64,
    "github.repository": "NspxMiguel/project-os",
}


def _fonte():
    return io.open(RELEASE_YML, encoding="utf-8").read()


def _script_do_manifesto():
    """O ``python3 - <<'PY' ... PY`` do passo que escreve o manifesto.

    Extrair em vez de reescrever: um teste que copia a lógica prova que a cópia
    funciona, e é a do workflow que roda na publicação.
    """
    fonte = _fonte()
    achado = re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY\n", fonte, re.S)
    assert achado, "o passo do manifesto não usa mais um bloco python3 -- teste desatualizado"
    corpo = textwrap.dedent(achado.group(1))
    # O GitHub troca as expressões antes do shell ver o script; aqui o mesmo,
    # com valores de mentira.
    def trocar(m):
        chave = m.group(1).strip()
        assert chave in FALSOS, "expressão nova no script do manifesto: %s" % chave
        return FALSOS[chave]

    return re.sub(r"\$\{\{(.*?)\}\}", trocar, corpo)


def _rodar(script, cwd):
    return subprocess.run(
        [sys.executable, "-c", script], cwd=str(cwd), capture_output=True, text=True
    )


# --------------------------------------------------------------------------- o merge


def test_o_sistema_anunciado_sobrevive_a_um_release_de_aplicacao(tmp_path):
    antes = {
        "version": "0.4.8",
        "url": "https://exemplo/project-os-0.4.8.tar.gz",
        "sha256": "b" * 64,
        "notes": "https://exemplo/v0.4.8",
        "system": {
            "version": "0.4.8",
            "url": "https://exemplo/rootfs-0.4.8.tar.gz",
            "sha256": "c" * 64,
            "size": 881187275,
            "notes": "https://exemplo/v0.4.8",
        },
    }
    (tmp_path / "release").mkdir()
    (tmp_path / "release" / "latest.json").write_text(json.dumps(antes), encoding="utf-8")

    resultado = _rodar(_script_do_manifesto(), tmp_path)
    assert resultado.returncode == 0, resultado.stderr

    depois = json.loads((tmp_path / "release" / "latest.json").read_text(encoding="utf-8"))
    assert depois["system"] == antes["system"], "o release da aplicação apagou o sistema"
    assert depois["version"] == "9.9.9"
    assert depois["url"].endswith("v9.9.9/project-os-9.9.9.tar.gz")
    assert depois["sha256"] == "a" * 64


def test_sem_manifesto_nenhum_ele_e_criado(tmp_path):
    """O primeiro release de um repositório novo não pode quebrar por falta de arquivo."""
    (tmp_path / "release").mkdir()
    resultado = _rodar(_script_do_manifesto(), tmp_path)
    assert resultado.returncode == 0, resultado.stderr

    depois = json.loads((tmp_path / "release" / "latest.json").read_text(encoding="utf-8"))
    assert depois["version"] == "9.9.9"
    assert "system" not in depois, "não há sistema para anunciar, e inventar um seria pior"


def test_o_arquivo_escrito_e_json_legivel(tmp_path):
    """O manifesto vai para o main e alguém abre no navegador."""
    (tmp_path / "release").mkdir()
    _rodar(_script_do_manifesto(), tmp_path)
    texto = (tmp_path / "release" / "latest.json").read_text(encoding="utf-8")
    assert texto.endswith("\n"), "arquivo de texto termina com quebra de linha"
    assert "\n  " in texto, "json numa linha só é ilegível para quem abre no GitHub"


def test_o_passo_nao_volta_a_sobrescrever_o_manifesto():
    fonte = _fonte()
    assert "cat > release/latest.json" not in fonte, (
        "sobrescrever o manifesto inteiro apaga a chave system"
    )


# --------------------------------------------------------------------------- o que está publicado


def test_o_manifesto_de_hoje_anuncia_os_dois():
    """Sem rede: o arquivo que está no repositório é o que o Pi vai ler.

    O teste de rede que confere o rootfs pula quando não há ``system`` -- este
    aqui falha, que é o comportamento certo para uma chave que sumiu.
    """
    dados = json.loads(io.open(MANIFESTO, encoding="utf-8").read())
    assert len(dados.get("sha256") or "") == 64
    sistema = dados.get("system") or {}
    assert sistema.get("url"), "o manifesto parou de anunciar o sistema"
    assert len(sistema.get("sha256") or "") == 64
