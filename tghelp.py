#!/usr/bin/env python3
import sys
import logging
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import quote, urlparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import config

# Настройка логирования
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format=config.LOG_FORMAT if hasattr(config, 'LOG_FORMAT') else '%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# --- Конфигурация безопасности (можно вынести в config.py) ---
ALLOWED_TARGET_HOSTS = [
    'api.telegram.org',
    'telegram.org',
    't.me',
    'web.telegram.org',
    'cdn.telegram.org',
    'core.telegram.org',
    'upload.telegram.org',
    'venus.telegram.org',
    'aurora.telegram.org',
    'vesta.telegram.org',
]
ALLOWED_TARGET_IP_RANGES = [
    '91.108.56.0/22', '91.108.4.0/22', '91.108.8.0/22',
    '91.108.16.0/22', '91.108.12.0/22', '149.154.160.0/20',
    '91.105.192.0/23', '91.108.20.0/22', '185.76.151.0/24',
    '2001:b28:f23d::/48', '2001:b28:f23f::/48', '2001:67c:4e8::/48',
    '2001:b28:f23c::/48', '2a0a:f280::/32'
]
ALLOWED_CLIENT_HEADERS = {
    'content-type', 'accept', 'accept-encoding', 'accept-language',
    'authorization', 'user-agent', 'x-requested-with',
    'cache-control', 'pragma', 'referer', 'origin'
}
MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 МБ
REQUEST_TIMEOUT = getattr(config, 'REQUEST_TIMEOUT', 30)
CONNECT_TIMEOUT = 10
MAX_RETRIES = 2

# --- Вспомогательные функции ---
def ip_in_range(ip, range_cidr):
    """Проверяет, входит ли IP в CIDR-диапазон."""
    import ipaddress
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(range_cidr, strict=False)
    except ValueError:
        return False

