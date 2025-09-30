# src/google_sheets_logger.py
"""
Модуль для логирования данных о рабочих периодах в Google Sheets.
Использует сервисный аккаунт для аутентификации и web3.py для получения данных из блокчейна.
"""

import os
import json
import gspread
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from web3 import Web3
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# Загружаем переменные окружения
load_dotenv()


class SheetLogger:
    """
    Класс для логирования данных о рабочих периодах в Google Sheets.
    
    Авторизуется через сервисный аккаунт и записывает данные о:
    - Времени начала и окончания рабочего периода
    - Цене BTC на момент начала и окончания
    - Общем балансе активов (cbbtc + usdc + cake)
    - Продолжительности рабочего периода в часах
    """
    
    def __init__(self, spreadsheet_id: str, wallet_address: str, service_account_path: str = "service-account.json"):
        """
        Инициализация логгера Google Sheets.
        
        Args:
            spreadsheet_id: ID Google таблицы
            wallet_address: Адрес кошелька (будет использован как название листа)
            service_account_path: Путь к файлу сервисного аккаунта
        """
        self.spreadsheet_id = spreadsheet_id
        self.wallet_address = wallet_address
        self.worksheet_name = wallet_address  # Используем адрес кошелька как название листа
        self.worksheet = None
        
        # Инициализация Web3 для работы с блокчейном
        self.w3 = Web3(Web3.HTTPProvider(os.getenv("RPC_URL")))
        if not self.w3.is_connected():
            raise ConnectionError("Не удалось подключиться к RPC узлу")
        
        # Адреса токенов из .env для BNB Chain
        self.usdt_address = os.getenv("TOKEN_1_ADDRESS")  # USDT на BNB Chain
        self.btcb_address = os.getenv("TOKEN_2_ADDRESS")  # BTCB на BNB Chain 
        self.cake_address = os.getenv("CAKE_ADDRESS")
        self.pool_address = os.getenv("POOL_ADDRESS")
        self.wallet_address = os.getenv("WALLET_ADDRESS")
        
        # ABI для ERC-20 токенов (базовый интерфейс)
        self.erc20_abi = [
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function"
            },
            {
                "constant": True,
                "inputs": [],
                "name": "decimals",
                "outputs": [{"name": "", "type": "uint8"}],
                "type": "function"
            }
        ]
        
        # Загружаем ABI для пула из файла (как в liquidity_manager.py)
        try:
            pool_abi_path = os.getenv("POOL_ABI_FILENAME", "PancakeswapV3Pool.json")
            project_root = Path(__file__).resolve().parent.parent
            pool_abi_full_path = str(project_root / 'src' / 'abi' / pool_abi_path)
            
            with open(pool_abi_full_path, 'r') as f:
                self.pool_abi = json.load(f)
        except Exception as e:
            print(f"⚠️ Не удалось загрузить ABI пула, используем упрощенный: {e}")
            # Fallback ABI только с функцией slot0
            self.pool_abi = [
                {
                    "inputs": [],
                    "name": "slot0",
                    "outputs": [
                        {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
                        {"internalType": "int24", "name": "tick", "type": "int24"},
                        {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
                        {"internalType": "uint16", "name": "observationCardinality", "type": "uint16"},
                        {"internalType": "uint16", "name": "observationCardinalityNext", "type": "uint16"},
                        {"internalType": "uint8", "name": "feeProtocol", "type": "uint8"},
                        {"internalType": "bool", "name": "unlocked", "type": "bool"}
                    ],
                    "stateMutability": "view",
                    "type": "function"
                }
            ]
        
        # Инициализация Google Sheets
        self._init_google_sheets(service_account_path)
    
    def _init_google_sheets(self, service_account_path: str):
        """
        Инициализация подключения к Google Sheets через сервисный аккаунт.
        
        Args:
            service_account_path: Путь к файлу сервисного аккаунта
        """
        try:
            # Определяем область доступа для Google Sheets API
            scope = [
                'https://spreadsheets.google.com/feeds',
                'https://www.googleapis.com/auth/drive'
            ]
            
            # Загружаем учетные данные сервисного аккаунта
            creds = Credentials.from_service_account_file(service_account_path, scopes=scope)
            
            # Создаем клиент gspread
            self.gc = gspread.authorize(creds)
            
            # Открываем таблицу по ID
            self.spreadsheet = self.gc.open_by_key(self.spreadsheet_id)
            
            # Проверяем существование листа с адресом кошелька
            try:
                self.worksheet = self.spreadsheet.worksheet(self.worksheet_name)
                print(f"✅ Найден существующий лист: {self.worksheet_name}")
            except gspread.WorksheetNotFound:
                # Создаем новый лист с адресом кошелька
                print(f"📝 Создаем новый лист: {self.worksheet_name}")
                self.worksheet = self.spreadsheet.add_worksheet(
                    title=self.worksheet_name, 
                    rows=1000, 
                    cols=10
                )
                
                # Добавляем заголовки столбцов
                self._setup_worksheet_headers()
                print(f"✅ Создан новый лист с заголовками: {self.worksheet_name}")
            
            print(f"✅ Успешно подключились к Google Sheets: {self.spreadsheet_id} -> {self.worksheet_name}")
            
        except Exception as e:
            raise ConnectionError(f"Ошибка подключения к Google Sheets: {e}")
    
    def _setup_worksheet_headers(self):
        """
        Настраивает заголовки столбцов в новом листе.
        """
        try:
            # Заголовки столбцов
            headers = [
                "Старт",           # A - Время начала
                "Финиш",           # B - Время окончания  
                "Часов",           # C - Продолжительность в часах
                "BTC, старт",      # D - Цена BTC на начало
                "BTC, финиш",      # E - Цена BTC на конец
                "Сумма старт",     # F - Общий баланс на начало
                "Сумма финиш",     # G - Общий баланс на конец
                "Прибыль/Убыток",  # H - Изменение баланса
                "Изменение BTC %", # I - Изменение цены BTC в %
                "Комментарий"      # J - Дополнительная информация
            ]
            
            # Записываем заголовки в первую строку
            self.worksheet.update('A1:J1', [headers])
            
            # Форматируем заголовки (жирный шрифт)
            self.worksheet.format('A1:J1', {
                'textFormat': {'bold': True},
                'backgroundColor': {'red': 0.9, 'green': 0.9, 'blue': 0.9}
            })
            
            print("✅ Заголовки столбцов настроены")
            
        except Exception as e:
            print(f"⚠️ Ошибка настройки заголовков: {e}")
    
    def _get_btc_price(self) -> Decimal:
        """
        Получает текущую цену BTCB в USD из пула PancakeSwap V3.
        Использует ту же логику, что и в liquidity_manager.py
        
        Returns:
            Decimal: Цена BTCB в USD
        """
        try:
            # Создаем контракт пула
            pool_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.pool_address),
                abi=self.pool_abi
            )
            
            # Получаем данные slot0 (содержит sqrtPriceX96)
            slot0_data = pool_contract.functions.slot0().call()
            sqrt_price_x96 = slot0_data[0]
            
            # Используем ту же логику, что и в liquidity_manager.py
            if sqrt_price_x96 == 0:
                raise ValueError("sqrt_price_x96 не может быть равен нулю.")
            
            # raw_val_assuming_t0_per_t1 = (sqrtPriceX96 / 2**96)**2
            # Это P_raw_USDT_per_BTCB (так как pool T0=USDT, T1=BTCB)
            raw_val_interpreted_as_t0_per_t1 = (Decimal(sqrt_price_x96) / Decimal(2**96))**2
            
            if raw_val_interpreted_as_t0_per_t1 == 0:
                raise ValueError("Рассчитанная сырая цена T0/T1 равна нулю.")

            # human_price P_T1/T0 = (1 / P_raw_T0/T1) * 10^(D1 - D0)
            # Оба токена имеют 18 decimals на BNB Chain
            human_price = (Decimal(1) / raw_val_interpreted_as_t0_per_t1) * \
                          (Decimal(10)**(18 - 18))  # BTCB decimals - USDT decimals = 1
            
            return human_price
            
        except Exception as e:
            print(f"❌ Ошибка получения цены BTC: {e}")
            return Decimal("100000")  # Fallback цена
    
    def _get_token_balance(self, token_address: str, decimals: int = 18) -> Decimal:
        """
        Получает баланс токена на кошельке.
        
        Args:
            token_address: Адрес контракта токена
            decimals: Количество десятичных знаков токена
            
        Returns:
            Decimal: Баланс токена в human-readable формате
        """
        try:
            # Создаем контракт токена
            token_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=self.erc20_abi
            )
            
            # Получаем баланс в raw формате
            balance_raw = token_contract.functions.balanceOf(
                Web3.to_checksum_address(self.wallet_address)
            ).call()
            
            # Конвертируем в human-readable формат
            balance = Decimal(balance_raw) / (Decimal(10) ** decimals)
            
            return balance
            
        except Exception as e:
            print(f"❌ Ошибка получения баланса токена {token_address}: {e}")
            return Decimal("0")
    
    def _get_total_balance(self) -> Decimal:
        """
        Получает общую стоимость всех активов (cbbtc + usdc + cake) в USD.
        
        Returns:
            Decimal: Общая стоимость портфеля в USD
        """
        try:
            # Получаем цену BTCB
            btc_price = self._get_btc_price()
            
            # Получаем балансы токенов (все токены имеют 18 decimals на BNB Chain)
            usdt_balance = self._get_token_balance(self.usdt_address, 18)  # USDT имеет 18 decimals
            btcb_balance = self._get_token_balance(self.btcb_address, 18)  # BTCB имеет 18 decimals
            cake_balance = self._get_token_balance(self.cake_address, 18)  # CAKE имеет 18 decimals
            
            # Рассчитываем общую стоимость в USD
            # CAKE пока считаем по цене 0 (нет API для получения цены)
            total_value = usdt_balance + (btcb_balance * btc_price)
            
            print(f"💰 Баланс USDT: {usdt_balance:.2f}")
            print(f"💰 Баланс BTCB: {btcb_balance:.8f} (${btcb_balance * btc_price:.2f})")
            print(f"💰 Баланс CAKE: {cake_balance:.2f}")
            print(f"💰 Общая стоимость: ${total_value:.2f}")
            
            return total_value
            
        except Exception as e:
            print(f"❌ Ошибка расчета общего баланса: {e}")
            return Decimal("0")
    
    def _get_current_datetime(self) -> str:
        """
        Получает текущую дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ.
        
        Returns:
            str: Отформатированная дата и время
        """
        now = datetime.now()
        return now.strftime("%d.%m.%Y %H:%M")
    
    def _format_number_for_sheets(self, number: Decimal, decimal_places: int = 2) -> str:
        """
        Форматирует число для записи в Google Sheets с запятой вместо точки.
        
        Args:
            number: Число для форматирования
            decimal_places: Количество знаков после запятой
            
        Returns:
            str: Отформатированное число с запятой
        """
        return f"{number:.{decimal_places}f}".replace('.', ',')
    
    def _format_duration_hours(self, duration_hours: float) -> str:
        """
        Форматирует продолжительность в часах с запятой вместо точки.
        
        Примеры:
        - 30 минут = 0.5 часа -> "0,50"
        - 20 минут = 0.33 часа -> "0,33" 
        - 2 часа 15 минут = 2.25 часа -> "2,25"
        - 1 час 45 минут = 1.75 часа -> "1,75"
        
        Args:
            duration_hours: Продолжительность в часах
            
        Returns:
            str: Отформатированная продолжительность с запятой
        """
        return f"{duration_hours:.2f}".replace('.', ',')
    
    def _parse_number_from_sheets(self, number_str: str) -> Decimal:
        """
        Парсит число из Google Sheets, конвертируя запятые в точки.
        
        Args:
            number_str: Строка с числом (может содержать запятые)
            
        Returns:
            Decimal: Распарсенное число
        """
        try:
            # Заменяем запятые на точки для корректного парсинга
            normalized_str = str(number_str).replace(',', '.')
            return Decimal(normalized_str)
        except Exception as e:
            print(f"⚠️ Ошибка парсинга числа '{number_str}': {e}")
            return Decimal("0")
    
    def _find_first_empty_row(self) -> int:
        """
        Находит первую пустую строку в таблице.
        
        Returns:
            int: Номер первой пустой строки
        """
        try:
            # Получаем все значения из столбца A
            column_a = self.worksheet.col_values(1)
            
            # Находим первую пустую ячейку
            for i, cell_value in enumerate(column_a, 1):
                if not cell_value.strip():
                    return i
            
            # Если все ячейки заполнены, возвращаем следующую
            return len(column_a) + 1
            
        except Exception as e:
            print(f"❌ Ошибка поиска пустой строки: {e}")
            return 1  # Fallback к первой строке
    
    def log_start(self) -> int:
        """
        Логирует начало рабочего периода в Google Sheets.
        
        Returns:
            int: Номер строки, в которую записаны данные
        """
        try:
            print("🟢 Логируем начало рабочего периода...")
            
            # Находим первую пустую строку
            row_number = self._find_first_empty_row()
            
            # Получаем текущие данные
            current_datetime = self._get_current_datetime()
            btc_price = self._get_btc_price()
            total_balance = self._get_total_balance()
            
            # Записываем данные в таблицу
            # Столбец A (Старт): Дата и время
            self.worksheet.update_cell(row_number, 1, current_datetime)
            
            # Столбец D (BTC, старт): Цена BTC (с запятой вместо точки)
            self.worksheet.update_cell(row_number, 4, self._format_number_for_sheets(btc_price))
            
            # Столбец F (Сумма старт): Общая сумма (с запятой вместо точки)
            self.worksheet.update_cell(row_number, 6, self._format_number_for_sheets(total_balance))
            
            print(f"✅ Данные записаны в строку {row_number}")
            print(f"   Время: {current_datetime}")
            print(f"   Цена BTC: ${btc_price:.2f}")
            print(f"   Общая сумма: ${total_balance:.2f}")
            
            return row_number
            
        except Exception as e:
            print(f"❌ Ошибка логирования начала периода: {e}")
            raise
    
    def log_finish(self, row_number: int) -> None:
        """
        Логирует окончание рабочего периода в Google Sheets.
        
        Args:
            row_number: Номер строки, в которую записывать данные
        """
        try:
            print("⏰ Логируем окончание рабочего периода...")
            
            # Получаем текущие данные
            current_datetime = self._get_current_datetime()
            btc_price = self._get_btc_price()
            total_balance = self._get_total_balance()
            
            # Записываем данные в таблицу
            # Столбец B (Финиш): Дата и время
            self.worksheet.update_cell(row_number, 2, current_datetime)
            
            # Столбец E (BTC, финиш): Цена BTC (с запятой вместо точки)
            self.worksheet.update_cell(row_number, 5, self._format_number_for_sheets(btc_price))
            
            # Столбец G (Сумма финиш): Общая сумма (с запятой вместо точки)
            self.worksheet.update_cell(row_number, 7, self._format_number_for_sheets(total_balance))
            
            # Рассчитываем дополнительные метрики
            try:
                # Читаем время начала и окончания
                start_time_str = self.worksheet.cell(row_number, 1).value
                finish_time_str = self.worksheet.cell(row_number, 2).value
                
                if start_time_str and finish_time_str:
                    # Парсим время с обработкой разных форматов года
                    try:
                        # Пробуем полный формат года (2025)
                        start_time = datetime.strptime(start_time_str, "%d.%m.%Y %H:%M")
                        finish_time = datetime.strptime(finish_time_str, "%d.%m.%Y %H:%M")
                    except ValueError:
                        try:
                            # Пробуем короткий формат года (25) и добавляем 2000
                            start_time = datetime.strptime(start_time_str, "%d.%m.%y %H:%M")
                            finish_time = datetime.strptime(finish_time_str, "%d.%m.%y %H:%M")
                        except ValueError:
                            print(f"⚠️ Не удалось распарсить время: {start_time_str} или {finish_time_str}")
                            return
                    
                    # Рассчитываем разность в часах
                    duration_hours = (finish_time - start_time).total_seconds() / 3600
                    
                    # Читаем данные о балансах и ценах
                    start_balance_str = self.worksheet.cell(row_number, 6).value
                    start_btc_price_str = self.worksheet.cell(row_number, 4).value
                    
                    if start_balance_str and start_btc_price_str:
                        start_balance = self._parse_number_from_sheets(start_balance_str)
                        start_btc_price = self._parse_number_from_sheets(start_btc_price_str)
                        
                        # Проверяем валидность распарсенных данных
                        if start_balance <= 0 or start_btc_price <= 0:
                            print(f"⚠️ Невалидные данные для расчета: баланс={start_balance}, цена={start_btc_price}")
                            return
                        
                        # Рассчитываем изменение баланса
                        balance_change = total_balance - start_balance
                        balance_change_pct = (balance_change / start_balance * 100) if start_balance > 0 else Decimal("0")
                        
                        # Рассчитываем изменение цены BTC
                        btc_price_change_pct = ((btc_price - start_btc_price) / start_btc_price * 100) if start_btc_price > 0 else Decimal("0")
                        
                        # Записываем все данные (с запятыми вместо точек)
                        self.worksheet.update_cell(row_number, 3, self._format_duration_hours(duration_hours))  # Часов
                        
                        print(f"✅ Продолжительность: {duration_hours:.2f} часов")
                        print(f"💰 Изменение баланса: ${balance_change:.2f} ({balance_change_pct:.2f}%)")
                        print(f"📈 Изменение цены BTC: {btc_price_change_pct:.2f}%")
                
            except Exception as e:
                print(f"⚠️ Ошибка расчета дополнительных метрик: {e}")
            
            print(f"✅ Данные обновлены в строке {row_number}")
            print(f"   Время: {current_datetime}")
            print(f"   Цена BTC: ${btc_price:.2f}")
            print(f"   Общая сумма: ${total_balance:.2f}")
            
        except Exception as e:
            print(f"❌ Ошибка логирования окончания периода: {e}")
            raise


