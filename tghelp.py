#!/usr/bin/env python3
import sys
import socket
import select
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

PHP_PROXY_URL = "https://max.ru.ydns.eu/prx.php"   # ваш PHP-скрипт (используется только для HTTP, не для CONNECT)

CONNECT_TIMEOUT = 10     # сек на установку TCP-соединения к цели при CONNECT
RELAY_IDLE_TIMEOUT = 60  # сек простоя, после которого туннель закрывается

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(threadName)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Обрабатывает каждый запрос в новом потоке, чтобы один сбой не ронял весь сервер."""
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Перехватываем любые исключения на уровне сервера, чтобы они не летели в консоль
        # и уж тем более не роняли ThreadingMixIn.
        logger.error(f"Ошибка обработки запроса от {client_address}: {traceback.format_exc()}")


class ProxyHandler(BaseHTTPRequestHandler):

    timeout = 60
    protocol_version = "HTTP/1.1"

    # ---------------- Обычные HTTP-методы (через PHP-релей) ----------------

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

    def do_PATCH(self):
        self._safe_forward()

    # ---------------- HTTPS-туннель (CONNECT) ----------------

    def do_CONNECT(self):
        self._safe_connect()

    # ---------------- Обёртки с защитой от падений ----------------

    def _safe_forward(self):
        try:
            self._forward()
        except Exception as e:
            logger.error(f"Критическая ошибка в обработчике HTTP: {e}")
            logger.debug(traceback.format_exc())
            self._safe_error(502, "Internal Proxy Error")

    def _safe_connect(self):
        try:
            self._connect()
        except Exception as e:
            logger.error(f"Критическая ошибка в обработчике CONNECT: {e}")
            logger.debug(traceback.format_exc())
            self._safe_error(502, "Internal Proxy Error")

    # ---------------- Реализация CONNECT (прямой TCP-туннель, минуя PHP) ----------------

    def _connect(self):
        remote = None
        try:
            target = self.path  # ожидается вида "host:port"
            if ':' in target:
                host, port_str = target.rsplit(':', 1)
                try:
                    port = int(port_str)
                except ValueError:
                    self._safe_error(400, "Bad CONNECT port")
                    return
            else:
                host, port = target, 443

            logger.info(f"-> CONNECT {host}:{port}")

            try:
                remote = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
            except socket.timeout:
                logger.warning(f"CONNECT timeout к {host}:{port}")
                self._safe_error(504, "Gateway Timeout")
                return
            except OSError as e:
                logger.warning(f"CONNECT не удалось к {host}:{port}: {e}")
                self._safe_error(502, "Bad Gateway")
                return

            remote.settimeout(None)  # дальше работаем через select

            try:
                self.send_response(200, "Connection Established")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError, OSError):
                logger.debug("Клиент отключился до установки туннеля")
                return

            self._relay(self.connection, remote)

        finally:
            if remote:
                try:
                    remote.close()
                except Exception:
                    pass

    def _relay(self, client_sock, remote_sock):
        """Двусторонняя перекачка байт между клиентом и целевым сервером."""
        sockets = [client_sock, remote_sock]
        try:
            client_sock.settimeout(None)
        except Exception:
            pass

        try:
            while True:
                try:
                    readable, _, exceptional = select.select(sockets, [], sockets, RELAY_IDLE_TIMEOUT)
                except (OSError, ValueError):
                    break

                if exceptional:
                    break

                if not readable:
                    logger.debug("Туннель закрыт по таймауту простоя")
                    break

                closed = False
                for s in readable:
                    try:
                        data = s.recv(8192)
                    except (ConnectionResetError, OSError):
                        closed = True
                        break

                    if not data:
                        closed = True
                        break

                    other = remote_sock if s is client_sock else client_sock
                    try:
                        other.sendall(data)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        closed = True
                        break

                if closed:
                    break
        except Exception as e:
            logger.debug(f"Ошибка в relay: {e}")
        # сокеты закрываются вызывающим кодом / сервером

    # ---------------- Реализация обычного HTTP через PHP-релей ----------------

    def _forward(self):
        session = None
        try:
            target = self.path
            if not target.startswith(('http://', 'https://')):
                host = self.headers.get('Host', '')
                if not host:
                    raise ValueError("Отсутствует заголовок Host")
                target = f'http://{host}{target}'

            upstream_url = f"{PHP_PROXY_URL}?url={quote(target, safe='')}"

            content_length = self.headers.get('Content-Length')
            body = None
            if content_length:
                try:
                    clen = int(content_length)
                    if clen > 0:
                        body = self.rfile.read(clen)
                except ValueError:
                    pass

            fwd_headers = {}
            for k, v in self.headers.items():
                kl = k.lower()
                if kl not in ('host', 'connection', 'proxy-connection', 'content-length', 'transfer-encoding', 'keep-alive'):
                    fwd_headers[k] = v

            if 'Content-Type' in self.headers:
                fwd_headers['Content-Type'] = self.headers['Content-Type']

            session = requests.Session()
            retries = Retry(
                total=2,
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
                timeout=(10, 60)
            )

            try:
                self.send_response(resp.status_code)

                # requests сам расжимает тело (gzip/deflate/br) в iter_content,
                # поэтому оригинальный Content-Length (длина СЖАТЫХ байт) больше не верен.
                # Если было сжатие - убираем Content-Length и закрываем соединение по концу
                # передачи, вместо того чтобы врать клиенту про длину (это ломает HTTP/1.1
                # keep-alive и десинхронизирует следующий запрос на этом же сокете).
                had_content_encoding = bool(resp.headers.get('Content-Encoding'))

                for k, v in resp.headers.items():
                    kl = k.lower()
                    if kl in ('transfer-encoding', 'connection', 'keep-alive', 'content-encoding'):
                        continue
                    if kl == 'content-length' and had_content_encoding:
                        continue
                    try:
                        self.send_header(k, v)
                    except Exception:
                        pass

                if had_content_encoding:
                    self.send_header('Connection', 'close')
                    self.close_connection = True

                self.end_headers()

                for chunk in resp.iter_content(8192):
                    if chunk:
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            logger.debug("Клиент отключился во время передачи данных")
                            return
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
            self._safe_error(502, "Proxy error")
        finally:
            if session:
                session.close()

    # ---------------- Утилиты ----------------

    def _safe_error(self, code, message):
        """Попытаться отправить код ошибки, игнорируя обрыв соединения."""
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(message.encode('utf-8'))
        except Exception:
            pass

    def handle_one_request(self):
        """Переопределяем, чтобы обрыв соединения клиентом не превращался в трейсбек в консоли."""
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception as e:
            logger.debug(f"handle_one_request: {e}")
            self.close_connection = True

    def log_message(self, format, *args):
        # Отключаем стандартный доступ-лог; используем свой logger.info в _forward/_connect
        pass


if __name__ == '__main__':
    port = 8021
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print("Неверный порт")
            sys.exit(1)

    server = ThreadedHTTPServer(('0.0.0.0', port), ProxyHandler)

    print(f"✅ Прокси запущен на 0.0.0.0:{port}")
    print(f"   HTTP  -> через PHP-релей: {PHP_PROXY_URL}")
    print(f"   HTTPS -> прямой CONNECT-туннель (PHP не используется)")
    print("Многопоточный режим (ThreadingMixIn), сервер устойчив к сбоям отдельных запросов")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⏹️ Сервер остановлен пользователем")
        server.server_close()
