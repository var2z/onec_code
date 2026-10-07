import textwrap

from onec_code.bsl_parser import parse_module, extract_query_sources


def P(src):
    return parse_module(textwrap.dedent(src).lstrip('\n'))


def meth(mod, name):
    return next(m for m in mod.methods if m.name == name)


def refs(m, kind):
    return [r for r in m.refs if r.kind == kind]


def calls(m):
    return ['.'.join(r.target) for r in m.refs if r.kind == 'call']


# ---------------------------------------------------------------- structure

def test_basic_methods_params_export():
    mod = P('''
        // Описание
        // функции
        Функция Сумма(Знач А, Б = 0, В = "x", Г = -1) Экспорт
            Возврат А + Б;
        КонецФункции

        Процедура Пустая()
        КонецПроцедуры
    ''')
    assert [m.name for m in mod.methods] == ['Сумма', 'Пустая']
    f = meth(mod, 'Сумма')
    assert f.kind == 'Функция' and f.export
    assert [(p.name, p.by_val, p.default) for p in f.params] == [
        ('А', True, None), ('Б', False, '0'), ('В', False, '"x"'), ('Г', False, '-1')]
    assert (f.doc_line, f.line_start, f.line_header, f.line_end) == (1, 3, 3, 5)
    assert f.signature == 'Функция Сумма(Знач А, Б = 0, В = "x", Г = -1) Экспорт'
    p = meth(mod, 'Пустая')
    assert not p.export and p.params == [] and (p.line_start, p.line_end) == (7, 8)
    assert not mod.errors


def test_directives_case_insensitive_and_line_start():
    mod = P('''
        &Наклиенте
        Процедура А()
        КонецПроцедуры

        &НаСервереБезконтекста
        Функция Б()
        КонецФункции

        &AtServer
        Procedure C() Export
        EndProcedure
    ''')
    assert [(m.name, m.directive, m.export) for m in mod.methods] == [
        ('А', 'НаКлиенте', False), ('Б', 'НаСервереБезКонтекста', False), ('C', 'НаСервере', True)]
    assert meth(mod, 'А').line_start == 1 and meth(mod, 'А').line_header == 2


def test_var_with_directive_does_not_leak():
    mod = P('''
        &НаКлиенте
        Перем Кэш;

        Процедура А()
        КонецПроцедуры
    ''')
    assert meth(mod, 'А').directive is None


def test_keywords_in_strings_and_comments():
    mod = P('''
        Процедура А()
            // Процедура Фейк() КонецПроцедуры
            Т = "КонецПроцедуры; Функция Б()";
            Т2 = "Процедура ""В"" ()";
        КонецПроцедуры
    ''')
    assert [m.name for m in mod.methods] == ['А']
    assert meth(mod, 'А').line_end == 5


def test_multiline_string_with_comments_inside():
    mod = P('''
        Функция Т()
            Запрос = Новый Запрос;
            Запрос.Текст = "ВЫБРАТЬ
            |	Т.Ссылка
            //|	КонецФункции
            |ИЗ
            |	Документ.РеализацияТоваровУслуг КАК Т";
            Возврат Запрос.Выполнить();
        КонецФункции

        Процедура После()
        КонецПроцедуры
    ''')
    assert [m.name for m in mod.methods] == ['Т', 'После']
    t = meth(mod, 'Т')
    assert t.line_end == 9
    q = refs(t, 'query')
    assert [r.target for r in q] == [('Документ', 'РеализацияТоваровУслуг', None)]
    assert q[0].line == 6
    # Запрос.Выполнить() is a normal method call, not dynamic Выполнить
    assert 'Запрос.Выполнить' in calls(t)
    assert not [r for r in t.refs if r.kind == 'dyn']


def test_end_keyword_not_at_line_start_and_async():
    mod = P('''
        &НаКлиенте
        Асинх Процедура А(Команда)
            Если Истина Тогда
                Ждать Б();
            КонецЕсли;КонецПроцедуры

        &НаКлиенте
        Асинх Функция Б()
        	Возврат 1; КонецФункции
    ''')
    a, b = mod.methods
    assert (a.name, a.is_async, a.line_start, a.line_header, a.line_end) == ('А', True, 1, 2, 5)
    assert (b.name, b.is_async, b.line_end) == ('Б', True, 9)
    assert b.signature == 'Асинх Функция Б()'
    assert calls(a) == ['Б']


