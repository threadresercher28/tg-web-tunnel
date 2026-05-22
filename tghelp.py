#!/usr/bin/env python3
import sys
import logging
import urllib3
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import quote
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Импортируем настройки
import config

# Отключаем предупреждения о сертификатах
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Настройка логирования
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format=config.LOG_FORMAT
)
logger = logging.getLogger(__name__)

class ServerManager:
    """Управляет списком серверов, их рейтингом и выбором лучшего."""

    def __init__(self, servers_list):
        self.original_servers = servers_list
        self.active_servers = [] # Список кортежей (url, latency)
        self.lock = threading.Lock()
        self.current_index = 0

        # Инициализация: проверяем серверы
        self._check_and_sort_servers()

    def _check_and_sort_servers(self):
        """Пингует все серверы и сортирует их по скорости."""
        logger.info("🔍 Проверка доступности серверов...")
        results = []

        for url in self.original_servers:
            latency = self._ping_server(url)
            if latency is not None:
                results.append((url, latency))
                logger.info(f"   ✅ {url} — OK ({latency:.2f}s)")
            else:
                logger.warning(f"   ❌ {url} — Недоступен")

        if not results:
            logger.error("⛔ Нет доступных серверов! Прокси не сможет работать.")
            self.active_servers = [(self.original_servers[0], 999)] # Fallback на первый
        else:
            # Сортируем по времени отклика (меньше = лучше)
            results.sort(key=lambda x: x[1])
            self.active_servers = results
            logger.info(f"🚀 Лучший сервер: {self.active_servers[0][0]}")

    def _ping_server(self, url):
        """Делает быстрый HEAD запрос для проверки сервера."""
        try:
            start_time = time.time()
            # Пробуем сделать легкий запрос к самому скрипту или его корню
            # Если prx.php требует параметр url, можно попробовать отправить пустой или неверный,
            # но лучше просто проверить доступность хоста.
            # Здесь мы просто делаем GET к URL, ожидая любого ответа (даже 400/404)
            resp = requests.get(url, timeout=config.PING_TIMEOUT, verify=False)
            elapsed = time.time() - start_time
            return elapsed
        except Exception:
            return None

    def get_best_server(self):
        """Возвращает URL текущего лучшего сервера."""
        with self.lock:
            if not self.active_servers:
                return self.original_servers[0] # Крайний случай
            return self.active_servers[self.current_index][0]

    def mark_failed_and_switch(self, failed_url):
        """Если сервер упал, переключаемся на следующий."""
        with self.lock:
            if not self.active_servers:
                return

            current_url = self.active_servers[self.current_index][0]

            # Если упал тот, который мы сейчас используем
            if current_url == failed_url:
                logger.warning(f"⚠️ Сервер {failed_url} недоступен. Переключение...")

                # Пробуем следующий
                next_index = (self.current_index + 1) % len(self.active_servers)

                # Если прошли круг и вернулись к тому же, значит все упали
                if next_index == self.current_index:
                    logger.error("❌ Все серверы в списке недоступны!")
                else:
                    self.current_index = next_index
                    new_url = self.active_servers[self.current_index][0]
                    logger.info(f"🔄 Переключено на: {new_url}")

# Глобальный менеджер серверов
server_manager = ServerManager(config.PHP_PROXY_SERVERS)

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class ProxyHandler(BaseHTTPRequestHandler):
    timeout = 60

    def do_GET(self): self._safe_forward()
    def do_POST(self): self._safe_forward()
    def do_PUT(self): self._safe_forward()
    def do_DELETE(self): self._safe_forward()
    def do_HEAD(self): self._safe_forward()
    def do_OPTIONS(self): self._safe_forward()

    def _safe_forward(self):
        try:
            self._forward()
        except Exception as e:
            logger.error(f"Критическая ошибка: {e}")
            try:
                if not self.wfile.closed:
                    self.send_error(502, "Internal Proxy Error")
            except:
                pass

    def _forward(self):
        session = None
        # Получаем текущий лучший сервер
        base_php_url = server_manager.get_best_server()

        try:
            # ---------- 1. Целевой URL ----------
            target = self.path
            if not target.startswith(('http://', 'https://')):
                host = self.headers.get('Host', '')
                if not host:
                    raise ValueError("Отсутствует Host")
                target = f'http://{host}{target}'

            upstream_url = f"{base_php_url}?url={quote(target, safe='')}"

            # ---------- 2. Тело запроса ----------
            content_length = self.headers.get('Content-Length')
            body = None
            if content_length:
                try:
                    clen = int(content_length)
                    if clen > 0:
                        body = self.rfile.read(clen)
                except ValueError:
                    pass

            # ---------- 3. Заголовки ----------
            fwd_headers = {}
            for k, v in self.headers.items():
                kl = k.lower()
                if kl not in ('host', 'connection', 'proxy-connection', 'content-length', 'transfer-encoding', 'keep-alive'):
                    fwd_headers[k] = v
            if 'Content-Type' in self.headers:
                 fwd_headers['Content-Type'] = self.headers['Content-Type']

            # ---------- 4. Сессия ----------
            session = requests.Session()
            retries = Retry(
                total=config.MAX_RETRIES,
                backoff_factor=config.BACKOFF_FACTOR,
                status_forcelist=config.RETRY_STATUS_CODES,
                allowed_methods=frozenset(['GET', 'POST', 'PUT', 'DELETE', 'HEAD'])
            )
            adapter = HTTPAdapter(max_retries=retries)
            session.mount('http://', adapter)
            session.mount('https://', adapter)

            logger.debug(f"-> {self.command} {target} via {base_php_url}")

            resp = session.request(
                method=self.command,
                url=upstream_url,
                headers=fwd_headers,
                data=body,
                verify=False,
                stream=True,
                timeout=config.REQUEST_TIMEOUT
            )

            # ---------- 5. Ответ клиенту ----------
            try:
                self.send_response(resp.status_code)
                for k, v in resp.headers.items():
                    kl = k.lower()
                    if kl not in ('transfer-encoding', 'connection', 'keep-alive', 'content-encoding'):
                        if kl == 'transfer-encoding' and v.lower() == 'chunked':
                            continue
                        try:
                            self.send_header(k, v)
                        except:
                            pass

                self.end_headers()

                for chunk in resp.iter_content(config.CHUNK_SIZE):
                    if chunk:
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        except Exception:
                            return
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as write_err:
                logger.error(f"Ошибка записи: {write_err}")

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout на сервере {base_php_url}")
            # Помечаем сервер как плохой и пробуем другой в следующий раз
            server_manager.mark_failed_and_switch(base_php_url)
            self._safe_error(504, "Gateway Timeout")

        except requests.exceptions.ConnectionError:
            logger.warning(f"ConnectionError на сервере {base_php_url}")
            server_manager.mark_failed_and_switch(base_php_url)
            self._safe_error(502, "Bad Gateway")

        except Exception as e:
            logger.error(f"Ошибка: {e}")
            self._safe_error(502, "Proxy Error")
        finally:
            if session:
                session.close()

    def _safe_error(self, code, message):
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(message.encode('utf-8'))
        except:
            pass

    def log_message(self, format, *args):
        pass

if __name__ == '__main__':
    port = config.LISTEN_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print("Неверный порт")
            sys.exit(1)

    server = ThreadedHTTPServer(('0.0.0.0', port), ProxyHandler)

    print(f"✅ Прокси запущен на 0.0.0.0:{port}")
    print(f"📋 Серверов в пуле: {len(config.PHP_PROXY_SERVERS)}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⏹️ Сервер остановлен")
        server.server_close()
