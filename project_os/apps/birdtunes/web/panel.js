// panel.js -- a tela do BirdTunes.
//
// Módulo ES puro, sem build: `h()` do dom.js do project-os, os tokens do tema
// do sistema e o panel.css ao lado deste arquivo. Fala com as rotas de
// ../app.py (build_router): status/play/pause/next, /library com busca e
// ordenação, playlists CRUD, /queue, /import/*, /schedule, /outputs, /stats.
//
// A organização é a de um app de streaming, e é uma reforma: a tela anterior
// empilhava sete cartões abertos ao mesmo tempo -- tocar, compatibilidade,
// acervo, playlists, importar, agenda e saída -- numa coluna só. Tudo pesava
// igual, nada tinha dono, e achar uma faixa era rolar a página inteira.
//
// Agora:
//   * navegação em cima (Início, Músicas, Playlists, Adicionar, Agenda);
//   * uma seção por vez, com título e ferramentas próprias;
//   * a barra do que está tocando fica fixa embaixo, em todas as seções;
//   * a saída de som mora na barra, atrás do nome do aparelho, que é onde se
//     procura por ela -- e não numa oitava caixa no fim da página;
//   * playlist tem tela própria: abre, mostra as faixas, reordena, remove.
//
// A busca e a ordenação são as do servidor (`/library?search=&sort=`), que já
// existiam e a tela antiga nunca usou: filtrar no navegador uma lista que o
// SQLite já sabe filtrar é trabalho repetido e não sobrevive a uma biblioteca
// grande.