def test_nested_regions_and_ifdef():
    mod = P('''
        #Если Сервер Или ВнешнееСоединение Тогда
        #Область ПрограммныйИнтерфейс

        #Область Вложенная
        Процедура А() Экспорт
        КонецПроцедуры
        #КонецОбласти

        Процедура Б() Экспорт
            #Если Клиент Тогда
            X = 1;
            #КонецЕсли
        КонецПроцедуры

        #Конецобласти

        #Region Internal
        Procedure C()
        EndProcedure
        #EndRegion
        #Иначе
        Процедура Д()
        КонецПроцедуры
        #КонецЕсли
    ''')
    assert meth(mod, 'А').regions == ['ПрограммныйИнтерфейс', 'Вложенная']
    assert meth(mod, 'Б').regions == ['ПрограммныйИнтерфейс']
    assert meth(mod, 'C').regions == ['Internal']
    assert meth(mod, 'А').ifdef == ['Сервер Или ВнешнееСоединение']
    assert meth(mod, 'Д').ifdef == ['Не (Сервер Или ВнешнееСоединение)']
    assert meth(mod, 'Д').regions == []
    assert not mod.errors


def test_extension_annotations_and_deleted_code():
    mod = P('''
        &НаСервере
        &Перед("ПриСозданииНаСервере")
        Процедура Расш_ПриСозданииНаСервере(Отказ, СтандартнаяОбработка)
            МойМодуль.Сделать();
        КонецПроцедуры

        &Вместо("Провести")
        Функция Расш_Провести()
            Возврат ПродолжитьВызов();
        КонецФункции

        &ИзменениеИКонтроль("Рассчитать")
        Процедура Расш_Рассчитать()
            #Удаление
            СтарыйМодуль.Старое();
            #КонецУдаления
            #Вставка
            НовыйМодуль.Новое();
            #КонецВставки
        КонецПроцедуры

        &After("OnOpen")
        Procedure Ext_OnOpen()
        EndProcedure
    ''')
    p = meth(mod, 'Расш_ПриСозданииНаСервере')
    assert p.directive == 'НаСервере'
    assert (p.extension_kind, p.extension_target) == ('Перед', 'ПриСозданииНаСервере')
    assert p.line_start == 1 and p.line_header == 3
    assert (meth(mod, 'Расш_Провести').extension_kind, meth(mod, 'Расш_Провести').extension_target) == ('Вместо', 'Провести')
    r = meth(mod, 'Расш_Рассчитать')
    assert r.extension_kind == 'ИзменениеИКонтроль'
    assert calls(r) == ['НовыйМодуль.Новое']
    assert meth(mod, 'Ext_OnOpen').extension_kind == 'После'


def test_unterminated_method_recovery():
    mod = P('''
        Процедура А()
            X = 1;

        Процедура Б()
        КонецПроцедуры
    ''')
    assert [m.name for m in mod.methods] == ['А', 'Б']
    assert meth(mod, 'А').broken and meth(mod, 'А').line_end == 3
    assert not meth(mod, 'Б').broken
    assert mod.errors


# ---------------------------------------------------------------- references

def test_calls():
    mod = P('''
        Процедура А()
            ОбщегоНазначения.СообщитьПользователю("x");
            Локальная(1);
            ЭтотОбъект.Локальная(2);
            Документы.РеализацияТоваровУслуг.ЗаполнитьЧтоТо(Ссылка);
            С = Новый Структура("А", 1);
            Если (Х > 1) Тогда
                Возврат;
            КонецЕсли;
            Объект.Товары.Добавить();
        КонецПроцедуры
    ''')
    a = meth(mod, 'А')
    assert calls(a) == ['ОбщегоНазначения.СообщитьПользователю', 'Локальная', 'ЭтотОбъект.Локальная',
                        'Документы.РеализацияТоваровУслуг.ЗаполнитьЧтоТо', 'Объект.Товары.Добавить']


