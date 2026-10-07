import json
import zipfile

import pytest

from onec_code import db, graph, indexer
from tests.test_indexer import CONFIG, FORM, FORM_META, md, syn


def cm(name, body='', glob=False):
    return md(f'<CommonModule uuid="x"><Properties><Name>{name}</Name>{syn(name)}<Global>{str(glob).lower()}</Global>'
              f'<Server>true</Server></Properties></CommonModule>'), body


DOC = md(f'''<Document uuid="d"><Properties><Name>Заказ</Name>{syn("Заказ клиента")}
  <RegisterRecords><xr:Item xsi:type="xr:MDObjectRef">AccumulationRegister.Остатки</xr:Item></RegisterRecords>
  </Properties><ChildObjects>
  <Attribute uuid="a"><Properties><Name>Клиент</Name>{syn("Клиент")}
    <Type><v8:TypeSet>cfg:DefinedType.Контрагент</v8:TypeSet></Type></Properties></Attribute>
  <Form>ФормаДокумента</Form></ChildObjects></Document>''')
REG = md(f'<AccumulationRegister uuid="r"><Properties><Name>Остатки</Name>{syn("Остатки")}'
         '<RegisterType>Balance</RegisterType></Properties></AccumulationRegister>')
CAT = md(f'<Catalog uuid="c"><Properties><Name>Клиенты</Name>{syn("Клиенты")}</Properties></Catalog>')
DEF = md(f'<DefinedType uuid="t"><Properties><Name>Контрагент</Name>{syn("К")}<Type>'
         '<v8:Type>cfg:CatalogRef.Клиенты</v8:Type></Type></Properties></DefinedType>')
DEF_DOCS = md(f'<DefinedType uuid="t2"><Properties><Name>Документы</Name>{syn("Д")}<Type>'
              '<v8:Type>cfg:DocumentRef.Заказ</v8:Type></Type></Properties></DefinedType>')
SUB = md(f'''<EventSubscription uuid="s"><Properties><Name>ЗаказПередЗаписью</Name>{syn("П")}
  <Source><v8:TypeSet>cfg:DefinedType.Документы</v8:TypeSet></Source><Event>BeforeWrite</Event>
  <Handler>CommonModule.ОбщийСервер.ПередЗаписьюДокумента</Handler></Properties></EventSubscription>''')
JOB = md(f'''<ScheduledJob uuid="j"><Properties><Name>Сломанное</Name>{syn("С")}
  <MethodName>CommonModule.ОбщийСервер.НетТакогоМетода</MethodName></Properties></ScheduledJob>''')

OBJ_BSL = '''Процедура ОбработкаПроведения(Отказ, Режим)
	ОбщийСервер.Привет("мир");
	ОбщийСервер.НетТакого();
	ГлобФункция();
	Локальная();
	ОбщегоНазначения.ОбщийМодуль("ОбщийСервер").Привет("x");
	ОбщегоНазначения.ОбщийМодуль("НетМодуля").Метод();
	Движения.Остатки.Записывать = Истина;
	Документы.Заказ.ИзМенеджера();
	Выполнить("А()");
	Запрос = Новый Запрос("ВЫБРАТЬ * ИЗ РегистрНакопления.Остатки.Остатки(&Д) КАК О");
КонецПроцедуры

Процедура Локальная()
КонецПроцедуры
'''
MANAGER_BSL = '''Процедура ИзМенеджера() Экспорт
КонецПроцедуры

Процедура ЗарегистрироватьУчетныеМеханизмы(МеханизмыДокумента) Экспорт
	МеханизмыДокумента.Добавить("Продажи");
КонецПроцедуры

Функция ДанныеДокументаДляПроведения(Документ, Регистры, Доп = Неопределено) Экспорт
КонецФункции
'''
ENGINE_BSL = '''Функция УчетныеМеханизмыКонфигурации() Экспорт
	М = Новый Соответствие;
	М.Вставить("Продажи", "Продажи");
	Возврат М;
КонецФункции

Процедура ОтразитьДвижения(Таблицы, Движения, ИмяРегистра) Экспорт
КонецПроцедуры

Процедура ОбщийМодуль(Имя) Экспорт
КонецПроцедуры
'''
SALES_BSL = '''Процедура ОтразитьДвижения(Таблицы, Движения, Отказ) Экспорт
	ПроведениеДокументов.ОтразитьДвижения(Таблицы, Движения, "Остатки");
КонецПроцедуры
'''
FORM_BSL = '''&НаКлиенте
Процедура ПриОткрытии(Отказ)
КонецПроцедуры
'''     # КлиентПриИзменении is missing -> broken form handler


