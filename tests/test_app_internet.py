"""O app que sabe dizer se a internet caiu -- e onde.

"Caiu a internet" quase nunca é uma coisa só. São três, e o conserto de cada uma
é diferente: o roteador (andar até o aparelho), o provedor (ligar, e é bom ter a
hora na mão) e o DNS (quase sempre dá para resolver sozinho). Para quem está na
frente da tela os três são idênticos, e é por isso que o app separa.

O que este arquivo amarra: as três perguntas são feitas de verdade e separadas,
a causa contada é a mais funda (com o wifi caído, dizer "o DNS falhou" seria
verdade e seria inútil), a queda é uma linha de banco que sobrevive a reinício,
e nada disto precisa de rede para ser medido.
"""

from __future__ import annotations

import io
import json
import os

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(RAIZ, "project_os", "apps", "internet")


def _ler(*partes):
    return io.open(os.path.join(*partes), encoding="utf-8").read()


# ------------------------------------------------------------- as três perguntas


def test_a_internet_e_medida_por_endereco_e_nunca_por_nome():
    """Um nome aqui misturaria "a internet responde?" com "o DNS responde?",
    que é exatamente a separação que o app existe para fazer."""
    from project_os.apps.internet import probes

    for alvo in probes.ALVOS_INTERNET:
        host = alvo.rsplit(":", 1)[0]
        assert all(parte.isdigit() for parte in host.split(".")), alvo


def test_uma_rodada_faz_as_tres_perguntas():
    from project_os.apps.internet import probes

    perguntas = []

    def _tcp(host, porta, prazo):
        perguntas.append((host, porta))
        return 12.0

    def _resolver(nome, porta):
        perguntas.append(("dns", nome))
        return [("fam", "tipo", 0, "", ("1.2.3.4", 0))]

    medida = probes.medir(endereco_do_roteador="10.0.0.1", tcp=_tcp, resolver=_resolver)
    assert ("10.0.0.1", 53) in perguntas, "perguntou ao roteador"
    assert any(h == "1.1.1.1" for h, _ in perguntas), "perguntou à internet"
    assert ("dns", probes.NOME_PARA_RESOLVER) in perguntas, "perguntou ao DNS"
    assert medida["state"] == probes.NO_AR


def test_o_roteador_que_recusa_a_porta_ainda_esta_vivo():
    """Aparelho desligado dá tempo esgotado; aparelho ligado que não gosta da
    porta 53 responde na 80. Tratar recusa como queda acusaria o roteador dele
    de morto toda medida."""
    from project_os.apps.internet import probes

    def _tcp(host, porta, prazo):
        if host != "10.0.0.1":
            return 9.0          # a internet responde normalmente
        return None if porta == 53 else 4.0

    medida = probes.medir(endereco_do_roteador="10.0.0.1", tcp=_tcp,
                          resolver=lambda n, p: [()])
    assert medida["gateway_ms"] == 4.0
    assert medida["state"] == probes.NO_AR


def test_a_internet_tenta_o_proximo_quando_o_primeiro_nao_responde():
    from project_os.apps.internet import probes

    vistos = []

    def _tcp(host, porta, prazo):
        vistos.append(host)
        return 8.0 if host == "9.9.9.9" else None

    medida = probes.medir(endereco_do_roteador="", tcp=_tcp, resolver=lambda n, p: [()])
    assert medida["internet_target"] == "9.9.9.9:53"
    assert "1.1.1.1" in vistos, "tentou o primeiro antes"


def test_a_medida_nao_estoura_quando_nada_responde():
    from project_os.apps.internet import probes

    def _explodir(*a, **k):
        raise OSError("rede fora")

    medida = probes.medir(endereco_do_roteador="10.0.0.1",
                          tcp=lambda h, p, t: None, resolver=_explodir)
    assert medida["state"] == probes.SEM_ROTEADOR
    assert medida["dns_ms"] is None


# ------------------------------------------------------------------ a causa


