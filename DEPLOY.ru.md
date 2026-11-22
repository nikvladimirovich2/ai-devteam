[English](DEPLOY.md) · **Русский** · [Srpski](DEPLOY.sr.md) · [中文](DEPLOY.zh.md)

# Руководство по развёртыванию

Пошаговая установка AI DevTeam на чистый хост. Занимает ~15 минут.

**Быстрый путь** — установщик одной командой из [README](README.md#quick-start): он скачивает
готовый образ (`ghcr.io/nikvladimirovich2/ai-devteam`), запускает команду и открывает браузер, где
мастер настройки (⚙ Настройка) запрашивает ключ модели и токен GitHub/GitLab. Это руководство — для серверов,
закрытых сетей и всего, что вместо этого можно настроить в `.env`.

**Windows?** Запускайте через CLI контейнеров WSL (`wslc`), Docker Desktop не нужен: см.
[11. Windows: контейнеры WSL (wslc)](#11-windows-контейнеры-wsl-wslc).

## 1. Требования

| | Минимум | Рекомендуется |
|---|---|---|
| ОС | Linux x86_64 (Debian 12/13, Ubuntu 22.04+, Astra, RHEL 9) | — |
| CPU / RAM | 4 vCPU / 8 ГБ | 8 vCPU / 16 ГБ (агенты запускают pip/npm/pytest) |
| Диск | 20 ГБ | 50 ГБ (образ ~1,7 ГБ + проекты + кэши pip/npm) |
| ПО | Docker Engine 24+, Docker Compose v2, git | — |
| Сеть | HTTPS до LLM API и до GitLab/GitHub; pypi.org / registry.npmjs.org для агентов | — |

Сама LLM **не** входит в этот стек — агенты обращаются к уже существующему OpenAI-совместимому эндпоинту
(vLLM, Ollama, прокси LiteLLM, GigaChat, …). GPU на этом хосте не нужен.

## 2. Установка Docker

Debian / Ubuntu (пакеты дистрибутива):

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose docker-buildx git curl jq
sudo systemctl enable --now docker
docker version && docker compose version
```

(Или Docker CE с download.docker.com — подойдёт любой Docker 24+ с плагином `compose`.)
Пользователь без root: `sudo usermod -aG docker $USER` и перелогиньтесь.

## 3. Получение кода

```bash
sudo mkdir -p /opt/devteam && sudo chown $USER: /opt/devteam
git clone https://github.com/<you>/ai-devteam.git /opt/devteam
cd /opt/devteam
```

## 4. Настройка `.env`

```bash
cp .env.example .env
chmod 600 .env
echo "INTERNAL_SECRET=$(openssl rand -hex 32)" >> .env
$EDITOR .env
```

Обязателен только `INTERNAL_SECRET`. Модель, ключ и Git можно задать **либо** здесь, **либо** в
браузере (⚙ Настройка → *Проверить и сохранить*). Значения из `.env` имеют приоритет; мастер показывает их как «задано в .env».
Настройки мастера хранятся в томах: модель и ключ — в `leaddata` (lead), токен Git — в
`devopsdata` (только devops, права 0600, недоступен для shell-пользователя агентов).

Настройка в `.env`:

| Переменная | Пример | Примечания |
|---|---|---|
| `LLM_MODEL` | `hosted_vllm/Qwen/Qwen3-32B-AWQ` | формат LiteLLM `<provider>/<model id from /v1/models>` |
| `LLM_BASE_URL` | `https://llm.corp.local/v1` | для vLLM/OpenAI-совместимых API — включая `/v1` |
| `LLM_API_KEY` | | один ключ на всю команду… |
| `LEAD_API_KEY` … `DEVOPS_API_KEY` | | …или 6 персональных ключей, по одному на роль (приоритетнее `LLM_API_KEY`) |
| `GIT_URL` | `https://gitlab.corp.local` или `https://github.com` | провайдер определяется по URL |
| `GIT_TOKEN` | | см. «Токены Git» ниже |
| `GIT_NAMESPACE` | `ai-team` | группа GitLab (обязательно) / организация GitHub (пусто = личный аккаунт) |
| `INTERNAL_SECRET` | `openssl rand -hex 32` | ≥ 32 символов; подписывает запросы между контейнерами (см. ниже) |

Рекомендуется:

| Переменная | Зачем |
|---|---|
| `UI_USER`, `UI_PASSWORD` | Basic Auth для веб-интерфейса |
| `ARCHITECT_MODEL`, `BACKEND_MODEL`, … | отдельная модель для каждой роли — см. [MODELS.md](MODELS.md) |

> **`$` в значениях.** Compose раскрывает `$VAR` внутри `.env`. Ключ, содержащий `$`, нужно заключать в одинарные кавычки:
> `QA_API_KEY='$abc#def'`, иначе его часть молча теряется.

### Изоляция агентов

Агенты выполняют shell-команды, которые пишет модель, а промпт-инъекция (README пакета, веб-страница,
текст задачи) может заставить модель выполнить что угодно. Два уровня защиты не подпускают эти команды
к ключам и токену Git:

- **Отдельный shell-пользователь.** Сервис работает от `agent` (uid 1000) и хранит API-ключ роли (а
  в `devops` — ещё и `GIT_TOKEN`). Каждая команда из `run_shell` выполняется от `runner` (uid 1001) с чистым
  окружением, поэтому `/proc/<pid>/environ` и память агента недоступны для чтения. Оба пользователя
  состоят в общей группе, так что оба могут редактировать файлы проекта. Токен Git передаётся в `git push` через
  окружение, а не в командной строке (`/proc/<pid>/cmdline` доступен для чтения любому пользователю).
- **Подписанные внутренние запросы.** Lead и воркеры подписывают каждый внутренний запрос HMAC-токеном
  роли, производным от `INTERNAL_SECRET`. Без него `POST devops:8000/publish` и
  `POST lead:8000/api/internal/events` возвращают `403`, поэтому команда не может опубликовать код под токеном
  DevOps или отправить поддельное событие `✅ pushed`. Роль события берётся из токена, а не из
  тела запроса. Воркер хранит только свой токен; сам секрет остаётся у lead.

`scripts/check.sh` проверяет и то, и другое (`shell user cannot read the agent environment`, `internal
endpoints reject unsigned calls`). И всё же: выдавайте токену только необходимые права, желательно для
отдельной организации / аккаунта.

### Токены Git

**GitLab** — Personal или Group Access Token, скоупы `api` + `write_repository`
(пользователь должен иметь право создавать проекты в `GIT_NAMESPACE`).

**GitHub** — fine-grained PAT: Settings → Developer settings → Fine-grained tokens:
- Resource owner = организация из `GIT_NAMESPACE` (или вы сами), **Repository access: All repositories**
  (агенты создают новые репозитории — «Only select» их не покрывает);
- Repository permissions: **Administration RW, Contents RW, Workflows RW, Actions R** (Metadata R выставляется автоматически).
- В организации: fine-grained токены должны быть разрешены (и одобрены, если включено одобрение), SAML SSO — авторизован.

Альтернатива — классический PAT: скоупы `repo` + `workflow`.

### Самоподписанные / корпоративные сертификаты

Если LLM или GitLab работают по HTTPS с приватным CA:

```bash
cp /path/to/ca-or-server.crt agent/certs/       # PEM, -----BEGIN CERTIFICATE-----
docker compose restart
```

Папка (`CERTS_DIR`, по умолчанию `./agent/certs`; установщики используют `./certs`) монтируется в `/certs` в каждом
контейнере; при старте сертификаты добавляются в доверенный бандл для Python (certifi,
`REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`), git (`GIT_SSL_CAINFO`) и node (`NODE_EXTRA_CA_CERTS`). Пересборка не нужна.
Если вы собираете образ сами, сертификаты из `agent/certs/` также запекаются в него.
SAN сертификата должен содержать хост из `LLM_BASE_URL` (для IP — `IP Address:`).
`agent/certs/*.crt` находится в git-ignore: копируйте его на каждый хост.

## 5. Требования к LLM-серверу

Агенты работают через **вызовы инструментов** (`write_file`, `run_shell`, `publish_repository`).
Эндпоинт модели должен возвращать нативные `tool_calls`:

- **vLLM**: запускайте с `--enable-auto-tool-choice --tool-call-parser <parser>`
  (`hermes` для Qwen2.5/Qwen3, `llama3_json` для Llama 3.x, `mistral` для Mistral, …).
- **Ollama**: используйте модель с возможностью `tools`.
- Контекст: рекомендуется ≥ 32k токенов (`--max-model-len 32768`); размер окна автоматически читается из
  vLLM `/v1/models` (`max_model_len`) или задаётся через `LLM_CONTEXT_WINDOW`.

Быстрая ручная проверка:

```bash
curl -s --cacert agent/certs/llm.crt "$LLM_BASE_URL/models" -H "Authorization: Bearer <key>" | jq '.data[].id'
```

## 6. Запуск

```bash
docker compose up -d         # pulls the prebuilt image
docker compose ps            # 6 containers, all (healthy) after ~30 s
./scripts/check.sh           # end-to-end pre-flight check
```

Нет доступа к ghcr.io (закрытая сеть)? Соберите образ прямо на хосте:
`docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`,
или загрузите его из файла: `docker save ghcr.io/nikvladimirovich2/ai-devteam:latest | gzip > devteam.tgz` на машине
с доступом в интернет, затем `docker load < devteam.tgz`.

`scripts/check.sh` проверяет: заполненность `.env` и экранирование `$`, сертификаты, состояние контейнеров,
**доступ каждой роли к LLM со своим ключом**, работу нативного вызова инструментов, токен/namespace Git,
наличие токена Git только в `devops`, состояние UI.

## 7. Открытие UI

UI слушает только `127.0.0.1:${UI_PORT:-8080}`. С вашей рабочей станции:

```bash
ssh -L 8080:localhost:8080 user@<host>                      # direct
ssh -J jump-user@jump-host:port -L 8080:localhost:8080 user@<host>   # via a jump host
# → http://localhost:8080
```

Чтобы открыть его в сеть: `UI_BIND=0.0.0.0` **и** `UI_USER`/`UI_PASSWORD`, желательно за
обратным прокси с TLS (nginx/traefik).

## 8. Первая задача

Для начала отправьте что-нибудь небольшое (см. «Пример задачи» в README). В ленте должно появиться:
`🔎 architect: +N files` → `🔎 backend/frontend: …` → вердикт QA → `✅ pushed …` → `⏳ Waiting for CI…` →
`✅ CI passed` → итоговый текстовый отчёт с расходом токенов по ролям.

## 9. Эксплуатация

```bash
docker compose logs -f lead devops         # logs
docker compose restart                     # after editing .env (or: up -d to recreate)
docker compose pull && docker compose up -d   # update (source builds: git pull + the build file)
docker compose down                        # stop, data kept
docker compose down -v                     # stop and DELETE task history and workspaces
```

Резервное копирование — два тома:

```bash
docker run --rm -v devteam_leaddata:/d -v $PWD:/b alpine tar czf /b/leaddata.tgz -C /d .
docker run --rm -v devteam_workspace:/d -v $PWD:/b alpine tar czf /b/workspace.tgz -C /d .
```

## 10. Устранение неполадок

| Симптом | Причина / решение |
|---|---|
| `The "X" variable is not set` при `docker compose` | значение в `.env` содержит `$` — заключите его в одинарные кавычки |
| `CERTIFICATE_VERIFY_FAILED` | положите сертификат CA/сервера в папку сертификатов (`CERTS_DIR`), `docker compose restart` |
| Мастер пишет «Ключ отклонён» / «не вызывает инструменты» | в сообщении сказано, что делать; технические подробности — под ним |
| `401` от LLM | неверный `<ROLE>_API_KEY`; `./scripts/check.sh` покажет, у какой роли |
| Агент «отчитался» о работе, но `🔎 … did not create files` | модель не возвращает вызовы инструментов → флаги vLLM выше, `LLM_FUNCTION_CALLING=auto` |
| `Action 'write_file' don't exist` / зацикливание | для этой роли слабая в вызове инструментов модель → возьмите более сильную (MODELS.md) |
| `GitLab namespace … not found` | неверный `GIT_NAMESPACE` или у пользователя токена нет к нему доступа |
| GitHub `403` при создании репозитория | fine-grained токен не одобрен в организации / нет Administration RW / SSO не авторизован |
| `⚠ CI: no_pipeline` | конфигурация CI не запушена, CI отключён в проекте или нет раннеров |
| CI красный после `MAX_CI_ROUNDS` | откройте задачу → **🔧 CI**, чтобы запустить ещё раунды исправлений, или исправьте вручную |
| Контейнер lead `unhealthy`, UI завис | `docker compose logs lead`; `docker compose restart lead` (выполняемая задача станет `interrupted`) |
| `INTERNAL_SECRET is missing or shorter than 32 characters` | добавьте `INTERNAL_SECRET=$(openssl rand -hex 32)` в `.env`, `docker compose up -d` |
| `403 internal endpoint` в логах | контейнеры запущены с разными значениями `INTERNAL_SECRET` (старые контейнеры): пересоздайте все через `docker compose up -d --force-recreate` |

## 11. Windows: контейнеры WSL (wslc)

`wslc.exe` — встроенный в WSL CLI для контейнеров. Он запускает тот же образ в легковесной Linux-VM,
без Docker Desktop. Проверено на Windows 10 22H2 (сборка 19044) с WSL 3.0.3.

### 11.1 Требования

| | Минимум | Примечания |
|---|---|---|
| Windows | Windows 10 2004+ (сборка 19041) или Windows 11, x64 | |
| WSL | **3.0.1 или новее** (`wsl --version`) | `wslc` поставляется вместе с WSL, больше ничего устанавливать не нужно |
| Виртуализация | VT-x/AMD-V в BIOS + компонент «Платформа виртуальной машины» | внутри VM: вложенная виртуализация |
| RAM | 8 ГБ на хосте | замерено: ~2 ГБ на 6 простаивающих агентов. VM wslc по умолчанию получает 4 ГБ (`wslc settings`); при параллельной работе pip/npm/pytest увеличьте до 6 ГБ |
| Диск | 10 ГБ свободно на диске с `%LOCALAPPDATA%\wslc` | образ 1,66 ГБ, диск VM `storage.vhdx` вырастает до ~3,3 ГБ после сборки, плюс проекты и кэши |
| ПО | PowerShell 5.1+ (встроен), git | `docker` / `docker compose` **не** нужны |
| Сеть | эндпоинт LLM, GitLab/GitHub, pypi.org, registry.npmjs.org | VM использует сеть Windows, включая VPN |

LLM не входит в стек: ноутбук без GPU вполне подойдёт, если `LLM_BASE_URL` указывает на сервер модели.

### 11.2 Установка / обновление WSL

В PowerShell **от имени администратора**, только если WSL ещё ни разу не устанавливался:

```powershell
wsl --install --no-distribution     # enables «Virtual Machine Platform»; restart Windows afterwards
```

Затем (от обычного пользователя):

```powershell
wsl --update
wsl --version                       # WSL version: 3.0.1 or newer
wslc version
wslc run --rm hello-world           # smoke test of the container runtime
```

### 11.3 Установка (одной командой)

В PowerShell (после установки WSL права администратора не нужны):

```powershell
irm https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.ps1 | iex
```

Установщик:
- запускает `wsl --update`, если `wslc` отсутствует (и подсказывает, что делать, если WSL не установлен вовсе);
- кладёт скрипты запуска/остановки в `%USERPROFILE%\ai-devteam`, подбирает свободный порт (8080, 8081, …) для `.env`;
- скачивает готовый образ (~1,7 ГБ) и запускает шесть контейнеров;
- открывает в браузере мастер настройки и создаёт на рабочем столе ярлык **AI DevTeam** (для запуска после перезагрузки).

Корпоративные сертификаты: `%USERPROFILE%\ai-devteam\certs\*.crt`, затем снова запустите через ярлык.

### 11.4 Из исходного кода (для разработчиков)

```powershell
git clone https://github.com/nikvladimirovich2/ai-devteam.git
cd ai-devteam
powershell -ExecutionPolicy Bypass -File .\scripts\wslc-up.ps1 -Build   # builds the image here (~8 min)
```

Особенности `.env` в Windows (необязательно, см. [4. Настройка `.env`](#4-настройка-env)):

- `INTERNAL_SECRET` можно оставить пустым: скрипт запуска сгенерирует его и запишет в `.env`.
- Скрипт читает значения буквально, поэтому `$` в ключах экранировать не нужно (кавычки при этом допускаются).
- Порт 8080 на машине разработчика часто занят: задайте, например, `UI_PORT=8099`.
  `netstat -ano | findstr :8099` покажет, свободен ли порт.
- Корпоративные сертификаты кладутся в `agent\certs\*.crt` (или в `CERTS_DIR`).
- Не коммитьте `.env`. Он есть в `.gitignore`. Чтобы ограничить доступ только своим пользователем: `icacls .env /inheritance:r /grant:r "$($env:USERNAME):F"`.
- Git с `core.autocrlf=true` (значение по умолчанию в Git for Windows) выгружает файлы с CRLF.
  Это проверено: образ собирается и работает. Env-файлы для ролей скрипт сам пишет с LF.

В wslc пока нет `compose`, поэтому `scripts\wslc-up.ps1` делает то же, что и `docker-compose.yml`: одна сеть
`devteam`, тома `devteam_workspace`, `devteam_leaddata` и `devteam_devopsdata`, шесть контейнеров. Каждый
контейнер получает **только свой** ключ; токен Git — только `devteam-devops`. Временные env-файлы
удаляются сразу после каждого запуска. Скрипт отказывается работать с `UI_BIND`, отличным от `127.0.0.1`, без `UI_USER`/`UI_PASSWORD`.

```powershell
wslc list                                                              # 6 containers, (healthy) after ~30 s
```

Smoke-тест без модели (использует `tests/mock_llm.py`; публикация без Git-хоста завершается ошибкой — это ожидаемо):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\wslc-up.ps1 -Build -Mock
```

Отправьте в UI небольшую задачу: в ленте появятся architect → backend/frontend → QA (pytest) → DevOps,
задача завершится со статусом `done_no_push`.

Проверка безопасности (та же, что в `check.sh`):

```powershell
wslc exec -u runner devteam-devops sh -c "cat /proc/1/environ > /dev/null && echo LEAK || echo ok"   # ok
wslc exec -u runner devteam-backend curl -s -o /dev/null -w "%{http_code}" -X POST http://devops:8000/publish -d "{}"   # 403
```

Сам `scripts/check.sh` вызывает `docker` и с wslc не работает.

### 11.5 Эксплуатация

```powershell
wslc logs -f devteam-lead                                   # logs (any devteam-<role>)
wslc stats                                                  # CPU / memory per container
.\scripts\wslc-up.ps1                                       # after editing .env: recreates the containers
.\scripts\wslc-up.ps1                                       # update: downloads the latest image (source: git pull; -Build)
.\scripts\wslc-down.ps1                                     # stop, task history and projects kept
.\scripts\wslc-down.ps1 -Volumes                            # stop and DELETE history and projects
wslc settings                                               # VM CPU / memory / storage location
```

Отличия от Docker Compose:

- **Нет политики перезапуска.** После перезагрузки Windows снова запустите `wslc-up.ps1` (это делает ярлык на рабочем столе, созданный установщиком). Тома сохраняются.
  Для автозапуска: Планировщик заданий → «При входе в систему» → `powershell -ExecutionPolicy Bypass -File <path>\scripts\wslc-up.ps1`.
- Тестовый GitLab из `tests/docker-compose.test.yml` скрипт не запускает (ему нужно ~4 ГБ RAM).
- Резервное копирование: `wslc run --rm -v devteam_leaddata:/d -v ${PWD}:/b alpine tar czf /b/leaddata.tgz -C /d .`
  (аналогично для `devteam_workspace`).

### 11.6 Освобождение места на диске

```powershell
.\scripts\wslc-down.ps1 -Volumes
wslc rmi devteam-agent:latest
wslc image prune -a -f
```

Удаление образов не уменьшает диск VM `%LOCALAPPDATA%\wslc\sessions\<session>\storage.vhdx`
(~3 ГБ после сборки). Чтобы вернуть место: `wslc system session terminate`, затем удалите этот
`storage.vhdx`. При следующем запуске wslc создаст новый пустой диск. **Все образы и тома этой
сессии будут потеряны.**

### 11.7 Устранение неполадок (Windows)

| Симптом | Причина / решение |
|---|---|
| `wslc` не найден | WSL старше 3.0.1: `wsl --update` |
| `running scripts is disabled on this system` | запускайте через `powershell -ExecutionPolicy Bypass -File ...` |
| `bind: address already in use` / UI недоступен | порт занят другим приложением: измените `UI_PORT` |
| Контейнер завершается сразу после запуска | `wslc logs devteam-<role>`; обычно пустой `LLM_MODEL` или неверный ключ |
| Сборка падает на `pip install` / `apt-get` с ошибками TLS | корпоративный прокси/CA: положите CA в `agent\certs\`, пересоберите с `-Rebuild` |
| Агенты работают медленно или убиваются во время тестов | память VM: `wslc settings` → увеличьте память, затем `wslc system session terminate` |
| Изменения в `.env` не применяются | снова запустите `wslc-up.ps1`: он пересоздаёт контейнеры |

## 12. Остановка, запуск, обновление, удаление

Остановка ничего не удаляет: история задач, папки проектов, ключи и токены из «⚙ Настройка» сохраняются.
Удаляет их только **удаление с данными**. Репозитории, уже опубликованные в GitHub/GitLab, не затрагиваются никогда.

### Windows (установка через `install.ps1`)

| Действие | Как |
|---|---|
| Остановить | двойной щелчок по ярлыку **AI DevTeam - stop** на рабочем столе или `powershell -ExecutionPolicy Bypass -File "$HOME\ai-devteam\scripts\wslc-down.ps1"` |
| Запустить (в том числе после перезагрузки Windows) | двойной щелчок по ярлыку **AI DevTeam** на рабочем столе |
| Обновить до последней версии | запустить установщик ещё раз: `irm https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.ps1 \| iex` (данные и настройки сохраняются) |
| Состояние, логи | `wslc list` · `wslc logs -f devteam-lead` |
| Удалить, сохранив данные | `powershell -ExecutionPolicy Bypass -File "$HOME\ai-devteam\scripts\uninstall.ps1" -KeepData` |
| Удалить полностью | `powershell -ExecutionPolicy Bypass -File "$HOME\ai-devteam\scripts\uninstall.ps1"` (перед удалением данных спросит подтверждение; `-Yes` — без вопросов) |

`uninstall.ps1` удаляет контейнеры, сеть, образ (~1,7 ГБ), оба ярлыка на рабочем столе и папку `ai-devteam`,
а без `-KeepData` — ещё и тома `devteam_workspace`, `devteam_leaddata`, `devteam_devopsdata`. Если других
контейнеров и образов WSL не осталось, удаляется и диск виртуальной машины контейнеров WSL
(`%LOCALAPPDATA%\wslc\sessions\…\storage.vhdx`) — так освобождается оставшееся место. Сам WSL остаётся установленным.

### Linux / macOS (установка через `install.sh`)

```bash
cd ~/ai-devteam
docker compose stop          # stop (data kept)
docker compose start         # start again
docker compose ps            # status
docker compose logs -f lead  # logs
curl -fsSL https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.sh | bash   # update
curl -fsSL https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.sh | bash -s -- --uninstall              # remove everything
curl -fsSL https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.sh | bash -s -- --uninstall --keep-data  # keep the data volumes
```

После перезагрузки контейнеры запускаются сами (`restart: unless-stopped`), если вы их не остановили.

### Установка из исходного кода

```bash
docker compose down                          # stop and remove the containers, data kept
docker compose down -v                       # … and delete the data volumes
docker compose down -v --rmi all             # … and the image
docker image prune -f                        # leftovers of local builds
```

Исходный код на Windows: `.\scripts\wslc-down.ps1` (остановить), `.\scripts\wslc-down.ps1 -Volumes` (и данные),
`.\scripts\uninstall.ps1` (всё, кроме самой папки).
