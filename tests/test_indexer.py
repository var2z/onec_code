import os
import zipfile

import pytest

from onec_code import db, indexer

NS = ('xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core" '
      'xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
      'xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config"')


def md(body):
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<MetaDataObject {NS} version="2.20">{body}</MetaDataObject>'


def syn(s):
    return f'<Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>{s}</v8:content></v8:item></Synonym>'


CONFIG = md(f'<Configuration uuid="c"><Properties><Name>Тест</Name>{syn("Тестовая")}<Version>1.0.0.1</Version>'
            '<Vendor>Я</Vendor></Properties></Configuration>')
CM = md(f'<CommonModule uuid="m"><Properties><Name>ОбщийСервер</Name>{syn("Общий сервер")}<Server>true</Server>'
        '</Properties></CommonModule>')
DOC = md(f'''<Document uuid="d"><Properties><Name>Заказ</Name>{syn("Заказ клиента")}
  <RegisterRecords><xr:Item xsi:type="xr:MDObjectRef">AccumulationRegister.Остатки</xr:Item></RegisterRecords>
  </Properties><ChildObjects>
  <Attribute uuid="a"><Properties><Name>Клиент</Name>{syn("Клиент")}
    <Type><v8:Type>cfg:CatalogRef.Клиенты</v8:Type></Type></Properties></Attribute>
  <Form>ФормаДокумента</Form></ChildObjects></Document>''')
FORM_META = md(f'<Form uuid="f"><Properties><Name>ФормаДокумента</Name>{syn("Форма заказа")}'
               '<FormType>Managed</FormType></Properties></Form>')
FORM = '''<?xml version="1.0" encoding="UTF-8"?>
<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" xmlns:v8="http://v8.1c.ru/8.1/data/core" version="2.20">
<Events><Event name="OnOpen">ПриОткрытии</Event></Events>
<ChildItems><InputField name="Клиент" id="1"><DataPath>Объект.Клиент</DataPath>
  <ExtendedTooltip name="КлиентРасширеннаяПодсказка" id="2"/>
  <Events><Event name="OnChange">КлиентПриИзменении</Event></Events></InputField></ChildItems>
<Attributes><Attribute name="Объект" id="1"><Type><v8:Type>cfg:DocumentObject.Заказ</v8:Type></Type>
<MainAttribute>true</MainAttribute></Attribute></Attributes>
</Form>'''
CM_BSL = '''#Область ПрограммныйИнтерфейс
// Возвращает приветствие
Функция Привет(Имя) Экспорт
	Возврат "Привет, " + Имя;
КонецФункции
#КонецОбласти
'''
OBJ_BSL = '''Процедура ОбработкаПроведения(Отказ, Режим)
	Движения.Остатки.Записывать = Истина;
	ОбщийСервер.Привет("мир");
КонецПроцедуры
'''
FORM_BSL = '''&НаКлиенте
Процедура ПриОткрытии(Отказ)
КонецПроцедуры

&НаКлиенте
Процедура КлиентПриИзменении(Элемент)
КонецПроцедуры
'''


def make_zip(path, cm_bsl=CM_BSL, extra=None):
    files = {
        'Configuration.xml': CONFIG,
        'CommonModules/ОбщийСервер.xml': CM,
        'CommonModules/ОбщийСервер/Ext/Module.bsl': cm_bsl,
        'Documents/Заказ.xml': DOC,
        'Documents/Заказ/Ext/ObjectModule.bsl': OBJ_BSL,
        'Documents/Заказ/Forms/ФормаДокумента.xml': FORM_META,
        'Documents/Заказ/Forms/ФормаДокумента/Ext/Form.xml': FORM,
        'Documents/Заказ/Forms/ФормаДокумента/Ext/Form/Module.bsl': FORM_BSL,
        'Documents/Заказ/Ext/Help.xml': '<skip/>',
        'CommonPictures/Картинка/Ext/Picture.png': 'png',
    }
    files.update(extra or {})
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for n, t in files.items():
            if t is not None:
                z.writestr(n, ('﻿' + t).encode('utf-8'))


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / 'index.db'))
    db.init(c)
    yield c
    c.close()