@pytest.mark.parametrize("medida,esperado", [
    ({"gateway": "10.0.0.1", "gateway_ms": 3, "internet_ms": 9, "dns_ms": 11}, "ok"),
    ({"gateway": "10.0.0.1", "gateway_ms": 3, "internet_ms": 9, "dns_ms": None}, "dns"),
    ({"gateway": "10.0.0.1", "gateway_ms": 3, "internet_ms": None, "dns_ms": None}, "internet"),
    ({"gateway": "10.0.0.1", "gateway_ms": None, "internet_ms": None, "dns_ms": None}, "roteador"),
    # Sem roteador conhecido, o julgamento sobra para a internet.
    ({"gateway": "", "gateway_ms": None, "internet_ms": 9, "dns_ms": 11}, "ok"),
    ({"gateway": "", "gateway_ms": None, "internet_ms": None, "dns_ms": None}, "internet"),
])
def test_a_causa_contada_e_a_mais_funda(medida, esperado):
    from project_os.apps.internet import probes

    assert probes.diagnosticar(medida) == esperado


def test_com_o_wifi_caido_nao_se_acusa_o_provedor():
    """As três falham juntas; contar as três mandaria ele ligar para o provedor
    por causa de um cabo solto."""
    from project_os.apps.internet import probes

    tudo_fora = {"gateway": "10.0.0.1", "gateway_ms": None, "internet_ms": None, "dns_ms": None}
    assert probes.diagnosticar(tudo_fora) == probes.SEM_ROTEADOR
    assert probes.pior(probes.SEM_ROTEADOR, probes.SEM_DNS) == probes.SEM_ROTEADOR


# ---------------------------------------------------------------- o roteador


def test_o_gateway_sai_da_rota_padrao_do_linux():
    """É o que o Pi tem. A terceira coluna é o roteador, em hexadecimal
    little-endian, e a rota padrão é a de destino 00000000."""
    from project_os.apps.internet import probes

    proc = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth0\t00000000\t0100000A\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t0000000A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    )
    assert probes._gateway_do_proc(proc) == "10.0.0.1"


def test_e_do_netstat_onde_proc_nao_existe():
    """O Mac de desenvolvimento não tem /proc, e um app que só funciona no alvo
    não dá para desenvolver."""
    from project_os.apps.internet import probes

    saida = ("Routing tables\n\nInternet:\n"
             "Destination        Gateway            Flags\n"
             "default            10.0.0.1           UGScg\n")
    assert probes._gateway_do_netstat(saida) == "10.0.0.1"


def test_sem_rota_padrao_ninguem_inventa_um_roteador():
    from project_os.apps.internet import probes

    assert probes._gateway_do_proc("Iface\tDestination\n") == ""
    assert probes._gateway_do_netstat("nada aqui") == ""
    assert probes.gateway(ler=lambda c: "", rodar=lambda a: "") == ""


# ------------------------------------------------------------------- o banco


class _BancoFalso(object):
    def __init__(self):
        self.medidas = []
        self.quedas = []
        self._proximo = 1

    def register_schema(self, nome, comandos):
        self.schema = (nome, comandos)

    def execute(self, sql, params=()):
        if "INSERT INTO app_internet_samples" in sql:
            self.medidas.append(params)
        elif "INSERT INTO app_internet_outages" in sql:
            self.quedas.append({"id": self._proximo, "kind": params[0],
                                "started_at": params[1], "ended_at": "", "seconds": 0})
            self._proximo += 1
        elif "UPDATE app_internet_outages" in sql:
            for q in self.quedas:
                if q["id"] == params[2]:
                    q["ended_at"], q["seconds"] = params[0], params[1]
        elif "DELETE FROM app_internet_samples" in sql:
            self.apagou = params
        return None

    def query(self, sql, params=()):
        if "FROM app_internet_outages WHERE ended_at = ''" in sql:
            return [q for q in reversed(self.quedas) if not q["ended_at"]][:1]
        if "FROM app_internet_outages" in sql:
            return list(reversed(self.quedas))
        if "FROM app_internet_samples" in sql:
            return []
        return []


def _app_falso(banco=None):
    from project_os.apps.internet.app import InternetApp

    instancia = InternetApp.__new__(InternetApp)
    class _Cfg(object):
        valores = {}

        def get(self, chave, padrao=None):
            return self.valores.get(chave, padrao)

    class _Ctx(object):
        def __init__(self):
            self.db = banco or _BancoFalso()
            self.config = _Cfg()

        def emit(self, *a, **k):
            pass

    instancia.ctx = _Ctx()
    import logging
    instancia.log = logging.getLogger("teste")
    instancia._ultima = {}
    instancia._estado = ""
    instancia._estado_desde = ""
    instancia._ultima_limpeza = 9e9  # não limpar durante o teste
    return instancia


