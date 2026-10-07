from onec_code import md_parser as mp
from onec_code.layout import classify

HDR = ('<?xml version="1.0" encoding="UTF-8"?>\n<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
       'xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" '
       'xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
       'xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" version="2.20">')
END = '</MetaDataObject>'


def syn(s):
    return f'<Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>{s}</v8:content></v8:item></Synonym>'


def b(s):
    return ('﻿' + s).encode('utf-8')


def test_configuration():
    x = HDR + f'''<Configuration uuid="1"><InternalInfo/><Properties>
        <Name>УправлениеТорговлей</Name>{syn("Управление торговлей")}<Comment/>
        <Vendor>Фирма "1С"</Vendor><Version>11.5.27.81</Version>
        <CompatibilityMode>Version8_3_27</CompatibilityMode></Properties></Configuration>''' + END
    c = mp.parse_configuration(b(x))
    assert (c.name, c.synonym, c.version, c.vendor, c.is_extension) == (
        'УправлениеТорговлей', 'Управление торговлей', '11.5.27.81', 'Фирма "1С"', False)


def test_extension_configuration():
    x = HDR + f'''<Configuration uuid="1"><Properties><ObjectBelonging>Adopted</ObjectBelonging>
        <Name>МоёРасширение</Name>{syn("Моё расширение")}<Comment/><NamePrefix>Мр_</NamePrefix>
        <ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose>
        <Version>1.0.1</Version></Properties></Configuration>''' + END
    c = mp.parse_configuration(b(x))
    assert c.is_extension and c.extension_purpose == 'Customization' and c.name_prefix == 'Мр_'


DOC = HDR + f'''<Document uuid="d1"><InternalInfo><xr:GeneratedType name="x" category="Ref"/></InternalInfo>
<Properties><Name>РеализацияТоваровУслуг</Name>{syn("Реализация товаров и услуг")}<Comment/>
  <Posting>Allow</Posting>
  <RegisterRecords>
    <xr:Item xsi:type="xr:MDObjectRef">AccumulationRegister.ТоварыНаСкладах</xr:Item>
    <xr:Item xsi:type="xr:MDObjectRef">InformationRegister.СтатусыДокументов</xr:Item>
  </RegisterRecords>
</Properties>
<ChildObjects>
  <Attribute uuid="a1"><Properties><Name>Контрагент</Name>{syn("Контрагент")}<Comment/>
    <Type><v8:Type>cfg:CatalogRef.Контрагенты</v8:Type><v8:Type>cfg:CatalogRef.ФизическиеЛица</v8:Type></Type>
  </Properties></Attribute>
  <Attribute uuid="a2"><Properties><Name>Комментарий</Name>{syn("Комментарий")}<Comment/>
    <Type><v8:Type>xs:string</v8:Type><v8:StringQualifiers><v8:Length>0</v8:Length></v8:StringQualifiers></Type>
  </Properties></Attribute>
  <Attribute uuid="a3"><Properties><Name>Сумма</Name>{syn("Сумма")}<Comment/>
    <Type><v8:TypeSet>cfg:DefinedType.ДенежнаяСумма</v8:TypeSet></Type></Properties></Attribute>
  <Form>ФормаДокумента</Form>
  <TabularSection uuid="t1"><InternalInfo/><Properties><Name>Товары</Name>{syn("Товары")}<Comment/></Properties>
    <ChildObjects>
      <Attribute uuid="ta1"><Properties><Name>Номенклатура</Name>{syn("Номенклатура")}<Comment/>
        <Type><v8:TypeSet>cfg:DefinedType.Номенклатура</v8:TypeSet></Type></Properties></Attribute>
      <Attribute uuid="ta2"><Properties><Name>Количество</Name>{syn("Количество")}<Comment/>
        <Type><v8:Type>xs:decimal</v8:Type><v8:NumberQualifiers><v8:Digits>15</v8:Digits>
        <v8:FractionDigits>3</v8:FractionDigits></v8:NumberQualifiers></Type></Properties></Attribute>
    </ChildObjects></TabularSection>
  <Template>ПФ_MXL_Накладная</Template>
  <Command uuid="c1"><Properties><Name>Печать</Name>{syn("Печать")}</Properties></Command>
</ChildObjects></Document>''' + END


