# Uplift Copilot

**Набор Python-модулей для измерения и аудита CRM-решений, attribution и экспериментальных политик.**

Uplift Copilot — исследовательский прототип архитектуры decision/measurement engine. В репозитории собраны контракты, детерминированные аналитические модули, адаптеры хранения, API-роутеры и тесты. Это **не готовая самостоятельная CRM-служба**: host-приложение/внутренний монолит, его точка входа и часть production-интеграций в публичную версию не входят.

> **Статус:** research / prototype. Не включайте production-влияние на основании одного факта, что unit-тесты проходят. В разделе [Текущие ограничения](#текущие-ограничения) перечислены известные блокеры корректности и безопасности.

## Задача и границы

Проект разделяет четыре разных процесса:

1. **Capture** — записать, какое решение было принято, какой ответ действительно отправлен и какой outcome был получен.
2. **Attribution** — определить, можно ли связать outcome с решением по явным идентификаторам или временным правилам.
3. **Evaluation** — посчитать наблюдаемые частоты и экспериментальные различия, отдельно показывая неизвестные и неполные данные.
4. **Governance** — хранить версии политик, проверять gates и требовать явного административного подтверждения для изменения состояния активации.

Цель архитектуры — не подменять неизвестное нулём, не называть корреляцию причинным эффектом и сохранять происхождение данных. Все реальные пользовательские коммуникации должны оставаться добровольными, прозрачными и подчиняться независимым правилам безопасности и responsible gambling.

## Структура модулей

| Модуль | Назначение | Текущий статус по коду |
|---|---|---|
| `copilot_decisions` | Контракт и запись decision ledger | Модуль записи; зависит от интеграции с host-приложением |
| `copilot_response_boundary` | Фиксация успешно отправленных частей ответа | Модуль записи; требует production-hook |
| `copilot_outcomes` | Контракты, capture и join outcomes | Адаптеры и хранилище; требует реальных источников |
| `copilot_attribution` | Direct и boundary-based temporal attribution | Детерминированные правила и evaluator |
| `copilot_evidence` | Provenance, fusion и arbitration evidence | Библиотека контрактов/адаптеров |
| `copilot_uplift` | Holdout assignment, offline evaluation, policy, gates и activation | ITT evaluator v2 с fail-closed activation gates; production-допуск остаётся заблокированным до статистической валидации и host/safety-интеграции |
| `copilot_bandit` | LinUCB, replay и reward contracts | Research/shadow; не production learner |
| `copilot_prediction` | Наблюдения и статусы outcome | Детерминированные вспомогательные правила |
| `copilot_learning` | Critic и learner для теневых данных | Изолированные модули; интеграция зависит от host |
| `copilot_psychology` | Rule-based классификация структурированных сигналов и нейтральные стратегии | Shadow-only; не является самостоятельным risk-screening или enforcement |
| `copilot_secrets` | Шифрование секретов и admin API router | Модуль; фактическая auth-интеграция задаётся host-приложением |
| `copilot_llm_router`, `copilot_openrouter` | Конфигурация маршрутизации и API-клиент | Изолированные компоненты; production-подключение не подтверждено этой публичной версией |
| `language_layer` | Нормализация языка и локализация | Библиотека правил и тестов |
| `copilot_offers`, `copilot_programs`, `copilot_leads`, `copilot_acquisition` | Контракты и сервисная логика смежных CRM-сценариев | Отдельные модули; не равны готовому развернутому продукту |
| `copilot_shadow`, `copilot_state_delta`, `copilot_contracts` | Shadow-анализ, изменения состояния и общие контракты | Вспомогательные библиотеки |
| `copilot_creative`, `copilot_voice` | Работа с креативами и голосовыми сценариями | Отдельные компоненты; требуют собственных внешних зависимостей |

## Архитектура

Ниже показан **предполагаемый поток между модулями**, а не гарантия, что публичный репозиторий уже развёрнут как единое приложение.

```mermaid
flowchart TD
    HOST["Host application<br/>(не входит в публичный snapshot)"]
    LEDGER["Decision ledger"]
    BOUNDARY["Sent response boundaries"]
    OUTCOMES["Outcome capture"]
    ASSIGN["Holdout assignment"]
    PROVIDER["Read-only SQL provider"]
    ATTR["Attribution v2"]
    EVAL["Offline evaluation"]
    GATES["Policy gates"]
    ADMIN["Explicit admin activation"]
    SHADOW["Shadow store"]
    BANDIT["Research bandit"]

    HOST --> LEDGER
    HOST --> BOUNDARY
    HOST --> OUTCOMES
    HOST --> ASSIGN
    LEDGER --> PROVIDER
    BOUNDARY --> PROVIDER
    OUTCOMES --> PROVIDER
    ASSIGN --> PROVIDER
    PROVIDER --> ATTR --> EVAL --> GATES --> ADMIN
    LEDGER --> SHADOW --> BANDIT
```

Ключевые принципы:

- **Provenance first.** Источник и время наблюдения должны сохраняться вместе с данными.
- **Unknown is not zero.** Неизвестный outcome не превращается в отрицательное событие.
- **Shadow before influence.** Новая логика сначала проверяется без изменения пользовательских ответов.
- **Explicit activation.** Активация требует административного действия и повторной проверки условий.
- **Fail closed.** Ошибка, отсутствие данных или нарушение integrity не должны разрешать автоматическое влияние.
- **Data minimization.** В аналитические признаки следует передавать только необходимые first-party поля.

## Uplift: что именно считается

Текущий `copilot_uplift/estimator.py` версии `estimator.v2` разделяет описательную статистику и экспериментальную оценку.

**Экспериментальная оценка использует intent-to-treat (ITT):**
- единица рандомизации — `chat_id`;
- каждое зрелое (полностью прошедшее outcome-window) валидное назначение даёт ровно один экспериментальный sample; незрелые назначения исключаются только по времени, а не по outcome;
- CONTROL соответствует `DO_NOTHING`, TREATMENT — назначению обычного proactive follow-up (`FOLLOW_UP`);
- обе группы измеряются в одном временном окне, начинающемся с `decided_at`;
- outcome учитывается независимо от того, удалось ли фактически отправить follow-up и к какому decision его можно атрибутировать;
- исторические решения и повторные сообщения назначенного чата не дублируют ITT sample. Неизвестные outcomes зрелых назначений блокируют production eligibility.

```text
ITT effect = outcome_rate(assigned TREATMENT) - outcome_rate(assigned CONTROL)
```

Это оценка эффекта **назначения follow-up против holdout**, а не CATE, не персональный uplift и не эффект отдельного текста/креатива. Если нужны эффекты конкретных вариантов сообщений, для них необходим отдельный эксперимент с независимой рандомизацией вариантов.

`conversion_by_action` — диагностическая сводка, а не причинная оценка: она может включать описательные decision samples и assignment-level samples. Для причинного вывода используйте только `uplift_estimates`, рассчитанные по assignment-level ITT samples. Экспериментальные оценки строятся только по назначениям; повторные решения назначенного чата не добавляются повторно. Минимальный размер проверяется отдельно для обеих рандомизированных групп.

Статусы качества данных:
- `INSUFFICIENT_DATA` — недостаточно измеримых или рандомизированных наблюдений;
- `OBSERVATIONAL` — нет достаточных назначенных CONTROL и TREATMENT групп;
- `EXPERIMENTAL` — обе группы имеют минимальный объём, но не все propensity доступны;
- `CAUSAL_READY` — обе группы имеют минимальный объём и для каждого assignment записана допустимая вероятность назначения.

Даже `CAUSAL_READY` не доказывает корректность дизайна эксперимента или статистическую значимость. Нужны независимые проверки рандомизации, баланса групп, capture completeness, доверительных интервалов и длительности наблюдения. Контракт evaluation обновлён до `uplift_evaluation.v2`: сохранённые v1 snapshots остаются в БД для аудита, но `EvaluationRepository.latest()` больше не возвращает их в activation path. После обновления нужно выполнить новый v2 evaluation.

## Установка и тесты

Требуется Python 3.11+.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m pytest tests -q
```

Эта команда запускает тестовый набор. Она **не запускает CRM/API-сервер**: публичный snapshot не содержит самостоятельной точки входа приложения, Docker-конфигурации или полной host-интеграции.

### Статус тестирования

В предыдущем README было заявлено **896 passed / 0 failed** — это исторический результат, сообщённый автором. В этой ветке добавлен GitHub Actions workflow, который запускает полный pytest suite; число тестов и результат нужно брать из актуального зелёного run, а не из старого README.

## Запуск миграций

Модули хранилищ предоставляют отдельные миграции. Они требуют настроенного `DATABASE_URL` и выполняются явно. Например:

```bash
# Сначала вывести план SQL без изменений:
python -m copilot_decisions.migrate

# Применять только после проверки плана и резервного копирования:
python -m copilot_decisions.migrate --apply
```

Аналогичные команды доступны у отдельных пакетов с `migrate.py`. Не запускайте все миграции автоматически в production: проверьте каждую схему, порядок, совместимость и rollback-план отдельно.

## Конфигурация и безопасные значения по умолчанию

Некоторые интеграционные hooks и эксперименты выключены по умолчанию:

| Переменная | Назначение | Безопасное значение по умолчанию |
|---|---|---|
| `COPILOT_DECISION_LEDGER_ENABLED` | Запись decision ledger | `false` |
| `COPILOT_UPLIFT_ASSIGN_ENABLED` | Назначение holdout/treatment для follow-up эксперимента | `false` |
| `COPILOT_UPLIFT_SHADOW_ENABLED` | Shadow Uplift capture | `false` |
| `COPILOT_UPLIFT_ENABLED` | Kill switch влияния Uplift | `false` |
| `COPILOT_BANDIT_SHADOW_ENABLED` | Shadow-рекомендация bandit | `false` |
| `COPILOT_BANDIT_ENABLED` | Флаг production-влияния bandit | `false` |
| `COPILOT_PSYCHOLOGY_SHADOW_ENABLED` | Shadow assessment психологии | `false` |

Не включайте флаги на основании только этого списка. Перед включением проверьте конфигурацию host-приложения, права доступа, схему БД, capture completeness, журналирование и отдельные safety-контроли. Секреты и master key должны передаваться через защищённое хранилище конфигурации, а не коммититься в Git.

## Безопасность и Responsible Gambling

- Не использовать психологические признаки, уязвимость, самоисключение, финансовые затруднения или статус несовершеннолетнего для персонализации коммерческого давления.
- Не отправлять коммерческие сообщения пользователям, для которых действует самоисключение, запрет контакта или иной защитный статус.
- Проверки риска должны выполняться независимым hard-gate непосредственно перед отправкой сообщения. Shadow-оценка или стратегия `SLOW_DOWN` не заменяет блокировку контакта.
- Секреты должны шифроваться master key, который хранится вне репозитория. Admin API получает `require_admin` от host-приложения; его реальную реализацию и сессию необходимо проверять отдельно.
- Проверяйте права БД и резервные копии. Наличие insert-only методов в Python не равнозначно неизменяемости данных на уровне базы.

## Текущие ограничения

Это известные ограничения публичного snapshot на момент ревью. До их устранения не следует включать production-влияние.

1. **Статистическая неопределённость.** Estimator v2 считает ITT-разницу долей, но пока не выдаёт доверительные интервалы, стандартные ошибки, p-value или поправки на множественные сравнения. Значение разницы нельзя трактовать как доказанный эффект без отдельного inference-слоя.
2. **Границы treatment.** Текущий assignment-контракт оценивает назначение обычного proactive follow-up против holdout. Он не рандомизирует конкретные тексты или креативы; оценка не доказывает эффективность отдельного сообщения.
3. **Неполные выборки.** SQL provider ограничивает загрузку decisions, outcomes, boundaries и assignments. Evaluator сохраняет `truncated`, unreadable rows и недоступность обязательных таблиц; activation gate, recommendation и influence boundary теперь блокируют production eligibility при таких проблемах. Реальный полноразмерный production snapshot ещё нужно проверить.
4. **Окно capture.** Отрицательный ITT-outcome теперь считается только если earliest known outcome предшествует времени назначения; если capture начался позже, sample остаётся UNKNOWN и production eligibility блокируется. Но раннее событие не доказывает непрерывную работу capture: нужен явный health/coverage marker или heartbeat источника.
5. **Bandit/reward.** `LinUCBBackend` хранит состояние в памяти процесса; стандартный reward resolver не задаёт числовой reward, а параметры reward v1 помечены как неподтверждённые. Это research/shadow-компонент, а не готовый persistently trained production bandit.
6. **Responsible Gambling signals.** Психологический shadow-hook получает ограниченный decision snapshot; реальные risk-сигналы несовершеннолетия, самоисключения и финансового кризиса не поступают в этот путь из проверенного источника. Этот модуль нельзя считать работающим enforcement-контуром.
7. **Неизменяемость Decision Ledger.** Для отдельного `copilot_decisions` хранилища нужно отдельно подтвердить DB-level запрет `DELETE` и SQLite-триггеры; Python insert-only API недостаточно. Таблицы `copilot_uplift` в этой ветке уже получают DB-триггеры, блокирующие `UPDATE` и `DELETE` в PostgreSQL и SQLite.
8. **Host-интеграция.** Публичный snapshot не содержит приложения, которое подключает все роутеры, hooks, auth, фоновые задачи и production-источники. Сквозное поведение работающей системы здесь не подтверждено.
9. **CI/release.** В этой ветке добавлен GitHub Actions workflow для pytest, но его первый прогон ещё не подтверждён зелёным статусом. Основная ветка пока не защищена branch protection.

## Перед production

- [ ] Разделить observational и randomized samples; явно задать experiment id, arm, exposure time и eligible decision path.
- [ ] Считать эффект на корректной единице рандомизации и добавить интервалы неопределённости / проверки баланса.
- [ ] Запретить activation при `truncated`, unreadable rows, missing tables и неполной assignment-coverage.
- [ ] Заменить эвристику `capture_since` на проверяемый capture health/heartbeat.
- [ ] Вынести Responsible Gambling hard-gate в обязательный путь отправки и протестировать его end-to-end.
- [ ] Защитить ledger от UPDATE **и DELETE** на уровне БД; применить least-privilege права.
- [ ] Добавить CI: установка зависимостей, unit + integration tests, lint/type checks, dependency/security scanning.
- [ ] Добавить host integration tests и воспроизводимый запуск тестовой среды.
- [ ] Добавить `.env.example`, конфигурационную документацию и release checklist; расширить secret-file patterns в `.gitignore`.

## Стек

Python 3.11+, SQLAlchemy 2 (async), FastAPI, Pydantic v2, HTTPX/aiohttp, NumPy, cryptography, pytest и pytest-asyncio.

## Лицензия

См. [LICENSE](LICENSE).