def test_cair_abre_uma_queda_e_voltar_fecha():
    from project_os.apps.internet import probes

    banco = _BancoFalso()
    app = _app_falso(banco)
    app._registrar({"state": probes.NO_AR, "gateway": "10.0.0.1"})
    assert banco.quedas == [], "no ar não abre queda"

    app._registrar({"state": probes.SEM_INTERNET, "gateway": "10.0.0.1"})
    assert len(banco.quedas) == 1 and banco.quedas[0]["kind"] == "internet"
    assert banco.quedas[0]["ended_at"] == "", "enquanto está fora, fica aberta"

    app._registrar({"state": probes.NO_AR, "gateway": "10.0.0.1"})
    assert banco.quedas[0]["ended_at"], "voltou, fechou"


def test_estado_que_nao_muda_nao_vira_queda_nova():
    """Cinco minutos fora são uma queda de cinco minutos, não cinco quedas."""
    from project_os.apps.internet import probes

    banco = _BancoFalso()
    app = _app_falso(banco)
    for _ in range(5):
        app._registrar({"state": probes.SEM_INTERNET, "gateway": "10.0.0.1"})
    assert len(banco.quedas) == 1
    assert len(banco.medidas) == 5, "mas toda medida é guardada"


def test_a_causa_que_muda_no_meio_vira_duas_quedas():
    """Roteador que volta e provedor que não são dois problemas e duas
    conversas diferentes."""
    from project_os.apps.internet import probes

    banco = _BancoFalso()
    app = _app_falso(banco)
    app._registrar({"state": probes.SEM_ROTEADOR, "gateway": "10.0.0.1"})
    app._registrar({"state": probes.SEM_INTERNET, "gateway": "10.0.0.1"})
    assert [q["kind"] for q in banco.quedas] == ["roteador", "internet"]
    assert banco.quedas[0]["ended_at"], "a primeira foi fechada ao mudar a causa"


def test_reiniciar_no_meio_de_uma_queda_continua_a_mesma():
    """Sem isso, uma queda de duas horas viraria três de quarenta minutos --
    uma por reinício do serviço."""
    from project_os.apps.internet import probes

    banco = _BancoFalso()
    banco.query = lambda sql, params=(): (
        [{"state": probes.SEM_INTERNET, "ts": "2026-08-18T03:00:00Z"}]
        if "FROM app_internet_samples" in sql else [])
    app = _app_falso(banco)
    app._recuperar_estado()
    assert app._estado == probes.SEM_INTERNET
    assert app._estado_desde == "2026-08-18T03:00:00Z"


def test_a_duracao_da_queda_e_em_segundos():
    from project_os.apps.internet.app import _segundos_entre

    assert _segundos_entre("2026-08-18T03:12:00Z", "2026-08-18T03:19:30Z") == 450
    assert _segundos_entre("", "2026-08-18T03:19:30Z") == 0
    assert _segundos_entre("2026-08-18T03:19:30Z", "2026-08-18T03:12:00Z") == 0, "nunca negativo"


def test_medida_de_rotina_e_apagada_e_queda_nao():
    """Milhares de medidas por semana contra uma linha por queda -- e a queda é
    o que ele vai querer citar para o provedor daqui a três meses."""
    fonte = _ler(APP, "app.py")
    assert "DELETE FROM %s WHERE ts < ?" in fonte
    assert "DELETE FROM app_internet_outages" not in fonte
    assert "DELETE" not in fonte[fonte.index("def quedas"):]


def test_o_intervalo_tem_piso():
    """Medir de três em três segundos castiga o roteador sem deixar a resposta
    mais verdadeira."""
    from project_os.apps.internet import app as modulo

    app = _app_falso()
    app.ctx.config.valores["interval_seconds"] = 1
    assert app.intervalo == modulo.INTERVALO_MINIMO
    app.ctx.config.valores["interval_seconds"] = 300
    assert app.intervalo == 300, "acima do piso, vale o que ele pediu"


