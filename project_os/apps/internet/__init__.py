# -*- coding: utf-8 -*-
"""O app que sabe responder "a internet caiu de madrugada?".

Uma caixa que fica ligada o tempo todo é a única testemunha possível de uma
queda de dez minutos às três da manhã. Enquanto ninguém anota, a conversa com o
provedor é sempre a mesma: "está funcionando agora, senhor".

Este app anota. E anota separando **onde** quebrou -- roteador, provedor ou DNS
--, porque para quem está na frente da tela os três são idênticos e o conserto
de cada um é diferente. A lógica das três perguntas está em ``probes.py``, sem
banco e sem asyncio, para poder ser medida inteira sem rede.

Nada aqui precisa de dependência nova nem de root: conexão TCP com prazo, da
biblioteca padrão. Ping seria mais bonito e precisa de root, que uma caixa
rodando como usuário comum não tem.
"""
