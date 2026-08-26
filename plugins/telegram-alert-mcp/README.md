# Telegram Alerts — публичный учебный плагин Codex

Плагин показывает на одном небольшом примере, как связаны:

```text
Schedule → Mail MCP → Agent + Skill → Telegram MCP → Telegram Bot API
```

Он предоставляет Codex один внешний инструмент:

```text
send_alert(message, event_id)
```

- `message` — короткий текст алерта;
- `event_id` — стабильный ID исходного события, защищающий циклический workflow
  от повторной отправки одного и того же алерта.

Получатель не является аргументом инструмента. Он заранее фиксируется через
`TELEGRAM_CHAT_ID`, поэтому модель не может самостоятельно выбрать другой чат.

## Состав плагина

```text
.codex-plugin/plugin.json          публичный манифест
.mcp.json                          подключение MCP к Codex
skills/telegram-alert-workflow/    правила работы агента
telegram_alert_mcp/                MCP-сервер и Telegram Bot API
WORKFLOW.md                        готовое учебное задание
SECURITY_REVIEW.md                 модель безопасности
PRIVACY.md                         обработка данных
```

Плагин распространяется через marketplace репозитория
[`Goldmanitu/my-open-mcps`](https://github.com/Goldmanitu/my-open-mcps).

## Установка из публичного marketplace

### 1. Установить Python runtime для MCP

Один раз создайте отдельное окружение. Плагин автоматически найдёт его:

```zsh
python3 -m venv "$HOME/.local/share/telegram-alert-mcp/venv"
"$HOME/.local/share/telegram-alert-mcp/venv/bin/pip" install 'mcp>=1.27,<2'
```

### 2. Добавить marketplace и плагин

```zsh
codex plugin marketplace add Goldmanitu/my-open-mcps --ref main
codex plugin add telegram-alert-mcp@goldmanitu-my-open-mcps
```

После установки начните новую задачу Codex, чтобы она получила skill и MCP tool.

## Настройка Telegram на macOS

### 1. Создать бота

1. Откройте `@BotFather` в Telegram.
2. Выполните `/newbot`.
3. Сохраните полученный BotFather token как пароль.
4. Напишите созданному боту `/start`.
5. Определите числовой chat ID пользователя или учебной группы.

Не вставляйте токен в чат с AI, Git, README, issue, скриншот или команду shell.

### 2. Сохранить настройки в Keychain

Обе команды открывают защищённый системный запрос и не содержат секрет в истории:

```zsh
security add-generic-password -U -s telegram-alert-mcp -a bot-token -w
security add-generic-password -U -s telegram-alert-mcp -a chat-id -w
```

В первый запрос вставьте BotFather token, во второй — числовой chat ID. Launcher
плагина извлекает их непосредственно перед запуском MCP.

На других ОС можно передать `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` через
защищённое окружение процесса Codex.

## Первый тест

Откройте новую задачу Codex и напишите:

```text
Используй Telegram Alerts. Отправь в настроенный чат тестовый алерт:
«Учебный тест Telegram MCP». event_id = course:test:001.
```

Отправка является внешним действием. Codex должен показать вызов `send_alert` для
подтверждения. Повторный вызов с тем же `event_id` и текстом вернёт
`duplicate_suppressed` и не создаст второе сообщение.

## Циклический workflow

Готовый промпт находится в [`WORKFLOW.md`](WORKFLOW.md). Для почтовых событий
используйте неизменяемый ID сообщения и версию правила:

```text
gmail:<Message-ID>:suspension-v1
yandex:<folder>:<UID>:suspension-v1
```

Нельзя использовать текущую дату, случайный UUID или ID запуска расписания: эти
значения меняются в каждом цикле и отключают дедупликацию.

## Безопасность

- Токен и chat ID не являются аргументами модели.
- Разрешены только числовые chat ID.
- Отправляется обычный текст без `parse_mode`; HTML/Markdown из письма не исполняется.
- Ссылочные preview отключены, `protect_content` по умолчанию включён.
- Ошибки не содержат токен, URL запроса или текст Telegram API.
- Успешные события фиксируются в локальном SQLite.
- Одинаковый `event_id` с изменённым текстом отклоняется.

Telegram Bot API не предоставляет idempotency key для `sendMessage`. После редкого
неопределённого сетевого тайм-аута нельзя гарантировать строго однократную доставку:
следующий цикл может повторить уже принятое Telegram сообщение. Для аварийных
алертов выбран принцип «редкий дубль лучше потерянного уведомления».

Подробнее: [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md) и [`PRIVACY.md`](PRIVACY.md).

## Локальная разработка

```zsh
git clone https://github.com/Goldmanitu/my-open-mcps.git
cd my-open-mcps/plugins/telegram-alert-mcp
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

Проверка структуры плагина выполняется официальным валидатором `plugin-creator`.

## Лицензия

[MIT](LICENSE). Плагин предназначен в том числе для учебных курсов, демонстраций и
самостоятельных экспериментов.
