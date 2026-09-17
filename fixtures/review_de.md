# Meeting review

## 1. Проверка формы входа до отправки на сервер
Проверять поля формы входа в браузере, пока ничего не ушло на сервер: формат e-mail и обязательные поля. API не трогать, он принадлежит Backend-Team.

- **Assigned by:** Teamlead
- **Assignee:** du (имя не названо)
- **Status:** decision
- **Deadline:** до пятницы (*bis Freitag*)

### Constraints
- проверка в браузере, до отправки на сервер (*Die Felder sollen im Browser geprüft werden, bevor irgendwas an den Server geht*)
- формат e-mail и обязательные поля (*also E-Mail-Format und Pflichtfelder*)

### Do not
- трогать API: он принадлежит Backend-Team (*Bitte die API nicht anfassen, die gehört dem Backend-Team.*)

### Ask back
- касается ли проверка и регистрации: тимлид сам этого ещё не знает
  > Gilt die Prüfung auch für die Registrierung?
  >
  > → Проверка касается и регистрации?

### Quotes
> Erstens das Login-Formular.
>
> → Во-первых, форма входа.

> Kannst du das bis Freitag machen?
>
> → Сможешь сделать это до пятницы?

## 2. Сообщения об ошибках на странице аккаунта на немецком
Перевести сообщения об ошибках на странице аккаунта с английского на немецкий; тексты пришлёт Julia из Produktteam. Срочность ниже, чем у проверки формы входа, срок не назван.

- **Assigned by:** Teamlead
- **Assignee:** du (имя не названо)
- **Status:** decision
- **Deadline:** —

### Constraints
- не срочно (*das ist nicht so dringend*)
- тексты пришлёт Julia из Produktteam (*die Texte schickt dir Julia aus dem Produktteam*)
- в проекте уже есть i18next (*wir haben schon i18next im Projekt*)

### Do not
- брать для этого новую библиотеку (*Nimm dafür bitte keine neue Bibliothek*)

### Quotes
> Die Fehlermeldungen auf der Kontoseite sind noch auf Englisch. *(not found verbatim in transcript)*
>
> → Сообщения об ошибках на странице аккаунта всё ещё на английском.

> das ist nicht so dringend … Die sollen auf Deutsch sein
>
> → это не так срочно … Они должны быть на немецком