def test_document():
    o = mp.parse_object(b(DOC))
    assert (o.md_type, o.name, o.synonym, o.full_name) == (
        'Документ', 'РеализацияТоваровУслуг', 'Реализация товаров и услуг', 'Документ.РеализацияТоваровУслуг')
    assert o.props['RegisterRecords'] == ['РегистрНакопления.ТоварыНаСкладах', 'РегистрСведений.СтатусыДокументов']
    assert o.props['Posting'] == 'Allow'
    assert [(a.name, a.types) for a in o.attributes] == [
        ('Контрагент', ['ref:Справочник.Контрагенты', 'ref:Справочник.ФизическиеЛица']),
        ('Комментарий', ['prim:Строка']),
        ('Сумма', ['def:ДенежнаяСумма']),
    ]
    ts = o.tabular_sections[0]
    assert ts.name == 'Товары'
    assert [(a.name, a.types) for a in ts.attributes] == [
        ('Номенклатура', ['def:Номенклатура']), ('Количество', ['prim:Число(15,3)'])]
    assert o.forms == ['ФормаДокумента'] and o.templates == ['ПФ_MXL_Накладная']
    assert o.commands == [('Печать', 'Печать')]
    assert not o.adopted


def test_expand_defined_types_recursive_and_cycles():
    defined = {'Номенклатура': ['ref:Справочник.Номенклатура', 'def:Услуги'],
               'Услуги': ['ref:Справочник.Услуги', 'def:Номенклатура'],   # cycle
               'ДенежнаяСумма': ['prim:Число(15,2)']}
    chars = {'ДопРеквизиты': ['ref:Справочник.Значения', 'prim:Строка']}
    assert mp.expand_types(['def:Номенклатура', 'char:ДопРеквизиты', 'ref:Справочник.Номенклатура'],
                           defined, chars) == [
        'ref:Справочник.Номенклатура', 'ref:Справочник.Услуги', 'ref:Справочник.Значения', 'prim:Строка']


def test_cfg_type_forms():
    f = mp.parse_cfg_type
    assert f('cfg:DocumentObject.Заказ') == 'obj:Документ.Заказ'
    assert f('cfg:InformationRegisterRecordSet.Цены') == 'obj:РегистрСведений.Цены'
    assert f('cfg:ChartOfCharacteristicTypesRef.ВидыСубконто') == 'ref:ПланВидовХарактеристик.ВидыСубконто'
    assert f('cfg:BusinessProcessRoutePointRef.Задание') == 'ref:БизнесПроцесс.Задание'
    assert f('cfg:CatalogRef') == 'any:Справочник'
    assert f('cfg:AnyRef') == 'anyref'
    assert f('cfg:Characteristic.ДопРеквизиты') == 'char:ДопРеквизиты'
    assert f('xs:boolean') == 'prim:Булево'


def test_subscription_and_scheduled_job():
    x = HDR + f'''<EventSubscription uuid="s"><Properties><Name>ПроверкаПередЗаписью</Name>{syn("П")}<Comment/>
        <Source><v8:Type>cfg:DocumentObject.Заказ</v8:Type>
                <v8:TypeSet>cfg:DefinedType.ДокументыСоСтатусом</v8:TypeSet></Source>
        <Event>BeforeWrite</Event><Handler>CommonModule.СтатусыСервер.ПередЗаписью</Handler>
        </Properties></EventSubscription>''' + END
    o = mp.parse_object(b(x))
    assert o.md_type == 'ПодпискаНаСобытие'
    assert o.props['Source'] == ['obj:Документ.Заказ', 'def:ДокументыСоСтатусом']
    assert o.props['Event'] == 'BeforeWrite' and o.props['EventRu'] == 'ПередЗаписью'
    assert o.props['Handler'] == ['ОбщийМодуль.СтатусыСервер', 'ПередЗаписью']
    x = HDR + f'''<ScheduledJob uuid="j"><Properties><Name>Обмен</Name>{syn("Обмен")}<Comment/>
        <MethodName>CommonModule.ОбменСервер.ВыполнитьОбмен</MethodName><Use>false</Use>
        <Predefined>true</Predefined></Properties></ScheduledJob>''' + END
    o = mp.parse_object(b(x))
    assert o.props['MethodName'] == ['ОбщийМодуль.ОбменСервер', 'ВыполнитьОбмен']
    assert o.props['Use'] is False and o.props['Predefined'] is True


