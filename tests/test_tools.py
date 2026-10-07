import time
import zipfile

import pytest

from onec_code import db, indexer, tools
from onec_code.query import NotFound, resolve_node, snapshots
from tests.test_graph import EXT_CONFIG, EXT_DOC, EXT_OBJ, base_files, write_zip


@pytest.fixture
def env(tmp_path):
    conn = db.connect(str(tmp_path / 'i.db'))
    db.init(conn)
    z = str(tmp_path / 'base.zip')
    write_zip(z, base_files())
    sid = indexer.create_snapshot(conn, 'УТ 11.5', 'upload', z)
    conn.execute("UPDATE snapshots SET profile_override='up' WHERE id=?", (sid,))
    indexer.index_snapshot(conn, sid, workers=1)
    yield conn, tmp_path
    conn.close()


def test_call_path_edge_kinds(env):
    conn, _ = env
    form = 'Документ.Заказ.Форма.ФормаДокумента'
    t = tools.call_path(conn, form, 'РегистрНакопления.Остатки')
    assert 'не найден' in t and 'edge_kinds' in t
    t = tools.call_path(conn, form, 'РегистрНакопления.Остатки',
                        edge_kinds=['form_handler', 'calls', 'posting_mechanism', 'writes_register'])
    assert (f'{form} → [form_handler] {form}.ПриОткрытии → ~эвристика [запись/проведение документа] Документ.Заказ'
            ' → ~эвристика [posting_mechanism] ОбщийМодуль.Продажи.ОтразитьДвижения'
            ' → ~эвристика [writes_register] РегистрНакопления.Остатки') in t
    # a form item as the start node
    t = tools.call_path(conn, f'{form}.Элементы.Клиент', 'Документ.Заказ', edge_kinds=['form_handler', 'posting_mechanism'])
    assert 'не найден' in t          # its handler is missing in the module (broken edge)


def test_profile_shown(env):
    conn, _ = env
    assert 'Профиль: УП' in tools.who_writes(conn, 'РегистрНакопления.Остатки')
    t = tools.on_write_or_post(conn, 'Документ.Заказ')
    assert 'Профиль: УП' in t and 'запись найдена в коде обработчиков выше: 1' in t
    assert 'профиль: up' in tools.list_configs(conn)


def test_config_param(env):
    conn, _ = env
    assert [s.label for s in snapshots(conn, 'ут   11.5')] == ['УТ 11.5']
    assert [s.label for s in snapshots(conn, 'тест')] == ['УТ 11.5']            # configuration name
    assert [s.label for s in snapshots(conn, 'Тест@1.0.0.1')] == ['УТ 11.5']
    with pytest.raises(NotFound) as e:
        snapshots(conn, 'УТ 11.6')
    assert 'УТ 11.5' in str(e.value)


def test_name_resolution(env):
    conn, _ = env
    s = snapshots(conn)[0]
    r = lambda t, w=None: resolve_node(conn, s, t, w)
    assert r('Документ.Заказ').kind == 'object'
    assert r('Document.Заказ').kind == 'object'
    assert r('документы.заказ').label == 'Документ.Заказ'
    assert r('Заказ клиента').label == 'Документ.Заказ'                      # synonym
    assert r('Документ.Заказ.МодульМенеджера').label == 'Документ.Заказ.МодульМенеджера'
    assert r('Document.Заказ.ObjectModule').kind == 'module'
    assert r('ОбщийМодуль.ОбщийСервер').kind == 'object'
    assert r('ОбщийСервер', 'module').label == 'ОбщийМодуль.ОбщийСервер.Модуль'
    assert r('ОбщийСервер.Привет').label == 'ОбщийМодуль.ОбщийСервер.Привет'
    assert r('Документ.Заказ.МодульОбъекта.Локальная').kind == 'method'
    assert r('Документ.Заказ.ИзМенеджера').label == 'Документ.Заказ.МодульМенеджера.ИзМенеджера'
    assert r('Документ.Заказ.Форма.ФормаДокумента').kind == 'form'
    assert r('Документ.Заказ.Форма.ФормаДокумента', 'module').label == 'Документ.Заказ.Форма.ФормаДокумента.Модуль'
    assert r('Документ.Заказ.Форма.ФормаДокумента.ПриОткрытии').kind == 'method'
    assert r('Документ.Заказ.Клиент').kind == 'attribute'
    with pytest.raises(NotFound) as e:
        r('Документ.Закз')
    assert 'Документ.Заказ' in e.value.suggestions
    with pytest.raises(NotFound) as e:
        r('ОбщийСервер.Првет')
    assert 'ОбщийМодуль.ОбщийСервер.Привет' in e.value.suggestions
    with pytest.raises(NotFound) as e:
        r('Документ.Заказ', 'module')                     # two modules -> ask which
    assert 'Документ.Заказ.МодульОбъекта' in e.value.suggestions


