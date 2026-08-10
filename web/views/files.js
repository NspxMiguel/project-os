// views/files.js — Advanced mode: a file browser over project_os/api/files.py.
//
// The server enforces the sandbox (home dir + media dir + configured extra
// roots, symlinks resolved before the check); this view's only job is to be
// honest about it -- the roots are shown up front, and a 403 with
// code "outside_sandbox" is shown as the server's own message rather than a
// generic failure, because that message already names what IS allowed.
//
// Navigation state (current directory) lives in the URL query string
// (`#/files?path=...`) rather than in memory, so a refresh or a bookmark lands
// back where the user was.

import {h, mount} from '../lib/dom.js';
import {icon} from '../lib/icons.js';
import * as fmt from '../lib/format.js';
import {t, setStrings} from '../lib/format.js';
import * as api from '../lib/api.js';
import {toast, confirm} from '../lib/toast.js';
import {navigate} from '../lib/router.js';

setStrings('en', {
  'files.title': 'Files',
  'files.lead': 'Browse, edit and move files project-os is allowed to touch.',
  'files.roots': 'Allowed locations',
  'files.readonly': 'Writing is turned off in Settings (security.allow_file_write).',
  'files.upload': 'Upload',
  'files.newFolder': 'New folder',
  'files.empty.title': 'This folder is empty',
  'files.empty.detail': 'Upload a file or make a folder to get started.',
  'files.error.title': 'Could not list this folder',
  'files.col.name': 'Name',
  'files.col.size': 'Size',
  'files.col.modified': 'Modified',
  'files.action.download': 'Download',
  'files.action.rename': 'Rename',
  'files.action.delete': 'Delete',
  'files.action.edit': 'Edit',
  'files.rename.prompt': 'New name for {name}',
  'files.mkdir.prompt': 'Folder name',
  'files.confirm.delete': 'Delete {name}? This cannot be undone.',
  'files.confirm.deleteDir': 'Delete {name} and everything inside it? This cannot be undone.',
  'files.editor.title': 'Editing {name}',
  'files.editor.save': 'Save',
  'files.editor.saved': 'Saved',
  'files.editor.tooLarge': 'This file is larger than the editor limit; download it instead.',
  'files.editor.notText': 'This does not look like a text file.',
  'files.uploading': 'Uploading {name}…',
  'files.uploaded': '{name} uploaded',
  'files.deleted': '{name} deleted',
  'files.permissionDenied': 'Permission denied',
}, {activate: false});

/* --------------------------------------------------------------- pieces */

function errorNotice(message, onRetry) {
  return h('div', {class: 'notice notice--error'},
    icon('warning', {size: 18}),
    h('div', {class: 'notice__body'},
      h('span', null, message),
      onRetry
        ? h('div', {class: 'notice__actions'},
            h('button', {class: 'btn btn--sm', type: 'button', onClick: onRetry},
              icon('refresh', {size: 14}), t('action.retry')))
        : null,
    ),
  );
}

function emptyState(title, message) {
  return h('div', {class: 'empty'},
    h('span', {class: 'empty__icon'}, icon('folder', {size: 22})),
    h('p', {class: 'empty__title'}, title),
    message ? h('p', {class: 'empty__text'}, message) : null,
  );
}

function basename(path) {
  const clean = String(path || '').replace(/\/+$/, '');
  const idx = clean.lastIndexOf('/');
  return idx === -1 ? clean : clean.slice(idx + 1);
}

function dirname(path) {
  const clean = String(path || '').replace(/\/+$/, '');
  const idx = clean.lastIndexOf('/');
  return idx <= 0 ? '/' : clean.slice(0, idx);
}

function joinPath(dir, name) {
  const clean = String(dir || '').replace(/\/+$/, '');
  return (clean || '') + '/' + name;
}

/** Breadcrumb segments from a root down to the current path. Paths outside
 *  every root simply do not appear (the sandbox rejects them anyway). */
