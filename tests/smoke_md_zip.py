"""Smoke run of the metadata parser over a real dump (zip or folder).

Checks cross references against parsed code: form/subscription/scheduled job
handlers must exist as methods; defined types must expand.
Usage: python tests/smoke_md_zip.py /inbox/ut_typical.zip [workers]
"""
import collections
import sys
import time
from concurrent.futures import ProcessPoolExecutor

from onec_code import md_parser as mp
from onec_code.bsl_parser import parse_module
from onec_code.source import open_source


def work(args):
    path, items = args
    src = open_source(path)
    res = []
    for kind, p, owner in items:
        data = src.read(p)
        try:
            if kind == 'object':
                res.append((kind, owner, mp.parse_object(data)))
            elif kind == 'form':
                res.append((kind, owner, mp.parse_form(data)))
            elif kind == 'form_meta':
                res.append((kind, owner, mp.parse_form_meta(data)))
            elif kind == 'module':
                m = parse_module(data.decode('utf-8-sig'), form_module=owner.endswith('.Модуль') and '.Форма.' in owner
                                 or owner.startswith('ОбщаяФорма.'))
                res.append((kind, owner, {x.name.lower(): (x.export, x.directive) for x in m.methods}))
        except Exception as e:  # noqa: BLE001
            res.append(('error', p, repr(e)))
    return res, len(data) if items else 0


