# Approach

- **Mode:** existing project, inside its stack
- **Project standards:** `project_frontend` (STACK.md, TESTING.md)

## Known

- формы проекта идут через React Hook Form, отдельной библиотеки состояния нет (standards)
- проверка в браузере до отправки, API не трогать (task)
- тимлид назвал React Hook Form вместе с zod, как на странице аккаунта (teamlead answers)

## Unknown

- в стандартах не сказано, в какой момент показывать ошибку: при вводе или при отправке

## Rejected in the standards

- **Formik**
  > **Formik:** replaced by React Hook Form; two form libraries in one bundle.
- **Redux**
  > **Redux:** nothing in the app needs a global store.

## Options

### 1. zod-Schema über den Resolver

Правила полей описываются схемой zod, а схема подключается к React Hook Form через резолвер.

- **Uses:** React Hook Form, zod
- **Pros:** правила лежат одним местом и переиспользуются на других формах; типы полей выводятся из схемы
- **Cons:** в бандл добавляется схема-библиотека
- **Risks:** версия резолвера привязана к мажорной версии React Hook Form
- **Cost:** полдня, обе библиотеки уже названы тимлидом, лицензия MIT
- **Sources:** S1, S2

### 2. Eingebaute Regeln von React Hook Form

Правила задаются прямо при регистрации поля, без отдельной схемы.

- **Uses:** React Hook Form
- **Pros:** ни одной новой зависимости
- **Cons:** правила размазаны по разметке формы и не переиспользуются
- **Risks:** на второй форме правила придётся писать заново
- **Cost:** два-три часа, ничего не добавляется
- **Sources:** S1

## Recommendation

**zod-Schema über den Resolver**

тимлид прямо назвал эту пару, а стандарты проекта уже ведут формы через React Hook Form: схема ложится рядом и не заводит второй библиотеки форм.

- **Sources:** S1, S2

### How to write it

1. схему держать рядом с формой одним модулем, а не разносить правила по полям
2. подключать её через zodResolver в useForm, не вызывая проверку руками
3. сообщение об ошибке брать из состояния формы у самого поля
4. тесты на обязательные поля и формат e-mail писать на Vitest с Testing Library, как велит TESTING.md

### Project standards

- `STACK.md`
  > **Forms go through React Hook Form.** Uncontrolled inputs keep re-renders out of large forms.
- `TESTING.md`
  > Form validation rules: every required field and every format rule has a test.

## New questions for the teamlead

- Soll die Fehlermeldung schon beim Tippen erscheinen oder erst beim Absenden des Login-Formulars?
  → Показывать ошибку уже при вводе или только при отправке формы входа?
  Why: от этого зависит режим проверки в шагах, а в стандартах проекта об этом ничего нет

## Sources

- **S1** Resolvers — https://example.org/react-hook-form/resolvers
- **S2** Strings — https://example.org/zod/strings

## Searches

- react hook form zod resolver validation
- zod schema email required field