def base_files():
    f = {'Configuration.xml': CONFIG, 'Documents/Заказ.xml': DOC,
         'Documents/Заказ/Ext/ObjectModule.bsl': OBJ_BSL, 'Documents/Заказ/Ext/ManagerModule.bsl': MANAGER_BSL,
         'Documents/Заказ/Forms/ФормаДокумента.xml': FORM_META,
         'Documents/Заказ/Forms/ФормаДокумента/Ext/Form.xml': FORM,
         'Documents/Заказ/Forms/ФормаДокумента/Ext/Form/Module.bsl': FORM_BSL,
         'AccumulationRegisters/Остатки.xml': REG, 'Catalogs/Клиенты.xml': CAT,
         'DefinedTypes/Контрагент.xml': DEF, 'DefinedTypes/Документы.xml': DEF_DOCS,
         'EventSubscriptions/ЗаказПередЗаписью.xml': SUB, 'ScheduledJobs/Сломанное.xml': JOB}
    for name, (x, b) in {
        'ОбщийСервер': cm('ОбщийСервер', 'Функция Привет(Имя) Экспорт\nКонецФункции\n'
                                         'Процедура ПередЗаписьюДокумента(Источник, Отказ) Экспорт\nКонецПроцедуры\n'),
        'Глобальный': cm('Глобальный', 'Функция ГлобФункция() Экспорт\nКонецФункции\n', glob=True),
        'ОбщегоНазначения': cm('ОбщегоНазначения', 'Функция ОбщийМодуль(Имя) Экспорт\nКонецФункции\n'),
        'ПроведениеДокументов': cm('ПроведениеДокументов', ENGINE_BSL),
        'Продажи': cm('Продажи', SALES_BSL),
    }.items():
        f[f'CommonModules/{name}.xml'] = x
        f[f'CommonModules/{name}/Ext/Module.bsl'] = b
    return f


EXT_CONFIG = md(f'''<Configuration uuid="e"><Properties><ObjectBelonging>Adopted</ObjectBelonging>
  <Name>Расширение</Name>{syn("Расш")}<NamePrefix>Р_</NamePrefix>
  <ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose><Version>1.0</Version>
  </Properties></Configuration>''')
EXT_DOC = md('<Document uuid="d"><Properties><ObjectBelonging>Adopted</ObjectBelonging><Name>Заказ</Name>'
             '</Properties><ChildObjects/></Document>')
EXT_OBJ = '''&Перед("ОбработкаПроведения")
Процедура Р_ОбработкаПроведения(Отказ, Режим)
КонецПроцедуры

&Вместо("НетВОсновной")
Процедура Р_Нет()
КонецПроцедуры
'''


def write_zip(path, files):
    with zipfile.ZipFile(path, 'w') as z:
        for n, t in files.items():
            z.writestr(n, ('﻿' + t).encode('utf-8'))


@pytest.fixture
def env(tmp_path):
    conn = db.connect(str(tmp_path / 'i.db'))
    db.init(conn)
    z = str(tmp_path / 'base.zip')
    write_zip(z, base_files())
    sid = indexer.create_snapshot(conn, 'base', 'upload', z)
    # the synthetic configuration is named "Тест": force the УП profile to test its heuristics
    conn.execute("UPDATE snapshots SET profile_override='up' WHERE id=?", (sid,))
    st = indexer.index_snapshot(conn, sid, workers=1)
    yield conn, sid, st, tmp_path
    conn.close()


def test_universal_without_profile(env):
    conn, sid, st, _ = env
    assert st['profile'] == 'up'
    with_profile = graph.build_edges(conn, sid)
    universal = graph.build_edges(conn, sid, profile_override='none')
    assert conn.execute('SELECT profile FROM snapshots WHERE id=?', (sid,)).fetchone()[0] is None
    assert 'posting_mechanism:heuristic' in with_profile and 'posting_mechanism:heuristic' not in universal
    # literal register names in ОтразитьДвижения are a profile heuristic; Движения.X is universal
    assert with_profile['writes_register:heuristic'] == 1 and 'writes_register:heuristic' not in universal
    assert universal['writes_register:exact'] == 1
    for k in ('calls:exact', 'event_subscription:exact', 'form_handler:exact', 'attr_type:exact', 'register_records:exact'):
        assert universal[k] == with_profile[k], k
    # auto: unknown configuration name -> no profile
    conn.execute("UPDATE snapshots SET profile_override=NULL WHERE id=?", (sid,))
    assert 'posting_mechanism:heuristic' not in graph.build_edges(conn, sid)


