# COPILOT — Uplift CRM Engine

AI-движок персонализации CRM-коммуникаций для iGaming-операторов: uplift-оценка действий, бандит-выбор вариантов, LLM-генерация и локализация сообщений, строгая evidence-архитектура и safety-слои.

> **Проект демонстрирует архитектуру и инженерные практики.** Все модули работают только с first-party данными: игроками, зарегистрированными в CRM оператора и давшими согласие на коммуникации. Проект не собирает и не обогащает внешние данные о пользователях.

## Что внутри

| Модуль | Что делает |
|---|---|
| `copilot_uplift` | Оценка прироста (uplift) действий: holdout-группы, A/B-присвоение, gates, policy, аудит causal-корректности |
| `copilot_bandit` | Многорукий бандит для выбора коммуникативных действий: reward-контракт v1, replay, append-only коррекции |
| `copilot_llm_router` | Роутинг LLM-моделей: health-check, failover, симулятор нагрузки, изоляция от production-пути |
| `copilot_openrouter` | Клиент OpenRouter с контрактами и тестами |
| `copilot_psychology` | Классификация состояния игрока → стратегия коммуникации. **Safety-слой глушит дожим для уязвимых сегментов (несовершеннолетние, самоисключение, финансовый кризис) — всегда, без исключений** |
| `copilot_offers` | Оффер-движок: контекст, валидация, url-guard, решение |
| `language_layer` | Локализация es/pt (Мексика, Перу, Колумбия, Эквадор, Бразилия): geo-резолюция по user-declared данным и лексике, anti-translationese guard, meaning-guard с откатом |
| `copilot_shadow` | Теневой анализ диалогов: snapshot, lead-score, momentum, reply-classification, canonical state через Evidence Arbiter |
| `copilot_evidence` | Каноническая evidence-модель: источники (production / client_state / shadow / external), fusion, детерминированный arbiter, репозиторий |
| `copilot_decisions` | Decision Ledger: append-only запись каждого решения с контрактом полей |
| `copilot_attribution` | Атрибуция outcome → decision: политики, boundary-правила v2 |
| `copilot_outcomes` | Capture outcomes (postback / production / telegram-адаптеры), joiner |
| `copilot_prediction` | Предсказания на политиках атрибуции |
| `copilot_learning` | Critic + learner: обучение на теневых решениях без доступа к production |
| `copilot_secrets` | Мастер-ключ, шифрование, аудит доступа к секретам |
| `copilot_state_delta` | Детерминированные дельты состояния между решениями |
| `copilot_contracts` | Стадии жизненного цикла и словари evidence |
| `copilot_voice` / `copilot_creative` | Голосовые решения; промпт-форж креативов по разбору видео (provenance-разделение фактов и AI-гипотез) |

## Архитектурные принципы

- **Evidence over inference.** Каждое состояние подкреплено evidence с provenance; конфликт источников → `UNRESOLVED`, конфликт виден, а не скрыт.
- **Shadow first.** Все новые механики запускаются в теневом режиме: пишут наблюдения, не влияют на production-ответы.
- **Append-only ledger.** Решения и reward-коррекции не переписываются — исправления дописываются.
- **Causal honesty.** Uplift оценивается против holdout-группы; исторические эксперименты без рандомизации помечаются non-causal.
- **Safety by design.** Психологический движок не может рекомендовать запретные тактики: они попадают только в `avoid_strategies`. Уязвимые игроки получают `SLOW_DOWN`, никогда — дожим.
- **Isolation tests.** Тесты проверяют, что LLM-слой не видит персональные данные, а capture/hooks не ломают production-путь.

## Запуск тестов

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest tests -q
```

Статус: **896 passed / 0 failed** (unit-набор; интеграционные тесты внутреннего монолита в публичную версию не входят).

## Ответственное использование

- Движок работает только по подписке пользователя (opt-in) и не обходит никакие системы защиты или верификации.
- Ответственная игра (responsible gambling) встроена в policy-слой: лимиты частоты, уважение самоисключения.
- Никаких личных данных из открытых источников: только поля, которые игрок сообщил сам, и его поведение в продукте.

## Стек

Python 3.11+, SQLAlchemy 2 (async), FastAPI, Pydantic v2, httpx/aiohttp, pytest (+ asyncio).