def test_literal_call_args():
    mod = P('''
        Процедура ОтразитьДвижения(Т, Д, Отказ) Экспорт
            ПроведениеДокументов.ОтразитьДвижения(Т, Д, "ТоварыНаСкладах");
            Механизмы.Вставить("Продажи", "РегистрыНакопления.Продажи");
            Сообщить(НСтр("ru = 'Длинный текст, не идентификатор'"));
            Ф(Сумма("А"), "Б" + В, "Г");
        КонецПроцедуры
    ''')
    got = {'.'.join(r.target): (r.detail or {}).get('s') for r in refs(mod.methods[0], 'call')}
    assert got['ПроведениеДокументов.ОтразитьДвижения'] == [[2, 'ТоварыНаСкладах']]
    assert got['Механизмы.Вставить'] == [[0, 'Продажи'], [1, 'РегистрыНакопления.Продажи']]
    assert got['Сообщить'] is None and got['НСтр'] is None
    assert got['Ф'] == [[2, 'Г']] and got['Сумма'] == [[0, 'А']]


def test_local_names_marked():
    mod = P('''
        Перем Кэш;
        Процедура А(Пользователи)
            Перем Справочник;
            Пользователи.Количество();
            Кэш.Очистить();
            Список = Новый Массив;
            Список.Добавить(1);
            Справочник.Найти(1);
            ОбщийМодульХ.Метод();
        КонецПроцедуры
    ''')
    got = {'.'.join(r.target): bool((r.detail or {}).get('l')) for r in refs(mod.methods[0], 'call')}
    assert got == {'Пользователи.Количество': True, 'Кэш.Очистить': True, 'Список.Добавить': True,
                   'Справочник.Найти': True, 'ОбщийМодульХ.Метод': False}


def test_body_refs_outside_methods():
    mod = P('''
        Перем А;
        Процедура П()
        КонецПроцедуры
        А = ОбщийМодульХ.Инициализация();
    ''')
    assert ['.'.join(r.target) for r in mod.body_refs if r.kind == 'call'] == ['ОбщийМодульХ.Инициализация']


def test_dynamic_execute_eval():
    mod = P('''
        Процедура А()
            Выполнить("Сообщить(1)");
            Х = Вычислить(Выражение);
            Результат = Запрос.Выполнить();
        КонецПроцедуры
    ''')
    d = refs(meth(mod, 'А'), 'dyn')
    assert [(r.detail['type'], r.detail['fn']) for r in d] == [('execute', 'Выполнить'), ('execute', 'Вычислить')]


def test_common_module_literal_chain_and_variable():
    mod = P('''
        Процедура А()
            ОбщегоНазначения.ОбщийМодуль("УправлениеДоступом").ЗаполнитьНаборы(Т);
            ОбщегоНазначенияКлиент.ОбщийМодуль("ФайловаяСистемаКлиент").Открыть();
            Модуль = ОбщегоНазначения.ОбщийМодуль("Интеграция");
            Модуль.Выгрузить(1);
        КонецПроцедуры

        Процедура Б()
            Модуль = ОбщегоНазначения.ОбщийМодуль("Первый");
            Если Условие Тогда
                Модуль = ОбщегоНазначения.ОбщийМодуль("Второй");
            КонецЕсли;
            Модуль.Метод();
            М2 = ОбщегоНазначения.ОбщийМодуль(ИмяМодуля);
            М2.Метод();
            ОбщегоНазначения.ОбщийМодуль(ИмяМодуля).Метод();
            Результат = ОбщегоНазначения.ОбщийМодуль("Х").Метод();
            Результат.Метод2();
        КонецПроцедуры
    ''')
    a = refs(meth(mod, 'А'), 'dyn')
    assert [(r.detail['module'], r.detail.get('method'), r.detail.get('reason')) for r in a] == [
        ('УправлениеДоступом', 'ЗаполнитьНаборы', None),
        ('ФайловаяСистемаКлиент', 'Открыть', None),
        ('Интеграция', 'Выгрузить', None),
    ]
    assert a[2].detail['var'] == 'Модуль'
    b = refs(meth(mod, 'Б'), 'dyn')
    got = [(r.detail['module'], r.detail.get('method'), r.detail.get('reason')) for r in b]
    assert got == [
        (None, 'Метод', 'ambiguous-branches'),
        (None, 'Метод', 'non-literal'),
        (None, 'Метод', 'non-literal'),
        ('Х', 'Метод', None),
    ]
    # the call to ОбщегоНазначения.ОбщийМодуль itself is an ordinary exact call
    assert 'ОбщегоНазначения.ОбщийМодуль' in calls(meth(mod, 'Б'))
    assert 'Результат.Метод2' in calls(meth(mod, 'Б'))


