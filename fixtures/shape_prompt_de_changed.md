Plan this task with the project's standards before writing any code.

## Task

Login-Formular vor dem Absenden im Browser prüfen
→ Проверять форму входа в браузере до отправки

Die Felder des Login-Formulars im Browser prüfen, bevor etwas an den Server geht: E-Mail-Format und Pflichtfelder. Die API bleibt unverändert.
→ Проверять поля формы входа в браузере, пока ничего не ушло на сервер: формат e-mail и обязательные поля. API не меняется.

## Deadline

до пятницы (*bis Freitag*)

## Constraints

- проверка в браузере, до отправки на сервер (*Die Felder sollen im Browser geprüft werden, bevor irgendwas an den Server geht*)
- формат e-mail и обязательные поля (*also E-Mail-Format und Pflichtfelder*)

## Do not

- трогать API: он принадлежит Backend-Team (*Bitte die API nicht anfassen, die gehört dem Backend-Team.*)

## Teamlead answers

> zu 1: Nehmt React Hook Form mit zod, das haben wir im Projekt schon für die Kontoseite.

## Research

Eingebaute Regeln von React Hook Form
→ Встроенные правила React Hook Form

Approach was changed at approval: the research below compared options for a different choice.

### Sources

- Resolvers — https://example.org/react-hook-form/resolvers
- Strings — https://example.org/zod/strings

## Steps

1. Validierungsschema mit zod für E-Mail-Format und Pflichtfelder anlegen
   → Описать схему проверки на zod для e-mail и обязательных полей
2. [уточнить: Frage 1] Schema über React Hook Form an das Login-Formular anbinden und Absenden mit Fehlern verhindern
   → [уточнить: вопрос 1] Подключить схему к форме входа через React Hook Form и не отправлять форму с ошибками
3. Fehlermeldung unter dem jeweiligen Feld anzeigen
   → Показывать сообщение об ошибке под полем
4. Prüfen, dass bei ungültigen Feldern keine Anfrage an die API geht
   → Проверить, что с неверными полями запрос к API не уходит

## Open questions

The teamlead has not answered these. Do not assume an answer: ask.

1. Gilt die Prüfung vor dem Absenden nur für das Login-Formular oder auch für das Registrierungsformular?
   → Проверка до отправки только для формы входа или и для формы регистрации?
3. Werden für die Prüfung des Login-Formulars zusätzlich automatisierte Tests erwartet?
   → Ждут ли для проверки формы входа ещё и автотесты?
4. Soll die Fehlermeldung schon beim Tippen erscheinen oder erst beim Absenden des Login-Formulars?
   → Показывать ошибку уже при вводе или только при отправке формы входа?