# -------------------------------------------------------------- o app inteiro


def test_o_manifesto_bate_com_o_codigo():
    dados = json.loads(_ler(APP, "manifest.json"))
    assert dados["id"] == "internet"
    assert dados["entrypoint"] == "app:setup"
    assert dados["ui"]["panel"] == "panel.js"
    assert dados["ui"]["styles"] == "panel.css"
    assert os.path.isfile(os.path.join(APP, "web", dados["ui"]["panel"]))
    assert os.path.isfile(os.path.join(APP, "web", dados["ui"]["styles"]))
    chaves = set(c["key"] for c in dados["config_schema"])
    assert {"interval_seconds", "timeout_seconds", "gateway", "keep_days"} == chaves


def test_o_app_nao_traz_dependencia_nova():
    """Um app que pede biblioteca é um app que às vezes não instala. Nem root:
    ping precisaria, e o serviço roda como usuário comum."""
    fonte = _ler(APP, "probes.py") + _ler(APP, "app.py")
    for proibido in ("import requests", "import httpx", "yt_dlp", "scapy", "icmp"):
        assert proibido not in fonte, proibido


def test_as_rotas_existem_e_pedem_sessao():
    fonte = _ler(APP, "app.py")
    for rota in ('@router.get("/status")', '@router.get("/outages")',
                 '@router.get("/samples")', '@router.post("/check")'):
        assert rota in fonte, rota
    assert fonte.count("Depends(auth.require_auth)") >= 4, "toda rota pede sessão"


def test_a_tela_fala_as_duas_linguas():
    painel = _ler(APP, "web", "panel.js")
    pt = _ler(RAIZ, "web", "lib", "strings-pt.js")
    for chave in ("net.state.ok", "net.state.dns", "net.state.internet",
                  "net.state.roteador", "net.check", "net.section.outages"):
        assert "'%s':" % chave in painel, "falta em inglês: " + chave
        assert "'%s':" % chave in pt, "falta em português: " + chave


def test_a_faixa_do_dia_rola_dentro_de_si():
    """240 marcas numa fileira esticariam a página para o lado num celular --
    o mesmo defeito da tira de abas, que já custou uma versão."""
    estilo = _ler(APP, "web", "panel.css")
    faixa = estilo[estilo.index(".net-strip {"):]
    faixa = faixa[:faixa.index("}")]
    assert "overflow-x: auto" in faixa
    assert "min-width: 0" in faixa


# ------------------------------------------- a categoria que a tela sabe dizer


def test_toda_categoria_do_catalogo_tem_nome_na_tela():
    """A categoria "system" foi parar no catálogo por um instante e a Loja não
    tem tradução para ela -- o cartão sairia com a palavra crua em inglês no
    meio de uma tela em português. São nove categorias e a tela nomeia as nove.
    """
    import yaml

    catalogo = yaml.safe_load(_ler(RAIZ, "project_os", "data", "catalog.yaml"))
    tela = _ler(RAIZ, "web", "views", "store.js")
    faltando = sorted(set(
        str(item.get("category") or "")
        for item in catalogo
        if "'store.category.%s':" % item.get("category") not in tela
    ))
    assert not faltando, (
        "o catálogo usa categoria que a Loja não sabe nomear: %s" % ", ".join(faltando))


def test_o_app_esta_na_loja():
    """Sem entrada no catálogo, ele não aparece na Loja -- e a Loja é onde se
    procura app nesta caixa, porque nada vem instalado de fábrica."""
    import yaml

    catalogo = yaml.safe_load(_ler(RAIZ, "project_os", "data", "catalog.yaml"))
    entrada = next((i for i in catalogo if i.get("id") == "internet"), None)
    assert entrada, "o app não está no catálogo"
    assert entrada["kind"] == "builtin"
    assert entrada["category"] == "network"
    assert entrada["summary"] and entrada["description"]


# ----------------------------------------- desligar não pode custar a rede


def _rodar(corrotina):
    import asyncio

    laco = asyncio.new_event_loop()
    try:
        return laco.run_until_complete(corrotina)
    finally:
        laco.close()