def main():
    path = sys.argv[1]
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    t0 = time.time()
    src = open_source(path)
    files = src.files()
    cfg = mp.parse_configuration(src.read('Configuration.xml'))
    t1 = time.time()
    kinds = collections.Counter(f.entry.kind for f in files)
    sizes = collections.Counter()
    for f in files:
        sizes[f.entry.kind] += f.size
    print(f'config: {cfg.name} {cfg.version} "{cfg.synonym}" ext={cfg.is_extension}; list {t1 - t0:.1f}s')
    for k, v in kinds.items():
        print(f'  {k:10} {v:>7,} files {sizes[k] / 1e6:8.1f} MB')
    items = [(f.entry.kind, f.entry.path, f.entry.module_name if f.entry.kind == 'module' else f.entry.owner)
             for f in files if f.entry.kind != 'config']
    chunks = [items[i::workers * 8] for i in range(workers * 8)]
    objects, forms, metas, modules, errors = {}, {}, {}, {}, []
    t2 = time.time()
    with ProcessPoolExecutor(workers) as ex:
        for res, _ in ex.map(work, [(path, c) for c in chunks]):
            for kind, owner, val in res:
                {'object': objects, 'form': forms, 'form_meta': metas, 'module': modules}.get(
                    kind, {}).__setitem__(owner, val) if kind != 'error' else errors.append((owner, val))
    print(f'parsed {len(items):,} files in {time.time() - t2:.1f}s; errors: {len(errors)}', errors[:5])

    by_type = collections.Counter(o.md_type for o in objects.values())
    print('objects by type:', dict(by_type.most_common()))
    n_attr = sum(len(o.attributes) for o in objects.values())
    n_ts = sum(len(o.tabular_sections) for o in objects.values())
    n_ts_attr = sum(len(t.attributes) for o in objects.values() for t in o.tabular_sections)
    print(f'attributes {n_attr:,}, tabular sections {n_ts:,} with {n_ts_attr:,} attributes, '
          f'enum values {sum(len(o.enum_values) for o in objects.values()):,}')

    # types
    defined = {o.name: o.props.get('Type', []) for o in objects.values() if o.md_type == 'ОпределяемыйТип'}
    chars = {o.name: o.props.get('Type', []) for o in objects.values() if o.md_type == 'ПланВидовХарактеристик'}
    tk = collections.Counter()
    other = collections.Counter()
    unresolved_def = collections.Counter()
    composite = 0
    for o in objects.values():
        for a in o.attributes + [x for t in o.tabular_sections for x in t.attributes]:
            if len(a.types) > 1:
                composite += 1
            for t in a.types:
                tk[t.split(':', 1)[0]] += 1
                if t.startswith('other:'):
                    other[t] += 1
            for t in mp.expand_types(a.types, defined, chars):
                if t.startswith('def:'):
                    unresolved_def[t] += 1
    print('attribute type kinds:', dict(tk), '; composite attrs:', composite)
    print('  other types:', other.most_common(8))
    print('  unresolved defined types after expansion:', unresolved_def.most_common(5))
    exp_sizes = [len([t for t in mp.expand_types(a.types, defined, chars) if t.startswith('ref:')])
                 for o in objects.values() for a in o.attributes if any(t.startswith('def:') for t in a.types)]
    print(f'  attrs with defined types: {len(exp_sizes):,}; refs after expansion: max {max(exp_sizes)}, '
          f'total {sum(exp_sizes):,}')

    # subscriptions
    subs = [o for o in objects.values() if o.md_type == 'ПодпискаНаСобытие']
    src_total = src_def = 0
    missing_h = []
    for s in subs:
        exp = mp.expand_types(s.props['Source'], defined)
        src_def += sum(1 for t in s.props['Source'] if t.startswith('def:'))
        src_total += len(exp)
        mod, meth = s.props['Handler']
        ms = modules.get(mod + '.Модуль', {})
        if meth.lower() not in ms:
            missing_h.append((s.name, mod, meth))
        elif not ms[meth.lower()][0]:
            missing_h.append((s.name, mod, meth, 'not export'))
    print(f'subscriptions {len(subs)}: sources after expansion {src_total:,} (defined-type sources {src_def}); '
          f'handler problems {len(missing_h)}', missing_h[:5])
    ev = collections.Counter(s.props['Event'] for s in subs)
    print('  events without ru name:', [e for e in ev if e not in mp.SUBSCRIPTION_EVENTS])
    jobs = [o for o in objects.values() if o.md_type == 'РегламентноеЗадание']
    miss_j = [(j.name, *j.props['MethodName']) for j in jobs
              if j.props['MethodName'][1].lower() not in modules.get(j.props['MethodName'][0] + '.Модуль', {})]
    print(f'scheduled jobs {len(jobs)}; handler problems {len(miss_j)}', miss_j[:5])

    docs = [o for o in objects.values() if o.md_type == 'Документ']
    rr = [len(d.props['RegisterRecords']) for d in docs]
    print(f'documents {len(docs)}: with movements {sum(1 for x in rr if x)}, register records total {sum(rr)}')

    # forms
    ftypes = collections.Counter(m['form_type'] for m in metas.values())
    fev = sum(len(f.events) for f in forms.values())
    iev = sum(len(e.events) for f in forms.values() for e in f.items)
    cev = sum(len(c.actions) for f in forms.values() for c in f.commands)
    items_total = sum(f.item_count for f in forms.values())
    print(f'forms {len(forms):,} ({dict(ftypes)}): form events {fev:,}, item events {iev:,} '
          f'(items with handlers {sum(len(f.items) for f in forms.values()):,} of {items_total:,}), '
          f'command actions {cev:,}')
    miss_f = []
    unknown_ev = collections.Counter()
    for owner, f in forms.items():
        ms = modules.get(owner + '.Модуль', {})
        for e in f.events + [x for i in f.items for x in i.events] + [x for c in f.commands for x in c.actions]:
            if e.handler.lower() not in ms:
                miss_f.append((owner, e.event, e.handler))
            if e.event != 'Action' and e.event not in mp.FORM_EVENTS:
                unknown_ev[e.event] += 1
    print(f'  form handlers not found in form module: {len(miss_f)}', miss_f[:6])
    print('  form events without ru name:', unknown_ev.most_common(15))
    print(f'total {time.time() - t0:.1f}s')


if __name__ == '__main__':
    main()