def test_catalog_and_code_tools(env):
    conn, _ = env
    t = tools.list_configs(conn)
    assert 'УТ 11.5' in t and 'v1.0.0.1' in t
    t = tools.list_objects(conn, 'заказ')
    assert 'Документ.Заказ — Заказ клиента' in t
    assert 'Документ.Заказ' in tools.list_objects(conn, None, 'Document')
    t = tools.get_object(conn, 'Документ.Заказ')
    assert 'Клиент: ОпределяемыйТип.Контрагент' in t
    assert 'Движения (1): РегистрНакопления.Остатки' in t
    assert 'ПередЗаписью: ЗаказПередЗаписью → ОбщийМодуль.ОбщийСервер.ПередЗаписьюДокумента' in t
    assert 'Продажи → ОбщийМодуль.Продажи.ОтразитьДвижения' in t
    t = tools.search_code(conn, 'ОбщийСервер.Привет')
    assert 'Документ.Заказ.МодульОбъекта.ОбработкаПроведения стр. 2' in t
    t = tools.find_procedure(conn, 'прив')
    assert 'по началу имени' in t and 'Функция Привет(Имя) Экспорт' in t
    assert 'по части имени' in tools.find_procedure(conn, 'вет')
    t = tools.find_procedure(conn, 'ОтразитьДвижения', module_hint='продаж')
    assert 'ОбщийМодуль.Продажи' in t and 'ПроведениеДокументов' not in t
    t = tools.get_procedure(conn, 'Документ.Заказ.МодульОбъекта', 'Локальная')
    assert '14: Процедура Локальная()' in t and '15: КонецПроцедуры' in t
    t = tools.get_module_outline(conn, 'ПроведениеДокументов')
    assert 'Функция УчетныеМеханизмыКонфигурации() Экспорт' in t
    t = tools.get_module(conn, 'Документ.Заказ.МодульОбъекта', start_line=2, end_line=3)
    assert t.splitlines()[1].startswith('2: \tОбщийСервер.Привет')
    with pytest.raises(NotFound):
        tools.find_procedure(conn, 'НетТакогоМетодаНигде')


def test_graph_tools(env):
    conn, _ = env
    t = tools.find_callers(conn, 'Привет')
    assert '← [calls] Документ.Заказ.МодульОбъекта.ОбработкаПроведения (стр. 2)' in t
    assert 'динамически: ОбщийМодуль' in t
    t = tools.find_calls(conn, 'Документ.Заказ.МодульОбъекта', 'ОбработкаПроведения')
    assert '[calls ✗битая (динамически: ОбщийМодуль)] ОбщийМодуль.НетМодуля.Метод' in t
    assert '[calls ✗битая] ОбщийМодуль.ОбщийСервер.НетТакого' in t
    assert '[writes_register] РегистрНакопления.Остатки' in t
    t = tools.who_writes(conn, 'РегистрНакопления.Остатки')
    assert 'Документ.Заказ' in t and 'ОбщийМодуль.Продажи.ОтразитьДвижения' in t and 'механизм Продажи' in t
    t = tools.who_reads(conn, 'РегистрНакопления.Остатки')
    assert 'Остатки 1' in t
    t = tools.on_write_or_post(conn, 'Документ.Заказ')
    assert '2. ПередЗаписью' in t and 'подписка ЗаказПередЗаписью' in t
    assert 'механизм проведения Продажи' in t and 'пишет регистры (по коду): РегистрНакопления.Остатки' in t
    assert tools.on_write_or_post(conn, 'Документ.Заказ', 'BeforeWrite').count('ПередЗаписью') >= 1
    t = tools.get_dependencies(conn, 'Документ.Заказ', 'out', ['register_records', 'posting_mechanism'], 1)
    assert '[register_records] РегистрНакопления.Остатки' in t
    t = tools.impact(conn, 'ОбщийСервер.Привет')
    assert 'Документ.Заказ.МодульОбъекта.ОбработкаПроведения' in t
    t = tools.call_path(conn, 'Документ.Заказ.МодульОбъекта.ОбработкаПроведения', 'ОбщийСервер.Привет')
    assert 'Документ.Заказ.МодульОбъекта.ОбработкаПроведения →' in t
    t = tools.find_broken_refs(conn)
    assert 'КлиентПриИзменении' in t and 'НетТакогоМетода' in t and 'НетМодуля' in t
    t = tools.find_form_usages(conn, 'Документ.Заказ.Клиент')
    assert 'Документ.Заказ.Форма.ФормаДокумента: элементы Клиент' in t
    t = tools.get_form_element(conn, 'Документ.Заказ.Форма.ФормаДокумента', 'клиент')
    assert 'Путь данных: Объект.Клиент' in t and 'Реквизит объекта: Документ.Заказ.Клиент' in t
    assert '✗битая' in t