function breadcrumbs(path, roots) {
  const root = roots.find((r) => path === r.path || path.startsWith(r.path + '/'));
  if (!root) return [{label: path, path}];
  const rest = path.slice(root.path.length).split('/').filter(Boolean);
  const crumbs = [{label: root.name, path: root.path}];
  let acc = root.path;
  for (const seg of rest) {
    acc = joinPath(acc, seg);
    crumbs.push({label: seg, path: acc});
  }
  return crumbs;
}

/* ------------------------------------------------------------------ view */

export default {
  id: 'files',
  title: t('nav.files'),

  async mount(root, ctx) {
    let disposed = false;
    let roots = [];
    let writable = false;
    let currentPath = ctx.query && ctx.query.path ? ctx.query.path : null;
    let listing = null; // {path, parent, entries, writable}
    let listError = null;
    let loading = true;
    let editingEntry = null;

    const headerHost = h('div', {class: 'page__header'});
    const crumbHost = h('div', {class: 'row', style: {flexWrap: 'wrap'}});
    const toolbarHost = h('div', {class: 'row row--end'});
    const bodyHost = h('div', {class: 'stack'});
    const fileInput = h('input', {
      type: 'file', style: {display: 'none'},
      onChange: (event) => {
        const file = event.target.files && event.target.files[0];
        event.target.value = '';
        if (file) uploadFile(file);
      },
    });

    mount(root, [
      headerHost,
      h('div', {class: 'stack'}, crumbHost, toolbarHost, bodyHost, fileInput),
    ]);

    function renderHeader() {
      mount(headerHost, [
        h('div', null,
          h('h2', null, t('files.title')),
          h('p', {class: 'page__lead'}, t('files.lead')),
        ),
        h('div', {class: 'page__actions'},
          roots.length
            ? h('div', {class: 'row', style: {gap: 'var(--space-1)'}},
                h('span', {class: 'tiny faint'}, t('files.roots') + ':'),
                roots.map((r) => h('span', {class: 'tag mono'}, r.name)))
            : null,
        ),
      ]);
    }

    function renderCrumbs() {
      const path = listing ? listing.path : currentPath;
      if (!path || !roots.length) {
        mount(crumbHost, h('span', {class: 'small faint'}, '—'));
        return;
      }
      const crumbs = breadcrumbs(path, roots);
      const nodes = [];
      crumbs.forEach((crumb, i) => {
        if (i > 0) nodes.push(h('span', {class: 'faint tiny'}, icon('chevron', {size: 12})));
        nodes.push(h('button', {
          class: 'btn btn--ghost btn--sm', type: 'button',
          disabled: i === crumbs.length - 1,
          onClick: () => go(crumb.path),
        }, crumb.label));
      });
      mount(crumbHost, nodes);
    }

    function renderToolbar() {
      const canWrite = writable && listing && listing.writable;
      mount(toolbarHost, [
        h('button', {
          class: 'btn btn--sm', type: 'button', disabled: !canWrite,
          onClick: () => fileInput.click(),
        }, icon('upload', {size: 15}), t('files.upload')),
        h('button', {
          class: 'btn btn--sm', type: 'button', disabled: !canWrite,
          onClick: makeFolder,
        }, icon('folder', {size: 15}), t('files.newFolder')),
        h('button', {
          class: 'btn btn--ghost btn--sm', type: 'button',
          onClick: () => load(currentPath, true),
        }, icon('refresh', {size: 15}), t('action.refresh')),
      ]);
    }

    /* --------------------------------------------------------- rendering */

    function fileIcon(entry) {
      if (entry.kind === 'directory') return icon('folder', {size: 17});
      return icon('file', {size: 17});
    }

    function entryRow(entry) {
      const isDir = entry.kind === 'directory';
      const nameNode = isDir
        ? h('button', {class: 'list__title', style: {background: 'none', border: 0, padding: 0, cursor: 'pointer', color: 'inherit', textAlign: 'left'}, type: 'button', onClick: () => go(entry.path)}, entry.name)
        : h('span', {class: 'list__title'}, entry.name);

      const canEdit = entry.editable;
      const canWrite = writable && listing && listing.writable && entry.writable !== false;

      const actions = [];
      if (canEdit) {
        actions.push(h('button', {
          class: 'btn btn--icon btn--sm', type: 'button', title: t('files.action.edit'),
          'aria-label': t('files.action.edit'), onClick: () => openEditor(entry),
        }, icon('edit', {size: 14})));
      }
      if (!isDir) {
        actions.push(h('a', {
          class: 'btn btn--icon btn--sm', title: t('files.action.download'),
          'aria-label': t('files.action.download'),
          href: api.url('/files/download?path=' + encodeURIComponent(entry.path)),
        }, icon('download', {size: 14})));
      }
      if (canWrite) {
        actions.push(h('button', {
          class: 'btn btn--icon btn--sm', type: 'button', title: t('files.action.rename'),
          'aria-label': t('files.action.rename'), onClick: () => renameEntry(entry),
        }, icon('edit', {size: 14})));
        actions.push(h('button', {
          class: 'btn btn--icon btn--sm', type: 'button', title: t('files.action.delete'),
          'aria-label': t('files.action.delete'), onClick: () => deleteEntry(entry),
        }, icon('trash', {size: 14})));
      }

      return h('div', {class: 'list__row'},
        h('span', {class: 'card__icon'}, fileIcon(entry)),
        h('div', {class: 'list__main'},
          nameNode,
          h('span', {class: 'list__sub'}, entry.readable === false ? t('files.permissionDenied') : (isDir ? '—' : '')),
        ),
        h('span', {class: 'list__aside'},
          h('span', {class: 'tiny faint mono'}, isDir ? '—' : fmt.bytes(entry.size)),
          h('span', {class: 'tiny faint'}, entry.modified ? fmt.relativeTime(entry.modified * 1000) : '—'),
          h('div', {class: 'row', style: {gap: 'var(--space-1)'}}, actions),
        ),
      );
    }

    function renderBody() {
      if (loading && !listing) {
        mount(bodyHost, h('div', {class: 'skeleton-stack'},
          h('div', {class: 'skeleton skeleton--block'}),
          h('div', {class: 'skeleton skeleton--text'}),
          h('div', {class: 'skeleton skeleton--text skeleton--line-sm'})));
        return;
      }
      if (listError) {
        mount(bodyHost, errorNotice(listError, () => load(currentPath, true)));
        return;
      }
      if (editingEntry) {
        mount(bodyHost, editorPane());
        return;
      }
      if (!listing || !listing.entries.length) {
        mount(bodyHost, emptyState(t('files.empty.title'), t('files.empty.detail')));
        return;
      }
      mount(bodyHost, [
        !writable ? h('div', {class: 'notice notice--info'},
          icon('lock', {size: 18}), h('div', {class: 'notice__body'}, h('span', {class: 'small muted'}, t('files.readonly')))) : null,
        h('div', {class: 'list'}, listing.entries.map(entryRow)),
      ]);
    }

    /* ----------------------------------------------------------- editor */

    let editorContent = '';
    let editorDirty = false;
    let editorTextarea = null;

    function editorPane() {
      editorTextarea = h('textarea', {
        class: 'code mono',
        style: {width: '100%', minHeight: '50vh', resize: 'vertical'},
        value: editorContent,
        onInput: () => { editorDirty = true; },
      });
      const saveBtn = h('button', {
        class: 'btn btn--primary btn--sm', type: 'button',
        onClick: () => saveEditor(),
      }, icon('save', {size: 15}), t('files.editor.save'));
      return h('div', {class: 'card'},
        h('div', {class: 'card__header'},
          h('span', {class: 'card__icon'}, icon('file', {size: 18})),
          h('div', {class: 'grow'}, h('div', {class: 'card__title'}, t('files.editor.title', {name: editingEntry.name}))),
          h('button', {class: 'btn btn--ghost btn--sm', type: 'button', onClick: closeEditor}, icon('x', {size: 14}), t('action.close')),
        ),
        h('div', {class: 'card__body'}, editorTextarea),
        h('div', {class: 'card__footer'}, saveBtn),
      );
    }

    async function openEditor(entry) {
      try {
        editorContent = await api.get('/files/read', {query: {path: entry.path}, raw: false});
        // The endpoint returns text/plain, and api.get() only parses JSON --
        // a non-JSON body already comes back as the raw string, which is what
        // we want here.
      } catch (err) {
        toast((err && err.message) || t('files.editor.notText'), {type: 'error'});
        return;
      }
      editingEntry = entry;
      editorDirty = false;
      renderBody();
    }

    function closeEditor() {
      editingEntry = null;
      renderBody();
    }

    async function saveEditor() {
      if (!editingEntry) return;
      try {
        await api.put('/files/write', {content: editorTextarea.value}, {query: {path: editingEntry.path}});
        toast(t('files.editor.saved'), {type: 'success'});
        editorDirty = false;
      } catch (err) {
        toast((err && err.message) || t('state.error'), {type: 'error'});
      }
    }

    /* ------------------------------------------------------------ writes */

    async function makeFolder() {
      const name = window.prompt(t('files.mkdir.prompt'));
      if (!name) return;
      try {
        await api.post('/files/mkdir', {path: joinPath(currentPath, name.trim())});
        await load(currentPath, true);
      } catch (err) {
        toast((err && err.message) || t('state.error'), {type: 'error'});
      }
    }

    async function renameEntry(entry) {
      const name = window.prompt(t('files.rename.prompt', {name: entry.name}), entry.name);
      if (!name || name === entry.name) return;
      try {
        await api.post('/files/move', {source: entry.path, destination: joinPath(dirname(entry.path), name.trim())});
        await load(currentPath, true);
      } catch (err) {
        toast((err && err.message) || t('state.error'), {type: 'error'});
      }
    }

    async function deleteEntry(entry) {
      const key = entry.kind === 'directory' ? 'files.confirm.deleteDir' : 'files.confirm.delete';
      const ok = await confirm(t(key, {name: entry.name}), {title: t('files.action.delete'), danger: true});
      if (!ok) return;
      try {
        await api.del('/files', null, {query: {path: entry.path, recursive: entry.kind === 'directory'}});
        toast(t('files.deleted', {name: entry.name}), {type: 'success', timeout: 2500});
        await load(currentPath, true);
      } catch (err) {
        toast((err && err.message) || t('state.error'), {type: 'error'});
      }
    }

    async function uploadFile(file) {
      const dismiss = toast(t('files.uploading', {name: file.name}), {type: 'info', timeout: 0});
      try {
        // api.upload() is XHR-based and has no query-string support (only
        // fetch-based request() does), so the destination directory has to be
        // baked into the path itself.
        await api.upload('/files/upload?path=' + encodeURIComponent(currentPath || ''), file, null);
        dismiss();
        toast(t('files.uploaded', {name: file.name}), {type: 'success'});
        await load(currentPath, true);
      } catch (err) {
        dismiss();
        toast((err && err.message) || t('state.error'), {type: 'error'});
      }
    }

    /* ------------------------------------------------------------ loads */

    async function loadRoots() {
      try {
        const raw = await api.get('/files/roots');
        if (disposed) return;
        roots = Array.isArray(raw.roots) ? raw.roots : [];
        writable = Boolean(raw.writable);
      } catch (err) {
        if (disposed) return;
        roots = [];
      }
      renderHeader();
      renderCrumbs();
    }

    async function load(path, manual) {
      loading = true;
      editingEntry = null;
      if (!manual) renderBody();
      try {
        const raw = await api.get('/files', {query: path ? {path} : null});
        if (disposed) return;
        listing = raw;
        currentPath = raw.path;
        listError = null;
        navigate('#/files?path=' + encodeURIComponent(raw.path), {replace: true});
      } catch (err) {
        if (disposed) return;
        listError = (err && err.message) || t('files.error.title');
      } finally {
        loading = false;
      }
      renderCrumbs();
      renderToolbar();
      renderBody();
    }

    function go(path) {
      load(path, false);
    }

    /* ------------------------------------------------------------ start */

    await loadRoots();
    await load(currentPath, false);

    return () => {
      disposed = true;
    };
  },
};