def node(conn, sid, full):
    """Resolve 'Модуль.Полное.Имя::Метод' or object/attribute full names to node id."""
    if '::' in full:
        mod, m = full.split('::')
        return conn.execute("SELECT n.id FROM nodes n JOIN nodes p ON p.id=n.parent_id JOIN content_methods cm "
                            "ON cm.id=n.content_id WHERE n.snapshot_id=? AND p.full_name=? AND cm.name=?",
                            (sid, mod, m)).fetchone()[0]
    return conn.execute('SELECT id FROM nodes WHERE snapshot_id=? AND full_name=?', (sid, full)).fetchone()[0]


def label(conn, nid):
    r = conn.execute('SELECT n.kind, n.full_name, n.name, p.full_name, cm.name FROM nodes n '
                     'LEFT JOIN nodes p ON p.id=n.parent_id LEFT JOIN content_methods cm '
                     "ON n.kind='method' AND cm.id=n.content_id WHERE n.id=?", (nid,)).fetchone()
    if r[0] == 'method':
        return f'{r[3]}::{r[4]}'
    return r[1] or f'{r[3]}/{r[2]}'


def out_edges(conn, frm, kind=None):
    q = 'SELECT to_id, kind, confidence, attrs FROM edges WHERE from_id=?' + (' AND kind=?' if kind else '')
    res = []
    for to, k, c, a in conn.execute(q, (frm, kind) if kind else (frm,)):
        a = json.loads(a) if a else {}
        res.append((k, label(conn, to) if to else a.get('target'), c))
    return sorted(res, key=str)


OM = 'Документ.Заказ.МодульОбъекта'


def test_call_edges(env):
    conn, sid, st, _ = env
    got = out_edges(conn, node(conn, sid, f'{OM}::ОбработкаПроведения'))
    assert ('calls', 'ОбщийМодуль.ОбщийСервер.Модуль::Привет', 'exact') in got
    assert ('calls', 'ОбщийМодуль.ОбщийСервер.Модуль::Привет', 'heuristic') in got      # via ОбщийМодуль("…")
    assert ('calls', 'ОбщийМодуль.ОбщийСервер.НетТакого', 'broken') in got
    assert ('calls', 'ОбщийМодуль.Глобальный.Модуль::ГлобФункция', 'exact') in got
    assert ('calls', f'{OM}::Локальная', 'exact') in got
    assert ('calls', 'ОбщийМодуль.НетМодуля.Метод', 'broken') in got
    assert ('calls', 'Документ.Заказ.МодульМенеджера::ИзМенеджера', 'exact') in got
    assert ('calls', None, 'unresolved') in got                                          # Выполнить
    assert ('writes_register', 'РегистрНакопления.Остатки', 'exact') in got
    assert ('reads_table', 'РегистрНакопления.Остатки', 'heuristic') in got


def test_metadata_and_form_edges(env):
    conn, sid, st, _ = env
    doc = node(conn, sid, 'Документ.Заказ')
    assert ('register_records', 'РегистрНакопления.Остатки', 'exact') in out_edges(conn, doc)
    # subscription source given by a DefinedType
    assert ('event_subscription', 'ОбщийМодуль.ОбщийСервер.Модуль::ПередЗаписьюДокумента', 'exact') in \
        out_edges(conn, doc, 'event_subscription')
    attrs = json.loads(conn.execute("SELECT attrs FROM edges WHERE kind='event_subscription'").fetchone()[0])
    assert attrs == {'event': 'ПередЗаписью', 'sub': 'ЗаказПередЗаписью'}
    job = node(conn, sid, 'РегламентноеЗадание.Сломанное')
    assert out_edges(conn, job) == [('scheduled_job', 'ОбщийМодуль.ОбщийСервер.НетТакогоМетода', 'broken')]
    # attribute type through a defined type
    attr = conn.execute("SELECT id FROM nodes WHERE snapshot_id=? AND kind='attribute' AND name='Клиент'",
                        (sid,)).fetchone()[0]
    assert out_edges(conn, attr) == [('attr_type', 'Справочник.Клиенты', 'exact')]
    form = node(conn, sid, 'Документ.Заказ.Форма.ФормаДокумента')
    fe = out_edges(conn, form)
    assert ('form_handler', 'Документ.Заказ.Форма.ФормаДокумента.Модуль::ПриОткрытии', 'exact') in fe
    assert ('form_attr', 'Документ.Заказ/Клиент', 'exact') in fe
    item = conn.execute("SELECT id FROM nodes WHERE kind='form_item' AND snapshot_id=?", (sid,)).fetchone()[0]
    assert out_edges(conn, item) == [('form_handler', 'КлиентПриИзменении', 'broken')]


