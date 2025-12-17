# Hiddify Manager - Child→Parent User Synchronization v4.0

**Надёжная автоматическая синхронизация пользователей и трафика между child и parent панелями Hiddify Manager**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Hiddify Manager](https://img.shields.io/badge/Hiddify-v11.0.12b1+-blue.svg)](https://github.com/hiddify/hiddify-manager)
[![Python](https://img.shields.io/badge/Python-3.13+-green.svg)](https://www.python.org/)

---

## 📋 Содержание

- [🎯 Основная проблема и решение](#-основная-проблема-и-решение)
- [✨ Возможности](#-возможности)
- [🏗 Архитектура системы](#-архитектура-системы)
- [🔧 Установка](#-установка)
- [⚙️ Конфигурация](#️-конфигурация)
- [📊 Мониторинг и отладка](#-мониторинг-и-отладка)
- [🔄 Как работает синхронизация](#-как-работает-синхронизация)
- [❓ FAQ - Частые вопросы](#-faq---частые-вопросы)
- [🐛 Troubleshooting](#-troubleshooting)
- [📦 Структура проекта](#-структура-проекта)
- [🤝 Вклад в проект](#-вклад-в-проект)

---

## 🎯 Основная проблема и решение

### Проблема

При использовании архитектуры **Parent-Child** в Hiddify Manager возникает необходимость:

1. **Синхронизировать пользователей** с parent панели на child серверы
2. **Собирать трафик** с child серверов и отправлять на parent (накопительно)
3. **Обнулять локальный трафик** после успешной отправки
4. **Автоматически блокировать** пользователей при превышении лимита на parent
5. **Создавать новых пользователей** на child при их появлении на parent

### Решение

Эта система предоставляет **полностью автоматическую двустороннюю синхронизацию**:

```
┌──────────────────────────────────────────────────────────────┐
│                    PARENT ПАНЕЛЬ                            │
│  • Хранит основную базу пользователей                       │
│  • Накапливает трафик со всех child серверов                │
│  • Блокирует пользователей при превышении лимита            │
└────────────────┬─────────────────────────────────────────────┘
                 │
        ┌────────┴────────┐
        ▼                 ▼
┌───────────────┐   ┌───────────────┐
│  CHILD #1     │   │  CHILD #2     │
│               │   │               │
│  ⚙ Каждые 5   │   │  ⚙ Каждые 5   │
│    минут:     │   │    минут:     │
│               │   │               │
│  ↓ Получает   │   │  ↓ Получает   │
│    юзеров     │   │    юзеров     │
│               │   │               │
│  ↑ Отправляет │   │  ↑ Отправляет │
│    трафик     │   │    трафик     │
│               │   │               │
│  🔄 Обнуляет  │   │  🔄 Обнуляет  │
│    локально   │   │    локально   │
└───────────────┘   └───────────────┘
```

---

## ✨ Возможности

### 🔄 Синхронизация пользователей (Parent → Child)

- ✅ **Получение** списка всех пользователей с parent панели
- ✅ **Создание** новых пользователей локально при их появлении на parent
- ✅ **Обновление** существующих пользователей (имя, лимиты, ключи, комментарии)
- ✅ **Блокировка** пользователей, заблокированных на parent
- ✅ **Разблокировка** пользователей, разблокированных на parent
- ✅ **Автоблокировка** пользователей, отсутствующих на parent

### 📈 Синхронизация трафика (Child → Parent)

- ✅ **Накопительная отправка** трафика на parent панель
- ✅ **Автоматическое обнуление** локального трафика после успешной отправки
- ✅ **Защита от дубликатов** - трафик не отправляется повторно
- ✅ **Поддержка обнуляемых тарифов** - корректная работа с monthly/weekly сбросом

### 🔍 Мониторинг

- ✅ **HTTP API** для проверки состояния синхронизации
- ✅ **Health check endpoint** - быстрая проверка работоспособности
- ✅ **Детальный статус** - информация о всех компонентах системы
- ✅ **Логи через journald** - интеграция с systemd

### 🛡️ Надёжность

- ✅ **Автоматический перезапуск** при сбоях
- ✅ **Восстановление после перезагрузки** сервера
- ✅ **Транзакционность** - изменения применяются атомарно
- ✅ **Обработка таймаутов** - корректная работа при недоступности parent

---

## 🏗 Архитектура системы

### Компоненты

| Компонент | Описание | Порт/Путь |
|-----------|----------|-----------|
| **stable_sync.py** | Основной скрипт синхронизации | - |
| **sync_health_api.py** | HTTP API для мониторинга | `localhost:8081` |
| **hiddify-child-sync.timer** | Systemd таймер (каждые 5 мин) | - |
| **hiddify-child-sync.service** | Systemd сервис синхронизации | - |
| **hiddify-sync-api.service** | Systemd сервис API мониторинга | - |

### Процесс синхронизации

```
┌─────────────────────────────────────────────────────────────┐
│  TIMER: Каждые 5 минут (hiddify-child-sync.timer)         │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  STEP 1: Сбор локальной дельта статистики                  │
│  • Находим пользователей с трафиком > 1MB                  │
│  • Конвертируем Bytes → GB                                  │
│  • Логируем найденных пользователей                        │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  STEP 2: Отправка статистики на parent (накопительно)      │
│  • Для каждого пользователя:                               │
│    1. GET parent_usage (текущий трафик на parent)          │
│    2. new_usage = parent_usage + local_delta               │
│    3. PATCH обновляем трафик на parent                     │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  STEP 3: Обнуление локального трафика                      │
│  • UPDATE user SET current_usage = 0                       │
│  • Только для успешно отправленных пользователей           │
│  • Транзакционно (commit после всех обновлений)            │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  STEP 4: Синхронизация пользователей с parent              │
│  • GET /api/v2/admin/user/ (все юзеры с parent)            │
│  • Для каждого пользователя:                               │
│    - Если не существует → CREATE                           │
│    - Если существует → UPDATE (все поля кроме трафика)     │
│  • Блокировка отсутствующих на parent                      │
└─────────────────────────────────────────────────────────────┘
```

---

## 🔧 Установка

### Быстрая установка (рекомендуется)

```bash
# Загрузите и запустите установщик одной командой
bash <(curl -fsSL https://raw.githubusercontent.com/slavafedoseev/hiddify-child-parent-user-sync/main/install.sh)
```

### Ручная установка

```bash
# 1. Клонируйте репозиторий
git clone https://github.com/slavafedoseev/hiddify-child-parent-user-sync.git
cd hiddify-child-parent-user-sync

# 2. Скопируйте файлы
sudo cp src/stable_sync.py /opt/hiddify-manager/
sudo cp src/sync_health_api.py /opt/hiddify-manager/
sudo chmod +x /opt/hiddify-manager/stable_sync.py
sudo chmod +x /opt/hiddify-manager/sync_health_api.py

# 3. Установите systemd сервисы
sudo cp systemd/hiddify-child-sync.service /etc/systemd/system/
sudo cp systemd/hiddify-child-sync.timer /etc/systemd/system/
sudo cp systemd/hiddify-sync-api.service /etc/systemd/system/

# 4. Перезагрузите systemd
sudo systemctl daemon-reload

# 5. Включите и запустите сервисы
sudo systemctl enable hiddify-child-sync.timer
sudo systemctl enable hiddify-sync-api.service
sudo systemctl start hiddify-child-sync.timer
sudo systemctl start hiddify-sync-api.service
```

---

## ⚙️ Конфигурация

### Настройка параметров parent панели

Отредактируйте файл `/opt/hiddify-manager/stable_sync.py`:

```python
# URL родительской панели (включая admin proxy path)
PARENT_URL = "https://my.example.com/rqkMip3ThY"

# API ключ для доступа к parent панели
API_KEY = "your-api-key-here"
```

### Получение API ключа

1. Откройте parent панель: `https://your-parent-domain.com/admin-path/`
2. Перейдите в **Settings** → **API Keys**
3. Создайте новый API ключ с правами **admin**
4. Скопируйте сгенерированный ключ

### Настройка интервала синхронизации

По умолчанию синхронизация выполняется **каждые 5 минут**. Для изменения интервала:

```bash
# Отредактируйте таймер
sudo systemctl edit hiddify-child-sync.timer

# Добавьте (например, для интервала 10 минут):
[Timer]
OnUnitActiveSec=10min
```

### Настройка порога минимального трафика

В `/opt/hiddify-manager/stable_sync.py`:

```python
# Минимальный объём трафика для отправки (в байтах)
# По умолчанию: 1MB
MIN_TRAFFIC_THRESHOLD = 1000000  # 1MB

# Для изменения на 10MB:
MIN_TRAFFIC_THRESHOLD = 10000000
```

---

## 📊 Мониторинг и отладка

### Проверка статуса сервисов

```bash
# Статус таймера
systemctl status hiddify-child-sync.timer

# Статус API мониторинга
systemctl status hiddify-sync-api.service

# Логи синхронизации (последние 50 строк)
sudo journalctl -u hiddify-child-sync.service -n 50 --no-pager

# Логи в реальном времени
sudo journalctl -u hiddify-child-sync.service -f
```

### HTTP API мониторинга

#### Health Check

```bash
# Основная проверка здоровья
curl http://localhost:8081/api/v2/hiddify-sync/health | jq

# Ожидаемый ответ:
{
  "status": "healthy",
  "timestamp": "2025-12-17T21:30:00",
  "sync_service": {
    "active": true,
    "enabled": true
  },
  "database": {
    "accessible": true,
    "user_count": 36
  },
  "users_summary": {
    "enabled_users": 33,
    "disabled_users": 3,
    "users_with_traffic": 2
  }
}
```

#### Детальный статус

```bash
curl http://localhost:8081/api/v2/hiddify-sync/status | jq
```

#### Последние логи

```bash
curl http://localhost:8081/api/v2/hiddify-sync/logs | jq
```

### Ручной запуск синхронизации

```bash
# Запустить синхронизацию вручную (не дожидаясь таймера)
sudo systemctl start hiddify-child-sync.service

# Проверить результат
sudo journalctl -u hiddify-child-sync.service -n 20 --no-pager
```

---

## 🔄 Как работает синхронизация

### Синхронизируемые поля пользователя

**От Parent к Child (обновляются):**

| Поле | Описание |
|------|----------|
| `name` | Имя пользователя |
| `usage_limit` | Лимит трафика (GB → Bytes) |
| `package_days` | Количество дней пакета |
| `mode` | Режим сброса (`no_reset`, `monthly`, `daily`) |
| `enable` | Статус активности (true/false) |
| `comment` | Комментарий администратора |
| `start_date` | Дата начала пакета |
| `last_reset_time` | Время последнего сброса |
| `telegram_id` | Telegram ID для уведомлений |
| `ed25519_private_key` | Приватный ключ Ed25519 |
| `ed25519_public_key` | Публичный ключ Ed25519 |
| `wg_pk`, `wg_psk`, `wg_pub` | WireGuard ключи |

**НЕ синхронизируются (локальные):**

- `current_usage` - локальный трафик (управляется child, отправляется на parent)
- `last_online` - последняя активность (локальная метрика)

### Накопительная синхронизация трафика

**Пример работы:**

1. **Начальное состояние:**
   - Parent: user.current_usage = 100GB
   - Child: user.current_usage = 0GB

2. **Пользователь использует 5GB на child:**
   - Child: user.current_usage = 5GB

3. **Синхронизация (через 5 минут):**
   - Скрипт получает parent_usage = 100GB
   - Вычисляет new_usage = 100GB + 5GB = 105GB
   - Обновляет parent: PATCH user.current_usage = 105GB
   - Обнуляет child: UPDATE user.current_usage = 0GB

4. **Результат:**
   - Parent: user.current_usage = 105GB ✅
   - Child: user.current_usage = 0GB ✅

### Блокировка пользователей

**Автоматическая блокировка происходит в 2 случаях:**

1. **Пользователь заблокирован на parent:**
   ```
   Parent: user.enable = false
   → Child: UPDATE user.enable = false
   ```

2. **Пользователь отсутствует на parent:**
   ```
   Parent: user не найден
   → Child: UPDATE user.enable = false
   ```

### Избежание коллизий

**Проблема:** Если несколько child серверов одновременно обновляют трафик на parent, возможна потеря данных.

**Решение:**

1. **RandomizedDelaySec=30s** в systemd таймере - случайная задержка до 30 секунд
2. **Накопительная синхронизация** - всегда GET текущий parent_usage перед UPDATE
3. **Транзакционность** - atomic операции в MySQL

**Пример с 2 child серверами:**

```
Время  | Child #1                  | Child #2                  | Parent
-------|---------------------------|---------------------------|--------
00:00  | current_usage = 3GB       | current_usage = 2GB       | 100GB
05:00  | Запуск sync (delay 10s)   | Запуск sync (delay 25s)   | 100GB
05:10  | GET parent_usage = 100GB  |                           | 100GB
05:10  | new = 100+3 = 103GB       |                           | 100GB
05:10  | PATCH 103GB               |                           | 103GB ✅
05:25  |                           | GET parent_usage = 103GB  | 103GB
05:25  |                           | new = 103+2 = 105GB       | 103GB
05:25  |                           | PATCH 105GB               | 105GB ✅
```

---

## ❓ FAQ - Частые вопросы

### Q: Нужна ли специальная настройка parent панели?

**A:** Нет, parent панель работает "из коробки". Единственное требование - API ключ с правами **admin**.

### Q: Что делать если parent панель временно недоступна?

**A:** Система автоматически обработает эту ситуацию:
- Локальный трафик продолжит накапливаться
- При следующей успешной синхронизации всё будет отправлено
- Пользователи не будут заблокированы

### Q: Можно ли использовать на нескольких child серверах одновременно?

**A:** Да! Система спроектирована для работы с множеством child серверов. RandomizedDelaySec предотвращает коллизии.

### Q: Как часто происходит синхронизация?

**A:** По умолчанию каждые 5 минут. Вы можете изменить интервал в `hiddify-child-sync.timer`.

### Q: Что произойдёт если пользователь превысит лимит на child?

**A:**
1. Child отправит трафик на parent
2. Parent заблокирует пользователя (`enable = false`)
3. При следующей синхронизации child получит `enable = false` и заблокирует локально

### Q: Сохраняется ли синхронизация после обновления Hiddify Manager?

**A:** Да, все файлы находятся в `/opt/hiddify-manager/` и не перезаписываются при обновлениях.

### Q: Как проверить что синхронизация работает?

**A:**
```bash
# Проверка API
curl http://localhost:8081/api/v2/hiddify-sync/health | jq

# Проверка логов
sudo journalctl -u hiddify-child-sync.service -n 20
```

---

## 🐛 Troubleshooting

### Синхронизация не запускается

```bash
# Проверьте статус таймера
systemctl status hiddify-child-sync.timer

# Если inactive:
sudo systemctl enable hiddify-child-sync.timer
sudo systemctl start hiddify-child-sync.timer
```

### Ошибка подключения к parent панели

```bash
# Проверьте логи
sudo journalctl -u hiddify-child-sync.service -n 50

# Проверьте доступность parent
curl -k "https://your-parent-domain.com/admin-path/api/v2/panel/ping/" \
  -H "Hiddify-API-Key: your-api-key"

# Ожидаемый ответ: {"status": "ok"}
```

### API мониторинга не отвечает

```bash
# Проверьте статус сервиса
systemctl status hiddify-sync-api.service

# Перезапустите если нужно
sudo systemctl restart hiddify-sync-api.service

# Проверьте порт
sudo netstat -tulpn | grep 8081
```

### База данных недоступна

```bash
# Проверьте MySQL
sudo systemctl status mysql

# Проверьте Unix socket
ls -lah /var/run/mysqld/mysqld.sock

# Проверьте права пользователя root
sudo mysql -u root hiddifypanel -e "SELECT COUNT(*) FROM user;"
```

### Трафик не обнуляется после отправки

**Возможные причины:**

1. **Ошибка отправки на parent** - проверьте логи
2. **Неправильный API ключ** - проверьте PARENT_URL и API_KEY
3. **Пользователь не найден на parent** - проверьте UUID

```bash
# Проверьте последнюю синхронизацию
sudo journalctl -u hiddify-child-sync.service -n 50 | grep "Сброшен трафик"
```

---

## 📦 Структура проекта

```
hiddify-child-parent-user-sync/
├── README.md                           # Документация (этот файл)
├── LICENSE                             # Лицензия MIT
├── install.sh                          # Быстрый установщик
├── src/
│   ├── stable_sync.py                 # Основной скрипт синхронизации
│   └── sync_health_api.py             # HTTP API мониторинга
├── systemd/
│   ├── hiddify-child-sync.service     # Systemd сервис
│   ├── hiddify-child-sync.timer       # Systemd таймер (каждые 5 мин)
│   └── hiddify-sync-api.service       # Systemd сервис API
├── docs/
│   ├── ARCHITECTURE.md                # Подробная архитектура
│   ├── API.md                          # Документация HTTP API
│   ├── TROUBLESHOOTING.md             # Подробный troubleshooting
│   └── CHANGELOG.md                    # История изменений
└── tests/
    ├── test_sync.py                   # Тесты синхронизации
    └── test_api.py                    # Тесты API
```

---

## 🤝 Вклад в проект

Мы приветствуем вклад в проект! Если вы хотите помочь:

1. **Fork** этот репозиторий
2. Создайте **feature branch**: `git checkout -b feature/amazing-feature`
3. **Commit** изменения: `git commit -m 'Add amazing feature'`
4. **Push** в branch: `git push origin feature/amazing-feature`
5. Откройте **Pull Request**

### Сообщение об ошибках

Если вы нашли ошибку, пожалуйста:

1. Проверьте [Issues](https://github.com/slavafedoseev/hiddify-child-parent-user-sync/issues) - возможно она уже была сообщена
2. Создайте новый Issue с подробным описанием:
   - Версия Hiddify Manager
   - Версия этого скрипта
   - Логи ошибки
   - Шаги для воспроизведения

---

## 📄 Лицензия

Этот проект распространяется под лицензией **MIT**. См. файл [LICENSE](LICENSE) для подробностей.

---

## 📞 Поддержка

- **Issues**: [GitHub Issues](https://github.com/slavafedoseev/hiddify-child-parent-user-sync/issues)
- **Telegram**: @slavafedoseev
- **Email**: slava@fedoseev.one

---

## 🙏 Благодарности

- **Hiddify Team** за отличную панель управления
- **Сообщество Hiddify** за тестирование и обратную связь
- Всем контрибьюторам этого проекта

---

**Made with ❤️ for Hiddify Community**

*Последнее обновление: 17 декабря 2025*
