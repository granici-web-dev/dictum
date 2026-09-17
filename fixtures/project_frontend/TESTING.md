<!-- Составлено вручную для тестов, не стратегия тестирования реального проекта. -->
# TESTING

## Worth testing
- Form validation rules: every required field and every format rule has a test.
- Components that branch on API errors.

## Not worth testing
- Styling and snapshot tests of markup.

## Tools
- Vitest with Testing Library; the network is mocked at the API client, never with a real server.