def names(conn, sid, kind):
    return sorted(r[0] for r in conn.execute(
        'SELECT COALESCE(n.full_name, n.name) FROM nodes n WHERE snapshot_id=? AND kind=?', (sid, kind)))


def test_index_incremental_dedup_gc(conn, tmp_path):
    z1 = str(tmp_path / 'v1.zip')
    make_zip(z1)
    sid = indexer.create_snapshot(conn, 'Тест  1', 'upload', z1)
    st = indexer.index_snapshot(conn, sid, workers=1)
    assert st['files'] == 8 and st['read'] == 8 and st['new_contents'] == 8
    assert st['modules'] == 3 and st['methods'] == 4 and st['objects'] == 2
    snap = conn.execute('SELECT * FROM snapshots WHERE id=?', (sid,)).fetchone()
    assert (snap['status'], snap['config_name'], snap['config_version'], snap['label']) == (
        'ready', 'Тест', '1.0.0.1', 'Тест 1')
    assert names(conn, sid, 'module') == ['Документ.Заказ.МодульОбъекта',
                                          'Документ.Заказ.Форма.ФормаДокумента.Модуль',
                                          'ОбщийМодуль.ОбщийСервер.Модуль']
    assert names(conn, sid, 'form') == ['Документ.Заказ.Форма.ФормаДокумента']
    assert names(conn, sid, 'form_item') == ['Клиент']
    form = conn.execute("SELECT n.synonym, c.parsed FROM nodes n JOIN contents c ON c.id = n.content_id "
                        "WHERE n.kind='form' AND n.snapshot_id=?", (sid,)).fetchone()
    assert form['synonym'] == 'Форма заказа' and indexer.unjz(form['parsed'])['main_attribute'] == 'Объект'
    m = conn.execute("SELECT flags FROM content_methods WHERE name='КлиентПриИзменении'").fetchone()
    assert db.directive_of(m['flags']) == 'НаКлиенте'
    # form elements without auxiliary ones
    assert [tuple(r) for r in conn.execute('SELECT idx, name, type, data_path, parent FROM form_elements')] == [
        (0, 'Клиент', 'InputField', 'Объект.Клиент', -1)]
    # FTS over method bodies through the external-content view (texts are stored once)
    hits = conn.execute("SELECT cm.name, snippet(code_fts, 0, '[', ']', '…', 5) FROM code_fts "
                        "JOIN content_methods cm ON cm.id = code_fts.rowid WHERE code_fts MATCH ?",
                        ('привет',)).fetchall()
    assert {h[0] for h in hits} == {'Привет', 'ОбработкаПроведения'}
    assert any('[Привет]' in h[1] for h in hits)
    # doc comment is part of the indexed body
    assert conn.execute("SELECT count(*) FROM code_fts WHERE code_fts MATCH 'приветствие'").fetchone()[0] == 1
    # names FTS: case-insensitive Cyrillic, synonym words
    assert conn.execute("SELECT count(*) FROM names_fts WHERE names_fts MATCH 'клиента'").fetchone()[0] == 1

    # nothing changed -> nothing read
    st = indexer.index_snapshot(conn, sid, workers=1)
    assert st['read'] == 0 and st['new_contents'] == 0 and st['methods'] == 4

    # parser upgraded -> stale results re-parsed although the archive is unchanged
    conn.execute('UPDATE contents SET pver = pver - 1')
    st = indexer.index_snapshot(conn, sid, workers=1)
    assert st['read'] == 8 and st['new_contents'] == 8 and st['methods'] == 4
    assert conn.execute('SELECT count(*) FROM contents WHERE pver = ?', (indexer.PARSER_VERSION,)).fetchone()[0] == 8
    assert conn.execute('SELECT count(*) FROM content_methods').fetchone()[0] == 4

    # module changed -> one file read, old content collected
    make_zip(z1, cm_bsl=CM_BSL.replace('Привет, ', 'Здравствуй, ') + 'Процедура Новая()\nКонецПроцедуры\n')
    st = indexer.index_snapshot(conn, sid, workers=1)
    assert st['read'] == 1 and st['new_contents'] == 1 and st['gc_contents'] == 1 and st['methods'] == 5
    assert conn.execute("SELECT count(*) FROM code_fts WHERE code_fts MATCH 'здравствуй'").fetchone()[0] == 1

    # second snapshot of the same content -> everything deduplicated
    z2 = str(tmp_path / 'v2.zip')
    make_zip(z2, cm_bsl=CM_BSL.replace('Привет, ', 'Здравствуй, ') + 'Процедура Новая()\nКонецПроцедуры\n')
    sid2 = indexer.create_snapshot(conn, 'Тест 2', 'upload', z2)
    st = indexer.index_snapshot(conn, sid2, workers=1)
    assert st['read'] == 8 and st['new_contents'] == 0 and st['reused_contents'] == 8
    assert conn.execute('SELECT count(*) FROM contents').fetchone()[0] == 8
    assert conn.execute("SELECT count(*) FROM nodes WHERE kind='method'").fetchone()[0] == 10

    # labels are unique ignoring case and extra spaces
    with pytest.raises(indexer.IndexError_):
        indexer.create_snapshot(conn, 'тест   2', 'upload', z2)

    # removed file -> dropped from the snapshot
    make_zip(z2, cm_bsl=None)
    st = indexer.index_snapshot(conn, sid2, workers=1)
    assert st['removed'] == 1 and st['modules'] == 2

    indexer.delete_snapshot(conn, sid)
    indexer.delete_snapshot(conn, sid2)
    for t in ('contents', 'content_methods', 'form_elements', 'files', 'nodes'):
        assert conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0] == 0, t
    assert conn.execute("SELECT count(*) FROM code_fts WHERE code_fts MATCH 'здравствуй'").fetchone()[0] == 0
    conn.execute("INSERT INTO code_fts(code_fts) VALUES('integrity-check')")
    conn.execute("INSERT INTO names_fts(names_fts) VALUES('integrity-check')")


