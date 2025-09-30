# DeFi Liquidity Manager v2.0

Продвинутая система автоматического управления ликвидностью для Uniswap V3 на Base сети. Специализируется на управлении позициями в пуле cbBTC/USDC с интеллектуальным ребалансированием, интеграцией с PancakeSwap Farm и Telegram управлением.

## 🌟 Основные возможности

- **🤖 Telegram управление** - Полное управление через бота с командами start/stop/rebalance/reset
- **⚡ Автоматическое управление 3 позициями** с тонким диапазоном (0.04% каждая)
- **🔄 Умный ребалансинг** при отклонении цены на 0.1%
- **🍰 Интеграция с CAKE токенами** - автоматический свап CAKE в USDC
- **🚜 PancakeSwap Farms** интеграция для дополнительной доходности
- **📊 Полное P&L отслеживание** с расчетом APY и временных потерь
- **🛡️ Защитные механизмы** от slippage и MEV атак
- **💹 Автоматические свапы** для оптимального соотношения токенов
- **🔥 Массовое закрытие позиций** через multicall оптимизацию

## 📊 Архитектура системы

### Управление позициями
- **3 слота позиций** с диапазоном по 4 тика (0.04%)
- **Общий диапазон:** 0.12% от текущей цены
- **Позиции располагаются встык** без гэпов
- **Автоматическое переставление** при выходе цены за пределы

### Логика ребалансинга
```
Цена в диапазоне → Мониторинг
Отклонение > 0.1% → Частичный ребаланс (1-2 позиции)
Отклонение > 0.19% → Полный ребаланс (все 3 позиции)
```

### Telegram управление
- `/start` - Запуск автоматического управления
- `/stop` - Остановка управления (мониторинг продолжается)
- `/rebalance` - Принудительный полный ребаланс всех позиций
- `/reset` - Полное закрытие всех позиций и сброс состояния
- `/status` - Проверка статуса системы
- `/help` - Список всех команд

## 🚀 Быстрый старт

### 1. Установка зависимостей
```bash
pip install -r requirements.txt
```

### 2. Настройка окружения
Создайте файл `.env`:

```bash
# Blockchain Configuration
RPC_URL=https://base-mainnet.g.alchemy.com/v2/YOUR_API_KEY
WALLET_ADDRESS=0xYourWalletAddress
PRIVATE_KEY=your_private_key_without_0x

# Pool Configuration  
POOL_ADDRESS=0xb94b22332ABf5f89877A14Cc88f2aBC48c34B3Df  # cbBTC/USDC Base
FEE_TIER=100
SWAP_POOL_FEE_TIER_FOR_REBALANCE=100

# Token Addresses (Base Network)
USDC_ADDRESS_BASE=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
CBBTC_ADDRESS_BASE=0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf
CAKE_ADDRESS=0x3055913c90Fcc1A6CE9a358911721eEb942013A1

# DEX Configuration
PANCAKESWAP_ROUTER_ADDRESS=0xFE6508f0015C778Bdcc1fB5465bA5ebE224C9912
NONF_POS_MANAGER_ADDRESS=0x46A15B0b27311cedF172AB29E4f4766fbE7F4364

# Farm Configuration
MASTERCHEF_V3_ADDRESS=0x21F626E5A8cBa47b94D69E75d5d1Faa9C5B04A81

# Telegram Bot (optional)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# ABI Files
NONF_POS_MANAGER_ABI_JSON_PATH=src/abi/PancakeswapV3NonfungiblePositionManager.json

# Google Sheets (optional)
GOOGLE_SHEETS_ID=15yp8anIMPNFFqtD5p-YGlg5u8ewUHeDjluKXrCoGrYo
GOOGLE_SERVICE_ACCOUNT_PATH=service-account.json
# Листы создаются автоматически по адресу кошелька (WALLET_ADDRESS)
```

### 3. Запуск
```bash
# Основная система управления ликвидностью
python src/main.py

# Массовое закрытие всех позиций
python src/close_all.py

# Отправка NFT в CAKE фарминг  
python src/cake_farm.py

# Тестирование Google Sheets Logger
python src/google_sheets_logger.py
```

## 📁 Структура проекта

```
defimanager/
├── src/
│   ├── main.py                 # Основной скрипт с Telegram интеграцией
│   ├── liquidity_manager.py    # Главная логика управления ликвидностью  
│   ├── close_all.py           # Массовое закрытие всех позиций
│   ├── telegram_controller.py  # Telegram бот контроллер
│   ├── data_collector.py       # Сбор данных о пуле и портфеле
│   ├── cake_farm.py           # Интеграция с PancakeSwap Farm
│   ├── google_sheets_logger.py # Логирование в Google Sheets
│   ├── abi/                   # ABI контрактов
│   ├── liquidity_manager_state.json  # Состояние позиций
│   └── data_collector_state.json     # Состояние сборщика данных
├── data/
├── service-account.json      # Google Service Account (не в git)
├── requirements.txt           # Python зависимости
├── GOOGLE_SHEETS_SETUP.md    # Настройка Google Sheets
├── FIXED_ISSUES.md           # История исправленных проблем
└── README.md
```