def test_posting_mechanism(env):
    conn, sid, st, _ = env
    doc = node(conn, sid, 'Документ.Заказ')
    got = out_edges(conn, doc, 'posting_mechanism')
    assert got == [('posting_mechanism', 'Документ.Заказ.МодульМенеджера::ДанныеДокументаДляПроведения', 'heuristic'),
                   ('posting_mechanism', 'ОбщийМодуль.Продажи.Модуль::ОтразитьДвижения', 'heuristic')]
    sales = node(conn, sid, 'ОбщийМодуль.Продажи.Модуль::ОтразитьДвижения')
    assert ('writes_register', 'РегистрНакопления.Остатки', 'heuristic') in out_edges(conn, sales)


def test_traversals(env):
    conn, sid, st, _ = env
    start = node(conn, sid, 'Документ.Заказ')
    d = graph.reach(conn, [start], 'out', ['posting_mechanism', 'writes_register'], 3)
    reg = node(conn, sid, 'РегистрНакопления.Остатки')
    assert d[reg] == 2
    # who writes the register (reverse)
    d = graph.reach(conn, [reg], 'in', ['writes_register', 'posting_mechanism', 'register_records'], 2)
    assert d[start] == 1
    paths = graph.call_paths(conn, [node(conn, sid, f'{OM}::ОбработкаПроведения')],
                             [node(conn, sid, 'ОбщийМодуль.ОбщийСервер.Модуль::Привет')])
    assert [[label(conn, x) for x in p] for p in paths] == [
        [f'{OM}::ОбработкаПроведения', 'ОбщийМодуль.ОбщийСервер.Модуль::Привет']]
    # cycles are safe
    a, b = node(conn, sid, f'{OM}::ОбработкаПроведения'), node(conn, sid, f'{OM}::Локальная')
    conn.execute("INSERT INTO edges(snapshot_id, from_id, to_id, kind, confidence) VALUES(?,?,?,'calls','exact')",
                 (sid, b, a))
    d = graph.reach(conn, [a], 'out', ['calls'], 5)
    assert d[a] == 0 and d[b] == 1


DCS = '''<?xml version="1.0" encoding="UTF-8"?>
<DataCompositionSchema xmlns="http://v8.1c.ru/8.1/data-composition-system/schema"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dataSource><name>ИсточникДанных1</name><dataSourceType>Local</dataSourceType></dataSource>
  <dataSet xsi:type="DataSetQuery"><name>Остатки</name><dataSource>ИсточникДанных1</dataSource>
    <query>ВЫБРАТЬ
	О.КоличествоОстаток КАК Количество
ИЗ
	РегистрНакопления.Остатки.Остатки(&amp;Дата, {(Склад).*}) КАК О</query></dataSet>
  <dataSet xsi:type="DataSetUnion"><name>Объединение</name>
    <item xsi:type="DataSetQuery"><name>Заказы</name><query>ВЫБРАТЬ З.Ссылка ИЗ Документ.Заказ КАК З</query></item>
    <item xsi:type="DataSetObject"><name>Внешние</name><objectName>Т</objectName></item>
  </dataSet>
</DataCompositionSchema>'''
REPORT = md(f'<Report uuid="o"><Properties><Name>Ведомость</Name>{syn("Ведомость")}</Properties>'
            '<ChildObjects><Template>Схема</Template><Template>Печать</Template></ChildObjects></Report>')