def test_not_a_configuration(conn, tmp_path):
    z = str(tmp_path / 'bad.zip')
    with zipfile.ZipFile(z, 'w') as f:
        f.writestr('readme.txt', 'x')
    sid = indexer.create_snapshot(conn, 'bad', 'upload', z)
    with pytest.raises(indexer.IndexError_):
        indexer.index_snapshot(conn, sid, workers=1)
    row = conn.execute('SELECT status, error FROM snapshots WHERE id=?', (sid,)).fetchone()
    assert row['status'] == 'error' and 'Configuration.xml' in row['error']


def test_zip_with_cp866_names_and_top_folder(conn, tmp_path, monkeypatch):
    """Windows 10 "Send to compressed folder": cp866 names without the UTF-8 flag, dump inside a folder."""
    z = str(tmp_path / 'win.zip')

    def cp866(self):
        return self.filename.encode('cp866'), self.flag_bits & ~0x800
    src = str(tmp_path / 'utf.zip')
    make_zip(src)
    monkeypatch.setattr(zipfile.ZipInfo, '_encodeFilenameFlags', cp866)
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(z, 'w') as b:
        for n in a.namelist():
            b.writestr('Выгрузка УТ/' + n, a.read(n))
    monkeypatch.undo()
    with zipfile.ZipFile(z) as f:
        assert not any('Заказ' in n for n in f.namelist())       # zipfile alone garbles the names
    sid = indexer.create_snapshot(conn, 'win', 'upload', z)
    st = indexer.index_snapshot(conn, sid, workers=1)
    assert st['files'] == 8 and st['methods'] == 4 and st['objects'] == 2


def test_folder_source(conn, tmp_path):
    z = str(tmp_path / 'v.zip')
    make_zip(z)
    d = tmp_path / 'dump'
    with zipfile.ZipFile(z) as f:
        f.extractall(d)
    sid = indexer.create_snapshot(conn, 'папка', 'dir', str(d))
    st = indexer.index_snapshot(conn, sid, workers=1)
    assert st['files'] == 8 and st['methods'] == 4
    st = indexer.index_snapshot(conn, sid, workers=1)
    assert st['read'] == 0
    os.utime(d / 'Documents' / 'Заказ' / 'Ext' / 'ObjectModule.bsl', ns=(1, 1))
    st = indexer.index_snapshot(conn, sid, workers=1)
    assert st['read'] == 1 and st['new_contents'] == 0     # touched but same content