def dyn_mods(m):
    return [(r.detail.get('module'), r.detail.get('method'), r.detail.get('reason'))
            for r in m.refs if r.kind == 'dyn' and r.detail['type'] == 'common_module']


def test_module_variable_nearest_assignment():
    mod = P('''
        Процедура Последовательно()
            М = ОбщегоНазначения.ОбщийМодуль("А");
            М.Первый();
            М = ОбщегоНазначения.ОбщийМодуль("Б");
            М.Второй();
        КонецПроцедуры

        Процедура ТотЖеМодульВВетках()
            Если Х Тогда
                М = ОбщегоНазначения.ОбщийМодуль("А");
            Иначе
                М = ОбщегоНазначения.ОбщийМодуль("а");
            КонецЕсли;
            М.Вызов();
        КонецПроцедуры

        Процедура РазныеВетки()
            Если Х Тогда
                М = ОбщегоНазначения.ОбщийМодуль("А");
            ИначеЕсли У Тогда
                М = ОбщегоНазначения.ОбщийМодуль("Б");
            КонецЕсли;
            М.Вызов();
        КонецПроцедуры

        Процедура ВнутриВеток()
            Если Х Тогда
                М = ОбщегоНазначения.ОбщийМодуль("А");
                М.ВызовА();
            Иначе
                М = ОбщегоНазначения.ОбщийМодуль("Б");
                М.ВызовБ();
            КонецЕсли;
        КонецПроцедуры

        Процедура НеопределеноИВетка()
            М = Неопределено;
            Если ПодсистемаСуществует Тогда
                М = ОбщегоНазначения.ОбщийМодуль("А");
            КонецЕсли;
            Если М <> Неопределено Тогда
                М.Вызов();
            КонецЕсли;
        КонецПроцедуры

        Процедура ЦиклПереприсваивает()
            М = ОбщегоНазначения.ОбщийМодуль("А");
            Для Каждого Имя Из Имена Цикл
                М.Вызов();
                М = ОбщегоНазначения.ОбщийМодуль("Б");
            КонецЦикла;
            М.После();
        КонецПроцедуры

        Процедура ДругоеЗначение()
            М = ОбщегоНазначения.ОбщийМодуль("А");
            М.Вызов();
            М = Новый Структура;
            М.Вставить("к", 1);
        КонецПроцедуры

        Процедура ДоПрисваивания()
            М.Вызов();
            М = ОбщегоНазначения.ОбщийМодуль("А");
        КонецПроцедуры
    ''')
    assert dyn_mods(meth(mod, 'Последовательно')) == [('А', 'Первый', None), ('Б', 'Второй', None)]
    assert dyn_mods(meth(mod, 'ТотЖеМодульВВетках')) == [('А', 'Вызов', None)]
    assert dyn_mods(meth(mod, 'РазныеВетки')) == [(None, 'Вызов', 'ambiguous-branches')]
    assert dyn_mods(meth(mod, 'ВнутриВеток')) == [('А', 'ВызовА', None), ('Б', 'ВызовБ', None)]
    assert dyn_mods(meth(mod, 'НеопределеноИВетка')) == [('А', 'Вызов', None)]
    # linear order: inside the loop the nearest preceding assignment is "А";
    # after the loop both "А" (loop not entered) and "Б" may reach the call
    assert dyn_mods(meth(mod, 'ЦиклПереприсваивает')) == [('А', 'Вызов', None),
                                                         (None, 'После', 'ambiguous-branches')]
    d = meth(mod, 'ДругоеЗначение')
    assert dyn_mods(d) == [('А', 'Вызов', None)]
    assert 'М.Вставить' in calls(d)
    assert dyn_mods(meth(mod, 'ДоПрисваивания')) == [(None, 'Вызов', 'no-preceding-assignment')]


def test_form_module_implicit_server_directive():
    src = '''
        &НаКлиенте
        Процедура А()
        КонецПроцедуры

        Процедура Б()
        КонецПроцедуры
    '''
    mod = parse_module(textwrap.dedent(src), form_module=True)
    assert [(m.directive, m.directive_implicit) for m in mod.methods] == [
        ('НаКлиенте', False), ('НаСервере', True)]
    mod = parse_module(textwrap.dedent(src))
    assert meth(mod, 'Б').directive is None


