# My Open MCPs

Открытый marketplace плагинов для Codex.

## Доступные плагины

- [`telegram-alert-mcp`](plugins/telegram-alert-mcp) — безопасные дедуплицируемые
  алерты в заранее настроенный Telegram-чат для циклических workflow.

## Подключение к Codex

```zsh
codex plugin marketplace add Goldmanitu/my-open-mcps --ref main
codex plugin add telegram-alert-mcp@goldmanitu-my-open-mcps
```

После установки начните новую задачу Codex. Настройка Telegram и модель
безопасности описаны в README самого плагина.

## Лицензия

Каждый плагин содержит собственный файл лицензии. Telegram Alerts опубликован под
лицензией MIT.
