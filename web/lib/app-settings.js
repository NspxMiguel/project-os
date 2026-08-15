// lib/app-settings.js — os ajustes de um app, montados a partir do manifesto.
//
// Existe para um caso só, e é um beco sem saída de verdade: quando um app **não
// sobe**, a tela dele mostrava o erro e o traceback, e mais nada. O painel do
// app é servido pelo próprio app, então um app quebrado não tem painel -- e é
// exatamente a configuração dele que precisa ser mexida para ele voltar. A
// mensagem chegava a mandar "veja os logs pelo painel do app", que é o painel
// que não existe.
//
// O backend já respondia por isso desde sempre (`GET/PUT /api/settings/apps/{id}`,
// que devolve o `config_schema` do manifesto junto com os valores atuais e
// reinicia o app depois de gravar); faltava alguém desenhar. Os tipos de campo
// são os que os manifestos usam: string, number, boolean, array/list.

import {h, mount, clear} from './dom.js';
import {icon} from './icons.js';
import * as api from './api.js';
import {t} from './format.js';
import {toast} from './toast.js';

const SEGREDO = /(token|secret|password|senha|key)$/i;

function ehSegredo(campo) {
  return campo.secret === true || SEGREDO.test(String(campo.key || ''));
}

function valorDe(valores, caminho, padrao) {
  let atual = valores;
  for (const parte of String(caminho).split('.')) {
    if (atual === null || atual === undefined) return padrao;
    atual = atual[parte];
  }
  return atual === undefined ? padrao : atual;
}

/** Um cartão com os ajustes do app, ou null se o app não declara nenhum.
 *
 *  `onSaved` é chamado depois de gravar -- normalmente para recarregar a rota,
 *  já que gravar reinicia o app e ele pode ter subido desta vez.
 */
export async function appSettingsCard(appId, {onSaved = null} = {}) {
  let dados;
  try {
    dados = await api.get('/settings/apps/' + encodeURIComponent(appId));
  } catch (err) {
    return null;
  }
  const schema = Array.isArray(dados && dados.schema) ? dados.schema : [];
  if (!schema.length) return null;

  const valores = (dados && dados.values) || {};
  const pendentes = {};
  const corpo = h('div', {class: 'form'});
  const raiz = h('div', {class: 'card'},
    h('div', {class: 'card__header'},
      h('h3', {class: 'card__title'}, t('appsettings.title')),
      h('span', {class: 'small muted'}, t('appsettings.lead')),
    ),
    corpo,
  );

  function campoDe(campo) {
    const tipo = String(campo.type || 'string');
    const atual = valorDe(valores, campo.key, campo.default);
    const rotulo = campo.label || campo.key;
    let controle;

    if (tipo === 'boolean') {
      controle = h('input', {
        type: 'checkbox', checked: Boolean(atual),
        onChange: (evento) => { pendentes[campo.key] = evento.target.checked; },
      });
    } else if (tipo === 'number') {
      controle = h('input', {
        type: 'number', value: atual === null || atual === undefined ? '' : String(atual),
        onChange: (evento) => {
          const bruto = evento.target.value;
          pendentes[campo.key] = bruto === '' ? null : Number(bruto);
        },
      });
    } else if (tipo === 'array' || tipo === 'list') {
      // Uma linha por item: é o formato que as duas listas de hoje têm
      // (pastas da biblioteca, números liberados) e o único que não obriga
      // ninguém a digitar JSON.
      const texto = Array.isArray(atual) ? atual.join('\n') : '';
      controle = h('textarea', {
        rows: 3, value: texto,
        onChange: (evento) => {
          pendentes[campo.key] = evento.target.value
            .split('\n').map((linha) => linha.trim()).filter(Boolean);
        },
      });
    } else {
      controle = h('input', {
        type: ehSegredo(campo) ? 'password' : 'text',
        value: atual === null || atual === undefined ? '' : String(atual),
        placeholder: campo.placeholder || '',
        onChange: (evento) => { pendentes[campo.key] = evento.target.value; },
      });
    }

    return h('div', {class: 'field'},
      h('label', {class: 'field__label'}, rotulo),
      controle,
      campo.help ? h('p', {class: 'field__hint'}, campo.help) : null,
    );
  }

  async function gravar(evento) {
    const botao = evento.currentTarget;
    if (!Object.keys(pendentes).length) return;
    botao.disabled = true;
    try {
      const resposta = await api.put(
        '/settings/apps/' + encodeURIComponent(appId), {values: pendentes},
      );
      toast(
        resposta && resposta.restarted
          ? t('appsettings.savedRestarted')
          : t('appsettings.saved'),
        {type: 'success'},
      );
      for (const chave of Object.keys(pendentes)) delete pendentes[chave];
      if (onSaved) onSaved(resposta);
    } catch (err) {
      toast((err && err.message) || t('state.error'), {type: 'error'});
    } finally {
      botao.disabled = false;
    }
  }

  mount(corpo, schema.map(campoDe).concat([
    h('div', {class: 'row'},
      h('button', {class: 'btn btn--primary', type: 'button', onClick: gravar},
        icon('save', {size: 15}), t('appsettings.save')),
    ),
  ]));
  return raiz;
}

export default {appSettingsCard};
