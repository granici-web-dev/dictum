<!-- Составлено вручную для тестов, не стек реального проекта. -->
# STACK

Written in the shape `/rigorous document` produces: a table of choices, decisions with reasons,
rejected technologies with reasons.

| Concern | Choice | Pin |
|---|---|---|
| Language | TypeScript, strict mode | 5.x |
| UI | React | 18.x |
| Build | Vite | 5.x |
| Forms | React Hook Form | 7.x |
| Styling | CSS Modules | |
| Translations | i18next with react-i18next | 23.x |
| Unit tests | Vitest, Testing Library | |

## Decisions
- **Forms go through React Hook Form.** Uncontrolled inputs keep re-renders out of large forms.
- **No global state library.** Server data comes through the API client, local state stays in
  components.

## Rejected
- **Formik:** replaced by React Hook Form; two form libraries in one bundle.
- **Redux:** nothing in the app needs a global store.
