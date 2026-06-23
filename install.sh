#!/bin/bash
#################################################################################
# Hiddify Manager - Child→Parent User Synchronization Installer v4.0
#
# Автоматическая установка и настройка системы синхронизации
# между child и parent панелями Hiddify Manager
#
# Использование:
#   bash <(curl -fsSL https://raw.githubusercontent.com/slavafedoseev/hiddify-child-parent-user-sync/main/install.sh)
#
# Автор: Slava Fedoseev
# Лицензия: MIT
# GitHub: https://github.com/slavafedoseev/hiddify-child-parent-user-sync
#################################################################################

set -e  # Exit on any error

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Версия скрипта
VERSION="5.0"

# GitHub репозиторий
GITHUB_REPO="slavafedoseev/hiddify-child-parent-user-sync"
GITHUB_BRANCH="main"
RAW_URL="https://raw.githubusercontent.com/${GITHUB_REPO}/${GITHUB_BRANCH}"

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

# ВАЖНО: все лог/декор-сообщения идут в stderr (>&2), чтобы не загрязнять stdout.
# Иначе command substitution вида temp_dir=$(download_files) захватывала бы лог-строки
# вместе с возвращаемым путём (баг «File name too long» при cp).
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1" >&2
}

log_success() {
    echo -e "${GREEN}[✓]${NC} $1" >&2
}

log_warning() {
    echo -e "${YELLOW}[⚠]${NC} $1" >&2
}

log_error() {
    echo -e "${RED}[✗]${NC} $1" >&2
}

print_header() {
    echo "" >&2
    echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}" >&2
    echo -e "${BLUE} $1${NC}" >&2
    echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}" >&2
    echo "" >&2
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "Этот скрипт должен быть запущен с правами root"
        log_info "Попробуйте: sudo bash install.sh"
        exit 1
    fi
}

check_hiddify() {
    if [ ! -f "/opt/hiddify-manager/current.json" ]; then
        log_error "Hiddify Manager не найден в /opt/hiddify-manager/"
        log_info "Пожалуйста, сначала установите Hiddify Manager"
        log_info "https://github.com/hiddify/hiddify-manager"
        exit 1
    fi
}

