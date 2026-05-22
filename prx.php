<?php
/**
 * Streaming PHP Proxy (No Buffering)
 */

// 1. Отключаем все, что мешает стримингу
ini_set('max_execution_time', 300);
ini_set('memory_limit', '512M');
ini_set('zlib.output_compression', 'Off');
ini_set('output_buffering', 'Off'); // ВАЖНО: Отключаем буферизацию
if (function_exists('apache_setenv')) {
    apache_setenv('no-gzip', '1');
}

if (session_status() === PHP_SESSION_ACTIVE) {
    session_write_close();
}

ini_set('display_errors', '0');
error_reporting(0);

// Очищаем любые остатки буфера
while (ob_get_level()) {
    ob_end_clean();
}

define('MAX_REDIRECTS', 5);
define('CONNECT_TIMEOUT', 15);
define('TIMEOUT', 60);
define('USER_AGENT', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36');

function runProxy() {
    // Надежное получение URL
    $target = $_REQUEST['url'] ?? null;

    if (!$target) {
        // Попытка взять из заголовка, если GET пуст
        $target = $_SERVER['HTTP_X_TARGET_URL'] ?? null;
    }

    if (!$target) {
        sendError(400, 'Missing target URL. Usage: ?url=http://example.com');
        return;
    }

    // Нормализация URL
    $target = normalizeUrl($target);

    if (!$target) {
        sendError(400, 'Invalid URL format');
        return;
    }

    // Проверка безопасности
    if (!isUrlSafe($target)) {
        sendError(403, 'Access to local/private addresses is forbidden');
        return;
    }

    followRedirects($target, 0);
}

function normalizeUrl($url) {
    $url = trim($url);
    if (empty($url)) return false;

    // Если нет схемы, добавляем http
    if (!preg_match('~^(?:f|ht)tps?://~i', $url)) {
        $url = 'http://' . $url;
    }

    $parsed = parse_url($url);
    if (!$parsed || !isset($parsed['host'])) {
        return false;
    }

    // Собираем URL заново, корректно кодируя путь
    $scheme = $parsed['scheme'];
    $host = $parsed['host'];
    $port = isset($parsed['port']) ? ':' . $parsed['port'] : '';

    $path = isset($parsed['path']) ? $parsed['path'] : '/';
    // Кодируем спецсимволы в пути, но оставляем слеши
    $path = preg_replace_callback('/[^A-Za-z0-9_\-\.~!$&\'()*+,;=:@\/]+/', function($matches) {
        return rawurlencode($matches[0]);
    }, $path);

    $query = isset($parsed['query']) ? '?' . $parsed['query'] : '';

    return "$scheme://$host$port$path$query";
}

function followRedirects($url, $redirectCount) {
    if ($redirectCount > MAX_REDIRECTS) {
        sendError(508, 'Too many redirects');
        return;
    }

    $method = $_SERVER['REQUEST_METHOD'];
    $headers = getClientHeaders();
    $body = file_get_contents('php://input');

    // executeCurlRequest вернет true, если нужно делать редирект (и сам очистит вывод)
    // или false, если запрос выполнен и данные уже отправлены клиенту (стриминг)
    $shouldRedirect = executeCurlRequest($url, $method, $headers, $body, $redirectCount);

    if ($shouldRedirect) {
        // Логика редиректа обрабатывается внутри executeCurlRequest через возвращаемый URL
        // Но так как мы хотим стриминг, мы не можем вернуть URL просто так.
        // Поэтому executeCurlRequest сам вызовет followRedirects рекурсивно, если нужно.
    }
}

function executeCurlRequest($url, $method, $headers, $body, $redirectCount) {
    $ch = curl_init();

    $curlHeaders = [];
    foreach ($headers as $name => $value) {
        $n = strtolower($name);
        if (!in_array($n, ['host', 'connection', 'content-length', 'expect', 'transfer-encoding'])) {
            $curlHeaders[] = "$name: $value";
        }
    }

    $parsed = parse_url($url);
    $host = $parsed['host'] ?? '';
    if (isset($parsed['port']) && !(($parsed['scheme'] === 'http' && $parsed['port'] == 80) || ($parsed['scheme'] === 'https' && $parsed['port'] == 443))) {
        $host .= ':' . $parsed['port'];
    }

    $curlHeaders[] = "Host: $host";
    $curlHeaders[] = "User-Agent: " . USER_AGENT;
    $curlHeaders[] = "Accept-Encoding: gzip, deflate";

    $options = [
        CURLOPT_URL            => $url,
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_HTTPHEADER     => $curlHeaders,
        CURLOPT_RETURNTRANSFER => false, // СТРИМИНГ: не копим в память
        CURLOPT_HEADER         => false, // Заголовки ловим через callback
        CURLOPT_FOLLOWLOCATION => false, // Редиректы вручную
        CURLOPT_CONNECTTIMEOUT => CONNECT_TIMEOUT,
        CURLOPT_TIMEOUT        => TIMEOUT,
        CURLOPT_SSL_VERIFYPEER => false,
        CURLOPT_SSL_VERIFYHOST => false,
    ];

    if ($body && in_array($method, ['POST', 'PUT', 'PATCH', 'DELETE'])) {
        $options[CURLOPT_POSTFIELDS] = $body;
    }

    // Переменные для состояния
    $headerLines = [];
    $httpCode = 0;
    $isRedirect = false;
    $locationUrl = '';
    $headersSentToClient = false;

    // Callback для заголовков
    $headerFunc = function($ch, $data) use (&$headerLines, &$httpCode, &$isRedirect, &$locationUrl, &$headersSentToClient, $redirectCount) {
        $line = trim($data);

        // Парсим статусную строку
        if (preg_match('/^HTTP\/[\d.]+\s+(\d+)/', $line, $m)) {
            $httpCode = (int)$m[1];
            if (in_array($httpCode, [301, 302, 303, 307, 308])) {
                $isRedirect = true;
            } else {
                $isRedirect = false;
            }
        }

        // Накопление заголовков до конца блока
        if ($line === '') {
            // Конец заголовков

            if ($isRedirect) {
                // Ищем Location
                foreach ($headerLines as $h) {
                    if (preg_match('/^Location:\s*(.*)$/i', $h, $loc)) {
                        $locationUrl = trim($loc[1]);
                        break;
                    }
                }

                // Если это редирект, мы НЕ отправляем заголовки клиенту.
                // Мы возвращаем управление, чтобы сделать новый запрос.
                // Но cURL продолжит вызываться. Нам нужно прервать выполнение?
                // Нет, cURL сам завершит запрос. Мы просто проигнорируем тело.
                return strlen($data);
            }

            // Это НЕ редирект. Отправляем заголовки клиенту.
            if (!$headersSentToClient) {
                http_response_code($httpCode);
                foreach ($headerLines as $h) {
                    if (empty($h)) continue;
                    if (preg_match('/^HTTP\//i', $h)) continue;

                        $parts = explode(':', $h, 2);
                    if (count($parts) == 2) {
                        $n = trim($parts[0]);
                        $v = trim($parts[1]);
                        $nLow = strtolower($n);
                        if (in_array($nLow, ['transfer-encoding', 'connection', 'keep-alive', 'proxy-connection'])) continue;
                        header("$n: $v", false);
                    }
                }
                // Важно: flush заголовков
                if (ob_get_level()) ob_flush();
                flush();
                $headersSentToClient = true;
            }

            $headerLines = []; // Сброс
            return strlen($data);
        }

        $headerLines[] = $data;
        return strlen($data);
    };

    // Callback для тела (СТРИМИНГ)
    $writeFunc = function($ch, $data) use ($isRedirect, $headersSentToClient) {
        // Если это редирект, игнорируем тело
        if ($isRedirect) return strlen($data);

        // Если заголовки еще не ушли (странный случай), пробуем отправить
        if (!$headersSentToClient) {
            // В норме headerFunc уже отправил их.
            // Если нет, значит ошибка протокола, но попробуем вывести тело
        }

        echo $data;
        if (ob_get_level()) ob_flush();
        flush();
        return strlen($data);
    };

    $options[CURLOPT_HEADERFUNCTION] = $headerFunc;
    $options[CURLOPT_WRITEFUNCTION] = $writeFunc;

    curl_setopt_array($ch, $options);

    curl_exec($ch);

    $info = curl_getinfo($ch);
    $error = curl_error($ch);
    $finalCode = $info['http_code'];

    curl_close($ch);

    // Обработка результатов
    if ($error) {
        if (!$headersSentToClient) {
            sendError(502, "Proxy Error: $error");
        }
        return;
    }

    // Если это был редирект, запускаем рекурсию
    if ($isRedirect && $locationUrl) {
        // Очищаем любой возможный мусор в буфере (хотя при стриминге его быть не должно)
        if (ob_get_level()) ob_clean();

        $newUrl = resolveUrl($locationUrl, $url);
        followRedirects($newUrl, $redirectCount + 1);
    }
}

function sendError($code, $message) {
    // При стриминге важно убедиться, что заголовки еще не ушли
    if (headers_sent()) {
        // Если ушли, ничего не поделаешь, просто обрываем
        return;
    }

    if (ob_get_level()) ob_clean();

    http_response_code($code);
    header('Content-Type: text/plain; charset=utf-8');
    exit($message);
}

function isUrlSafe($url) {
    $parsed = parse_url($url);
    if (!isset($parsed['host'])) return false;

    $host = strtolower($parsed['host']);

    if ($host === 'localhost' || $host === '127.0.0.1' || $host === '::1' || $host === '0.0.0.0') {
        return false;
    }

    if (filter_var($host, FILTER_VALIDATE_IP)) {
        // Разрешаем публичные IP (Telegram и др.)
        if (filter_var($host, FILTER_VALIDATE_IP, FILTER_FLAG_NO_PRIV_RANGE)) {
            return true;
        } else {
            return false;
        }
    }

    return true;
}

function resolveUrl($rel, $base) {
    $parsedBase = parse_url($base);
    $scheme = $parsedBase['scheme'];
    $host = $parsedBase['host'];
    $port = isset($parsedBase['port']) ? ':' . $parsedBase['port'] : '';

    if (strpos($rel, '//') === 0) {
        return $scheme . ':' . $rel;
    }
    if (strpos($rel, '/') === 0) {
        return $scheme . '://' . $host . $port . $rel;
    }

    $path = dirname($parsedBase['path'] ?? '/');
    if ($path === '\\') $path = '/';

    return $scheme . '://' . $host . $port . $path . '/' . $rel;
}

function getClientHeaders() {
    $headers = [];
    if (function_exists('getallheaders')) {
        $headers = getallheaders();
    } else {
        foreach ($_SERVER as $name => $value) {
            if (substr($name, 0, 5) == 'HTTP_') {
                $name = str_replace(' ', '-', ucwords(strtolower(str_replace('_', ' ', substr($name, 5)))));
                $headers[$name] = $value;
            }
        }
    }
    return $headers;
}

// Запуск
runProxy();
?>
