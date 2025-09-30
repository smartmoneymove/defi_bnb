# src/liquidity_manager.py
import os
import json
import math
import time
import asyncio
import csv
from decimal import Decimal, getcontext, ROUND_HALF_UP
from pathlib import Path
import binascii
from eth_abi import encode
from web3 import Web3
import pandas as pd
from dotenv import load_dotenv

class GasManager:
    """Умный менеджер газа с кэшированием и адаптивностью"""
    
    def __init__(self, w3: Web3):
        self.w3 = w3
        self.gas_cache = {}  # Кэш для похожих операций
        
    async def estimate_smart_gas(self, 
                               contract_func, 
                               tx_params: dict,
                               operation_type: str = "default",
                               buffer_multiplier: float = 1.2) -> int:
        """
        Умная оценка газа с кэшированием и fallback
        """
        try:
            # Пробуем estimateGas
            estimated = contract_func.estimate_gas(tx_params)
            
            # Применяем буфер в зависимости от типа операции
            buffers = {
                "mint": 1.5,      # Mint может быть непредсказуемым
                "swap": 1.2,      # Swap стабильный
                "collect": 1.2,   # Collect простой
                "multicall": 1.3, # Multicall сложный
                "nft_transfer": 1.4, # NFT операции могут варьироваться
                "withdraw": 1.2,  # Farm withdraw операции
                "burn": 1.2       # Burn позиций
            }
            
            buffer = buffers.get(operation_type, buffer_multiplier)
            final_gas = int(estimated * buffer)
            
            # Кэшируем для похожих операций
            self.gas_cache[operation_type] = estimated
            
            return final_gas
            
        except Exception as e:
            print(f"EstimateGas failed for {operation_type}: {e}")
            
            # Используем кэш если есть
            if operation_type in self.gas_cache:
                cached_estimate = self.gas_cache[operation_type]
                return int(cached_estimate * buffer_multiplier * 1.1)
            
            # Fallback значения
            fallbacks = {
                "mint": 600000,
                "swap": 300000, 
                "collect": 200000,
                "multicall": 250000,
                "nft_transfer": 400000,
                "withdraw": 500000,
                "burn": 300000,
                "default": 500000
            }
            
            return fallbacks.get(operation_type, 500000)

    async def get_current_gas_price(self) -> int:
        """Получает актуальную цену газа с буфером"""
        try:
            base_fee = self.w3.eth.gas_price
            # Добавляем 5% буфер для волатильности
            return int(base_fee * 1.05)
        except:
            return 1000000  # Fallback



# Константы
PATH_ROUTER_ABI = "abis/UniversalRouter.json"
STATE_FILE_LM = Path(__file__).parent / 'liquidity_manager_state.json'

# Определяем корень проекта
project_root_for_test = Path(__file__).parent.parent

# Пути к файлам логов
FARM_REWARDS_LOG_FILE = project_root_for_test / 'data' / 'farm_rewards_claimed.csv'

from cake_farm import stake_nft_in_farm


# Селекторы для взаимодействия с роутером
EXECUTE_SELECTOR = "0x3593564c" # Селектор для execute

# Команды Universal Router для различных операций
UNIVERSAL_ROUTER_COMMANDS = {
    "V3_SWAP_EXACT_IN": 0x00,   # Код команды для свапа с точным входом в V3
    "PERMIT2_PERMIT": 0x01,
    "PERMIT2_TRANSFER_FROM": 0x02,
    "V2_SWAP_EXACT_IN": 0x03,
    "V2_SWAP_EXACT_OUT": 0x04,
    "V3_SWAP_EXACT_OUT": 0x05,
    "UNWRAP_WBNB": 0x06,  # Разворачивание WBNB в BNB
    "WRAP_BNB": 0x07,      # Оборачивание BNB в WBNB
}

# Константа для swap transactions (в PPM - частях на миллион)
# 100 = 0.01%, 500 = 0.05%, 3000 = 0.3%
FEE_TIER_FOR_SWAP_TRANSACTION = 100  # Используем 0.01% fee tier для свапов

# Устанавливаем точность для Decimal. 
# 36 может быть избыточно, но безопасно. 18-24 часто достаточно.
getcontext().prec = 36 

# Импорты удаленных модулей закомментированы
# from prediction_model import PredictionModel
# from prediction_model import prepare_features
# from volatility_analysis import load_and_preprocess_swaps as va_load_and_preprocess_swaps
# from volatility_analysis import resample_to_ohlcv_by_time as va_resample_to_ohlcv_by_time

load_dotenv()

NONF_POS_MANAGER_ADDRESS_ENV = os.getenv("NONF_POS_MANAGER_ADDRESS")
NONF_POS_MANAGER_ABI_JSON_PATH = os.getenv("NONF_POS_MANAGER_ABI_JSON_PATH") 

# Функция прямой проверки балансов (до инициализации LM)
async def check_balances_directly(rpc_url, wallet_address, token0_addr, token0_dec, token0_sym, token1_addr, token1_dec, token1_sym):
    print("\n=== ПРЯМАЯ ПРОВЕРКА БАЛАНСОВ (до инициализации LM) ===")
    w3_direct = Web3(Web3.HTTPProvider(rpc_url))
    if not w3_direct.is_connected():
        print("  Не удалось подключиться к RPC для прямой проверки балансов.")
        return None, None

    checksum_wallet = Web3.to_checksum_address(wallet_address)
    
    async def _get_bal(token_addr, dec, sym):
        checksum_token = Web3.to_checksum_address(token_addr)
        erc20_abi_balance_only = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]
        contract = w3_direct.eth.contract(address=checksum_token, abi=erc20_abi_balance_only)
        try:
            bal_raw = contract.functions.balanceOf(checksum_wallet).call()
            bal_human = Decimal(bal_raw) / (Decimal(10)**dec)
            print(f"  Баланс {sym} ({token_addr}): сырой={bal_raw}, человеческий={bal_human:.6f}")
            return bal_raw
        except Exception as e:
            print(f"  Ошибка при получении баланса {sym} ({token_addr}): {e}")
            return 0

    bal0 = await _get_bal(token0_addr, token0_dec, token0_sym)
    bal1 = await _get_bal(token1_addr, token1_dec, token1_sym)
    print("========================================================")
    return bal0, bal1