def test_versions(env):
    conn, tmp = env
    files = base_files()
    files['CommonModules/ОбщийСервер/Ext/Module.bsl'] = files['CommonModules/ОбщийСервер/Ext/Module.bsl'].replace(
        'Функция Привет(Имя) Экспорт\n', 'Функция Привет(Имя) Экспорт\n\tВозврат "Привет, " + Имя;\n') + \
        'Процедура Новая()\nКонецПроцедуры\n'
    files['Documents/Заказ.xml'] = files['Documents/Заказ.xml'].replace(
        '<Form>ФормаДокумента</Form>', '<Form>ФормаДокумента</Form><Form>ФормаСписка</Form>')
    # dump noise: an empty module file appears, a form changes only in markup
    files['Catalogs/Клиенты/Ext/ObjectModule.bsl'] = '\n'
    files['Documents/Заказ/Forms/ФормаДокумента/Ext/Form.xml'] += '\n'
    z = str(tmp / 'v2.zip')
    write_zip(z, files)
    sid = indexer.create_snapshot(conn, 'УТ 11.6', 'upload', z)
    indexer.index_snapshot(conn, sid, workers=1)
    t = tools.diff_procedure(conn, 'ОбщийСервер', 'Привет', 'УТ 11.5', 'ут 11.6')
    assert '+\tВозврат "Привет, " + Имя;' in t
    assert 'идентичны' in tools.diff_procedure(conn, 'Документ.Заказ.МодульОбъекта', 'Локальная', 'УТ 11.5', 'УТ 11.6')
    t = tools.diff_module(conn, 'ОбщийСервер', 'УТ 11.5', 'УТ 11.6')
    assert 'добавлено 1' in t and 'Новая' in t and 'изменено 1' in t
    t = tools.diff_configs(conn, 'УТ 11.5', 'УТ 11.6')
    assert 'модуль изменён: ОбщийМодуль.ОбщийСервер' in t and 'формы: +ФормаСписка' in t
    assert 'пустые файлы модулей появились/исчезли: 1' in t and 'Клиенты' not in t.split(')')[-1]
    assert 'только в разметке' in t and 'форма изменён' not in t
    # without config: results carry snapshot tags, identical code shown once
    t = tools.get_procedure(conn, 'Документ.Заказ.МодульОбъекта', 'Локальная')
    assert '[УТ 11.5 1.0.0.1] [УТ 11.6 1.0.0.1]' in t
    t = tools.get_procedure(conn, 'ОбщийСервер', 'Привет')
    assert 'В других снимках текст отличается' in t


def test_ids_not_reused_after_delete(env):
    conn, tmp = env
    z = str(tmp / 'c.zip')
    write_zip(z, base_files())
    s2 = indexer.create_snapshot(conn, 'копия 1', 'upload', z)
    indexer.index_snapshot(conn, s2, workers=1)
    tools.get_procedure(conn, 'ОбщийСервер', 'Привет')          # warm per-snapshot caches
    indexer.delete_snapshot(conn, s2)
    s3 = indexer.create_snapshot(conn, 'копия 2', 'upload', z)
    indexer.index_snapshot(conn, s3, workers=1)
    assert s3 != s2
    t = tools.get_procedure(conn, 'ОбщийСервер', 'Привет')
    assert '[УТ 11.5 1.0.0.1] [копия 2 1.0.0.1]' in t