def test_variable_tracking_is_per_method():
    mod = P('''
        Процедура А()
            М = ОбщегоНазначения.ОбщийМодуль("Икс");
        КонецПроцедуры
        Процедура Б()
            М.Метод();
        КонецПроцедуры
    ''')
    assert calls(meth(mod, 'Б')) == ['М.Метод']
    assert refs(meth(mod, 'Б'), 'dyn') == []


def test_notify_description_and_string_method():
    mod = P('''
        &НаКлиенте
        Процедура А()
            Оповещение = Новый ОписаниеОповещения("ПослеВыбора", ЭтотОбъект, Параметры);
            ПодключитьОбработчикОжидания("Обновить", 1, Истина);
            ДлительныеОперации.ВыполнитьВФоне("Обмен.ВыполнитьОбмен", П, НП);
        КонецПроцедуры
    ''')
    d = refs(meth(mod, 'А'), 'dyn')
    assert d[0].detail == {'type': 'notify', 'method': 'ПослеВыбора', 'target': 'ЭтотОбъект'}
    assert d[1].detail == {'type': 'string', 'module': 'Обмен', 'method': 'ВыполнитьОбмен'}
    assert 'ПодключитьОбработчикОжидания' in calls(meth(mod, 'А'))


def test_register_records():
    mod = P('''
        Процедура ОбработкаПроведения(Отказ, Режим)
            Движения.ТоварыНаСкладах.Записывать = Истина;
            ЭтотОбъект.Движения.ДвиженияПоНДС.Записать();
            Движения.Записать();
        КонецПроцедуры
    ''')
    w = refs(meth(mod, 'ОбработкаПроведения'), 'writes')
    assert [r.target[0] for r in w] == ['ТоварыНаСкладах', 'ДвиженияПоНДС']


def test_english_keywords():
    mod = P('''
        &AtClient
        Async Function F(Val A = Undefined) Export
            Return CommonModuleX.Do(A);
        EndFunction
    ''')
    f = mod.methods[0]
    assert (f.name, f.kind, f.is_async, f.export, f.directive) == ('F', 'Функция', True, True, 'НаКлиенте')
    assert f.params[0].by_val and f.params[0].default == 'Undefined'
    assert calls(f) == ['CommonModuleX.Do']


# ---------------------------------------------------------------- queries

def test_query_sources():
    q = '''ВЫБРАТЬ
        Т.Номенклатура,
        ЗНАЧЕНИЕ(Справочник.Склады.ПустаяСсылка) КАК С
    ИЗ
        РегистрНакопления.ТоварыНаСкладах.Остатки(&Дата, Склад В (
            ВЫБРАТЬ С.Ссылка ИЗ Справочник.Склады КАК С)) КАК Т
        ЛЕВОЕ СОЕДИНЕНИЕ Документ.РеализацияТоваровУслуг.Товары КАК РТ
        ПО РТ.Номенклатура = Т.Номенклатура
            И РТ.Склад = ЗНАЧЕНИЕ(Справочник.Склады.ПустаяСсылка)
        , РегистрСведений.ЦеныНоменклатуры.СрезПоследних КАК Ц
    ГДЕ
        Т.Номенклатура ССЫЛКА Справочник.Номенклатура
    ОБЪЕДИНИТЬ ВСЕ
    ВЫБРАТЬ Х.Ссылка ИЗ Catalog.Items AS Х
    '''
    got = [s[:3] for s in extract_query_sources(q)]
    assert got == [
        ('РегистрНакопления', 'ТоварыНаСкладах', 'Остатки'),
        ('Справочник', 'Склады', None),
        ('Документ', 'РеализацияТоваровУслуг', 'Товары'),
        ('РегистрСведений', 'ЦеныНоменклатуры', 'СрезПоследних'),
        ('Справочник', 'Items', None),
    ]


def test_query_temp_tables_ignored():
    q = 'ВЫБРАТЬ * ПОМЕСТИТЬ ВТ ИЗ Справочник.А КАК А; ВЫБРАТЬ * ИЗ ВТ КАК ВТ'
    assert [s[:3] for s in extract_query_sources(q)] == [('Справочник', 'А', None)]