class LiquidityManager:
    def __init__(self, 
                 rpc_url: str, signer_address: str, private_key: str,
                 pool_address: str, pool_abi_path: str, 
                 token0_address: str, token1_address: str,
                 token0_decimals: int, token1_decimals: int,
                 token0_symbol: str, token1_symbol: str,
                 fee_tier: int, 
                 strategy_params: dict,
                 pancakeswap_router_address: str = None,
                 farm_address: str = None,
                 farm_abi_path: str = None,
                 swap_pool_fee_tier: int = 100):
        
        # Сохраняем параметры
        self.rpc_url = rpc_url
        self.signer_address = signer_address
        self.private_key = private_key
        self.pool_address = pool_address
        self.pool_abi_path = pool_abi_path
        self.token0_for_calcs = token0_address
        self.token1_for_calcs = token1_address
        self.decimals0_for_calcs = token0_decimals
        self.decimals1_for_calcs = token1_decimals
        self.token0_for_calcs_symbol = token0_symbol
        self.token1_for_calcs_symbol = token1_symbol
        self.fee_tier = fee_tier
        self.strategy_params = strategy_params
        self.pancakeswap_router_address = pancakeswap_router_address
        self.farm_address = farm_address
        self.farm_abi_path = farm_abi_path
        self.swap_pool_fee_tier = swap_pool_fee_tier

        # Инициализируем Web3 с поддержкой PoA (BNB Chain)
        from web3.middleware import geth_poa_middleware
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        self.signer_address = Web3.to_checksum_address(signer_address)
        
        # Инициализируем газ менеджер
        self.gas_manager = GasManager(self.w3)
        
        # Инициализируем nonce кэш для предотвращения конфликтов
        self._nonce_cache = None
        self._nonce_cache_time = 0
        
        # Инициализируем контракты
        self._init_contracts()
        
    async def _get_next_nonce(self, use_pending=True):
        """
        Получает следующий nonce с кэшированием для предотвращения конфликтов.
        
        Args:
            use_pending: Использовать 'pending' состояние для получения актуального nonce
            
        Returns:
            int: Следующий nonce для использования
        """
        import time
        current_time = time.time()
        
        # Обновляем кэш каждые 10 секунд или если он пустой
        if (self._nonce_cache is None or 
            current_time - self._nonce_cache_time > 10):
            
            if use_pending:
                self._nonce_cache = self.w3.eth.get_transaction_count(self.signer_address, 'pending')
            else:
                self._nonce_cache = self.w3.eth.get_transaction_count(self.signer_address, 'latest')
            self._nonce_cache_time = current_time
            print(f"  DEBUG: Обновили nonce кэш: {self._nonce_cache}")
        else:
            # Используем кэшированный nonce и увеличиваем его
            self._nonce_cache += 1
            print(f"  DEBUG: Используем следующий nonce из кэша: {self._nonce_cache}")
            
        return self._nonce_cache
        
    def _init_contracts(self):
        """Инициализирует контракты после создания Web3 подключения"""
        # Загружаем ABI пула
        with open(self.pool_abi_path, 'r') as f:
            pool_abi = json.load(f)
        
        # Создаем контракт пула
        self.pool_address = Web3.to_checksum_address(self.pool_address)
        self.pool_contract = self.w3.eth.contract(address=self.pool_address, abi=pool_abi)
        
        # Инициализируем контракт фарминга если указан адрес
        if self.farm_address:
            self.farm_address = Web3.to_checksum_address(self.farm_address)
            if self.farm_abi_path:
                with open(self.farm_abi_path, 'r') as f:
                    farm_abi = json.load(f)
                self.farm_contract = self.w3.eth.contract(address=self.farm_address, abi=farm_abi)
        else:
            self.farm_address = None
        
        # Инициализируем роутер для свапов
        if self.pancakeswap_router_address:
            self.pancakeswap_router_address = Web3.to_checksum_address(self.pancakeswap_router_address)
        
        # Настраиваем токены
        self.token0_address = Web3.to_checksum_address(self.token0_for_calcs)
        self.token1_address = Web3.to_checksum_address(self.token1_for_calcs)
        self.token0_decimals = self.decimals0_for_calcs
        self.token1_decimals = self.decimals1_for_calcs
        self.token0_symbol = self.token0_for_calcs_symbol
        self.token1_symbol = self.token1_for_calcs_symbol

        if not self.w3.is_connected():
            raise ConnectionError(f"Не удалось подключиться к RPC: {self.rpc_url}")

        self.pool_actual_token0_addr = Web3.to_checksum_address(self.pool_contract.functions.token0().call())
        self.pool_actual_token1_addr = Web3.to_checksum_address(self.pool_contract.functions.token1().call())

        # Параметрические токен0 и токен1 (например, USDT и BTCB на BNB Chain)
        self.param_token0_addr = Web3.to_checksum_address(self.token0_for_calcs)
        self.param_token1_addr = Web3.to_checksum_address(self.token1_for_calcs)
        self.param_token0_decimals = self.decimals0_for_calcs
        self.param_token1_decimals = self.decimals1_for_calcs
        
        # Определяем, соответствуют ли фактические токены пула нашим параметрическим токенам
        # и сохраняем фактические десятичные знаки для токенов пула
        if self.pool_actual_token0_addr == self.param_token0_addr and self.pool_actual_token1_addr == self.param_token1_addr:
            self.pool_order_matches_params = True # paramT0 = poolT0, paramT1 = poolT1
            self.invert_price_for_t0_t1 = False # Не требуется инверсия цены
            self.pool_actual_token0_decimals = self.param_token0_decimals
            self.pool_actual_token1_decimals = self.param_token1_decimals
            # Добавляем адреса для расчетов
            self.token0_for_calcs = self.param_token0_addr
            self.token1_for_calcs = self.param_token1_addr
            print(f"Порядок токенов совпадает: Пул T0 ({self.pool_actual_token0_addr}) = Param T0, Пул T1 ({self.pool_actual_token1_addr}) = Param T1")
        elif self.pool_actual_token0_addr == self.param_token1_addr and self.pool_actual_token1_addr == self.param_token0_addr:
            self.pool_order_matches_params = False # paramT0 = poolT1, paramT1 = poolT0 (инверсия)
            self.invert_price_for_t0_t1 = True # Требуется инверсия цены
            self.pool_actual_token0_decimals = self.param_token1_decimals # т.к. poolT0 это наш paramT1
            self.pool_actual_token1_decimals = self.param_token0_decimals # т.к. poolT1 это наш paramT0
            # Добавляем адреса для расчетов (инвертированные)
            self.token0_for_calcs = self.param_token1_addr
            self.token1_for_calcs = self.param_token0_addr
            print(f"Порядок токенов инвертирован: Пул T0 ({self.pool_actual_token0_addr}) = Param T1, Пул T1 ({self.pool_actual_token1_addr}) = Param T0")
        else:
            raise ValueError("Переданные адреса токенов (param_token0, param_token1) не соответствуют токенам пула.")

        # Определение tick_spacing на основе комиссии пула Uniswap V3
        if self.fee_tier == 100: self.tick_spacing = 1
        elif self.fee_tier == 500: self.tick_spacing = 10
        elif self.fee_tier == 2500: self.tick_spacing = 50 # Проверить актуальные значения для вашей сети/DEX
        elif self.fee_tier == 3000: self.tick_spacing = 60
        elif self.fee_tier == 10000: self.tick_spacing = 200
        else: 
            # По умолчанию можно взять наиболее частый или выбросить ошибку
            print(f"ПРЕДУПРЕЖДЕНИЕ: Неизвестный fee_tier {self.fee_tier}, используется tick_spacing=60 по умолчанию.")
            self.tick_spacing = 1 
        print(f"Для комиссии {self.fee_tier}, tick_spacing = {self.tick_spacing}")

        # Инициализируем NFT Position Manager
        self.nonf_pos_manager_address = Web3.to_checksum_address(NONF_POS_MANAGER_ADDRESS_ENV)
        abi_path_from_env = NONF_POS_MANAGER_ABI_JSON_PATH
        if not abi_path_from_env:
            raise ValueError("NONF_POS_MANAGER_ABI_JSON_PATH не задан в .env")
        
        # Проверяем, является ли путь абсолютным или относительным к project_root_for_test
        manager_abi_path = Path(abi_path_from_env)
        if not manager_abi_path.is_absolute() and project_root_for_test:
             manager_abi_path = project_root_for_test / manager_abi_path
        
        if manager_abi_path.exists():
             with open(manager_abi_path, 'r') as f: 
                 manager_abi_full = json.load(f)
        else: 
            raise FileNotFoundError(f"ABI файл для NonfungiblePositionManager не найден: {manager_abi_path}")
        self.nonf_pos_manager = self.w3.eth.contract(address=self.nonf_pos_manager_address, abi=manager_abi_full)
        
        self.num_managed_positions = self.strategy_params.get('num_positions', 3)  # Default to 3 positions
        self.position_mode = self.strategy_params.get('position_mode', '3_positions')  # Новый параметр
        
        # Загружаем состояние из файла или создаем пустые слоты
        self.managed_positions_slots = self._load_state_from_file()
        self.initial_position_data = {}  # Словарь для хранения начальных данных позиций
        if self.managed_positions_slots is None:
            self.managed_positions_slots = [None] * self.num_managed_positions
            print("  Файл состояния не найден или пуст, начинаем с пустыми слотами.")
        else:
            print(f"  Состояние управляемых позиций успешно загружено из файла.")
        


        
        # Капитал будет рассчитываться динамически на основе актуальных балансов
        print(f"LiquidityManager инициализирован. Капитал будет рассчитываться из текущих балансов.")

        print(f"LiquidityManager инициализирован для пула {self.pool_address}.")
        print(f"Режим: {self.position_mode} ({self.num_managed_positions} позиции)")
        print(f"Общий диапазон: ~{self.strategy_params.get('total_range_width_pct', Decimal('0.01'))*100}%")

    def _convert_human_price_param_t1_t0_to_raw_pool_price(self, human_price_param_t1_t0: Decimal) -> Decimal:
        # Шаг 1: Используем цену напрямую без масштабирования (оба токена 18 decimals на BNB Chain)
        scaled_human_price = human_price_param_t1_t0
        
        # Шаг 2: Преобразовать human_price_param_t1_t0 в human_price_pool_actual_t1_in_t0
        if self.pool_order_matches_params:
            # paramT1 = pool_actual_T1, paramT0 = pool_actual_T0
            human_price_pool_actual_t1_in_t0 = scaled_human_price
        else:
            # paramT1 = pool_actual_T0, paramT0 = pool_actual_T1 (инверсия)
            # human_price_param_t1_t0 - это цена pool_actual_T0 / pool_actual_T1
            # Нам нужна цена pool_actual_T1 / pool_actual_T0, поэтому инвертируем
            if scaled_human_price == Decimal(0):
                raise ValueError("Human price (param_T1/param_T0) is zero, cannot convert.")
            human_price_pool_actual_t1_in_t0 = Decimal(1) / scaled_human_price
        
        # Шаг 3: Конвертировать human_price_pool_actual_t1_in_t0 в raw_price_pool_actual_t1_in_t0
        # Human_P(T1/T0) = Raw_P(T1/T0) * 10^(decimals_T0_pool - decimals_T1_pool)
        # => Raw_P(T1/T0) = Human_P(T1/T0) / 10^(decimals_T0_pool - decimals_T1_pool)
        decimal_adj_exponent = self.pool_actual_token0_decimals - self.pool_actual_token1_decimals
        adj_factor = Decimal(10)**decimal_adj_exponent
        
        if adj_factor == Decimal(0): # Избегаем деления на ноль, хотя для 10^X это маловероятно
            raise ValueError("Decimal adjustment factor is zero during human_price to raw_price conversion.")
            
        raw_price_pool_actual_t1_t0 = human_price_pool_actual_t1_in_t0 / adj_factor
        return raw_price_pool_actual_t1_t0

    def _convert_raw_pool_price_to_human_price_param_t1_t0(self, raw_price_pool_actual_t1_t0: Decimal) -> Decimal:
        # Шаг 1: Конвертировать raw_price_pool_actual_t1_in_t0 в human_price_pool_actual_t1_in_t0
        # Human_P(T1/T0) = Raw_P(T1/T0) * 10^(decimals_T0_pool - decimals_T1_pool)
        decimal_adj_exponent = self.pool_actual_token0_decimals - self.pool_actual_token1_decimals
        adj_factor = Decimal(10)**decimal_adj_exponent
        human_price_pool_actual_t1_in_t0 = raw_price_pool_actual_t1_t0 * adj_factor

        # Шаг 2: Преобразовать human_price_pool_actual_t1_in_t0 в human_price_param_t1_t0
        if self.pool_order_matches_params:
            # paramT1 = pool_actual_T1, paramT0 = pool_actual_T0
            final_human_price_param_t1_t0 = human_price_pool_actual_t1_in_t0
        else:
            # paramT1 = pool_actual_T0, paramT0 = pool_actual_T1 (инверсия)
            # human_price_pool_actual_t1_in_t0 - это цена pool_actual_T1 / pool_actual_T0 = paramT0 / paramT1
            # Нам нужна paramT1 / paramT0, поэтому инвертируем
            if human_price_pool_actual_t1_in_t0 == Decimal(0):
                raise ValueError("Cannot invert zero human_price_pool_actual_t1_in_t0.")
            final_human_price_param_t1_t0 = Decimal(1) / human_price_pool_actual_t1_in_t0
        
        # Шаг 3: Возвращаем финальную цену без дополнительного масштабирования (оба токена 18 decimals)
        return final_human_price_param_t1_t0
    
    def _param_t1_t0_human_to_pool_t1_t0_raw(self, human_price_param_t1_t0: Decimal) -> Decimal:
        """
        Конвертирует P_paramT1/paramT0 (human, например BTCB/USDT ~100k) 
        в P_poolT1/poolT0 (raw, например BTCB_raw/USDT_raw, ~1), которую ожидает price_to_tick.
        P_raw(T1/T0) = P_human(T1/T0) * 10^(DecimalsT0_pool - DecimalsT1_pool)
        
        УСТАРЕВШАЯ ФУНКЦИЯ: используйте _human_price_param_t1_t0_to_raw_price_pool_t1_t0 вместо нее.
        """
        # paramT0=USDT (self.decimals0_for_calcs), paramT1=BTCB (self.decimals1_for_calcs)
        # pool_actual_token0 = USDT (self.pool_actual_token0_decimals), 
        # pool_actual_token1 = BTCB (self.pool_actual_token1_decimals)
        # В твоем случае self.pool_order_matches_params = True, поэтому _for_calcs и _actual_pool совпадают

        if self.invert_price_for_t0_t1: # Если paramT1 это token0 пула (НЕ твой случай)
            if human_price_param_t1_t0 == Decimal(0): raise ValueError("Human price is zero, cannot invert.")
            # P_human(paramT1/paramT0) -> P_human(poolT0/poolT1)
            # P_raw(poolT1/poolT0) = (1 / P_human(poolT0/poolT1)) * 10^(D_poolT0 - D_poolT1)
            # D_poolT0 = self.pool_actual_token0_decimals (который был paramT1_decimals)
            # D_poolT1 = self.pool_actual_token1_decimals (который был paramT0_decimals)
            raw_price = (Decimal(1) / human_price_param_t1_t0) * \
                        (Decimal(10)**(self.pool_actual_token0_decimals - self.pool_actual_token1_decimals))
        else: # paramT1 это token1 пула (ТВОЙ СЛУЧАЙ)
              # human_price_param_t1_t0 это P_human(poolT1/poolT0)
              # P_raw(poolT1/poolT0) = P_human(poolT1/poolT0) * 10^(D_poolT0 - D_poolT1)
            raw_price = human_price_param_t1_t0 * \
                        (Decimal(10)**(self.pool_actual_token0_decimals - self.pool_actual_token1_decimals))
        
        if raw_price <= 0:
            raise ValueError(f"Сырая цена для тиков P_poolT1/T0 не может быть <=0: {raw_price} из human {human_price_param_t1_t0}")
        return raw_price

    def _pool_t1_t0_raw_to_param_t1_t0_human(self, raw_price_pool_t1_t0: Decimal) -> Decimal:
        """
        Конвертирует P_poolT1/poolT0 (raw, например BTCB_raw/USDT_raw) 
        обратно в P_paramT1/paramT0 (human, например BTCB/USDT, ~100k).
        
        УСТАРЕВШАЯ ФУНКЦИЯ: используйте _raw_price_pool_t1_t0_to_human_price_param_t1_t0 вместо нее.
        """
        # raw_price_pool_t1_t0 это P_raw(poolT1/poolT0)
        if not self.invert_price_for_t0_t1: # paramT1=poolT1 (ТВОЙ СЛУЧАЙ)
            # P_human(paramT1/paramT0) = P_raw(poolT1/poolT0) * 10^(D_poolT1 - D_poolT0)
            human_price = raw_price_pool_t1_t0 * \
                          (Decimal(10)**(self.pool_actual_token1_decimals - self.pool_actual_token0_decimals))
        else: # paramT1=poolT0
            if raw_price_pool_t1_t0 == Decimal(0): raise ValueError("Raw pool price is zero.")
            # P_human(paramT1/paramT0) = (1 / P_raw(poolT1/poolT0)) * 10^(D_paramT0 - D_paramT1)
            # D_paramT1 -> self.pool_actual_token0_decimals
            # D_paramT0 -> self.pool_actual_token1_decimals
            human_price = (Decimal(1) / raw_price_pool_t1_t0) * \
                          (Decimal(10)**(self.pool_actual_token0_decimals - self.pool_actual_token1_decimals))
        return human_price

    def price_to_tick(self, raw_price_pool_t1_t0: Decimal) -> int: # Ожидает P_raw(token1_pool/token0_pool)
        if raw_price_pool_t1_t0 <= 0:
            raise ValueError(f"Сырая цена для конвертации в тик должна быть положительной: {raw_price_pool_t1_t0}")
        return math.floor(math.log(float(raw_price_pool_t1_t0), 1.0001))

    def tick_to_raw_price_pool_t1_t0(self, tick: int) -> Decimal: # Возвращает P_raw(token1_pool/token0_pool)
        return Decimal('1.0001')**Decimal(tick)

    def _get_human_price_from_raw_sqrt_price_x96(self, sqrt_price_x96: int) -> Decimal:
        """
        Конвертирует sqrtPriceX96 из пула в человекочитаемую цену param_T1/param_T0 (BTCB/USDT).
        """
        if sqrt_price_x96 == 0: raise ValueError("sqrt_price_x96 не может быть равен нулю.")
        
        # raw_val_assuming_t0_per_t1 = (sqrtPriceX96 / 2**96)**2
        # Это P_raw_USDT_per_BTCB (так как pool T0=USDT, T1=BTCB)
        raw_val_interpreted_as_t0_per_t1 = (Decimal(sqrt_price_x96) / Decimal(2**96))**2
        
        if raw_val_interpreted_as_t0_per_t1 == 0:
            raise ValueError("Рассчитанная сырая цена T0/T1 равна нулю.")

        # human_price P_T1/T0 = (1 / P_raw_T0/T1) * 10^(D1 - D0)
        # Для токенов с одинаковыми decimals (18-18) множитель = 1
        human_price = (Decimal(1) / raw_val_interpreted_as_t0_per_t1) * \
                      (Decimal(10)**(self.decimals1_for_calcs - self.decimals0_for_calcs))
        return human_price

    def _get_raw_price_for_tick_calc_from_human_price(self, human_price_param_t1_t0: Decimal) -> Decimal:
        """
        Конвертирует человеческую цену param_T1/param_T0 (BTCB/USDT, ~100k) 
        в сырую цену P_poolT1/poolT0_raw (BTCB_raw/USDT_raw, ~1), 
        которую ожидает price_to_tick.
        """
        # P_raw_T1/T0 = P_human_T1/T0 * 10^(D0 - D1)
        # Для токенов с одинаковыми decimals (18-18) множитель = 1
        raw_price = human_price_param_t1_t0 * \
                    (Decimal(10)**(self.decimals0_for_calcs - self.decimals1_for_calcs))
        if raw_price <= 0:
            raise ValueError(f"Сырая цена для тиков не может быть <=0: {raw_price} из human {human_price_param_t1_t0}")
        return raw_price

    def _get_human_price_from_raw_tick_price(self, raw_price_pool_t1_t0: Decimal) -> Decimal:
        """
        Конвертирует сырую цену P_poolT1/poolT0_raw (из tick_to_price) 
        обратно в человеческую цену param_T1/param_T0 (BTCB/USDT).
        """
        # P_human_T1/T0 = P_raw_T1/T0 * 10^(D1 - D0)
        # Для токенов с одинаковыми decimals (18-18) множитель = 1
        return raw_price_pool_t1_t0 * \
               (Decimal(10)**(self.decimals1_for_calcs - self.decimals0_for_calcs))

    def align_tick_to_spacing(self, tick: int, round_strategy: str = "closest") -> int:
        """
        Выравнивает тик по tick_spacing пула.
        round_strategy: "closest", "down", "up"
        """
        remainder = tick % self.tick_spacing
        if remainder == 0:
            return tick

        if round_strategy == "down":
            return tick - remainder
        elif round_strategy == "up":
            return tick - remainder + self.tick_spacing
        elif round_strategy == "closest":
            if remainder < self.tick_spacing / 2:
                return tick - remainder
            else:
                return tick - remainder + self.tick_spacing
        else: # По умолчанию к ближайшему
            if remainder < self.tick_spacing / 2:
                return tick - remainder
            else:
                return tick - remainder + self.tick_spacing

    async def get_current_pool_state(self):
        try:
            # 🔥 ПРИНУДИТЕЛЬНОЕ ОБНОВЛЕНИЕ: Каждый раз получаем свежие данные из блокчейна
            slot0_data = self.pool_contract.functions.slot0().call()
            sqrt_price_x96 = slot0_data[0]
            current_tick_from_slot0 = slot0_data[1]
            
            human_price = self._get_human_price_from_raw_sqrt_price_x96(sqrt_price_x96)
            
            # ОБЯЗАТЕЛЬНЫЙ вывод актуального состояния
            print(f"🔄 СВЕЖИЕ ДАННЫЕ ПУЛА:")
            print(f"   sqrt_price_x96: {sqrt_price_x96}")
            print(f"   current_tick: {current_tick_from_slot0}")
            print(f"   human_price: ${human_price:.4f}")
            
            # Проверяем валидность данных
            if human_price is None or human_price <= 0:
                raise ValueError(f"Невалидная цена: {human_price}")
            if current_tick_from_slot0 is None:
                raise ValueError(f"Невалидный тик: {current_tick_from_slot0}")

            return human_price, current_tick_from_slot0, sqrt_price_x96
        except Exception as e:
            print(f"🚨 КРИТИЧЕСКАЯ ОШИБКА получения актуальной цены: {e}")
            import traceback
            traceback.print_exc()
            return None, None, None

    def _tick_to_human_price_param_t1_t0(self, tick: int) -> Decimal:
        """
        Преобразует тик в человекочитаемую цену param_T1/param_T0 (BTCB/USDT).
        
        Args:
            tick: Тик в формате Uniswap V3
        
        Returns:
            Decimal: Цена в формате param_T1/param_T0 (BTCB/USDT, ~100k)
        """
        # 1. Конвертируем тик в raw_price (poolT1/poolT0, в вашем случае USDT_raw/BTCB_raw)
        raw_price = self.tick_to_raw_price_pool_t1_t0(tick)
        
        # 2. Конвертируем raw_price в human_price (paramT1/paramT0, BTCB/USDT)
        return self._raw_price_pool_t1_t0_to_human_price_param_t1_t0(raw_price)
    
    def _load_state_from_file(self) -> list | None:
        """
        Загружает состояние управляемых позиций и начальные данные из JSON-файла.
        
        Returns:
            list: Список данных слотов или None, если файл отсутствует или повреждён
        """
        if STATE_FILE_LM.exists():
            try:
                with open(STATE_FILE_LM, 'r') as f:
                    full_state = json.load(f)
                
                # Проверяем новый формат файла
                if isinstance(full_state, dict) and 'managed_positions_slots' in full_state:
                    state = full_state['managed_positions_slots']
                    
                    # Загружаем initial_position_data
                    if 'initial_position_data' in full_state:
                        self.initial_position_data = {}
                        for nft_id_str, data in full_state['initial_position_data'].items():
                            # Конвертируем строковые ключи обратно в int и Decimal значения
                            converted_data = {}
                            for key, value in data.items():
                                if key in ['initial_usdt', 'initial_btcb', 'initial_value_usdt', 'btcb_price_open']:
                                    try:
                                        converted_data[key] = float(value)  # Для PnL расчетов используем float
                                    except:
                                        converted_data[key] = value
                                else:
                                    converted_data[key] = value
                            self.initial_position_data[int(nft_id_str)] = converted_data
                        print(f"  Загружено {len(self.initial_position_data)} записей начальных данных позиций")
                    else:
                        self.initial_position_data = {}
                        
                else:
                    # Старый формат - только managed_positions_slots
                    state = full_state
                    self.initial_position_data = {}
                
                # Проверяем, что это список нужной длины
                if isinstance(state, list) and len(state) == self.num_managed_positions:
                    # Конвертируем ликвидность обратно в Decimal
                    for item in state:
                        if item and 'liquidity' in item and isinstance(item['liquidity'], str):
                            try:
                                item['liquidity'] = Decimal(item['liquidity'])
                            except:
                                if isinstance(item['liquidity'], (int, float)):
                                    item['liquidity'] = Decimal(str(item['liquidity']))
                                else:
                                    item['liquidity'] = Decimal(0)
                        elif item and 'liquidity' in item and isinstance(item['liquidity'], (int, float)):
                            item['liquidity'] = Decimal(str(item['liquidity']))
                    return state
                else:
                    print(f"  Файл состояния {STATE_FILE_LM} имеет неверный формат или длину. Будет перезаписан.")
                    self.initial_position_data = {}
                    return None
            except Exception as e:
                print(f"  Ошибка при загрузке состояния из {STATE_FILE_LM}: {e}. Будет создан новый файл.")
                self.initial_position_data = {}
                return None
        
        self.initial_position_data = {}
        return None

    def _save_state_to_file(self):
        """
        Сохраняет текущее состояние управляемых позиций и начальные данные в JSON-файл.
        """
        print(f"  Сохранение состояния управляемых позиций в {STATE_FILE_LM}...")
        # Конвертируем Decimal в строку для JSON-сериализации
        data_to_save = []
        for slot_data in self.managed_positions_slots:
            if slot_data is None:
                data_to_save.append(None)
                continue
                
            # Создаем копию словаря, чтобы не изменять оригинал
            slot_copy = slot_data.copy()
            
            # Обрабатываем поле liquidity, если оно есть
            if 'liquidity' in slot_copy and isinstance(slot_copy['liquidity'], Decimal):
                slot_copy['liquidity'] = str(slot_copy['liquidity'])
            
            data_to_save.append(slot_copy)

        # Подготавливаем initial_position_data для сохранения
        initial_data_to_save = {}
        if hasattr(self, 'initial_position_data'):
            for nft_id, data in self.initial_position_data.items():
                # Конвертируем все Decimal в строки
                converted_data = {}
                for key, value in data.items():
                    if isinstance(value, Decimal):
                        converted_data[key] = str(value)
                    else:
                        converted_data[key] = value
                initial_data_to_save[str(nft_id)] = converted_data

        # Создаем полную структуру для сохранения
        full_state = {
            'managed_positions_slots': data_to_save,
            'initial_position_data': initial_data_to_save
        }

        try:
            with open(STATE_FILE_LM, 'w') as f:
                json.dump(full_state, f, indent=4)
            print(f"  Состояние и начальные данные позиций успешно сохранены.")
        except Exception as e:
            print(f"  Ошибка при сохранении состояния в {STATE_FILE_LM}: {e}")
    
    def calculate_target_ranges_in_ticks(self, center_human_price_param_t1_t0: Decimal) -> list:
        """
        Рассчитывает целевые диапазоны в тиках на основе центральной цены и стратегии.
        Позиции идут впритык друг к другу.
        """
        print(f"\n=== РАСЧЕТ ЦЕЛЕВЫХ ДИАПАЗОНОВ В ТИКАХ ===")
        print(f"Расчет диапазонов для 3 позиций вокруг цены {center_human_price_param_t1_t0:.4f} ({self.token1_for_calcs_symbol}/{self.token0_for_calcs_symbol})")
        
        # Получаем параметры диапазонов из стратегии
        total_half_width_pct = Decimal('0.0006')  # 0.06% в каждую сторону (общая ширина 0.12%)
        pos_width_pct = total_half_width_pct * Decimal('2') / Decimal('3')  # Делим общую ширину на 3 равные части
        
        # Нижняя граница всего блока позиций
        block_lower_human_price = center_human_price_param_t1_t0 * (Decimal(1) - total_half_width_pct)
        
        target_ranges = [None] * 3  # Инициализируем список с 3 пустыми элементами
        
        # Для отладки записываем ожидаемый диапазон
        print(f"Ожидаемый общий диапазон позиций (human price): "
              f"{block_lower_human_price:.2f} - "
              f"{block_lower_human_price * (1 + total_half_width_pct * 2):.2f}")
        
        # Создаем позиции впритык друг к другу
        for i in range(3):
            current_pos_lower_human = block_lower_human_price * (1 + pos_width_pct * i)
            current_pos_upper_human = current_pos_lower_human * (1 + pos_width_pct)
            
            # Конвертируем человеческие цены paramT1/paramT0 в СЫРУЮ ЦЕНУ ПУЛА poolT1/poolT0
            raw_low_for_tick = self._human_price_param_t1_t0_to_raw_price_pool_t1_t0(current_pos_lower_human)
            raw_high_for_tick = self._human_price_param_t1_t0_to_raw_price_pool_t1_t0(current_pos_upper_human)
            
            print(f"DEBUG: Позиция {i}: Human цены paramT1/T0 ~{current_pos_lower_human:.2f}-{current_pos_upper_human:.2f} -> Raw цены poolT1/T0 для тиков ~{raw_low_for_tick:.8f}-{raw_high_for_tick:.8f}")
            
            # Выбираем правильный порядок raw_low и raw_high для price_to_tick
            tick_h = self.align_tick_to_spacing(self.price_to_tick(raw_low_for_tick), round_strategy="down")
            tick_l = self.align_tick_to_spacing(self.price_to_tick(raw_high_for_tick), round_strategy="up")
            
            if tick_l >= tick_h:
                tick_l = tick_h - self.tick_spacing
                print(f"  Предупреждение: tick_lower был >= tick_upper для позиции {i}. Установлен минимальный диапазон.")
            
            # Получаем человеческие цены из конечных тиков (для проверки)
            raw_price_l = self.tick_to_raw_price_pool_t1_t0(tick_l)
            raw_price_h = self.tick_to_raw_price_pool_t1_t0(tick_h)
            
            human_price_low_calc = self._raw_price_pool_t1_t0_to_human_price_param_t1_t0(raw_price_l)
            human_price_high_calc = self._raw_price_pool_t1_t0_to_human_price_param_t1_t0(raw_price_h)
            
            print(f"  Позиция {i}: Тики [{tick_l}, {tick_h}] -> "
                  f"Цены human paramT1/T0: {human_price_low_calc:.2f}-{human_price_high_calc:.2f}")
            
            target_ranges[i] = {
                'tickLower': tick_l,
                'tickUpper': tick_h,
                'priceRangeHuman': (human_price_low_calc, human_price_high_calc)
            }
        
        # === ПОСТОБРАБОТКА: Убираем промежутки ===
        sorted_ranges = sorted([(i, r) for i, r in enumerate(target_ranges)], key=lambda x: x[1]['tickLower'])
        for j in range(len(sorted_ranges) - 1):
            current_idx, current_range = sorted_ranges[j]
            next_idx, next_range = sorted_ranges[j + 1]
            # Если есть промежуток - убираем его
            if next_range['tickLower'] > current_range['tickUpper']:
                target_ranges[next_idx]['tickLower'] = current_range['tickUpper']
        
        return target_ranges
    
    def calculate_target_ranges_2_positions(self, center_human_price_param_t1_t0: Decimal) -> list:
        """
        Рассчитывает целевые диапазоны для 2-позиционной стратегии.
        Позиция 0: ниже текущей цены (только BTCB)
        Позиция 1: выше текущей цены (только USDT)
        Общая ширина: 0.08% (каждая позиция 4 тика = 0.04%)
        """
        print(f"\n=== РАСЧЕТ 2-ПОЗИЦИОННОЙ СТРАТЕГИИ ===")
        print(f"Расчет диапазонов для 2 позиций вокруг цены {center_human_price_param_t1_t0:.4f} ({self.token1_for_calcs_symbol}/{self.token0_for_calcs_symbol})")
        
        # Параметры для 2-позиционной стратегии
        individual_position_width_pct = Decimal('0.0004')  # 0.04% каждая позиция (4 тика)
        
        # Позиция 0: ниже цены
        lower_pos_upper_human = center_human_price_param_t1_t0  # Верхняя граница = текущая цена
        lower_pos_lower_human = center_human_price_param_t1_t0 * (Decimal(1) - individual_position_width_pct)
        
        # Позиция 1: выше цены  
        upper_pos_lower_human = center_human_price_param_t1_t0  # Нижняя граница = текущая цена
        upper_pos_upper_human = center_human_price_param_t1_t0 * (Decimal(1) + individual_position_width_pct)
        
        print(f"Позиция 0 (ниже): {lower_pos_lower_human:.2f} - {lower_pos_upper_human:.2f}")
        print(f"Позиция 1 (выше): {upper_pos_lower_human:.2f} - {upper_pos_upper_human:.2f}")
        
        target_ranges = [None] * 2
        
        # Рассчитываем тики для нижней позиции (позиция 0)
        raw_low_0 = self._human_price_param_t1_t0_to_raw_price_pool_t1_t0(lower_pos_lower_human)
        raw_high_0 = self._human_price_param_t1_t0_to_raw_price_pool_t1_t0(lower_pos_upper_human)
        
        # ✅ ПРАВИЛЬНОЕ сопоставление: низкая цена = низкий тик, высокая цена = высокий тик
        tick_lower_0 = self.align_tick_to_spacing(self.price_to_tick(raw_low_0), round_strategy="down")
        tick_upper_0 = self.align_tick_to_spacing(self.price_to_tick(raw_high_0), round_strategy="up")
        
        if tick_lower_0 >= tick_upper_0:
            tick_upper_0 = tick_lower_0 + self.tick_spacing
        
        # Рассчитываем тики для верхней позиции (позиция 1)  
        raw_low_1 = self._human_price_param_t1_t0_to_raw_price_pool_t1_t0(upper_pos_lower_human)
        raw_high_1 = self._human_price_param_t1_t0_to_raw_price_pool_t1_t0(upper_pos_upper_human)
        
        # ✅ ПРАВИЛЬНОЕ сопоставление для верхней позиции
        tick_lower_1 = self.align_tick_to_spacing(self.price_to_tick(raw_low_1), round_strategy="down")
        tick_upper_1 = self.align_tick_to_spacing(self.price_to_tick(raw_high_1), round_strategy="up")
        
        if tick_lower_1 >= tick_upper_1:
            tick_upper_1 = tick_lower_1 + self.tick_spacing
            
        # 🔧 ОБЕСПЕЧИВАЕМ ШИРИНУ 4 ТИКА ДЛЯ КАЖДОЙ ПОЗИЦИИ И ВПЛОТНОСТЬ
        min_width_ticks = 4
        
        # Корректируем позицию 0 (ниже цены) - ширина должна быть 4 тика
        tick_upper_0 = tick_lower_0 + min_width_ticks
        
        # 🚨 КРИТИЧЕСКИЙ ФИКС: Позиция 1 должна быть ВПЛОТНУЮ к позиции 0
        # Позиция 1 начинается там, где заканчивается позиция 0
        tick_lower_1 = tick_upper_0  # Вплотную к позиции 0
        tick_upper_1 = tick_lower_1 + min_width_ticks
        
        print(f"🔧 Фиксированная ширина позиций: позиция 0 [{tick_lower_0}, {tick_upper_0}] = {tick_upper_0 - tick_lower_0} тиков")
        print(f"🔧 Фиксированная ширина позиций: позиция 1 [{tick_lower_1}, {tick_upper_1}] = {tick_upper_1 - tick_lower_1} тиков")
        
        # Получаем человеческие цены для проверки
        raw_price_l0 = self.tick_to_raw_price_pool_t1_t0(tick_lower_0)
        raw_price_h0 = self.tick_to_raw_price_pool_t1_t0(tick_upper_0)
        human_price_low_0 = self._raw_price_pool_t1_t0_to_human_price_param_t1_t0(raw_price_l0)
        human_price_high_0 = self._raw_price_pool_t1_t0_to_human_price_param_t1_t0(raw_price_h0)
        
        raw_price_l1 = self.tick_to_raw_price_pool_t1_t0(tick_lower_1)
        raw_price_h1 = self.tick_to_raw_price_pool_t1_t0(tick_upper_1)
        human_price_low_1 = self._raw_price_pool_t1_t0_to_human_price_param_t1_t0(raw_price_l1)
        human_price_high_1 = self._raw_price_pool_t1_t0_to_human_price_param_t1_t0(raw_price_h1)
        
        print(f"  Позиция 0 (ниже): Тики [{tick_lower_0}, {tick_upper_0}] -> Цены: {human_price_low_0:.2f}-{human_price_high_0:.2f}")
        print(f"  Позиция 1 (выше): Тики [{tick_lower_1}, {tick_upper_1}] -> Цены: {human_price_low_1:.2f}-{human_price_high_1:.2f}")
        
        target_ranges[0] = {
            'tickLower': tick_lower_0,
            'tickUpper': tick_upper_0,
            'priceRangeHuman': (human_price_low_0, human_price_high_0),
            'position_type': 'below_price'
        }
        
        target_ranges[1] = {
            'tickLower': tick_lower_1,
            'tickUpper': tick_upper_1,
            'priceRangeHuman': (human_price_low_1, human_price_high_1),
            'position_type': 'above_price'
        }
        
        return target_ranges
    
    def calculate_target_ranges(self, center_human_price_param_t1_t0: Decimal) -> list:
        """
        Универсальный метод для расчета диапазонов в зависимости от выбранного режима.
        """
        if self.position_mode == '2_positions':
            return self.calculate_target_ranges_2_positions(center_human_price_param_t1_t0)
        else:
            return self.calculate_target_ranges_in_ticks(center_human_price_param_t1_t0)
    
    def _round_tick_down(self, tick: int, spacing: int) -> int:
        """Округляет тик вниз до ближайшего кратного tick_spacing."""
        return tick - (tick % spacing)
    
    def _round_tick_up(self, tick: int, spacing: int) -> int:
        """Округляет тик вверх до ближайшего кратного tick_spacing."""
        return tick + ((spacing - (tick % spacing)) % spacing)

    def _calculate_desired_amounts_for_position_from_capital(self, 
                                                tick_lower: int, 
                                                tick_upper: int, 
                                                current_price_param_t1_t0: Decimal,
                                                capital_usdt: Decimal,
                                                slot_index: int = None,
                                                is_smart_rebalance: bool = False) -> tuple[int, int]:
        """
        Рассчитывает ИДЕАЛЬНЫЕ суммы токенов для создания новой позиции ликвидности на основе капитала,
        без учета текущих балансов на кошельке.
        
        Args:
            tick_lower: Нижний тик позиции
            tick_upper: Верхний тик позиции
            current_price_param_t1_t0: Текущая цена в человеческом формате (param_T1/param_T0, например BTCB/USDT)
            capital_usdt: Капитал в USDT для этой позиции (~333$ для каждой позиции)
            slot_index: Индекс слота для позиции (0-2), определяет логику распределения токенов
            
        Returns:
            Кортеж (amount0_desired_raw, amount1_desired_raw) - идеальные суммы токенов
        """
        print(f"  Внутри _calculate_desired_amounts_for_position_from_capital: current_price_param_t1_t0 = {current_price_param_t1_t0}, capital_usdt = {capital_usdt}, slot_index = {slot_index}")
        
        # Получаем человеческие цены из тиков для информации в лог
        price_lower_human = self._tick_to_human_price_param_t1_t0(tick_lower)
        price_upper_human = self._tick_to_human_price_param_t1_t0(tick_upper)
        print(f"  Целевой слот {slot_index}: тики [{tick_lower}, {tick_upper}] (цены ~{price_lower_human:.2f}-{price_upper_human:.2f})")

        # 🎯 ПРАВИЛЬНАЯ ЛОГИКА: Сравниваем ЦЕНЫ, а не тики!
        price_lower_human = self._tick_to_human_price_param_t1_t0(tick_lower)
        price_upper_human = self._tick_to_human_price_param_t1_t0(tick_upper)
        
        # Определяем границы позиции по ЦЕНАМ
        min_position_price = min(price_lower_human, price_upper_human)
        max_position_price = max(price_lower_human, price_upper_human)
        
        print(f"    DEBUG: цена={current_price_param_t1_t0:.2f}, позиция=[{min_position_price:.2f}, {max_position_price:.2f}]")
        
        if current_price_param_t1_t0 < min_position_price:
            # Цена НИЖЕ позиции -> 100% BTCB
            print(f"    Позиция {slot_index}: цена {current_price_param_t1_t0:.2f} < {min_position_price:.2f} (НИЖЕ позиции) -> 100% BTCB")
            amount0_desired_raw = 0
            amount1_human = capital_usdt / current_price_param_t1_t0
            amount1_desired_raw = int(amount1_human * (Decimal(10) ** self.decimals1_for_calcs))
            
        elif current_price_param_t1_t0 > max_position_price:
            # Цена ВЫШЕ позиции -> 100% USDT
            print(f"    Позиция {slot_index}: цена {current_price_param_t1_t0:.2f} > {max_position_price:.2f} (ВЫШЕ позиции) -> 100% USDT")
            amount0_human = capital_usdt
            amount0_desired_raw = int(amount0_human * (Decimal(10) ** self.decimals0_for_calcs))
            amount1_desired_raw = 0
            
        else:
            # Цена ВНУТРИ позиции
            print(f"    ✅ Позиция {slot_index}: цена {current_price_param_t1_t0:.2f} ВНУТРИ [{min_position_price:.2f}, {max_position_price:.2f}] -> СМЕШАННОЕ соотношение")
            
            if is_smart_rebalance:
                print(f"    [УМНЫЙ РАСЧЕТ] Используем точные формулы для позиции внутри диапазона")
                
                # Простой расчет пропорции
                price_range = max_position_price - min_position_price
                price_position = (current_price_param_t1_t0 - min_position_price) / price_range
                
                # Чем ближе к верхней границе, тем больше USDT нужно
                usdt_ratio = price_position
                btcb_ratio = Decimal("1") - price_position
                
                amount0_human = capital_usdt * usdt_ratio
                amount1_human = (capital_usdt * btcb_ratio) / current_price_param_t1_t0
                
                amount0_desired_raw = int(amount0_human * (Decimal(10) ** self.decimals0_for_calcs))
                amount1_desired_raw = int(amount1_human * (Decimal(10) ** self.decimals1_for_calcs))
                
                print(f"    [РАСЧЕТ] price_position={price_position:.3f}, USDT={amount0_human:.2f}, BTCB={amount1_human:.8f}")
            else:
                # ⚡ ВСЕГДА используем точную формулу для цены внутри диапазона
                print(f"    [ТОЧНАЯ ФОРМУЛА] Расчет для цены внутри диапазона")
                
                # Простой расчет пропорции основанный на позиции цены в диапазоне
                price_range = max_position_price - min_position_price
                price_position = (current_price_param_t1_t0 - min_position_price) / price_range
                
                # Чем ближе к верхней границе, тем больше USDT нужно
                usdt_ratio = price_position
                btcb_ratio = Decimal("1") - price_position
                
                amount0_human = capital_usdt * usdt_ratio
                amount1_human = (capital_usdt * btcb_ratio) / current_price_param_t1_t0
                
                amount0_desired_raw = int(amount0_human * (Decimal(10) ** self.decimals0_for_calcs))
                amount1_desired_raw = int(amount1_human * (Decimal(10) ** self.decimals1_for_calcs))
                
                print(f"    [РАСЧЕТ] price_position={price_position:.3f}, USDT={amount0_human:.2f}, BTCB={amount1_human:.8f}")

        # Убеждаемся, что обе суммы больше 0, если они должны быть предоставлены
        # ВАЖНО: PancakeSwap V3 требует ненулевые значения для обоих токенов
        # Даже если позиция вне диапазона, нужен минимум 1 wei для каждого токена
            amount0_desired_raw = max(amount0_desired_raw, 1)
            amount1_desired_raw = max(amount1_desired_raw, 1)

        print(f"  Рассчитаны ИДЕАЛЬНЫЕ суммы для слота {slot_index}: {self.token0_for_calcs_symbol}={amount0_desired_raw}, {self.token1_for_calcs_symbol}={amount1_desired_raw}")
        return amount0_desired_raw, amount1_desired_raw
    async def _validate_nft_exists(self, nft_id: int) -> bool:
        """Проверяет существование NFT перед операциями"""
        try:
            self.nonf_pos_manager.functions.positions(nft_id).call()
            return True
        except Exception as e:
            if "Invalid token ID" in str(e):
                return False
            raise e

    def _cleanup_invalid_positions(self):
        """Очищает слоты с несуществующими NFT или без ликвидности"""
        cleaned = False
        for slot_idx, pos_data in enumerate(self.managed_positions_slots):
            if pos_data and 'nft_id' in pos_data:
                nft_id = pos_data['nft_id']
                try:
                    position = self.nonf_pos_manager.functions.positions(nft_id).call()
                    liquidity = position[7]  # liquidity is at index 7
                    if liquidity == 0:
                        print(f"Очищаем слот {slot_idx} (NFT {nft_id} без ликвидности)")
                        self.managed_positions_slots[slot_idx] = None
                        cleaned = True
                except Exception as e:
                    if "Invalid token ID" in str(e) or "execution reverted" in str(e).lower():
                        print(f"Очищаем слот {slot_idx} (NFT {nft_id} не существует)")
                        self.managed_positions_slots[slot_idx] = None
                        cleaned = True
                    else:
                        print(f"Ошибка при проверке NFT {nft_id}: {e}, очищаем слот для безопасности")
                        self.managed_positions_slots[slot_idx] = None
                        cleaned = True
        
        if cleaned:
            print(f"Очистка завершена, сохраняем состояние")
            self._save_state_to_file()

    async def _calculate_smart_position_ranges(self, current_price: Decimal, empty_slots: list) -> dict:
        """
        Рассчитывает умные диапазоны для пустых слотов, делает свапы и создает позиции.
        """
        print(f"\n🧠 Умный расчет и создание позиций для {len(empty_slots)} пустых слотов")
        
        # Используем разную логику в зависимости от режима
        if self.position_mode == '2_positions':
            return await self._calculate_smart_position_ranges_2_pos(current_price, empty_slots)
        
        # Получаем активные позиции для 3-позиционного режима
        active_positions = [slot for slot in self.managed_positions_slots if slot is not None]
        
        if not active_positions:
            print("Нет активных позиций, используем стандартные диапазоны")
            target_ranges = self.calculate_target_ranges(current_price)
            ranges_to_create = {slot_idx: target_ranges[slot_idx] for slot_idx in empty_slots if slot_idx < len(target_ranges)}
        else:
            # Рассчитываем диапазоны вплотную к существующим позициям
            all_tick_lowers = [pos['tickLower'] for pos in active_positions]
            all_tick_uppers = [pos['tickUpper'] for pos in active_positions]
            
            min_existing_tick = min(all_tick_lowers)
            max_existing_tick = max(all_tick_uppers)
            position_width_ticks = 4  # Фиксированная ширина 4 тиков (0.04%)
            
            ranges_to_create = {}
            
            if len(empty_slots) == 1:
                slot_idx = empty_slots[0]
                min_existing_price = self._tick_to_human_price_param_t1_t0(min_existing_tick)
                max_existing_price = self._tick_to_human_price_param_t1_t0(max_existing_tick)
                
                distance_to_min = abs(current_price - min_existing_price)
                distance_to_max = abs(current_price - max_existing_price)
                
                if distance_to_min < distance_to_max:
                    new_tick_upper = min_existing_tick
                    new_tick_lower = new_tick_upper - position_width_ticks
                    print(f"1 слот: создаем позицию НИЖЕ существующего диапазона")
                else:
                    new_tick_lower = max_existing_tick
                    new_tick_upper = new_tick_lower + position_width_ticks
                    print(f"1 слот: создаем позицию ВЫШЕ существующего диапазона")
                
                ranges_to_create[slot_idx] = {
                    'tickLower': self.align_tick_to_spacing(new_tick_lower),
                    'tickUpper': self.align_tick_to_spacing(new_tick_upper)
                }
                
            elif len(empty_slots) == 2:
                print("2 слота: создаем позиции ВПЛОТНУЮ по краям от существующей позиции")
                
                lower_tick_upper = min_existing_tick
                lower_tick_lower = lower_tick_upper - position_width_ticks
                upper_tick_lower = max_existing_tick
                upper_tick_upper = upper_tick_lower + position_width_ticks
                
                # ПРАВИЛЬНО: Просто присваиваем диапазоны слотам по порядку
                ranges_to_create[empty_slots[0]] = {
                    'tickLower': self.align_tick_to_spacing(lower_tick_lower),
                    'tickUpper': self.align_tick_to_spacing(lower_tick_upper)
                }
                ranges_to_create[empty_slots[1]] = {
                    'tickLower': self.align_tick_to_spacing(upper_tick_lower),
                    'tickUpper': self.align_tick_to_spacing(upper_tick_upper)
                }
        
        if not ranges_to_create:
            print("❌ Не удалось рассчитать диапазоны")
            return {}
        
        # Получаем текущие балансы
        wallet_usdt_raw = await self._get_token_balance_raw(self.token0_for_calcs)
        wallet_btcb_raw = await self._get_token_balance_raw(self.token1_for_calcs)
        
        wallet_usdt_human = Decimal(wallet_usdt_raw) / (Decimal(10) ** self.decimals0_for_calcs)
        wallet_btcb_human = Decimal(wallet_btcb_raw) / (Decimal(10) ** self.decimals1_for_calcs)
        
        print(f"💰 Текущие балансы: USDT=${wallet_usdt_human:.2f}, BTCB={wallet_btcb_human:.8f}")
        
        # Рассчитываем общую стоимость портфеля в USDT
        total_portfolio_value_usdc = wallet_usdt_human + (wallet_btcb_human * current_price)
        print(f"💰 Общая стоимость портфеля: ${total_portfolio_value_usdc:.2f}")

        # Делим капитал на количество СОЗДАВАЕМЫХ позиций
        capital_per_position = total_portfolio_value_usdc / Decimal(len(empty_slots))
        print(f"📊 Капитал на позицию: ${capital_per_position:.2f}")  

        # Рассчитываем требуемые токены для всех позиций
        total_usdt_needed = Decimal("0")
        total_btcb_needed = Decimal("0")

        # Делим токены между позициями если их несколько
        num_positions = len(ranges_to_create)
        if num_positions > 1:
            wallet_usdt_per_position = wallet_usdt_raw // num_positions
            wallet_btcb_per_position = wallet_btcb_raw // num_positions
        else:
            wallet_usdt_per_position = wallet_usdt_raw
            wallet_btcb_per_position = wallet_btcb_raw
            
        created_positions = {}
        for slot_idx, range_info in ranges_to_create.items():
            print(f"\n⚡ Подготовка позиции в слоте {slot_idx}:")
            print(f"   Тики: {range_info['tickLower']}-{range_info['tickUpper']}")
            
            # 🎯 БЫСТРЫЙ РАСЧЕТ: Актуальная цена → расчет → свап → создание
            try:
                # 1. Получаем актуальную цену
                fresh_price, _, _ = await self.get_current_pool_state()
                print(f"   💱 Актуальная цена: {fresh_price:.2f}")
                
                # 2. Определяем доступные токены для этой позиции
                available_usdt_raw = wallet_usdt_per_position
                available_btcb_raw = wallet_btcb_per_position
                available_usdc = Decimal(available_usdt_raw) / (Decimal(10) ** self.decimals0_for_calcs)
                available_cbbtc = Decimal(available_btcb_raw) / (Decimal(10) ** self.decimals1_for_calcs)
                
                print(f"   💰 Доступно для позиции: USDT=${available_usdc:.2f}, BTCB={available_cbbtc:.8f}")
                
                # 3. Рассчитываем максимальный капитал в USDT
                total_capital_usdt = available_usdc + (available_cbbtc * fresh_price)
                print(f"   📊 Общий капитал: ${total_capital_usdt:.2f}")
                
                # 4. Рассчитываем нужные токены для этой позиции
                required_amount0_raw, required_amount1_raw = self._calculate_desired_amounts_for_position_from_capital(
                    tick_lower=range_info['tickLower'],
                    tick_upper=range_info['tickUpper'],
                    current_price_param_t1_t0=fresh_price,
                    capital_usdt=total_capital_usdt,
                    slot_index=slot_idx,
                    is_smart_rebalance=True
                )
                
                required_usdc = Decimal(required_amount0_raw) / (Decimal(10) ** self.decimals0_for_calcs)
                required_cbbtc = Decimal(required_amount1_raw) / (Decimal(10) ** self.decimals1_for_calcs)
                
                print(f"   🎯 Нужно: USDT=${required_usdc:.2f}, BTCB={required_cbbtc:.8f}")
                
                # 5. Делаем свап если нужно
                swap_needed = False
                
                if required_usdc > available_usdc:
                    # Нужно больше USDT - продаем BTCB
                    usdt_deficit = required_usdc - available_usdc
                    btcb_to_sell = usdt_deficit / fresh_price
                    
                    if btcb_to_sell <= available_cbbtc:
                        print(f"   💱 Продаем {btcb_to_sell:.8f} BTCB -> ${usdt_deficit:.2f} USDT")
                        
                        swap_success = await self._execute_swap(
                            self.token1_for_calcs,  # BTCB
                            self.token0_for_calcs,  # USDT
                            int(btcb_to_sell * (Decimal(10) ** self.decimals1_for_calcs)),
                            int(usdt_deficit * Decimal("0.98") * (Decimal(10) ** self.decimals0_for_calcs)),
                            self.swap_pool_fee_tier  # Передаем правильный fee tier
                        )
                        
                        if swap_success:
                            await asyncio.sleep(1)
                            swap_needed = True
                        else:
                            print(f"   ❌ Свап не удался, используем что есть")
                            
                elif required_cbbtc > available_cbbtc:
                    # Нужно больше BTCB - продаем USDT
                    btcb_deficit = required_cbbtc - available_cbbtc
                    usdt_to_sell = btcb_deficit * fresh_price
                    
                    if usdt_to_sell <= available_usdc:
                        print(f"   💱 Продаем ${usdt_to_sell:.2f} USDT -> {btcb_deficit:.8f} BTCB")
                        
                        swap_success = await self._execute_swap(
                            self.token0_for_calcs,  # USDT
                            self.token1_for_calcs,  # BTCB
                            int(usdt_to_sell * (Decimal(10) ** self.decimals0_for_calcs)),
                            int(btcb_deficit * Decimal("0.98") * (Decimal(10) ** self.decimals1_for_calcs)),
                            self.swap_pool_fee_tier  # Передаем правильный fee tier
                        )
                        
                        if swap_success:
                            await asyncio.sleep(1)
                            swap_needed = True
                        else:
                            print(f"   ❌ Свап не удался, используем что есть")
                
                # 6. Получаем финальные балансы после свапа  
                if swap_needed:
                    final_usdt_raw = await self._get_token_balance_raw(self.token0_for_calcs)
                    final_btcb_raw = await self._get_token_balance_raw(self.token1_for_calcs)
                    
                    # ⚡ ПЕРЕСЧИТЫВАЕМ соотношение токенов после свапа с НОВОЙ ценой
                    print(f"   🔄 Пересчитываем соотношение токенов после свапа...")
                    fresh_price_after_swap, _, _ = await self.get_current_pool_state()
                    print(f"   💱 Цена после свапа: {fresh_price_after_swap:.2f}")
                    
                    # Рассчитываем максимальный доступный капитал
                    available_usdt_after = Decimal(final_usdt_raw) / (Decimal(10) ** self.decimals0_for_calcs)
                    available_btcb_after = Decimal(final_btcb_raw) / (Decimal(10) ** self.decimals1_for_calcs)
                    total_capital_after = available_usdt_after + (available_btcb_after * fresh_price_after_swap)
                    
                    # Пересчитываем нужные токены с НОВОЙ ценой
                    required_amount0_raw_new, required_amount1_raw_new = self._calculate_desired_amounts_for_position_from_capital(
                        tick_lower=range_info['tickLower'],
                        tick_upper=range_info['tickUpper'], 
                        current_price_param_t1_t0=fresh_price_after_swap,
                        capital_usdt=total_capital_after,
                        slot_index=slot_idx,
                        is_smart_rebalance=True
                    )
                    
                    # Используем НОВЫЕ расчеты
                    final_amount0_raw = min(required_amount0_raw_new, final_usdt_raw)
                    final_amount1_raw = min(required_amount1_raw_new, final_btcb_raw)
                    
                    print(f"   🎯 НОВЫЕ amounts после свапа: USDT={final_amount0_raw}, BTCB={final_amount1_raw}")
                
                # 7. Создаем позицию
                new_pos_info = await self._execute_add_liquidity(
                    slot_id=slot_idx,
                    tick_lower=range_info['tickLower'],
                    tick_upper=range_info['tickUpper'],
                    amount0_desired_raw=final_amount0_raw,
                    amount1_desired_raw=final_amount1_raw
                )
                
                if new_pos_info:
                    self.managed_positions_slots[slot_idx] = new_pos_info
                    created_positions[slot_idx] = range_info
                    print(f"   ✅ Позиция создана в слоте {slot_idx}")
                    
                    # Обновляем глобальные балансы для следующей позиции
                    wallet_usdt_raw = await self._get_token_balance_raw(self.token0_for_calcs)
                    wallet_btcb_raw = await self._get_token_balance_raw(self.token1_for_calcs)
                    wallet_usdt_human = Decimal(wallet_usdt_raw) / (Decimal(10) ** self.decimals0_for_calcs)
                    wallet_btcb_human = Decimal(wallet_btcb_raw) / (Decimal(10) ** self.decimals1_for_calcs)

                    print(f"   💰 Обновленные балансы: USDT=${wallet_usdt_human:.2f}, BTCB={wallet_btcb_human:.8f}")

                    # Пересчитываем доступное для следующих позиций
                    remaining_positions = len(ranges_to_create) - len(created_positions)
                    if remaining_positions > 0:
                        wallet_usdt_per_position = wallet_usdt_raw // remaining_positions
                        wallet_btcb_per_position = wallet_btcb_raw // remaining_positions
                        print(f"   📊 Доступно на позицию: USDT=${wallet_usdt_per_position / (10**self.decimals0_for_calcs):.2f}, BTCB={wallet_btcb_per_position / (10**self.decimals1_for_calcs):.8f}")
                else:
                    print(f"   ❌ Не удалось создать позицию в слоте {slot_idx}")
                    
            except Exception as e:
                print(f"   ❌ Ошибка при обработке слота {slot_idx}: {e}")
                continue
        
        # Сохраняем состояние
        self._save_state_to_file()
        
        return created_positions

    def analyze_rebalance_with_price(self, current_price: Decimal) -> bool:
        """
        Анализирует ребаланс с известной ценой.
        """
        # Используем разную логику в зависимости от режима
        if self.position_mode == '2_positions':
            return self._analyze_rebalance_2_positions(current_price)
        else:
            return self._analyze_rebalance_3_positions(current_price)
    
    def _analyze_rebalance_2_positions(self, current_price: Decimal) -> bool:
        """
        Асимметричная логика ребалансировки для 2-позиционного режима.
        """
        active_positions = [slot for slot in self.managed_positions_slots if slot is not None]
        empty_slots = [i for i, slot in enumerate(self.managed_positions_slots) if slot is None]
        
        print(f"📊 2-позиционный анализ: {len(active_positions)} активных, {len(empty_slots)} пустых")
        
        if len(active_positions) == 0:
            # Нет позиций - создаем обе
            print("🔄 Нет активных позиций - создаем обе позиции")
            self.positions_to_rebalance = 0
            self.rebalance_side = None
            return False  # Создание через smart rebalance
        
        if len(active_positions) == 1:
            # Одна позиция - создаем вторую
            pos = active_positions[0]
            current_tick = self.price_to_tick(self._human_price_param_t1_t0_to_raw_price_pool_t1_t0(current_price))
            
            # Определяем где находится активная позиция относительно цены
            if current_tick < pos['tickLower']:
                # Цена ниже позиции - позиция сверху, создаем снизу
                print("📍 Активная позиция выше цены, создаем позицию ниже")
                self.positions_to_rebalance = 0
                self.rebalance_side = None
                return False
            elif current_tick > pos['tickUpper']:
                # Цена выше позиции - позиция снизу, создаем сверху  
                print("📍 Активная позиция ниже цены, создаем позицию выше")
                self.positions_to_rebalance = 0
                self.rebalance_side = None
                return False
            else:
                # Цена внутри позиции - проверяем отклонение
                print("📍 Цена внутри активной позиции, создаем вторую позицию")
                self.positions_to_rebalance = 0
                self.rebalance_side = None
                return False
        
        # Обе позиции активны - проверяем нужен ли асимметричный ребаланс
        if len(active_positions) == 2:
            current_tick = self.price_to_tick(self._human_price_param_t1_t0_to_raw_price_pool_t1_t0(current_price))
            
            # Находим общие границы всех позиций
            all_tick_lowers = [pos['tickLower'] for pos in active_positions]
            all_tick_uppers = [pos['tickUpper'] for pos in active_positions]
            min_tick = min(all_tick_lowers)
            max_tick = max(all_tick_uppers)
            
            print(f"🔍 ДИАГНОСТИКА: Тик {current_tick}, общий диапазон [{min_tick} to {max_tick}]")
            
            # Рассчитываем отклонение от общих границ
            deviation_pct = Decimal("0")
            rebalance_direction = None
            
            if current_tick < min_tick:
                # Цена ниже всех позиций - нужно переместить верхнюю позицию вниз
                min_price = self._tick_to_human_price_param_t1_t0(min_tick)
                deviation_pct = ((min_price - current_price) / min_price) * 100
                rebalance_direction = "move_above_down"
                print(f"🔵 Цена НИЖЕ диапазона на {deviation_pct:.3f}%")
                
            elif current_tick > max_tick:
                # Цена выше всех позиций - нужно переместить нижнюю позицию вверх
                max_price = self._tick_to_human_price_param_t1_t0(max_tick)
                deviation_pct = ((current_price - max_price) / max_price) * 100
                rebalance_direction = "move_below_up"
                print(f"🔴 Цена ВЫШЕ диапазона на {deviation_pct:.3f}%")
                
            else:
                print(f"✅ Цена ВНУТРИ диапазона")
                deviation_pct = Decimal("0")
            
            print(f"📊 ТЕКУЩИЙ ТИК: {current_tick}")
            print(f"📊 ПОЗИЦИИ: {[(pos['tickLower'], pos['tickUpper']) for pos in active_positions]}")
            print(f"📊 МАКСИМАЛЬНОЕ ОТКЛОНЕНИЕ: {deviation_pct:.3f}%")
            
            # Логика ребалансировки с двумя порогами
            if abs(deviation_pct) >= Decimal("0.04"):
                # Полный ребаланс при отклонении ≥ 0.04%
                print(f"🚨 Отклонение |{deviation_pct:.3f}|% ≥ 0.04% → ПОЛНЫЙ РЕБАЛАНС")
                self.positions_to_rebalance = 2  # Закрываем обе позиции
                self.rebalance_side = None
                return True
            elif abs(deviation_pct) >= Decimal("0.02"):
                # Асимметричный ребаланс при отклонении ≥ 0.02%
                print(f"🚨 Отклонение |{deviation_pct:.3f}|% ≥ 0.02% → Асимметричный ребаланс")
                if rebalance_direction == "move_below_up":
                    # Перемещаем нижнюю позицию вверх (цена выше диапазона)
                    self.positions_to_rebalance = 1
                    self.rebalance_side = "below"
                    print("🔄 Перемещаем нижнюю позицию вверх")
                elif rebalance_direction == "move_above_down":
                    # Перемещаем верхнюю позицию вниз (цена ниже диапазона)
                    self.positions_to_rebalance = 1  
                    self.rebalance_side = "above"
                    print("🔄 Перемещаем верхнюю позицию вниз")
                return True
            else:
                print(f"✅ Отклонение |{deviation_pct:.3f}|% < 0.02% - ребаланс не нужен")
                self.positions_to_rebalance = 0
                self.rebalance_side = None
                return False
        
        return False
    
    def _analyze_rebalance_3_positions(self, current_price: Decimal) -> bool:
        """
        Стандартная логика ребалансировки для 3-позиционного режима.
        """
        # Сначала проверяем активные позиции для анализа цены
        active_positions = [slot for slot in self.managed_positions_slots if slot is not None]
        
        if len(active_positions) == 0:
            # Нет активных позиций - нужен полный ребаланс
            self.positions_to_rebalance = self.num_managed_positions
            self.rebalance_side = None
            print("🔄 Нет активных позиций - требуется полный ребаланс")
            return True
        
        # Проверяем пустые слоты только ПОСЛЕ анализа цены
        empty_slots = [i for i, slot in enumerate(self.managed_positions_slots) if slot is None]        

        if len(active_positions) < 2:
            # СНАЧАЛА проверяем отклонение, даже с 1 позицией!
            lowest_tick = min([pos['tickLower'] for pos in active_positions])
            highest_tick = max([pos['tickUpper'] for pos in active_positions])
            
            lowest_price = self._tick_to_human_price_param_t1_t0(lowest_tick)
            highest_price = self._tick_to_human_price_param_t1_t0(highest_tick)
            
            min_boundary_price = min(lowest_price, highest_price)
            max_boundary_price = max(lowest_price, highest_price)
            
            # Рассчитываем отклонение
            deviation_pct = Decimal("0")
            if current_price > max_boundary_price:
                deviation_pct = ((current_price - max_boundary_price) / max_boundary_price) * 100
                print(f"🔴 1 позиция: Цена выше на {deviation_pct:.3f}%")
            elif current_price < min_boundary_price:
                deviation_pct = ((min_boundary_price - current_price) / min_boundary_price) * 100
                print(f"🔵 1 позиция: Цена ниже на {deviation_pct:.3f}%")
            
            # ПРОВЕРЯЕМ ПОРОГ даже для 1 позиции
            if deviation_pct >= Decimal("0.19"):
                print(f"🚨 1 позиция: Отклонение {deviation_pct:.3f}% ≥ 0.19% → ПОЛНЫЙ РЕБАЛАНС")
                self.positions_to_rebalance = self.num_managed_positions
                self.rebalance_side = None
                return True
            
            # Если отклонение меньше, то создаем новые позиции только если не хватает активных позиций
            if len(empty_slots) > 0 and len(active_positions) < self.num_managed_positions:
                print(f"📝 1 позиция: Отклонение {deviation_pct:.3f}% < 0.19% + {len(empty_slots)} пустых слотов - создаем новые позиции")
                self.positions_to_rebalance = 0
                self.rebalance_side = None
                return False
            else:
                print(f"📝 1 позиция: Отклонение {deviation_pct:.3f}% < 0.19%, позиций достаточно ({len(active_positions)}/{self.num_managed_positions})")
                self.positions_to_rebalance = 0
                self.rebalance_side = None
                return False

        # Находим границы общего диапазона
        all_tick_lowers = [pos['tickLower'] for pos in active_positions]
        all_tick_uppers = [pos['tickUpper'] for pos in active_positions]
        
        lowest_tick = min(all_tick_lowers)
        highest_tick = max(all_tick_uppers)
        
        # Конвертируем границы в цены
        lowest_price = self._tick_to_human_price_param_t1_t0(lowest_tick)
        highest_price = self._tick_to_human_price_param_t1_t0(highest_tick)
        
        # Убеждаемся, что границы правильно упорядочены
        min_boundary_price = min(lowest_price, highest_price)
        max_boundary_price = max(lowest_price, highest_price)
        
        print(f"📊 Анализ: цена {current_price:.2f}, диапазон [{min_boundary_price:.2f} - {max_boundary_price:.2f}]")
        
        # Рассчитываем отклонение цены от границ
        deviation_pct = Decimal("0")
        deviation_side = None
        
        if current_price > max_boundary_price:
            # Цена выше верхней границы
            deviation_pct = ((current_price - max_boundary_price) / max_boundary_price) * 100
            deviation_side = "above"
            print(f"🔴 Цена выше диапазона на {deviation_pct:.3f}%")
            
        elif current_price < min_boundary_price:
            # Цена ниже нижней границы  
            deviation_pct = ((min_boundary_price - current_price) / min_boundary_price) * 100
            deviation_side = "below"
            print(f"🔵 Цена ниже диапазона на {deviation_pct:.3f}%")
            
        else:
            # Цена внутри диапазона - проверяем пустые слоты
            if len(empty_slots) > 0 and len(active_positions) < self.num_managed_positions:
                print(f"✅ Цена внутри диапазона + {len(empty_slots)} пустых слотов - создаем новые позиции")
                self.positions_to_rebalance = 0
                self.rebalance_side = None
                return False  # НЕ делаем ребаланс, а создаем новые позиции
            else:
                print(f"✅ Цена внутри диапазона - ребаланс не требуется (активных позиций: {len(active_positions)}/{self.num_managed_positions})")
                self.positions_to_rebalance = 0
                self.rebalance_side = None
                return False
        
        # === ПРИМЕНЯЕМ ЛОГИКУ РЕБАЛАНСА ===
        
        if deviation_pct >= Decimal("0.19"):
            # Полный ребаланс
            print(f"🚨 Отклонение {deviation_pct:.3f}% ≥ 0.19% → ПОЛНЫЙ РЕБАЛАНС")
            self.positions_to_rebalance = self.num_managed_positions
            self.rebalance_side = None
            return True
            
        elif deviation_pct >= Decimal("0.08"):
            # Ребаланс 2 позиций
            print(f"🟠 Отклонение {deviation_pct:.3f}% ≥ 0.04% → РЕБАЛАНС 2 ПОЗИЦИЙ")
            self.positions_to_rebalance = min(2, len(active_positions))
            self.rebalance_side = "lower" if deviation_side == "above" else "upper"
            return True
            
        elif deviation_pct >= Decimal("0.02"):
            # Ребаланс 1 позиции
            print(f"🟡 Отклонение {deviation_pct:.3f}% ≥ 0.02% → РЕБАЛАНС 1 ПОЗИЦИИ")
            self.positions_to_rebalance = 1
            self.rebalance_side = "lower" if deviation_side == "above" else "upper"
            return True
            
        else:
            # Ребаланс не нужен
            print(f"✅ Отклонение {deviation_pct:.3f}% < 0.02% → ребаланс не требуется")
            self.positions_to_rebalance = 0
            self.rebalance_side = None
            return False   

    async def decide_and_manage_liquidity(self, latest_ohlcv_features: pd.DataFrame):
        """
        Основная функция управления ликвидностью.
        Упрощенная версия без машинного обучения.
        """
        try:
            print("\n=== Управление ликвидностью ===")
            
            # Получаем текущее состояние пула
            current_price, current_tick, sqrt_price_x96 = await self.get_current_pool_state()
            
            # 🚨 КРИТИЧЕСКАЯ ПРОВЕРКА: Валидность цены
            if current_price is None or current_tick is None:
                print("🚨 ОШИБКА: Не удалось получить актуальную цену пула! Пропускаем итерацию.")
                return
                
            print(f"💰 АКТУАЛЬНАЯ ЦЕНА ПУЛА: ${current_price:.4f} (тик: {current_tick})")

            # Очищаем несуществующие NFT
            self._cleanup_invalid_positions()

            # Инициализируем или обновляем управляемые позиции
            await self._initialize_or_update_managed_positions()

            # Инициализируем атрибуты для ребаланса
            if not hasattr(self, 'positions_to_rebalance'):
                self.positions_to_rebalance = 0
            if not hasattr(self, 'rebalance_side'):
                self.rebalance_side = None

            # Анализируем необходимость ребаланса с текущей ценой
            print(f"🔍 АНАЛИЗ РЕБАЛАНСА с ценой ${current_price:.4f}")
            rebalance_needed = self.analyze_rebalance_with_price(current_price)

            if rebalance_needed:
                print(f"\nТребуется ребаланс {self.positions_to_rebalance} позиций со стороны {self.rebalance_side if self.rebalance_side else 'все'}")
                
                if self.positions_to_rebalance == self.num_managed_positions:
                    print("Выполняем полный ребаланс всех позиций...")
                    await self._perform_full_rebalance(current_price)
                    return
                elif self.positions_to_rebalance > 0:
                    print(f"Выполняем частичный ребаланс {self.positions_to_rebalance} позиций...")
                    await self._perform_partial_rebalance(current_price, self.positions_to_rebalance, self.rebalance_side)
                    return
            else:
                # Если нет необходимости в ребалансе, заполняем пустые слоты, если есть
                print("Проверяем наличие пустых слотов для новых позиций...")

                # Получаем баланс USDT
                wallet_usdt_raw = await self._get_token_balance_raw(self.token0_for_calcs)
                wallet_usdt_human = Decimal(wallet_usdt_raw) / (Decimal(10) ** self.decimals0_for_calcs)
                
                # Подсчитываем количество пустых слотов и активных позиций
                empty_slots = [i for i, slot in enumerate(self.managed_positions_slots) if slot is None]
                active_slots = [slot for slot in self.managed_positions_slots if slot is not None]
                empty_slots_count = len(empty_slots)
                active_positions_count = len(active_slots)
                
                if empty_slots_count == 0:
                    print("Нет пустых слотов для создания новых позиций.")
                    return
                
                if active_positions_count >= self.num_managed_positions:
                    print(f"Достаточно активных позиций ({active_positions_count}/{self.num_managed_positions}). Новые позиции не создаем.")
                    return
                
                print(f"🎯 Обнаружено {empty_slots_count} пустых слотов, {active_positions_count} активных позиций")
                print(f"   Создаем новые позиции в слотах: {empty_slots}")
                
                # УМНЫЙ расчет диапазонов на основе существующих позиций
                smart_ranges = await self._calculate_smart_position_ranges(current_price, empty_slots)
                
                if not smart_ranges:
                    print("Не удалось рассчитать умные диапазоны для пустых слотов")
                    
                # Все остальные операции уже выполнены в _calculate_smart_position_ranges
                
        except Exception as e:
            print(f"Ошибка при управлении ликвидностью: {e}")
            import traceback
            traceback.print_exc()

    async def _perform_full_rebalance(self, target_price: Decimal):
        """
        Выполняет полный ребаланс всех позиций.
        Закрывает все существующие позиции и создает новые с центром в target_price.
        """
        print("\n=== ПОЛНЫЙ РЕБАЛАНС ПОЗИЦИЙ ===")
        
        # Получаем актуальную цену из пула
        current_price, _, _ = await self.get_current_pool_state()
        print(f"Текущая цена пула: {current_price}")
        
        # Закрываем все существующие позиции через multicall
        print("Закрытие всех существующих позиций через multicall...")
        positions_to_close = []
        
        # Собираем позиции из managed_positions_slots
        for slot_idx, pos_data in enumerate(self.managed_positions_slots):
            if pos_data and 'nft_id' in pos_data:
                nft_id = pos_data['nft_id']
                position_info = await self.get_position_info(nft_id)
                if position_info and 'error' not in position_info:
                    positions_to_close.append((slot_idx, nft_id, position_info))
                    print(f"  Позиция для закрытия: слот {slot_idx}, NFT {nft_id}")
        
        # Ищем и добавляем осиротевшие позиции
        orphaned_positions = await self.find_orphaned_positions()
        for orphaned_pos in orphaned_positions:
            nft_id = orphaned_pos['nft_id']
            position_info = await self.get_position_info(nft_id)
            if position_info and 'error' not in position_info:
                positions_to_close.append((-1, nft_id, position_info))  # slot_id = -1 для орфанов
                print(f"  🚨 Осиротевшая позиция для закрытия: NFT {nft_id}")
        
        if positions_to_close:
            success = await self._execute_remove_liquidity_multicall(positions_to_close)
            if not success:
                print("Ошибка при закрытии позиций через multicall. Используем обычное закрытие.")
                # Fallback к обычному методу
                for slot_idx, pos_data in enumerate(self.managed_positions_slots):
                    if pos_data and 'nft_id' in pos_data:
                        nft_id = pos_data['nft_id']
                        print(f"Закрытие позиции в слоте {slot_idx} (NFT ID: {nft_id})...")
                        await self._execute_remove_liquidity_multicall([(slot_idx, nft_id, pos_data)])
        
        # Сбрасываем информацию о позициях
        self.managed_positions_slots = [None] * len(self.managed_positions_slots)

        # УМНОЕ ожидание возврата токенов на баланс
        balance_result = await self._wait_for_tokens_return(expected_min_value=Decimal("10"))
        if not balance_result:
            print("❌ Не дождались возврата токенов. Отмена ребаланса.")
            return
            
        wallet_usdt_raw, wallet_btcb_raw, total_portfolio_value_usdc = balance_result
        
        # Рассчитываем человеческие значения балансов
        wallet_usdt_human = Decimal(wallet_usdt_raw) / (Decimal(10) ** self.decimals0_for_calcs)
        wallet_btcb_human = Decimal(wallet_btcb_raw) / (Decimal(10) ** self.decimals1_for_calcs)
        
        print(f"Текущие балансы после возврата:")
        print(f"USDT: ${wallet_usdt_human:.2f}")
        print(f"BTCB: {wallet_btcb_human:.8f} (${wallet_btcb_human * current_price:.2f})")
        print(f"Общая стоимость портфеля: ${total_portfolio_value_usdc:.2f}")
        
        # Проверяем, достаточно ли средств для создания новых позиций
        if total_portfolio_value_usdc < Decimal("1"):
            print(f"Недостаточно средств для создания новых позиций: ${total_portfolio_value_usdc:.2f}")
            return
        
        # Ребалансируем портфель к соотношению 1:1
        print("\n=== Ребаланс портфеля к соотношению 1:1 ===")
        target_value_per_token = total_portfolio_value_usdc / Decimal("2")
        current_usdt_value = wallet_usdt_human
        current_btcb_value = wallet_btcb_human * current_price
        
        print(f"Текущие значения:")
        print(f"USDT: ${current_usdt_value:.2f}")
        print(f"BTCB: ${current_btcb_value:.2f} (={wallet_btcb_human:.8f} BTCB)")
        print(f"Целевое значение на токен: ${target_value_per_token:.2f}")
        
        if current_usdt_value > target_value_per_token:
            # Нужно купить BTCB
            usdt_to_swap = current_usdt_value - target_value_per_token
            print(f"Свап {usdt_to_swap:.2f} USDT -> BTCB")
            
            amount_in_raw = int(usdt_to_swap * (Decimal(10) ** self.decimals0_for_calcs))
            amount_out_min_raw = int((usdt_to_swap / current_price * Decimal("0.99")) * (Decimal(10) ** self.decimals1_for_calcs))
            
            swap_success = await self._execute_swap(
                self.token0_for_calcs,  # USDT
                self.token1_for_calcs,  # BTCB
                amount_in_raw,
                amount_out_min_raw,
                self.swap_pool_fee_tier  # Передаем правильный fee tier
            )
            if not swap_success:
                print("Ошибка при свапе USDT -> BTCB. Отмена ребаланса.")
                return
            
        elif current_btcb_value > target_value_per_token:
            # Нужно купить USDT
            btcb_value_to_swap = current_btcb_value - target_value_per_token
            btcb_amount_to_swap = btcb_value_to_swap / current_price
            print(f"Свап {btcb_amount_to_swap:.8f} BTCB -> USDT")
            
            amount_in_raw = int(btcb_amount_to_swap * (Decimal(10) ** self.decimals1_for_calcs))
            amount_out_min_raw = int(btcb_value_to_swap * Decimal("0.99") * (Decimal(10) ** self.decimals0_for_calcs))
            
            swap_success = await self._execute_swap(
                self.token1_for_calcs,  # BTCB
                self.token0_for_calcs,  # USDT
                amount_in_raw,
                amount_out_min_raw,
                self.swap_pool_fee_tier  # Передаем правильный fee tier
            )
            if not swap_success:
                print("Ошибка при свапе BTCB -> USDT. Отмена ребаланса.")
                return
        
        # Получаем обновленные балансы после ребаланса
        wallet_usdt_raw = await self._get_token_balance_raw(self.token0_for_calcs)
        wallet_btcb_raw = await self._get_token_balance_raw(self.token1_for_calcs)
        
        wallet_usdt_human = Decimal(wallet_usdt_raw) / (Decimal(10) ** self.decimals0_for_calcs)
        wallet_btcb_human = Decimal(wallet_btcb_raw) / (Decimal(10) ** self.decimals1_for_calcs)
        
        print(f"\nБалансы после ребаланса:")
        print(f"USDT: ${wallet_usdt_human:.2f}")
        print(f"BTCB: ${(wallet_btcb_human * current_price):.2f} (={wallet_btcb_human:.8f} BTCB)")
        
        # Рассчитываем целевые диапазоны для новых позиций с центром в текущей цене
        target_ranges = self.calculate_target_ranges(current_price)
        if not target_ranges:
            print("Не удалось рассчитать целевые диапазоны. Отмена ребаланса.")
            return
        
        # Равномерно распределяем капитал между позициями
        capital_per_position = total_portfolio_value_usdc / Decimal(len(self.managed_positions_slots))
        print(f"Распределение капитала: {total_portfolio_value_usdc} USDT = {capital_per_position} USDT на каждую из {len(self.managed_positions_slots)} позиций")
        
        # Создаем новые позиции точно так же, как при первом запуске
        print("\n=== Создание новых позиций после полного ребаланса ===")
        
        # Сначала создаем центральную позицию (слот 1)
        if len(target_ranges) > 1:
            target_range_info = target_ranges[1]
            print(f"Создание центральной позиции в слоте 1:")
            print(f"Целевой диапазон: {target_range_info['tickLower']} - {target_range_info['tickUpper']}")
            
            amount0_desired_raw, amount1_desired_raw = self._calculate_desired_amounts_for_position_from_capital(
                tick_lower=target_range_info['tickLower'],
                tick_upper=target_range_info['tickUpper'],
                current_price_param_t1_t0=current_price,
                capital_usdt=capital_per_position,
                slot_index=1
            )
            
            if amount0_desired_raw > 0 or amount1_desired_raw > 0:
                new_pos_info = await self._execute_add_liquidity_fast(
                    slot_id=1,
                    tick_lower=target_range_info['tickLower'],
                    tick_upper=target_range_info['tickUpper'],
                    capital_usdt=capital_per_position,
                    is_smart_rebalance=True  # ← ВАЖНО!
                )
                if new_pos_info:
                    self.managed_positions_slots[1] = new_pos_info
                    print(f"Центральная позиция успешно создана в слоте 1")
                else:
                    print(f"Не удалось создать центральную позицию в слоте 1")
            else:
                print(f"Недостаточно токенов для создания центральной позиции в слоте 1")
        
        # Затем создаем остальные позиции в правильном порядке: 0, 2
        remaining_slots = [0, 2]
        for slot_idx in remaining_slots:
            if slot_idx < len(target_ranges):
                target_range_info = target_ranges[slot_idx]
                print(f"Создание новой позиции в слоте {slot_idx}:")
                print(f"Целевой диапазон: {target_range_info['tickLower']} - {target_range_info['tickUpper']}")
                
                amount0_desired_raw, amount1_desired_raw = self._calculate_desired_amounts_for_position_from_capital(
                    tick_lower=target_range_info['tickLower'],
                    tick_upper=target_range_info['tickUpper'],
                    current_price_param_t1_t0=current_price,
                    capital_usdt=capital_per_position,
                    slot_index=slot_idx
                )
                
                if amount0_desired_raw > 0 or amount1_desired_raw > 0:
                    new_pos_info = await self._execute_add_liquidity_fast(
                        slot_id=slot_idx,
                        tick_lower=target_range_info['tickLower'],
                        tick_upper=target_range_info['tickUpper'],
                        capital_usdt=capital_per_position,
                        is_smart_rebalance=True
                    )
                    if new_pos_info:
                        self.managed_positions_slots[slot_idx] = new_pos_info
                        print(f"Позиция успешно создана в слоте {slot_idx}")
                    else:
                        print(f"Не удалось создать позицию в слоте {slot_idx}")
                else:
                    print(f"Недостаточно токенов для создания позиции в слоте {slot_idx}")
        
        # Обновляем статус позиций и сохраняем состояние
        await self._update_managed_positions_status()
        self._save_state_to_file()
        print("\nРебаланс позиций завершен.")

    async def _perform_partial_rebalance(self, target_price: Decimal, positions_count: int, rebalance_side: str):
        """
        Выполняет частичный ребаланс - удаляет и пересоздает указанное количество позиций.
        В 2-позиционном режиме поддерживает асимметричный ребаланс.
        """
        if self.position_mode == '2_positions' and rebalance_side:
            await self._perform_asymmetric_rebalance_2_positions(target_price, rebalance_side)
            return
        
        # Стандартная логика для 3-позиционного режима
        """
        Выполняет частичный ребаланс 1-2 позиций.
        Закрывает дальние позиции и создает новые вплотную к границам на 0.04%.
        
        Args:
            target_price: Текущая цена пула
            positions_count: Количество позиций для ребаланса (1 или 2)
            rebalance_side: Сторона ребаланса ("lower" или "upper")
        """
        print(f"\n=== ЧАСТИЧНЫЙ РЕБАЛАНС {positions_count} ПОЗИЦИЙ ({rebalance_side}) ===")
        
        # Проверяем валидность всех NFT и очищаем невалидные
        valid_positions = []
        for slot_idx, pos_data in enumerate(self.managed_positions_slots):
            if pos_data and 'nft_id' in pos_data:
                nft_id = pos_data['nft_id']
                if await self._validate_nft_exists(nft_id):
                    valid_positions.append((slot_idx, pos_data))
                else:
                    print(f"NFT {nft_id} не существует. Очищаем слот {slot_idx}")
                    self.managed_positions_slots[slot_idx] = None

        if not valid_positions:
            print("Нет валидных позиций для частичного ребаланса. Выполняем полный ребаланс.")
            await self._perform_full_rebalance(target_price)
            return

        # Получаем текущие активные позиции
        active_positions = valid_positions
        
        # Определяем позиции для закрытия
        if rebalance_side == "lower":
            # Цена ВЫШЕ позиций - закрываем самые НИЖНИЕ позиции (максимальные тики = минимальные цены)
            sorted_positions = sorted(active_positions, key=lambda p: p[1]['tickUpper'], reverse=True)
            positions_to_close = sorted_positions[:positions_count]
            print(f"Закрываем {positions_count} нижних позиций")
            
        elif rebalance_side == "upper":
            # Цена НИЖЕ позиций - закрываем самые ВЕРХНИЕ позиции (минимальные тики = максимальные цены)
            sorted_positions = sorted(active_positions, key=lambda p: p[1]['tickUpper'])  # БЕЗ reverse!
            positions_to_close = sorted_positions[:positions_count]
            print(f"Закрываем {positions_count} верхних позиций")
            
        else:
            print(f"Неизвестная сторона ребаланса: {rebalance_side}")
            return
        
        # Получаем информацию о позициях для multicall
        positions_with_info = []
        for slot_idx, pos_data in positions_to_close:
            nft_id = pos_data['nft_id']
            position_info = await self.get_position_info(nft_id)
            if position_info and 'error' not in position_info:
                positions_with_info.append((slot_idx, nft_id, position_info))
                print(f"  Позиция для закрытия: слот {slot_idx}, NFT {nft_id}")
        
        if not positions_with_info:
            print("Не удалось получить информацию о позициях. Отмена ребаланса.")
            return
        
        # Выполняем multicall для закрытия позиций
        success = await self._execute_remove_liquidity_multicall(positions_with_info)
        if not success:
            print("Не удалось закрыть позиции через multicall. Отмена ребаланса.")
            return
        
        # Очищаем закрытые слоты
        closed_slots = [slot_idx for slot_idx, _, _ in positions_with_info]
        for slot_idx in closed_slots:
            self.managed_positions_slots[slot_idx] = None

        # Ждем возврата токенов
        balance_result = await self._wait_for_tokens_return(expected_min_value=Decimal("5"))
        if not balance_result:
            print("❌ Не дождались возврата токенов. Отмена частичного ребаланса.")
            return
            
        wallet_usdt_raw, wallet_btcb_raw, total_value_usdc = balance_result
        
        # Определяем где создавать новые позиции
        remaining_positions = [(idx, pos) for idx, pos in enumerate(self.managed_positions_slots) if pos is not None]
        
        if not remaining_positions:
            print("Нет оставшихся позиций. Выполняем полный ребаланс.")
            await self._perform_full_rebalance(target_price)
            return
        
        # Находим границы оставшихся позиций
        remaining_ticks_lower = [pos['tickLower'] for _, pos in remaining_positions]
        remaining_ticks_upper = [pos['tickUpper'] for _, pos in remaining_positions]
        
        if rebalance_side == "lower":
            # Создаем позицию НИЖЕ самой нижней оставшейся ВПЛОТНУЮ
            lowest_tick = min(remaining_ticks_lower)
            new_tick_upper = lowest_tick  # Строго вплотную, без гэпа
            new_tick_lower = new_tick_upper - 4  # Диапазон 0.04% = 4 тика
            
        else:  # upper
            # Создаем позицию ВЫШЕ самой верхней оставшейся ВПЛОТНУЮ
            highest_tick = max(remaining_ticks_upper)
            new_tick_lower = highest_tick  # Строго вплотную, без гэпа
            new_tick_upper = new_tick_lower + 4  # Диапазон 0.04% = 4 тика
        
        # Выравниваем тики по spacing
        new_tick_lower = self.align_tick_to_spacing(new_tick_lower, "down")
        new_tick_upper = self.align_tick_to_spacing(new_tick_upper, "up")
        
        print(f"Создаем новую позицию: тики [{new_tick_lower}, {new_tick_upper}]")
        
        # Определяем количество пустых слотов
        empty_slots_count = len([slot for slot in self.managed_positions_slots if slot is None])
        
        # Создаем новую позицию
        new_slot_idx = closed_slots[0]  # Используем первый закрытый слот
        
        # Распределяем капитал в зависимости от количества пустых слотов
        if empty_slots_count == 2:
            capital_for_position = total_value_usdc / Decimal(2)  # Делим на 2 если пустых слотов ровно 2
            print(f"  Капитал: {total_value_usdc:.2f} USDT / 2 слота = {capital_for_position:.2f} USDT на позицию")
        else:
            capital_for_position = total_value_usdc  # Весь капитал если пустых слотов 1 (или 3 - полный ребаланс)
            print(f"  Капитал: {total_value_usdc:.2f} USDT весь на позицию ({empty_slots_count} пустых слотов)")
        
        new_pos_info = await self._execute_add_liquidity_fast(
            slot_id=new_slot_idx,
            tick_lower=new_tick_lower,
            tick_upper=new_tick_upper,
            capital_usdt=capital_for_position,
            is_smart_rebalance=True
        )
        
        if new_pos_info:
            self.managed_positions_slots[new_slot_idx] = new_pos_info
            print(f"✅ Позиция создана в слоте {new_slot_idx}")
        else:
            print(f"❌ Не удалось создать позицию в слоте {new_slot_idx}")
        
        # Сохраняем состояние
        self._save_state_to_file()    

    async def _get_token_balance_raw(self, token_address: str) -> int:
        """ Получает сырой баланс токена (в минимальных единицах). """
        checksum_token_address = Web3.to_checksum_address(token_address)
        erc20_abi_balance_only = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]
        token_contract = self.w3.eth.contract(address=checksum_token_address, abi=erc20_abi_balance_only)
        try:
            balance = token_contract.functions.balanceOf(self.signer_address).call()
            return balance
        except Exception as e:
            print(f"  Ошибка при получении баланса токена {token_address}: {e}")
            return 0

    async def _execute_swap(self, token_in_addr: str, token_out_addr: str, amount_in_raw: int, 
                            amount_out_min_raw: int, pool_fee_for_swap: int = 100):
        """
        Выполняет свап токенов через Universal Router.
        
        Args:
            token_in_addr: Адрес входящего токена
            token_out_addr: Адрес исходящего токена
            amount_in_raw: Сумма входящего токена в сырых единицах
            amount_out_min_raw: Минимальная сумма исходящего токена
            pool_fee_for_swap: Fee Tier пула для свапа в ppm (500 = 0.05%, 3000 = 0.3%)
            
        Returns:
            tuple[bool, str|None]: (успех, хеш транзакции)
        """
        # Проверяем наличие адреса роутера для свапа
        if not self.pancakeswap_router_address:
            print("  Ошибка: Адрес роутера для свапа не указан. Отмена свапа.")
            return False, None
        
        wallet_address = self.signer_address
        router_address = Web3.to_checksum_address(self.pancakeswap_router_address)
        token_in = Web3.to_checksum_address(token_in_addr)
        token_out = Web3.to_checksum_address(token_out_addr)
        
        # CRITICAL: Проверяем и устанавливаем approve для токена через Permit2
        # Двухэтапный approve: ERC20.approve(Permit2) + Permit2.approve(Router)
        approve_success = await self._check_and_approve_token_for_permit2(token_in, router_address, amount_in_raw)
        
        if not approve_success:
            print(f"  Не удалось установить approve для токена {token_in}. Отмена свапа.")
            return False, None
        
        # Устанавливаем дедлайн на 20 минут вперед
        deadline = int(time.time()) + 1200  # 20 минут от текущего времени
        
        # Создаем команды для Universal Router
        # Для V3_SWAP_EXACT_IN нужен только один байт
        commands = bytes([UNIVERSAL_ROUTER_COMMANDS["V3_SWAP_EXACT_IN"]])
        
        # Создаем путь свапа для V3
        # Токены идут в порядке in->out с fee посередине
        # Создаем массив байт с адресами и fee
        path_bytes = bytes.fromhex(token_in[2:].lower()) + int(pool_fee_for_swap).to_bytes(3, 'big') + bytes.fromhex(token_out[2:].lower())
        
        # Кодируем параметры для V3_SWAP_EXACT_IN
        # recipient, amountIn, amountOutMinimum, path, payerIsUser
        v3_params = [
            wallet_address,  # recipient
            amount_in_raw,   # amountIn
            amount_out_min_raw,  # amountOutMinimum
            path_bytes,      # path
            True             # payerIsUser - этот параметр важен!
        ]
        
        v3_params_encoded = encode(
            ['address', 'uint256', 'uint256', 'bytes', 'bool'],
            v3_params
        )
        
        # Кодируем аргументы для execute с deadline
        function_selector = binascii.unhexlify(EXECUTE_SELECTOR[2:])
        
        # Создаем byte-строки для commands и inputs
        commands_bytes = commands
        inputs_bytes = [v3_params_encoded]
        
        # Кодируем команды и входные данные для execute
        execute_data = encode(
            ['bytes', 'bytes[]', 'uint256'],
            [commands_bytes, inputs_bytes, deadline]
        )
        
        # Общие закодированные данные для вызова
        data = function_selector + execute_data
        
        try:
            print(f"  Отправляем свап: {amount_in_raw} сырых {token_in} -> мин {amount_out_min_raw} сырых {token_out}")
            # Подготавливаем транзакцию с прямым вызовом
            base_gas_price = await self._get_gas_price()
            # Минимум 0.1 Gwei для BNB Chain
            max_priority_fee = max(100000000, int(base_gas_price * 0.5))
            
            tx = {
                "from": wallet_address,
                "to": router_address,
                "gas": 1000000,  # Увеличиваем лимит газа для сложных операций
                "maxFeePerGas": base_gas_price + max_priority_fee * 2,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": await self._get_next_nonce(),
                "data": data,
                "value": 0,
                "chainId": 56,  # BNB Chain mainnet chain ID
            }
            
            # Подписываем и отправляем транзакцию
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            
            print(f"  Транзакция свапа отправлена: {tx_hash.hex()}")
            print(f"  🔗 BscScan: https://bscscan.com/tx/{tx_hash.hex()}")
            
            # Ждем подтверждения транзакции с увеличенным таймаутом
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status == 1:
                print(f"  Свап успешно выполнен. Tx: {tx_hash.hex()}")
                
                # Ждем обновления баланса выходного токена
                await self._wait_for_balance_update(token_out, amount_out_min_raw)
                
                return True, tx_hash.hex()
            else:
                print(f"  Ошибка при выполнении свапа. Tx: {tx_hash.hex()}")
                return False, tx_hash.hex()
        except Exception as e:
            print(f"  Произошла ошибка при выполнении свапа: {e}")
            # Сбрасываем nonce кэш при ошибке для переинициализации
            if "nonce too low" in str(e).lower():
                print(f"  ⚠️ Обнаружена ошибка nonce в свапе, сбрасываем кэш")
                self._nonce_cache = None
            return False, None

    async def _wait_for_balance_update(self, token_address: str, expected_min_amount: int, timeout: int = 30):
        """
        Ждет пока баланс токена обновится после свапа.
        
        Args:
            token_address: Адрес токена для проверки
            expected_min_amount: Минимальное абсолютное количество токенов после свапа
            timeout: Таймаут ожидания в секундах
        """
        start_time = time.time()
        initial_balance = await self._get_token_balance_raw(token_address)
        expected_threshold = int(expected_min_amount * 0.95)  # 95% от ожидаемого минимума
        
        print(f"  ⏳ Ожидаем обновления баланса {token_address}...")
        print(f"      Начальный баланс: {initial_balance}")
        print(f"      Ожидаем минимум: {expected_threshold}")
        
        check_count = 0
        while time.time() - start_time < timeout:
            current_balance = await self._get_token_balance_raw(token_address)
            check_count += 1
            
            if current_balance >= expected_threshold:
                elapsed = time.time() - start_time
                print(f"  ✅ Баланс обновился за {elapsed:.1f}с (проверок: {check_count})")
                print(f"      Новый баланс: {current_balance} (+{current_balance - initial_balance})")
                return True
            
            # Показываем прогресс каждые 5 секунд
            if check_count % 5 == 0:
                elapsed = time.time() - start_time
                print(f"      Проверка {check_count}: баланс {current_balance}, прошло {elapsed:.1f}с")
            
            await asyncio.sleep(1)
        
        # Таймаут - получаем финальный баланс для диагностики
        final_balance = await self._get_token_balance_raw(token_address)
        elapsed = time.time() - start_time
        print(f"  ⚠️ Таймаут ожидания обновления баланса ({elapsed:.1f}с)")
        print(f"      Начальный: {initial_balance}")
        print(f"      Финальный: {final_balance}")
        print(f"      Прирост: {final_balance - initial_balance}")
        print(f"      Ожидали: {expected_min_amount}")
        
        # Возвращаем True если хоть какой-то прирост есть
        return final_balance > initial_balance

    async def _check_and_approve_token(self, token_address_to_approve: str, spender_address: str, amount_raw: int):
        """Обычный approve для NonfungiblePositionManager (не Permit2)"""
        token_address = Web3.to_checksum_address(token_address_to_approve)
        spender_address = Web3.to_checksum_address(spender_address)
        
        erc20_abi = json.loads('''
        [
            {"constant":true,"inputs":[{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},
            {"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"}
        ]
        ''')
        token_contract = self.w3.eth.contract(address=token_address, abi=erc20_abi)
        
        try:
            current_allowance = token_contract.functions.allowance(self.signer_address, spender_address).call()
        except Exception as e:
            print(f"  Ошибка при проверке allowance: {e}")
            return False

        if current_allowance >= amount_raw:
            print(f"  Разрешение уже достаточно: {current_allowance}")
            return True
        
        current_balance = await self._get_token_balance_raw(token_address)
        if current_balance < amount_raw:
            print(f"  Ошибка: Недостаточный баланс токена")
            return False
        
        print(f"  Установка разрешения для {token_address}...")
        try:
            approve_func = token_contract.functions.approve(spender_address, amount_raw)
            gas_to_use = 100000
            base_gas_price = await self._get_gas_price()
            # Минимум 0.1 Gwei для BNB Chain
            max_priority_fee = max(100000000, int(base_gas_price * 0.5))
            
            tx_params = {
                "from": self.signer_address,
                "nonce": await self._get_next_nonce(),
                "gas": gas_to_use,
                "maxFeePerGas": base_gas_price + max_priority_fee * 2,
                "maxPriorityFeePerGas": max_priority_fee
            }
            approve_tx = approve_func.build_transaction(tx_params)
            signed_tx = self.w3.eth.account.sign_transaction(approve_tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            print(f"    Approve tx: {tx_hash.hex()}")
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            
            if receipt.status == 1:
                print(f"    ✅ Approve успешно")
                return True
            else:
                print(f"    Ошибка approve")
                return False
        except Exception as e:
            print(f"    Исключение при approve: {e}")
            return False

    async def _check_and_approve_token_for_permit2(self, token_address_to_approve: str, router_address: str, amount_raw: int):
        """
        Двухэтапный approve для работы с Permit2 и Universal Router:
        1. ERC20.approve(Permit2) - разрешаем Permit2 тратить токены (один раз, infinite)
        2. Permit2.approve(Router) - разрешаем Router использовать Permit2 allowance
        """
        token_address = Web3.to_checksum_address(token_address_to_approve)
        router_address = Web3.to_checksum_address(router_address)
        permit2_address = Web3.to_checksum_address(
            os.getenv("PANCAKESWAP_PERMIT2_ADDRESS", "0x31c2F6fcFf4F8759b3Bd5Bf0e1084A055615c768")
        )
        
        # Шаг 1: ERC20.approve(Permit2, infinite)
        erc20_abi = json.loads('''
        [
            {"constant":true,"inputs":[{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},
            {"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"}
        ]
        ''')
        token_contract = self.w3.eth.contract(address=token_address, abi=erc20_abi)
        
        try:
            # Проверяем ERC20 allowance для Permit2
            erc20_allowance = token_contract.functions.allowance(self.signer_address, permit2_address).call()
            if erc20_allowance < amount_raw:
                print(f"  [Шаг 1/2] ERC20.approve(Permit2) - infinite allowance...")
                infinite_amount = 2**256 - 1
                approve_func = token_contract.functions.approve(permit2_address, infinite_amount)
                
                gas_to_use = 100000
                base_gas_price = await self._get_gas_price()
                # Минимум 0.1 Gwei для BNB Chain
                max_priority_fee = max(100000000, int(base_gas_price * 0.5))
                
                tx_params = {
                    "from": self.signer_address,
                    "nonce": await self._get_next_nonce(),
                    "gas": gas_to_use,
                    "maxFeePerGas": base_gas_price + max_priority_fee * 2,
                    "maxPriorityFeePerGas": max_priority_fee
                }
                approve_tx = approve_func.build_transaction(tx_params)
                signed_tx = self.w3.eth.account.sign_transaction(approve_tx, self.private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
                print(f"    ERC20 approve tx: {tx_hash.hex()}")
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                
                if receipt.status != 1:
                    print(f"    Ошибка ERC20 approve (статус {receipt.status})")
                    return False
                print(f"    ✅ ERC20 approve успешно")
            else:
                print(f"  [Шаг 1/2] ERC20 allowance уже достаточно: {erc20_allowance}")
            
            # Шаг 2: Permit2.approve(Router, amount, expiration)
            # Загружаем ABI Permit2
            permit2_abi_path = os.path.join(os.path.dirname(__file__), 'abi', 'PANCAKESWAP_PERMIT2_ADDRESS.json')
            with open(permit2_abi_path, 'r') as f:
                permit2_abi = json.load(f)
            
            permit2_contract = self.w3.eth.contract(address=permit2_address, abi=permit2_abi)
            
            # Проверяем Permit2 allowance для Router
            permit2_allowance_data = permit2_contract.functions.allowance(
                self.signer_address, 
                token_address, 
                router_address
            ).call()
            
            # allowance возвращает (amount, expiration, nonce)
            current_permit2_amount = permit2_allowance_data[0]
            current_expiration = permit2_allowance_data[1]
            
            # Проверяем, нужен ли Permit2 approve
            import time
            needs_permit2_approve = (
                current_permit2_amount < amount_raw or 
                current_expiration < int(time.time()) + 3600  # Истекает в течение часа
            )
            
            if needs_permit2_approve:
                print(f"  [Шаг 2/2] Permit2.approve(Router) - authorizing router...")
                
                # Устанавливаем максимальные значения
                max_uint160 = 2**160 - 1
                max_uint48 = 2**48 - 1
                
                permit2_approve_func = permit2_contract.functions.approve(
                    token_address,
                    router_address,
                    max_uint160,
                    max_uint48
                )
                
                gas_to_use = 150000
                base_gas_price = await self._get_gas_price()
                # Минимум 0.1 Gwei для BNB Chain
                max_priority_fee = max(100000000, int(base_gas_price * 0.5))
                
                tx_params = {
                    "from": self.signer_address,
                    "nonce": await self._get_next_nonce(),
                    "gas": gas_to_use,
                    "maxFeePerGas": base_gas_price + max_priority_fee * 2,
                    "maxPriorityFeePerGas": max_priority_fee
                }
                
                permit2_tx = permit2_approve_func.build_transaction(tx_params)
                signed_tx = self.w3.eth.account.sign_transaction(permit2_tx, self.private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
                print(f"    Permit2 approve tx: {tx_hash.hex()}")
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                
                if receipt.status != 1:
                    print(f"    Ошибка Permit2 approve (статус {receipt.status})")
                    return False
                print(f"    ✅ Permit2 approve успешно")
            else:
                print(f"  [Шаг 2/2] Permit2 allowance уже достаточно: {current_permit2_amount}")
            
            return True
            
        except Exception as e:
            print(f"    Исключение при approve: {e}")
            import traceback
            traceback.print_exc()
            return False

            
    async def _print_managed_positions_status(self):
        """Выводит текущее состояние управляемых позиций."""
        print("Текущее состояние управляемых позиций:")
        for i, pos in enumerate(self.managed_positions_slots):
            if pos is not None:
                print(f"  Слот {i}: NFT ID {pos.get('nft_id', 'Н/Д')}, тики ({pos.get('tickLower', 'Н/Д')}-{pos.get('tickUpper', 'Н/Д')}), ликвидность {pos.get('liquidity', 'Н/Д')}")
            else:
                print(f"  Слот {i}: пуст")


    async def _collect_tokens(self, nft_id: int) -> bool:
        """
        Собирает начисленные токены с позиции NFT.
        
        Args:
            nft_id: ID NFT позиции
            
        Returns:
            bool: True в случае успеха, False в случае ошибки
        """
        try:
            collect_params = {
                'tokenId': nft_id,
                'recipient': self.signer_address,
                'amount0Max': 2**128 - 1,  # Максимальное значение uint128
                'amount1Max': 2**128 - 1   # Максимальное значение uint128
            }
            
            gas_price_to_use = await self._get_gas_price()
            collect_func = self.nonf_pos_manager.functions.collect(collect_params)
            
            # Используем умную оценку газа
            tx_params = {
                "from": self.signer_address,
                "maxFeePerGas": gas_price_to_use,
                "maxPriorityFeePerGas": max(100000000, int(gas_price_to_use * 0.5))
            }
            gas_estimate = await self.gas_manager.estimate_smart_gas(
                collect_func, tx_params, "collect"
            )
            
            collect_tx = collect_func.build_transaction({
                "from": self.signer_address,
                "gas": gas_estimate,
                "maxFeePerGas": gas_price_to_use,
                "maxPriorityFeePerGas": max(100000000, int(gas_price_to_use * 0.5)),
                "nonce": await self._get_next_nonce()
            })
            
            signed_tx = self.w3.eth.account.sign_transaction(collect_tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            print(f"  Транзакция collect отправлена: {tx_hash.hex()}")
            
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status != 1:
                print(f"  Ошибка при сборе токенов. Tx: {tx_hash.hex()}")
                return False
                
            print(f"  Токены успешно собраны. Tx: {tx_hash.hex()}")
            return True
            
        except Exception as e:
            print(f"  Произошла ошибка при сборе токенов: {e}")
            import traceback
            return False

    async def _get_gas_price(self) -> int:
        """Получает актуальную цену газа через GasManager"""
        return await self.gas_manager.get_current_gas_price()

    async def _get_priority_gas_price(self) -> int:
        """Возвращает повышенную цену газа для быстрого выполнения критических операций"""
        try:
            base_gas = await self._get_gas_price()
            priority_gas = int(base_gas * Decimal('1.5'))  # +50% к газу для скорости
            print(f"  Приоритетный газ: {priority_gas} (базовый: {base_gas})")
            return priority_gas
        except Exception as e:
            print(f"  Ошибка при получении приоритетного газа: {e}. Используем фикс. 1500000")
            return 1500000

    async def _estimate_swap_output_raw(self, token_in_addr: str, token_out_addr: str, amount_in_raw: int, 
                                        current_price_for_estimation: Decimal,
                                        swap_pool_fee_tier: int = 100) -> int:
        """
        Оценивает количество токенов, которое будет получено при свапе.
        
        Args:
            token_in_addr: Адрес входящего токена
            token_out_addr: Адрес исходящего токена
            amount_in_raw: Сумма входящего токена в сырых единицах
            current_price_for_estimation: Текущая цена paramT1/paramT0 (BTCB/USDT)
            swap_pool_fee_tier: Fee Tier пула для свапа (500=0.05%, 3000=0.3%)
            
        Returns:
            int: Оценка количества выходящих токенов в сырых единицах
        """
        # Преобразуем адреса в формат checksum
        token_in_checksum = Web3.to_checksum_address(token_in_addr)
        token_out_checksum = Web3.to_checksum_address(token_out_addr)
        
        di, do, sym_in, sym_out = 0, 0, "?", "?"
        
        # Получаем информацию о входящем токене
        if token_in_checksum == self.token0_for_calcs:
            di, sym_in = self.decimals0_for_calcs, self.token0_for_calcs_symbol
        elif token_in_checksum == self.token1_for_calcs:
            di, sym_in = self.decimals1_for_calcs, self.token1_for_calcs_symbol
        else:
            raise ValueError(f"Неизвестный token_in_addr для оценки свапа: {token_in_addr}")
        
        # Получаем информацию о исходящем токене
        if token_out_checksum == self.token0_for_calcs:
            do, sym_out = self.decimals0_for_calcs, self.token0_for_calcs_symbol
        elif token_out_checksum == self.token1_for_calcs:
            do, sym_out = self.decimals1_for_calcs, self.token1_for_calcs_symbol
        else:
            raise ValueError(f"Неизвестный token_out_addr для оценки свапа: {token_out_addr}")
        
        # Конвертируем сырое количество в человеческий формат
        amount_in_human = Decimal(amount_in_raw) / (Decimal(10)**di)
        amount_out_human = Decimal(0)
        
        # Рассчитываем примерное количество выходных токенов на основе цены
        if token_in_checksum == self.token0_for_calcs:  # USDT -> BTCB
            if current_price_for_estimation > 0:
                amount_out_human = amount_in_human / current_price_for_estimation
        elif token_in_checksum == self.token1_for_calcs:  # BTCB -> USDT
            amount_out_human = amount_in_human * current_price_for_estimation
        else:
            print(f"  Оценка свапа: Ошибка в логике определения пары {sym_in} -> {sym_out}")
            return 0
        
        # Учитываем комиссию пула
        # Fee Tier (500, 3000, 10000) переводим в процент.
        fee_percentage = Decimal(swap_pool_fee_tier) / Decimal(1_000_000)
        amount_out_human_after_fee = amount_out_human * (Decimal(1) - fee_percentage)
        
        # Конвертируем обратно в сырой формат
        amount_out_raw = int(amount_out_human_after_fee * (Decimal(10)**do))
        
        print(f"  Оценка свапа: {amount_in_human:.8f} {sym_in} (raw: {amount_in_raw}) -> "
              f"~{amount_out_human_after_fee:.8f} {sym_out} (raw est: {amount_out_raw}) "
              f"(цена LP пула {current_price_for_estimation:.2f}, комиссия свап-пула {swap_pool_fee_tier} ppm)")
        
        return amount_out_raw

    async def _initialize_or_update_managed_positions(self):
        """
        Инициализирует или обновляет информацию об управляемых позициях ликвидности.
        Если инициализируем первый раз, заполняет self.managed_positions_slots.
        """
        print("\n=== ИНИЦИАЛИЗАЦИЯ/ОБНОВЛЕНИЕ УПРАВЛЯЕМЫХ ПОЗИЦИЙ (на основе файла состояния и ончейн) ===")
        
        # ВСЕГДА обновляем состояние позиций (проверяем ликвидность)
        await self._update_managed_positions_status()
        
        # Проверяем, есть ли пустые слоты после очистки
        empty_slots_count = sum(1 for slot in self.managed_positions_slots if slot is None)
        active_slots_count = len(self.managed_positions_slots) - empty_slots_count
        
        print(f"  После очистки: {active_slots_count} активных позиций, {empty_slots_count} пустых слотов")
        
        # Если все слоты пусты, пытаемся найти существующие позиции на кошельке
        if all(slot is None for slot in self.managed_positions_slots):
            print("  Все слоты пусты. Попытка найти существующие NFT на кошельке...")

            # Реальный код: запрашиваем все NFT позиции для адреса сигнера
            my_positions = await self.get_my_current_positions()
            print(f"  Найдено NFT позиций на кошельке: {len(my_positions)}")
            
            # Также проверяем позиции в фарме
            farm_positions = await self.get_my_farm_positions()
            print(f"  Найдено NFT позиций в фарме: {len(farm_positions)}")
            
            # Объединяем все позиции и помечаем статус фарминга
            all_positions = []
            for pos in my_positions:
                pos['farm_staked'] = False
                all_positions.append(pos)
            for pos in farm_positions:
                pos['farm_staked'] = True  
                all_positions.append(pos)
            
            print(f"  Всего найдено позиций: {len(all_positions)}")
            
            # Если найдены позиции, инициализируем наши управляемые слоты
            if all_positions:
                slots_filled = 0
                for pos in all_positions:
                    # Проверяем, что позиция имеет ликвидность
                    if pos.get('liquidity', 0) > 0 and slots_filled < self.num_managed_positions:
                        # Найдем свободный слот
                        free_slot_idx = -1
                        for i, slot in enumerate(self.managed_positions_slots):
                            if slot is None:
                                free_slot_idx = i
                                break
                        
                        if free_slot_idx >= 0:
                            self.managed_positions_slots[free_slot_idx] = pos
                            print(f"  Позиция с токеном ID {pos['nft_id']} добавлена в слот {free_slot_idx}")
                            slots_filled += 1
            
            # Проверим, инициализированы ли какие-то позиции в слотах
            if all(slot is None for slot in self.managed_positions_slots):
                print("  Не найдено существующих подходящих позиций для автоматической инициализации.")
        else:
            # Если слоты не полностью пусты, проверяем нет ли осиротевших позиций
            print("  Проверка на наличие осиротевших позиций...")
            orphaned_positions = await self.find_orphaned_positions()
            if orphaned_positions:
                print(f"  Найдено {len(orphaned_positions)} осиротевших позиций. Добавляю в свободные слоты...")
                for orphaned_pos in orphaned_positions:
                    # Найдем свободный слот
                    free_slot_idx = -1
                    for i, slot in enumerate(self.managed_positions_slots):
                        if slot is None:
                            free_slot_idx = i
                            break
                    
                    if free_slot_idx >= 0:
                        self.managed_positions_slots[free_slot_idx] = orphaned_pos
                        print(f"  🚨 Осиротевшая позиция NFT {orphaned_pos['nft_id']} добавлена в слот {free_slot_idx}")
                    else:
                        print(f"  ⚠️ Нет свободных слотов для осиротевшей позиции NFT {orphaned_pos['nft_id']}")
        
        # Сохраняем обновленное состояние
        self._save_state_to_file()
            
        print("Текущее состояние слотов позиций (до принятия решений):")
        await self._print_managed_positions_status()

    async def _update_managed_positions_status(self):
        """Обновляет статус управляемых позиций и очищает неактивные."""
        print("Обновление статуса управляемых позиций...")
        updated_active_managed_positions = []
        slots_cleared = 0
        
        for slot_index, pos_data_in_slot in enumerate(self.managed_positions_slots):
            if pos_data_in_slot and 'nft_id' in pos_data_in_slot:
                nft_id = pos_data_in_slot['nft_id']
                try:
                    on_chain_pos = self.nonf_pos_manager.functions.positions(nft_id).call()
                    current_liquidity = on_chain_pos[7]
                    
                    # 🔍 Если ликвидность = 0, проверяем фарм
                    if current_liquidity == 0 and self.farm_address:
                        is_in_farm = await self._is_nft_in_farm(nft_id)
                        if is_in_farm:
                            print(f"  🌾 Слот {slot_index} (NFT {nft_id}) находится в фарме")
                            # Получаем ликвидность из фарма используя правильную структуру
                            try:
                                user_info = self.farm_contract.functions.userPositionInfos(nft_id).call()
                                # Структура userPositionInfos: liquidity, boostLiquidity, tickLower, tickUpper, rewardGrowthInside, reward, user, pid, boostMultiplier
                                farm_liquidity = user_info[0] if user_info else 0
                                boost_liquidity = user_info[1] if len(user_info) > 1 else 0
                                current_liquidity = farm_liquidity
                                print(f"  🌾 Ликвидность в фарме: {farm_liquidity} (boost: {boost_liquidity})")
                            except Exception as farm_e:
                                print(f"  ⚠️ Не удалось получить ликвидность из фарма: {farm_e}")
                                current_liquidity = 1  # Считаем активной, если в фарме
                    
                    if current_liquidity > 0: 
                        updated_pos_info = {
                            'nft_id': nft_id, 
                            'tickLower': on_chain_pos[5], 
                            'tickUpper': on_chain_pos[6], 
                            'liquidity': current_liquidity
                        }
                        # Обновляем слот
                        self.managed_positions_slots[slot_index] = updated_pos_info
                        updated_active_managed_positions.append(updated_pos_info)
                        print(f"  ✅ Слот {slot_index} (NFT {nft_id}) активен. Ликвидность: {current_liquidity}")
                    else:
                        print(f"  ❌ Слот {slot_index} (NFT {nft_id}) без ликвидности. Очищаем слот.")
                        self.managed_positions_slots[slot_index] = None
                        slots_cleared += 1
                        
                except Exception as e:
                    if "Invalid token ID" in str(e) or "execution reverted" in str(e).lower():
                        print(f"  Слот {slot_index} (NFT {nft_id}) не существует. Очищаем слот.")
                        self.managed_positions_slots[slot_index] = None
                        slots_cleared += 1
                    else:
                        print(f"  Ошибка при проверке NFT {nft_id} в слоте {slot_index}: {e}")
                        # В случае неопределенной ошибки тоже очищаем слот для безопасности
                        self.managed_positions_slots[slot_index] = None
                        slots_cleared += 1
        
        if slots_cleared > 0:
            print(f"  Очищено {slots_cleared} неактивных слотов")
                
        # Возвращаем список только активных позиций из тех, что мы управляем
        active_count = len(updated_active_managed_positions)
        print(f"  Итого активных управляемых позиций: {active_count}")
        
        # Проверяем нужно ли доливать остатки в позиции
        expected_positions = 2 if self.position_mode == '2_positions' else 3
        if active_count == expected_positions:
            await self._add_remaining_liquidity_to_positions()
        
        return updated_active_managed_positions

    async def _proactive_portfolio_rebalance(self, target_usdt_value_ratio: Decimal = Decimal("0.5"), 
                                           rebalance_threshold_pct: Decimal = Decimal("0.05")): # 5% порог отклонения
        """
        Проактивная ребалансировка портфеля для поддержания целевого соотношения активов.
        
        Args:
            target_usdt_value_ratio: Целевая доля USDT в общей стоимости портфеля (0.5 = 50%)
            rebalance_threshold_pct: Порог отклонения для запуска ребалансировки (0.05 = 5%)
        """
        print("\n--- Проактивная Ребалансировка Портфеля Активов ---")
        
        # Получаем текущую цену и состояние пула
        current_price_human, _, _ = await self.get_current_pool_state()
        
        # Получаем балансы токенов
        wallet_usdt_raw = await self._get_token_balance_raw(self.token0_for_calcs)
        wallet_btcb_raw = await self._get_token_balance_raw(self.token1_for_calcs)
        
        # Конвертируем в human значения
        wallet_usdt_human = Decimal(wallet_usdt_raw) / (Decimal(10) ** self.decimals0_for_calcs)
        wallet_btcb_human = Decimal(wallet_btcb_raw) / (Decimal(10) ** self.decimals1_for_calcs)
        
        # Рассчитываем общую стоимость портфеля в USDT
        usdt_value = wallet_usdt_human
        btcb_value_in_usdc = wallet_btcb_human * current_price_human
        total_portfolio_value_usdc = usdt_value + btcb_value_in_usdc
        
        print(f"  Баланс USDT: {wallet_usdt_human} (${wallet_usdt_human:.2f})")
        print(f"  Баланс BTCB: {wallet_btcb_human} (${btcb_value_in_usdc:.2f} по курсу ${current_price_human:.2f})")
        print(f"  Общая стоимость портфеля: ${total_portfolio_value_usdc:.2f}")
        
        # Проверяем минимальную сумму портфеля для ребалансировки
        min_portfolio_value_for_rebalance = Decimal("100")  # 100 USDT
        if total_portfolio_value_usdc < min_portfolio_value_for_rebalance:
            print(f"  Портфель слишком мал для ребалансировки (${total_portfolio_value_usdc:.2f} < ${min_portfolio_value_for_rebalance}). Пропускаем свапы.")
            return
        
        # Рассчитываем текущее соотношение USDT к общей стоимости
        current_usdt_ratio = usdt_value / total_portfolio_value_usdc if total_portfolio_value_usdc > 0 else Decimal("0")
        print(f"  Текущее соотношение USDT/Всего: {current_usdt_ratio * 100:.2f}% (целевое: {target_usdt_value_ratio * 100:.2f}%)")
        
        # Рассчитываем отклонение от целевого соотношения
        deviation = abs(current_usdt_ratio - target_usdt_value_ratio)
        print(f"  Отклонение от цели: {deviation * 100:.2f}% (порог для ребалансировки: {rebalance_threshold_pct * 100:.2f}%)")
        
        # Если отклонение меньше порога, не делаем свап
        if deviation < rebalance_threshold_pct:
            print("  Отклонение меньше порога, ребалансировка токенов не требуется.")
            
            # Проверяем, есть ли пустые слоты для позиций, и создаем в них позиции
            empty_slots = [i for i, slot in enumerate(self.managed_positions_slots) if slot is None]
            empty_slots_count = len(empty_slots)
            
            if empty_slots_count > 0:
                print(f"  Обнаружены пустые слоты позиций ({empty_slots_count}). Создаем новые позиции...")
                
                # Рассчитываем капитал для каждой позиции из общей стоимости портфеля
                capital_per_position = total_portfolio_value_usdc / Decimal(len(self.managed_positions_slots))
                print(f"  Распределение капитала: {total_portfolio_value_usdc} USDT на {len(self.managed_positions_slots)} позиций = {capital_per_position} USDT на позицию")
                
                # Рассчитываем целевые диапазоны для новых позиций
                target_ranges = self.calculate_target_ranges(current_price_human)
                
                # Создаем позиции в пустых слотах
                # Сначала создаем центральную позицию (слот 1), если она пустая
                if 1 in empty_slots:
                    target_range_info = target_ranges[1]
                    print(f"\n  Создание центральной позиции в слоте 1:")
                    print(f"  Целевой диапазон: {target_range_info['tickLower']} - {target_range_info['tickUpper']}")
                    
                    # Рассчитываем желаемые количества токенов
                    amount0_desired_raw, amount1_desired_raw = self._calculate_desired_amounts_for_position_from_capital(
                        tick_lower=target_range_info['tickLower'],
                        tick_upper=target_range_info['tickUpper'],
                        current_price_param_t1_t0=current_price_human,
                        capital_usdt=capital_per_position,
                        slot_index=1  # Центральная позиция
                    )
                    
                    if amount0_desired_raw > 0 or amount1_desired_raw > 0:
                        new_pos_info = await self._execute_add_liquidity(
                            slot_id=1,
                            tick_lower=target_range_info['tickLower'],
                            tick_upper=target_range_info['tickUpper'],
                            amount0_desired_raw=amount0_desired_raw,
                            amount1_desired_raw=amount1_desired_raw
                        )
                        if new_pos_info:
                            self.managed_positions_slots[1] = new_pos_info
                            print(f"  Центральная позиция успешно создана в слоте 1")
                        else:
                            print(f"  Не удалось создать центральную позицию в слоте 1")
                    else:
                        print(f"  Недостаточно токенов для создания центральной позиции в слоте 1")
                    
                    # Удаляем слот 1 из списка пустых слотов
                    empty_slots.remove(1)
                
                # Затем создаем остальные позиции
                for slot_idx in empty_slots:
                    if slot_idx >= len(target_ranges):
                        print(f"Предупреждение: слот {slot_idx} выходит за пределы target_ranges (длина {len(target_ranges)})")
                        continue
                        
                    # Слот пустой, создаем новую позицию
                    target_range_info = target_ranges[slot_idx]
                    print(f"\n  Создание новой позиции в слоте {slot_idx}:")
                    print(f"  Целевой диапазон: {target_range_info['tickLower']} - {target_range_info['tickUpper']}")
                    
                    # Рассчитываем желаемые количества токенов
                    amount0_desired_raw, amount1_desired_raw = self._calculate_desired_amounts_for_position_from_capital(
                        tick_lower=target_range_info['tickLower'],
                        tick_upper=target_range_info['tickUpper'],
                        current_price_param_t1_t0=current_price_human,
                        capital_usdt=capital_per_position,
                        slot_index=slot_idx  # Добавлен параметр для определения стратегии распределения
                    )
                    
                    if amount0_desired_raw > 0 or amount1_desired_raw > 0:
                        new_pos_info = await self._execute_add_liquidity(
                            slot_id=slot_idx,
                            tick_lower=target_range_info['tickLower'],
                            tick_upper=target_range_info['tickUpper'],
                            amount0_desired_raw=amount0_desired_raw,
                            amount1_desired_raw=amount1_desired_raw
                        )
                        if new_pos_info:
                            self.managed_positions_slots[slot_idx] = new_pos_info
                            print(f"  Позиция успешно создана в слоте {slot_idx}")
                        else:
                            print(f"  Не удалось создать позицию в слоте {slot_idx}")
                    else:
                        print(f"  Недостаточно токенов для создания позиции в слоте {slot_idx}")
                
                # Сохраняем обновленное состояние после создания позиций
                self._save_state_to_file()
                return True
            
            return False
        
        # Рассчитаем необходимую сумму для свапа
        target_usdt_value = total_portfolio_value_usdc * target_usdt_value_ratio
        usdt_value_difference = usdt_value - target_usdt_value  # Положительно, если нужно уменьшить USDT (свапнуть в BTCB)
        
        # Устанавливаем fee tier для свапа
        swap_pool_fee_tier = FEE_TIER_FOR_SWAP_TRANSACTION  # Используем константу для fee tier
        
        if usdt_value_difference > 0:  # Нужно свапнуть USDT в BTCB
            # Проверяем, что разница достаточно значима для свапа (например, > $1)
            if usdt_value_difference < 1:
                print("  Рассчитанная разница слишком мала для свапа USDT -> BTCB.")
                return False
            
            # Сумма USDT для свапа
            usdt_to_swap_human = usdt_value_difference
            usdt_to_swap_raw = int(usdt_to_swap_human * (Decimal(10) ** self.decimals0_for_calcs))
            
            print(f"\n  СВАП: USDT -> BTCB")
            print(f"  Сумма для свапа: {usdt_to_swap_human:.6f} USDT (сырое значение: {usdt_to_swap_raw})")
            
            # Проверяем достаточность баланса
            if usdt_to_swap_raw > wallet_usdt_raw:
                print(f"  Недостаточно USDT для свапа. Требуется: {usdt_to_swap_human}, есть: {wallet_usdt_human}")
                return False
            
            # Оцениваем получаемое количество BTCB
            estimated_btcb_raw = await self._estimate_swap_output_raw(
                self.token0_for_calcs, self.token1_for_calcs, 
                usdt_to_swap_raw, current_price_human, swap_pool_fee_tier
            )
            
            # Устанавливаем допустимое проскальзывание (slippage)
            slippage = Decimal("0.005")  # 0.5%
            btcb_min_raw = int(Decimal(estimated_btcb_raw) * (Decimal(1) - slippage))
            btcb_min_human = Decimal(btcb_min_raw) / (Decimal(10) ** self.decimals1_for_calcs)
            
            print(f"  Ожидаемое получение: {Decimal(estimated_btcb_raw) / (Decimal(10) ** self.decimals1_for_calcs):.8f} BTCB")
            print(f"  Минимальное получение (с учетом slippage): {btcb_min_human:.8f} BTCB")
            
            # Выполняем свап
            swap_success, tx_hash = await self._execute_swap(
                self.token0_for_calcs, 
                self.token1_for_calcs, 
                usdt_to_swap_raw, 
                btcb_min_raw,
                swap_pool_fee_tier
            )
            
            if swap_success:
                print(f"  Свап USDT -> BTCB успешно выполнен. Tx: {tx_hash}")
                return True
            else:
                print(f"  Ошибка при выполнении свапа USDT -> BTCB")
                return False
                
        else:  # Нужно свапнуть BTCB в USDT
            usdt_value_difference = abs(usdt_value_difference)
            
            # Проверяем, что разница достаточно значима для свапа (например, > $1)
            if usdt_value_difference < 1:
                print("  Рассчитанная разница слишком мала для свапа BTCB -> USDT.")
                return False
            
            # Рассчитываем количество BTCB для свапа
            btcb_to_swap_human = usdt_value_difference / current_price_human
            btcb_to_swap_raw = int(btcb_to_swap_human * (Decimal(10) ** self.decimals1_for_calcs))
            
            print(f"\n  СВАП: BTCB -> USDT")
            print(f"  Сумма для свапа: {btcb_to_swap_human:.8f} BTCB (сырое значение: {btcb_to_swap_raw})")
            
            # Проверяем достаточность баланса
            if btcb_to_swap_raw > wallet_btcb_raw:
                print(f"  Недостаточно BTCB для свапа. Требуется: {btcb_to_swap_human}, есть: {wallet_btcb_human}")
                return False
            
            # Оцениваем получаемое количество USDT
            estimated_usdt_raw = await self._estimate_swap_output_raw(
                self.token1_for_calcs, self.token0_for_calcs, 
                btcb_to_swap_raw, current_price_human, swap_pool_fee_tier
            )
            
            # Устанавливаем допустимое проскальзывание (slippage)
            slippage = Decimal("0.005")  # 0.5%
            usdt_min_raw = int(Decimal(estimated_usdt_raw) * (Decimal(1) - slippage))
            usdt_min_human = Decimal(usdt_min_raw) / (Decimal(10) ** self.decimals0_for_calcs)
            
            print(f"  Ожидаемое получение: {Decimal(estimated_usdt_raw) / (Decimal(10) ** self.decimals0_for_calcs):.6f} USDT")
            print(f"  Минимальное получение (с учетом slippage): {usdt_min_human:.6f} USDT")
            
            # Выполняем свап
            swap_success, tx_hash = await self._execute_swap(
                self.token1_for_calcs, 
                self.token0_for_calcs, 
                btcb_to_swap_raw, 
                usdt_min_raw,
                swap_pool_fee_tier
            )
            
            if swap_success:
                print(f"  Свап BTCB -> USDT успешно выполнен. Tx: {tx_hash}")
                return True
            else:
                print(f"  Ошибка при выполнении свапа BTCB -> USDT")
                return False
        
        return False

    async def get_my_current_positions(self):
        """
        Получает список текущих активных позиций пользователя из контракта PositionManager.
        
        Returns:
            list: Список словарей с информацией о позициях
        """
        print("Запрашиваем текущие NFT позиции пользователя...")
        
        try:
            # Определяем, сколько NFT токенов (позиций) у пользователя
            balance_of_method = self.nonf_pos_manager.functions.balanceOf(self.signer_address)
            nft_count = balance_of_method.call()
            
            if nft_count == 0:
                print("  У пользователя нет NFT позиций в PositionManager.")
                return []
                
            print(f"  Найдено {nft_count} NFT позиций в PositionManager.")
            
            # Для каждого NFT получаем его ID и данные
            positions_info = []
            for i in range(nft_count):
                try:
                    # Получаем ID токена по индексу
                    token_id = self.nonf_pos_manager.functions.tokenOfOwnerByIndex(self.signer_address, i).call()
                    
                    # Запрашиваем данные о позиции по ID
                    position_data = self.nonf_pos_manager.functions.positions(token_id).call()
                    
                    # Извлекаем нужные данные из ответа
                    # positions возвращает кортеж (nonce, operator, token0, token1, fee, tickLower, tickUpper, liquidity, feeGrowthInside0LastX128, feeGrowthInside1LastX128, tokensOwed0, tokensOwed1)
                    token0 = position_data[2]
                    token1 = position_data[3]
                    fee = position_data[4]
                    tick_lower = position_data[5]
                    tick_upper = position_data[6]
                    liquidity = position_data[7]
                    
                    # Проверяем, соответствует ли эта позиция нашему пулу
                    if token0.lower() == self.pool_actual_token0_addr.lower() and \
                       token1.lower() == self.pool_actual_token1_addr.lower() and \
                       fee == self.fee_tier and liquidity > 0:
                        
                        position_info = {
                            'nft_id': token_id,
                            'tickLower': tick_lower,
                            'tickUpper': tick_upper,
                            'liquidity': liquidity
                        }
                        positions_info.append(position_info)
                        print(f"    Найдена активная позиция с NFT ID {token_id}, тиками [{tick_lower}, {tick_upper}] и ликвидностью {liquidity}")
                        
                except Exception as e:
                    print(f"  Ошибка при получении данных о позиции {i}: {e}")
                    continue
                    
            return positions_info
            
        except Exception as e:
            print(f"Ошибка при получении списка NFT позиций: {e}")
            import traceback
            return []

    async def get_my_farm_positions(self):
        """
        Получает список позиций пользователя из фарма.
        
        Returns:
            list: Список словарей с информацией о позициях в фарме
        """
        if not self.farm_address:
            print("  Фарм не настроен")
            return []
            
        print("Запрашиваем текущие позиции пользователя из фарма...")
        
        try:
            # Получаем количество позиций в фарминге
            farm_balance = self.farm_contract.functions.balanceOf(self.signer_address).call()
            
            if farm_balance == 0:
                print("  У пользователя нет позиций в фарме.")
                return []
                
            print(f"  Найдено {farm_balance} позиций в фарме.")
            
            positions_info = []
            for i in range(farm_balance):
                try:
                    # Получаем ID токена по индексу в фарминге
                    token_id = self.farm_contract.functions.tokenOfOwnerByIndex(self.signer_address, i).call()
                    
                    # Получаем информацию о позиции из фарминга
                    user_info = self.farm_contract.functions.userPositionInfos(token_id).call()
                    
                    # Структура userPositionInfos: liquidity, boostLiquidity, tickLower, tickUpper, rewardGrowthInside, reward, user, pid, boostMultiplier
                    liquidity = user_info[0]
                    tick_lower = user_info[2]
                    tick_upper = user_info[3]
                    user_address = user_info[6]
                    pid = user_info[7]
                    
                    # Проверяем что это наша позиция
                    if user_address.lower() != self.signer_address.lower():
                        print(f"    NFT ID {token_id} принадлежит другому пользователю")
                        continue
                    
                    # Получаем информацию о пуле
                    pool_info = self.farm_contract.functions.poolInfo(pid).call()
                    # Структура poolInfo: allocPoint, v3Pool, token0, token1, fee, totalLiquidity, totalBoostLiquidity
                    token0 = pool_info[2]
                    token1 = pool_info[3]
                    fee = pool_info[4]
                    
                    # Проверяем соответствие нашему пулу
                    if (token0.lower() == self.pool_actual_token0_addr.lower() and 
                        token1.lower() == self.pool_actual_token1_addr.lower() and 
                        fee == self.fee_tier):
                        
                        position_info = {
                            'nft_id': token_id,
                            'tickLower': tick_lower,
                            'tickUpper': tick_upper,
                            'liquidity': liquidity,
                            'location': 'farm'
                        }
                        positions_info.append(position_info)
                        print(f"    Найдена позиция в фарме с NFT ID {token_id}, тиками [{tick_lower}, {tick_upper}] и ликвидностью {liquidity}")
                        
                except Exception as e:
                    print(f"  Ошибка при получении данных о позиции {i} из фарма: {e}")
                    continue
                    
            return positions_info
            
        except Exception as e:
            print(f"Ошибка при получении списка позиций из фарма: {e}")
            import traceback
            traceback.print_exc()
            return []

    async def find_orphaned_positions(self) -> list:
        """
        Находит позиции которые существуют on-chain но отсутствуют в managed_positions_slots.
        Это позиции которые были созданы но по каким-то причинам не попали в систему управления.
        
        Returns:
            list: Список словарей с информацией об осиротевших позициях
        """
        print("🔍 Поиск осиротевших позиций...")
        
        try:
            # Получаем все реальные позиции on-chain
            wallet_positions = await self.get_my_current_positions()
            farm_positions = await self.get_my_farm_positions()
            all_real_positions = wallet_positions + farm_positions
            
            if not all_real_positions:
                print("  Не найдено позиций on-chain")
                return []
            
            # Получаем NFT ID из managed_positions_slots
            managed_nft_ids = set()
            for slot in self.managed_positions_slots:
                if slot and 'nft_id' in slot:
                    managed_nft_ids.add(slot['nft_id'])
            
            print(f"  Найдено {len(all_real_positions)} позиций on-chain")
            print(f"  В managed_positions_slots: {len(managed_nft_ids)} позиций")
            
            # Находим орфанов - позиции которые есть on-chain но не в managed_positions_slots
            orphaned_positions = []
            for pos in all_real_positions:
                nft_id = pos['nft_id']
                if nft_id not in managed_nft_ids:
                    # Дополнительно проверяем что позиция действительно имеет ликвидность
                    if pos.get('liquidity', 0) > 0:
                        orphaned_positions.append(pos)
                        print(f"  🚨 Найдена осиротевшая позиция: NFT {nft_id}, ликвидность: {pos['liquidity']}")
            
            if not orphaned_positions:
                print("  ✅ Осиротевших позиций не найдено")
            else:
                print(f"  ⚠️  Найдено {len(orphaned_positions)} осиротевших позиций")
            
            return orphaned_positions
            
        except Exception as e:
            print(f"  ❌ Ошибка при поиске осиротевших позиций: {e}")
            import traceback
            traceback.print_exc()
            return []

    async def _execute_add_liquidity(self, slot_id: int, tick_lower: int, tick_upper: int, 
                                 amount0_desired_raw: int, amount1_desired_raw: int) -> dict:
        """
        Создает новую позицию ликвидности с указанными параметрами.
        
        Args:
            slot_id: Индекс слота для новой позиции
            tick_lower: Нижний тик
            tick_upper: Верхний тик
            amount0_desired_raw: Желаемое количество token0 (USDT) в сыром виде
            amount1_desired_raw: Желаемое количество token1 (BTCB) в сыром виде
            
        Returns:
            dict: Информация о созданной позиции или None в случае ошибки
        """
        print(f"\n[РЕАЛЬНЫЙ ВЫЗОВ] Слот {slot_id}: Создание позиции с тиками [{tick_lower}, {tick_upper}]")
        
        # Проверяем текущие балансы
        balance0_raw = await self._get_token_balance_raw(self.token0_for_calcs)
        balance1_raw = await self._get_token_balance_raw(self.token1_for_calcs)
        
        # Корректируем суммы, если они превышают баланс
        if amount0_desired_raw > balance0_raw:
            print(f"  Корректировка суммы {self.token0_for_calcs_symbol}: запрошено {amount0_desired_raw}, доступно {balance0_raw}")
            amount0_desired_raw = balance0_raw
            
        if amount1_desired_raw > balance1_raw:
            print(f"  Корректировка суммы {self.token1_for_calcs_symbol}: запрошено {amount1_desired_raw}, доступно {balance1_raw}")
            amount1_desired_raw = balance1_raw
        
        print(f"  Предоставляем токены: {self.token0_for_calcs_symbol}={amount0_desired_raw}, {self.token1_for_calcs_symbol}={amount1_desired_raw}")
        
        # Минимальная сумма token0 в сыром виде
        min_amount0 = 1
        # Минимальная сумма token1 в сыром виде
        min_amount1 = 1
        
        if amount0_desired_raw < min_amount0 and amount1_desired_raw < min_amount1:
            print(f"  Ошибка: Обе суммы меньше минимальных ({min_amount0} и {min_amount1}). Пропускаем создание позиции.")
            return None
            
        # Проверка и установка разрешений для обоих токенов
        approve_success_0 = await self._check_and_approve_token(
            self.token0_for_calcs, self.nonf_pos_manager_address, amount0_desired_raw
        ) if amount0_desired_raw > 0 else True
        
        approve_success_1 = await self._check_and_approve_token(
            self.token1_for_calcs, self.nonf_pos_manager_address, amount1_desired_raw
        ) if amount1_desired_raw > 0 else True
        
        if not approve_success_0 or not approve_success_1:
            print("  Не удалось установить разрешения на токены. Отмена создания позиции.")
            return None
            
        # Формируем параметры для mint
        # === КРИТИЧНОЕ: Получаем актуальную цену для проверки изменений ===
        price_at_mint, _, _ = await self.get_current_pool_state()
        
        # Если у нас есть предыдущая цена для сравнения, проверяем изменения
        # (В данном случае пропускаем проверку и всегда используем актуальную цену)
        print(f"💱 Актуальная цена перед mint: {price_at_mint:.6f}")
        
        # === ОПТИМИЗАЦИЯ 2: Минимальные amounts = 0% ===
        min_ratio = Decimal("0.0")  # Используем 0% от желаемых amounts
        min_amount0 = int(amount0_desired_raw * min_ratio)
        min_amount1 = int(amount1_desired_raw * min_ratio)
        
        deadline = int(time.time()) + 300  # 5 минут
        
        # Параметры для mint
        mint_params = {
            'token0': self.token0_for_calcs,
            'token1': self.token1_for_calcs,
            'fee': self.fee_tier,
            'tickLower': tick_lower,
            'tickUpper': tick_upper,
            'amount0Desired': amount0_desired_raw,
            'amount1Desired': amount1_desired_raw,
            'amount0Min': min_amount0,
            'amount1Min': min_amount1,
            'recipient': self.signer_address,
            'deadline': deadline
        }
        
        try:
            # Получаем текущую цену и пересчитываем amounts ПРЯМО перед mint
            price_at_mint, _, _ = await self.get_current_pool_state()
            
            # КРИТИЧНЫЙ пересчет amounts с актуальной ценой
            print(f"🔄 Финальный пересчет amounts с ценой {price_at_mint:.6f}")
            final_balance0 = await self._get_token_balance_raw(self.token0_for_calcs)
            final_balance1 = await self._get_token_balance_raw(self.token1_for_calcs)
            final_capital = (Decimal(final_balance0) / (Decimal(10) ** self.decimals0_for_calcs)) + \
                           (Decimal(final_balance1) / (Decimal(10) ** self.decimals1_for_calcs)) * price_at_mint
            
            final_amount0, final_amount1 = self._calculate_desired_amounts_for_position_from_capital(
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                current_price_param_t1_t0=price_at_mint,
                capital_usdt=final_capital,
                is_smart_rebalance=True
            )
            
            # Корректируем amounts до реальных балансов
            amount0_desired_raw = min(final_amount0, final_balance0)
            amount1_desired_raw = min(final_amount1, final_balance1)
            
            # Пересчитываем min amounts с новыми значениями
            min_amount0 = int(amount0_desired_raw * min_ratio)
            min_amount1 = int(amount1_desired_raw * min_ratio)
            
            # Обновляем mint_params
            mint_params.update({
                'amount0Desired': amount0_desired_raw,
                'amount1Desired': amount1_desired_raw,
                'amount0Min': min_amount0,
                'amount1Min': min_amount1
            })
            
            print(f"🎯 Финальные amounts: USDT={amount0_desired_raw}, BTCB={amount1_desired_raw}")
            print(f"🎯 Min amounts (0%): USDT={min_amount0}, BTCB={min_amount1}")
            
            # Отправляем транзакцию mint
            base_gas_price = await self._get_gas_price()
            max_priority_fee = max(100000000, int(base_gas_price * 0.5))  # Минимум 0.1 Gwei  # Не больше половины базовой цены и не больше 1 Gwei
            
            tx = self.nonf_pos_manager.functions.mint(mint_params).build_transaction({
                'from': self.signer_address,
                'gas': 1500000,
                'maxFeePerGas': base_gas_price + max_priority_fee * 2,
                'maxPriorityFeePerGas': max_priority_fee,
                'nonce': await self._get_next_nonce()
            })
            
            # Подписываем и отправляем транзакцию
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            
            print(f"  Транзакция mint отправлена: {tx_hash.hex()}. Ожидание подтверждения...")
            
            # Ждем подтверждения транзакции
            tx_receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
            
            if tx_receipt['status'] == 1:
                print(f"  Mint УСПЕШЕН! Tx: {tx_hash.hex()}")
                
                # Получаем ID токена из события
                token_id = None
                for log in tx_receipt['logs']:
                    if log['address'].lower() == self.nonf_pos_manager_address.lower():
                        try:
                            event = self.nonf_pos_manager.events.IncreaseLiquidity().process_log(log)
                            token_id = event['args']['tokenId']
                            break
                        except:
                            continue
                
                if token_id is None:
                    print("  Ошибка: Не удалось получить ID токена из событий транзакции")
                    return None
                
                print(f"  Получен NFT ID: {token_id}. Ожидание 5 секунд для индексации узлом...")
                await asyncio.sleep(5)  # Ждем индексацию
                
                # Сохраняем начальные данные позиции для P&L
                try:
                    # Получаем текущие балансы после создания позиции
                    current_usdt_raw = await self._get_token_balance_raw(self.token0_for_calcs)
                    current_btcb_raw = await self._get_token_balance_raw(self.token1_for_calcs)
                    
                    current_usdt_human = Decimal(current_usdt_raw) / (Decimal(10) ** self.decimals0_for_calcs)
                    current_btcb_human = Decimal(current_btcb_raw) / (Decimal(10) ** self.decimals1_for_calcs)
                    
                    # Получаем текущую цену
                    current_price, _, _ = await self.get_current_pool_state()
                    
                    # Рассчитываем общую стоимость в USDT
                    total_value_usdc = current_usdt_human + (current_btcb_human * current_price)
                    
                    # Сохраняем начальные данные для P&L расчета
                    if not hasattr(self, 'initial_position_data'):
                        self.initial_position_data = {}
                    
                    self.initial_position_data[token_id] = {
                        'timestamp_open': pd.Timestamp.now(tz='UTC').isoformat(),
                        'initial_usdc': float(current_usdt_human),
                        'initial_cbbtc': float(current_btcb_human),
                        'initial_value_usdc': float(total_value_usdc),
                        'btcb_price_open': float(current_price),
                        'tick_lower': tick_lower,
                        'tick_upper': tick_upper,
                        'slot_id': slot_id
                    }
                    print(f"\nНачальные данные позиции {token_id} сохранены для P&L расчета")
                    
                except Exception as e:
                    print(f"Ошибка при сохранении начальных данных позиции: {e}")
                                # Записываем открытие позиции в лог
                
                # Отправляем NFT в фарминг, если настроено
                farm_success = False
                if self.farm_address is not None:
                    print(f"  Отправляем NFT ID {token_id} в фарминг...")
                    farm_success = await self.stake_nft_in_farm(token_id)
                    if not farm_success:
                        print(f"  Не удалось отправить NFT ID {token_id} в фарминг")
                    else:
                        print(f"  NFT ID {token_id} успешно отправлен в фарминг")
                
                # Получаем подробности о позиции
                try:
                    position_data = self.nonf_pos_manager.functions.positions(token_id).call()
                    liquidity = position_data[7]  # Ликвидность находится в 7-м элементе возвращаемого кортежа
                    
                    # Рассчитываем начальную стоимость позиции в USDT
                    initial_usdt_value = Decimal(amount0_desired_raw) / (Decimal(10) ** self.decimals0_for_calcs)
                    initial_btcb_value = Decimal(amount1_desired_raw) / (Decimal(10) ** self.decimals1_for_calcs)
                    initial_total_value_usdc = initial_usdt_value + (initial_btcb_value * price_at_mint)
                    
                    position_info = {
                        'nft_id': token_id,
                        'tickLower': tick_lower,
                        'tickUpper': tick_upper,
                        'liquidity': liquidity,
                        'amount0_actual_raw': amount0_desired_raw,
                        'amount1_actual_raw': amount1_desired_raw,
                        'initial_value_usdc': str(initial_total_value_usdc.quantize(Decimal("0.000001"))),
                        'initial_price': str(price_at_mint.quantize(Decimal("0.000001"))),  # Добавляем начальную цену
                        'timestamp_created': pd.Timestamp.now(tz='UTC').isoformat(),
                        'staked_in_farm': farm_success
                    }
                    
                    print(f"  Позиция успешно создана: NFT ID {token_id}, ликвидность {liquidity}, начальная стоимость {initial_total_value_usdc:.6f} USDT")
                    return position_info
                    
                except Exception as e:
                    print(f"  Ошибка при получении информации о позиции: {e}")
                    return None
            else:
                print(f"  Ошибка: Транзакция mint не удалась. Tx: {tx_hash.hex()}")
                return None
                
        except Exception as e:
            print(f"  Ошибка при создании позиции: {e}")
            return None

    def human_price_to_tick_param_t1_t0(self, human_price_param_t1_t0: Decimal) -> int:
        """
        Преобразует человеческую цену (param_T1/param_T0, например BTCB/USDT, ~100k) в тик.
        ВАЖНО: В результате получаем тик, который представляет ИНВЕРТИРОВАННУЮ цену (USDT/BTCB, ~0.01).
        
        Args:
            human_price_param_t1_t0: Цена в человеческом формате (param_T1/param_T0, например BTCB/USDT ~100k)
            
        Returns:
            int: Тик в формате Uniswap V3 (представляет инвертированное отношение USDT/BTCB)
        """
        # Конвертируем человеческую цену в сырую для расчета тиков
        # Здесь происходит инверсия: из BTCB/USDT -> USDT/BTCB в raw формате
        raw_price_for_tick_calc = self._human_price_param_t1_t0_to_raw_price_pool_t1_t0(human_price_param_t1_t0)
        
        # Конвертируем сырую цену в тик
        tick = self.price_to_tick(raw_price_for_tick_calc)
        
        return tick

    # --- Стандартизированные функции конвертации ---
    # param_T0 = USDT (self.decimals0_for_calcs), param_T1 = BTCB (self.decimals1_for_calcs)
    # pool_actual_token0 = USDT, pool_actual_token1 = BTCB (т.к. self.invert_price_for_t0_t1 = False)

    def _human_price_param_t1_t0_to_raw_price_pool_t1_t0(self, human_price: Decimal) -> Decimal:
        """
        Конвертирует человеческую цену paramT1/paramT0 (BTCB/USDT, ~100k)
        в сырую цену poolT1/poolT0 (BTCB_raw/USDT_raw), используемую для расчета тиков.
        """
        if human_price == Decimal(0): raise ValueError("Human price is zero, cannot invert.")
        
        # Инвертируем цену и применяем коррекцию для BNB Chain (оба токена 18 decimals)
        # P_raw = (1 / P_human) * 10^(decimals_t0 - decimals_t1)
        # Для 18-18 decimals: множитель = 10^0 = 1
        return (Decimal(1) / human_price) * (Decimal(10)**(self.decimals0_for_calcs - self.decimals1_for_calcs))

    def _raw_price_pool_t1_t0_to_human_price_param_t1_t0(self, raw_price_pool_t1_t0: Decimal) -> Decimal:
        """
        Конвертирует сырую цену poolT1/poolT0 (BTCB_raw/USDT_raw)
        обратно в человеческую цену paramT1/paramT0 (BTCB/USDT, ~100k).
        """
        if raw_price_pool_t1_t0 == Decimal(0): raise ValueError("Raw pool price is zero.")
        
        # Обратное преобразование: инвертируем и применяем коррекцию decimals
        # P_human = (1 / P_raw) * 10^(decimals_t1 - decimals_t0)
        # Для 18-18 decimals: множитель = 10^0 = 1
        return (Decimal(1) / raw_price_pool_t1_t0) * (Decimal(10)**(self.decimals1_for_calcs - self.decimals0_for_calcs))

    async def _execute_burn_nft(self, nft_id: int) -> bool:
        """
        Сжигает NFT-позицию с нулевой ликвидностью.
        
        Args:
            nft_id: ID NFT позиции для сжигания
            
        Returns:
            bool: True в случае успеха, False в случае ошибки
        """
        print(f"\n[РЕАЛЬНЫЙ ВЫЗОВ] Сжигание NFT ID: {nft_id}")
        
        try:
            # Проверяем, что ликвидность равна 0
            position_data = self.nonf_pos_manager.functions.positions(nft_id).call()
            current_liquidity = position_data[7]
            
            if current_liquidity > 0:
                print(f"  Ошибка: Невозможно сжечь NFT с ненулевой ликвидностью ({current_liquidity}). Сначала удалите ликвидность.")
                return False
            
            # Выполняем сжигание NFT
            burn_func = self.nonf_pos_manager.functions.burn(nft_id)
            gas_price_to_use = await self._get_gas_price()
            
            try:
                gas_estimate_burn = int(burn_func.estimate_gas({
                    "from": self.signer_address,
                    "maxFeePerGas": gas_price_to_use,
                    "maxPriorityFeePerGas": max(100000000, int(gas_price_to_use * 0.5))
                }) * Decimal('1.2'))
            except Exception as e_est:
                print(f"  Не удалось оценить газ для burn: {e_est}. Используем фикс. 1000000")
                gas_estimate_burn = 1000000
            
            tx_params_burn = {
                "from": self.signer_address,
                "gas": gas_estimate_burn,
                "maxFeePerGas": gas_price_to_use,
                "maxPriorityFeePerGas": max(100000000, int(gas_price_to_use * 0.5)),
                "nonce": await self._get_next_nonce()
            }
            
            tx_burn = burn_func.build_transaction(tx_params_burn)
            signed_tx_burn = self.w3.eth.account.sign_transaction(tx_burn, self.private_key)
            tx_hash_burn = self.w3.eth.send_raw_transaction(signed_tx_burn.rawTransaction)
            
            print(f"  Транзакция burn отправлена: {tx_hash_burn.hex()}. Ожидание подтверждения...")
            
            receipt_burn = self.w3.eth.wait_for_transaction_receipt(tx_hash_burn, timeout=60)
            
            if receipt_burn.status == 1:
                print(f"  NFT с ID {nft_id} успешно сожжен.")
                return True
            else:
                print(f"  Транзакция burn НЕ УДАЛАСЬ. Статус: {receipt_burn.status}.")
                return False
                
        except Exception as e:
            print(f"  Исключение при сжигании NFT: {e}")
            import traceback
            return False

    async def _unstake_nft_from_farm(self, nft_id: int) -> bool:
        """
        Выводит NFT из фарминга используя метод withdraw.
        
        Args:
            nft_id: ID NFT позиции для вывода
            
        Returns:
            bool: True в случае успеха, False в случае ошибки
        """
        print(f"  Попытка вывода NFT ID {nft_id} из фарминга...")
        
        try:
            # Вызываем withdraw(uint256 _tokenId, address _to)
            withdraw_func = self.farm_contract.functions.withdraw(
                nft_id,  # _tokenId
                self.signer_address  # _to
            )
            
            gas_price_to_use = await self._get_gas_price()
            
            # Используем умную оценку газа
            tx_params = {
                "from": self.signer_address,
                "maxFeePerGas": gas_price_to_use,
                "maxPriorityFeePerGas": max(100000000, int(gas_price_to_use * 0.5))
            }
            gas_estimate = await self.gas_manager.estimate_smart_gas(
                withdraw_func, tx_params, "withdraw"
            )
            
            withdraw_tx = withdraw_func.build_transaction({
                "from": self.signer_address,
                "gas": gas_estimate,
                "maxFeePerGas": gas_price_to_use,
                "maxPriorityFeePerGas": max(100000000, int(gas_price_to_use * 0.5)),
                "nonce": await self._get_next_nonce()
            })
            
            signed_tx = self.w3.eth.account.sign_transaction(withdraw_tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            print(f"    Транзакция withdraw из фарминга отправлена: {tx_hash.hex()}")
            
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status != 1:
                print(f"    Транзакция withdraw НЕ УДАЛАСЬ. Статус: {receipt.status}. Проверьте, находится ли NFT в фарме.")
                return False
                
            print(f"    NFT успешно выведен из фарминга. Tx: {tx_hash.hex()}")
            return True
            
        except Exception as e:
            print(f"    Произошла ошибка при выводе из фарминга: {e}")
            import traceback
            return False



    async def _approve_position_manager(self, token_id: int) -> bool:
        """
        Устанавливает разрешение (approve) для NonfungiblePositionManager на управление NFT.
        
        Args:
            token_id: ID NFT токена для установки разрешения
            
        Returns:
            bool: True в случае успеха, False в случае ошибки
        """
        print(f"  Проверяем и устанавливаем разрешение для NonfungiblePositionManager на NFT ID {token_id}...")
        
        # ABI для getApproved и approve функций ERC-721
        nft_approve_abi = [
            {"inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}], "name": "getApproved", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
            {"inputs": [{"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "tokenId", "type": "uint256"}], "name": "approve", "outputs": [], "stateMutability": "nonpayable", "type": "function"}
        ]
        
        # Создаем контракт для работы с функциями approve
        nft_contract = self.w3.eth.contract(address=self.nonf_pos_manager_address, abi=nft_approve_abi)
        
        try:
            # Проверяем текущее разрешение
            current_approved = nft_contract.functions.getApproved(token_id).call()
            
            # Если текущее разрешение уже установлено для NonfungiblePositionManager, ничего не делаем
            if current_approved.lower() == self.nonf_pos_manager_address.lower():
                print(f"  NFT ID {token_id} уже имеет необходимое разрешение.")
                return True
            
            print(f"  Устанавливаем разрешение для NFT ID {token_id}...")
            
            # Создаем транзакцию approve
            approve_func = nft_contract.functions.approve(
                self.nonf_pos_manager_address,
                token_id
            )
            
            # Используем фиксированный лимит газа для approve
            gas_to_use = 1000000
            base_gas_price = await self._get_gas_price()
            max_priority_fee = max(100000000, int(base_gas_price * 0.5))  # Минимум 0.1 Gwei
            
            tx_params = {
                "from": self.signer_address,
                "nonce": await self._get_next_nonce(),
                "gas": gas_to_use,
                "maxFeePerGas": base_gas_price + max_priority_fee * 2,
                "maxPriorityFeePerGas": max_priority_fee
            }
            
            # Подписываем и отправляем транзакцию
            approve_tx = approve_func.build_transaction(tx_params)
            signed_tx = self.w3.eth.account.sign_transaction(approve_tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            
            print(f"  Транзакция approve для NFT отправлена: {tx_hash.hex()}. Ожидание подтверждения...")
            
            # Ждем подтверждения транзакции
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            
            if receipt.status == 1:
                print(f"  Разрешение для NFT ID {token_id} успешно установлено.")
                return True
            else:
                print(f"  Ошибка при установке разрешения для NFT ID {token_id}. Статус: {receipt.status}.")
                return False
        
        except Exception as e:
            print(f"  Ошибка при работе с разрешениями NFT: {e}")
            return False
        
    async def get_position_info(self, token_id: int) -> dict:
        """
        Получает информацию о позиции NFT.
        
        Args:
            token_id: ID токена NFT
            
        Returns:
            dict: Словарь с информацией о позиции или словарь с ключом 'error', если произошла ошибка
        """
        try:
            # Получаем информацию о позиции
            position = self.nonf_pos_manager.functions.positions(token_id).call()
            
            return {
                "nonce": position[0],
                "operator": position[1],
                "token0": position[2],
                "token1": position[3],
                "fee": position[4],
                "tickLower": position[5],
                "tickUpper": position[6],
                "liquidity": position[7],
                "feeGrowthInside0LastX128": position[8],
                "feeGrowthInside1LastX128": position[9],
                "tokensOwed0": position[10],
                "tokensOwed1": position[11]
            }
        except Exception as e:
            return {'error': f"Ошибка при получении информации о позиции NFT: {e}"}
            

    async def _execute_add_liquidity_fast(self, slot_id: int, tick_lower: int, tick_upper: int,
                                    capital_usdt: Decimal, is_smart_rebalance: bool = False) -> dict:
        """
        ОПТИМИЗИРОВАННЫЙ метод создания позиции с пересчетом сумм в реальном времени.
        Сокращает время от расчета до mint для точности.

        Args:
            slot_id: Индекс слота для новой позиции
            tick_lower: Нижний тик
            tick_upper: Верхний тик
            capital_usdt: Капитал в USDT для позиции

        Returns:
            dict: Информация о созданной позиции или None в случае ошибки
        """
        print(f"\n[БЫСТРОЕ СОЗДАНИЕ] Слот {slot_id}: Позиция с тиками [{tick_lower}, {tick_upper}]")

        # === ОПТИМИЗАЦИЯ 1: Пересчет сумм прямо перед mint ===
        print("  Получаем актуальную цену для пересчета...")
        current_price, _, _ = await self.get_current_pool_state()

        # Пересчитываем суммы с актуальной ценой
        amount0_desired_raw, amount1_desired_raw = self._calculate_desired_amounts_for_position_from_capital(
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            current_price_param_t1_t0=current_price,
            capital_usdt=capital_usdt,
            slot_index=slot_id,
            is_smart_rebalance=is_smart_rebalance
        )

        # Проверяем текущие балансы
        balance0_raw = await self._get_token_balance_raw(self.token0_for_calcs)
        balance1_raw = await self._get_token_balance_raw(self.token1_for_calcs)

        balance0_human = Decimal(balance0_raw) / (Decimal(10) ** self.decimals0_for_calcs)
        balance1_human = Decimal(balance1_raw) / (Decimal(10) ** self.decimals1_for_calcs)

        print(f"  Текущие балансы: {self.token0_for_calcs_symbol}=${balance0_human:.2f}, {self.token1_for_calcs_symbol}={balance1_human:.8f}")

        # СВАПАЕМ ТОКЕНЫ ДЛЯ ПРАВИЛЬНОГО СООТНОШЕНИЯ (только если расхождение > 5%)
        amount0_human = Decimal(amount0_desired_raw) / (Decimal(10) ** self.decimals0_for_calcs)
        amount1_human = Decimal(amount1_desired_raw) / (Decimal(10) ** self.decimals1_for_calcs)

        # Рассчитываем расхождение для каждого токена
        usdt_deficit_pct = Decimal("0")
        btcb_deficit_pct = Decimal("0")

        if amount0_desired_raw > balance0_raw:
            usdt_deficit = amount0_human - balance0_human
            usdt_deficit_pct = (usdt_deficit / amount0_human) * 100 if amount0_human > 0 else Decimal("0")
            print(f"  Нехватка USDT: {usdt_deficit:.2f} ({usdt_deficit_pct:.1f}%)")

        if amount1_desired_raw > balance1_raw:
            btcb_deficit = amount1_human - balance1_human  
            btcb_deficit_pct = (btcb_deficit / amount1_human) * 100 if amount1_human > 0 else Decimal("0")
            print(f"  Нехватка BTCB: {btcb_deficit:.8f} ({btcb_deficit_pct:.1f}%)")

        # Выполняем свап только если нужно и проверяем результат
        swap_success = True
        
        # Проверяем что нужно и свапаем недостающее (только если расхождение > 5%)
        if amount0_desired_raw > balance0_raw and usdt_deficit_pct > 5:
            # Нужно больше USDT - продаем BTCB (только если нехватка > 5%)
            usdt_needed = amount0_human - balance0_human
            btcb_to_sell = usdt_needed / current_price
            
            print(f"  DEBUG: USDT нужно {usdt_needed:.2f}, BTCB продать {btcb_to_sell:.8f}, есть {balance1_human:.8f}")

            # Проверяем минимальную сумму для свапа (минимум $0.01 или 0.0000001 BTCB)
            if balance1_human >= btcb_to_sell and usdt_needed >= Decimal("0.01") and btcb_to_sell >= Decimal("0.0000001"):
                print(f"  Свап {btcb_to_sell:.8f} BTCB -> {usdt_needed:.2f} USDT (нехватка {usdt_deficit_pct:.1f}% > 5%)")
                amount_in_raw = int(btcb_to_sell * (Decimal(10) ** self.decimals1_for_calcs))
                amount_out_min_raw = int(usdt_needed * Decimal("0.99") * (Decimal(10) ** self.decimals0_for_calcs))
                
                # Дополнительная проверка что amount_in_raw > 0
                if amount_in_raw == 0 or amount_out_min_raw == 0:
                    print(f"  ⚠️  Сумма свапа слишком мала (amount_in={amount_in_raw}, amount_out={amount_out_min_raw}), пропускаем свап")
                    # Используем имеющийся баланс
                    amount0_desired_raw = balance0_raw
                else:
                    swap_result, _ = await self._execute_swap(
                        self.token1_for_calcs,  # BTCB
                        self.token0_for_calcs,  # USDT
                        amount_in_raw,
                        amount_out_min_raw,
                        self.swap_pool_fee_tier  # Передаем правильный fee tier
                    )
                    swap_success = swap_result
            else:
                print(f"  ⚠️  Сумма свапа слишком мала или недостаточно BTCB. Используем имеющийся баланс.")
                amount0_desired_raw = balance0_raw
        elif amount0_desired_raw > balance0_raw:
            print(f"  Нехватка USDT {usdt_deficit_pct:.1f}% < 5%, свап не нужен. Корректируем позицию.")
            amount0_desired_raw = balance0_raw

        elif amount1_desired_raw > balance1_raw and btcb_deficit_pct > 5:
            # Нужно больше BTCB - продаем USDT (только если нехватка > 5%)
            btcb_needed = amount1_human - balance1_human
            usdt_to_sell = btcb_needed * current_price
            
            print(f"  DEBUG: BTCB нужно {btcb_needed:.8f}, USDT продать {usdt_to_sell:.2f}, есть {balance0_human:.2f}")

            # Проверяем минимальную сумму для свапа (минимум $0.01 или 0.0000001 BTCB)
            if balance0_human >= usdt_to_sell and usdt_to_sell >= Decimal("0.01") and btcb_needed >= Decimal("0.0000001"):
                print(f"  Свап {usdt_to_sell:.2f} USDT -> {btcb_needed:.8f} BTCB (нехватка {btcb_deficit_pct:.1f}% > 5%)")
                amount_in_raw = int(usdt_to_sell * (Decimal(10) ** self.decimals0_for_calcs))
                amount_out_min_raw = int(btcb_needed * Decimal("0.99") * (Decimal(10) ** self.decimals1_for_calcs))
                
                # Дополнительная проверка что amount_in_raw > 0
                if amount_in_raw == 0 or amount_out_min_raw == 0:
                    print(f"  ⚠️  Сумма свапа слишком мала (amount_in={amount_in_raw}, amount_out={amount_out_min_raw}), пропускаем свап")
                    # Используем имеющийся баланс
                    amount1_desired_raw = balance1_raw
                else:
                    swap_result, _ = await self._execute_swap(
                        self.token0_for_calcs,  # USDT
                        self.token1_for_calcs,  # BTCB
                        amount_in_raw,
                        amount_out_min_raw,
                        self.swap_pool_fee_tier  # Передаем правильный fee tier
                    )
                    swap_success = swap_result
            else:
                print(f"  ⚠️  Сумма свапа слишком мала или недостаточно USDT. Используем имеющийся баланс.")
                amount1_desired_raw = balance1_raw
        elif amount1_desired_raw > balance1_raw:
            print(f"  Нехватка BTCB {btcb_deficit_pct:.1f}% <= 5%, свап не нужен. Корректируем позицию.")
            amount1_desired_raw = balance1_raw

        # Если свап не удался, возвращаем ошибку
        if not swap_success:
            print(f"  ❌ Свап не удался, создание позиции отменено")
            return None

        # Минимальные проверки
        min_amount0, min_amount1 = 1, 1
        if amount0_desired_raw < min_amount0 and amount1_desired_raw < min_amount1:
            print(f"  Ошибка: Суммы меньше минимальных. Отмена.")
            return None

        # КРИТИЧНОЕ ИСПРАВЛЕНИЕ: Перепроверяем баланс после свапа
        final_balance0_raw = await self._get_token_balance_raw(self.token0_for_calcs)
        final_balance1_raw = await self._get_token_balance_raw(self.token1_for_calcs)
        
        # Корректируем amounts на основе реального баланса
        amount0_desired_raw = min(amount0_desired_raw, final_balance0_raw)
        amount1_desired_raw = min(amount1_desired_raw, final_balance1_raw)
        
        print(f"  Финальные amounts после коррекции: USDT={amount0_desired_raw}, BTCB={amount1_desired_raw}")

        # Быстрая проверка и установка разрешений
        approve_success_0 = await self._check_and_approve_token(
            self.token0_for_calcs, self.nonf_pos_manager_address, amount0_desired_raw
        ) if amount0_desired_raw > 0 else True

        approve_success_1 = await self._check_and_approve_token(
            self.token1_for_calcs, self.nonf_pos_manager_address, amount1_desired_raw
        ) if amount1_desired_raw > 0 else True

        if not approve_success_0 or not approve_success_1:
            print("  Ошибка разрешений. Отмена.")
            return None

        # === ОПТИМИЗАЦИЯ 2: Минимальные amounts = 0% ===
        min_ratio = Decimal("0.0")  # Используем 0% от желаемых amounts
        min_amount0 = int(amount0_desired_raw * min_ratio)
        min_amount1 = int(amount1_desired_raw * min_ratio)

        deadline = int(time.time()) + 120  # Уменьшено с 300 до 120 секунд (2 минуты)

        # Параметры для mint
        mint_params = {
            'token0': self.token0_for_calcs,
            'token1': self.token1_for_calcs,
            'fee': self.fee_tier,
            'tickLower': tick_lower,
            'tickUpper': tick_upper,
            'amount0Desired': amount0_desired_raw,
            'amount1Desired': amount1_desired_raw,
            'amount0Min': min_amount0,
            'amount1Min': min_amount1,
            'recipient': self.signer_address,
            'deadline': deadline
        }

        try:
            # === ОПТИМИЗАЦИЯ СКОРОСТИ: Кэшируем газ ===
            priority_gas_price = await self._get_priority_gas_price()
            max_priority_fee = max(100000000, priority_gas_price)  # Минимум 0.1 Gwei для BNB Chain

            # УБИРАЕМ лишние проверки балансов перед mint для ускорения
            # Проверяем только критическое изменение цены (более 0.1%)
            fresh_price, _, _ = await self.get_current_pool_state()
            print(f"🔍 DEBUG: current_price={current_price:.6f}, fresh_price={fresh_price:.6f}, change={(abs(fresh_price - current_price) / current_price * 100):.4f}%")
            if abs(fresh_price - current_price) / current_price > Decimal('0.0001'):  # 0.01% изменение цены
                print(f"⚠️ Цена изменилась {current_price:.2f} -> {fresh_price:.2f}, пересчитываем...")
                amount0_desired_raw, amount1_desired_raw = self._calculate_desired_amounts_for_position_from_capital(
                    tick_lower=tick_lower,
                    tick_upper=tick_upper,
                    current_price_param_t1_t0=fresh_price,
                    capital_usdt=capital_usdt,
                    is_smart_rebalance=is_smart_rebalance
                )
                min_amount0 = int(amount0_desired_raw * min_ratio)
                min_amount1 = int(amount1_desired_raw * min_ratio)

                # Быстрое обновление параметров mint
                mint_params.update({
                    'amount0Desired': amount0_desired_raw,
                    'amount1Desired': amount1_desired_raw,
                    'amount0Min': min_amount0,
                    'amount1Min': min_amount1
                })

            # === МАКСИМАЛЬНАЯ СКОРОСТЬ: Высокий газ + минимальные задержки ===
            tx = self.nonf_pos_manager.functions.mint(mint_params).build_transaction({
                'from': self.signer_address,
                'gas': 1500000,  # Увеличен gas limit для гарантии
                'maxFeePerGas': priority_gas_price * 2,  # Удвоенный газ для скорости
                'maxPriorityFeePerGas': max_priority_fee,
                'nonce': await self._get_next_nonce()
            })

            # Подписываем и отправляем БЕЗ задержек
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)

            print(f"  Транзакция mint отправлена: {tx_hash.hex()}")

            # Ждем подтверждения
            tx_receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)

            if tx_receipt['status'] == 1:
                print(f"  БЫСТРЫЙ Mint УСПЕШЕН! Tx: {tx_hash.hex()}")

                # Получаем ID токена из события
                token_id = None
                for log in tx_receipt['logs']:
                    if log['address'].lower() == self.nonf_pos_manager_address.lower():
                        try:
                            event = self.nonf_pos_manager.events.IncreaseLiquidity().process_log(log)
                            token_id = event['args']['tokenId']
                            break
                        except:
                            continue

                if token_id is None:
                    print("  Ошибка: Не удалось получить ID токена")
                    return None

                print(f"  Получен NFT ID: {token_id}")
                await asyncio.sleep(1)  # Уменьшено ожидание с 3 до 1 секунды

                # КРИТИЧНО: Получаем детали позиции ДО отправки в фарм
                try:
                    position_data = self.nonf_pos_manager.functions.positions(token_id).call()
                    liquidity = position_data[7]
                    print(f"  Получена информация о позиции: ликвидность={liquidity}")
                except Exception as e:
                    print(f"  Ошибка получения деталей позиции ДО фарминга: {e}")
                    # Устанавливаем значение по умолчанию если не удалось получить
                    liquidity = 0

                # Создаем информацию о позиции ДО отправки в фарм
                position_info = {
                    'nft_id': token_id,
                    'tickLower': tick_lower,
                    'tickUpper': tick_upper,
                    'liquidity': liquidity,
                    'amount0_actual_raw': amount0_desired_raw,
                    'amount1_actual_raw': amount1_desired_raw,
                    'initial_capital_usdt': float(capital_usdt),
                    'created_at_price': float(current_price),
                    'farm_staked': False  # Пока не в фарме
                }

                # Отправляем в фарминг если настроено
                if self.farm_address is not None:
                    print(f"  Отправка NFT {token_id} в фарминг...")
                    farm_success = await self.stake_nft_in_farm(token_id)
                    if farm_success:
                        print(f"  NFT {token_id} в фарминге")
                        position_info['farm_staked'] = True  # Обновляем статус фарминга
                    else:
                        print(f"  Ошибка фарминга NFT {token_id}")
                        position_info['farm_staked'] = False
                    # Записываем открытие позиции в лог
                    try:
                        # Получаем фактически добавленные amounts из события
                        actual_amount0 = 0
                        actual_amount1 = 0

                        # Ищем событие IncreaseLiquidity для получения фактических amounts
                        for log in tx_receipt['logs']:
                            if log['address'].lower() == self.nonf_pos_manager_address.lower():
                                try:
                                    # Пробуем декодировать как IncreaseLiquidity
                                    decoded = self.w3.eth.contract(address=self.nonf_pos_manager_address, abi=[{
                                        "anonymous": False,
                                        "inputs": [
                                            {"indexed": True, "name": "tokenId", "type": "uint256"},
                                            {"indexed": False, "name": "liquidity", "type": "uint128"},
                                            {"indexed": False, "name": "amount0", "type": "uint256"},
                                            {"indexed": False, "name": "amount1", "type": "uint256"}
                                        ],
                                        "name": "IncreaseLiquidity",
                                        "type": "event"
                                    }]).events.IncreaseLiquidity().processLog(log)
                                    
                                    actual_amount0 = decoded['args']['amount0']
                                    actual_amount1 = decoded['args']['amount1']
                                    break
                                except:
                                    continue

                        # Если не удалось получить из события, используем desired amounts
                        if actual_amount0 == 0 and actual_amount1 == 0:
                            actual_amount0 = amount0_desired_raw
                            actual_amount1 = amount1_desired_raw

                        current_usdt_human = Decimal(actual_amount0) / (Decimal(10) ** self.decimals0_for_calcs)
                        current_btcb_human = Decimal(actual_amount1) / (Decimal(10) ** self.decimals1_for_calcs)
                        total_value_usdc = current_usdt_human + (current_btcb_human * current_price)
                        
                        # Сохраняем начальные данные для P&L расчета
                        if not hasattr(self, 'initial_position_data'):
                            self.initial_position_data = {}
                        
                        self.initial_position_data[token_id] = {
                            'timestamp_open': pd.Timestamp.now(tz='UTC').isoformat(),
                            'initial_usdc': float(current_usdt_human),
                            'initial_cbbtc': float(current_btcb_human),
                            'initial_value_usdc': float(total_value_usdc),
                            'btcb_price_open': float(current_price),
                            'tick_lower': tick_lower,
                            'tick_upper': tick_upper,
                            'slot_id': slot_id
                        }
                        
                        print(f"  Позиция создана: ликвидность={liquidity}")
                        return position_info
                        
                    except Exception as e:
                        print(f"  Ошибка при обработке данных позиции: {e}")

        except Exception as e:
            print(f"  Ошибка при создании позиции: {e}")
            # Сбрасываем nonce кэш при ошибке для переинициализации
            if "nonce too low" in str(e).lower():
                print(f"  ⚠️ Обнаружена ошибка nonce, сбрасываем кэш")
                self._nonce_cache = None
            import traceback
            traceback.print_exc()
            return None   

    async def _execute_remove_liquidity_multicall(self, positions_to_close: list) -> bool:
        """
        Удаляет ликвидность из нескольких позиций через одну multicall транзакцию.
        
        Args:
            positions_to_close: список кортежей (slot_id, nft_id, position_info)
        
        Returns:
            bool: True в случае успеха
        """
        if not positions_to_close:
            return True
            
        print(f"\n[MULTICALL] Удаление ликвидности из {len(positions_to_close)} позиций")
        
        # Шаг 1: Выводим все NFT из фарминга, если есть фарм-контракт
        if self.farm_address:
            print(f"\n===== Вывод из фарминга =====")
            for slot_id, nft_id, _ in positions_to_close:
                # Сначала проверяем, находится ли NFT в фарме
                is_in_farm = await self._is_nft_in_farm(nft_id)
                
                if is_in_farm:
                    print(f"  NFT {nft_id} находится в фарме, выводим...")
                    unstake_success = await self._unstake_nft_from_farm(nft_id)
                    if not unstake_success:
                        print(f"❌ Не удалось вывести NFT {nft_id} из фарминга")
                        return False
                    print(f"✅ NFT {nft_id} успешно выведен из фарминга")
                    print("⏳ Ожидание 5 секунд после вывода NFT из фарминга...")
                    await asyncio.sleep(5)
                else:
                    print(f"  NFT {nft_id} не находится в фарме, пропускаем вывод")
        
        # Подготавливаем вызовы для multicall
        multicall_data = []
        deadline = int(time.time()) + 3600  # 1 час от текущего времени
        
        for slot_id, nft_id, position_info in positions_to_close:
            print(f"  Добавляем в multicall: слот {slot_id}, NFT {nft_id}")
            
            # 1. decreaseLiquidity (только если есть ликвидность)
            if int(position_info['liquidity']) > 0:
                decrease_params = (
                    nft_id,
                    int(position_info['liquidity']) & ((1 << 128) - 1),
                    0,  # amount0Min
                    0,  # amount1Min
                    deadline
                )
                decrease_call = self.nonf_pos_manager.encodeABI(
                    fn_name='decreaseLiquidity',
                    args=[decrease_params]
                )
                multicall_data.append(decrease_call)
            
            # 2. collect
            collect_params = (
                nft_id,
                self.signer_address,  # recipient
                2**128 - 1,  # amount0Max
                2**128 - 1   # amount1Max
            )
            collect_call = self.nonf_pos_manager.encodeABI(
                fn_name='collect',
                args=[collect_params]
            )
            multicall_data.append(collect_call)
            
            # 3. burn (всегда после collect - очищаем NFT)
            burn_call = self.nonf_pos_manager.encodeABI(
                fn_name='burn',
                args=[nft_id]
            )
            multicall_data.append(burn_call)
        
        # Получаем балансы ДО закрытия позиций для P&L расчета
        before_usdt_raw = await self._get_token_balance_raw(self.token0_for_calcs)
        before_btcb_raw = await self._get_token_balance_raw(self.token1_for_calcs)
        
        try:
            # Выполняем multicall
            gas_price_to_use = await self._get_gas_price()
            max_priority_fee = max(100000000, int(gas_price_to_use * 0.5))  # Минимум 0.1 Gwei
            
            multicall_func = self.nonf_pos_manager.functions.multicall(multicall_data)
            
            # Используем умную оценку газа для multicall
            tx_params = {
                "from": self.signer_address,
                "maxFeePerGas": gas_price_to_use,
                "maxPriorityFeePerGas": max_priority_fee
            }
            gas_estimate = await self.gas_manager.estimate_smart_gas(
                multicall_func, tx_params, "multicall"
            )
            
            multicall_tx = multicall_func.build_transaction({
                "from": self.signer_address,
                "gas": gas_estimate,
                "maxFeePerGas": gas_price_to_use,
                "maxPriorityFeePerGas": max_priority_fee,
                "nonce": await self._get_next_nonce()
            })
            
            signed_tx = self.w3.eth.account.sign_transaction(multicall_tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            print(f"  Multicall транзакция отправлена: {tx_hash.hex()}")
            
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status != 1:
                print(f"  Ошибка multicall транзакции: {tx_hash.hex()}")
                return False
                
            print(f"  Multicall успешно выполнен: {tx_hash.hex()}")
            
            # Получаем балансы ПОСЛЕ закрытия позиций
            after_usdt_raw = await self._get_token_balance_raw(self.token0_for_calcs)
            after_btcb_raw = await self._get_token_balance_raw(self.token1_for_calcs)
            
            # Логируем P&L с полными данными
            try:
                current_price_data = await self.get_current_pool_state()
                current_price_human = current_price_data[0]  # Исправлено: берем первый элемент кортежа
                
                # Рассчитываем общие комиссии
                total_fees_usdc = Decimal(after_usdt_raw - before_usdt_raw) / (Decimal(10) ** self.decimals0_for_calcs)
                total_fees_cbbtc = Decimal(after_btcb_raw - before_btcb_raw) / (Decimal(10) ** self.decimals1_for_calcs)
                
                # Получаем общую ликвидность всех закрываемых позиций для пропорционального распределения
                total_liquidity = Decimal(0)
                for slot_id, nft_id, position_info in positions_to_close:
                    if 'liquidity' in position_info:
                        total_liquidity += Decimal(str(position_info['liquidity']))
                
                for slot_id, nft_id, position_info in positions_to_close:
                    # Рассчитываем долю этой позиции в общих комиссиях
                    if total_liquidity > 0 and 'liquidity' in position_info:
                        position_liquidity = Decimal(str(position_info['liquidity']))
                        liquidity_ratio = position_liquidity / total_liquidity
                        
                        position_fees_usdc = total_fees_usdc * liquidity_ratio
                        position_fees_cbbtc = total_fees_cbbtc * liquidity_ratio
                    else:
                        # Если нет ликвидности или данных - равное распределение
                        position_fees_usdc = total_fees_usdc / len(positions_to_close)
                        position_fees_cbbtc = total_fees_cbbtc / len(positions_to_close)
                    
            except Exception as e:
                print(f"Ошибка при логировании P&L: {e}")
                import traceback
                traceback.print_exc()
            
            # Очищаем слоты (только для обычных позиций, не для осиротевших)
            for slot_id, nft_id, _ in positions_to_close:
                if slot_id >= 0:  # Обычная позиция из managed_positions_slots
                    self.managed_positions_slots[slot_id] = None
                    print(f"  Слот {slot_id} (NFT {nft_id}) очищен")
                else:  # Осиротевшая позиция (slot_id = -1)
                    print(f"  🚨 Осиротевшая позиция NFT {nft_id} закрыта")
            
            return True
            
        except Exception as e:
            print(f"  Ошибка при выполнении multicall: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def stake_nft_in_farm(self, token_id: int) -> bool:
        """
        Отправляет NFT в фарминг используя safeTransferFrom
        
        Args:
            token_id: ID NFT для отправки в фарминг
            
        Returns:
            bool: Успешность операции
        """
        print(f"Отправляем NFT с ID {token_id} в фарминг используя safeTransferFrom...")
        
        if not self.farm_address:
            print(f"Ошибка: Адрес фарминга не указан")
            return False
            
        print(f"Адрес фарминга: {self.farm_address}")
        
        # ABI для safeTransferFrom (ERC-721)
        nft_transfer_abi = [
            {
                "inputs": [
                    {"internalType": "address", "name": "from", "type": "address"},
                    {"internalType": "address", "name": "to", "type": "address"},
                    {"internalType": "uint256", "name": "tokenId", "type": "uint256"}
                ],
                "name": "safeTransferFrom",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function"
            }
        ]
        
        nft_contract = self.w3.eth.contract(address=self.nonf_pos_manager_address, abi=nft_transfer_abi)
        
        # Пробуем до 2 раз с увеличением газа
        for attempt in range(2):
            try:
                # Получаем газ (увеличиваем на 20% при повторной попытке)
                gas_price_to_use = await self._get_gas_price()
                if attempt > 0:
                    gas_price_to_use = int(gas_price_to_use * 1.2)
                    print(f"  Повторная попытка #{attempt + 1} с увеличенным газом: {gas_price_to_use}")
                
                max_priority_fee = max(100000000, int(gas_price_to_use * 0.5))  # Минимум 0.1 Gwei
                
                # Создаем транзакцию safeTransferFrom
                transfer_tx = nft_contract.functions.safeTransferFrom(
                    self.signer_address,  # от кого
                    self.farm_address,    # кому
                    token_id             # ID токена
                ).build_transaction({
                    "from": self.signer_address,
                    "nonce": await self._get_next_nonce(),
                    "gas": 500000,  # Фиксированное значение газа
                    "maxFeePerGas": gas_price_to_use,
                    "maxPriorityFeePerGas": max_priority_fee
                })
                
                # Подписываем и отправляем транзакцию
                signed_tx = self.w3.eth.account.sign_transaction(transfer_tx, self.private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
                
                print(f"Транзакция отправлена: {tx_hash.hex()}")
                
                # Ждем подтверждения транзакции
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                if receipt.status == 1:
                    print(f"NFT успешно отправлен в фарминг. Tx: {tx_hash.hex()}")
                    return True
                else:
                    print(f"Ошибка при отправке NFT в фарминг. Tx: {tx_hash.hex()}")
                    return False
                    
            except Exception as e:
                error_msg = str(e)
                print(f"Исключение при отправке NFT в фарминг (попытка {attempt + 1}): {error_msg}")
                
                # Если это ошибка "replacement transaction underpriced" и есть еще попытки
                if "replacement transaction underpriced" in error_msg and attempt < 1:
                    print("  Пробуем еще раз с увеличенным газом...")
                    await asyncio.sleep(2)  # Небольшая пауза
                    continue
                else:
                    import traceback
                    traceback.print_exc()
                    return False
        
        return False


    async def _wait_for_tokens_return(self, expected_min_value: Decimal = Decimal("10")) -> tuple[int, int, Decimal] | None:

        """
        Умное ожидание возврата токенов на баланс после закрытия позиций.
        
        Args:
            expected_min_value: Минимальная ожидаемая стоимость портфеля в USDT
            
        Returns:
            tuple: (wallet_usdt_raw, wallet_btcb_raw, total_value_usdc) или None при неудаче
        """
        print(f"⏳ Ждем возврата токенов на баланс (мин. ${expected_min_value:.2f})...")
        
        # Первичная пауза для обработки транзакций
        await asyncio.sleep(3)
        
        for attempt in range(12):  # 12 попыток = ~30 сек максимум
            try:
                # Получаем текущую цену
                current_price, _, _ = await self.get_current_pool_state()
                if not current_price:
                    if attempt < 11:
                        await asyncio.sleep(2)
                        continue
                    return None
                
                # Получаем актуальные балансы
                wallet_usdt_raw = await self._get_token_balance_raw(self.token0_for_calcs)
                wallet_btcb_raw = await self._get_token_balance_raw(self.token1_for_calcs)
                
                wallet_usdt_human = Decimal(wallet_usdt_raw) / (Decimal(10) ** self.decimals0_for_calcs)
                wallet_btcb_human = Decimal(wallet_btcb_raw) / (Decimal(10) ** self.decimals1_for_calcs)
                
                total_value_usdc = wallet_usdt_human + (wallet_btcb_human * current_price)
                
                print(f"  Попытка {attempt + 1}: USDT=${wallet_usdt_human:.2f}, BTCB={wallet_btcb_human:.8f}, Всего=${total_value_usdc:.2f}")
                
                # Если баланс достаточен - возвращаем результат
                if total_value_usdc >= expected_min_value:
                    print(f"✅ Токены вернулись на баланс: ${total_value_usdc:.2f}")
                    return wallet_usdt_raw, wallet_btcb_raw, total_value_usdc
                
                # Ждем еще
                if attempt < 11:
                    await asyncio.sleep(2.5)
                    
            except Exception as e:
                print(f"  Ошибка проверки баланса (попытка {attempt + 1}): {e}")
                if attempt < 11:
                    await asyncio.sleep(2)
        
        print("❌ Не дождались возврата токенов")
        return None

    async def _is_nft_in_farm(self, nft_id: int) -> bool:
        """
        Проверяет, находится ли NFT в фарминге.
        
        Args:
            nft_id: ID NFT позиции для проверки
            
        Returns:
            bool: True если NFT в фарме, False если нет
        """
        if not self.farm_address or not hasattr(self, 'farm_contract'):
            return False
            
        try:
            # Вызываем userPositionInfos(uint256 tokenId)
            position_info = self.farm_contract.functions.userPositionInfos(nft_id).call()
            
            # Если liquidity > 0, то NFT находится в фарме
            liquidity = position_info[0]  # Первый элемент - liquidity
            user_address = position_info[6]  # Седьмой элемент - user address
            
            # Проверяем что liquidity > 0 и пользователь - это мы
            is_in_farm = liquidity > 0 and user_address.lower() == self.signer_address.lower()
            
            print(f"    NFT {nft_id} в фарме: {is_in_farm} (liquidity={liquidity}, user={user_address})")
            return is_in_farm
            
        except Exception as e:
            # Если произошла ошибка (например, NFT не найден в фарме), считаем что его там нет
            print(f"    Проверка NFT {nft_id} в фарме: ошибка {e} - считаем что не в фарме")
            return False

    async def _unstake_nft_from_farm(self, nft_id: int) -> bool:
        """
        Выводит NFT из фарминга используя метод withdraw.
        
        Args:
            nft_id: ID NFT позиции для вывода
            
        Returns:
            bool: True в случае успеха, False в случае ошибки
        """
        print(f"  Попытка вывода NFT ID {nft_id} из фарминга...")
        
        try:
            # Вызываем withdraw(uint256 _tokenId, address _to)
            withdraw_func = self.farm_contract.functions.withdraw(
                nft_id,  # _tokenId
                self.signer_address  # _to
            )
            
            gas_price_to_use = await self._get_gas_price()
            
            # Используем умную оценку газа
            tx_params = {
                "from": self.signer_address,
                "maxFeePerGas": gas_price_to_use,
                "maxPriorityFeePerGas": max(100000000, int(gas_price_to_use * 0.5))
            }
            gas_estimate = await self.gas_manager.estimate_smart_gas(
                withdraw_func, tx_params, "withdraw"
            )
            
            withdraw_tx = withdraw_func.build_transaction({
                "from": self.signer_address,
                "gas": gas_estimate,
                "maxFeePerGas": gas_price_to_use,
                "maxPriorityFeePerGas": max(100000000, int(gas_price_to_use * 0.5)),
                "nonce": await self._get_next_nonce()
            })
            
            signed_tx = self.w3.eth.account.sign_transaction(withdraw_tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            print(f"    Транзакция withdraw из фарминга отправлена: {tx_hash.hex()}")
            
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status != 1:
                print(f"    Транзакция withdraw НЕ УДАЛАСЬ. Статус: {receipt.status}. Проверьте, находится ли NFT в фарме.")
                return False
                
            print(f"    NFT успешно выведен из фарминга. Tx: {tx_hash.hex()}")
            return True
            
        except Exception as e:
            print(f"    Произошла ошибка при выводе из фарминга: {e}")
            import traceback
            return False

    async def _calculate_smart_position_ranges_2_pos(self, current_price: Decimal, empty_slots: list) -> dict:
        """
        Умный расчет диапазонов для 2-позиционного режима.
        """
        print(f"\n🧠 2-позиционный умный расчет для {len(empty_slots)} пустых слотов")
        
        active_positions = [slot for slot in self.managed_positions_slots if slot is not None]
        current_tick = self.price_to_tick(self._human_price_param_t1_t0_to_raw_price_pool_t1_t0(current_price))
        
        # Получаем текущие балансы
        wallet_usdt_raw = await self._get_token_balance_raw(self.token0_for_calcs)
        wallet_btcb_raw = await self._get_token_balance_raw(self.token1_for_calcs)
        wallet_usdt_human = Decimal(wallet_usdt_raw) / (Decimal(10) ** self.decimals0_for_calcs)
        wallet_btcb_human = Decimal(wallet_btcb_raw) / (Decimal(10) ** self.decimals1_for_calcs)
        
        print(f"💰 Текущие балансы: USDT=${wallet_usdt_human:.2f}, BTCB={wallet_btcb_human:.8f}")
        
        total_portfolio_value_usdc = wallet_usdt_human + (wallet_btcb_human * current_price)
        capital_per_position = total_portfolio_value_usdc / Decimal(len(empty_slots))
        
        print(f"💼 Общая стоимость портфеля: ${total_portfolio_value_usdc:.2f}")
        print(f"💰 Капитал на позицию: ${capital_per_position:.2f}")
        
        created_positions = {}
        
        if len(active_positions) == 0:
            # Нет активных позиций - создаем стандартные 2 позиции
            print("📍 Нет активных позиций - создаем стандартные 2 позиции")
            target_ranges = self.calculate_target_ranges_2_positions(current_price)
            
            for slot_idx in empty_slots:
                if slot_idx < len(target_ranges):
                    range_info = target_ranges[slot_idx]
                    print(f"\n📊 Слот {slot_idx}: создание {range_info.get('position_type', 'неизвестного')} диапазона")
                    print(f"   Диапазон: [{range_info['tickLower']}, {range_info['tickUpper']}]")
                    
                    # Создаем позицию
                    new_pos_info = await self._execute_add_liquidity_fast(
                        slot_id=slot_idx,
                        tick_lower=range_info['tickLower'],
                        tick_upper=range_info['tickUpper'],
                        capital_usdt=capital_per_position,
                        is_smart_rebalance=True
                    )
                    
                    if new_pos_info:
                        self.managed_positions_slots[slot_idx] = new_pos_info
                        created_positions[slot_idx] = range_info
                        print(f"   ✅ Позиция создана в слоте {slot_idx}")
                    else:
                        print(f"   ❌ Не удалось создать позицию в слоте {slot_idx}")
        
        elif len(active_positions) == 1:
            # Одна позиция активна - создаем вторую на противоположной стороне
            pos = active_positions[0]
            slot_idx = empty_slots[0]
            
            print(f"📍 Активная позиция: слот {self.managed_positions_slots.index(pos)}, тики [{pos['tickLower']}, {pos['tickUpper']}]")
            print(f"📍 Текущий тик цены: {current_tick}")
            
            # Определяем где создавать новую позицию
            if current_tick < pos['tickLower']:
                # Цена ниже активной позиции - создаем позицию еще ниже
                position_width_ticks = 4
                new_tick_upper = pos['tickLower']
                new_tick_lower = new_tick_upper - position_width_ticks
                position_type = "below_price"
                print("📍 Создаем позицию НИЖЕ активной позиции")
                
            elif current_tick > pos['tickUpper']:
                # Цена выше активной позиции - создаем позицию еще выше
                position_width_ticks = 4
                new_tick_lower = pos['tickUpper']
                new_tick_upper = new_tick_lower + position_width_ticks
                position_type = "above_price"
                print("📍 Создаем позицию ВЫШЕ активной позиции")
                
            else:
                # Цена внутри активной позиции - создаем стандартные диапазоны
                print("📍 Цена внутри активной позиции - создаем стандартную вторую позицию")
                target_ranges = self.calculate_target_ranges_2_positions(current_price)
                
                # Определяем какая позиция отсутствует
                for i, range_info in enumerate(target_ranges):
                    if i in empty_slots:
                        new_tick_lower = range_info['tickLower']
                        new_tick_upper = range_info['tickUpper']
                        position_type = range_info['position_type']
                        break
            
            range_info = {
                'tickLower': self.align_tick_to_spacing(new_tick_lower),
                'tickUpper': self.align_tick_to_spacing(new_tick_upper),
                'position_type': position_type
            }
            
            print(f"📊 Создание позиции в слоте {slot_idx}: тики [{range_info['tickLower']}, {range_info['tickUpper']}]")
            
            # Создаем позицию
            new_pos_info = await self._execute_add_liquidity_fast(
                slot_id=slot_idx,
                tick_lower=range_info['tickLower'],
                tick_upper=range_info['tickUpper'],
                capital_usdt=capital_per_position,
                is_smart_rebalance=True
            )
            
            if new_pos_info:
                self.managed_positions_slots[slot_idx] = new_pos_info
                created_positions[slot_idx] = range_info
                print(f"   ✅ Позиция создана в слоте {slot_idx}")
            else:
                print(f"   ❌ Не удалось создать позицию в слоте {slot_idx}")
        
        # Сохраняем состояние
        self._save_state_to_file()
        
        return created_positions

    async def _perform_asymmetric_rebalance_2_positions(self, target_price: Decimal, rebalance_side: str):
        """
        Асимметричный ребаланс для 2-позиционного режима.
        Перемещает только одну позицию (дальнюю от цены).
        """
        print(f"\n🔄 Асимметричный ребаланс: перемещение {rebalance_side} позиции")
        
        active_positions = [slot for slot in self.managed_positions_slots if slot is not None]
        current_tick = self.price_to_tick(self._human_price_param_t1_t0_to_raw_price_pool_t1_t0(target_price))
        
        # Определяем какую позицию перемещать
        position_to_move = None
        slot_to_move = None
        
        if rebalance_side == "above":
            # Перемещаем верхнюю позицию (цена ниже диапазона)
            # Находим позицию с максимальным tickLower
            max_tick_lower = max([pos['tickLower'] for pos in active_positions])
            for i, slot in enumerate(self.managed_positions_slots):
                if slot and slot['tickLower'] == max_tick_lower:
                    position_to_move = slot
                    slot_to_move = i
                    break
            print(f"🎯 Перемещаем ВЕРХНЮЮ позицию: NFT {position_to_move['nft_id']} из слота {slot_to_move}")
            
        elif rebalance_side == "below":
            # Перемещаем нижнюю позицию (цена выше диапазона)
            # Находим позицию с минимальным tickUpper
            min_tick_upper = min([pos['tickUpper'] for pos in active_positions])
            for i, slot in enumerate(self.managed_positions_slots):
                if slot and slot['tickUpper'] == min_tick_upper:
                    position_to_move = slot
                    slot_to_move = i
                    break
            print(f"🎯 Перемещаем НИЖНЮЮ позицию: NFT {position_to_move['nft_id']} из слота {slot_to_move}")
        
        if not position_to_move:
            print("❌ Не удалось найти позицию для перемещения")
            return
        
        try:
            # 1. Закрываем позицию
            print(f"🗑️  Закрываем позицию NFT {position_to_move['nft_id']}")
            # Формируем правильный формат: (slot_id, nft_id, position_info)
            positions_to_close = [(slot_to_move, position_to_move['nft_id'], position_to_move)]
            success = await self._execute_remove_liquidity_multicall(positions_to_close)
            
            if not success:
                print("❌ Не удалось закрыть позицию")
                return
            
            # Очищаем слот
            self.managed_positions_slots[slot_to_move] = None
            
            # ⏳ ЗАДЕРЖКА: Ждем 2 секунды для синхронизации блокчейна
            import asyncio
            print("⏳ Ожидание 2 секунды для синхронизации блокчейна...")
            await asyncio.sleep(2)
            
            # 2. Создаем новую позицию вплотную к активной
            print(f"🆕 Создаем новую позицию в слоте {slot_to_move}")
            
            # 🚨 ФИКС: Рассчитываем новую позицию ВПЛОТНУЮ к существующей
            remaining_position = None
            for slot in self.managed_positions_slots:
                if slot is not None:
                    remaining_position = slot
                    break
            
            if not remaining_position:
                print("❌ Не найдена оставшаяся позиция")
                return
            
            # Определяем где создавать новую позицию относительно существующей
            if rebalance_side == "above":
                # rebalance_side="above" означает "перемещаем верхнюю позицию вниз" (цена ниже диапазона)
                # Значит нужно создать позицию НИЖЕ существующей (вплотную снизу)  
                new_tick_upper = remaining_position['tickLower']  # Заканчиваем где начинается существующая
                new_tick_lower = new_tick_upper - 4  # Ширина 4 тика
                print(f"🎯 Создаем позицию НИЖЕ существующей: [{new_tick_lower}, {new_tick_upper}]")
                
            elif rebalance_side == "below":
                # rebalance_side="below" означает "перемещаем нижнюю позицию вверх" (цена выше диапазона)
                # Значит нужно создать позицию ВЫШЕ существующей (вплотную сверху)
                new_tick_lower = remaining_position['tickUpper']  # Начинаем где заканчивается существующая
                new_tick_upper = new_tick_lower + 4  # Ширина 4 тика
                print(f"🎯 Создаем позицию ВЫШЕ существующей: [{new_tick_lower}, {new_tick_upper}]")
            
            new_range = {
                'tickLower': new_tick_lower,
                'tickUpper': new_tick_upper
            }
            
            # Проверяем что новая позиция отличается от существующих
            if (new_range['tickLower'] == remaining_position['tickLower'] and 
                new_range['tickUpper'] == remaining_position['tickUpper']):
                print(f"⚠️  ВНИМАНИЕ: Новая позиция [{new_range['tickLower']}, {new_range['tickUpper']}] идентична существующей!")
                print(f"⚠️  Это создаст дублирующую позицию. Пропускаем создание.")
                return
                
            # Получаем текущие балансы для расчета капитала
            wallet_usdt_raw = await self._get_token_balance_raw(self.token0_for_calcs)
            wallet_btcb_raw = await self._get_token_balance_raw(self.token1_for_calcs)
            
            wallet_usdt = Decimal(wallet_usdt_raw) / (Decimal(10) ** self.decimals0_for_calcs)
            wallet_cbbtc = Decimal(wallet_btcb_raw) / (Decimal(10) ** self.decimals1_for_calcs)
            
            # Общий капитал в USDT
            total_capital = wallet_usdt + (wallet_cbbtc * target_price)
            
            # 💰 50% капитала на каждую позицию в 2-позиционной стратегии
            capital_per_position = total_capital / Decimal(len(self.managed_positions_slots))
            
            print(f"💰 Капитал на позицию (50%): ${capital_per_position:.2f}")
            print(f"💰 Общий капитал: ${total_capital:.2f}")
            
            # Создаем позицию
            new_position = await self._execute_add_liquidity_fast(
                slot_id=slot_to_move,
                tick_lower=new_range['tickLower'],
                tick_upper=new_range['tickUpper'],
                capital_usdt=capital_per_position
            )
            
            if new_position:
                self.managed_positions_slots[slot_to_move] = new_position
                print(f"✅ Новая позиция создана: NFT {new_position['nft_id']}")
            else:
                print("❌ Не удалось создать новую позицию")
            
            # Сохраняем состояние
            self._save_state_to_file()
            print("🔄 Асимметричный ребаланс завершен")
            
        except Exception as e:
            print(f"❌ Ошибка асимметричного ребаланса: {e}")
            import traceback
            traceback.print_exc()

    async def _add_remaining_liquidity_to_positions(self):
        """
        Проверяет остатки после создания всех позиций и доливает их в позицию с наименьшим капиталом.
        Вызывается только когда все позиции созданы (2 для режима '2_positions', 3 для режима '3_positions').
        """
        # Проверяем что все позиции созданы
        active_positions = [slot for slot in self.managed_positions_slots if slot is not None]
        expected_positions = 2 if self.position_mode == '2_positions' else 3
        
        if len(active_positions) < expected_positions:
            return  # Не все позиции созданы, выходим
        
        # Получаем текущие балансы
        balance0_raw = await self._get_token_balance_raw(self.token0_for_calcs)
        balance1_raw = await self._get_token_balance_raw(self.token1_for_calcs)
        
        balance0_human = Decimal(balance0_raw) / (Decimal(10) ** self.decimals0_for_calcs)
        balance1_human = Decimal(balance1_raw) / (Decimal(10) ** self.decimals1_for_calcs)
        
        # Получаем текущую цену
        current_price, _, _ = await self.get_current_pool_state()
        total_remaining_value = balance0_human + (balance1_human * current_price)
        
        print(f"\n💰 Проверка остатков после создания всех позиций: ${total_remaining_value:.2f}")
        
        # Если остатки < $50 - не доливаем
        if total_remaining_value < Decimal("50"):
            print(f"💡 Остатки ${total_remaining_value:.2f} < $50 - доливка не требуется")
            return
            
        print(f"💡 Остатки ${total_remaining_value:.2f} ≥ $50 - доливаем в позицию с наименьшим капиталом")
        
        # Находим позицию с наименьшим капиталом
        position_values = []
        for i, pos in enumerate(self.managed_positions_slots):
            if pos and 'nft_id' in pos:
                try:
                    # Получаем информацию о позиции
                    on_chain_pos = self.nonf_pos_manager.functions.positions(pos['nft_id']).call()
                    
                    # Используем ликвидность из слота (уже правильно получена из фарма если нужно)
                    liquidity = pos.get('liquidity', 0)
                    
                    if liquidity > 0:
                        # Рассчитываем РЕАЛЬНУЮ стоимость позиции в USD
                        tick_lower = pos['tickLower']
                        tick_upper = pos['tickUpper']
                        
                        # ИСПРАВЛЕНИЕ: Простой и надежный расчет стоимости позиции
                        # Используем пропорциональную оценку основанную на ликвидности и ширине диапазона
                        
                        # Рассчитываем ширину позиции в тиках
                        width_ticks = tick_upper - tick_lower
                        
                        # Простая оценка: ликвидность * ширина * коэффициент
                        # Коэффициент подобран эмпирически для приближенного соответствия реальным стоимостям
                        liquidity_normalized = Decimal(liquidity) / Decimal(10**12)  # Нормализуем ликвидность
                        width_factor = Decimal(width_ticks) / Decimal(4)  # Нормализуем по стандартной ширине 4 тика
                        
                        # Приближенная стоимость = ликвидность * ширина * коэффициент
                        position_value_usd = liquidity_normalized * width_factor * Decimal("0.01")  # Коэффициент 0.01
                        
                        position_values.append((i, pos, position_value_usd))
                        print(f"  Найдена позиция NFT ID {pos['nft_id']}, оценка стоимости: ${position_value_usd:.2f} (liquidity: {liquidity}, width: {width_ticks} ticks)")
                except Exception as e:
                    print(f"⚠️  Ошибка оценки позиции NFT {pos.get('nft_id', 'unknown')}: {e}")
        
        if not position_values:
            print("❌ Не найдено активных позиций для доливки")
            return
            
        # Сортируем по стоимости (наименьшая первой)
        position_values.sort(key=lambda x: x[2])
        smallest_slot_idx, smallest_pos, smallest_value = position_values[0]
        
        print(f"🎯 Доливаем в позицию слота {smallest_slot_idx} (NFT {smallest_pos['nft_id']}, стоимость: ${smallest_value:.2f})")
        
        # Получаем тики позиции для расчета нужного соотношения токенов
        tick_lower = smallest_pos['tickLower']
        tick_upper = smallest_pos['tickUpper']
        
        try:
            # Используем ту же логику что и в _execute_add_liquidity_fast для расчета соотношений
            amount0_desired_raw, amount1_desired_raw = self._calculate_desired_amounts_for_position_from_capital(
                tick_lower=tick_lower,
                tick_upper=tick_upper, 
                current_price_param_t1_t0=current_price,
                capital_usdt=total_remaining_value,
                slot_index=smallest_slot_idx,
                is_smart_rebalance=True
            )
            
            print(f"📊 Рассчитаны amounts для доливки: USDT={Decimal(amount0_desired_raw) / (Decimal(10) ** self.decimals0_for_calcs):.2f}, BTCB={Decimal(amount1_desired_raw) / (Decimal(10) ** self.decimals1_for_calcs):.8f}")
            
            # Рассчитываем нехватку токенов и выполняем свап если нужно
            amount0_human = Decimal(amount0_desired_raw) / (Decimal(10) ** self.decimals0_for_calcs)
            amount1_human = Decimal(amount1_desired_raw) / (Decimal(10) ** self.decimals1_for_calcs)
            
            # Логика свапа как в _execute_add_liquidity_fast
            swap_success = True
            
            if amount0_desired_raw > balance0_raw:
                usdt_deficit = amount0_human - balance0_human
                usdt_deficit_pct = (usdt_deficit / amount0_human) * 100 if amount0_human > 0 else Decimal("0")
                
                if usdt_deficit_pct > 5:
                    btcb_to_sell = usdt_deficit / current_price
                    print(f"💱 Свапаем {btcb_to_sell:.8f} BTCB -> {usdt_deficit:.2f} USDT для доливки")
                    
                    if balance1_human >= btcb_to_sell:
                        amount_in_raw = int(btcb_to_sell * (Decimal(10) ** self.decimals1_for_calcs))
                        amount_out_min_raw = int(usdt_deficit * Decimal("0.99") * (Decimal(10) ** self.decimals0_for_calcs))
                        
                        swap_result, _ = await self._execute_swap(
                            self.token1_for_calcs,  # BTCB
                            self.token0_for_calcs,  # USDT  
                            amount_in_raw,
                            amount_out_min_raw,
                            self.swap_pool_fee_tier
                        )
                        swap_success = swap_result
                    else:
                        amount0_desired_raw = balance0_raw
                        
            elif amount1_desired_raw > balance1_raw:
                btcb_deficit = amount1_human - balance1_human
                btcb_deficit_pct = (btcb_deficit / amount1_human) * 100 if amount1_human > 0 else Decimal("0")
                
                if btcb_deficit_pct > 5:
                    usdt_to_sell = btcb_deficit * current_price
                    print(f"💱 Свапаем {usdt_to_sell:.2f} USDT -> {btcb_deficit:.8f} BTCB для доливки")
                    
                    if balance0_human >= usdt_to_sell:
                        amount_in_raw = int(usdt_to_sell * (Decimal(10) ** self.decimals0_for_calcs))
                        amount_out_min_raw = int(btcb_deficit * Decimal("0.99") * (Decimal(10) ** self.decimals1_for_calcs))
                        
                        swap_result, _ = await self._execute_swap(
                            self.token0_for_calcs,  # USDT
                            self.token1_for_calcs,  # BTCB
                            amount_in_raw, 
                            amount_out_min_raw,
                            self.swap_pool_fee_tier
                        )
                        swap_success = swap_result
                    else:
                        amount1_desired_raw = balance1_raw
            
            if not swap_success:
                print("❌ Свап для доливки не удался")
                return
                
            # Ждем немного после свапа для обновления балансов
            import asyncio
            await asyncio.sleep(1)
            
            # Получаем обновленные балансы после свапа
            balance0_raw = await self._get_token_balance_raw(self.token0_for_calcs)
            balance1_raw = await self._get_token_balance_raw(self.token1_for_calcs)
            
            # Корректируем amounts под актуальные балансы
            final_amount0 = min(amount0_desired_raw, balance0_raw)
            final_amount1 = min(amount1_desired_raw, balance1_raw)
            
            print(f"🔄 Финальные amounts для доливки: USDT={Decimal(final_amount0) / (Decimal(10) ** self.decimals0_for_calcs):.2f}, BTCB={Decimal(final_amount1) / (Decimal(10) ** self.decimals1_for_calcs):.8f}")
            
            # Проверяем что есть что доливать
            if final_amount0 == 0 and final_amount1 == 0:
                print("⚠️  Нечего доливать после корректировки")
                return
                
            # Выполняем increase liquidity
            success = await self._execute_increase_liquidity(
                nft_id=smallest_pos['nft_id'],
                amount0_desired=final_amount0,
                amount1_desired=final_amount1
            )
            
            if success:
                print(f"✅ Успешно долили ликвидность в позицию NFT {smallest_pos['nft_id']}")
            else:
                print(f"❌ Не удалось долить ликвидность в позицию NFT {smallest_pos['nft_id']}")
                
        except Exception as e:
            print(f"❌ Ошибка при доливке ликвидности: {e}")
            import traceback
            traceback.print_exc()
    
    async def _execute_increase_liquidity(self, nft_id: int, amount0_desired: int, amount1_desired: int) -> bool:
        """
        Увеличивает ликвидность в существующей позиции.
        
        Args:
            nft_id: ID NFT позиции
            amount0_desired: Желаемое количество токена 0 (raw)
            amount1_desired: Желаемое количество токена 1 (raw)
            
        Returns:
            bool: True если операция успешна
        """
        try:
            # Проверяем разрешения для токенов
            await self._check_and_approve_token(self.token0_for_calcs, self.nonf_pos_manager.address, amount0_desired)
            await self._check_and_approve_token(self.token1_for_calcs, self.nonf_pos_manager.address, amount1_desired)
            
            # Параметры для increaseLiquidity
            increase_params = {
                'tokenId': nft_id,
                'amount0Desired': amount0_desired,
                'amount1Desired': amount1_desired,
                'amount0Min': int(amount0_desired * Decimal('0.80')),  # 20% slippage
                'amount1Min': int(amount1_desired * Decimal('0.80')),  # 20% slippage
                'deadline': int(time.time()) + 300  # 5 минут
            }
            
            print(f"🔧 Увеличиваем ликвидность NFT {nft_id}: amount0={amount0_desired}, amount1={amount1_desired}")
            
            # Получаем nonce
            nonce = await self._get_next_nonce()
            
            # Рассчитываем газ
            try:
                gas_estimate = self.nonf_pos_manager.functions.increaseLiquidity(increase_params).estimate_gas({
                    'from': self.signer_address,
                    'nonce': nonce
                })
                gas_limit = int(gas_estimate * Decimal('1.2'))  # 20% буфер
            except Exception as e:
                print(f"⚠️ Не удалось оценить газ для increaseLiquidity: {e}")
                gas_limit = 300000  # Фиксированный лимит
            
            # Получаем цены газа
            base_fee = self.w3.eth.get_block('latest')['baseFeePerGas']
            priority_fee = int(base_fee * Decimal('0.5'))
            max_fee = base_fee + priority_fee
            
            print(f"  Приоритетный газ: {priority_fee} (базовый: {base_fee})")
            
            # Создаем транзакцию
            transaction = self.nonf_pos_manager.functions.increaseLiquidity(increase_params).build_transaction({
                'from': self.signer_address,
                'gas': gas_limit,
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee,
                'nonce': nonce,
                'type': 2
            })
            
            # Подписываем и отправляем
            signed_txn = self.w3.eth.account.sign_transaction(transaction, private_key=self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.rawTransaction)
            
            print(f"  Транзакция increaseLiquidity отправлена: {tx_hash.hex()}")
            
            # Ждем подтверждения
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            
            if receipt.status == 1:
                print(f"  ✅ Ликвидность успешно увеличена. Tx: {tx_hash.hex()}")
                return True
            else:
                print(f"  ❌ Транзакция increaseLiquidity провалилась: {tx_hash.hex()}")
                return False
                
        except Exception as e:
            print(f"❌ Ошибка увеличения ликвидности: {e}")
            return False
    
    def _calculate_amounts_from_liquidity(self, liquidity: int, sqrt_price_x96: int, tick_lower: int, tick_upper: int) -> tuple:
        """
        Рассчитывает количество токенов в позиции по ликвидности.
        Использует правильные формулы Uniswap V3.
        
        Args:
            liquidity: Ликвидность позиции
            sqrt_price_x96: Текущая цена пула
            tick_lower: Нижний тик
            tick_upper: Верхний тик
            
        Returns:
            tuple: (amount0, amount1) в raw формате
        """
        try:
            from decimal import Decimal, getcontext
            getcontext().prec = 50  # Высокая точность для расчетов
            
            # Конвертируем в Decimal для точных расчетов
            L = Decimal(str(liquidity))
            sqrt_price_current = Decimal(str(sqrt_price_x96))
            
            # Рассчитываем sqrt цены для тиков
            sqrt_price_lower = Decimal(str(1.0001)) ** (Decimal(str(tick_lower)) / 2) * (Decimal(2) ** 96)
            sqrt_price_upper = Decimal(str(1.0001)) ** (Decimal(str(tick_upper)) / 2) * (Decimal(2) ** 96)
            
            # Обеспечиваем правильный порядок
            if sqrt_price_lower > sqrt_price_upper:
                sqrt_price_lower, sqrt_price_upper = sqrt_price_upper, sqrt_price_lower
            
            if sqrt_price_current <= sqrt_price_lower:
                # Цена ниже диапазона - только token1
                amount0 = Decimal(0)
                amount1 = L * (sqrt_price_upper - sqrt_price_lower) / (sqrt_price_lower * sqrt_price_upper) * (Decimal(2) ** 96)
            elif sqrt_price_current >= sqrt_price_upper:
                # Цена выше диапазона - только token0
                amount0 = L * (sqrt_price_upper - sqrt_price_lower) / (Decimal(2) ** 96)
                amount1 = Decimal(0)
            else:
                # Цена внутри диапазона - оба токена
                amount0 = L * (sqrt_price_current - sqrt_price_lower) / (Decimal(2) ** 96)
                amount1 = L * (sqrt_price_upper - sqrt_price_current) / (sqrt_price_current * sqrt_price_upper) * (Decimal(2) ** 96)
            
            return (max(0, int(amount0)), max(0, int(amount1)))
            
        except Exception as e:
            print(f"⚠️  Ошибка расчета amounts из ликвидности: {e}")
            return (0, 0)