export default {
  async mount(root, ctx) {
    const {h, t, appApi, config, toast, confirm} = ctx;
    const fmtMod = await import('/lib/format.js');
    const dialogs = await import('/lib/toast.js');

    fmtMod.setStrings('en', {
      'bt.title': 'BirdTunes',
      'bt.tagline': 'Your music, on the speakers in the house',
      'bt.nav.home': 'Home',
      'bt.nav.library': 'Songs',
      'bt.nav.playlists': 'Playlists',
      'bt.nav.add': 'Add',
      'bt.nav.schedule': 'Schedule',
      'bt.now_playing': 'Now playing',
      'bt.nothing_playing': 'Nothing playing',
      'bt.nothing_playing.hint': 'Press play to start the whole library, or open a playlist.',
      'bt.play': 'Play',
      'bt.pause': 'Pause',
      'bt.resume': 'Resume',
      'bt.stop': 'Stop',
      'bt.next': 'Next',
      'bt.quiet_hours': 'Quiet hours -- BirdTunes will not play right now.',
      'bt.next_change': 'Next',
      'bt.queue': 'Up next',
      'bt.queue.empty': 'Nothing queued.',
      'bt.queue.more': 'and %d more',
      'bt.section.output': 'Where it plays',
      'bt.output.backend': 'Connection',
      'bt.output.device': 'Speaker',
      'bt.output.none': 'Not chosen',
      'bt.output.empty': 'No speakers found yet. Run device discovery from Devices.',
      'bt.output.pair': 'Pair',
      'bt.output.pin': 'PIN shown on the speaker',
      'bt.output.pin.send': 'Confirm PIN',
      'bt.output.paired': 'Paired.',
      'bt.output.volume': 'Volume',
      'bt.output.silent': 'No speaker',
      'bt.section.library': 'Songs',
      'bt.library.empty': 'No tracks yet. Add from YouTube, or drop files in the media folder and scan.',
      'bt.library.empty.search': 'Nothing matches "%s".',
      'bt.library.scan': 'Scan folder',
      'bt.library.scanned': 'Scan done: %d added, %d already known.',
      'bt.library.search': 'Search by title, artist or album',
      'bt.library.sort': 'Sort',
      'bt.library.sort.title': 'Title',
      'bt.library.sort.artist': 'Artist',
      'bt.library.sort.added': 'Recently added',
      'bt.library.sort.duration': 'Longest',
      'bt.library.count': '%d songs',
      'bt.library.like': 'Like',
      'bt.library.less': 'Recommend less',
      'bt.library.block': 'Never play',
      'bt.library.unblock': 'Allow again',
      'bt.library.play': 'Play this',
      'bt.library.remove': 'Remove from library',
      'bt.library.remove.confirm': 'Remove "%s" from the library? The file on disk is not deleted.',
      'bt.library.removed': 'Removed from the library.',
      'bt.library.liked': 'Liked',
      'bt.library.lessened': 'less often',
      'bt.library.blocked': 'blocked',
      'bt.library.missing': 'file missing',
      'bt.section.compat': 'Compatibility',
      'bt.compat.warn': '%d of %d tracks cannot be played on this output.',
      'bt.compat.convert': 'Convert to MP3',
      'bt.compat.no_ffmpeg': 'Install ffmpeg to convert incompatible tracks.',
      'bt.section.playlists': 'Playlists',
      'bt.playlists.empty': 'No playlists yet. Create one and add songs to it.',
      'bt.playlists.name': 'Playlist name',
      'bt.playlists.create': 'New playlist',
      'bt.playlists.created': 'Playlist "%s" created.',
      'bt.playlists.play': 'Play',
      'bt.playlists.delete': 'Delete playlist',
      'bt.playlists.delete.confirm': 'Delete the playlist "%s"? The songs stay in the library.',
      'bt.playlists.rename': 'Rename',
      'bt.playlists.tracks': '%d songs',
      'bt.playlists.track': '1 song',
      'bt.playlists.empty.detail': 'This playlist is empty. Add songs from Songs, or paste a YouTube link.',
      'bt.playlists.back': 'All playlists',
      'bt.playlists.remove': 'Remove from playlist',
      'bt.playlists.up': 'Move up',
      'bt.playlists.down': 'Move down',
      'bt.playlists.add': 'Add to playlist',
      'bt.playlists.added': 'Added to %s.',
      'bt.playlists.add.youtube': 'Add from YouTube',
      'bt.state.idle': 'idle',
      'bt.state.playing': 'playing',
      'bt.state.paused': 'paused',
      'bt.state.connecting': 'connecting',
      'bt.state.stopped': 'stopped',
      'bt.section.import': 'Add from YouTube',
      'bt.import.url': 'Video or playlist link',
      'bt.import.preview': 'Preview',
      'bt.import.start': 'Add',
      'bt.import.found': 'Found: %s (%d items)',
      'bt.import.no_ytdlp': 'yt-dlp is not installed. YouTube import is unavailable.',
      'bt.import.no_ytdlp.cast': 'Playing on the TV still works — it needs no download.',
      'bt.import.jobs': 'Recent imports',
      'bt.import.jobs.empty': 'No imports yet.',
      'bt.import.cast': 'Play on the TV (no download)',
      'bt.import.cast.hint': 'Plays through the television’s own YouTube app — so the ads come from the TV, not from here, and nothing on this box can cut them. The ad-free path is Add: the audio is kept here and BirdTunes plays it.',
      'bt.adblock.title': 'Cut ads out of what you add',
      'bt.adblock.on': 'Cut sponsors and promos (SponsorBlock)',
      'bt.adblock.what': 'Uses the SponsorBlock database — segments people marked in the video — and cuts %s from the audio kept here.',
      'bt.adblock.tv': 'On "play on the TV" it cannot work: that video is fetched by the television, and this box is not in the way.',
      'bt.adblock.saved': 'Saved.',
      'bt.import.dest': 'Add to',
      'bt.import.dest.library': 'Library only',
      'bt.import.dest.new': 'New playlist from this link',
      'bt.import.queued': 'Downloading. It lands in the library when it finishes.',
      'bt.import.cast.ok': 'Playing on %s.',
      'bt.stats.tracks': 'Songs',
      'bt.stats.playlists': 'Playlists',
      'bt.stats.plays': 'Plays',
      'bt.stats.likes': 'Likes',
      'bt.section.schedule': 'Schedule',
      'bt.schedule.lead': 'What plays while nobody is watching.',
      'bt.schedule.blocked': 'This schedule will not play',
      'bt.schedule.pick_output': 'Choose a speaker',
      'bt.schedule.last_failed': 'The last window did not play',
      'bt.schedule.enabled': 'Schedule enabled',
      'bt.schedule.quiet': 'Quiet hours',
      'bt.schedule.quiet_start': 'From',
      'bt.schedule.quiet_end': 'To',
      'bt.schedule.windows.empty': 'No windows yet -- add one or use a preset below.',
      'bt.schedule.windows': 'When it plays',
      'bt.schedule.presets': 'Start from a preset',
      'bt.schedule.add': 'Add a window',
      'bt.schedule.remove': 'Remove this window',
      'bt.schedule.window.name': 'Name',
      'bt.schedule.window.from': 'From',
      'bt.schedule.window.to': 'To',
      'bt.schedule.window.days': 'Days',
      'bt.schedule.window.playlist': 'Plays',
      'bt.schedule.window.on': 'On',
      'bt.schedule.window.off': 'Off',
      'bt.schedule.days.short': 'Mon Tue Wed Thu Fri Sat Sun',
      'bt.schedule.quiet.hint': 'BirdTunes never plays between these two times, whatever the windows say.',
      'bt.save.ok': 'Saved',
      'bt.save.failed': 'Could not save',
      'bt.action.failed': 'Action failed',
    }, {activate: false});

    const t2 = (key) => {
      const value = fmtMod.t(key);
      return value === key ? t(key) : value;
    };
    // %d/%s à moda antiga: as frases já vinham assim do dicionário e trocar o
    // formato quebraria as traduções que existem.
    const fmtStr = (key, ...args) => {
      let text = t2(key);
      args.forEach((a) => { text = text.replace(/%d|%s/, String(a)); });
      return text;
    };

    const icon = ctx.icon;
    const clock = (seconds) => fmtMod.duration(Number(seconds) || 0);

    const state = {
      view: 'home',          // home | library | playlists | add | schedule
      playlistId: '',        // aberta dentro de "playlists"
      search: '',
      sort: 'title',
      status: null,
      queue: [],
      library: [],
      playlists: [],
      playlist: null,        // a aberta, com as faixas
      compat: null,
      schedule: null,
      presets: [],
      importJobs: [],
      importDest: '',
      preview: null,
      stats: null,
      outputs: null,
      outputOpen: false,
      pairing: null,
      busy: false,
    };

    // ---------------------------------------------------------------- layout

    const nav = h('div', {class: 'bt-nav'});
    const main = h('div', {class: 'bt-main'});
    const playerBar = h('div', {class: 'bt-player'});
    const sheet = h('div', {class: 'bt-sheet', hidden: true});
    const shell = h('div', {class: 'bt'},
      h('div', {class: 'bt-top'},
        h('div', {class: 'bt-brand'},
          h('div', {class: 'bt-brand__mark'}, icon('music', {size: 20})),
          h('div', null,
            h('div', {class: 'bt-brand__name'}, t2('bt.title')),
            h('div', {class: 'bt-brand__sub'}, t2('bt.tagline')),
          ),
        ),
        nav,
      ),
      main,
      sheet,
      playerBar,
    );
    root.appendChild(shell);

    // ------------------------------------------------------------------ dados

    async function safeGet(path, query) {
      try {
        return await appApi.get(path, query ? {query} : undefined);
      } catch (err) {
        return null;
      }
    }

    async function loadCommon() {
      const [status, playlists, outputs, queue] = await Promise.all([
        safeGet('/status'), safeGet('/playlists'), safeGet('/outputs'), safeGet('/queue'),
      ]);
      try { await config.reload(); } catch (err) { /* a tela ainda desenha */ }
      state.status = status;
      state.playlists = (playlists && playlists.playlists) || [];
      state.outputs = outputs;
      state.queue = (queue && queue.queue) || [];
    }

    async function loadLibrary() {
      const data = await safeGet('/library', {search: state.search, sort: state.sort});
      state.library = (data && data.tracks) || [];
      const compat = await safeGet('/compat');
      state.compat = compat;
    }

    async function loadPlaylist(id) {
      const data = await safeGet('/playlists/' + encodeURIComponent(id));
      state.playlist = (data && data.playlist) || null;
    }

    async function loadImports() {
      const [jobs, compat] = await Promise.all([safeGet('/import'), safeGet('/compat')]);
      state.importJobs = (jobs && jobs.jobs) || [];
      state.compat = compat;
    }

    async function loadSchedule() {
      const [schedule, presets] = await Promise.all([safeGet('/schedule'), safeGet('/schedule/presets')]);
      state.schedule = schedule;
      state.presets = (presets && presets.presets) || [];
    }

    async function loadHome() {
      state.stats = await safeGet('/stats');
    }

    async function loadForView() {
      if (state.view === 'library') await loadLibrary();
      else if (state.view === 'playlists') {
        if (state.playlistId) await loadPlaylist(state.playlistId);
      } else if (state.view === 'add') await loadImports();
      else if (state.view === 'schedule') await loadSchedule();
      else await loadHome();
    }

    async function act(fn) {
      state.busy = true;
      renderPlayer();
      try {
        await fn();
      } catch (err) {
        toast(t2('bt.action.failed') + ': ' + (err.message || err), {type: 'error'});
      } finally {
        state.busy = false;
        await loadCommon();
        await loadForView();
        render();
      }
    }

    // ------------------------------------------------------------------ peças

    // A cor sai do nome, não de uma tabela de capas que não existe: dois nomes
    // diferentes dão dois ladrilhos diferentes e o mesmo nome dá sempre o mesmo.
    function hue(text) {
      let total = 0;
      String(text || '?').split('').forEach((ch) => { total = (total * 31 + ch.charCodeAt(0)) % 360; });
      return total;
    }

    function art(track, size) {
      const label = String((track && (track.title || track.name)) || '?').trim().charAt(0).toUpperCase() || '?';
      const node = h('div', {
        class: 'bt-art bt-art--' + (size || 'sm'),
        style: 'background: linear-gradient(150deg, hsl(' + hue(track && (track.title || track.name)) +
          ' 32% 30%), hsl(' + ((hue(track && (track.title || track.name)) + 40) % 360) + ' 28% 20%))',
      }, label);
      // Miniatura de verdade quando o import trouxe uma; nunca uma capa
      // genérica fingindo ser a do disco.
      if (track && track.thumbnail) {
        node.replaceChildren(h('img', {src: track.thumbnail, alt: '', loading: 'lazy'}));
      }
      return node;
    }

    function iconBtn(name, title, onClick, options) {
      const opts = options || {};
      return h('button', {
        class: 'btn btn--icon btn--sm', type: 'button', title, 'aria-label': title,
        disabled: Boolean(opts.disabled), dataset: opts.on ? {on: 'true'} : undefined,
        onClick,
      }, icon(name, {size: 16}));
    }

    // O conjunto de ícones tem uma seta só, apontando para a direita. Girar é
    // trabalho do CSS -- desenhar mais três iguais não seria.
    function chevBtn(direction, title, onClick, options) {
      const opts = options || {};
      return h('button', {
        class: 'btn btn--icon btn--sm', type: 'button', title, 'aria-label': title,
        disabled: Boolean(opts.disabled), onClick,
      }, icon('chevron', {size: 16, class: 'bt-rot-' + (direction === 'up' ? '270' : '90')}));
    }

    function sectionHead(title, sub, tools) {
      return h('div', {class: 'bt-section__head'},
        h('h3', {class: 'bt-section__title'}, title),
        sub ? (sub.nodeType ? sub : h('span', {class: 'bt-section__sub'}, sub)) : null,
        tools ? h('div', {class: 'bt-section__tools'}, tools) : null,
      );
    }

    function empty(text) {
      return h('div', {class: 'empty empty--sm'}, h('p', {class: 'empty__text'}, text));
    }

    function playlistMenu(track) {
      const usable = (state.playlists || []).filter((pl) => !pl.virtual);
      if (!usable.length) return null;
      return h('select', {
        class: 'input input--sm select--compact', title: t2('bt.playlists.add'),
        onChange: (event) => {
          const target = event.target.value;
          event.target.selectedIndex = 0;
          if (!target) return;
          const playlist = usable.filter((pl) => pl.id === target)[0];
          act(async () => {
            await appApi.post('/playlists/' + target + '/tracks', {track_ids: [track.id]});
            toast(fmtStr('bt.playlists.added', (playlist && playlist.name) || ''), {type: 'success'});
          });
        },
      }, [h('option', {value: ''}, '+')].concat(
        usable.map((pl) => h('option', {value: pl.id}, pl.name))));
    }

    // Uma linha de faixa, igual em todo lugar: acervo, fila e playlist. O que
    // muda é só o que vem na direita.
    function trackRow(track, options) {
      const opts = options || {};
      const fb = track.feedback || {};
      const status = state.status || {};
      const current = status.track_id && status.track_id === track.id;
      const marks = [
        track.album || '',
        fb.likes ? '♥ ' + fb.likes : '',
        fb.less ? t2('bt.library.lessened') : '',
        fb.blocked ? t2('bt.library.blocked') : '',
        track.missing ? t2('bt.library.missing') : '',
      ].filter(Boolean);
      return h('div', {class: 'bt-track', dataset: {current: current ? 'true' : 'false'}},
        opts.index !== undefined
          ? h('span', {class: 'bt-track__index'}, String(opts.index))
          : art(track, 'sm'),
        h('div', {class: 'bt-track__body'},
          h('div', {class: 'bt-track__title'}, track.title || track.path || track.id),
          h('div', {class: 'bt-track__sub'},
            [track.artist || ''].concat(marks).filter(Boolean).join(' · ')),
        ),
        track.duration ? h('span', {class: 'bt-track__time'}, clock(track.duration)) : null,
        h('div', {class: 'bt-track__actions'}, (opts.actions || defaultActions)(track, fb)),
      );
    }

    function defaultActions(track, fb) {
      return [
        iconBtn('play', t2('bt.library.play'),
          () => act(() => appApi.post('/play', {track_id: track.id})), {disabled: track.missing}),
        iconBtn('heart', t2('bt.library.like'),
          () => act(() => appApi.post('/tracks/' + track.id + '/feedback', {action: 'like'})),
          {on: Boolean(fb.likes)}),
        iconBtn('thumbs-down', t2('bt.library.less'),
          () => act(() => appApi.post('/tracks/' + track.id + '/feedback', {action: 'less'})),
          {on: Boolean(fb.less)}),
        playlistMenu(track),
        iconBtn(fb.blocked ? 'check' : 'x',
          fb.blocked ? t2('bt.library.unblock') : t2('bt.library.block'),
          () => act(() => appApi.post('/tracks/' + track.id + '/feedback',
            {action: fb.blocked ? 'unblock' : 'block'})), {on: Boolean(fb.blocked)}),
        iconBtn('trash', t2('bt.library.remove'), async () => {
          const ok = await confirm(fmtStr('bt.library.remove.confirm', track.title || track.id));
          if (!ok) return;
          act(async () => {
            await appApi.del('/library/' + track.id);
            toast(t2('bt.library.removed'), {type: 'success'});
          });
        }),
      ];
    }

    // ------------------------------------------------------------------ início

    function progress() {
      const status = state.status || {};
      const total = Number(status.duration) || 0;
      // Sem duração não há barra: desenhar uma vazia é inventar um dado que o
      // player não deu.
      if (!total) return null;
      const done = Math.min(Number(status.position) || 0, total);
      return h('div', {class: 'bt-progress'},
        h('span', null, clock(done)),
        h('div', {class: 'bt-progress__track'},
          h('div', {class: 'bt-progress__fill', style: 'width: ' + ((done / total) * 100).toFixed(1) + '%'})),
        h('span', null, clock(total)),
      );
    }

    function homeView() {
      const status = state.status || {};
      const track = status.track || null;
      const nextChange = status.next_change || {};
      const stats = state.stats || {};
      const quick = (state.playlists || []).filter((pl) => !pl.virtual).slice(0, 6);

      return [
        status.quiet_hours_active
          ? h('div', {class: 'notice notice--warning'}, t2('bt.quiet_hours'))
          : null,
        h('div', {class: 'bt-hero'},
          art(track || {title: 'BirdTunes'}, 'lg'),
          h('div', {class: 'bt-hero__body'},
            h('div', {class: 'bt-hero__eyebrow'},
              track ? t2('bt.now_playing') : t2('bt.state.' + (status.state || 'idle'))),
            h('h2', {class: 'bt-hero__title'}, track ? (track.title || track.path) : t2('bt.nothing_playing')),
            h('div', {class: 'bt-hero__artist'},
              track ? (track.artist || '') : t2('bt.nothing_playing.hint')),
            progress(),
            nextChange.message ? h('div', {class: 'bt-hero__artist'},
              t2('bt.next_change') + ': ' + nextChange.message) : null,
            h('div', {class: 'bt-hero__actions'},
              h('button', {class: 'btn btn--primary', disabled: state.busy,
                onClick: () => act(() => appApi.post('/play', {}))},
                icon('play', {size: 16}), ' ', t2('bt.play')),
              h('button', {class: 'btn btn--outline', disabled: state.busy,
                onClick: () => act(() => appApi.post('/next', {}))},
                icon('next', {size: 16}), ' ', t2('bt.next')),
              h('button', {class: 'btn btn--outline', disabled: state.busy,
                onClick: () => act(() => appApi.post('/stop', {}))}, t2('bt.stop')),
            ),
          ),
        ),
        h('div', {class: 'bt-section'},
          sectionHead(t2('bt.queue'), state.queue.length
            ? fmtStr('bt.library.count', state.queue.length) : ''),
          state.queue.length === 0
            ? empty(t2('bt.queue.empty'))
            : h('div', {class: 'bt-tracks'}, state.queue.slice(0, 8).map((track, index) =>
                trackRow(track, {
                  index: index + 1,
                  actions: (item) => [
                    iconBtn('play', t2('bt.library.play'),
                      () => act(() => appApi.post('/play', {track_id: item.id}))),
                  ],
                }))),
          state.queue.length > 8
            ? h('p', {class: 'field__hint'}, fmtStr('bt.queue.more', state.queue.length - 8))
            : null,
        ),
        quick.length ? h('div', {class: 'bt-section'},
          sectionHead(t2('bt.section.playlists'), '',
            h('button', {class: 'btn btn--ghost btn--sm',
              onClick: () => go('playlists')}, t2('bt.playlists.back'))),
          h('div', {class: 'bt-grid'}, quick.map(playlistTile)),
        ) : null,
        h('div', {class: 'bt-stats'},
          statTile(stats.tracks, t2('bt.stats.tracks')),
          statTile(stats.playlists, t2('bt.stats.playlists')),
          statTile(stats.total_plays, t2('bt.stats.plays')),
          statTile(stats.total_likes, t2('bt.stats.likes')),
        ),
      ];
    }

    function statTile(value, label) {
      return h('div', {class: 'bt-stat'},
        h('div', {class: 'bt-stat__value'}, value === undefined || value === null ? '—' : String(value)),
        h('div', {class: 'bt-stat__label'}, label),
      );
    }

    // ------------------------------------------------------------------ acervo

    let searchTimer = null;

    function libraryView() {
      const tracks = state.library || [];
      const compat = state.compat || {};
      const incompatible = compat.incompatible || 0;

      const search = h('input', {
        class: 'input bt-search', type: 'search', value: state.search,
        placeholder: t2('bt.library.search'), 'data-bt-search': '1',
        onInput: (event) => {
          state.search = event.target.value;
          if (searchTimer) clearTimeout(searchTimer);
          // Uma consulta por pausa de digitação, e não uma por tecla.
          searchTimer = setTimeout(async () => {
            await loadLibrary();
            renderList();
          }, 220);
        },
      });

      const sort = h('select', {
        class: 'input input--sm bt-pick', title: t2('bt.library.sort'),
        onChange: async (event) => {
          state.sort = event.target.value;
          await loadLibrary();
          renderList();
        },
      }, ['title', 'artist', 'added', 'duration'].map((key) =>
        h('option', {value: key, selected: state.sort === key}, t2('bt.library.sort.' + key))));

      listHost = h('div', {class: 'bt-section'});
      // O contador fica junto do título e muda com a busca: dizer "8 músicas"
      // enquanto a lista mostra uma é a tela contando outra história.
      countNode = h('span', {class: 'bt-section__sub'}, fmtStr('bt.library.count', tracks.length));

      const node = [
        h('div', {class: 'bt-section'},
          sectionHead(t2('bt.section.library'), countNode,
            [
              h('button', {class: 'btn btn--ghost btn--sm', onClick: () => act(async () => {
                const result = await appApi.post('/library/scan', {});
                toast(fmtStr('bt.library.scanned', result.added || 0, result.updated || 0),
                  {type: 'success'});
              })}, icon('refresh', {size: 14}), ' ', t2('bt.library.scan')),
              h('button', {class: 'btn btn--outline btn--sm', onClick: () => go('add')},
                icon('plus', {size: 14}), ' ', t2('bt.playlists.add.youtube')),
            ]),
          h('div', {class: 'bt-toolbar'}, search, sort),
          incompatible > 0 ? h('div', {class: 'notice notice--warning'},
            h('div', {class: 'notice__body'},
              h('span', null, fmtStr('bt.compat.warn', incompatible, compat.total || 0)),
              compat.ffmpeg_available
                ? h('button', {class: 'btn btn--outline btn--sm', onClick: () => act(() =>
                    appApi.post('/convert', {track_ids: compat.incompatible_tracks || []}))},
                    t2('bt.compat.convert'))
                : h('span', {class: 'small muted'}, ' ' + t2('bt.compat.no_ffmpeg')),
            )) : null,
        ),
        listHost,
      ];
      renderList();
      return node;
    }

    let listHost = null;
    let countNode = null;

    // Só a lista se redesenha na busca: refazer a seção inteira tirava o foco
    // do campo no meio da digitação.
    function renderList() {
      if (!listHost) return;
      const tracks = state.library || [];
      if (countNode) countNode.textContent = fmtStr('bt.library.count', tracks.length);
      listHost.replaceChildren(tracks.length === 0
        ? empty(state.search
            ? fmtStr('bt.library.empty.search', state.search)
            : t2('bt.library.empty'))
        : h('div', {class: 'bt-tracks'}, tracks.map((track) => trackRow(track))));
    }

    // --------------------------------------------------------------- playlists

    function playlistTile(pl) {
      return h('button', {class: 'bt-tile', type: 'button', onClick: () => openPlaylist(pl.id)},
        h('div', {class: 'bt-tile__cover',
          style: 'background: linear-gradient(150deg, hsl(' + hue(pl.name) + ' 38% 34%), hsl(' +
            ((hue(pl.name) + 50) % 360) + ' 30% 22%))'},
          String(pl.name || '?').trim().charAt(0).toUpperCase()),
        h('div', null,
          h('div', {class: 'bt-tile__name'}, pl.name),
          h('div', {class: 'bt-tile__meta'}, pl.track_count === 1
            ? t2('bt.playlists.track') : fmtStr('bt.playlists.tracks', pl.track_count || 0)),
        ),
        h('span', {class: 'bt-tile__play'},
          h('span', {class: 'btn btn--icon btn--sm', title: t2('bt.playlists.play'),
            onClick: (event) => {
              event.stopPropagation();
              act(() => appApi.post('/playlists/' + pl.id + '/play', {}));
            }}, icon('play', {size: 16}))),
      );
    }

    function playlistsView() {
      if (state.playlistId && state.playlist) return playlistDetail(state.playlist);
      const list = state.playlists || [];
      return [
        h('div', {class: 'bt-section'},
          sectionHead(t2('bt.section.playlists'), '',
            h('button', {class: 'btn btn--outline btn--sm', onClick: async () => {
              const name = await dialogs.promptText(t2('bt.playlists.name'), {confirmLabel: t2('bt.playlists.create')});
              if (!name || !String(name).trim()) return;
              act(async () => {
                await appApi.post('/playlists', {name: String(name).trim()});
                toast(fmtStr('bt.playlists.created', String(name).trim()), {type: 'success'});
              });
            }}, icon('plus', {size: 14}), ' ', t2('bt.playlists.create'))),
          list.length === 0
            ? empty(t2('bt.playlists.empty'))
            : h('div', {class: 'bt-grid'}, list.map(playlistTile)),
        ),
      ];
    }

    function playlistDetail(pl) {
      const tracks = pl.tracks || [];
      const totalTime = tracks.reduce((sum, item) => sum + (Number(item.duration) || 0), 0);
      const move = (index, delta) => {
        const ids = tracks.map((item) => item.id);
        const target = index + delta;
        if (target < 0 || target >= ids.length) return;
        const swap = ids[index];
        ids[index] = ids[target];
        ids[target] = swap;
        act(() => appApi.post('/playlists/' + pl.id + '/reorder', {track_ids: ids}));
      };
      return [
        h('button', {class: 'bt-back', type: 'button', onClick: () => openPlaylist('')},
          icon('chevron', {size: 16, class: 'bt-rot-180'}), t2('bt.playlists.back')),
        h('div', {class: 'bt-hero'},
          h('div', {class: 'bt-art bt-art--lg',
            style: 'background: linear-gradient(150deg, hsl(' + hue(pl.name) + ' 38% 34%), hsl(' +
              ((hue(pl.name) + 50) % 360) + ' 30% 22%))'},
            String(pl.name || '?').trim().charAt(0).toUpperCase()),
          h('div', {class: 'bt-hero__body'},
            h('div', {class: 'bt-hero__eyebrow'}, t2('bt.section.playlists')),
            h('h2', {class: 'bt-hero__title'}, pl.name),
            h('div', {class: 'bt-hero__artist'},
              [tracks.length === 1 ? t2('bt.playlists.track') : fmtStr('bt.playlists.tracks', tracks.length),
                totalTime ? clock(totalTime) : ''].filter(Boolean).join(' · ')),
            h('div', {class: 'bt-hero__actions'},
              h('button', {class: 'btn btn--primary', disabled: !tracks.length,
                onClick: () => act(() => appApi.post('/playlists/' + pl.id + '/play', {}))},
                icon('play', {size: 16}), ' ', t2('bt.playlists.play')),
              h('button', {class: 'btn btn--outline', onClick: () => {
                state.importDest = pl.id;
                go('add');
              }}, icon('plus', {size: 16}), ' ', t2('bt.playlists.add.youtube')),
              pl.virtual ? null : h('button', {class: 'btn btn--outline', onClick: async () => {
                const name = await dialogs.promptText(t2('bt.playlists.rename'), {value: pl.name});
                if (!name || !String(name).trim()) return;
                act(() => appApi.patch('/playlists/' + pl.id, {name: String(name).trim()}));
              }}, t2('bt.playlists.rename')),
              pl.virtual ? null : h('button', {class: 'btn btn--danger', onClick: async () => {
                const ok = await confirm(fmtStr('bt.playlists.delete.confirm', pl.name));
                if (!ok) return;
                await appApi.del('/playlists/' + pl.id);
                state.playlistId = '';
                state.playlist = null;
                await loadCommon();
                render();
              }}, t2('bt.playlists.delete')),
            ),
          ),
        ),
        tracks.length === 0
          ? empty(t2('bt.playlists.empty.detail'))
          : h('div', {class: 'bt-tracks'}, tracks.map((track, index) => trackRow(track, {
              index: index + 1,
              actions: (item) => [
                iconBtn('play', t2('bt.library.play'),
                  () => act(() => appApi.post('/play', {track_id: item.id}))),
                chevBtn('up', t2('bt.playlists.up'), () => move(index, -1),
                  {disabled: index === 0 || pl.virtual}),
                chevBtn('down', t2('bt.playlists.down'), () => move(index, 1),
                  {disabled: index === tracks.length - 1 || pl.virtual}),
                pl.virtual ? null : iconBtn('x', t2('bt.playlists.remove'), () => act(() =>
                  appApi.del('/playlists/' + pl.id + '/tracks', {track_ids: [item.id]}))),
              ].filter(Boolean),
            }))),
      ];
    }

    async function openPlaylist(id) {
      state.playlistId = id;
      state.playlist = null;
      if (id) await loadPlaylist(id);
      render();
    }

    // ---------------------------------------------------------------- adicionar

    function addFromYouTube(url, dest) {
      const target = dest === undefined ? state.importDest : dest;
      const body = {url: url};
      if (target === 'new') body.as_playlist = true;
      else if (target) body.playlist_id = target;
      else body.as_playlist = Boolean(state.preview && state.preview.kind === 'playlist');
      return act(async () => {
        await appApi.post('/import/youtube', body);
        state.preview = null;
        toast(t2('bt.import.queued'), {type: 'success'});
      });
    }

    // O corta-propaganda. Ele age no que é *baixado*: o SponsorBlock é uma base
    // de trechos marcados por gente, e o corte é feito no arquivo aqui. No
    // caminho "tocar na TV" não há o que interceptar -- quem busca o vídeo é a
    // televisão, direto do YouTube, e o Pi não está no meio. Dizer isso é o que
    // separa um botão de um enfeite.
    function adblockCard() {
      const estado = (state.compat && state.compat.sponsorblock) || null;
      if (!estado) return null;
      const categorias = (estado.categories || []).join(', ');
      return h('div', {class: 'bt-sheet'},
        h('div', {class: 'bt-section__title'}, t2('bt.adblock.title')),
        h('label', {class: 'row row--tight'},
          h('input', {
            type: 'checkbox', checked: Boolean(estado.enabled), disabled: !estado.available,
            onChange: (event) => act(async () => {
              await config.set('import.youtube.skip_sponsors', event.target.checked);
              toast(t2('bt.adblock.saved'), {type: 'success'});
            }),
          }),
          h('span', null, t2('bt.adblock.on')),
        ),
        h('p', {class: 'field__hint'}, fmtStr('bt.adblock.what', categorias)),
        estado.available
          ? null
          : h('div', {class: 'notice notice--warning'},
              h('div', {class: 'notice__body'}, h('span', null, estado.reason))),
        h('p', {class: 'field__hint'}, t2('bt.adblock.tv')),
      );
    }

    function addView() {
      let urlRef = null;
      const jobs = state.importJobs || [];
      // Sem yt-dlp não há *download*: Prever e Adicionar ficam desligados e a
      // razão aparece antes do clique. Mandar o vídeo para a televisão continua
      // de pé -- isso só precisa do id, que sai do próprio link.
      const podeImportar = !state.compat || state.compat.ytdlp_available !== false;
      const readUrl = () => (urlRef && urlRef.value ? urlRef.value.trim() : '');

      return [
        h('div', {class: 'bt-section'},
          sectionHead(t2('bt.section.import')),
          podeImportar ? null : h('div', {class: 'notice notice--warning'},
            h('div', {class: 'notice__body'},
              h('span', null, t2('bt.import.no_ytdlp') + ' ' + t2('bt.import.no_ytdlp.cast')))),
          h('div', {class: 'bt-toolbar'},
            h('input', {class: 'input bt-search', type: 'text', 'data-import-url': '1',
              placeholder: t2('bt.import.url'), ref: (el) => { urlRef = el; }}),
            h('button', {class: 'btn btn--outline', disabled: !podeImportar, onClick: async () => {
              const url = readUrl();
              if (!url) return;
              try {
                state.preview = await appApi.get('/import/preview', {query: {url}});
              } catch (err) {
                toast(String(err.message || err), {type: 'error'});
                state.preview = null;
              }
              render();
            }}, t2('bt.import.preview')),
            h('button', {class: 'btn btn--primary', disabled: !podeImportar, onClick: () => {
              const url = readUrl();
              if (url) addFromYouTube(url);
            }}, t2('bt.import.start')),
          ),
          h('label', {class: 'field'},
            h('span', {class: 'field__label'}, t2('bt.import.dest')),
            h('select', {class: 'input',
              onChange: (event) => { state.importDest = event.target.value; }},
              [
                h('option', {value: '', selected: !state.importDest}, t2('bt.import.dest.library')),
                h('option', {value: 'new', selected: state.importDest === 'new'}, t2('bt.import.dest.new')),
              ].concat((state.playlists || []).filter((pl) => !pl.virtual).map(
                (pl) => h('option', {value: pl.id, selected: pl.id === state.importDest}, pl.name))),
            ),
          ),
          state.preview
            ? h('p', {class: 'field__hint'},
                fmtStr('bt.import.found', state.preview.title, state.preview.count))
            : null,
          // Duas coisas diferentes, lado a lado de propósito: Adicionar guarda o
          // áudio aqui; este manda a televisão tocar.
          h('div', {class: 'bt-toolbar'},
            h('button', {class: 'btn btn--outline', onClick: () => {
              const url = readUrl();
              if (!url) return;
              act(async () => {
                const result = await appApi.post('/play/youtube', {url});
                toast(fmtStr('bt.import.cast.ok', result.device || ''), {type: 'success'});
              });
            }}, icon('cast', {size: 16}), ' ', t2('bt.import.cast')),
          ),
          h('p', {class: 'field__hint'}, t2('bt.import.cast.hint')),
          adblockCard(),
        ),
        h('div', {class: 'bt-section'},
          sectionHead(t2('bt.import.jobs')),
          jobs.length === 0
            ? empty(t2('bt.import.jobs.empty'))
            : h('div', {class: 'bt-jobs'}, jobs.slice(0, 6).map((job) => h('div', {class: 'bt-job'},
                h('div', {class: 'bt-track__title'}, job.title || job.url),
                job.state === 'running'
                  ? h('div', {class: 'bt-progress'},
                      h('div', {class: 'bt-progress__track'},
                        h('div', {class: 'bt-progress__fill',
                          style: 'width: ' + Math.round((job.progress || 0) * 100) + '%'})),
                      h('span', null, Math.round((job.progress || 0) * 100) + '%'))
                  : h('div', {class: 'bt-track__sub'}, job.message || job.state),
              ))),
        ),
      ];
    }

    // ------------------------------------------------------------------ agenda

    const DAY_LABELS = () => t2('bt.schedule.days.short').split(' ');

    function windowRow(sched, windows, index) {
      const w = windows[index];
      const days = Array.isArray(w.days) ? w.days : [0, 1, 2, 3, 4, 5, 6];
      const patch = (changes) => {
        const next = windows.slice();
        next[index] = Object.assign({}, w, changes);
        saveSchedule(Object.assign({}, sched, {windows: next}));
      };
      return h('div', {class: 'bt-window'},
        h('div', {class: 'bt-toolbar'},
          h('input', {class: 'input', type: 'text', value: w.name || '',
            placeholder: t2('bt.schedule.window.name'),
            onChange: (event) => patch({name: event.target.value})}),
          h('input', {class: 'input input--sm', type: 'time', value: w.start || '08:00',
            title: t2('bt.schedule.window.from'),
            onChange: (event) => patch({start: event.target.value})}),
          h('input', {class: 'input input--sm', type: 'time', value: w.end || '09:00',
            title: t2('bt.schedule.window.to'),
            onChange: (event) => patch({end: event.target.value})}),
          h('label', {class: 'row row--tight'},
            h('input', {type: 'checkbox', checked: w.enabled !== false,
              onChange: (event) => patch({enabled: event.target.checked})}),
            // Diz o que a janela *é*, não o que o clique faria.
            h('span', {class: 'small muted'},
              w.enabled !== false ? t2('bt.schedule.window.on') : t2('bt.schedule.window.off')),
          ),
          iconBtn('trash', t2('bt.schedule.remove'), () => saveSchedule(
            Object.assign({}, sched, {windows: windows.filter((_, i) => i !== index)}))),
        ),
        h('div', {class: 'bt-days'}, DAY_LABELS().map((label, day) => h('button', {
          class: 'bt-day', type: 'button', dataset: {on: days.indexOf(day) === -1 ? 'false' : 'true'},
          title: t2('bt.schedule.window.days'),
          onClick: () => patch({
            days: days.indexOf(day) === -1
              ? days.concat([day]).sort((a, b) => a - b)
              : days.filter((d) => d !== day),
          }),
        }, label))),
        h('label', {class: 'field'},
          h('span', {class: 'field__label'}, t2('bt.schedule.window.playlist')),
          h('select', {class: 'input',
            onChange: (event) => patch({playlist_id: event.target.value})},
            (state.playlists || []).map((pl) => h('option', {
              value: pl.id, selected: pl.id === (w.playlist_id || 'all'),
            }, pl.name))),
        ),
      );
    }

    // O aviso que faltava: a agenda toca sozinha, então o único momento útil de
    // dizer que ela não vai conseguir é antes da hora. O servidor confere agora
    // (tem caixa de som escolhida? tem faixa que dê para tocar nela?) e conta o
    // que aconteceu da última vez que um horário chegou.
    function scheduleWarnings() {
      const info = state.schedule || {};
      const blocked = info.blocked;
      const last = info.last_attempt;
      const avisos = [];
      if (blocked) {
        avisos.push(h('div', {class: 'bt-warn'},
          icon('warning', {size: 16}),
          h('div', {class: 'grow'},
            h('div', {class: 'bt-warn__title'}, t2('bt.schedule.blocked')),
            h('div', {class: 'small muted'}, blocked.message || ''),
          ),
          blocked.code === 'no_output'
            ? h('button', {
                class: 'btn btn--sm',
                onClick: () => { state.outputOpen = true; renderSheet(); },
              }, t2('bt.schedule.pick_output'))
            : null,
        ));
      }
      if (last && !last.ok && (!blocked || blocked.code !== last.code)) {
        avisos.push(h('div', {class: 'bt-warn'},
          icon('warning', {size: 16}),
          h('div', {class: 'grow'},
            h('div', {class: 'bt-warn__title'}, t2('bt.schedule.last_failed')),
            h('div', {class: 'small muted'}, last.message || ''),
          ),
        ));
      }
      return avisos.length ? h('div', {class: 'stack stack--sm'}, avisos) : null;
    }

    function scheduleView() {
      const sched = (state.schedule && state.schedule.schedule) || {};
      const quiet = sched.quiet_hours || {};
      const windows = sched.windows || [];
      return [
        scheduleWarnings(),
        h('div', {class: 'bt-section'},
          sectionHead(t2('bt.section.schedule'), t2('bt.schedule.lead'),
            h('label', {class: 'row row--tight'},
              h('input', {type: 'checkbox', checked: sched.enabled !== false,
                onChange: (event) => saveSchedule(
                  Object.assign({}, sched, {enabled: event.target.checked}))}),
              h('span', {class: 'small muted'}, t2('bt.schedule.enabled')),
            )),
          windows.length === 0
            ? empty(t2('bt.schedule.windows.empty'))
            : h('div', {class: 'bt-jobs'}, windows.map((_, index) => windowRow(sched, windows, index))),
          h('div', {class: 'bt-toolbar'},
            h('button', {class: 'btn btn--outline btn--sm', onClick: () => saveSchedule(
              Object.assign({}, sched, {windows: windows.concat([{
                id: 'window-' + (windows.length + 1),
                name: '', start: '08:00', end: '09:00',
                days: [0, 1, 2, 3, 4, 5, 6], playlist_id: 'all', enabled: true,
              }])}))}, icon('plus', {size: 14}), ' ', t2('bt.schedule.add')),
            (state.presets || []).map((preset) => h('button', {
              class: 'btn btn--ghost btn--sm', onClick: () => {
                // Substitui a janela de mesmo id em vez de empilhar uma segunda
                // idêntica em cima.
                const win = {
                  id: preset.id, name: preset.name, start: preset.start, end: preset.end,
                  days: preset.days, playlist_id: 'all', enabled: true,
                };
                const kept = windows.filter((w) => w.id !== preset.id);
                saveSchedule(Object.assign({}, sched, {windows: kept.concat([win])}));
              },
            }, preset.name)),
          ),
        ),
        h('div', {class: 'bt-section'},
          sectionHead(t2('bt.schedule.quiet')),
          h('div', {class: 'bt-toolbar'},
            h('label', {class: 'field'},
              h('span', {class: 'field__label'}, t2('bt.schedule.quiet_start')),
              h('input', {class: 'input input--sm', type: 'time', value: quiet.start || '20:00',
                onChange: (event) => saveSchedule(Object.assign({}, sched, {
                  quiet_hours: Object.assign({}, quiet, {start: event.target.value})}))}),
            ),
            h('label', {class: 'field'},
              h('span', {class: 'field__label'}, t2('bt.schedule.quiet_end')),
              h('input', {class: 'input input--sm', type: 'time', value: quiet.end || '07:00',
                onChange: (event) => saveSchedule(Object.assign({}, sched, {
                  quiet_hours: Object.assign({}, quiet, {end: event.target.value})}))}),
            ),
          ),
          h('p', {class: 'field__hint'}, t2('bt.schedule.quiet.hint')),
        ),
      ];
    }

    async function saveSchedule(next) {
      try {
        state.schedule = await appApi.put('/schedule', next);
        toast(t2('bt.save.ok'), {type: 'success'});
      } catch (err) {
        toast(t2('bt.save.failed') + ': ' + (err.message || err), {type: 'error'});
      }
      render();
    }

    // ------------------------------------------------------------------- saída

    function deviceName() {
      const status = state.status || {};
      if (status.device) return status.device;
      const outputs = state.outputs || {};
      const chosen = (config && config.get && config.get('output.device_id', '')) || '';
      const device = (outputs.devices || []).filter((d) => d.id === chosen)[0];
      if (device) return device.name;
      return t2('bt.output.silent');
    }

    function renderSheet() {
      if (!state.outputOpen) {
        sheet.hidden = true;
        sheet.replaceChildren();
        return;
      }
      sheet.hidden = false;
      const outputs = state.outputs || {};
      const backends = outputs.backends || [];
      const devices = outputs.devices || [];
      const status = state.status || {};
      const currentBackend = status.output || 'null';
      const currentDevice = (config && config.get && config.get('output.device_id', '')) || '';
      // Só os aparelhos que a conexão escolhida sabe tocar: oferecer um
      // Chromecast ao AirPlay produz um erro que ninguém em casa resolve.
      const backend = backends.filter((b) => b.kind === currentBackend)[0];
      const kinds = (backend && backend.device_kinds) || [];
      const usable = kinds.length ? devices.filter((d) => kinds.indexOf(d.kind) !== -1) : devices;
      let pinRef = null;

      // replaceChildren não é o h(): um `null` na lista vira o *texto* "null"
      // na tela. Era o que aparecia embaixo do seletor de conexão.
      sheet.replaceChildren(...[
        sectionHead(t2('bt.section.output'), '',
          iconBtn('x', t2('bt.stop'), () => { state.outputOpen = false; renderSheet(); })),
        h('label', {class: 'field'},
          h('span', {class: 'field__label'}, t2('bt.output.backend')),
          h('select', {class: 'input',
            onChange: (event) => act(() => appApi.put('/output', {type: event.target.value, device_id: ''})),
          }, backends.map((b) => h('option', {
            value: b.kind, selected: b.kind === currentBackend, disabled: !b.available,
          }, b.name + (b.available ? '' : ' — ' + (b.hint || 'indisponível'))))),
        ),
        currentBackend === 'null' ? null : h('label', {class: 'field'},
          h('span', {class: 'field__label'}, t2('bt.output.device')),
          usable.length === 0
            ? h('p', {class: 'field__hint'}, t2('bt.output.empty'))
            : h('select', {class: 'input',
                onChange: (event) => act(() => appApi.put('/output', {
                  type: currentBackend, device_id: event.target.value,
                })),
              }, [h('option', {value: '', selected: !currentDevice}, t2('bt.output.none'))].concat(
                usable.map((d) => h('option', {value: d.id, selected: d.id === currentDevice},
                  d.name + (d.online ? '' : ' (offline)'))))),
        ),
        // Uma Apple TV recusa áudio até ser pareada, e o PIN aparece na própria TV.
        currentBackend === 'airplay' && currentDevice ? h('div', {class: 'bt-toolbar'},
          h('button', {class: 'btn btn--outline btn--sm', onClick: async () => {
            try {
              state.pairing = await appApi.post('/outputs/' + encodeURIComponent(currentDevice) + '/pair', {});
              toast(state.pairing.message || '', {type: 'info'});
            } catch (err) {
              toast(String(err.message || err), {type: 'error'});
            }
            renderSheet();
          }}, t2('bt.output.pair')),
          state.pairing ? h('input', {class: 'input input--sm', type: 'text', inputmode: 'numeric',
            placeholder: t2('bt.output.pin'), ref: (el) => { pinRef = el; }}) : null,
          state.pairing ? h('button', {class: 'btn btn--primary btn--sm', onClick: () => {
            const pin = pinRef && pinRef.value ? pinRef.value.trim() : '';
            if (!pin) return;
            act(async () => {
              await appApi.post('/outputs/' + encodeURIComponent(currentDevice) + '/pair', {pin});
              state.pairing = null;
              toast(t2('bt.output.paired'), {type: 'success'});
            });
          }}, t2('bt.output.pin.send')) : null,
        ) : null,
      ].filter(Boolean));
    }

    // -------------------------------------------------------------------- barra

    function renderPlayer() {
      const status = state.status || {};
      const track = status.track || null;
      const playing = status.state === 'playing';
      const volume = Math.round((status.volume || 0) * 100);

      playerBar.replaceChildren(
        h('div', {class: 'bt-player__now'},
          art(track || {title: 'BirdTunes'}, 'md'),
          h('div', {class: 'bt-player__text'},
            h('div', {class: 'bt-player__title'},
              track ? (track.title || track.path) : t2('bt.nothing_playing')),
            h('div', {class: 'bt-player__sub'}, track
              ? [track.artist || '', status.position && status.duration
                  ? clock(status.position) + ' / ' + clock(status.duration) : ''].filter(Boolean).join(' · ')
              : t2('bt.state.' + (status.state || 'idle'))),
          ),
        ),
        h('div', {class: 'bt-player__controls'},
          h('button', {
            class: 'bt-play', type: 'button', disabled: state.busy,
            title: playing ? t2('bt.pause') : t2('bt.play'),
            'aria-label': playing ? t2('bt.pause') : t2('bt.play'),
            onClick: () => act(() => appApi.post(
              playing ? '/pause' : (status.state === 'paused' ? '/resume' : '/play'), {})),
          }, icon(playing ? 'pause' : 'play', {size: 20})),
          iconBtn('next', t2('bt.next'), () => act(() => appApi.post('/next', {})),
            {disabled: state.busy}),
          iconBtn('stop', t2('bt.stop'), () => act(() => appApi.post('/stop', {})),
            {disabled: state.busy}),
        ),
        h('div', {class: 'bt-player__side'},
          h('div', {class: 'bt-volume'},
            icon('volume', {size: 16}),
            h('input', {type: 'range', min: '0', max: '100', step: '5', value: String(volume),
              title: t2('bt.output.volume') + ' ' + volume + '%',
              onChange: (event) => act(() => appApi.post('/volume',
                {value: Number(event.target.value) / 100})),
            }),
          ),
          h('button', {class: 'bt-chip', type: 'button',
            dataset: {live: status.connected ? 'true' : 'false'},
            title: t2('bt.section.output'),
            onClick: () => {
              state.outputOpen = !state.outputOpen;
              renderSheet();
              // O painel abre acima da barra: numa tela longa ele nasceria fora
              // do campo de visão e o clique pareceria não ter feito nada.
              if (state.outputOpen) sheet.scrollIntoView({block: 'nearest', behavior: 'smooth'});
            }},
            icon('cast', {size: 14}),
            h('span', {class: 'bt-chip__text'}, deviceName()),
          ),
        ),
      );
    }

    // ----------------------------------------------------------------- navegação

    const TABS = [
      {id: 'home', icon: 'home', label: 'bt.nav.home'},
      {id: 'library', icon: 'music', label: 'bt.nav.library'},
      {id: 'playlists', icon: 'menu', label: 'bt.nav.playlists'},
      {id: 'add', icon: 'plus', label: 'bt.nav.add'},
      {id: 'schedule', icon: 'clock', label: 'bt.nav.schedule'},
    ];

    function renderNav() {
      nav.replaceChildren(...TABS.map((tab) => h('button', {
        class: 'bt-nav__item', type: 'button', dataset: {active: state.view === tab.id ? 'true' : 'false'},
        onClick: () => go(tab.id),
      }, icon(tab.icon, {size: 15}), t2(tab.label))));
    }

    async function go(view) {
      if (view === 'playlists' && state.view === 'playlists') state.playlistId = '';
      state.view = view;
      if (view !== 'playlists') { state.playlistId = ''; state.playlist = null; }
      renderNav();
      main.replaceChildren(h('div', {class: 'card'},
        h('div', {class: 'card__body'}, h('div', {class: 'skeleton skeleton--text'}))));
      await loadForView();
      render();
    }

    function render() {
      renderNav();
      listHost = null;
      const nodes =
        state.view === 'library' ? libraryView()
          : state.view === 'playlists' ? playlistsView()
          : state.view === 'add' ? addView()
          : state.view === 'schedule' ? scheduleView()
          : homeView();
      main.replaceChildren(...nodes.filter(Boolean));
      renderPlayer();
      renderSheet();
    }

    // -------------------------------------------------------------------- vida

    await loadCommon();
    await loadForView();
    render();

    let cancelled = false;
    // A barra é o que muda sozinho; o resto só muda quando alguém mexe. Menos
    // pedidos que a tela antiga, que recarregava as sete seções a cada quinze
    // segundos.
    const poll = setInterval(async () => {
      if (cancelled) return;
      const status = await safeGet('/status');
      if (cancelled || !status) return;
      state.status = status;
      renderPlayer();
      if (state.view === 'home') {
        const queue = await safeGet('/queue');
        state.queue = (queue && queue.queue) || [];
        if (!cancelled) render();
      }
    }, 8000);

    // O app publica o que acontece; ouvir é mais barato e mais rápido que
    // perguntar de novo.
    const offState = ctx.ws && ctx.ws.on
      ? ctx.ws.on('app.birdtunes.state', async () => {
          state.status = await safeGet('/status');
          if (cancelled) return;
          // O Início mostra a mesma faixa que a barra, em letra grande: atualizar
          // só a barra deixava as duas anunciando músicas diferentes na mesma tela.
          if (state.view === 'home') render();
          else renderPlayer();
        })
      : null;
    const offLibrary = ctx.ws && ctx.ws.on
      ? ctx.ws.on('app.birdtunes.library', async () => {
          if (cancelled) return;
          await loadCommon();
          await loadForView();
          if (!cancelled) render();
        })
      : null;

    return () => {
      cancelled = true;
      clearInterval(poll);
      if (searchTimer) clearTimeout(searchTimer);
      if (typeof offState === 'function') offState();
      if (typeof offLibrary === 'function') offLibrary();
    };
  },
};
