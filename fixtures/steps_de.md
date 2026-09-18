# Login-Formular vor dem Absenden im Browser prüfen
→ Проверять форму входа в браузере до отправки

Die Felder des Login-Formulars im Browser prüfen, bevor etwas an den Server geht: E-Mail-Format und Pflichtfelder. Die API bleibt unverändert.
→ Проверять поля формы входа в браузере, пока ничего не ушло на сервер: формат e-mail и обязательные поля. API не меняется.

- **From review:** `3f9c1a7e5b2d8c40`, task 1
- **Deadline:** до пятницы (*bis Freitag*)
- **Teamlead answers:** received 2026-09-19 08:14 UTC
- **Project standards:** not set

## Constraints

- проверка в браузере, до отправки на сервер (*Die Felder sollen im Browser geprüft werden, bevor irgendwas an den Server geht*)
- формат e-mail и обязательные поля (*also E-Mail-Format und Pflichtfelder*)

## Do not

- трогать API: он принадлежит Backend-Team (*Bitte die API nicht anfassen, die gehört dem Backend-Team.*)

## Steps

1. Validierungsschema mit zod für E-Mail-Format und Pflichtfelder anlegen
   → Описать схему проверки на zod для e-mail и обязательных полей
2. [уточнить: Frage 1] Schema über React Hook Form an das Login-Formular anbinden und Absenden mit Fehlern verhindern
   → [уточнить: вопрос 1] Подключить схему к форме входа через React Hook Form и не отправлять форму с ошибками
3. Fehlermeldung unter dem jeweiligen Feld anzeigen
   → Показывать сообщение об ошибке под полем
4. Prüfen, dass bei ungültigen Feldern keine Anfrage an die API geht
   → Проверить, что с неверными полями запрос к API не уходит

## Questions for the teamlead

1. Gilt die Prüfung vor dem Absenden nur für das Login-Formular oder auch für das Registrierungsformular? *(no answer)*
   → Проверка до отправки только для формы входа или и для формы регистрации?
2. Soll für die Prüfung des Login-Formulars eine bestimmte Bibliothek verwendet werden, oder ist die Wahl frei?
   → Нужна ли для проверки формы входа определённая библиотека, или выбор свободный?
3. Werden für die Prüfung des Login-Formulars zusätzlich automatisierte Tests erwartet? *(no answer)*
   → Ждут ли для проверки формы входа ещё и автотесты?

## Teamlead answers

> zu 1: Nehmt React Hook Form mit zod, das haben wir im Projekt schon für die Kontoseite.
