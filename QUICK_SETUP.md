# 🚀 Быстрая настройка DeFi Liquidity Manager для BNB Chain

## Шаг 1: Установка зависимостей

```bash
cd /root/defi_bnb
pip install -r requirements.txt
```

## Шаг 2: Настройка .env файла

Скопируйте пример конфигурации:
```bash
cp .env.example .env
nano .env
```

Заполните **обязательные** параметры:

### 2.1. Blockchain Configuration
```bash
# URL для подключения к BNB Chain (можно получить на Alchemy/QuickNode)
RPC_URL=https://bnb-mainnet.g.alchemy.com/v2/YOUR_API_KEY

# Адрес вашего кошелька
WALLET_ADDRESS=0xYourWalletAddress

# Приватный ключ (БЕЗ префикса 0x!)
PRIVATE_KEY=your_private_key_without_0x_prefix
```

### 2.2. Telegram Bot (обязательно для управления)
```bash
# Получите токен у @BotFather в Telegram
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz

# Ваш Chat ID (получите у @userinfobot)
TELEGRAM_CHAT_ID=123456789
```

### 2.3. Остальные параметры (уже настроены)
Токены, контракты, пулы - **уже правильно настроены** в `.env.example`

## Шаг 3: Настройка Google Sheets (опционально)

Если хотите логировать данные в Google Sheets:

### 3.1. Создайте Service Account
1. Перейдите в [Google Cloud Console](https://console.cloud.google.com/)
2. Создайте новый проект или выберите существующий
3. Включите **Google Sheets API**:
   - APIs & Services → Library → Google Sheets API → Enable
4. Создайте Service Account:
   - APIs & Services → Credentials → Create Credentials → Service Account
   - Назовите его (например, `defi-logger`)
   - Скачайте JSON ключ

### 3.2. Настройте доступ
```bash
# Скопируйте скачанный JSON файл
cp ~/Downloads/your-project-xxxxx.json /root/defi_bnb/service_account.json
```

В `.env` укажите:
```bash
GOOGLE_SHEETS_ID="YOUR_SPREADSHEET_ID"
GOOGLE_SERVICE_ACCOUNT_PATH="service_account.json"
```

### 3.3. Предоставьте доступ к таблице
1. Откройте вашу Google Sheets таблицу
2. Нажмите **Share** (Поделиться)
3. Добавьте email из `service_account.json` (поле `client_email`)
4. Дайте права **Editor** (Редактор)

## Шаг 4: Проверка балансов

Убедитесь что на кошельке есть:
- **BNB** для gas fees (~0.01 BNB минимум, рекомендуется 0.1 BNB)
- **USDT** и **BTCB** для создания позиций ликвидности

```bash
# Проверка подключения
curl -X POST $RPC_URL -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_chainId","params":[],"id":1}'

# Должно вернуть: {"jsonrpc":"2.0","id":1,"result":"0x38"}  (0x38 = 56 decimal = BNB Chain)
```

## Шаг 5: Первый запуск

### 5.1. Тестовый запуск
```bash
cd /root/defi_bnb
python src/main.py
```

Скрипт должен:
- ✅ Подключиться к BNB Chain
- ✅ Загрузить состояние позиций
- ✅ Получить текущую цену BTCB/USDT (~$113,000)
- ✅ Запустить Telegram бота

### 5.2. Проверка Telegram бота
В Telegram отправьте боту:
```
/start
```

Бот должен ответить приветствием и списком команд.

## Шаг 6: Создание позиций

Если позиций еще нет, скрипт автоматически создаст 3 позиции:
- **Позиция 0:** Ниже текущей цены
- **Позиция 1:** Центральная позиция
- **Позиция 2:** Выше текущей цены

Каждая позиция имеет ширину **4 тика (~0.04%)** и располагается встык.

## Шаг 7: Управление через Telegram

### Основные команды:
```
/start     - Запустить автоматическое управление
/stop      - Остановить управление (мониторинг продолжается)
/rebalance - Принудительный ребаланс всех позиций
/reset     - Закрыть все позиции и сбросить состояние
/status    - Проверить статус системы
/help      - Список всех команд
```

## Шаг 8: Настройка расписания (опционально)

Отредактируйте `src/schedule.json` для настройки рабочих часов:

```json
{
  "liquidityScheduleUTC": {
    "Monday": [
      {
        "startUTC": "09:00",
        "endUTC": "18:00"
      }
    ],
    "Tuesday": [
      {
        "startUTC": "09:00",
        "endUTC": "18:00"
      }
    ]
    // ... остальные дни
  }
}
```

Подробнее: [SCHEDULE_README.md](SCHEDULE_README.md)

## Шаг 9: Запуск в фоновом режиме

### Используя screen:
```bash
# Создать новую screen сессию
screen -S defi_manager

# Запустить скрипт
cd /root/defi_bnb
python src/main.py

# Отключиться от screen: Ctrl+A, затем D
# Подключиться обратно:
screen -r defi_manager
```

### Используя tmux:
```bash
# Создать новую tmux сессию
tmux new -s defi_manager

# Запустить скрипт
cd /root/defi_bnb
python src/main.py

# Отключиться от tmux: Ctrl+B, затем D
# Подключиться обратно:
tmux attach -t defi_manager
```

### Используя systemd service (рекомендуется):
```bash
# Создать service файл
sudo nano /etc/systemd/system/defi_manager.service
```

Содержимое:
```ini
[Unit]
Description=DeFi Liquidity Manager
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/defi_bnb
ExecStart=/usr/bin/python3 /root/defi_bnb/src/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Активация:
```bash
sudo systemctl daemon-reload
sudo systemctl enable defi_manager
sudo systemctl start defi_manager

# Проверка статуса
sudo systemctl status defi_manager

# Просмотр логов
sudo journalctl -u defi_manager -f
```

## Шаг 10: Дополнительные скрипты

### Массовое закрытие позиций:
```bash
python src/close_all.py
```

Используйте когда нужно:
- Закрыть все позиции одновременно
- Собрать все rewards
- Свапнуть CAKE в USDT

### Отправка в CAKE фарминг:
```bash
python src/cake_farm.py
```

Отправляет NFT позиции в MasterChef V3 для получения дополнительных CAKE rewards.

## 🎉 Готово!

Система настроена и работает. Вы можете:
- 📱 Управлять через Telegram
- 📊 Мониторить логи в консоли или Google Sheets
- 🔄 Автоматический ребалансинг будет происходить при необходимости

## 🆘 Помощь при проблемах

### Проблема: "Connection refused"
```bash
# Проверьте RPC URL
curl -X POST $RPC_URL -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
```

### Проблема: "Transaction underpriced"
```
Решение: Скрипт уже настроен на минимум 0.1 Gwei priority fee
```

### Проблема: "Insufficient funds for gas"
```bash
# Пополните баланс BNB
# Рекомендуется иметь минимум 0.1 BNB
```

### Проблема: Telegram бот не отвечает
```bash
# Проверьте токен:
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"

# Проверьте Chat ID:
# Отправьте любое сообщение боту, затем:
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates"
```

### Проблема: Google Sheets не работает
```bash
# Проверьте что API включен
# Проверьте что service account имеет доступ к таблице
# Проверьте путь к файлу:
ls -la service_account.json
```

## 📚 Дополнительная информация

- [README.md](README.md) - Полная документация
- [GOOGLE_SHEETS_SETUP.md](GOOGLE_SHEETS_SETUP.md) - Детальная настройка Google Sheets
- [SCHEDULE_README.md](SCHEDULE_README.md) - Настройка расписания

---

**💡 Совет:** Начните с небольших сумм чтобы протестировать систему!