def is_target_url_safe(url):
    """Жёсткая проверка целевого URL."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != 'https':
        return False
    host = parsed.hostname
    if not host:
        return False
    # Запрет IP-адресов в URL
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    # Домен должен быть в белом списке
    if host.lower() not in [h.lower() for h in ALLOWED_TARGET_HOSTS]:
        return False
    # Резолвинг хоста и проверка IP (защита от DNS-ребиндинга)
    try:
        import socket
        ips = [addr[4][0] for addr in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]
    except socket.gaierror:
        return False
    for ip in ips:
        allowed = any(ip_in_range(ip, r) for r in ALLOWED_TARGET_IP_RANGES)
        if not allowed:
            return False
    return True

def filter_client_headers(raw_headers):
    """Оставляет только разрешённые заголовки, удаляет CRLF-инъекции."""
    clean = {}
    for name, value in raw_headers.items():
        safe_name = name.replace('\r', '').replace('\n', '').strip().lower()
        safe_value = value.replace('\r', '').replace('\n', '').strip()
        if safe_name in ALLOWED_CLIENT_HEADERS:
            clean[safe_name] = safe_value
    return clean

# --- Менеджер серверов (оставлен почти без изменений) ---
class ServerManager:
    def __init__(self, servers_list):
        self.original_servers = servers_list
        self.active_servers = []
        self.lock = threading.Lock()
        self.current_index = 0
        self._check_and_sort_servers()

    def _check_and_sort_servers(self):
        logger.info("Checking servers...")
        results = []
        for url in self.original_servers:
            latency = self._ping_server(url)
            if latency is not None:
                results.append((url, latency))
                logger.info(f"  OK: {url} ({latency:.2f}s)")
            else:
                logger.warning(f"  FAIL: {url}")
        if not results:
            logger.error("No servers available, using first as fallback")
            self.active_servers = [(self.original_servers[0], 999)]
        else:
            results.sort(key=lambda x: x[1])
            self.active_servers = results
            logger.info(f"Best server: {self.active_servers[0][0]}")

    def _ping_server(self, url):
        try:
            start = time.time()
            resp = requests.get(url, timeout=CONNECT_TIMEOUT, verify=True)
            elapsed = time.time() - start
            return elapsed
        except Exception:
            return None

    def get_best_server(self):
        with self.lock:
            if not self.active_servers:
                return self.original_servers[0]
            return self.active_servers[self.current_index][0]

    def mark_failed_and_switch(self, failed_url):
        with self.lock:
            if not self.active_servers:
                return
            cur = self.active_servers[self.current_index][0]
            if cur == failed_url:
                logger.warning(f"Server failed: {failed_url}")
                self.current_index = (self.current_index + 1) % len(self.active_servers)
                if self.current_index == 0:
                    logger.error("All servers are down!")
                else:
                    logger.info(f"Switched to: {self.active_servers[self.current_index][0]}")

# Глобальный менеджер
server_manager = ServerManager(config.PHP_PROXY_SERVERS)

# --- HTTP обработчик ---
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class ProxyHandler(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT

    def do_GET(self): self._safe_forward()
    def do_POST(self): self._safe_forward()
    # Остальные методы не нужны для прокси Telegram
    # def do_PUT etc.

    def _safe_forward(self):
        try:
            self._forward()
        except Exception as e:
            logger.error(f"Unhandled error: {e}", exc_info=True)
            self._send_error(502)

    def _forward(self):
        # --- 1. Извлечение и валидация целевого URL ---
        target = self.path
        if target.startswith('/'):
            target = target.lstrip('/')
        # Ожидаем абсолютный URL, начинающийся с https://
        if not target.startswith('https://'):
            self._send_error(400, "Only absolute HTTPS URLs are allowed")
            return
        if len(target) > 2048:
            self._send_error(414)
            return
        if not is_target_url_safe(target):
            self._send_error(403, "Access denied")
            return

        base_php_url = server_manager.get_best_server()
        upstream_url = f"{base_php_url}?url={quote(target, safe='')}"

        # --- 2. Чтение тела запроса с ограничением ---
        content_length = self.headers.get('Content-Length')
        body = None
        if content_length:
            try:
                clen = int(content_length)
                if clen < 0 or clen > MAX_BODY_SIZE:
                    self._send_error(413)
                    return
                if clen > 0:
                    body = self.rfile.read(clen)
            except (ValueError, OSError):
                self._send_error(400)
                return

        # --- 3. Фильтрация заголовков ---
        client_headers = {k: v for k, v in self.headers.items()}
        fwd_headers = filter_client_headers(client_headers)
        # Принудительно заменяем User-Agent для единообразия
        fwd_headers['User-Agent'] = 'TelegramProxy/2.0'

        # --- 4. Отправка запроса через requests с проверкой SSL ---
        session = requests.Session()
        retries = Retry(
            total=MAX_RETRIES,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504]
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount('https://', adapter)   # только HTTPS, т.к. PHP-сервер должен быть HTTPS
        session.mount('http://', adapter)    # на случай, если PHP-сервер на HTTP (тогда verify=False? риск)

        try:
            resp = session.request(
                method=self.command,
                url=upstream_url,
                headers=fwd_headers,
                data=body,
                verify=True,                # Требуем валидный SSL-сертификат
                timeout=REQUEST_TIMEOUT,
                stream=True
            )
        except requests.exceptions.Timeout:
            server_manager.mark_failed_and_switch(base_php_url)
            self._send_error(504)
            return
        except requests.exceptions.ConnectionError:
            server_manager.mark_failed_and_switch(base_php_url)
            self._send_error(502)
            return
        except Exception as e:
            logger.error(f"Upstream request error: {e}")
            self._send_error(502)
            return

        # --- 5. Проброс ответа с фильтрацией заголовков ---
        try:
            self.send_response(resp.status_code)
            # Заголовки, которые нельзя передавать клиенту
            prohibited = {'transfer-encoding', 'connection', 'keep-alive', 'content-encoding', 'content-security-policy'}
            for k, v in resp.headers.items():
                if k.lower() in prohibited:
                    continue
                self.send_header(k, v)
            self.end_headers()

            # Чтение ответа с ограничением размера? Можно добавить в будущем.
            for chunk in resp.iter_content(chunk_size=8192):
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            logger.error(f"Error sending response: {e}")
        finally:
            session.close()

    def _send_error(self, code, message=None):
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            default_msgs = {400: 'Bad Request', 403: 'Forbidden', 413: 'Payload Too Large',
                            414: 'URI Too Long', 502: 'Bad Gateway', 504: 'Gateway Timeout'}
            msg = message or default_msgs.get(code, 'Error')
            self.wfile.write(msg.encode())
        except Exception:
            pass

    def log_message(self, format, *args):
        # Отключаем стандартное логирование, используем наш logger
        pass

if __name__ == '__main__':
    port = config.LISTEN_PORT
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    server = ThreadedHTTPServer(('0.0.0.0', port), ProxyHandler)
    print(f"Proxy started on 0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped")
        server.server_close()
