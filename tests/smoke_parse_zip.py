"""Smoke run of the BSL parser over every module of a real configuration archive.

Usage: python tests/smoke_parse_zip.py /inbox/ut_typical.zip [workers]
"""
import collections
import re
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor

from onec_code.bsl_parser import parse_module

NAIVE_RE = re.compile(r'^\s*(?:Асинх\s+)?(?:Процедура|Функция)\s+[\wА-Яа-яЁё]+\s*\(', re.M | re.I)


def work(args):
    path, names = args
    z = zipfile.ZipFile(path)
    st = collections.Counter()
    dyn = collections.Counter()
    errs = []
    for n in names:
        text = z.read(n).decode('utf-8-sig')
        m = parse_module(text)
        st['modules'] += 1
        st['methods'] += len(m.methods)
        st['naive'] += len(NAIVE_RE.findall(text))
        st['broken'] += sum(x.broken for x in m.methods)
        st['export'] += sum(x.export for x in m.methods)
        st['async'] += sum(x.is_async for x in m.methods)
        st['no_directive_in_form'] += sum(x.directive is None for x in m.methods) if '/Form/' in n else 0
        for r in m.body_refs + [r for x in m.methods for r in x.refs]:
            st['ref_' + r.kind] += 1
            if r.kind == 'dyn':
                d = r.detail
                dyn[(d['type'], d.get('reason') or ('ok' if d.get('module') or d.get('method') else '-'))] += 1
        if m.errors:
            errs.append((n, m.errors[:3]))
    return st, dyn, errs


def main():
    path = sys.argv[1]
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    names = [n for n in zipfile.ZipFile(path).namelist() if n.endswith('.bsl')]
    chunks = [names[i::workers * 8] for i in range(workers * 8)]
    t = time.time()
    st, dyn, errs = collections.Counter(), collections.Counter(), []
    with ProcessPoolExecutor(workers) as ex:
        for a, b, c in ex.map(work, [(path, c) for c in chunks]):
            st += a
            dyn += b
            errs += c
    print(f'parsed in {time.time() - t:.1f}s with {workers} workers')
    for k, v in sorted(st.items()):
        print(f'  {k:24} {v:>10,}')
    print('dynamic refs by (type, status):')
    for k, v in dyn.most_common():
        print(f'  {k!s:40} {v:>8,}')
    print(f'modules with parse errors: {len(errs)}')
    for n, e in errs[:15]:
        print('  ', n, e)


if __name__ == '__main__':
    main()