def test_dcs_schema(tmp_path):
    """Data composition schemas are indexed like modules: data sets -> reads_table, FTS, outline, diff."""
    from onec_code import tools
    files = base_files()
    files['Reports/Ведомость.xml'] = REPORT
    files['Reports/Ведомость/Templates/Схема/Ext/Template.xml'] = DCS
    files['Reports/Ведомость/Templates/Печать/Ext/Template.xml'] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<document xmlns="http://v8.1c.ru/8.2/data/spreadsheet">'
        'Документ.Заказ РегистрНакопления.Остатки</document>')
    z = str(tmp_path / 'r.zip')
    write_zip(z, files)
    conn = db.connect(str(tmp_path / 'i.db'))
    db.init(conn)
    sid = indexer.create_snapshot(conn, 'r', 'upload', z)
    st = indexer.index_snapshot(conn, sid, workers=1)
    assert st['dcs'] == 1 and st['dcs_datasets'] == 4          # Остатки, Объединение, Заказы, Внешние
    sch = 'Отчет.Ведомость.Макет.Схема'
    reads = out_edges(conn, node(conn, sid, f'{sch}::Остатки'), 'reads_table')
    assert reads == [('reads_table', 'РегистрНакопления.Остатки', 'heuristic')]
    assert out_edges(conn, node(conn, sid, f'{sch}::Заказы'), 'reads_table') == [
        ('reads_table', 'Документ.Заказ', 'heuristic')]
    t = tools.who_reads(conn, 'РегистрНакопления.Остатки', source='dcs')
    assert 'наборы данных схем СКД (отчёты и др.) 1' in t and 'Схема.Остатки' in t and '[СКД]' in t
    assert 'ОбработкаПроведения' not in t                     # code readers filtered out
    assert 'Набор данных Заказы (запрос, в объединении «Объединение»)' in tools.get_module_outline(conn, sch)
    assert 'КоличествоОстаток' in tools.get_procedure(conn, sch, 'Остатки')
    assert 'Схема' in tools.search_code(conn, 'КоличествоОстаток')
    # spreadsheet template: not a schema, not indexed
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE full_name LIKE '%Печать%'").fetchone()[0] == 0
    # the report's own module (none here) is not shadowed by the schema in module lookups
    assert 'Отчет.Ведомость.Макет.Схема' in tools.get_module(conn, 'Отчет.Ведомость.Макет.Схема')
    conn.close()


def test_unf_profile(tmp_path):
    """УНФ: ПроведениеДокументовУНФ.ОтразитьДвижения("Регистр", …) -> writes_register, exact for own RegisterRecords."""
    files = base_files()
    files['Documents/Заказ/Ext/ObjectModule.bsl'] = (
        'Процедура ОбработкаПроведения(Отказ, Режим)\n'
        '\tПроведениеДокументовУНФ.ОтразитьДвижения("Остатки", ТаблицыДляДвижений, Движения, Отказ);\n'
        'КонецПроцедуры\n')
    files['CommonModules/ПроведениеДокументовУНФ.xml'] = cm('ПроведениеДокументовУНФ')[0]
    files['CommonModules/ПроведениеДокументовУНФ/Ext/Module.bsl'] = (
        'Процедура ОтразитьДвижения(ИмяРегистра, Таблицы, Движения, Отказ) Экспорт\nКонецПроцедуры\n')
    files['CommonModules/Сервис.xml'] = cm('Сервис')[0]
    files['CommonModules/Сервис/Ext/Module.bsl'] = (
        'Процедура Перезаписать() Экспорт\n'
        '\tПроведениеДокументовУНФ.ОтразитьДвижения("Остатки", Т, Д, О);\nКонецПроцедуры\n')
    z = str(tmp_path / 'u.zip')
    write_zip(z, files)
    conn = db.connect(str(tmp_path / 'i.db'))
    db.init(conn)
    sid = indexer.create_snapshot(conn, 'u', 'upload', z)
    conn.execute("UPDATE snapshots SET profile_override='unf' WHERE id=?", (sid,))
    st = indexer.index_snapshot(conn, sid, workers=1)
    assert st['profile'] == 'unf'
    assert ('writes_register', 'РегистрНакопления.Остатки', 'exact') in \
        out_edges(conn, node(conn, sid, f'{OM}::ОбработкаПроведения'), 'writes_register')
    # outside the document's own modules -> heuristic
    assert out_edges(conn, node(conn, sid, 'ОбщийМодуль.Сервис.Модуль::Перезаписать'), 'writes_register') == [
        ('writes_register', 'РегистрНакопления.Остатки', 'heuristic')]
    # universal only: no literal-based edge
    graph.build_edges(conn, sid, profile_override='none')
    assert out_edges(conn, node(conn, sid, f'{OM}::ОбработкаПроведения'), 'writes_register') == []
    conn.close()


