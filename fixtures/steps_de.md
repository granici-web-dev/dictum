# Login-Formular vor dem Absenden im Browser prüfen
→ Проверять форму входа в браузере до отправки

Die Felder des Login-Formulars im Browser prüfen, bevor etwas an den Server geht: E-Mail-Format und Pflichtfelder. Die API bleibt unverändert.
→ Проверять поля формы входа в браузере, пока ничего не ушло на сервер: формат e-mail и обязательные поля. API не меняется.

- **From review:** `3f9c1a7e5b2d8c40`, task 1
- **Deadline:** до пятницы (*bis Freitag*)

## Constraints

- проверка в браузере, до отправки на сервер (*Die Felder sollen im Browser geprüft werden, bevor irgendwas an den Server geht*)
- формат e-mail и обязательные поля (*also E-Mail-Format und Pflichtfelder*)

## Do not

- трогать API: он принадлежит Backend-Team (*Bitte die API nicht anfassen, die gehört dem Backend-Team.*)

## Steps

1. Prüfregeln für E-Mail-Format und Pflichtfelder festlegen
   → Описать правила проверки для e-mail и обязательных полей
2. Prüfung an das Login-Formular anbinden und Absenden mit Fehlern verhindern
   → Подключить проверку к форме входа и не отправлять форму с ошибками
3. Fehlermeldung unter dem jeweiligen Feld anzeigen
   → Показывать сообщение об ошибке под полем
4. Prüfen, dass bei ungültigen Feldern keine Anfrage an die API geht
   → Проверить, что с неверными полями запрос к API не уходит

## Ask back

- касается ли проверка и регистрации: тимлид сам этого ещё не знает
  > Gilt die Prüfung auch für die Registrierung?
  >
  > → Проверка касается и регистрации?