def test_extension_in_tools(env):
    conn, tmp = env
    z = str(tmp / 'ext.zip')
    write_zip(z, {'Configuration.xml': EXT_CONFIG, 'Documents/Заказ.xml': EXT_DOC,
                  'Documents/Заказ/Ext/ObjectModule.bsl': EXT_OBJ})
    sid = indexer.create_snapshot(conn, 'расш', 'upload', z)
    indexer.index_snapshot(conn, sid, workers=1)
    t = tools.on_write_or_post(conn, 'Документ.Заказ', config='УТ 11.5')
    assert 'перехват &Перед: Документ.Заказ.МодульОбъекта.Р_ОбработкаПроведения [расширение расш]' in t
    t = tools.find_callers(conn, 'ОбработкаПроведения', 'Документ.Заказ.МодульОбъекта', config='УТ 11.5')
    assert 'extension_intercepts' in t


def test_web_upload(tmp_path):
    from fastapi.testclient import TestClient
    from onec_code import web
    from onec_code.service import IndexService
    inbox = tmp_path / 'inbox'
    inbox.mkdir()
    write_zip(str(inbox / 'из_inbox.zip'), base_files())
    svc = IndexService(str(tmp_path / 'i.db'), str(inbox), str(tmp_path / 'archives'), workers=1, inbox_scan_seconds=0)
    svc.start()
    client = TestClient(web.build(svc, str(tmp_path / 'i.db')))
    assert client.get('/').status_code == 200
    z = tmp_path / 'u.zip'
    write_zip(str(z), base_files())
    r = client.post('/api/upload?title=УТ 11.5&delete_after=true', content=z.read_bytes())
    assert r.status_code == 200, r.text
    # duplicate title (case-insensitive) and not-a-dump archive are rejected
    assert client.get('/api/check?title=ут  11.5').status_code == 400
    bad = tmp_path / 'bad.zip'
    with zipfile.ZipFile(bad, 'w') as f:
        f.writestr('readme.txt', 'x')
    r = client.post('/api/upload?title=плохой', content=bad.read_bytes())
    assert r.status_code == 400 and 'Configuration.xml' in r.json()['error']
    r = client.post('/api/upload?title=не zip', content=b'hello')
    assert r.status_code == 400 and 'zip' in r.json()['error']
    for _ in range(100):
        data = client.get('/api/snapshots').json()
        if not data['busy']:
            break
        time.sleep(0.1)
    snaps = {s['label']: s for s in data['snapshots']}
    assert snaps['УТ 11.5']['status'] == 'ready' and snaps['УТ 11.5']['methods'] == 14
    assert snaps['УТ 11.5']['source_exists'] is False            # deleted after indexing
    assert snaps['из_inbox']['status'] == 'ready' and snaps['из_inbox']['origin'] == 'inbox'
    sid = snaps['УТ 11.5']['id']
    assert client.post(f'/api/snapshots/{sid}/rename', json={'label': 'из_INBOX'}).status_code == 400
    assert client.post(f'/api/snapshots/{sid}/rename', json={'label': 'УТ 11.5 тест'}).status_code == 200
    assert client.delete(f'/api/snapshots/{sid}').status_code == 200
    for _ in range(100):
        if not client.get('/api/snapshots').json()['busy']:
            break
        time.sleep(0.1)
    assert [s['label'] for s in client.get('/api/snapshots').json()['snapshots']] == ['из_inbox']
    assert list((tmp_path / 'archives').iterdir()) == []
    # inbox snapshots can be renamed too (matched by file path); a case-only change of its own name is allowed
    iid = snaps['из_inbox']['id']
    assert client.post(f'/api/snapshots/{iid}/rename', json={'label': 'Из Inbox'}).status_code == 200
    assert client.post(f'/api/snapshots/{iid}/rename', json={'label': 'УТ из папки'}).status_code == 200
    assert svc.sync_inbox() == []
    assert [s['label'] for s in client.get('/api/snapshots').json()['snapshots']] == ['УТ из папки']
