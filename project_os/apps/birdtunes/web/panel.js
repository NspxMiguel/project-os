// panel.js -- control panel for the birdtunes app.
//
// Plain ES module, no build step, no framework: `h()` from project-os's own
// dom.js, reuses web/style.css classes only. Talks to the routes defined in
// ../app.py's build_router(): status/play/pause/stop, playlists CRUD,
// /import/youtube + /import/preview, /schedule, /compat.
//
// Sections, per docs/BIRDTUNES.md section 8: Playlists, Import from YouTube
// (with an ffmpeg-missing warning), Schedule editor, Compatibility banner.

export default {
  async mount(root, ctx) {
    const {h, t, appApi, config, toast} = ctx;
    const fmtMod = await import('/lib/format.js');
    fmtMod.setStrings('en', {
      'bt.title': 'BirdTunes',
      'bt.now_playing': 'Now playing',
      'bt.nothing_playing': 'Nothing playing',
      'bt.play': 'Play',
      'bt.pause': 'Pause',
      'bt.resume': 'Resume',
      'bt.stop': 'Stop',
      'bt.next': 'Next',
      'bt.quiet_hours': 'Quiet hours -- BirdTunes will not play right now.',
      'bt.next_change': 'Next',
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
      'bt.section.library': 'Library',
      'bt.library.empty': 'No tracks yet. Import from YouTube below, or drop files in the media folder and scan.',
      'bt.library.scan': 'Scan for files',
      'bt.library.scanned': 'Scan done: %d added, %d already known.',
      'bt.library.like': 'Like',
      'bt.library.less': 'Recommend less',
      'bt.library.block': 'Never play',
      'bt.library.unblock': 'Allow again',
      'bt.library.play': 'Play this',
      'bt.library.liked': 'Liked',
      'bt.library.lessened': 'It will come up less often.',
      'bt.library.blocked': 'Blocked',
      'bt.library.missing': 'file missing',
      'bt.section.compat': 'Compatibility',
      'bt.compat.ok': 'Todas as faixas funcionam na saída atual.',
      'bt.compat.warn': '%d of %d tracks cannot be played on this output.',
      'bt.compat.convert': 'Convert to MP3',
      'bt.compat.no_ffmpeg': 'Install ffmpeg to convert incompatible tracks.',
      'bt.section.playlists': 'Playlists',
      'bt.playlists.empty': 'No playlists yet.',
      'bt.playlists.new': 'New playlist',
      'bt.playlists.name': 'Playlist name',
      'bt.playlists.create': 'Create',
      'bt.playlists.play': 'Play now',
      'bt.playlists.delete': 'Delete',
      'bt.playlists.tracks': 'tracks',
      'bt.playlists.track': 'track',
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
      'bt.import.jobs.empty': 'No imports yet.',
      'bt.import.cast': 'Play on the TV (no download)',
      'bt.import.cast.hint': 'Plays through the television’s own YouTube app, so YouTube shows its own ads. To listen without ads, use Add instead: the audio is kept here and BirdTunes plays it.',
      'bt.import.dest': 'Add to',
      'bt.import.dest.library': 'Library only',
      'bt.import.dest.new': 'New playlist from this link',
      'bt.import.queued': 'Downloading. It lands in the library when it finishes.',
      'bt.library.add.url': 'YouTube link',
      'bt.library.add': 'Add',
      'bt.playlists.add.youtube': 'Add from YouTube',
      'bt.import.cast.ok': 'Playing on %s.',
      'bt.playlists.add': 'Add to playlist',
      'bt.playlists.added': 'Added to %s.',
      'bt.section.schedule': 'Schedule',
      'bt.schedule.enabled': 'Schedule enabled',
      'bt.schedule.quiet_start': 'Quiet hours start',
      'bt.schedule.quiet_end': 'Quiet hours end',
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
      'bt.schedule.save': 'Save schedule',
      'bt.save.ok': 'Saved',
      'bt.save.failed': 'Could not save',
      'bt.action.failed': 'Action failed',
    }, {activate: false});
    const t2 = (key, ...args) => {
      const value = fmtMod.t(key, args.length ? {n: args[0], total: args[1]} : undefined);
      return value === key ? t(key) : value;
    };
    const fmtStr = (key, ...args) => {
      const raw = fmtMod.strings ? key : key; // strings resolved via t(); fall back to manual sprintf
      let text = fmtMod.t(key);
      if (text === key) text = key;
      args.forEach((a) => { text = text.replace(/%d|%s/, String(a)); });
      return text;
    };

    const state = {
      importDest: '',   // '' = library only, 'new' = fresh playlist, else a playlist id
      status: null,
      playlists: [],
      compat: null,
      schedule: null,
      presets: [],
      importJobs: [],
      preview: null,
      busy: false,
      outputs: null,     // {backends, devices}
      library: [],
      pairing: null,     // {device_id, needs_pin, message} while pairing runs
    };

    const view = h('div', {class: 'stack'});
    root.appendChild(view);

    async function safeGet(path, query) {
      try {
        return await appApi.get(path, query ? {query} : undefined);
      } catch (err) {
        return null;
      }
    }

    async function refreshAll() {
      const [status, playlistsRes, compat, schedule, presetsRes, importsRes, outputs, libraryRes] =
        await Promise.all([
          safeGet('/status'),
          safeGet('/playlists'),
          safeGet('/compat'),
          safeGet('/schedule'),
          safeGet('/schedule/presets'),
          safeGet('/import'),
          safeGet('/outputs'),
          safeGet('/library'),
        ]);
      // The chosen speaker lives in the app's config, not in /status: it is set
      // even when nothing is connected yet.
      try { await config.reload(); } catch (err) { /* panel still renders */ }
      state.outputs = outputs;
      state.library = (libraryRes && libraryRes.tracks) || [];
      state.status = status;
      state.playlists = (playlistsRes && playlistsRes.playlists) || [];
      state.compat = compat;
      state.schedule = schedule;
      state.presets = (presetsRes && presetsRes.presets) || [];
      state.importJobs = (importsRes && importsRes.jobs) || [];
    }

    async function act(fn) {
      state.busy = true;
      render();
      try {
        await fn();
      } catch (err) {
        toast(t2('bt.action.failed') + ': ' + (err.message || err), {type: 'error'});
      } finally {
        state.busy = false;
        await refreshAll();
        render();
      }
    }

    // 300 is not a duration a person reads; 5:00 is.
    function clock(seconds) {
      const total = Math.round(Number(seconds) || 0);
      const mins = Math.floor(total / 60);
      const secs = total % 60;
      return mins + ':' + (secs < 10 ? '0' : '') + secs;
    }

    function transportCard() {
      const status = state.status || {};
      const track = status.track;
      const quiet = Boolean(status.quiet_hours_active);
      const nextChange = status.next_change || {};
      return h('div', {class: 'card'},
        h('div', {class: 'card__header'},
          h('h3', {class: 'card__title'}, t2('bt.title')),
          h('div', {class: 'card__tools'},
            // Idle is not a failure: a red dot next to "idle" reads as "this app
            // is broken". Only a real error earns the danger colour.
            h('span', {class: 'conn', dataset: {state:
              status.state === 'playing' ? 'open'
                : status.state === 'connecting' ? 'connecting'
                : status.error ? 'closed' : 'neutral'}},
              h('span', {class: 'conn__dot'}),
              t2('bt.state.' + (status.state || 'idle'))),
          ),
        ),
        h('div', {class: 'card__body stack'},
          quiet ? h('div', {class: 'notice notice--warning'}, t2('bt.quiet_hours')) : null,
          h('div', {class: 'list__row'},
            h('div', {class: 'list__main'},
              h('div', {class: 'list__title'}, track ? track.title || track.path : t2('bt.nothing_playing')),
              track ? h('div', {class: 'list__sub'}, track.artist || '') : null,
            ),
          ),
          h('p', {class: 'field__hint'}, t2('bt.next_change') + ': ' + (nextChange.message || '')),
          h('div', {class: 'input-group'},
            h('button', {class: 'btn btn--primary', disabled: state.busy,
              onClick: () => act(() => appApi.post('/play', {}))}, t2('bt.play')),
            h('button', {class: 'btn btn--outline', disabled: state.busy,
              onClick: () => act(() => appApi.post(status.state === 'paused' ? '/resume' : '/pause', {}))},
              status.state === 'paused' ? t2('bt.resume') : t2('bt.pause')),
            h('button', {class: 'btn btn--outline', disabled: state.busy,
              onClick: () => act(() => appApi.post('/stop', {}))}, t2('bt.stop')),
            h('button', {class: 'btn btn--outline', disabled: state.busy,
              onClick: () => act(() => appApi.post('/next', {}))}, t2('bt.next')),
          ),
        ),
      );
    }

    // Only when something is wrong. A card that permanently says "all fine" is
    // one more thing to read past every time you open the app.
    function compatCard() {
      const compat = state.compat;
      if (!compat) return null;
      const bad = compat.incompatible || 0;
      if (bad === 0) return null;
      return h('div', {class: 'card'},
        h('div', {class: 'card__header'}, h('h3', {class: 'card__title'}, t2('bt.section.compat'))),
        h('div', {class: 'card__body stack'},
          h('div', {class: 'notice notice--warning'}, fmtStr('bt.compat.warn', bad, compat.total)),
          bad > 0 && compat.ffmpeg_available
            ? h('button', {class: 'btn btn--outline btn--sm', onClick: () => act(() => appApi.post('/convert', {track_ids: compat.incompatible_tracks}))},
                t2('bt.compat.convert'))
            : null,
          bad > 0 && !compat.ffmpeg_available
            ? h('p', {class: 'field__hint'}, t2('bt.compat.no_ffmpeg'))
            : null,
        ),
      );
    }

    // Where it plays. The whole app is "conecta no meu apple tv ... ou
    // chromecast (configuravel)", so this is not a settings detail -- it is the
    // first thing you set, and until it is set the transport plays to nothing.
    function outputCard() {
      const outputs = state.outputs || {};
      const backends = outputs.backends || [];
      const devices = outputs.devices || [];
      const status = state.status || {};
      const currentBackend = status.output || 'null';
      const currentDevice = (config && config.get && config.get('output.device_id', '')) || '';

      // Only devices the chosen backend can actually drive: offering a
      // Chromecast to the AirPlay backend produces a failure the person cannot
      // do anything about.
      const backend = backends.filter((b) => b.kind === currentBackend)[0];
      const kinds = (backend && backend.device_kinds) || [];
      const usable = kinds.length ? devices.filter((d) => kinds.indexOf(d.kind) !== -1) : devices;
      let pinRef = null;

      return h('div', {class: 'card'},
        h('div', {class: 'card__header'}, h('h3', {class: 'card__title'}, t2('bt.section.output'))),
        h('div', {class: 'card__body stack'},
          h('div', {class: 'field'},
            h('label', {class: 'field__label'}, t2('bt.output.backend')),
            h('select', {
              onChange: (event) => act(() => appApi.put('/output', {type: event.target.value, device_id: ''})),
            }, backends.map((b) => h('option', {
              value: b.kind, selected: b.kind === currentBackend, disabled: !b.available,
            }, b.name + (b.available ? '' : ' — ' + (b.hint || 'unavailable'))))),
          ),
          currentBackend === 'null' ? null : h('div', {class: 'field'},
            h('label', {class: 'field__label'}, t2('bt.output.device')),
            usable.length === 0
              ? h('p', {class: 'field__hint'}, t2('bt.output.empty'))
              : h('select', {
                  onChange: (event) => act(() => appApi.put('/output', {
                    type: currentBackend, device_id: event.target.value,
                  })),
                },
                [h('option', {value: '', selected: !currentDevice}, t2('bt.output.none'))].concat(
                  usable.map((d) => h('option', {value: d.id, selected: d.id === currentDevice},
                    d.name + (d.online ? '' : ' (offline)'))),
                )),
          ),
          // Pairing exists because an Apple TV refuses audio until it has been
          // paired, and the PIN is shown on the TV itself.
          currentBackend === 'airplay' && currentDevice ? h('div', {class: 'input-group'},
            h('button', {class: 'btn btn--outline btn--sm', onClick: async () => {
              try {
                state.pairing = await appApi.post('/outputs/' + encodeURIComponent(currentDevice) + '/pair', {});
                toast(state.pairing.message || '', {type: 'info'});
              } catch (err) {
                toast(String(err.message || err), {type: 'error'});
              }
              render();
            }}, t2('bt.output.pair')),
            state.pairing ? h('input', {type: 'text', inputmode: 'numeric',
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
          h('div', {class: 'field'},
            h('label', {class: 'field__label'},
              t2('bt.output.volume') + ' — ' + Math.round((status.volume || 0) * 100) + '%'),
            h('input', {
              type: 'range', min: '0', max: '100', step: '5',
              value: String(Math.round((status.volume || 0) * 100)),
              onChange: (event) => act(() => appApi.post('/volume', {value: Number(event.target.value) / 100})),
            }),
          ),
        ),
      );
    }

    // "coloca pra adicionar musicas direto do youtube em library playlist e etc tbm"
    // One path for every entry point: the library row, the playlist row and the
    // import card all end up here, so "add" means the same thing everywhere.
    function addFromYouTube(url, dest) {
      const target = dest === undefined ? state.importDest : dest;
      const body = {url: url};
      if (target === 'new') body.as_playlist = true;
      else if (target) body.playlist_id = target;
      else body.as_playlist = Boolean(state.preview && state.preview.kind === 'playlist');
      return act(async () => {
        await appApi.post('/import/youtube', body);
        toast(t2('bt.import.queued'), {type: 'success'});
      });
    }

    // "botao de curtir musicas, recomendar menos e etc" -- the buttons live on
    // the track, next to the track, because that is the only place the judgement
    // makes sense.
    function libraryCard() {
      let libraryUrlRef = null;
      const tracks = state.library || [];
      return h('div', {class: 'card'},
        h('div', {class: 'card__header'},
          h('h3', {class: 'card__title'}, t2('bt.section.library')),
          h('div', {class: 'card__tools'},
            h('button', {class: 'btn btn--ghost btn--sm', onClick: () => act(async () => {
              const result = await appApi.post('/library/scan', {});
              toast(fmtStr('bt.library.scanned', result.added || 0, result.updated || 0), {type: 'success'});
            })}, t2('bt.library.scan')),
          ),
        ),
        h('div', {class: 'card__body stack'},
          tracks.length === 0
            ? h('div', {class: 'empty empty--sm'}, h('p', {class: 'empty__text'}, t2('bt.library.empty')))
            : h('div', {class: 'list'}, tracks.map((track) => {
                const fb = track.feedback || {};
                const blocked = Boolean(fb.blocked);
                return h('div', {class: 'list__row'},
                  h('div', {class: 'list__main'},
                    h('div', {class: 'list__title'}, track.title || track.path),
                    h('div', {class: 'list__sub'}, [
                      track.artist || '',
                      track.duration ? clock(track.duration) : '',
                      fb.likes ? '♥ ' + fb.likes : '',
                      fb.less ? t2('bt.library.lessened') : '',
                      blocked ? t2('bt.library.blocked') : '',
                      track.missing ? t2('bt.library.missing') : '',
                    ].filter(Boolean).join(' · ')),
                  ),
                  h('div', {class: 'list__aside'},
                    h('button', {class: 'btn btn--icon btn--sm', title: t2('bt.library.play'),
                      onClick: () => act(() => appApi.post('/play', {track_id: track.id}))},
                      ctx.icon('play', {size: 16})),
                    h('button', {class: 'btn btn--icon btn--sm', title: t2('bt.library.like'),
                      onClick: () => act(() => appApi.post('/tracks/' + track.id + '/feedback', {action: 'like'}))},
                      ctx.icon('heart', {size: 16})),
                    h('button', {class: 'btn btn--icon btn--sm', title: t2('bt.library.less'),
                      onClick: () => act(() => appApi.post('/tracks/' + track.id + '/feedback', {action: 'less'}))},
                      ctx.icon('thumbs-down', {size: 16})),
                    state.playlists.filter((pl) => !pl.virtual).length
                      ? h('select', {
                          class: 'input input--sm select--compact',
                          title: t2('bt.playlists.add'),
                          onChange: (event) => {
                            const target = event.target.value;
                            event.target.selectedIndex = 0;
                            if (!target) return;
                            const playlist = state.playlists.filter((pl) => pl.id === target)[0];
                            act(async () => {
                              await appApi.post('/playlists/' + target + '/tracks', {track_ids: [track.id]});
                              toast(fmtStr('bt.playlists.added', (playlist && playlist.name) || ''), {type: 'success'});
                            });
                          },
                        },
                        [h('option', {value: ''}, '+')].concat(
                          state.playlists.filter((pl) => !pl.virtual).map(
                            (pl) => h('option', {value: pl.id}, pl.name)),
                        ))
                      : null,
                    h('button', {class: 'btn btn--icon btn--sm',
                      title: blocked ? t2('bt.library.unblock') : t2('bt.library.block'),
                      onClick: () => act(() => appApi.post('/tracks/' + track.id + '/feedback',
                        {action: blocked ? 'unblock' : 'block'}))},
                      ctx.icon(blocked ? 'check' : 'x', {size: 16})),
                  ),
                );
              })),
          h('div', {class: 'input-group'},
            h('input', {type: 'text', placeholder: t2('bt.library.add.url'),
              ref: (el) => { libraryUrlRef = el; }}),
            h('button', {class: 'btn btn--outline', onClick: () => {
              const url = libraryUrlRef && libraryUrlRef.value ? libraryUrlRef.value.trim() : '';
              if (!url) return;
              if (libraryUrlRef) libraryUrlRef.value = '';
              addFromYouTube(url, '');
            }}, t2('bt.library.add')),
          ),
        ),
      );
    }

    function playlistsCard() {
      let nameRef = null;
      const list = state.playlists || [];
      return h('div', {class: 'card'},
        h('div', {class: 'card__header'}, h('h3', {class: 'card__title'}, t2('bt.section.playlists'))),
        h('div', {class: 'card__body stack'},
          list.length === 0
            ? h('div', {class: 'empty empty--sm'}, h('p', {class: 'empty__text'}, t2('bt.playlists.empty')))
            : h('div', {class: 'list'}, list.map((pl) => h('div', {class: 'list__row'},
                h('div', {class: 'list__main'},
                  h('div', {class: 'list__title'}, pl.name),
                  h('div', {class: 'list__sub'}, (pl.track_count || 0) + ' ' +
                    t2(pl.track_count === 1 ? 'bt.playlists.track' : 'bt.playlists.tracks')),
                ),
                h('div', {class: 'list__aside'},
                  h('button', {class: 'btn btn--ghost btn--sm',
                    onClick: () => act(() => appApi.post('/playlists/' + pl.id + '/play', {}))}, t2('bt.playlists.play')),
                  pl.virtual ? null : h('button', {class: 'btn btn--ghost btn--sm',
                    title: t2('bt.playlists.add.youtube'),
                    onClick: () => {
                      // Sends you to the one input that already exists, aimed at
                      // this playlist -- rather than a second field per row.
                      state.importDest = pl.id;
                      render();
                      const field = root.querySelector('[data-import-url]');
                      if (field) { field.scrollIntoView({block: 'center'}); field.focus(); }
                    }}, t2('bt.playlists.add.youtube')),
                  pl.virtual ? null : h('button', {class: 'btn btn--icon btn--sm', title: t2('bt.playlists.delete'),
                    onClick: () => act(() => appApi.delete('/playlists/' + pl.id))}, ctx.icon('trash', {size: 16})),
                ),
              ))),
          h('div', {class: 'input-group'},
            h('input', {type: 'text', placeholder: t2('bt.playlists.name'), ref: (el) => { nameRef = el; }}),
            h('button', {class: 'btn btn--outline', onClick: () => {
              const name = nameRef && nameRef.value ? nameRef.value.trim() : '';
              if (!name) return;
              if (nameRef) nameRef.value = '';
              act(() => appApi.post('/playlists', {name}));
            }}, t2('bt.playlists.create')),
          ),
        ),
      );
    }

    function importCard() {
      let urlRef = null;
      const jobs = state.importJobs || [];
      return h('div', {class: 'card'},
        h('div', {class: 'card__header'}, h('h3', {class: 'card__title'}, t2('bt.section.import'))),
        h('div', {class: 'card__body stack'},
          h('div', {class: 'input-group'},
            h('input', {type: 'text', placeholder: t2('bt.import.url'), 'data-import-url': '1',
              ref: (el) => { urlRef = el; }}),
            h('button', {class: 'btn btn--outline', onClick: async () => {
              const url = urlRef && urlRef.value ? urlRef.value.trim() : '';
              if (!url) return;
              try {
                state.preview = await appApi.get('/import/preview', {query: {url}});
              } catch (err) {
                toast(String(err.message || err), {type: 'error'});
                state.preview = null;
              }
              render();
            }}, t2('bt.import.preview')),
            h('button', {class: 'btn btn--primary', onClick: () => {
              const url = urlRef && urlRef.value ? urlRef.value.trim() : '';
              if (!url) return;
              addFromYouTube(url);
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
          // Two different things, side by side on purpose: Import keeps a copy
          // on the Pi; this one just tells the television to play it.
          h('div', {class: 'input-group'},
            h('button', {class: 'btn btn--outline', onClick: () => {
              const url = urlRef && urlRef.value ? urlRef.value.trim() : '';
              if (!url) return;
              act(async () => {
                const result = await appApi.post('/play/youtube', {url});
                toast(fmtStr('bt.import.cast.ok', result.device || ''), {type: 'success'});
              });
            }}, t2('bt.import.cast')),
          ),
          h('p', {class: 'field__hint'}, t2('bt.import.cast.hint')),
          state.preview
            ? h('p', {class: 'field__hint'}, fmtStr('bt.import.found', state.preview.title, state.preview.count))
            : null,
          // The last few only. A full history of every link ever pasted is not
          // something anyone reads, and it buried the controls above it.
          jobs.length === 0
            ? h('div', {class: 'empty empty--sm'}, h('p', {class: 'empty__text'}, t2('bt.import.jobs.empty')))
            : h('div', {class: 'list'}, jobs.slice(0, 4).map((job) => h('div', {class: 'list__row'},
                h('div', {class: 'list__main'},
                  h('div', {class: 'list__title'}, job.title || job.url),
                  h('div', {class: 'list__sub'},
                    job.state === 'running'
                      ? Math.round((job.progress || 0) * 100) + '%'
                      : (job.message || job.state)),
                ),
              ))),
        ),
      );
    }

    // The schedule is the part that acts while nobody is watching, so the screen
    // has to show exactly what will happen: every window readable, editable and
    // removable in place. It used to be a read-only list that presets appended
    // to, which is how "While I'm out" ended up in there twice with no way out.
    const DAY_LABELS = () => t2('bt.schedule.days.short').split(' ');

    function windowRow(sched, windows, index) {
      const w = windows[index];
      const days = Array.isArray(w.days) ? w.days : [0, 1, 2, 3, 4, 5, 6];
      const patch = (changes) => {
        const next = windows.slice();
        next[index] = Object.assign({}, w, changes);
        saveSchedule(Object.assign({}, sched, {windows: next}));
      };
      const playlists = (state.playlists || []);
      return h('div', {class: 'list__row list__row--stack'},
        h('div', {class: 'input-group'},
          h('input', {type: 'text', value: w.name || '', placeholder: t2('bt.schedule.window.name'),
            onChange: (event) => patch({name: event.target.value})}),
          h('input', {type: 'time', value: w.start || '08:00', title: t2('bt.schedule.window.from'),
            onChange: (event) => patch({start: event.target.value})}),
          h('input', {type: 'time', value: w.end || '09:00', title: t2('bt.schedule.window.to'),
            onChange: (event) => patch({end: event.target.value})}),
          h('button', {class: 'btn btn--icon btn--sm', title: t2('bt.schedule.remove'),
            onClick: () => saveSchedule(Object.assign({}, sched, {
              windows: windows.filter((_, i) => i !== index),
            }))}, ctx.icon('trash', {size: 16})),
        ),
        h('div', {class: 'input-group'},
          DAY_LABELS().map((label, day) => h('button', {
            class: 'btn btn--sm ' + (days.indexOf(day) === -1 ? 'btn--ghost' : 'btn--outline'),
            title: t2('bt.schedule.window.days'),
            onClick: () => patch({
              days: days.indexOf(day) === -1
                ? days.concat([day]).sort((a, b) => a - b)
                : days.filter((d) => d !== day),
            }),
          }, label)),
        ),
        h('div', {class: 'input-group'},
          h('select', {title: t2('bt.schedule.window.playlist'),
            onChange: (event) => patch({playlist_id: event.target.value})},
            playlists.map((pl) => h('option', {
              value: pl.id, selected: pl.id === (w.playlist_id || 'all'),
            }, pl.name))),
          h('label', {class: 'row row--tight'},
            h('input', {type: 'checkbox', checked: w.enabled !== false,
              onChange: (event) => patch({enabled: event.target.checked})}),
            // Says what the window *is*, not what the click would do -- a ticked
            // box next to the word "Off" reads as if it were off.
            h('span', {class: 'small muted'},
              w.enabled !== false ? t2('bt.schedule.window.on') : t2('bt.schedule.window.off')),
          ),
        ),
      );
    }

    function scheduleCard() {
      const sched = (state.schedule && state.schedule.schedule) || {};
      const quiet = sched.quiet_hours || {};
      const windows = sched.windows || [];
      return h('div', {class: 'card'},
        h('div', {class: 'card__header'},
          h('h3', {class: 'card__title'}, t2('bt.section.schedule')),
          h('div', {class: 'card__tools'},
            h('label', {class: 'row row--tight'},
              h('input', {type: 'checkbox', checked: sched.enabled !== false,
                onChange: (event) => saveSchedule(Object.assign({}, sched, {enabled: event.target.checked}))}),
              h('span', {class: 'small muted'}, t2('bt.schedule.enabled')),
            ),
          ),
        ),
        h('div', {class: 'card__body stack'},
          h('div', {class: 'field'},
            h('span', {class: 'field__label'}, t2('bt.schedule.windows')),
            windows.length === 0
              ? h('p', {class: 'field__hint'}, t2('bt.schedule.windows.empty'))
              : h('div', {class: 'list'}, windows.map((_, index) => windowRow(sched, windows, index))),
            h('button', {class: 'btn btn--outline btn--sm', onClick: () => saveSchedule(
              Object.assign({}, sched, {windows: windows.concat([{
                id: 'window-' + (windows.length + 1),
                name: '', start: '08:00', end: '09:00',
                days: [0, 1, 2, 3, 4, 5, 6], playlist_id: 'all', enabled: true,
              }])}))}, t2('bt.schedule.add')),
          ),
          h('div', {class: 'field'},
            h('span', {class: 'field__label'}, t2('bt.schedule.presets')),
            h('div', {class: 'input-group'}, (state.presets || []).map((preset) =>
              h('button', {class: 'btn btn--ghost btn--sm', onClick: () => {
                // Replaces the window with the same id instead of stacking a
                // second identical one on top of it.
                const win = {
                  id: preset.id, name: preset.name, start: preset.start, end: preset.end,
                  days: preset.days, playlist_id: 'all', enabled: true,
                };
                const kept = windows.filter((w) => w.id !== preset.id);
                saveSchedule(Object.assign({}, sched, {windows: kept.concat([win])}));
              }}, preset.name),
            )),
          ),
          h('div', {class: 'field'},
            h('label', {class: 'field__label'}, t2('bt.schedule.quiet_start')),
            h('input', {type: 'time', value: quiet.start || '20:00',
              onChange: (event) => saveSchedule(Object.assign({}, sched, {quiet_hours: Object.assign({}, quiet, {start: event.target.value})}))}),
          ),
          h('div', {class: 'field'},
            h('label', {class: 'field__label'}, t2('bt.schedule.quiet_end')),
            h('input', {type: 'time', value: quiet.end || '07:00',
              onChange: (event) => saveSchedule(Object.assign({}, sched, {quiet_hours: Object.assign({}, quiet, {end: event.target.value})}))}),
          ),
          h('p', {class: 'field__hint'}, t2('bt.schedule.quiet.hint')),
        ),
      );
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

    function render() {
      // Ordered by how often you touch it: what is playing, what it can play,
      // when it plays, and only then the setup you touch once.
      const nodes = [transportCard(), compatCard(), libraryCard(), playlistsCard(),
        importCard(), scheduleCard(), outputCard()];
      view.replaceChildren(...nodes.filter(Boolean));
    }

    await refreshAll();
    render();

    let cancelled = false;
    const poll = setInterval(async () => {
      if (cancelled) return;
      state.status = await safeGet('/status');
      if (!cancelled) render();
    }, 15000);

    return () => {
      cancelled = true;
      clearInterval(poll);
    };
  },
};
