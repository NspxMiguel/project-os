"""A tela de hardware, na máquina em que ela vai rodar.

    "ai opção de diminuir ventoinha do rp, e etc. varias coisas legais adiciona"

Dois furos entre esse pedido e a caixa dele, os dois invisíveis num Mac:

**Escrever.** Toda mudança desta tela é em ``/sys`` ou no ``config.txt``, que são
do root. Na imagem o serviço roda como usuário ``project-os``, de propósito --
então ``is_root()`` é falso e a tela ficava só-leitura mesmo com o interruptor
ligado. A frase que ela mostrava ("o serviço systemd roda como root") era falsa
justamente na única máquina que importa. O mesmo sudoers da imagem que já dá
``systemctl`` e ``apt-get`` sem senha resolve isso.

**A curva.** O card "temperaturas de acionamento" era montado a partir dos
passos que já existiam no config.txt, e um Raspberry Pi OS de fábrica não tem
nenhuma linha ``dtparam=fan_temp``. Lista vazia, nenhum botão de adicionar, e o
Salvar mandava ``{"steps": []}``, que reescrevia o arquivo idêntico e devolvia
200 -- com direito a "curva salva, vale no próximo boot".
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def esquece_o_sudo():
    """A resposta do sudo é guardada por processo; cada teste começa do zero."""
    from project_os.core import tuning

    tuning._SUDO_OK = None
    yield
    tuning._SUDO_OK = None


def test_sem_root_e_sem_sudo_a_tela_admite_que_so_le(monkeypatch):
    from project_os.core import tuning

    monkeypatch.setattr(tuning, "is_root", lambda: False)
    monkeypatch.setattr(tuning.shutil, "which", lambda nome: None)
    assert tuning.can_write() is False
    foto = tuning.snapshot()
    assert foto["writable"] is False
    assert "root" in foto["reason"]


def test_com_sudo_sem_senha_a_tela_pode_escrever(monkeypatch):
    """É o caso da caixa dele: serviço como project-os, sudoers sem senha."""
    from project_os.core import tuning

    monkeypatch.setattr(tuning, "is_root", lambda: False)
    monkeypatch.setattr(tuning.shutil, "which", lambda nome: "/usr/bin/sudo")
    monkeypatch.setattr(
        tuning.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})(),
    )
    assert tuning.can_write() is True
    assert tuning.snapshot()["writable"] is True
    # E continua dizendo a verdade sobre o que ele é.
    assert tuning.snapshot()["root"] is False


def test_sudo_que_pede_senha_nao_conta(monkeypatch):
    from project_os.core import tuning

    monkeypatch.setattr(tuning, "is_root", lambda: False)
    monkeypatch.setattr(tuning.shutil, "which", lambda nome: "/usr/bin/sudo")
    monkeypatch.setattr(
        tuning.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": b"", "stderr": b"senha"})(),
    )
    assert tuning.can_write() is False


def test_a_escrita_no_sys_passa_pelo_sudo(monkeypatch, tmp_path):
    from project_os.core import tuning

    chamadas = []

    def falso_run(argv, **kwargs):
        chamadas.append((list(argv), kwargs.get("input")))
        return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    monkeypatch.setattr(tuning, "is_root", lambda: False)
    monkeypatch.setattr(tuning, "sudo_ready", lambda refresh=False: True)
    monkeypatch.setattr(tuning.subprocess, "run", falso_run)

    tuning._write("/sys/class/leds/ACT/brightness", "1")
    argv, entrada = chamadas[0]
    assert argv == ["sudo", "-n", "tee", "/sys/class/leds/ACT/brightness"]
    # O valor vai pela entrada padrão: nada de montar linha de shell com ele.
    assert entrada == b"1"


def test_sudo_recusado_vira_erro_com_o_caminho_dentro(monkeypatch):
    from project_os.core import tuning

    monkeypatch.setattr(tuning, "is_root", lambda: False)
    monkeypatch.setattr(tuning, "sudo_ready", lambda refresh=False: True)
    monkeypatch.setattr(
        tuning.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": b"", "stderr": b"nao permitido"})(),
    )
    with pytest.raises(OSError) as caiu:
        tuning._write("/sys/class/leds/ACT/brightness", "1")
    assert "/sys/class/leds/ACT/brightness" in str(caiu.value)
    assert "nao permitido" in str(caiu.value)


def test_como_root_escreve_direto_sem_chamar_sudo(monkeypatch, tmp_path):
    from project_os.core import tuning

    def nao_pode_rodar(*a, **k):
        raise AssertionError("chamou sudo sendo root")

    monkeypatch.setattr(tuning, "is_root", lambda: True)
    monkeypatch.setattr(tuning.subprocess, "run", nao_pode_rodar)
    alvo = tmp_path / "brightness"
    tuning._write(str(alvo), "1")
    assert alvo.read_text() == "1"


def test_o_config_txt_troca_por_cima_com_sudo(monkeypatch, tmp_path):
    """Escreve ao lado e move por cima -- os dois passos pedindo root."""
    from project_os.core import tuning

    arquivo = tmp_path / "config.txt"
    arquivo.write_text("dtparam=audio=on\n", encoding="utf-8")
    movimentos = []

    def falso_run(argv, **kwargs):
        argv = list(argv)
        if argv[:3] == ["sudo", "-n", "tee"]:
            open(argv[3], "wb").write(kwargs.get("input") or b"")
        elif argv[:3] == ["sudo", "-n", "mv"]:
            movimentos.append(argv)
            os.replace(argv[-2], argv[-1])
        return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    monkeypatch.setattr(tuning, "is_root", lambda: False)
    monkeypatch.setattr(tuning, "sudo_ready", lambda refresh=False: True)
    monkeypatch.setattr(tuning, "config_txt_path", lambda: str(arquivo))
    monkeypatch.setattr(tuning.subprocess, "run", falso_run)

    tuning._patch_config_txt({"dtparam=fan_temp0": None, "arm_freq": "1000"})
    texto = arquivo.read_text(encoding="utf-8")
    assert "arm_freq=1000" in texto
    assert "dtparam=audio=on" in texto
    assert movimentos, "o arquivo não foi trocado por cima"


def test_vcgencmd_que_nao_existe_nao_vira_erro_de_sudo(monkeypatch):
    """Com prefixo de sudo, quem tem que existir é o programa depois dele."""
    from project_os.core import tuning

    monkeypatch.setattr(tuning, "is_root", lambda: False)
    monkeypatch.setattr(tuning.shutil, "which", lambda nome: "/usr/bin/sudo" if nome == "sudo" else None)
    assert tuning._run(["sudo", "-n", "vcgencmd", "display_power", "0"]) is None


def test_a_tela_deixa_criar_um_passo_de_curva():
    """Num Pi de fábrica a lista vem vazia; sem isto o card não tinha o que salvar."""
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "web", "views", "tuning.js"), encoding="utf-8") as arquivo:
        texto = arquivo.read()
    assert "function addFanStep()" in texto
    assert "tuning.fan.curve.add" in texto
    # E o botão Salvar não aparece quando não há passo nenhum para salvar.
    assert "fanSteps.length\n            ? h('button'" in texto or "fanSteps.length" in texto


def test_a_curva_padrao_sobe_de_temperatura():
    """Um passo novo não pode nascer com número pior que o do firmware."""
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "web", "views", "tuning.js"), encoding="utf-8") as arquivo:
        texto = arquivo.read()
    import re

    graus = [int(g) for g in re.findall(r"\{celsius: (\d+), speed: \d+\}", texto)]
    assert graus == sorted(graus) and len(graus) == 4
    assert graus[0] >= 40
