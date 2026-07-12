PHP_PROXY_SERVERS = ["https://max.ru.ydns.eu/prx.php"]
LISTEN_PORT = 8021
PING_TIMEOUT = 5
REQUEST_TIMEOUT = (10, 60)
CHUNK_SIZE = 8192
MAX_RETRIES = 2
BACKOFF_FACTOR = 0.5
RETRY_STATUS_CODES = [500, 502, 503, 504]
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(threadName)s] %(levelname)s: %(message)s"
# Логирование запросов
LOG_LEVEL = 'INFO'          # DEBUG, INFO, WARNING, ERROR
LOG_FORMAT = '%(asctime)s [%(levelname)s] %(message)s'
LOG_FILE = 'proxy.log'   # или None для вывода в stderr