def test_common_module_flags():
    x = HDR + f'''<CommonModule uuid="m"><Properties><Name>ОбщегоНазначения</Name>{syn("ОН")}<Comment/>
        <Global>false</Global><ClientManagedApplication>false</ClientManagedApplication><Server>true</Server>
        <ExternalConnection>true</ExternalConnection><ServerCall>false</ServerCall>
        <Privileged>false</Privileged><ReturnValuesReuse>DontUse</ReturnValuesReuse></Properties></CommonModule>''' + END
    o = mp.parse_object(b(x))
    assert o.props == {'Global': False, 'ClientManagedApplication': False, 'Server': True,
                       'ExternalConnection': True, 'ServerCall': False, 'Privileged': False,
                       'ReturnValuesReuse': 'DontUse'}


def test_adopted_object_in_extension():
    x = HDR + f'''<Document uuid="d2"><Properties><ObjectBelonging>Adopted</ObjectBelonging>
        <Name>РеализацияТоваровУслуг</Name><Comment/>
        <ExtendedConfigurationObject>8eac9a31</ExtendedConfigurationObject></Properties>
        <ChildObjects><Form>ФормаДокумента</Form>
        <Attribute uuid="n"><Properties><Name>Мр_НовыйРеквизит</Name>{syn("Новый")}<Comment/>
        <Type><v8:Type>xs:boolean</v8:Type></Type></Properties></Attribute></ChildObjects></Document>''' + END
    o = mp.parse_object(b(x))
    assert o.adopted and o.synonym == '' and [a.name for a in o.attributes] == ['Мр_НовыйРеквизит']


FORM = '''<?xml version="1.0" encoding="UTF-8"?>
<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" xmlns:v8="http://v8.1c.ru/8.1/data/core" version="2.20">
  <AutoCommandBar name="ФормаКоманднаяПанель" id="-1">
    <ChildItems><Button name="ФормаПровести" id="1"><CommandName>Form.Command.Провести</CommandName>
      <ExtendedTooltip name="ФормаПровестиРасширеннаяПодсказка" id="2"/></Button></ChildItems>
  </AutoCommandBar>
  <Events>
    <Event name="OnCreateAtServer">ПриСозданииНаСервере</Event>
    <Event name="OnOpen">ПриОткрытии</Event>
  </Events>
  <ChildItems>
    <UsualGroup name="Группа" id="3"><ChildItems>
      <InputField name="Контрагент" id="4"><DataPath>Объект.Контрагент</DataPath>
        <Events><Event name="OnChange">КонтрагентПриИзменении</Event>
                <Event name="StartChoice">КонтрагентНачалоВыбора</Event></Events></InputField>
    </ChildItems></UsualGroup>
    <Table name="Товары" id="5"><DataPath>Объект.Товары</DataPath>
      <Events><Event name="OnActivateRow">ТоварыПриАктивизацииСтроки</Event></Events>
      <ChildItems><InputField name="ТоварыКоличество" id="6"><DataPath>Объект.Товары.Количество</DataPath>
        <Events><Event name="OnChange">ТоварыКоличествоПриИзменении</Event></Events></InputField></ChildItems>
    </Table>
  </ChildItems>
  <Attributes>
    <Attribute name="Объект" id="1"><Type><v8:Type>cfg:DocumentObject.Реализация</v8:Type></Type>
      <MainAttribute>true</MainAttribute>
      <Columns><AdditionalColumns table="Объект.Товары"><Column name="Доп" id="2"/></AdditionalColumns></Columns>
    </Attribute>
    <Attribute name="Прочее" id="3"><Type/></Attribute>
  </Attributes>
  <Commands>
    <Command name="Провести" id="1"><Title/><Action>ПровестиВыполнить</Action></Command>
    <Command name="Пустая" id="2"><Title/></Command>
  </Commands>
</Form>'''