# Пример использования
if __name__ == "__main__":
    """
    Пример использования модуля google_sheets_logger.py
    """
    try:
        # Инициализация логгера
        # Замените на реальные ID вашей таблицы и адрес кошелька
        logger = SheetLogger(
            spreadsheet_id="15yp8anIMPNFFqtD5p-YGlg5u8ewUHeDjluKXrCoGrYo",  # ID вашей Google таблицы
            wallet_address="0x6BEf7820d0ec29B821f585F46F4F650F61BF3cce",    # Адрес кошелька (будет название листа)
            service_account_path="service-account.json"
        )
        
        print("🚀 Тестирование Google Sheets Logger")
        print("=" * 50)
        
        # Логируем начало рабочего периода
        print("\n1. Логируем начало рабочего периода...")
        row_number = logger.log_start()
        
        # Имитируем работу (в реальном коде здесь будет ваша основная логика)
        import time
        print(f"\n2. Имитируем работу в течение 5 секунд...")
        time.sleep(5)
        
        # Логируем окончание рабочего периода
        print("\n3. Логируем окончание рабочего периода...")
        logger.log_finish(row_number)
        
        print("\n✅ Тест завершен успешно!")
        
    except Exception as e:
        print(f"❌ Ошибка в тесте: {e}")
        import traceback
        traceback.print_exc()
