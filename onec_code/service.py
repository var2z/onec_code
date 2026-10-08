"""Background indexing service: one job at a time, inbox synchronisation, uploads.

Jobs run in a dedicated thread with its own write connection; MCP and web readers use
their own connections and keep answering from ready snapshots meanwhile.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
import zipfile
from collections import deque

from . import db, indexer
from .query import NotFound, _vkey
from .source import ZipSource

log = logging.getLogger('onec.service')


class UserError(Exception):
    pass


class IndexService:
    def __init__(self, db_path: str, inbox: str | None, archives: str, workers: int | None = None,
                 inbox_scan_seconds: int = 60):
        self.db_path = db_path
        self.inbox = inbox
        self.archives = archives
        self.workers = workers
        self.scan_every = inbox_scan_seconds
        self.jobs: deque = deque()             # (kind, snapshot id, full)
        self.cv = threading.Condition()
        self.current: dict | None = None
        self.recent: deque = deque(maxlen=20)  # finished jobs (for status)
        self.notes: list[str] = []             # inbox conflicts etc.
        self._conn = None
        self.wlock = threading.RLock()        # guards the shared service connection
        self._seen: dict[str, tuple] = {}     # inbox zip path -> (size, mtime_ns) at the previous scan

    # -------------------------------------------------------------- lifecycle

    def start(self):
        os.makedirs(self.archives, exist_ok=True)
        conn = self._write_conn()
        db.init(conn)
        # interrupted jobs (container restart) are resumed
        conn.execute("UPDATE snapshots SET status='queued' WHERE status='indexing'")
        for r in conn.execute("SELECT id FROM snapshots WHERE status='queued' ORDER BY id").fetchall():
            self._enqueue('index', r[0], False)
        # after an upgrade: stale parse results or graph built by an older version -> rebuild
        stale_parse = conn.execute('SELECT 1 FROM contents WHERE pver != ? LIMIT 1',
                                   (indexer.PARSER_VERSION,)).fetchone()
        for r in conn.execute("SELECT id, stats FROM snapshots WHERE status='ready'").fetchall():
            gv = json.loads(r['stats']).get('graph_version') if r['stats'] else None
            if stale_parse or gv != indexer.GRAPH_VERSION:
                self._enqueue('index', r['id'], False)
        self.sync_inbox()
        threading.Thread(target=self._run, name='indexer', daemon=True).start()
        if self.inbox and self.scan_every > 0:
            threading.Thread(target=self._scan_loop, name='inbox-scan', daemon=True).start()

    def _write_conn(self):
        if self._conn is None:
            self._conn = db.connect(self.db_path)
        return self._conn

    def _enqueue(self, kind, sid, full=False):
        with self.cv:
            if any(j[0] == kind and j[1] == sid for j in self.jobs):
                return
            if self.current and self.current['sid'] == sid and kind == 'index' and not full:
                # already running: run once more after it (the archive may have changed)
                pass
            self.jobs.append((kind, sid, full))
            self.cv.notify()

    def _scan_loop(self):
        while True:
            time.sleep(self.scan_every)
            try:
                self.sync_inbox(dirs=False)
            except Exception:  # noqa: BLE001
                log.exception('inbox scan failed')

    def _run(self):
        conn = db.connect(self.db_path)
        while True:
            with self.cv:
                while not self.jobs:
                    self.cv.wait()
                kind, sid, full = self.jobs.popleft()
                row = conn.execute('SELECT label FROM snapshots WHERE id=?', (sid,)).fetchone()
                if row is None:
                    continue
                self.current = {'sid': sid, 'label': row[0], 'job': kind, 'full': full, 'started': time.time(),
                                'progress': {}}
            t0 = time.time()
            err = None
            try:
                if kind == 'index':
                    self._index(conn, sid, full)
                elif kind == 'delete':
                    self._delete(conn, sid)
            except Exception as ex:  # noqa: BLE001 - recorded in snapshot + status
                err = str(ex)
                log.error('job %s %s failed: %s', kind, sid, ex)
            with self.cv:
                self.recent.appendleft({'label': self.current['label'], 'job': kind, 'error': err,
                                        'seconds': round(time.time() - t0, 1), 'finished': time.strftime('%H:%M:%S')})
                self.current = None

    def _index(self, conn, sid, full):
        snap = conn.execute('SELECT * FROM snapshots WHERE id=?', (sid,)).fetchone()
        if not os.path.exists(snap['source_path']):
            msg = 'Архив удалён после индексации — загрузите его заново' if snap['origin'] == 'upload' \
                else f"Источник не найден: {snap['source_path']}"
            if snap['indexed_at']:
                raise UserError(msg)      # keep the ready index, just report
            conn.execute("UPDATE snapshots SET status='error', error=? WHERE id=?", (msg, sid))
            raise UserError(msg)

        def progress(p):
            if self.current is not None:
                self.current['progress'] = p

        indexer.index_snapshot(conn, sid, full=full, workers=self.workers, progress_cb=progress)
        if snap['delete_source_after'] and snap['origin'] == 'upload':
            try:
                os.remove(snap['source_path'])
                log.info('archive %s removed after indexing', snap['source_path'])
            except OSError as ex:
                log.warning('cannot remove %s: %s', snap['source_path'], ex)

    def _delete(self, conn, sid):
        snap = conn.execute('SELECT origin, source_path FROM snapshots WHERE id=?', (sid,)).fetchone()
        indexer.delete_snapshot(conn, sid)
        if snap and snap['origin'] == 'upload' and os.path.exists(snap['source_path']):
            os.remove(snap['source_path'])

    # -------------------------------------------------------------- inbox

    def sync_inbox(self, dirs=True) -> list[str]:
        """Register new dumps from inbox, reindex changed zips, drop snapshots of removed ones."""
        with self.wlock:
            return self._sync_inbox(dirs)

    def _sync_inbox(self, dirs) -> list[str]:
        if not self.inbox or not os.path.isdir(self.inbox):
            return []
        conn = self._write_conn()
        actions = []
        present = {}
        for name in sorted(os.listdir(self.inbox)):
            p = os.path.join(self.inbox, name)
            if os.path.isfile(p) and name.lower().endswith('.zip'):
                present[p] = os.path.splitext(name)[0]
            elif os.path.isdir(p) and os.path.isfile(os.path.join(p, 'Configuration.xml')):
                present[p] = name
        known = {r['source_path']: r for r in conn.execute("SELECT * FROM snapshots WHERE origin IN ('inbox', 'dir')")}
        notes = []
        seen, self._seen = self._seen, {}
        for path, label in present.items():
            st = os.stat(path)
            row = known.get(path)
            is_new_or_changed = row is None or (os.path.isfile(path) and (
                row['source_size'] != st.st_size or row['source_mtime'] != int(st.st_mtime)))
            if os.path.isfile(path) and is_new_or_changed:
                # an archive being copied: wait until it stops changing and is a complete zip
                stamp = (st.st_size, st.st_mtime_ns)
                self._seen[path] = stamp
                if self.scan_every > 0 and seen.get(path) != stamp:
                    notes.append(f'inbox/{os.path.basename(path)}: файл появился или изменился — жду, пока '
                                 'закончится копирование (до следующей проверки)')
                    continue
                if not zipfile.is_zipfile(path):
                    notes.append(f'inbox/{os.path.basename(path)}: не zip-архив или архив не докопирован')
                    continue
            if row is None:
                try:
                    sid = indexer.create_snapshot(conn, label, 'inbox' if path.endswith('.zip') else 'dir', path)
                except indexer.IndexError_ as ex:
                    notes.append(f'inbox/{os.path.basename(path)}: {ex}')
                    continue
                self._enqueue('index', sid)
                actions.append(f'новый снимок {label}')
            elif os.path.isfile(path) and (row['source_size'] != st.st_size or row['source_mtime'] != int(st.st_mtime)):
                conn.execute('UPDATE snapshots SET source_size=?, source_mtime=? WHERE id=?',
                             (st.st_size, int(st.st_mtime), row['id']))
                self._enqueue('index', row['id'])
                actions.append(f'изменился {row["label"]}')
            elif os.path.isdir(path) and dirs:
                self._enqueue('index', row['id'])
        for path, row in known.items():
            if path not in present:
                self._enqueue('delete', row['id'])
                actions.append(f'удалён из inbox: {row["label"]}')
        self.notes = notes
        if actions:
            log.info('inbox: %s', '; '.join(actions))
        return actions

    # -------------------------------------------------------------- web / MCP API

    def check_title(self, title: str, own_id: int | None = None) -> str:
        label = ' '.join((title or '').split())
        if not label:
            raise UserError('Укажите название')
        if len(label) > 100:
            raise UserError('Название длиннее 100 символов')
        conn = self._write_conn()
        if conn.execute('SELECT 1 FROM snapshots WHERE label_norm=? AND id IS NOT ?',
                        (db.norm_label(label), own_id)).fetchone():
            raise UserError(f'Снимок «{label}» уже есть (названия сравниваются без учёта регистра)')
        return label

    def new_upload_path(self) -> str:
        return os.path.join(self.archives, f'.upload-{uuid.uuid4().hex}.part')

    def register_upload(self, title: str, part_path: str, delete_after: bool) -> int:
        """Validate an uploaded archive and queue it. Removes the file on failure."""
        try:
            label = self.check_title(title)
            try:
                with ZipSource(part_path) as src:
                    if not src.has_configuration():
                        raise UserError('Это не выгрузка конфигурации: в архиве нет файла Configuration.xml. '
                                        'Выгрузите конфигурацию в файлы (Конфигуратор → Конфигурация → '
                                        'Выгрузить конфигурацию в файлы) и упакуйте папку в zip.')
            except zipfile.BadZipFile:
                raise UserError('Файл не является zip-архивом') from None
            final = os.path.join(self.archives, f'{uuid.uuid4().hex}.zip')
            os.replace(part_path, final)
        except Exception:
            if os.path.exists(part_path):
                os.remove(part_path)
            raise
        with self.wlock:
            sid = indexer.create_snapshot(self._write_conn(), label, 'upload', final,
                                          delete_source_after=delete_after)
        self._enqueue('index', sid)
        return sid

    def rename(self, sid: int, title: str):
        with self.wlock:
            label = self.check_title(title, own_id=sid)
            self._write_conn().execute('UPDATE snapshots SET label=?, label_norm=? WHERE id=?',
                                       (label, db.norm_label(label), sid))

    def reindex(self, sid: int, full=False):
        self._enqueue('index', sid, full)

    def delete(self, sid: int):
        with self.cv:
            self.jobs = deque(j for j in self.jobs if j[1] != sid)
        self._enqueue('delete', sid)

    def request_reindex(self, config: str | None, full: bool) -> str:
        acts = self.sync_inbox()
        conn = self._write_conn()
        rows = conn.execute('SELECT id, label, config_name, config_version, config_synonym FROM snapshots').fetchall()
        if config and config.strip():
            c = db.norm_label(config)
            sel = [r for r in rows if db.norm_label(r['label']) == c]
            if not sel and '@' in c:
                n, _, v = c.partition('@')
                sel = [r for r in rows if (r['config_name'] or '').casefold() == n.strip() and r['config_version'] == v.strip()]
            if not sel:
                same = [r for r in rows if (r['config_name'] or '').casefold() == c or
                        db.norm_label(r['config_synonym'] or '') == c]
                sel = [max(same, key=lambda r: _vkey(r['config_version']))] if same else []
            if not sel:
                raise NotFound(f'Конфигурация «{config}» не найдена', [r['label'] for r in rows])
        else:
            sel = rows
        for r in sel:
            self._enqueue('index', r['id'], full)
        text = f"Поставлено в очередь ({'полная' if full else 'инкрементальная'} индексация): " + \
            ', '.join(r['label'] for r in sel)
        if acts:
            text += '\ninbox: ' + '; '.join(acts)
        return text + '\nПрогресс: index_status\n'

    def status(self) -> dict:
        with self.cv:
            cur = dict(self.current) if self.current else None
            queue = list(self.jobs)
            recent = list(self.recent)
        conn = self._write_conn()
        labels = {r[0]: r[1] for r in conn.execute('SELECT id, label FROM snapshots')}
        errors = [{'label': r['label'], 'error': r['error']} for r in
                  conn.execute("SELECT label, error FROM snapshots WHERE status='error'")]
        return {'current': cur, 'queue': [{'job': k, 'label': labels.get(s, s), 'full': f} for k, s, f in queue],
                'recent': recent, 'errors': errors, 'notes': self.notes}

    def status_text(self) -> str:
        s = self.status()
        lines = []
        cur = s['current']
        if cur:
            p = cur.get('progress') or {}
            stage = p.get('stage') or 'подготовка'
            done = f" {p.get('done')}/{p.get('total')}" if p.get('total') else ''
            lines.append(f"Идёт: {cur['job']} «{cur['label']}» — этап {stage}{done}, "
                         f"{round(time.time() - cur['started'])} с; этапы: {p.get('stages', {})}")
        else:
            lines.append('Индексация не выполняется')
        if s['queue']:
            lines.append('Очередь: ' + ', '.join(f"{j['job']} {j['label']}" for j in s['queue']))
        for r in s['recent'][:5]:
            lines.append(f"Готово {r['finished']}: {r['job']} «{r['label']}» за {r['seconds']} с"
                         + (f" — ОШИБКА: {r['error']}" if r['error'] else ''))
        for e in s['errors']:
            lines.append(f"Ошибка снимка «{e['label']}»: {e['error']}")
        for n in s['notes']:
            lines.append(f'Внимание: {n}')
        return '\n'.join(lines) + '\n'