def test_desligar_nao_espera_a_medida_que_ficou_no_meio():
    """Descoberto pela suíte ficando cinco vezes mais lenta.

    Este app é builtin, então todo teste que sobe o gerenciador de apps o liga,
    e ligar significa medir -- socket de verdade, com prazo de três segundos por
    aparelho que não responde. Thread bloqueada em socket não é cancelável, e
    esperar por ela no stop() punha o pior caso da rede no caminho de cada
    desligamento. Numa caixa sem rede seria o serviço demorando para parar.

    O resultado da medida em voo não interessa a ninguém: quem está desligando
    não vai ler.
    """
    import concurrent.futures
    import time

    app = _app_falso()
    app._threads = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    app._threads.submit(time.sleep, 3)   # a medida presa no socket
    app._task = None

    comeco = time.time()
    _rodar(app.stop())
    gasto = time.time() - comeco
    assert gasto < 0.5, "o desligamento esperou %.1fs pela medida" % gasto
    assert app._threads is None


def test_a_medida_roda_na_thread_do_app_e_nao_na_do_sistema():
    """O executor padrão é compartilhado com o processo inteiro; encher ele com
    socket de rede atrasaria coisa que não tem nada a ver com este app."""
    fonte = _ler(APP, "app.py")
    assert "run_in_executor(\n            self._threads" in fonte or \
           "run_in_executor(self._threads" in fonte
    assert "shutdown(wait=False)" in fonte


# ------------------------------------------------------ o cartão da tela inicial


def test_o_cartao_diz_uma_frase_e_nao_um_despejo_de_campos():
    """A tela inicial não sabe traduzir chave de app nenhum: quem manda a frase
    pronta é o app. Sem isso o cartão dumpava "state: roteador / gateway_ms:
    None", que é saída de depuração e não tela de casa.
    """
    from project_os.apps.internet import probes

    app = _app_falso()
    app._estado = probes.SEM_ROTEADOR
    app._ultima = {"gateway_ms": None, "internet_ms": 8.0, "dns_ms": 9.0}
    cartao = app.status()
    assert cartao["level"] == "danger"
    assert "roteador não responde" in cartao["summary"]
    assert "aqui dentro de casa" in cartao["summary"], "diz o que fazer, não só o que houve"
    rotulos = [c["label"] for c in cartao["fields"]]
    assert rotulos[:3] == ["Roteador", "Internet", "Nomes (DNS)"]
    assert cartao["fields"][0]["value"] == "não respondeu"


def test_o_cartao_sem_queda_nenhuma_e_curto():
    app = _app_falso()
    app._estado = "ok"
    app._ultima = {"gateway_ms": 4.0, "internet_ms": 7.0, "dns_ms": 10.0}
    cartao = app.status()
    assert cartao["level"] == "ok"
    assert cartao["summary"] == "Funcionando. Nenhuma queda nas últimas 24 horas."
    assert len(cartao["fields"]) == 3, "sem queda, não há o que contar sobre quedas"


def test_o_plural_e_de_gente_e_nao_de_programa():
    """"1 queda(s)" é jeito de programador escrever; ninguém fala assim."""
    app = _app_falso()
    app._estado = "ok"
    app.quedas = lambda limite=30: [
        {"id": 1, "kind": "internet", "started_at": "2999-01-01T00:00:00Z",
         "ended_at": "2999-01-01T00:00:30Z", "seconds": 30}]
    frase = app.status()["summary"]
    assert "1 queda nas" in frase, frase
    app.quedas = lambda limite=30: [
        {"id": i, "kind": "internet", "started_at": "2999-01-01T00:00:00Z",
         "ended_at": "2999-01-01T00:00:30Z", "seconds": 30} for i in (1, 2)]
    assert "2 quedas nas" in app.status()["summary"]


def test_o_dns_e_amarelo_e_nao_vermelho():
    """Nomes que não resolvem com a conexão de pé é chato, não é apagão -- e é o
    único dos três que costuma dar para consertar sozinho."""
    from project_os.apps.internet import probes

    app = _app_falso()
    app._estado = probes.SEM_DNS
    assert app.status()["level"] == "warn"


def test_a_duracao_sai_legivel():
    from project_os.apps.internet.app import _duracao

    assert [_duracao(v) for v in (0, 8, 95, 3600, 7530)] == ["0s", "8s", "2min", "1h", "2h6min"]
