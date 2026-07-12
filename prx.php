<?php
/**
 * PHP Proxy (Full Buffering, No Streaming)
 */

// Отключаем ограничения времени и памяти (опционально)
ini_set('max_execution_time', 300);
ini_set('memory_limit', '512M');

// Включаем буферизацию вывода (можно оставить как есть)
// (но если включена, мы будем использовать ob_* для управления)
// Отключаем сжатие, чтобы не мешать
ini_set('zlib.output_compression', 'Off');
if (function_exists('apache_setenv')) {
    apache_setenv('no-gzip', '1');
}

// Закрываем сессию, если открыта
if (session_status() === PHP_SESSION_ACTIVE) {
    session_write_close();
}

// Отключаем вывод ошибок
ini_set('display_errors', '0');
error_reporting(0);

// Очищаем все буферы вывода, чтобы начать с чистой страницы
while (ob_get_level()) {
    ob_end_clean();
}

define('MAX_REDIRECTS', 5);
define('CONNECT_TIMEOUT', 15);
define('TIMEOUT', 60);
define('USER_AGENT', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36');

function runProxy() {
    // Получаем целевой URL
    $target = $_REQUEST['url'] ?? $_SERVER['HTTP_X_TARGET_URL'] ?? null;

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

    // Выполняем запрос с обработкой редиректов
    $result = fetchUrlWithRedirects($target, 0);

    if ($result === false) {
        // Ошибка уже отправлена внутри
        return;
    }

    // Отправляем ответ клиенту
    $httpCode = $result['http_code'];
    $headers = $result['headers'];
    $body = $result['body'];

    http_response_code($httpCode);
    foreach ($headers as $header) {
        if (empty($header)) continue;
        if (preg_match('/^HTTP\//i', $header)) continue;

            $parts = explode(':', $header, 2);
        if (count($parts) == 2) {
            $name = trim($parts[0]);
            $value = trim($parts[1]);
            $nameLow = strtolower($name);
            // Пропускаем заголовки, которые могут мешать
            if (in_array($nameLow, ['transfer-encoding', 'connection', 'keep-alive', 'proxy-connection'])) {
                continue;
            }
            header("$name: $value", false);
        }
    }

    // Выводим тело
    echo $body;
}

function fetchUrlWithRedirects($url, $redirectCount) {
    if ($redirectCount > MAX_REDIRECTS) {
        sendError(508, 'Too many redirects');
        return false;
    }

    $method = $_SERVER['REQUEST_METHOD'];
    $headers = getClientHeaders();
    $body = file_get_contents('php://input');

    // Выполняем запрос и получаем результат
    $result = executeCurlRequest($url, $method, $headers, $body);

    if ($result === false) {
        return false; // Ошибка уже обработана
    }

    // Проверяем, является ли ответ редиректом
    $httpCode = $result['http_code'];
    if (in_array($httpCode, [301, 302, 303, 307, 308])) {
        // Ищем Location
        $location = null;
        foreach ($result['headers'] as $header) {
            if (preg_match('/^Location:\s*(.*)$/i', $header, $matches)) {
                $location = trim($matches[1]);
                break;
            }
        }

        if ($location) {
            $newUrl = resolveUrl($location, $url);
            return fetchUrlWithRedirects($newUrl, $redirectCount + 1);
        } else {
            // Редирект без Location – ошибка
            sendError(502, 'Redirect without Location header');
            return false;
        }
    }

    return $result;
}

function executeCurlRequest($url, $method, $headers, $body) {
    $ch = curl_init();

    // Подготовка заголовков для cURL
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

    // Переменные для сбора заголовков и тела
    $responseHeaders = [];
    $responseBody = '';

    $options = [
        CURLOPT_URL            => $url,
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_HTTPHEADER     => $curlHeaders,
        CURLOPT_RETURNTRANSFER => true,          // Включаем буферизацию
        CURLOPT_HEADERFUNCTION => function($ch, $header) use (&$responseHeaders) {
            $responseHeaders[] = trim($header);
            return strlen($header);
        },
        CURLOPT_WRITEFUNCTION  => function($ch, $data) use (&$responseBody) {
            $responseBody .= $data;
            return strlen($data);
        },
        CURLOPT_FOLLOWLOCATION => false,         // Редиректы обрабатываем вручную
        CURLOPT_CONNECTTIMEOUT => CONNECT_TIMEOUT,
        CURLOPT_TIMEOUT        => TIMEOUT,
        CURLOPT_SSL_VERIFYPEER => false,
        CURLOPT_SSL_VERIFYHOST => false,
    ];

    if ($body && in_array($method, ['POST', 'PUT', 'PATCH', 'DELETE'])) {
        $options[CURLOPT_POSTFIELDS] = $body;
    }

    curl_setopt_array($ch, $options);

    $result = curl_exec($ch);
    $info = curl_getinfo($ch);
    $error = curl_error($ch);
    $httpCode = $info['http_code'] ?? 0;

    curl_close($ch);

    if ($result === false || $error) {
        sendError(502, "Proxy Error: " . ($error ?: 'Unknown cURL error'));
        return false;
    }

    // Если ответ пустой (например, HEAD-запрос), тело может быть пустым
    return [
        'http_code' => $httpCode,
        'headers'   => $responseHeaders,
        'body'      => $responseBody
    ];
}

function sendError($code, $message) {
    if (headers_sent()) {
        // Если заголовки уже отправлены, просто завершаем
        exit;
    }
    if (ob_get_level()) {
        ob_clean();
    }
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
        // Разрешаем публичные IP
        if (filter_var($host, FILTER_VALIDATE_IP, FILTER_FLAG_NO_PRIV_RANGE)) {
            return true;
        } else {
            return false;
        }
    }

    return true;
}

function normalizeUrl($url) {
    $url = trim($url);
    if (empty($url)) return false;

    if (!preg_match('~^(?:f|ht)tps?://~i', $url)) {
        $url = 'http://' . $url;
    }

    $parsed = parse_url($url);
    if (!$parsed || !isset($parsed['host'])) {
        return false;
    }

    $scheme = $parsed['scheme'];
    $host = $parsed['host'];
    $port = isset($parsed['port']) ? ':' . $parsed['port'] : '';

    $path = isset($parsed['path']) ? $parsed['path'] : '/';
    $path = preg_replace_callback('/[^A-Za-z0-9_\-\.~!$&\'()*+,;=:@\/]+/', function($matches) {
        return rawurlencode($matches[0]);
    }, $path);

    $query = isset($parsed['query']) ? '?' . $parsed['query'] : '';

    return "$scheme://$host$port$path$query";
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
