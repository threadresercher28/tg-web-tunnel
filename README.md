# 🌐 TG Web Tunnel

Превращает обычный PHP-сайт в ```HTTP-прокси``` для Telegram Desktop .

[![Python](https://img.shields.io/badge/Python-3.6+-blue.svg)](https://python.org)
[![PHP](https://img.shields.io/badge/PHP-7.0+-purple.svg)](https://php.net)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## ✨ Возможности

- 🔄 Работа через обычный PHP-хостинг с cURL
- 🚀 Поддержка нескольких прокси-серверов с резервированием
- 🔁 Автоматические повторные попытки при ошибках
- 📦 Чанковая передача данных для оптимизации
- 🛡️ Маскировка трафика под обычные веб-запросы
- 📊 Подробное логирование
- 🔌 Простое подключение к Telegram Desktop

## 🔧 Как это работает

1. **Клиентская часть (Python)**:
   - Создаёт локальный HTTP-прокси сервер
   - Принимает запросы от Telegram Desktop
   - Перенаправляет их на PHP-скрипт через обычные HTTP-запросы
   - Разбивает большие ответы на чанки для стабильности

2. **Серверная часть (PHP)**:
   - Принимает зашифрованные MTProto запросы
   - Через cURL отправляет их к реальным серверам Telegram
   - Возвращает ответ обратно Python-прокси

3. **Telegram Desktop**:
   - Подключается к локальному прокси как к обычному HTTP-прокси
   - Весь трафик прозрачно проходит через цепочку

## 📦 Требования

### Клиент (ваш компьютер)
- Python 3.6 или выше
- pip (менеджер пакетов Python)
- Модули: `requests`, `urllib3`

### Сервер (хостинг)
- PHP 7.0 или выше
- PHP расширение cURL (`php-curl`)
- Разрешённые внешние запросы (`allow_url_fopen = On`)
- Любой хостинг с поддержкой HTTPS

## 🚀 Установка

### 1. Клиентская часть

```bash
git clone https://github.com/yourusername/tg-web-tunnel.git
cd tg-web-tunnel
pip install -r requirements.txt
python3 tghelp.py
```

## Настройка telegram
Telegram Desktop версия 4.0+<br>
```Настройки → Дополнительно → Тип соединения → Добавить прокси → HTTP```:
<br> Хост: ```127.0.0.1```<br>
Порт: ```(из config.py)```

Telegram Desktop версия 3.x и старше<br> 
```Настройки → Продвинутые настройки → Тип соединения → Добавить прокси → HTTP```<br>
Хост: ```127.0.0.1```<br>
Порт: ```(из config.py)```