## 💰 P&L и аналитика

### Автоматическое отслеживание
Система ведет детальную аналитику всех операций:

- **📈 Чистый P&L** в USDC для каждой позиции
- **📊 Процентная доходность** и годовая APY
- **⏰ Временные потери** (Impermanent Loss)
- **💵 Заработанные комиссии** от трейдеров
- **🔄 Сравнение с HODL** стратегией

## ⚙️ Ключевые компоненты

### 1. LiquidityManager (`src/liquidity_manager.py`)
Основной класс для управления ликвидностью:
- `decide_and_manage_liquidity()` - Главная логика принятия решений
- `analyze_rebalance_with_price()` - Анализ необходимости ребаланса
- `_perform_full_rebalance()` - Полное переставление всех позиций
- `_perform_partial_rebalance()` - Частичное переставление 1-2 позиций
- `_execute_add_liquidity_fast()` - Быстрое создание позиций с свапами

### 2. PositionCloser (`src/close_all.py`)
Специализированный класс для массового закрытия:
- `close_all_positions_multicall()` - Закрытие всех позиций одной транзакцией
- `swap_cake_to_usdc()` - Автоматический свап CAKE токенов
- `rebalance_portfolio_1_to_1()` - Балансировка портфеля 50/50
- Поддержка позиций как на кошельке, так и в фарминге

### 3. TelegramController (`src/telegram_controller.py`)
Telegram бот для удаленного управления:
- Безопасность через проверку Chat ID
- Команды старт/стоп управления
- Принудительный ребаланс и полный сброс
- Мониторинг статуса системы

### 4. CakeFarm (`src/cake_farm.py`)
Интеграция с PancakeSwap Farm:
- Отправка NFT позиций в фарминг
- Получение дополнительных CAKE rewards
- Автоматизация через `safeTransferFrom`

## 🛡️ Защитные механизмы

- **🎯 Slippage контроль:** Адаптивные настройки до 0.5% для волатильных активов
- **⛽ Gas оптимизация:** Умное управление ценой газа +5%
- **🔄 Multicall батчинг:** Минимизация транзакций через объединение
- **🏦 Balance проверки:** Контроль достаточности средств
- **🔐 Private ключи:** Безопасное хранение в `.env`
- **✅ Checksum адреса:** Предотвращение ошибок в адресах

## 🚀 Продвинутые функции

### Умный ребалансинг
```python
strategy_params = {
    'num_positions': 3,                                    # Количество позиций
    'individual_position_width_pct': Decimal('0.0004'),   # 0.04% на позицию  
    'total_range_width_pct': Decimal('0.0012'),           # 0.12% общий диапазон
    'rebalance_threshold_pct': Decimal('0.001'),          # 0.1% порог ребаланса
}
```

### Автоматические свапы
- **CAKE → USDC:** Автоматический свап наград перед ребалансировкой
- **Portfolio balancing:** Поддержание соотношения 50/50 USDC/cbBTC
- **Universal Router:** Использование эффективного роутера PancakeSwap

### Мониторинг в реальном времени
- Непрерывный мониторинг цены cbBTC
- Анализ позиций каждые 10 секунд
- Автоматические снимки портфеля
- Telegram уведомления о важных событиях

## 🐛 Устранение неполадок

### Частые проблемы
```bash
# Проверка подключения
curl -X POST $RPC_URL -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'

# Проверка балансов
python -c "from src.liquidity_manager import *; print('Check balances')"

# Очистка состояния (осторожно!)
rm src/liquidity_manager_state.json src/data_collector_state.json
```

### Логи для отладки
```bash
# Основная активность
tail -f liquidity_manager.log

# P&L анализ

```

## ⚠️ Важные замечания

1. **🧪 Тестирование:** Система протестирована на Base mainnet с реальными средствами
2. **⚠️ Риски:** DeFi протоколы несут риски импермонентных потерь и smart contract багов  
3. **👀 Мониторинг:** Рекомендуется постоянный мониторинг через Telegram
4. **💸 Газ:** Учитывайте расходы на gas при частых ребалансах
5. **🔄 Multicall:** Используйте `close_all.py` для экономии газа при массовых операциях

## 📞 Поддержка

При возникновении проблем проверьте:
1. ✅ Подключение к Base RPC
2. ⛽ Баланс ETH для gas fees  
3. 💰 Баланс USDC/cbBTC токенов
4. 🔗 Правильность адресов контрактов
5. 🤖 Настройку Telegram бота (опционально)

## 📄 Лицензия

MIT License - используйте на свой страх и риск.

---

**⚠️ DISCLAIMER:** Данное ПО предназначено для опытных DeFi пользователей. Автор не несет ответственности за финансовые потери. Всегда проводите собственные исследования и используйте на свой страх и риск! 