check_dependencies() {
    log_info "Проверка зависимостей..."

    local missing_deps=()

    # Проверяем наличие необходимых команд
    for cmd in curl systemctl journalctl mysql; do
        if ! command -v $cmd &> /dev/null; then
            missing_deps+=("$cmd")
        fi
    done

    if [ ${#missing_deps[@]} -ne 0 ]; then
        log_error "Отсутствуют зависимости: ${missing_deps[*]}"
        log_info "Установите их с помощью: apt-get install ${missing_deps[*]}"
        exit 1
    fi

    # Проверяем MySQL
    if ! systemctl is-active --quiet mysql && ! systemctl is-active --quiet mariadb; then
        log_error "MySQL/MariaDB не запущен"
        log_info "Запустите с помощью: systemctl start mysql"
        exit 1
    fi

    # Проверяем PyMySQL в venv
    if ! /opt/hiddify-manager/.venv313/bin/python -c "import pymysql" 2>/dev/null; then
        log_warning "PyMySQL не установлен, устанавливаем..."
        /opt/hiddify-manager/.venv313/bin/pip install pymysql --quiet
        log_success "PyMySQL установлен"
    fi

    log_success "Все зависимости найдены"
}

get_user_input() {
    print_header "Настройка параметров подключения к Parent панели"

    # URL Parent панели
    echo -e "${BLUE}Введите URL parent панели (включая admin proxy path)${NC}"
    echo -e "Пример: ${YELLOW}https://my.example.com/rqkMip3ThY${NC}"
    read -p "Parent URL: " PARENT_URL

    if [ -z "$PARENT_URL" ]; then
        log_error "URL parent панели не может быть пустым"
        exit 1
    fi

    # Проверка формата URL
    if ! echo "$PARENT_URL" | grep -qE '^https?://'; then
        log_error "URL должен начинаться с http:// или https://"
        exit 1
    fi

    echo ""

    # API ключ
    echo -e "${BLUE}Введите API ключ для доступа к parent панели${NC}"
    echo -e "Получить: ${YELLOW}Parent Panel → Settings → API Keys${NC}"
    read -p "API Key: " API_KEY

    if [ -z "$API_KEY" ]; then
        log_error "API ключ не может быть пустым"
        exit 1
    fi

    echo ""

    # Подтверждение
    echo -e "${YELLOW}Проверьте введённые данные:${NC}"
    echo -e "  Parent URL: ${GREEN}$PARENT_URL${NC}"
    echo -e "  API Key:    ${GREEN}${API_KEY:0:8}...${API_KEY: -8}${NC}"
    echo ""
    read -p "Всё верно? (y/n): " confirm

    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        log_info "Установка отменена"
        exit 0
    fi
}

test_parent_connection() {
    log_info "Проверка подключения к parent панели..."

    local response
    response=$(curl -s -k -w "%{http_code}" -o /dev/null \
        "${PARENT_URL}/api/v2/panel/ping/" \
        -H "Hiddify-API-Key: ${API_KEY}" \
        --connect-timeout 10 \
        --max-time 15)

    if [ "$response" = "200" ]; then
        log_success "Подключение к parent панели успешно"
    else
        log_error "Не удалось подключиться к parent панели (HTTP $response)"
        log_warning "Проверьте URL и API ключ"
        read -p "Продолжить установку? (y/n): " continue
        if [ "$continue" != "y" ] && [ "$continue" != "Y" ]; then
            exit 1
        fi
    fi
}

download_files() {
    print_header "Загрузка файлов из GitHub"

    local temp_dir="/tmp/hiddify-child-sync-install"
    rm -rf "$temp_dir"
    mkdir -p "$temp_dir"

    log_info "Загрузка stable_sync.py..."
    curl -fsSL --retry 4 --retry-delay 2 --retry-connrefused "${RAW_URL}/src/stable_sync.py" -o "$temp_dir/stable_sync.py"

    log_info "Загрузка sync_health_api.py..."
    curl -fsSL --retry 4 --retry-delay 2 --retry-connrefused "${RAW_URL}/src/sync_health_api.py" -o "$temp_dir/sync_health_api.py"

    log_info "Загрузка activate_new_users_direct.py..."
    curl -fsSL --retry 4 --retry-delay 2 --retry-connrefused "${RAW_URL}/src/activate_new_users_direct.py" -o "$temp_dir/activate_new_users_direct.py"

    log_info "Загрузка deactivate_users_direct.py..."
    curl -fsSL --retry 4 --retry-delay 2 --retry-connrefused "${RAW_URL}/src/deactivate_users_direct.py" -o "$temp_dir/deactivate_users_direct.py"

    log_info "Загрузка systemd файлов..."
    curl -fsSL --retry 4 --retry-delay 2 --retry-connrefused "${RAW_URL}/systemd/hiddify-child-sync.service" -o "$temp_dir/hiddify-child-sync.service"
    curl -fsSL --retry 4 --retry-delay 2 --retry-connrefused "${RAW_URL}/systemd/hiddify-child-sync.timer" -o "$temp_dir/hiddify-child-sync.timer"
    curl -fsSL --retry 4 --retry-delay 2 --retry-connrefused "${RAW_URL}/systemd/hiddify-sync-api.service" -o "$temp_dir/hiddify-sync-api.service"

    log_info "Загрузка hiddify-patch-celery-rollback.py..."
    curl -fsSL --retry 4 --retry-delay 2 --retry-connrefused "${RAW_URL}/src/hiddify-patch-celery-rollback.py" -o "$temp_dir/hiddify-patch-celery-rollback.py"

    log_info "Загрузка celery-rollback-patch.conf..."
    curl -fsSL --retry 4 --retry-delay 2 --retry-connrefused "${RAW_URL}/systemd/celery-rollback-patch.conf" -o "$temp_dir/celery-rollback-patch.conf"

    log_success "Все файлы загружены"

    echo "$temp_dir"
}

install_scripts() {
    local temp_dir="$1"

    print_header "Установка скриптов"

    # Копируем скрипты
    log_info "Копирование stable_sync.py..."
    cp "$temp_dir/stable_sync.py" /opt/hiddify-manager/
    chmod +x /opt/hiddify-manager/stable_sync.py

    log_info "Копирование sync_health_api.py..."
    cp "$temp_dir/sync_health_api.py" /opt/hiddify-manager/
    chmod +x /opt/hiddify-manager/sync_health_api.py

    log_info "Копирование activate_new_users_direct.py..."
    cp "$temp_dir/activate_new_users_direct.py" /opt/hiddify-manager/
    chmod +x /opt/hiddify-manager/activate_new_users_direct.py

    log_info "Копирование deactivate_users_direct.py..."
    cp "$temp_dir/deactivate_users_direct.py" /opt/hiddify-manager/
    chmod +x /opt/hiddify-manager/deactivate_users_direct.py

    log_info "Копирование hiddify-patch-celery-rollback.py..."
    cp "$temp_dir/hiddify-patch-celery-rollback.py" /usr/local/bin/
    chmod +x /usr/local/bin/hiddify-patch-celery-rollback.py

    # Устанавливаем права
    chown root:hiddify-common /opt/hiddify-manager/stable_sync.py
    chown root:hiddify-common /opt/hiddify-manager/sync_health_api.py
    chown root:hiddify-common /opt/hiddify-manager/activate_new_users_direct.py
    chown root:hiddify-common /opt/hiddify-manager/deactivate_users_direct.py

    log_success "Скрипты установлены"
}

configure_scripts() {
    print_header "Настройка параметров"

    log_info "Обновление PARENT_URL..."
    sed -i "s|PARENT_URL = \".*\"|PARENT_URL = \"$PARENT_URL\"|" /opt/hiddify-manager/stable_sync.py

    log_info "Обновление API_KEY..."
    sed -i "s|API_KEY = \".*\"|API_KEY = \"$API_KEY\"|" /opt/hiddify-manager/stable_sync.py

    log_success "Параметры обновлены"
}

install_systemd_services() {
    local temp_dir="$1"

    print_header "Установка systemd сервисов"

    log_info "Копирование systemd файлов..."
    cp "$temp_dir/hiddify-child-sync.service" /etc/systemd/system/
    cp "$temp_dir/hiddify-child-sync.timer" /etc/systemd/system/
    cp "$temp_dir/hiddify-sync-api.service" /etc/systemd/system/

    # Устанавливаем drop-in для автопатча Celery (переживает обновления Hiddify)
    log_info "Установка Celery rollback patch drop-in..."
    mkdir -p /etc/systemd/system/hiddify-panel-background-tasks.service.d
    cp "$temp_dir/celery-rollback-patch.conf" /etc/systemd/system/hiddify-panel-background-tasks.service.d/

    log_info "Перезагрузка systemd daemon..."
    systemctl daemon-reload

    # Применяем патч и перезапускаем background tasks
    log_info "Применение Celery stability patch..."
    /usr/bin/python3 /usr/local/bin/hiddify-patch-celery-rollback.py || true
    systemctl restart hiddify-panel-background-tasks.service 2>/dev/null || true

    log_success "Systemd сервисы установлены"
}

start_services() {
    print_header "Запуск сервисов"

    # Останавливаем старые версии если они запущены
    log_info "Остановка старых сервисов (если есть)..."
    systemctl stop hiddify-child-sync.timer 2>/dev/null || true
    systemctl stop hiddify-sync-api.service 2>/dev/null || true
    systemctl stop hiddify-user-sync.timer 2>/dev/null || true

    # Включаем и запускаем таймер
    log_info "Включение и запуск hiddify-child-sync.timer..."
    systemctl enable hiddify-child-sync.timer
    systemctl start hiddify-child-sync.timer

    # Включаем и запускаем API
    log_info "Включение и запуск hiddify-sync-api.service..."
    systemctl enable hiddify-sync-api.service
    systemctl start hiddify-sync-api.service

    log_success "Сервисы запущены"
}

run_test_sync() {
    print_header "Тестовая синхронизация"

    log_info "Запуск тестовой синхронизации..."
    systemctl start hiddify-child-sync.service

    sleep 5

    log_info "Проверка результата..."
    journalctl -u hiddify-child-sync.service --no-pager -n 15
}

verify_installation() {
    print_header "Проверка установки"

    local all_ok=true

    # Проверка файлов
    log_info "Проверка файлов..."
    for file in "/opt/hiddify-manager/stable_sync.py" \
                "/opt/hiddify-manager/sync_health_api.py" \
                "/opt/hiddify-manager/activate_new_users_direct.py" \
                "/opt/hiddify-manager/deactivate_users_direct.py" \
                "/usr/local/bin/hiddify-patch-celery-rollback.py" \
                "/etc/systemd/system/hiddify-child-sync.service" \
                "/etc/systemd/system/hiddify-child-sync.timer" \
                "/etc/systemd/system/hiddify-sync-api.service" \
                "/etc/systemd/system/hiddify-panel-background-tasks.service.d/celery-rollback-patch.conf"; do
        if [ -f "$file" ]; then
            log_success "Найден: $file"
        else
            log_error "Отсутствует: $file"
            all_ok=false
        fi
    done

    # Проверка сервисов
    log_info "Проверка сервисов..."
    if systemctl is-active --quiet hiddify-child-sync.timer; then
        log_success "hiddify-child-sync.timer активен"
    else
        log_error "hiddify-child-sync.timer не активен"
        all_ok=false
    fi

    if systemctl is-active --quiet hiddify-sync-api.service; then
        log_success "hiddify-sync-api.service активен"
    else
        log_error "hiddify-sync-api.service не активен"
        all_ok=false
    fi

    # Проверка API
    log_info "Проверка HTTP API..."
    sleep 2
    if curl -s http://localhost:8081/api/v2/hiddify-sync/health >/dev/null 2>&1; then
        log_success "HTTP API доступен на порту 8081"
    else
        log_warning "HTTP API пока недоступен (может потребоваться время)"
    fi

    if [ "$all_ok" = true ]; then
        log_success "Все проверки пройдены успешно"
    else
        log_warning "Некоторые проверки не пройдены, проверьте логи"
    fi
}

print_usage_info() {
    print_header "Установка завершена успешно! 🎉"

    echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Система синхронизации установлена и запущена${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "${BLUE}📊 Управление сервисами:${NC}"
    echo ""
    echo -e "  Статус таймера:"
    echo -e "    ${YELLOW}systemctl status hiddify-child-sync.timer${NC}"
    echo ""
    echo -e "  Статус API:"
    echo -e "    ${YELLOW}systemctl status hiddify-sync-api.service${NC}"
    echo ""
    echo -e "  Логи синхронизации:"
    echo -e "    ${YELLOW}journalctl -u hiddify-child-sync.service -f${NC}"
    echo ""
    echo -e "  Ручной запуск синхронизации:"
    echo -e "    ${YELLOW}systemctl start hiddify-child-sync.service${NC}"
    echo ""
    echo -e "${BLUE}🔍 HTTP API мониторинга:${NC}"
    echo ""
    echo -e "  Health check:"
    echo -e "    ${YELLOW}curl http://localhost:8081/api/v2/hiddify-sync/health | jq${NC}"
    echo ""
    echo -e "  Детальный статус:"
    echo -e "    ${YELLOW}curl http://localhost:8081/api/v2/hiddify-sync/status | jq${NC}"
    echo ""
    echo -e "${BLUE}⏱️  Автоматическая синхронизация:${NC}"
    echo ""
    echo -e "  • Запускается каждые ${GREEN}5 минут${NC}"
    echo -e "  • Первый запуск через ${GREEN}2 минуты${NC} после загрузки"
    echo -e "  • Случайная задержка до ${GREEN}30 секунд${NC} (избежание коллизий)"
    echo ""
    echo -e "${BLUE}📖 Документация:${NC}"
    echo ""
    echo -e "  GitHub: ${YELLOW}https://github.com/${GITHUB_REPO}${NC}"
    echo ""
    echo -e "${GREEN}✅ Готово! Child-server синхронизируется с parent панелью${NC}"
    echo ""
}

cleanup() {
    log_info "Очистка временных файлов..."
    rm -rf "/tmp/hiddify-child-sync-install"
}

# ============================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================================

main() {
    clear

    print_header "Hiddify Child→Parent User Sync Installer v$VERSION"

    # Проверки
    check_root
    check_hiddify
    check_dependencies

    # Получаем параметры от пользователя
    get_user_input

    # Проверяем подключение к parent
    test_parent_connection

    # Загружаем файлы
    temp_dir=$(download_files)

    # Устанавливаем
    install_scripts "$temp_dir"
    configure_scripts
    install_systemd_services "$temp_dir"
    start_services

    # Тестируем
    run_test_sync

    # Проверяем
    verify_installation

    # Очищаем
    cleanup

    # Показываем инструкции
    print_usage_info
}

# Запуск
main "$@"
