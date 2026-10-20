# JARVIS WorkOS

Голосовой ассистент для Omarchy Linux: окно-компаньон, локальные команды без ИИ, Gemini для свободных вопросов,
единая система разрешений и проверяемые результаты. Первый клиентский сценарий — проект «Флора» на
синтетических демо-данных.

## Запуск

```bash
cd ~/Projects/jarvis
scripts/install.sh            # команда `jarvis` + пункт «JARVIS» в меню приложений (без root)
jarvis                        # запускает ядро (если не запущено) и открывает окно
```

Необязательно:

```bash
scripts/install.sh --service          # ядро как служба systemd --user (старт при входе)
scripts/omarchy-integrate.sh apply    # индикатор в Waybar + Super+H / Super+Shift+H (с резервной копией)
scripts/omarchy-integrate.sh remove   # убрать интеграцию
jarvis setup-gemini                   # ключ Gemini в GNOME Keyring (скрытый ввод); или Настройки → Gemini
```

Другие команды: `jarvis status`, `jarvis stop` (экстренная остановка), `jarvis resume`, `jarvis say "текст"`.

## Что умеет сейчас

| Возможность | Статус |
|---|---|
| Окно с компаньоном, беседа, задачи, настройки (русский интерфейс, тёмная/светлая тема) | VERIFIED LOCAL (Omarchy 3.8.3, реальный экран) |
| Локальные команды без ИИ: система, окна, рабочие столы, громкость, приложения, поиск/открытие файлов, заметки, напоминания, снимок экрана | VERIFIED LOCAL |
| Разрешения: низкий/средний/высокий риск, подтверждение с привязкой к точным параметрам, правка перед подтверждением, экстренная остановка | VERIFIED LOCAL (тесты + UI) |
| Задачи в SQLite: состояния, шаги, проверка результата, пауза/отмена, восстановление после перезапуска | VERIFIED LOCAL (тесты) |
| Gemini: потоковые ответы, вызов инструментов через ту же политику | VERIFIED MOCK — без ключа живой вызов не проверен |
| Голосовой ввод (по нажатию, распознавание Gemini), озвучивание (Gemini TTS) | IMPLEMENTED UNVERIFIED — нужен ключ |
| «Флора»: разбор почты с доказательствами, сравнение КП, чертежи с пометками, черновик, отправка с подтверждением | VERIFIED MOCK (синтетические данные, демо-почта) |
| Индикатор Waybar, горячие клавиши Hyprland | VERIFIED LOCAL на копии конфигурации; в вашу панель не установлено |

Подробно: [docs/acceptance/TEST_MATRIX.md](docs/acceptance/TEST_MATRIX.md).

## Примеры команд

«Состояние системы» · «Какие окна открыты» · «Переключись на третий рабочий стол» · «Громкость 40» ·
«Открой калькулятор» · «Найди файл смета» → «Открой второй» · «Запиши заметку: позвонить фабрике» ·
«Напомни через 10 минут проверить почту» · «Сделай скриншот» · «Что важного по Флоре?» · «Сравни сметы по Флоре» ·
«Подготовь ответ по Флоре» · «Пауза» / «Продолжи» / «Отмени» / «Стоп».

## Устройство

```
окно (Chromium --app, web/)  ⇄  WebSocket (127.0.0.1, токен + cookie + проверка Origin/Host)
        │                                   │
Waybar-модуль (SSE, bearer)          ядро jarvis/ (Python, aiohttp)
                                       ├─ router.py      — команды без ИИ (уровень 0/1)
                                       ├─ director.py    — задачи, Gemini-цикл, пауза/отмена/стоп
                                       ├─ gateway.py     — единственный путь к инструментам
                                       ├─ policy.py      — риск, подтверждения (digest), сессионные разрешения
                                       ├─ tools/         — система, файлы, рабочий стол
                                       ├─ scheduler.py   — напоминания
                                       ├─ providers/gemini.py — google-genai, ручной вызов функций
                                       └─ flora/         — демо-почта, анализ, сметы, чертежи, черновики
SQLite: ~/.local/share/jarvis/jarvis.db · журнал аудита: ~/.local/state/jarvis/audit.jsonl
Ключи: GNOME Keyring (secret-tool), не в файлах и не в журналах.
```

## Разработка

```bash
uv run pytest -q                                      # 84 теста
uv run python scripts/bench.py 40                     # задержки локальных команд
scripts/dev-server.sh start /tmp/jh                   # изолированное ядро (порт из /tmp/jh/config/config.toml)
cd tests/ui && npm install && node drive.mjs <port> <token> <out> smoke|flora   # снимки UI
uv run python scripts/make_flora_fixtures.py          # пересоздать демо-данные
```

Зависимости: google-genai 2.31.0 (Apache-2.0), aiohttp 3.14.5 (Apache-2.0/MIT), openpyxl 3.1.5 (MIT);
для тестов UI — playwright-core (Apache-2.0) с системным Chromium. Персонаж-компаньон нарисован с нуля.