def test_form():
    f = mp.parse_form(b(FORM))
    assert [(e.event, e.handler) for e in f.events] == [('OnCreateAtServer', 'ПриСозданииНаСервере'),
                                                       ('OnOpen', 'ПриОткрытии')]
    assert [(i.name, i.item_type, i.data_path, [(e.event, e.handler) for e in i.events]) for i in f.items] == [
        ('Контрагент', 'InputField', 'Объект.Контрагент',
         [('OnChange', 'КонтрагентПриИзменении'), ('StartChoice', 'КонтрагентНачалоВыбора')]),
        ('Товары', 'Table', 'Объект.Товары', [('OnActivateRow', 'ТоварыПриАктивизацииСтроки')]),
        ('ТоварыКоличество', 'InputField', 'Объект.Товары.Количество', [('OnChange', 'ТоварыКоличествоПриИзменении')]),
    ]
    assert [(c.name, [a.handler for a in c.actions]) for c in f.commands] == [
        ('Провести', ['ПровестиВыполнить']), ('Пустая', [])]
    assert f.item_count == 7          # command bar, button, tooltip, group, field, table, column
    assert [(e.name, e.item_type, e.data_path, e.parent, e.auxiliary) for e in f.elements] == [
        ('ФормаКоманднаяПанель', 'AutoCommandBar', None, -1, False),
        ('ФормаПровести', 'Button', None, 0, False),
        ('ФормаПровестиРасширеннаяПодсказка', 'ExtendedTooltip', None, 1, True),
        ('Группа', 'UsualGroup', None, -1, False),
        ('Контрагент', 'InputField', 'Объект.Контрагент', 3, False),
        ('Товары', 'Table', 'Объект.Товары', -1, False),
        ('ТоварыКоличество', 'InputField', 'Объект.Товары.Количество', 5, False),
    ]
    assert f.main_attribute == 'Объект' and f.main_attribute_types == ['obj:Документ.Реализация']
    assert mp.form_event_ru('OnCreateAtServer') == 'ПриСозданииНаСервере'


def test_extension_form_call_types():
    x = '''<?xml version="1.0" encoding="UTF-8"?>
<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.20">
  <Events>
    <Event name="OnCreateAtServer" callType="After">Мр_ПриСозданииНаСервереПосле</Event>
  </Events>
  <ChildItems><InputField name="Склад" id="7"><Events>
    <Event name="OnChange" callType="Override">Мр_СкладПриИзменении</Event></Events></InputField></ChildItems>
  <Commands><Command name="Мр_Команда" id="9"><Action callType="Before">Мр_КомандаПеред</Action></Command></Commands>
  <BaseForm version="2.20"><Events><Event name="OnCreateAtServer">ПриСозданииНаСервере</Event></Events></BaseForm>
</Form>'''
    f = mp.parse_form(b(x))
    assert f.base_form
    assert [(e.event, e.handler, e.call_type) for e in f.events] == [
        ('OnCreateAtServer', 'Мр_ПриСозданииНаСервереПосле', 'После')]
    assert f.items[0].events[0].call_type == 'Вместо'
    assert f.commands[0].actions[0].call_type == 'Перед'


def test_layout_classify():
    c = classify
    assert c('Configuration.xml').kind == 'config'
    e = c('Ext/SessionModule.bsl')
    assert (e.kind, e.module_name) == ('module', 'Конфигурация.МодульСеанса')
    e = c('Documents/Р.xml')
    assert (e.kind, e.owner) == ('object', 'Документ.Р')
    assert c('Documents/Р/Ext/ObjectModule.bsl').module_name == 'Документ.Р.МодульОбъекта'
    assert c('CommonModules/ОН/Ext/Module.bsl').module_name == 'ОбщийМодуль.ОН.Модуль'
    assert c('AccumulationRegisters/Т/Ext/RecordSetModule.bsl').module_name == 'РегистрНакопления.Т.МодульНабораЗаписей'
    assert c('Documents/Р/Forms/Ф.xml').kind == 'form_meta'
    e = c('Documents/Р/Forms/Ф/Ext/Form.xml')
    assert (e.kind, e.owner) == ('form', 'Документ.Р.Форма.Ф')
    assert c('Documents/Р/Forms/Ф/Ext/Form/Module.bsl').module_name == 'Документ.Р.Форма.Ф.Модуль'
    assert c('CommonForms/ОФ/Ext/Form.xml').owner == 'ОбщаяФорма.ОФ'
    assert c('CommonForms/ОФ/Ext/Form/Module.bsl').module_name == 'ОбщаяФорма.ОФ.Модуль'
    assert c('Documents/Р/Commands/Печать/Ext/CommandModule.bsl').module_name == 'Документ.Р.Команда.Печать.МодульКоманды'
    assert c('CommonCommands/К/Ext/CommandModule.bsl').module_name == 'ОбщаяКоманда.К.МодульКоманды'
    assert c('Constants/К/Ext/ValueManagerModule.bsl').module_name == 'Константа.К.МодульМенеджераЗначения'
    for skip in ('Documents/Р/Ext/Help.xml', 'Documents/Р/Templates/Т.xml', 'Roles/Р/Ext/Rights.xml',
                 'CommonPictures/К/Ext/Picture.png', 'Documents/Р/Forms/Ф/Ext/Help.xml',
                 'Subsystems/А/Subsystems/Б.xml', 'Ext/ParentConfigurations.bin'):
        assert c(skip) is None, skip
