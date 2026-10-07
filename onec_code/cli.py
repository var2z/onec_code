"""Command line: python -m onec_code.cli <command> ...

  index PATH [--label L] [--full] [--workers N]   index (or incrementally reindex) a dump
  status                                          snapshots and their state
  sizes                                           index size by table
  delete LABEL                                    remove a snapshot
  profile LABEL auto|none|up                      configuration profile of a snapshot, rebuilds its edges
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from . import db, indexer

DEFAULT_DB = os.environ.get('ONEC_DB', '/data/index.db')

# logical groups for the size report
GROUPS = [
    ('Тексты модулей (contents.ztext) и результаты разбора', ('contents', 'sqlite_autoindex_contents_1')),
    ('Методы по содержимому (content_methods)', ('content_methods', 'cm_content', 'cm_key')),
    ('FTS по коду (code_fts)', ('code_fts_',)),
    ('Элементы форм (form_elements)', ('form_elements',)),
    ('Файлы снимков (files)', ('files', 'files_content')),
    ('Узлы графа (nodes)', ('nodes', 'nodes_lookup', 'nodes_parent', 'nodes_content')),
    ('FTS по именам (names_fts)', ('names_fts_',)),
    ('Рёбра (edges)', ('edges', 'edges_from', 'edges_to', 'edges_snap')),
]


def cmd_index(conn, a):
    path = os.path.abspath(a.path)
    label = a.label or os.path.splitext(os.path.basename(path.rstrip('/')))[0]
    row = conn.execute('SELECT id FROM snapshots WHERE label_norm=?', (db.norm_label(label),)).fetchone()
    if row:
        sid = row[0]
    else:
        origin = 'inbox' if path.startswith('/inbox/') else ('dir' if os.path.isdir(path) else 'upload')
        sid = indexer.create_snapshot(conn, label, origin, path)
    stats = indexer.index_snapshot(conn, sid, full=a.full, workers=a.workers)
    print(json.dumps(stats, ensure_ascii=False, indent=1))


def cmd_status(conn, a):
    for r in conn.execute('SELECT id, label, config_name, config_version, status, error, indexed_at, stats '
                          'FROM snapshots ORDER BY config_name, label'):
        st = json.loads(r['stats']) if r['stats'] else {}
        print(f"{r['id']:3} {r['label']:20} {r['config_name'] or '':25} {r['config_version'] or '':12} "
              f"{r['status']:8} modules={st.get('modules')} methods={st.get('methods')} {r['error'] or ''}")


def cmd_sizes(conn, a):
    sizes = dict(db.table_sizes(conn))
    total_file = os.path.getsize(a.db)
    used = set()
    print(f'{"Группа":58} {"МБ":>9}')
    for title, names in GROUPS:
        s = 0
        for n, v in sizes.items():
            if n in names or any(n.startswith(p) for p in names if p.endswith('_')):
                s += v
                used.add(n)
        print(f'{title:58} {s / 1e6:9.1f}')
    rest = sum(v for n, v in sizes.items() if n not in used)
    print(f'{"Прочее (служебные таблицы)":58} {rest / 1e6:9.1f}')
    print(f'{"Итого страниц в БД":58} {sum(sizes.values()) / 1e6:9.1f}')
    print(f'{"Размер файла index.db":58} {total_file / 1e6:9.1f}')
    if a.verbose:
        for n, v in sorted(sizes.items(), key=lambda x: -x[1]):
            print(f'   {n:40} {v / 1e6:9.1f}')
    r = conn.execute("SELECT COUNT(*), SUM(size), SUM(length(ztext)), SUM(length(parsed)) FROM contents "
                     "WHERE kind='bsl'").fetchone()
    print(f'bsl contents: {r[0]:,}, text {(r[1] or 0) / 1e6:.1f} MB -> zlib {(r[2] or 0) / 1e6:.1f} MB; '
          f'parse results {(r[3] or 0) / 1e6:.1f} MB')
    for k in ('object', 'form', 'form_meta', 'config'):
        r = conn.execute('SELECT COUNT(*), SUM(length(parsed)) FROM contents WHERE kind=?', (k,)).fetchone()
        print(f'{k} contents: {r[0]:,}, parse results {(r[1] or 0) / 1e6:.1f} MB')


def cmd_profile(conn, a):
    """Set the profile of a snapshot (auto | none | code) and rebuild its edges."""
    from . import graph, profiles
    row = conn.execute('SELECT id, config_name FROM snapshots WHERE label_norm=?', (db.norm_label(a.label),)).fetchone()
    if not row:
        sys.exit(f'нет снимка {a.label}')
    profiles.select(row['config_name'], a.profile)          # validates the value
    t = time.time()
    with db.Tx(conn):
        conn.execute('UPDATE snapshots SET profile_override=? WHERE id=?',
                     (None if a.profile == 'auto' else a.profile, row['id']))
        counts = graph.build_edges(conn, row['id'])
        conn.execute('UPDATE snapshots SET graph_rev = graph_rev + 1 WHERE id=?', (row['id'],))
    applied = conn.execute('SELECT profile FROM snapshots WHERE id=?', (row['id'],)).fetchone()[0]
    print(f'{profiles.describe(applied)}; рёбра перестроены за {time.time() - t:.1f} с')
    print(json.dumps(counts, ensure_ascii=False, indent=1))
    print('всего рёбер:', sum(counts.values()))


def cmd_delete(conn, a):
    row = conn.execute('SELECT id FROM snapshots WHERE label_norm=?', (db.norm_label(a.label),)).fetchone()
    if not row:
        sys.exit(f'нет снимка {a.label}')
    indexer.delete_snapshot(conn, row[0])
    print('удалён')


def main(argv=None):
    p = argparse.ArgumentParser(prog='onec_code.cli')
    p.add_argument('--db', default=DEFAULT_DB)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('index')
    s.add_argument('path')
    s.add_argument('--label')
    s.add_argument('--full', action='store_true')
    s.add_argument('--workers', type=int)
    sub.add_parser('status')
    s = sub.add_parser('sizes')
    s.add_argument('-v', '--verbose', action='store_true')
    s = sub.add_parser('delete')
    s.add_argument('label')
    s = sub.add_parser('profile', help='профиль снимка: auto | none | up')
    s.add_argument('label')
    s.add_argument('profile')
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    conn = db.connect(a.db)
    db.init(conn)
    {'index': cmd_index, 'status': cmd_status, 'sizes': cmd_sizes, 'delete': cmd_delete,
     'profile': cmd_profile}[a.cmd](conn, a)


if __name__ == '__main__':
    main()