def test_module_without_source(tmp_path):
    """Module.bin (shipped without source): calls into it are unresolved, not broken."""
    from onec_code import tools
    files = base_files()
    files['Documents/Заказ/Ext/ObjectModule.bsl'] = (
        'Процедура ОбработкаПроведения(Отказ, Режим)\n\tЗакрытый.Проверить(1);\n'
        '\tОбщегоНазначения.ОбщийМодуль("Закрытый").Другой();\nКонецПроцедуры\n')
    files['CommonModules/Закрытый.xml'] = cm('Закрытый')[0]
    files['EventSubscriptions/ЗаказПередЗаписью.xml'] = SUB.replace('ОбщийСервер.ПередЗаписьюДокумента',
                                                                     'Закрытый.ПодпискаПередЗаписью')
    z = str(tmp_path / 'b.zip')
    write_zip(z, files)
    with zipfile.ZipFile(z, 'a') as zf:
        zf.writestr('CommonModules/Закрытый/Ext/Module.bin', bytes(range(256)) * 4)
    conn = db.connect(str(tmp_path / 'i.db'))
    db.init(conn)
    sid = indexer.create_snapshot(conn, 'b', 'upload', z)
    st = indexer.index_snapshot(conn, sid, workers=1)
    assert st['nosrc_modules'] == 1
    mod = node(conn, sid, 'ОбщийМодуль.Закрытый.Модуль')
    got = conn.execute("SELECT kind, confidence, attrs FROM edges WHERE to_id=? ORDER BY kind, line", (mod,)).fetchall()
    assert [(k, c, json.loads(a)['method']) for k, c, a in got] == [
        ('calls', 'unresolved', 'Проверить'), ('calls', 'unresolved', 'Другой'),
        ('handler', 'unresolved', 'ПодпискаПередЗаписью')]
    assert not conn.execute("SELECT 1 FROM edges WHERE snapshot_id=? AND confidence='broken' AND kind!='form_handler' "
                            "AND kind!='scheduled_job'", (sid,)).fetchone()
    assert 'без исходных текстов' in tools.get_module(conn, 'Закрытый')
    assert 'вызывающих 1' in tools.find_callers(conn, 'Закрытый.Проверить')
    calls = tools.find_calls(conn, OM, 'ОбработкаПроведения')
    assert 'Закрытый' in calls and '.Проверить' in calls and 'модуль без исходных текстов' in calls
    conn.close()
    from onec_code.layout import classify
    e = classify('Documents/Заказ/Forms/Ф/Ext/Form/Module.bin')
    assert e.kind == 'module' and e.nosrc and e.owner == 'Документ.Заказ.Форма.Ф'
    assert classify('Reports/Отчет/Templates/М/Ext/Template.bin') is None


def test_extension(env):
    conn, sid, st, tmp = env
    z = str(tmp / 'ext.zip')
    write_zip(z, {'Configuration.xml': EXT_CONFIG, 'Documents/Заказ.xml': EXT_DOC,
                  'Documents/Заказ/Ext/ObjectModule.bsl': EXT_OBJ})
    eid = indexer.create_snapshot(conn, 'ext', 'upload', z)
    indexer.index_snapshot(conn, eid, workers=1)
    assert conn.execute('SELECT base_snapshot_id FROM snapshots WHERE id=?', (eid,)).fetchone()[0] == sid
    m = node(conn, eid, f'{OM}::Р_ОбработкаПроведения')
    to = conn.execute("SELECT to_id, attrs FROM edges WHERE from_id=? AND kind='extension_intercepts'", (m,)).fetchone()
    assert label(conn, to[0]) == f'{OM}::ОбработкаПроведения' and json.loads(to[1]) == {'type': 'Перед'}
    assert node(conn, sid, f'{OM}::ОбработкаПроведения') == to[0]
    bad = node(conn, eid, f'{OM}::Р_Нет')
    assert out_edges(conn, bad) == [('extension_intercepts', f'{OM.lower()}.НетВОсновной', 'broken')]
    # base reindexed (node ids change) -> extension links rebuilt
    indexer.index_snapshot(conn, sid, workers=1, full=True)
    to2 = conn.execute("SELECT to_id FROM edges WHERE from_id=(SELECT id FROM nodes WHERE snapshot_id=? AND "
                       "content_id=(SELECT content_id FROM nodes WHERE id=?)) AND kind='extension_intercepts' "
                       "AND confidence='exact'", (eid, m)).fetchone()[0]
    assert to2 == node(conn, sid, f'{OM}::ОбработкаПроведения')
