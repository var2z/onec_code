"""Indexing of a configuration dump into SQLite.

Pipeline for one snapshot:
  scan      list the dump (zip entries / folder files) and compare CRC32+size (zip) or
            size+mtime (folder) with the stored ones -> changed / new / removed files
  parse     changed files are read and hashed (sha1) in worker processes; contents
            already known (same hash, same parser version) are reused, others parsed
  store     new contents: compressed text, parse result, methods, FTS rows, form elements
  files     files table of the snapshot updated
  nodes     graph nodes of the snapshot rebuilt from stored parse results (fast; no parsing)
  gc        contents no longer referenced by any snapshot removed
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

from . import db
from .bsl_parser import extract_query_sources, parse_module
from .layout import CONFIG_OWNER, classify
from .md_parser import DCS_ROOT, parse_configuration, parse_dcs, parse_form, parse_form_meta, parse_object
from .source import open_source

log = logging.getLogger('onec.indexer')

# Bump when parsers change in a way that affects stored results.
PARSER_VERSION = 4
# Bump when node/edge building changes (graph rebuilt even if no file changed).
GRAPH_VERSION = 7


class IndexError_(Exception):
    """Indexing failed with a message meant for the user."""


# ------------------------------------------------------------------ serialization

def _jz(obj) -> bytes:
    return zlib.compress(json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8'), 6)


unjz = db.unjz


def _ref(r):
    return [r.kind, list(r.target), r.line, r.detail]


def _params_text(params) -> str:
    return ', '.join(('Знач ' if p.by_val else '') + p.name + (f' = {p.default}' if p.default else '')
                     for p in params)


def parse_bsl(data: bytes, form_module: bool) -> dict:
    text = data.decode('utf-8-sig', errors='replace')
    mod = parse_module(text, form_module=form_module)
    if text.startswith('﻿'):
        text = text[1:]
    starts = [0]
    pos = text.find('\n')
    while pos >= 0:
        starts.append(pos + 1)
        pos = text.find('\n', pos + 1)
    nl = len(starts)
    methods = []
    details = []
    for i, m in enumerate(mod.methods):
        flags = (db.FLAG_EXPORT if m.export else 0) | (db.FLAG_ASYNC if m.is_async else 0) \
            | (db.FLAG_FUNCTION if m.kind == 'Функция' else 0) | (db.FLAG_IMPLICIT if m.directive_implicit else 0) \
            | (db.FLAG_BROKEN if m.broken else 0) | (db.DIRECTIVE_CODE.get(m.directive, 0) << 8)
        cstart = starts[min(m.doc_line, nl) - 1]
        cend = starts[m.line_end] if m.line_end < nl else len(text)
        methods.append((i, m.name, m.name.lower(), flags, m.line_start, m.line_end, cstart, cend))
        # params, regions, #Если, other annotations, extension kind/target, doc line, header line
        details.append([_params_text(m.params), m.regions or None, m.ifdef or None,
                        [list(a) for a in m.annotations] or None, m.extension_kind, m.extension_target,
                        m.doc_line, m.line_header])
    parsed = {'m': details,
              'r': [[_ref(r) for r in m.refs] for m in mod.methods],
              'b': [_ref(r) for r in mod.body_refs],
              'e': mod.errors}
    return {'kind': 'bsl', 'lines': mod.line_count, 'ztext': zlib.compress(text.encode('utf-8'), 6),
            'parsed': _jz(parsed), 'methods': methods, 'text': text}


SKIP_HASH = hashlib.sha1(b'onec:template:not-a-data-composition-schema').digest()


def parse_dcs_template(data: bytes) -> dict:
    """A data composition schema stored like a module: one "method" per data set, its text is the query.
    Query sources become 'query' refs, so reads_table edges, FTS, outline and diff work as for code."""
    lines, methods, details, refs = [], [], [], []
    pos = 0
    for i, ds in enumerate(parse_dcs(data)):
        start = len(lines) + 1
        head = f'// Набор данных «{ds.name}» ({ds.kind}' + (f', в объединении «{ds.union}»' if ds.union else '') + ')'
        body = (ds.query or '').replace('\r\n', '\n').strip('\n').split('\n') if ds.query else \
            (['// данные передаются из кода (внешний набор данных)'] if ds.kind == 'объект' else [])
        chunk = [head] + body
        text = '\n'.join(chunk) + '\n'
        lines.extend(chunk)
        end = len(lines)
        methods.append((i, ds.name, ds.name.lower(), db.FLAG_DATASET, start, end, pos, pos + len(text)))
        pos += len(text)
        details.append([ds.kind + (f', в объединении «{ds.union}»' if ds.union else ''), None, None, None, None, None,
                        start, start])
        r = []
        if ds.query:
            for typ, name, sub, off in extract_query_sources(ds.query.replace('\r\n', '\n').strip('\n')):
                r.append(['query', [typ, name, sub], start + 1 + off, None])
        refs.append(r)
    text = '\n'.join(lines) + ('\n' if lines else '')
    parsed = {'m': details, 'r': refs, 'b': [], 'e': []}
    return {'kind': 'bsl', 'lines': len(lines), 'ztext': zlib.compress(text.encode('utf-8'), 6),
            'parsed': _jz(parsed), 'methods': methods, 'text': text}


def _elements_rows(info) -> list[tuple]:
    """Non-auxiliary elements; parents re-pointed to the nearest non-auxiliary ancestor."""
    new_idx: dict[int, int] = {}
    rows = []
    els = info.elements
    for i, e in enumerate(els):
        if e.auxiliary:
            continue
        p = e.parent
        while p >= 0 and els[p].auxiliary:
            p = els[p].parent
        new_idx[i] = len(rows)
        rows.append((len(rows), e.name, e.item_type, e.data_path, new_idx.get(p, -1) if p >= 0 else -1))
    return rows


def parse_file(kind: str, data: bytes, form_module: bool = False) -> dict:
    if kind == 'module':
        return parse_bsl(data, form_module)
    if kind == 'object':
        return {'kind': 'object', 'parsed': _jz(dataclasses.asdict(parse_object(data)))}
    if kind == 'form':
        info = parse_form(data)
        d = dataclasses.asdict(info)
        d.pop('elements')
        return {'kind': 'form', 'parsed': _jz(d), 'elements': _elements_rows(info),
                'aux_count': sum(1 for e in info.elements if e.auxiliary)}
    if kind == 'form_meta':
        return {'kind': 'form_meta', 'parsed': _jz(parse_form_meta(data))}
    if kind == 'config':
        return {'kind': 'config', 'parsed': _jz(dataclasses.asdict(parse_configuration(data)))}
    if kind == 'template':
        return parse_dcs_template(data)
    raise ValueError(kind)


# ------------------------------------------------------------------ workers

_KNOWN: set[bytes] = set()      # set in the parent before forking the pool
_SOURCES: dict = {}


def _is_form_module(e) -> bool:
    return e.kind == 'module' and e.module_kind == 'Модуль' and (
        '.Форма.' in (e.owner or '') or (e.owner or '').startswith('ОбщаяФорма.'))


def _work(args):
    src_path, items = args
    src = _SOURCES.get(src_path)
    if src is None:
        src = _SOURCES[src_path] = open_source(src_path)
    out = []
    for path, kind, form_module in items:
        if kind == 'template' and DCS_ROOT not in src.head(path, 1024):
            # spreadsheet / text / binary template: not indexed, only a shared marker is stored
            out.append((path, SKIP_HASH, 0, None if SKIP_HASH in _KNOWN else
                        {'kind': 'skip', 'parsed': _jz(None)}, None))
            continue
        data = src.read(path)
        h = hashlib.sha1(data).digest()
        if h in _KNOWN:
            out.append((path, h, len(data), None, None))
            continue
        if kind == 'module' and path.endswith('.bin'):
            data = b''          # module without source text: only its presence is indexed
        try:
            out.append((path, h, len(data), parse_file(kind, data, form_module), None))
        except Exception as ex:  # noqa: BLE001 - reported per file, indexing continues
            out.append((path, h, len(data), None, f'{type(ex).__name__}: {ex}'))
    return out


# ------------------------------------------------------------------ snapshots

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def create_snapshot(conn, label: str, origin: str, source_path: str, delete_source_after=False) -> int:
    label = ' '.join(label.split())
    if not label:
        raise IndexError_('Пустое название снимка')
    norm = db.norm_label(label)
    if conn.execute('SELECT 1 FROM snapshots WHERE label_norm=?', (norm,)).fetchone():
        raise IndexError_(f'Снимок с названием «{label}» уже есть (названия сравниваются без учёта регистра)')
    st = os.stat(source_path)
    cur = conn.execute(
        'INSERT INTO snapshots(label, label_norm, origin, source_path, source_size, source_mtime, created_at, '
        'status, delete_source_after) VALUES(?,?,?,?,?,?,?,?,?)',
        (label, norm, origin, source_path, st.st_size if os.path.isfile(source_path) else None,
         int(st.st_mtime), now_iso(), 'queued', int(delete_source_after)))
    return cur.lastrowid


def delete_snapshot(conn, snapshot_id: int):
    with db.Tx(conn):
        _drop_names_fts(conn, snapshot_id)
        conn.execute('DELETE FROM edges WHERE snapshot_id=?', (snapshot_id,))
        conn.execute('DELETE FROM nodes WHERE snapshot_id=?', (snapshot_id,))
        conn.execute('DELETE FROM files WHERE snapshot_id=?', (snapshot_id,))
        conn.execute('DELETE FROM snapshots WHERE id=?', (snapshot_id,))
    relink_extensions(conn)
    gc_contents(conn)


def relink_extensions(conn):
    """Rebuild edges of extension snapshots: their extension_intercepts point to node ids of
    the base configuration snapshot, which change when it is reindexed or deleted."""
    from .graph import build_edges
    for r in conn.execute("SELECT id FROM snapshots WHERE is_extension=1 AND status='ready'").fetchall():
        with db.Tx(conn):
            build_edges(conn, r[0])
            conn.execute('UPDATE snapshots SET graph_rev = graph_rev + 1 WHERE id=?', (r[0],))


def _drop_names_fts(conn, snapshot_id):
    conn.execute("INSERT INTO names_fts(names_fts, rowid, name, synonym) "
                 "SELECT 'delete', id, name, synonym FROM nodes WHERE snapshot_id=? AND kind IN ('object','form')",
                 (snapshot_id,))


# ------------------------------------------------------------------ indexing

class Progress:
    def __init__(self, cb=None):
        self.cb = cb
        self.stage = ''
        self.done = 0
        self.total = 0
        self.t_stage = time.time()
        self.t0 = time.time()
        self.stages: dict[str, float] = {}

    def start(self, stage, total=0):
        if self.stage:
            self.stages[self.stage] = round(time.time() - self.t_stage, 2)
        self.stage, self.done, self.total, self.t_stage = stage, 0, total, time.time()
        log.info('stage %s (%s)', stage, total)
        self._emit()

    def step(self, n=1):
        self.done += n
        self._emit()

    def finish(self):
        self.start('')

    def _emit(self):
        if self.cb:
            self.cb(self.as_dict())

    def as_dict(self):
        return {'stage': self.stage, 'done': self.done, 'total': self.total,
                'stages': dict(self.stages), 'elapsed': round(time.time() - self.t0, 1)}


def index_snapshot(conn, snapshot_id: int, full: bool = False, workers: int | None = None,
                   progress_cb=None) -> dict:
    snap = conn.execute('SELECT * FROM snapshots WHERE id=?', (snapshot_id,)).fetchone()
    pr = Progress(progress_cb)
    workers = workers or max(1, min(8, (os.cpu_count() or 2) - 1))
    conn.execute("UPDATE snapshots SET status='indexing', error=NULL WHERE id=?", (snapshot_id,))
    try:
        stats = _index(conn, snap, full, workers, pr)
    except Exception as ex:
        log.exception('indexing %s failed', snap['label'])
        msg = str(ex) if isinstance(ex, IndexError_) else f'{type(ex).__name__}: {ex}'
        conn.execute("UPDATE snapshots SET status='error', error=?, progress=? WHERE id=?",
                     (msg, json.dumps(pr.as_dict(), ensure_ascii=False), snapshot_id))
        raise
    pr.finish()
    stats['timings'] = pr.stages
    stats['elapsed'] = round(time.time() - pr.t0, 1)
    conn.execute("UPDATE snapshots SET status='ready', indexed_at=?, stats=?, progress=? WHERE id=?",
                 (now_iso(), json.dumps(stats, ensure_ascii=False), json.dumps(pr.as_dict(), ensure_ascii=False),
                  snapshot_id))
    log.info('snapshot %s indexed in %.1fs: %s', snap['label'], stats['elapsed'], stats)
    return stats


def _index(conn, snap, full, workers, pr: Progress) -> dict:
    sid = snap['id']
    pr.start('scan')
    src = open_source(snap['source_path'])
    try:
        if not src.has_configuration():
            raise IndexError_('Это не выгрузка конфигурации: в архиве нет файла Configuration.xml')
        files = src.files()
    finally:
        src.close()
    old = {r['path']: r for r in conn.execute(
        'SELECT path, crc, size, stamp, content_id FROM files WHERE snapshot_id=?', (sid,))}
    cur_paths = {f.entry.path for f in files}
    # files whose stored parse result was produced by an older parser version
    stale = {r[0] for r in conn.execute(
        'SELECT f.path FROM files f JOIN contents c ON c.id=f.content_id WHERE f.snapshot_id=? AND c.pver != ?',
        (sid, PARSER_VERSION))}
    todo = [f for f in files
            if full or f.entry.path not in old or f.entry.path in stale
            or old[f.entry.path]['size'] != f.size or old[f.entry.path]['stamp'] != f.stamp]
    removed = [p for p in old if p not in cur_paths]
    log.info('%s: %d files, %d to read, %d removed', snap['label'], len(files), len(todo), len(removed))

    # ---------------- parse (workers)
    global _KNOWN
    _KNOWN = set() if full else {r[0] for r in conn.execute(
        'SELECT hash FROM contents WHERE pver=?', (PARSER_VERSION,))}
    todo.sort(key=lambda f: -f.size)
    batches = []
    cur, cur_size = [], 0
    for f in todo:
        cur.append((f.entry.path, f.entry.kind, _is_form_module(f.entry)))
        # templates: usually only the head is read (most are spreadsheets), size says little about work
        cur_size += f.size if f.entry.kind != 'template' else 20_000
        if cur_size > 4_000_000 or len(cur) >= 300:
            batches.append(cur)
            cur, cur_size = [], 0
    if cur:
        batches.append(cur)
    # interleave big and small batches for balance
    batches = batches[::2] + batches[1::2]

    pr.start('parse', len(todo))
    hash_ids = {}           # sha1 -> content id (filled while storing)
    file_rows = []          # (path, crc, size, stamp, content_id)
    fmeta = {f.entry.path: f for f in todo}
    errors = []
    new_contents = reused = 0
    aux_elements = 0
    t_store = 0.0

    def store(results):
        nonlocal new_contents, reused, aux_elements, t_store
        t = time.time()
        with db.Tx(conn):
            for path, h, size, res, err in results:
                if err:
                    errors.append(f'{path}: {err}')
                    continue
                cid = hash_ids.get(h)
                if cid is None:
                    if res is None:
                        row = conn.execute('SELECT id FROM contents WHERE hash=?', (h,)).fetchone()
                        cid = row[0]
                        reused += 1
                    else:
                        cid = _store_content(conn, h, size, res)
                        new_contents += 1
                        aux_elements += res.get('aux_count', 0)
                    hash_ids[h] = cid
                else:
                    reused += 1
                f = fmeta[path]
                file_rows.append((path, f.crc, f.size, f.stamp, cid))
        t_store += time.time() - t

    if batches:
        if workers > 1 and len(batches) > 1:
            with ProcessPoolExecutor(workers) as ex:
                for res in ex.map(_work, [(snap['source_path'], b) for b in batches]):
                    store(res)
                    pr.step(len(res))
        else:
            for b in batches:
                res = _work((snap['source_path'], b))
                store(res)
                pr.step(len(res))
    _SOURCES.clear()
    _KNOWN = set()
    if errors:
        log.warning('%d files failed to parse, e.g. %s', len(errors), errors[:3])

    prev_stats = json.loads(snap['stats']) if snap['stats'] else {}
    if not todo and not removed and not full and snap['status'] == 'ready' \
            and prev_stats.get('graph_version') == GRAPH_VERSION:
        log.info('%s: no changes, graph kept', snap['label'])
        return {**{k: v for k, v in prev_stats.items() if k not in ('timings', 'elapsed')},
                'read': 0, 'removed': 0, 'new_contents': 0, 'reused_contents': 0, 'unchanged': True}

    # ---------------- files + nodes (one transaction: readers see old or new state)
    pr.start('nodes')
    with db.Tx(conn):
        if removed:
            conn.executemany('DELETE FROM files WHERE snapshot_id=? AND path=?', [(sid, p) for p in removed])
        conn.executemany('INSERT OR REPLACE INTO files(snapshot_id, path, crc, size, stamp, content_id) '
                         'VALUES(?,?,?,?,?,?)', [(sid, *r) for r in file_rows])
        cfg = _config_of(conn, sid)
        conn.execute('UPDATE snapshots SET config_name=?, config_synonym=?, config_version=?, vendor=?, '
                     'is_extension=?, name_prefix=? WHERE id=?',
                     (cfg['name'], cfg['synonym'], cfg['version'], cfg['vendor'], int(cfg['is_extension']),
                      cfg['name_prefix'], sid))
        counts = rebuild_nodes(conn, sid, cfg)
        pr.start('edges')
        from .graph import build_edges
        counts['edges'] = build_edges(conn, sid)
        counts['profile'] = conn.execute('SELECT profile FROM snapshots WHERE id=?', (sid,)).fetchone()[0]
        conn.execute('UPDATE snapshots SET graph_rev = graph_rev + 1 WHERE id=?', (sid,))
    if not cfg['is_extension']:
        pr.start('extensions')
        relink_extensions(conn)
    pr.start('gc')
    gc = gc_contents(conn)
    if new_contents + gc > 2000:
        # many FTS deletes/inserts fragment the index; merge segments and give pages back
        pr.start('optimize')
        conn.execute("INSERT INTO code_fts(code_fts) VALUES('optimize')")
        conn.execute("INSERT INTO names_fts(names_fts) VALUES('optimize')")
        conn.execute('PRAGMA incremental_vacuum')
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    return {'files': len(files), 'read': len(todo), 'removed': len(removed), 'new_contents': new_contents,
            'reused_contents': reused, 'parse_errors': len(errors), 'errors_sample': errors[:5],
            'gc_contents': gc, 'aux_form_elements_skipped': aux_elements, 'graph_version': GRAPH_VERSION,
            'store_seconds': round(t_store, 1), **counts}


def _store_content(conn, h, size, res) -> int:
    kind = res['kind']
    row = conn.execute('SELECT id FROM contents WHERE hash=?', (h,)).fetchone()
    if row:       # stale parser version or full reindex: replace derived data
        cid = row[0]
        _drop_content_derived(conn, cid, res.get('text'))
        conn.execute('UPDATE contents SET kind=?, size=?, lines=?, pver=?, ztext=?, parsed=? WHERE id=?',
                     (kind, size, res.get('lines'), PARSER_VERSION, res.get('ztext'), res['parsed'], cid))
        db.TEXTS.drop(cid)
    else:
        cid = conn.execute('INSERT INTO contents(hash, kind, size, lines, pver, ztext, parsed) VALUES(?,?,?,?,?,?,?)',
                           (h, kind, size, res.get('lines'), PARSER_VERSION, res.get('ztext'),
                            res['parsed'])).lastrowid
    if kind == 'bsl':
        text = res['text']
        for m in res['methods']:
            mid = conn.execute(
                'INSERT INTO content_methods(content_id, ord, name, key, flags, line_start, line_end, cstart, cend) '
                'VALUES(?,?,?,?,?,?,?,?,?)', (cid, *m)).lastrowid
            conn.execute('INSERT INTO code_fts(rowid, body) VALUES(?,?)', (mid, text[m[-2]:m[-1]]))
    elif kind == 'form' and res['elements']:
        conn.executemany('INSERT INTO form_elements(content_id, idx, name, type, data_path, parent) '
                         'VALUES(?,?,?,?,?,?)', [(cid, *e) for e in res['elements']])
    return cid


def _drop_content_derived(conn, cid, text=None):
    rows = conn.execute('SELECT id, cstart, cend FROM content_methods WHERE content_id=?', (cid,)).fetchall()
    if rows:
        if text is None:
            text = db.module_text(conn, cid)
        conn.executemany("INSERT INTO code_fts(code_fts, rowid, body) VALUES('delete', ?, ?)",
                         [(r[0], text[r[1]:r[2]]) for r in rows])
        conn.execute('DELETE FROM content_methods WHERE content_id=?', (cid,))
    conn.execute('DELETE FROM form_elements WHERE content_id=?', (cid,))


def gc_contents(conn) -> int:
    ids = [r[0] for r in conn.execute(
        'SELECT id FROM contents c WHERE NOT EXISTS (SELECT 1 FROM files f WHERE f.content_id = c.id)')]
    if not ids:
        return 0
    with db.Tx(conn):
        for cid in ids:
            _drop_content_derived(conn, cid)
            conn.execute('DELETE FROM contents WHERE id=?', (cid,))
            db.TEXTS.drop(cid)
    return len(ids)


def _config_of(conn, sid) -> dict:
    row = conn.execute("SELECT c.parsed FROM files f JOIN contents c ON c.id=f.content_id "
                       "WHERE f.snapshot_id=? AND f.path='Configuration.xml'", (sid,)).fetchone()
    if not row:
        raise IndexError_('Configuration.xml не проиндексирован')
    return unjz(row[0])


# ------------------------------------------------------------------ nodes

def rebuild_nodes(conn, sid: int, cfg: dict) -> dict:
    """Recreate all nodes of a snapshot from stored parse results."""
    _drop_names_fts(conn, sid)
    conn.execute('DELETE FROM edges WHERE snapshot_id=?', (sid,))
    conn.execute('DELETE FROM nodes WHERE snapshot_id=?', (sid,))
    next_id = (conn.execute('SELECT COALESCE(MAX(id), 0) FROM nodes').fetchone()[0] or 0) + 1
    rows: list[list] = []
    by_full: dict[str, int] = {}
    idx_of: dict[int, int] = {}      # node id -> index in rows

    def add(kind, typ, name, full=None, synonym=None, parent=None, content=None, attrs=None):
        nonlocal next_id
        nid = next_id
        next_id += 1
        idx_of[nid] = len(rows)
        rows.append([nid, sid, kind, typ, name, name.lower() if name else None, full, synonym or None,
                     parent, content, attrs])
        if full:
            by_full[full.lower()] = nid
        return nid

    def dumps(d):
        d = {k: v for k, v in d.items() if v not in (None, [], {}, '', False)}
        return json.dumps(d, ensure_ascii=False, separators=(',', ':')) if d else None

    cfg_id = add('config', 'Конфигурация', cfg['name'], CONFIG_OWNER, cfg['synonym'],
                 attrs=dumps({'version': cfg['version'], 'vendor': cfg['vendor'],
                              'is_extension': cfg['is_extension'], 'name_prefix': cfg['name_prefix']}))

    entries = []
    for r in conn.execute('SELECT f.path, f.content_id, c.kind, c.parsed FROM files f '
                          'JOIN contents c ON c.id=f.content_id WHERE f.snapshot_id=?', (sid,)):
        e = classify(r['path'])
        if e is not None:
            entries.append((e, r['content_id'], r['kind'], r['parsed']))
    order = {'object': 0, 'form_meta': 1, 'form': 2, 'module': 3, 'config': 4}
    entries.sort(key=lambda x: order.get(x[0].kind, 9))

    counts = {'objects': 0, 'attributes': 0, 'forms': 0, 'form_items': 0, 'form_commands': 0,
              'modules': 0, 'methods': 0}
    for e, cid, ckind, parsed in entries:
        # Details (types, properties, events...) are not copied into nodes: they are read
        # from the parse result via content_id (object/form nodes) when needed.
        if e.kind == 'object':
            o = unjz(parsed)
            full = f"{o['md_type']}.{o['name']}"
            oid = add('object', o['md_type'], o['name'], full, o['synonym'], content=cid,
                      attrs=dumps({'adopted': o['adopted']}))
            counts['objects'] += 1
            for a in o['attributes']:
                add('attribute', a['kind'], a['name'], None, a['synonym'], oid)
                counts['attributes'] += 1
            for t in o['tabular_sections']:
                tid = add('tabular', 'ТабличнаяЧасть', t['name'], None, t['synonym'], oid)
                for a in t['attributes']:
                    add('attribute', 'Реквизит', a['name'], None, a['synonym'], tid)
                    counts['attributes'] += 1
            for name, syn in o['enum_values']:
                add('enum_value', 'ЗначениеПеречисления', name, None, syn, oid)
            for name, syn in o['commands']:
                add('command', 'Команда', name, f'{full}.Команда.{name}', syn, oid)
            for fname in o['forms']:
                add('form', 'Форма', fname, f'{full}.Форма.{fname}', None, oid)
        elif e.kind == 'form_meta':
            fid = by_full.get(e.owner.lower())
            if fid is None:
                continue
            m = unjz(parsed)
            row = rows[idx_of[fid]]
            row[7] = m['synonym'] or None
            row[10] = dumps({'ordinary': m['form_type'] != 'Managed', 'adopted': m['adopted']})
        elif e.kind == 'form':
            fid = by_full.get(e.owner.lower())
            if fid is None:       # form without metadata entry: attach to the object
                parent = by_full.get(f'{e.md_type}.{e.name}'.lower())
                fid = add('form', 'Форма', e.sub, e.owner, None, parent)
            f = unjz(parsed)
            row = rows[idx_of[fid]]
            if row[2] == 'object':            # common form: the object node carries the form
                row[10] = dumps({**(json.loads(row[10]) if row[10] else {}), 'form_content': cid})
            else:
                row[9] = cid
            counts['forms'] += 1
            for it in f['items']:
                add('form_item', it['item_type'], it['name'], None, None, fid)
                counts['form_items'] += 1
            for c in f['commands']:
                add('form_command', 'КомандаФормы', c['name'], None, None, fid)
                counts['form_commands'] += 1

    cm_by_content: dict[int, list[int]] = {}
    for r in conn.execute('SELECT cm.content_id, cm.id FROM content_methods cm WHERE cm.content_id IN '
                          '(SELECT content_id FROM files WHERE snapshot_id=?) ORDER BY cm.content_id, cm.ord',
                          (sid,)):
        cm_by_content.setdefault(r[0], []).append(r[1])
    with_src = {e.module_name.lower() for e, *_ in entries if e.kind == 'module' and not e.nosrc}
    for e, cid, ckind, parsed in entries:
        if e.kind == 'template':
            if ckind != 'bsl':          # not a data composition schema
                continue
            mid = add('module', 'СхемаКомпоновкиДанных', e.sub or 'СКД', e.module_name, None,
                      by_full.get(e.owner.lower()), cid)
            counts['dcs'] = counts.get('dcs', 0) + 1
            for cm_id in cm_by_content.get(cid, ()):
                add('method', None, None, None, None, mid, cm_id)
                counts['dcs_datasets'] = counts.get('dcs_datasets', 0) + 1
            continue
        if e.kind != 'module' or (e.nosrc and e.module_name.lower() in with_src):
            continue
        parent = cfg_id if e.owner == CONFIG_OWNER else by_full.get(e.owner.lower())
        if parent is None and e.md_type:
            parent = by_full.get(f'{e.md_type}.{e.name}'.lower())
        mid = add('module', e.module_kind, e.module_kind, e.module_name, None, parent, cid,
                  attrs=dumps({'nosrc': e.nosrc}))
        counts['nosrc_modules'] = counts.get('nosrc_modules', 0) + e.nosrc
        counts['modules'] += 1
        for cm_id in cm_by_content.get(cid, ()):
            add('method', None, None, None, None, mid, cm_id)
            counts['methods'] += 1

    conn.executemany('INSERT INTO nodes(id, snapshot_id, kind, type, name, key, full_name, synonym, parent_id, '
                     'content_id, attrs) VALUES(?,?,?,?,?,?,?,?,?,?,?)', rows)
    conn.execute("INSERT INTO names_fts(rowid, name, synonym) SELECT id, name, synonym FROM nodes "
                 "WHERE snapshot_id=? AND kind IN ('object','form')", (sid,))
    counts['nodes'] = len(rows)
    return counts
