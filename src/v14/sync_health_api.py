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
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
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
    'charset': 'utf8mb4',
    'connect_timeout': 5,       # зависшая БД не должна вешать health
    'read_timeout': 10,
    'write_timeout': 10,
}

# Статус последнего прогона синка (пишет stable_sync.py v4.5.x-h14)
STATUS_FILE = '/var/lib/hiddify-child-sync/status.json'
MAX_AGE_S = 15 * 60     # статус и последний успешный шаг 6 не старше 15 минут (таймер — раз в 5 минут)

# ============================================================================
# HTTP REQUEST HANDLER
# ============================================================================

class SyncHealthHandler(BaseHTTPRequestHandler):
    """HTTP request handler для мониторинга синхронизации"""
    timeout = 10    # «молчащее» соединение не держит обработчик дольше 10 с (сервер потоковый)

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
            "status": "healthy" | "degraded" | "unhealthy",   # unhealthy → HTTP 503
            "reasons": [str],       # почему не healthy (пусто при healthy)
            "last_run": {...},      # статус последнего прогона синка (STATUS_FILE)
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

            # Общий статус: не только «таймер и БД живы», но и «синк реально проходит»
            # (v4.3 выходил с кодом 0 и при упавшем шаге 6 — health сутки показывал healthy).
            last_run = self.get_last_run()
            health_data["last_run"] = last_run
            bad, warn = [], []
            if not health_data["sync_service"].get("active"):
                bad.append("таймер синка не активен")
            if not health_data["database"].get("accessible"):
                bad.append("БД недоступна")
            if last_run is None:
                bad.append("нет или нечитаем статус синка " + STATUS_FILE)
            else:
                fails = last_run.get("consecutive_failures") or 0
                if last_run["age_s"] > MAX_AGE_S:
                    bad.append(f"статус устарел ({last_run['age_s']} с)")
                if fails >= 2:
                    bad.append(f"{fails} прогона подряд с ошибкой: {last_run.get('last_error') or last_run.get('steps')}")
                elif not last_run.get("ok"):
                    warn.append(f"последний прогон с ошибкой: {last_run.get('last_error') or last_run.get('steps')}")
                if last_run.get("users_skipped"):
                    warn.append("шаг 6 выключен (SYNC_SKIP_USERS=1)")
                elif last_run.get("users_ok_age_s") is None or last_run["users_ok_age_s"] > MAX_AGE_S:
                    bad.append("синхронизация пользователей (шаг 6) давно не проходила")
                expected, actual = last_run.get("expected_count"), health_data["database"].get("user_count")
                if isinstance(expected, int) and isinstance(actual, int) and expected != actual:
                    warn.append(f"ожидается {expected} пользователей по parent, в БД {actual}")
                if last_run.get("backlog"):
                    warn.append(f"синк догоняет: {last_run['backlog']} изменений перенесено на следующие прогоны")
                if last_run.get("held_blocks"):
                    warn.append(f"массовое выключение: {last_run['held_blocks']} ждут подтверждения parent")
                if last_run.get("held_deletes"):
                    warn.append(f"массовое удаление: {last_run['held_deletes']} удалений отложено (идёт по частям)")
            health_data["reasons"] = bad + warn
            health_data["status"] = "unhealthy" if bad else ("degraded" if warn else "healthy")

            self.send_json_response(health_data, 503 if bad else 200)
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
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

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
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            active = result.stdout.strip() == 'active'

            cmd = ['systemctl', 'is-enabled', 'hiddify-child-sync.timer']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            enabled = result.stdout.strip() == 'enabled'

            return {"active": active, "enabled": enabled}
        except:
            return {"active": False, "enabled": False}

    def get_timer_status(self):
        """Получить детальный статус таймера"""
        try:
            cmd = ['systemctl', 'status', 'hiddify-child-sync.timer', '--no-pager', '-l']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return {"status_output": result.stdout}
        except:
            return {"status_output": "Unable to get timer status"}

    def get_database_status(self):
        """Получить статус подключения к базе данных"""
        try:
            conn = pymysql.connect(**DB_CONFIG)
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM user WHERE COALESCE(deleted,0)=0")
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
                cursor.execute("SELECT COUNT(*) FROM user WHERE enable = 1 AND COALESCE(deleted,0)=0")
                enabled_count = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM user WHERE enable = 0 AND COALESCE(deleted,0)=0")
                disabled_count = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM user WHERE current_usage > 1000000 AND COALESCE(deleted,0)=0")
                with_traffic_count = cursor.fetchone()[0]

            conn.close()
            return {
                "enabled_users": enabled_count,
                "disabled_users": disabled_count,
                "users_with_traffic": with_traffic_count
            }
        except Exception as e:
            return {"error": str(e)}

    def get_last_run(self):
        """Статус последнего прогона синка из STATUS_FILE + результат systemd-юнита; None, если нечитаем."""
        import json as _json
        import time as _time
        try:
            with open(STATUS_FILE, encoding="utf-8") as f:
                st = _json.load(f)
            if not isinstance(st, dict) or not isinstance(st.get("ts"), (int, float)):
                return None
        except Exception:
            return None
        now = _time.time()
        st["age_s"] = int(now - st["ts"])
        ok_ts = st.get("last_users_ok_ts")
        st["users_ok_age_s"] = int(now - ok_ts) if isinstance(ok_ts, (int, float)) else None
        try:
            out = subprocess.run(['systemctl', 'show', '-p', 'Result,ExecMainStatus', 'hiddify-child-sync.service'],
                                 capture_output=True, text=True, timeout=10).stdout
            st["unit"] = dict(line.split('=', 1) for line in out.strip().splitlines() if '=' in line)
        except Exception:
            pass
        return st

    def get_last_sync_info(self):
        """Получить информацию о последней синхронизации"""
        try:
            cmd = ['journalctl', '-u', 'hiddify-child-sync.service', '--no-pager', '-n', '1']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

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
            '/opt/hiddify-custom/tools/stable_sync.py',
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
    httpd = ThreadingHTTPServer(server_address, SyncHealthHandler)
    httpd.daemon_threads = True

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
