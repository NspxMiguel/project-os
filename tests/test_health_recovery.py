"""O health conta se o esquema de dois sistemas está de pé.

Escrito com o Pi dele ligado e esperando ele criar a conta, do outro lado da
cidade. Eu conseguia ver versão, saúde e "ainda não tem dono" -- e não tinha
como saber a única coisa que importava: se o primeiro boot reparticionou o
cartão, se a partição de dados está montada, se o slot B existe. Tudo isso mora
atrás de login, e criar a conta é dele.

Uma caixa que acabou de ser gravada é exatamente a caixa que ninguém pode
consultar, e é exatamente a hora em que a pergunta vale mais. Então o health --
que já dizia versão e "setup_required" para quem chegasse -- passa a dizer
também: tem dois slots? qual está rodando? qual é o bom? os dados estão em
partição própria?

Nada disso é segredo: quem está na rede já vê a versão. E o valor prático é
grande — é a diferença entre "confere pelo navegador" e "tira o cartão do Pi".
"""

from __future__ import annotations


def test_o_health_publico_conta_o_estado_dos_slots(client):
    corpo = client.get("/api/system/health").json()
    assert "recovery" in corpo, "o health não conta nada sobre os dois sistemas"
    recovery = corpo["recovery"]
    for chave in ("slots", "slot", "good", "tries", "data_partition"):
        assert chave in recovery, "faltou %s" % chave


def test_numa_maquina_comum_ele_diz_que_nao_tem_slots(client):
    """No Mac ou num contêiner não há cartão -- e isso é resposta, não erro."""
    recovery = client.get("/api/system/health").json()["recovery"]
    assert recovery["slots"] is False
    assert recovery["slot"] == ""


def test_com_cartao_de_dois_sistemas_ele_diz_qual_esta_rodando(client, monkeypatch, tmp_path):
    from project_os.api import system as api_system

    conf = tmp_path / "project-os-slot.conf"
    conf.write_text("slot=B\ngood=A\ntries=1\nrecovery=0\n", encoding="utf-8")

    monkeypatch.setattr(api_system.slots, "current_slot", lambda: "B")
    monkeypatch.setattr(api_system.slots, "state_path", lambda: str(conf))
    monkeypatch.setattr(api_system.slots, "read_state", lambda path="": {
        "slot": "B", "good": "A", "tries": 1, "recovery": 0,
    })

    recovery = client.get("/api/system/health").json()["recovery"]
    assert recovery["slots"] is True
    assert recovery["slot"] == "B"
    assert recovery["good"] == "A", "o caminho de volta é o que mais interessa saber"
    assert recovery["tries"] == 1


def test_um_erro_aqui_nao_derruba_o_health(client, monkeypatch):
    """Health é a última coisa que pode quebrar: é ele que diz que a caixa vive."""
    from project_os.api import system as api_system

    def explode():
        raise RuntimeError("o cartão sumiu")

    monkeypatch.setattr(api_system.slots, "current_slot", explode)

    resposta = client.get("/api/system/health")
    assert resposta.status_code == 200
    assert resposta.json()["status"] == "ok"
    assert resposta.json()["recovery"]["slots"] is False


def test_diz_se_os_dados_estao_em_particao_propria(client, monkeypatch):
    from project_os.api import system as api_system

    monkeypatch.setattr(api_system.os.path, "ismount", lambda caminho: True)
    recovery = client.get("/api/system/health").json()["recovery"]
    assert recovery["data_partition"] is True
