#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hiddify Child Sync Health API v2.0
HTTP API для мониторинга состояния синхронизации child-server с parent панелью

ENDPOINTS:
- GET /api/v2/hiddify-sync/health - основная проверка здоровья системы
- GET /api/v2/hiddify-sync/status - детальный статус всех компонентов
- GET /api/v2/hiddify-sync/logs - последние логи синхронизации

ПОРТ: 8081 (localhost only для безопасности)

ИСПОЛЬЗОВАНИЕ:
curl http://localhost:8081/api/v2/hiddify-sync/health | jq

АВТОР: Система мониторинга для Hiddify Manager Child Sync
ЛИЦЕНЗИЯ: MIT
"""

import json
import subprocess
import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import pymysql

# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================

# Порт для HTTP сервера (только localhost)
API_PORT = 8081

# Конфигурация подключения к базе данных
DB_CONFIG = {
    'unix_socket': '/var/run/mysqld/mysqld.sock',
    'user': 'root',
    'database': 'hiddifypanel',
    'charset': 'utf8mb4'
}

# ============================================================================
# HTTP REQUEST HANDLER
# ============================================================================

class SyncHealthHandler(BaseHTTPRequestHandler):
    """HTTP request handler для мониторинга синхронизации"""

    def do_GET(self):
        """Обработка GET запросов"""
        parsed = urlparse(self.path)

        if parsed.path == '/api/v2/hiddify-sync/health':
            self.handle_health()
        elif parsed.path == '/api/v2/hiddify-sync/status':
            self.handle_status()
        elif parsed.path == '/api/v2/hiddify-sync/logs':
            self.handle_logs()
        else:
            self.send_error(404, "Not Found")

    def handle_health(self):
        """
        Основной endpoint для проверки здоровья синхронизации

        Возвращает:
        {
            "status": "healthy" | "unhealthy",
            "timestamp": "ISO datetime",
            "sync_service": {"active": bool, "enabled": bool},
            "database": {"accessible": bool, "user_count": int},
            "last_sync": {"last_log": str},
            "users_summary": {
                "enabled_users": int,
                "disabled_users": int,
                "users_with_traffic": int
            }
        }
        """
        try:
            health_data = {
                "status": "healthy",
                "timestamp": datetime.datetime.now().isoformat(),
                "sync_service": self.get_sync_service_status(),
                "database": self.get_database_status(),
                "last_sync": self.get_last_sync_info(),
                "users_summary": self.get_users_summary()
            }

            # Определяем общий статус
            if (health_data["sync_service"]["active"] and
                health_data["database"]["accessible"]):
                health_data["status"] = "healthy"
            else:
                health_data["status"] = "unhealthy"

            self.send_json_response(health_data)
        except Exception as e:
            error_data = {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.datetime.now().isoformat()
            }
            self.send_json_response(error_data, 500)

    def handle_status(self):
        """
        Детальный статус синхронизации

        Возвращает:
        {
            "sync_timer": {"status_output": str},
            "sync_service": {"active": bool, "enabled": bool},
            "database": {"accessible": bool, "user_count": int},
            "configuration": {"files": {...}}
        }
        """
        try:
            status_data = {
                "sync_timer": self.get_timer_status(),
                "sync_service": self.get_sync_service_status(),
                "database": self.get_database_status(),
                "configuration": self.get_config_status()
            }
            self.send_json_response(status_data)
        except Exception as e:
            self.send_json_response({"error": str(e)}, 500)

    def handle_logs(self):
        """
        Последние логи синхронизации

        Возвращает:
        {
            "logs": [
                {
                    "timestamp": str,
                    "message": str,
                    "priority": str
                },
                ...
            ]
        }
        """
        try:
            cmd = ['journalctl', '-u', 'hiddify-child-sync.service', '--no-pager', '-n', '20', '--output=json']
            result = subprocess.run(cmd, capture_output=True, text=True)

            logs = []
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line.strip():
                        try:
                            log_entry = json.loads(line)
                            logs.append({
                                "timestamp": log_entry.get("__REALTIME_TIMESTAMP"),
                                "message": log_entry.get("MESSAGE", ""),
                                "priority": log_entry.get("PRIORITY", "6")
                            })
                        except json.JSONDecodeError:
                            continue

            self.send_json_response({"logs": logs})
        except Exception as e:
            self.send_json_response({"error": str(e)}, 500)

    # ========================================================================
    # HELPER METHODS
    # ========================================================================

    def get_sync_service_status(self):
        """Получить статус systemd сервиса синхронизации"""
        try:
            cmd = ['systemctl', 'is-active', 'hiddify-child-sync.timer']
            result = subprocess.run(cmd, capture_output=True, text=True)
            active = result.stdout.strip() == 'active'

            cmd = ['systemctl', 'is-enabled', 'hiddify-child-sync.timer']
            result = subprocess.run(cmd, capture_output=True, text=True)
            enabled = result.stdout.strip() == 'enabled'

            return {"active": active, "enabled": enabled}
        except:
            return {"active": False, "enabled": False}

    def get_timer_status(self):
        """Получить детальный статус таймера"""
        try:
            cmd = ['systemctl', 'status', 'hiddify-child-sync.timer', '--no-pager', '-l']
            result = subprocess.run(cmd, capture_output=True, text=True)
            return {"status_output": result.stdout}
        except:
            return {"status_output": "Unable to get timer status"}

    def get_database_status(self):
        """Получить статус подключения к базе данных"""
        try:
            conn = pymysql.connect(**DB_CONFIG)
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM user")
                user_count = cursor.fetchone()[0]
            conn.close()
            return {"accessible": True, "user_count": user_count}
        except Exception as e:
            return {"accessible": False, "error": str(e)}

    def get_users_summary(self):
        """Получить краткую сводку по пользователям"""
        try:
            conn = pymysql.connect(**DB_CONFIG)
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM user WHERE enable = 1")
                enabled_count = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM user WHERE enable = 0")
                disabled_count = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM user WHERE current_usage > 1000000")
                with_traffic_count = cursor.fetchone()[0]

            conn.close()
            return {
                "enabled_users": enabled_count,
                "disabled_users": disabled_count,
                "users_with_traffic": with_traffic_count
            }
        except Exception as e:
            return {"error": str(e)}

    def get_last_sync_info(self):
        """Получить информацию о последней синхронизации"""
        try:
            cmd = ['journalctl', '-u', 'hiddify-child-sync.service', '--no-pager', '-n', '1']
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split('\n')
                last_line = lines[-1] if lines else ""
                return {"last_log": last_line}
            else:
                return {"last_log": "No recent sync logs"}
        except Exception as e:
            return {"error": str(e)}

    def get_config_status(self):
        """Получить статус конфигурационных файлов"""
        import os
        files_to_check = [
            '/opt/hiddify-manager/stable_sync.py',
            '/etc/systemd/system/hiddify-child-sync.service',
            '/etc/systemd/system/hiddify-child-sync.timer'
        ]

        file_status = {}
        for file_path in files_to_check:
            file_status[file_path] = {
                "exists": os.path.exists(file_path),
                "size": os.path.getsize(file_path) if os.path.exists(file_path) else 0
            }

        return {"files": file_status}

    def send_json_response(self, data, status_code=200):
        """Отправить JSON ответ"""
        response = json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')

        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(response)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args):
        """Отключаем стандартное логирование запросов (используем journald)"""
        pass

# ============================================================================
# MAIN
# ============================================================================

def main():
    """Запуск HTTP сервера на localhost:8081"""
    server_address = ('127.0.0.1', API_PORT)
    httpd = HTTPServer(server_address, SyncHealthHandler)

    print(f"🔍 Hiddify Sync Health API v2.0 запущен на порту {API_PORT}")
    print(f"📊 Доступные endpoints:")
    print(f"   • GET /api/v2/hiddify-sync/health - основная проверка здоровья")
    print(f"   • GET /api/v2/hiddify-sync/status - детальный статус")
    print(f"   • GET /api/v2/hiddify-sync/logs - последние логи")
    print(f"")
    print(f"🔒 ВАЖНО: API доступен только на localhost для безопасности!")
    print(f"")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Сервер остановлен")
        httpd.server_close()

if __name__ == '__main__':
    main()
