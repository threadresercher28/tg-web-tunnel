#!/usr/bin/env python3
import sys
import logging
import urllib3
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import quote
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import traceback

# Отключаем предупреждения о непроверенных сертификатах
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PHP_PROXY_URL = "http://f7-cert.x10.mx/prx.php"   # ваш PHP-скрипт

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(threadName)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Обрабатывает каждый запрос в новом потоке, чтобы один сбой не ронял весь сервер."""
    daemon_threads = True  # Потоки будут убиты при выходе из основного процесса
    allow_reuse_address = True

class ProxyHandler(BaseHTTPRequestHandler):

    # Увеличиваем таймаут для самих сокетов сервера (опционально)
    timeout = 60

    def do_GET(self):
        self._safe_forward()
    def do_POST(self):
        self._safe_forward()
    def do_PUT(self):
        self._safe_forward()
    def do_DELETE(self):
        self._safe_forward()
    def do_HEAD(self):
        self._safe_forward()
    def do_OPTIONS(self):
        self._safe_forward()

    def _safe_forward(self):
        """Обертка для перехвата любых исключений, чтобы поток не падал молча."""
        try:
            self._forward()
        except Exception as e:
            # Ловим ВСЕ ошибки, чтобы поток не крашился незаметно
            logger.error(f"Критическая ошибка в обработчике: {e}")
            logger.debug(traceback.format_exc())
            try:
                if not self.wfile.closed:
                    self.send_error(502, "Internal Proxy Error")
            except:
                pass

    def _forward(self):
        session = None
        try:
            # ---------- 1. Целевой URL ----------
            target = self.path
            # Проверка на абсолютный URL в path (иногда клиенты шлют полный URL)
            if not target.startswith(('http://', 'https://')):
                host = self.headers.get('Host', '')
                if not host:
                    # Если нет Host, пытаемся взять из server_address (редкий кейс)
                    raise ValueError("Отсутствует заголовок Host")
                target = f'http://{host}{target}'

            upstream_url = f"{PHP_PROXY_URL}?url={quote(target, safe='')}"

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

            # ---------- 3. Заголовки для пересылки ----------
            fwd_headers = {}
            for k, v in self.headers.items():
                kl = k.lower()
                # Удаляем hop-by-hop заголовки
                if kl not in ('host', 'connection', 'proxy-connection', 'content-length', 'transfer-encoding', 'keep-alive'):
                    fwd_headers[k] = v

            # Важно: Content-Type нужно передать, если есть тело
            if 'Content-Type' in self.headers:
                 fwd_headers['Content-Type'] = self.headers['Content-Type']

            # ---------- 4. Сессия с повторными попытками ----------
            session = requests.Session()
            retries = Retry(
                total=2,                # уменьшил до 2, чтобы не ждать слишком долго при сбоях PHP
                backoff_factor=0.5,
                status_forcelist=[500, 502, 503, 504],
                allowed_methods=frozenset(['GET', 'POST', 'PUT', 'DELETE', 'HEAD'])
            )
            adapter = HTTPAdapter(max_retries=retries)
            session.mount('http://', adapter)
            session.mount('https://', adapter)

            logger.info(f"-> {self.command} {target}")

            resp = session.request(
                method=self.command,
                url=upstream_url,
                headers=fwd_headers,
                data=body,
                verify=False,
                stream=True,
                timeout=(10, 60)       # connect 10s, read 60s
            )

            # ---------- 5. Отправка ответа клиенту ----------
            try:
                self.send_response(resp.status_code)

                # Передаем заголовки ответа
                for k, v in resp.headers.items():
                    kl = k.lower()
                    # Фильтруем заголовки, которые могут сломать ответ от нашего сервера
                    if kl not in ('transfer-encoding', 'connection', 'keep-alive', 'content-encoding'):
                        # Иногда PHP прокси может вернуть chunked, лучше убрать, если мы пишем сами
                        if kl == 'transfer-encoding' and v.lower() == 'chunked':
                            continue
                        try:
                            self.send_header(k, v)
                        except:
                            pass

                self.end_headers()

                # Читаем и пишем чанками
                for chunk in resp.iter_content(8192):
                    if chunk:
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            logger.debug("Клиент отключился во время передачи данных")
                            return # Прерываем цикл, клиент ушел
                        except Exception as write_err:
                            logger.warning(f"Ошибка записи в сокет: {write_err}")
                            return

            except (BrokenPipeError, ConnectionResetError):
                logger.debug("Клиент оборвал соединение до начала ответа")
            except Exception as write_err:
                logger.error(f"Ошибка при формировании ответа: {write_err}")

        except requests.exceptions.Timeout:
            logger.warning("Timeout при запросе к PHP прокси")
            self._safe_error(504, "Gateway Timeout")
        except requests.exceptions.ConnectionError:
            logger.warning("ConnectionError при запросе к PHP прокси")
            self._safe_error(502, "Bad Gateway (PHP Unreachable)")
        except Exception as e:
            logger.error(f"Непредвиденная ошибка прокси: {e}")
            self._safe_error(502, f"Proxy error")
        finally:
            if session:
                session.close()

    def _safe_error(self, code, message):
        """Попытаться отправить код ошибки, игнорируя обрыв соединения."""
        try:
            # send_error сам делает end_headers и пишет тело, если нужно
            self.send_response(code)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(message.encode('utf-8'))
        except:
            pass

    def log_message(self, format, *args):
        # Можно включить, если нужно видеть каждый запрос в логе
        # logger.info("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), format%args))
        pass


if __name__ == '__main__':
    port = 80
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print("Неверный порт")
            sys.exit(1)

    # Используем ThreadedHTTPServer вместо обычного
    server = ThreadedHTTPServer(('0.0.0.0', port), ProxyHandler)

    print(f"✅ Прокси запущен на 0.0.0.0:{port} -> {PHP_PROXY_URL}")
    print("Используется многопоточный режим (ThreadingMixIn)")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⏹️ Сервер остановлен пользователем")
        server.server_close()
