# src/close_all.py
"""
Модуль для закрытия всех ликвидных позиций.
Извлекает позиции из фарминга, собирает награды, удаляет ликвидность и сжигает NFT.
Использует multicall для оптимизации.
"""
import os
import json
import time
import asyncio
import binascii
from decimal import Decimal, getcontext
from pathlib import Path
from web3 import Web3
from dotenv import load_dotenv
from eth_abi.abi import encode

# Импортируем GasManager из liquidity_manager
import sys
sys.path.append(str(Path(__file__).parent))
from liquidity_manager import GasManager

# Устанавливаем точность для Decimal
getcontext().prec = 36

load_dotenv()

# Определяем корень проекта
project_root = Path(__file__).parent.parent

# Константы из окружения
NONF_POS_MANAGER_ADDRESS_ENV = os.getenv("NONF_POS_MANAGER_ADDRESS")
NONF_POS_MANAGER_ABI_JSON_PATH = os.getenv("NONF_POS_MANAGER_ABI_JSON_PATH")

# Селекторы для взаимодействия с роутером
EXECUTE_SELECTOR = "0x3593564c"  # Селектор для execute

# Команды Universal Router для различных операций
UNIVERSAL_ROUTER_COMMANDS = {
    "V3_SWAP_EXACT_IN": 0x00,   # Код команды для свапа с точным входом в V3
}

# Константа для swap transactions (в PPM - частях на миллион)
FEE_TIER_FOR_SWAP_TRANSACTION = 100  # Используем 0.01% fee tier для свапов


class PositionCloser:
    """Класс для закрытия всех ликвидных позиций через multicall"""
    
    def __init__(self, 
                 rpc_url: str, 
                 signer_address: str, 
                 private_key: str,
                 pool_address: str, 
                 token0_address: str, 
                 token1_address: str,
                 token0_decimals: int, 
                 token1_decimals: int,
                 fee_tier: int,
                 farm_address: str | None = None,
                 farm_abi_path: str | None = None):
        
        # Инициализируем Web3
        # Инициализируем Web3 с поддержкой PoA (BNB Chain)
        from web3.middleware import geth_poa_middleware
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        self.signer_address = Web3.to_checksum_address(signer_address)
        self.private_key = private_key
        
        # Инициализируем газ менеджер
        self.gas_manager = GasManager(self.w3)
        
        # Кэш для nonce
        self._nonce_cache = None
        
        # Параметры токенов
        self.token0_address = Web3.to_checksum_address(token0_address)
        self.token1_address = Web3.to_checksum_address(token1_address)
        self.token0_decimals = token0_decimals
        self.token1_decimals = token1_decimals
        self.fee_tier = fee_tier
        
        # Инициализируем контракт фарминга если указан адрес
        if farm_address:
            self.farm_address = Web3.to_checksum_address(farm_address)
            if farm_abi_path and Path(farm_abi_path).exists():
                with open(farm_abi_path, 'r') as f:
                    farm_abi = json.load(f)
                self.farm_contract = self.w3.eth.contract(address=self.farm_address, abi=farm_abi)
            else:
                self.farm_contract = None
                print(f"  Файл ABI фарминга не найден: {farm_abi_path}")
        else:
            self.farm_address = None
            self.farm_contract = None
        
        # Инициализируем Position Manager
        if not NONF_POS_MANAGER_ADDRESS_ENV:
            raise ValueError("NONF_POS_MANAGER_ADDRESS не задан в .env")
        if not NONF_POS_MANAGER_ABI_JSON_PATH:
            raise ValueError("NONF_POS_MANAGER_ABI_JSON_PATH не задан в .env")
            
        self.nonf_pos_manager_address = Web3.to_checksum_address(NONF_POS_MANAGER_ADDRESS_ENV)
        abi_path = Path(NONF_POS_MANAGER_ABI_JSON_PATH)
        if not abi_path.is_absolute():
            abi_path = project_root / abi_path
            
        if not abi_path.exists():
            raise FileNotFoundError(f"ABI файл не найден: {abi_path}")
            
        with open(abi_path, 'r') as f:
            manager_abi = json.load(f)
        self.nonf_pos_manager = self.w3.eth.contract(
            address=self.nonf_pos_manager_address, 
            abi=manager_abi
        )
        
        if not self.w3.is_connected():
            raise ConnectionError(f"Не удалось подключиться к RPC: {rpc_url}")
        
        print(f"PositionCloser инициализирован для пула {pool_address}")
        print(f"Фарминг: {'включен' if self.farm_address else 'выключен'}")

    async def get_all_my_positions(self) -> list:
        """
        Получает список всех позиций пользователя (и на кошельке, и в фарминге)
        
        Returns:
            list: Список словарей с информацией о позициях для multicall
        """
        print("Запрашиваем все NFT позиции пользователя...")
        all_positions = []
        
        # 1. Получаем позиции с кошелька
        wallet_positions = await self._get_wallet_positions()
        all_positions.extend(wallet_positions)
        
        # 2. Получаем позиции из фарминга (если фарминг настроен)
        if self.farm_contract:
            farm_positions = await self._get_farm_positions()
            all_positions.extend(farm_positions)
        
        print(f"  Общее количество позиций: {len(all_positions)}")
        return all_positions

    async def _get_wallet_positions(self) -> list:
        """Получает позиции с кошелька пользователя в формате для multicall"""
        print("  Проверяем позиции на кошельке...")
        
        try:
            # Определяем количество NFT токенов у пользователя на кошельке
            nft_count = self.nonf_pos_manager.functions.balanceOf(self.signer_address).call()
            
            if nft_count == 0:
                print("    На кошельке нет NFT позиций")
                return []
                
            print(f"    Найдено {nft_count} NFT позиций на кошельке")
            
            positions_info = []
            for i in range(nft_count):
                try:
                    # Получаем ID токена по индексу
                    token_id = self.nonf_pos_manager.functions.tokenOfOwnerByIndex(self.signer_address, i).call()
                    
                    # Запрашиваем данные о позиции
                    position_data = self.nonf_pos_manager.functions.positions(token_id).call()
                    
                    # Извлекаем данные из ответа
                    token0 = position_data[2]
                    token1 = position_data[3]
                    fee = position_data[4]
                    tick_lower = position_data[5]
                    tick_upper = position_data[6]
                    liquidity = position_data[7]
                    
                    # Проверяем соответствие нашему пулу
                    if (token0.lower() == self.token0_address.lower() and 
                        token1.lower() == self.token1_address.lower() and 
                        fee == self.fee_tier):
                        
                        # Формат для multicall (slot_id, nft_id, position_info)
                        position_for_multicall = (
                            f"wallet_{i}",  # используем псевдо slot_id
                            token_id,
                            {
                                'liquidity': str(liquidity),
                                'tickLower': tick_lower,
                                'tickUpper': tick_upper,
                                'location': 'wallet'
                            }
                        )
                        positions_info.append(position_for_multicall)
                        print(f"      Найдена позиция NFT ID {token_id}, ликвидность: {liquidity}")
                    else:
                        print(f"      NFT ID {token_id} не соответствует нашему пулу")
                        
                except Exception as e:
                    print(f"      Ошибка при обработке позиции {i}: {e}")
                    continue
            
            print(f"    Подходящих позиций на кошельке: {len(positions_info)}")
            return positions_info
            
        except Exception as e:
            print(f"    Ошибка при получении позиций с кошелька: {e}")
            return []

    async def _get_farm_positions(self) -> list:
        """Получает позиции из фарминга в формате для multicall"""
        print("    Проверяем позиции в фарминге...")
        
        try:
            # Получаем количество позиций в фарминге
            farm_balance = self.farm_contract.functions.balanceOf(self.signer_address).call()
            
            if farm_balance == 0:
                print("      В фарминге нет позиций")
                return []
                
            print(f"      Найдено {farm_balance} позиций в фарминге")
            
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
                        print(f"        NFT ID {token_id} принадлежит другому пользователю")
                        continue
                    
                    # Получаем информацию о пуле
                    pool_info = self.farm_contract.functions.poolInfo(pid).call()
                    # Структура poolInfo: allocPoint, v3Pool, token0, token1, fee, totalLiquidity, totalBoostLiquidity
                    token0 = pool_info[2]
                    token1 = pool_info[3]
                    fee = pool_info[4]
                    
                    # Проверяем соответствие нашему пулу
                    if (token0.lower() == self.token0_address.lower() and 
                        token1.lower() == self.token1_address.lower() and 
                        fee == self.fee_tier):
                        
                        # Формат для multicall (slot_id, nft_id, position_info)
                        position_for_multicall = (
                            f"farm_{i}",  # используем псевдо slot_id
                            token_id,
                            {
                                'liquidity': str(liquidity),
                                'tickLower': tick_lower,
                                'tickUpper': tick_upper,
                                'location': 'farm'
                            }
                        )
                        positions_info.append(position_for_multicall)
                        print(f"        Найдена позиция в фарминге NFT ID {token_id}, ликвидность: {liquidity}")
                    else:
                        print(f"        NFT ID {token_id} в фарминге не соответствует нашему пулу")
                        
                except Exception as e:
                    print(f"        Ошибка при обработке фарм позиции {i}: {e}")
                    continue
            
            print(f"      Подходящих позиций в фарминге: {len(positions_info)}")
            return positions_info
            
        except Exception as e:
            print(f"      Ошибка при получении позиций из фарминга: {e}")
            return []

    async def _unstake_nft_from_farm(self, nft_id: int) -> bool:
        """
        Выводит NFT из фарминга используя метод withdraw (копия из liquidity_manager.py)
        
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
            print(f"    Транзакция withdraw отправлена: {tx_hash.hex()}")
            
            # Обновляем nonce кэш
            self._update_nonce_cache(withdraw_tx['nonce'])
            
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt["status"] != 1:
                print(f"    Ошибка при выводе из фарминга. Tx: {tx_hash.hex()}")
                return False
                
            print(f"    NFT успешно выведен из фарминга. Tx: {tx_hash.hex()}")
            return True
            
        except Exception as e:
            print(f"    Ошибка при выводе из фарминга: {e}")
            return False

    async def _get_next_nonce(self, use_pending=True):
        """Получает следующий nonce с кэшированием"""
        try:
            if self._nonce_cache is None:
                if use_pending:
                    self._nonce_cache = self.w3.eth.get_transaction_count(self.signer_address, 'pending')
                else:
                    self._nonce_cache = self.w3.eth.get_transaction_count(self.signer_address, 'latest')
            return self._nonce_cache
        except Exception as e:
            print(f"  Ошибка при получении nonce: {e}")
            # fallback к простому получению
            return self.w3.eth.get_transaction_count(self.signer_address, 'latest')
    
    def _update_nonce_cache(self, used_nonce: int):
        """Обновляет кэш nonce после использования"""
        if self._nonce_cache is not None and used_nonce >= self._nonce_cache:
            self._nonce_cache = used_nonce + 1
    
    async def _get_gas_price(self) -> int:
        """Получает актуальную цену газа через GasManager"""
        return await self.gas_manager.get_current_gas_price()

    async def _get_token_balance_raw(self, token_address: str) -> int:
        """Получает баланс токена в сырых единицах"""
        try:
            # Создаем минимальный ERC-20 контракт для балансов
            erc20_abi = [
                {
                    "constant": True,
                    "inputs": [{"name": "_owner", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "balance", "type": "uint256"}],
                    "type": "function"
                }
            ]
            
            token_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(token_address), 
                abi=erc20_abi
            )
            balance = token_contract.functions.balanceOf(self.signer_address).call()
            return balance
        except Exception as e:
            print(f"  Ошибка при получении баланса токена {token_address}: {e}")
            return 0

    async def _wait_for_tokens_return(self, expected_min_value: Decimal = Decimal("10")) -> tuple[int, int, Decimal] | None:
        """
        Ожидает возврата токенов на кошелек после закрытия позиций
        
        Args:
            expected_min_value: Минимальная ожидаемая стоимость портфеля в USDT
            
        Returns:
            tuple[int, int, Decimal] | None: (usdt_raw, btcb_raw, total_value_usd) или None при тайм-ауте
        """
        print(f"    Ожидаем возврата токенов (мин. стоимость: ${expected_min_value})")
        
        # Получаем текущую цену для оценки стоимости
        try:
            current_price_human, _, _ = await self.get_current_pool_state()
        except Exception as e:
            print(f"    Ошибка получения цены: {e}, используем fallback")
            current_price_human = Decimal("118000")  # fallback цена
        
        max_wait_time = 60  # 60 секунд максимум
        check_interval = 3   # проверяем каждые 3 секунды
        elapsed_time = 0
        
        while elapsed_time < max_wait_time:
            # Получаем текущие балансы
            usdt_raw = await self._get_token_balance_raw(self.token0_address)
            btcb_raw = await self._get_token_balance_raw(self.token1_address)
            
            # Конвертируем в human значения
            usdt_human = Decimal(usdt_raw) / (Decimal(10) ** self.token0_decimals)
            btcb_human = Decimal(btcb_raw) / (Decimal(10) ** self.token1_decimals)
            
            # Рассчитываем общую стоимость в USDT
            total_value = usdt_human + (btcb_human * current_price_human)
            
            print(f"    [{elapsed_time}s] USDT: {usdt_human:.2f}, BTCB: {btcb_human:.8f}, Всего: ${total_value:.2f}")
            
            # Проверяем, достигли ли мы минимальной стоимости
            if total_value >= expected_min_value:
                print(f"    ✅ Токены вернулись! Общая стоимость: ${total_value:.2f}")
                return usdt_raw, btcb_raw, total_value
            
            # Ждем перед следующей проверкой
            await asyncio.sleep(check_interval)
            elapsed_time += check_interval
        
        print(f"    ⏰ Тайм-аут ожидания ({max_wait_time}s)")
        
        # Возвращаем последние известные значения
        usdt_raw = await self._get_token_balance_raw(self.token0_address)
        btcb_raw = await self._get_token_balance_raw(self.token1_address)
        usdt_human = Decimal(usdt_raw) / (Decimal(10) ** self.token0_decimals)
        btcb_human = Decimal(btcb_raw) / (Decimal(10) ** self.token1_decimals)
        total_value = usdt_human + (btcb_human * current_price_human)
        
        return usdt_raw, btcb_raw, total_value

    async def _execute_remove_liquidity_multicall(self, positions_to_close: list) -> bool:
        """
        Удаляет ликвидность из нескольких позиций через одну multicall транзакцию.
        Скопировано из liquidity_manager.py с адаптацией под close_all.py
        
        Args:
            positions_to_close: список кортежей (slot_id, nft_id, position_info)
        
        Returns:
            bool: True в случае успеха
        """
        if not positions_to_close:
            return True
            
        print(f"\n[MULTICALL] Удаление ликвидности из {len(positions_to_close)} позиций")
        
        # Шаг 1: Выводим все NFT из фарминга, если есть фарм-контракт
        if self.farm_address and self.farm_contract:
            print(f"\n===== Вывод из фарминга =====")
            for slot_id, nft_id, position_info in positions_to_close:
                if position_info.get('location') == 'farm':
                    unstake_success = await self._unstake_nft_from_farm(nft_id)
                    if not unstake_success:
                        print(f"Не удалось вывести NFT {nft_id} из фарминга")
                        return False
                    print(f"NFT {nft_id} успешно выведен из фарминга")
                else:
                    print(f"NFT {nft_id} находится на кошельке, пропускаем вывод из фарминга")
        
        # Подготавливаем вызовы для multicall
        multicall_data = []
        deadline = int(time.time()) + 3600  # 1 час от текущего времени
        
        for slot_id, nft_id, position_info in positions_to_close:
            print(f"  Добавляем в multicall: слот {slot_id}, NFT {nft_id}")
            
            # 1. decreaseLiquidity (только если есть ликвидность)
            liquidity_value = int(position_info['liquidity'])
            if liquidity_value > 0:
                decrease_params = (
                    nft_id,
                    liquidity_value & ((1 << 128) - 1),  # Ограничиваем до uint128
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
        
        # Получаем балансы ДО закрытия позиций
        before_usdt_raw = await self._get_token_balance_raw(self.token0_address)
        before_btcb_raw = await self._get_token_balance_raw(self.token1_address)
        
        # Пробуем multicall до 2 раз с увеличением газа
        for attempt in range(2):
            try:
                # Получаем газ (увеличиваем на 30% при повторной попытке)
                gas_price_to_use = await self._get_gas_price()
                if attempt > 0:
                    gas_price_to_use = int(gas_price_to_use * 1.3)
                    print(f"  Повторная попытка multicall #{attempt + 1} с увеличенным газом: {gas_price_to_use}")
                
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
                
                # Обновляем nonce кэш
                self._update_nonce_cache(multicall_tx['nonce'])
                
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                if receipt["status"] != 1:
                    print(f"  Ошибка multicall транзакции: {tx_hash.hex()}")
                    return False
                    
                print(f"  Multicall успешно выполнен: {tx_hash.hex()}")
                break  # Успешно выполнено
                
            except Exception as e:
                error_msg = str(e)
                print(f"  Ошибка при выполнении multicall (попытка {attempt + 1}): {error_msg}")
                
                # Если это ошибка nonce/gas и есть еще попытки
                if ("replacement transaction underpriced" in error_msg or 
                    "nonce too low" in error_msg or
                    "insufficient funds" in error_msg) and attempt < 1:
                    print("  Пробуем еще раз с обновленными параметрами...")
                    # Сбрасываем nonce кэш для получения актуального значения
                    self._nonce_cache = None
                    await asyncio.sleep(3)  # Пауза
                    continue
                else:
                    import traceback
                    traceback.print_exc()
                    return False
        else:
            print("  Все попытки multicall исчерпаны")
            return False
            
        # Ждем возврата токенов на кошелек
        print("  Ожидаем возврата токенов на кошелек...")
        tokens_returned = await self._wait_for_tokens_return()
        
        if tokens_returned:
            after_usdt_raw, after_btcb_raw, total_value = tokens_returned
            
            # Показываем статистику полученных токенов
            gained_usdc = Decimal(after_usdt_raw - before_usdt_raw) / (Decimal(10) ** self.token0_decimals)
            gained_cbbtc = Decimal(after_btcb_raw - before_btcb_raw) / (Decimal(10) ** self.token1_decimals)
            
            print(f"  Получено токенов:")
            print(f"    USDT: {gained_usdc:.6f}")
            print(f"    BTCB: {gained_cbbtc:.8f}")
            print(f"    Общая стоимость: ${total_value:.2f}")
        else:
            print("  Тайм-аут ожидания возврата токенов")
        
        return True

    async def close_all_positions_multicall(self) -> dict:
        """
        Закрывает все позиции пользователя через multicall
        
        Returns:
            dict: Статистика закрытия
        """
        print("\n🔥 ЗАКРЫТИЕ ВСЕХ ПОЗИЦИЙ ЧЕРЕЗ MULTICALL 🔥")
        
        # Получаем все позиции в формате для multicall
        positions = await self.get_all_my_positions()
        
        if not positions:
            print("Нет позиций для закрытия")
            return {"total": 0, "closed": 0, "failed": 0}
        
        print(f"Найдено {len(positions)} позиций для закрытия")
        
        # Выполняем multicall для всех позиций сразу
        success = await self._execute_remove_liquidity_multicall(positions)
        
        if success:
            print(f"\n🎉 Все {len(positions)} позиций успешно закрыты через multicall!")
            return {"total": len(positions), "closed": len(positions), "failed": 0}
        else:
            print(f"\n❌ Ошибка при закрытии позиций через multicall")
            return {"total": len(positions), "closed": 0, "failed": len(positions)}

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
                print(f"  [1/2] ERC20.approve(Permit2)...")
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
                self._update_nonce_cache(approve_tx['nonce'])
                print(f"    ERC20 approve: {tx_hash.hex()}")
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                
                if receipt.status != 1:
                    print(f"    Ошибка ERC20 approve")
                    return False
                print(f"    ✅ ERC20 approve успешно")
            else:
                print(f"  [1/2] ERC20 allowance достаточно")
            
            # Шаг 2: Permit2.approve(Router, amount, expiration)
            permit2_abi_path = os.path.join(os.path.dirname(__file__), 'abi', 'PANCAKESWAP_PERMIT2_ADDRESS.json')
            with open(permit2_abi_path, 'r') as f:
                permit2_abi = json.load(f)
            
            permit2_contract = self.w3.eth.contract(address=permit2_address, abi=permit2_abi)
            permit2_allowance_data = permit2_contract.functions.allowance(
                self.signer_address, token_address, router_address
            ).call()
            
            current_permit2_amount = permit2_allowance_data[0]
            current_expiration = permit2_allowance_data[1]
            
            import time
            needs_permit2_approve = (
                current_permit2_amount < amount_raw or 
                current_expiration < int(time.time()) + 3600
            )
            
            if needs_permit2_approve:
                print(f"  [2/2] Permit2.approve(Router)...")
                max_uint160 = 2**160 - 1
                max_uint48 = 2**48 - 1
                
                permit2_approve_func = permit2_contract.functions.approve(
                    token_address, router_address, max_uint160, max_uint48
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
                self._update_nonce_cache(permit2_tx['nonce'])
                print(f"    Permit2 approve: {tx_hash.hex()}")
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                
                if receipt.status != 1:
                    print(f"    Ошибка Permit2 approve")
                    return False
                print(f"    ✅ Permit2 approve успешно")
            else:
                print(f"  [2/2] Permit2 allowance достаточно")
            
            return True
            
        except Exception as e:
            print(f"    Исключение при approve: {e}")
            return False

    async def _check_and_approve_token(self, token_address_to_approve: str, spender_address: str, amount_raw: int):
        """Проверяет и устанавливает разрешение на токен (скопировано из liquidity_manager.py)"""
        token_address = Web3.to_checksum_address(token_address_to_approve)
        spender_address = Web3.to_checksum_address(spender_address)
        
        # Стандартный ERC20 ABI для allowance и approve
        erc20_abi = json.loads('''
        [
            {"constant":true,"inputs":[{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},
            {"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"}
        ]
        ''')
        token_contract = self.w3.eth.contract(address=token_address, abi=erc20_abi)
        
        # СНАЧАЛА проверяем allowance - если достаточно, то approve не нужен
        try:
            current_allowance = token_contract.functions.allowance(self.signer_address, spender_address).call()
        except Exception as e:
            print(f"  Ошибка при проверке allowance для {token_address}: {e}")
            return False # Не можем проверить, считаем, что нет разрешения

        if current_allowance >= amount_raw:
            print(f"  Разрешение для токена {token_address} для {spender_address} уже достаточно ({current_allowance}).")
            return True
        
        # ТОЛЬКО если allowance недостаточно - проверяем баланс и делаем approve
        current_balance = await self._get_token_balance_raw(token_address)
        if current_balance < amount_raw:
            print(f"  Ошибка: Недостаточный баланс токена {token_address} для approve. На кошельке: {current_balance}, требуется: {amount_raw}")
            return False
        
        print(f"  Установка разрешения для токена {token_address} на сумму {amount_raw} для {spender_address}...")
        try:
            approve_func = token_contract.functions.approve(spender_address, amount_raw)
            # Используем фиксированный газ вместо estimate_gas
            gas_to_use = 200000  # Фиксированный лимит газа для approve
            base_gas_price = await self._get_gas_price()
            max_priority_fee = max(100000000, int(base_gas_price * 0.5))  # Минимум 0.1 Gwei
            
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
            print(f"    Транзакция approve отправлена: {tx_hash.hex()}")
            
            # Обновляем nonce кэш
            self._update_nonce_cache(approve_tx['nonce'])
            
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status == 1:
                print(f"    Разрешение для токена {token_address} успешно установлено.")
                return True
            else:
                print(f"    Ошибка при установке разрешения для токена {token_address} (статус транзакции {receipt.status}).")
                return False
        except Exception as e:
            print(f"    Исключение при установке разрешения для токена {token_address}: {e}")
            return False

    async def _execute_swap(self, token_in_addr: str, token_out_addr: str, amount_in_raw: int, 
                            amount_out_min_raw: int, router_address: str, pool_fee_for_swap: int = 100) -> tuple[bool, str | None]:
        """
        Выполняет свап токенов через Universal Router (скопировано из liquidity_manager.py)
        
        Args:
            token_in_addr: Адрес входящего токена
            token_out_addr: Адрес исходящего токена
            amount_in_raw: Сумма входящего токена в сырых единицах
            amount_out_min_raw: Минимальная сумма исходящего токена
            router_address: Адрес Universal Router
            pool_fee_for_swap: Fee Tier пула для свапа в ppm (100 = 0.01%)
            
        Returns:
            tuple[bool, str|None]: (успех, хеш транзакции)
        """
        wallet_address = self.signer_address
        router_address = Web3.to_checksum_address(router_address)
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
            # Подготавливаем транзакцию с прямым вызовом
            base_gas_price = await self._get_gas_price()
            max_priority_fee = max(100000000, int(base_gas_price * 0.5))  # Минимум 0.1 Gwei
            
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
            
            # Обновляем nonce кэш
            self._update_nonce_cache(tx['nonce'])
            
            print(f"  Транзакция свапа отправлена: {tx_hash.hex()}")
            
            # Ждем подтверждения транзакции
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status == 1:
                print(f"  Свап успешно выполнен. Tx: {tx_hash.hex()}")
                return True, tx_hash.hex()
            else:
                print(f"  Ошибка при выполнении свапа. Tx: {tx_hash.hex()}")
                return False, tx_hash.hex()
        except Exception as e:
            print(f"  Произошла ошибка при выполнении свапа: {e}")
            return False, None

    def _get_human_price_from_raw_sqrt_price_x96(self, sqrt_price_x96: int) -> Decimal:
        """
        Конвертирует sqrtPriceX96 из пула в человекочитаемую цену param_T1/param_T0 (BTCB/USDT).
        Скопировано из liquidity_manager.py.
        """
        if sqrt_price_x96 == 0: 
            raise ValueError("sqrt_price_x96 не может быть равен нулю.")
        
        # raw_val_assuming_t0_per_t1 = (sqrtPriceX96 / 2**96)**2
        # Это P_raw_USDT_per_BTCB (так как pool T0=USDT, T1=BTCB)
        raw_val_interpreted_as_t0_per_t1 = (Decimal(sqrt_price_x96) / Decimal(2**96))**2
        
        if raw_val_interpreted_as_t0_per_t1 == 0:
            raise ValueError("Рассчитанная сырая цена T0/T1 равна нулю.")

        # human_price P_T1/T0 = (1 / P_raw_T0/T1) * 10^(D1 - D0)
        human_price = (Decimal(1) / raw_val_interpreted_as_t0_per_t1) * \
                      (Decimal(10)**(self.token1_decimals - self.token0_decimals))
        return human_price

    async def get_current_pool_state(self) -> tuple[Decimal, int, Decimal]:
        """Получает текущее состояние пула (цена, sqrtPriceX96, ликвидность)"""
        try:
            # Получаем текущие данные о пуле (используем абсолютный путь)
            pool_abi_filename = os.getenv("POOL_ABI_FILENAME", "PancakeswapV3Pool.json")
            pool_abi_path = str(project_root / 'src' / 'abi' / pool_abi_filename)
            
            with open(pool_abi_path, 'r') as f:
                pool_abi = json.load(f)
            
            pool_address = os.getenv("POOL_ADDRESS")
            if not pool_address:
                raise ValueError("POOL_ADDRESS не задан в .env")
                
            pool_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(pool_address), 
                abi=pool_abi
            )
            
            slot0_data = pool_contract.functions.slot0().call()
            sqrt_price_x96 = slot0_data[0]
            current_tick_from_slot0 = slot0_data[1]
            
            # Используем правильную конвертацию из liquidity_manager.py
            human_price = self._get_human_price_from_raw_sqrt_price_x96(sqrt_price_x96)
            
    
            
            return human_price, sqrt_price_x96, Decimal(current_tick_from_slot0)
            
        except Exception as e:
            print(f"Ошибка получения состояния пула: {e}")
            return Decimal("104000"), 0, Decimal("0")  # fallback значения

    async def rebalance_portfolio_1_to_1(self, router_address: str) -> bool:
        """
        Балансирует портфель к соотношению 1:1 по стоимости USDT и BTCB
        (скопировано из liquidity_manager.py)
        """
        print("\n=== БАЛАНСИРОВКА ПОРТФЕЛЯ 1:1 ===")
        
        # Получаем текущую цену
        current_price_human, _, _ = await self.get_current_pool_state()
        
        # Получаем балансы токенов
        wallet_usdt_raw = await self._get_token_balance_raw(self.token0_address)
        wallet_btcb_raw = await self._get_token_balance_raw(self.token1_address)
        
        # Конвертируем в human значения
        wallet_usdt_human = Decimal(wallet_usdt_raw) / (Decimal(10) ** self.token0_decimals)
        wallet_btcb_human = Decimal(wallet_btcb_raw) / (Decimal(10) ** self.token1_decimals)
        
        # Рассчитываем общую стоимость портфеля в USDT
        usdt_value = wallet_usdt_human
        btcb_value_in_usdc = wallet_btcb_human * current_price_human
        total_portfolio_value_usdc = usdt_value + btcb_value_in_usdc
        
        print(f"  Баланс USDT: {wallet_usdt_human:.2f} (${wallet_usdt_human:.2f})")
        print(f"  Баланс BTCB: {wallet_btcb_human:.8f} (${btcb_value_in_usdc:.2f} по курсу ${current_price_human:.2f})")
        print(f"  Общая стоимость портфеля: ${total_portfolio_value_usdc:.2f}")
        
        # Проверяем минимальную сумму портфеля для ребалансировки
        min_portfolio_value_for_rebalance = Decimal("50")  # 50 USDT
        if total_portfolio_value_usdc < min_portfolio_value_for_rebalance:
            print(f"  Портфель слишком мал для ребалансировки (${total_portfolio_value_usdc:.2f} < ${min_portfolio_value_for_rebalance}). Пропускаем.")
            return True
        
        # Рассчитываем текущее соотношение USDT к общей стоимости
        current_usdt_ratio = usdt_value / total_portfolio_value_usdc if total_portfolio_value_usdc > 0 else Decimal("0")
        target_usdt_ratio = Decimal("0.5")  # 50%
        
        print(f"  Текущее соотношение USDT/Всего: {current_usdt_ratio * 100:.2f}% (целевое: 50%)")
        
        # Рассчитываем отклонение от целевого соотношения
        deviation = abs(current_usdt_ratio - target_usdt_ratio)
        rebalance_threshold_pct = Decimal("0.05")  # 5% порог
        
        print(f"  Отклонение от цели: {deviation * 100:.2f}% (порог: {rebalance_threshold_pct * 100:.2f}%)")
        
        # Если отклонение меньше порога, не делаем свап
        if deviation < rebalance_threshold_pct:
            print("  Отклонение меньше порога, ребалансировка не требуется.")
            return True
        
        # Рассчитываем необходимую сумму для свапа
        target_usdt_value = total_portfolio_value_usdc * target_usdt_ratio
        usdt_value_difference = usdt_value - target_usdt_value  # Положительно, если нужно уменьшить USDT
        
        if usdt_value_difference > 0:  # Нужно свапнуть USDT в BTCB
            # Проверяем, что разница достаточно значима
            if usdt_value_difference < 1:
                print("  Рассчитанная разница слишком мала для свапа USDT -> BTCB.")
                return True
            
            usdt_to_swap_human = usdt_value_difference
            usdt_to_swap_raw = int(usdt_to_swap_human * (Decimal(10) ** self.token0_decimals))
            
            print(f"\n  СВАП: USDT -> BTCB")
            print(f"  Сумма для свапа: {usdt_to_swap_human:.6f} USDT")
            
            # Проверяем достаточность баланса
            if usdt_to_swap_raw > wallet_usdt_raw:
                print(f"  Недостаточно USDT для свапа. Требуется: {usdt_to_swap_human}, есть: {wallet_usdt_human}")
                return False
            
            # Устанавливаем минимальное получение с учетом slippage 0.01%
            slippage = Decimal("0.0001")
            expected_cbbtc = usdt_to_swap_human / current_price_human
            btcb_min_raw = int(expected_cbbtc * (Decimal(1) - slippage) * (Decimal(10) ** self.token1_decimals))
            
            print(f"  Ожидаемое получение: {expected_cbbtc:.8f} BTCB")
            print(f"  Минимальное получение: {Decimal(btcb_min_raw) / (Decimal(10) ** self.token1_decimals):.8f} BTCB")
            
            # Выполняем свап
            swap_success, tx_hash = await self._execute_swap(
                self.token0_address,  # USDT
                self.token1_address,  # BTCB
                usdt_to_swap_raw,
                btcb_min_raw,
                router_address,
                FEE_TIER_FOR_SWAP_TRANSACTION
            )
            
            if swap_success:
                print(f"  Свап USDT -> BTCB успешно выполнен. Tx: {tx_hash}")
                return True
            else:
                print(f"  Ошибка при выполнении свапа USDT -> BTCB")
                return False
                
        else:  # Нужно свапнуть BTCB в USDT
            usdt_value_difference = abs(usdt_value_difference)
            
            # Проверяем, что разница достаточно значима
            if usdt_value_difference < 1:
                print("  Рассчитанная разница слишком мала для свапа BTCB -> USDT.")
                return True
            
            btcb_to_swap_human = usdt_value_difference / current_price_human
            btcb_to_swap_raw = int(btcb_to_swap_human * (Decimal(10) ** self.token1_decimals))
            
            print(f"\n  СВАП: BTCB -> USDT")
            print(f"  Сумма для свапа: {btcb_to_swap_human:.8f} BTCB")
            
            # Проверяем достаточность баланса
            if btcb_to_swap_raw > wallet_btcb_raw:
                print(f"  Недостаточно BTCB для свапа. Требуется: {btcb_to_swap_human}, есть: {wallet_btcb_human}")
                return False
            
            # Устанавливаем минимальное получение с учетом slippage 0.5%
            slippage = Decimal("0.005")
            expected_usdc = btcb_to_swap_human * current_price_human
            usdt_min_raw = int(expected_usdc * (Decimal(1) - slippage) * (Decimal(10) ** self.token0_decimals))
            
            print(f"  Ожидаемое получение: {expected_usdc:.6f} USDT")
            print(f"  Минимальное получение: {Decimal(usdt_min_raw) / (Decimal(10) ** self.token0_decimals):.6f} USDT")
            
            # Выполняем свап
            swap_success, tx_hash = await self._execute_swap(
                self.token1_address,  # BTCB
                self.token0_address,  # USDT
                btcb_to_swap_raw,
                usdt_min_raw,
                router_address,
                FEE_TIER_FOR_SWAP_TRANSACTION
            )
            
            if swap_success:
                print(f"  Свап BTCB -> USDT успешно выполнен. Tx: {tx_hash}")
                return True
            else:
                print(f"  Ошибка при выполнении свапа BTCB -> USDT")
                return False
        
        return False

    async def swap_cake_to_usdc(self, router_address: str, cake_address: str) -> bool:
        """
        Свапает все токены CAKE в USDT через PancakeSwap Router
        
        Args:
            router_address: Адрес PancakeSwap Router
            cake_address: Адрес токена CAKE
            
        Returns:
            bool: True в случае успеха
        """
        # Получаем баланс CAKE
        cake_balance_raw = await self._get_token_balance_raw(cake_address)
        if cake_balance_raw == 0:
            print("  Баланс CAKE равен нулю, свап не требуется")
            return True
        
        # CAKE имеет 18 decimals
        cake_decimals = 18
        cake_balance_human = Decimal(cake_balance_raw) / (Decimal(10) ** cake_decimals)
        print(f"  Баланс CAKE: {cake_balance_human:.6f}")
        
        # Минимальная сумма для свапа
        min_cake_for_swap = Decimal("0.001")  # 0.001 CAKE
        if cake_balance_human < min_cake_for_swap:
            print(f"  Баланс CAKE слишком мал для свапа ({cake_balance_human:.6f} < {min_cake_for_swap})")
            return True
        
        # Используем весь баланс CAKE для свапа
        cake_to_swap_raw = cake_balance_raw
        
        # Устанавливаем минимальное получение с учетом slippage 1%
        # Примерная цена CAKE ~$2, но ставим большой slippage для безопасности
        slippage = Decimal("0.01")  # 1%
        estimated_cake_price_usd = Decimal("2")  # Примерная цена CAKE в USD
        expected_usdc = cake_balance_human * estimated_cake_price_usd
        usdt_min_raw = int(expected_usdc * (Decimal(1) - slippage) * (Decimal(10) ** self.token0_decimals))
        
        print(f"  Свапаем {cake_balance_human:.6f} CAKE -> ~{expected_usdc:.2f} USDT (мин: {Decimal(usdt_min_raw) / (Decimal(10) ** self.token0_decimals):.2f})")
        
        # Выполняем свап CAKE -> USDT через fee tier 2500 (0.25%)
        cake_usdt_fee_tier = 2500  # 0.25% fee tier для CAKE/USDT пула
        
        swap_success, tx_hash = await self._execute_swap(
            cake_address,           # CAKE
            self.token0_address,    # USDT
            cake_to_swap_raw,
            usdt_min_raw,
            router_address,
            cake_usdt_fee_tier
        )
        
        if swap_success:
            print(f"  Свап CAKE -> USDT успешно выполнен. Tx: {tx_hash}")
            return True
        else:
            print(f"  Ошибка при выполнении свапа CAKE -> USDT")
            return False


async def main():
    """Пример использования с multicall"""
    
    # Получаем параметры из окружения для BNB Chain
    rpc_url = os.getenv("RPC_URL")
    signer_address = os.getenv("WALLET_ADDRESS")
    private_key = os.getenv("PRIVATE_KEY")
    pool_address = os.getenv("POOL_ADDRESS")
    token0_address = os.getenv("TOKEN_1_ADDRESS")  # USDT на BNB Chain
    token1_address = os.getenv("TOKEN_2_ADDRESS")  # BTCB на BNB Chain
    token0_decimals = 18  # Все токены имеют 18 decimals
    token1_decimals = 18  # Все токены имеют 18 decimals
    fee_tier = int(os.getenv("FEE_TIER", "100"))
    farm_address = os.getenv("MASTERCHEF_V3_ADDRESS")
    farm_abi_path = str(project_root / 'src' / 'abi' / 'MasterChefV3.json')  # Абсолютный путь к ABI фарма
    
    if not all([rpc_url, signer_address, private_key, pool_address, token0_address, token1_address]):
        print("❌ Не все обязательные переменные окружения заданы")
        print(f"RPC_URL: {rpc_url}")
        print(f"WALLET_ADDRESS: {signer_address}")
        print(f"PRIVATE_KEY: {'***' if private_key else None}")
        print(f"POOL_ADDRESS: {pool_address}")
        print(f"TOKEN_1_ADDRESS (USDT): {token0_address}")
        print(f"TOKEN_2_ADDRESS (BTCB): {token1_address}")
        return
    
    try:
        # Создаем экземпляр PositionCloser
        closer = PositionCloser(
            rpc_url=rpc_url,
            signer_address=signer_address,
            private_key=private_key,
            pool_address=pool_address,
            token0_address=token0_address,
            token1_address=token1_address,
            token0_decimals=token0_decimals,
            token1_decimals=token1_decimals,
            fee_tier=fee_tier,
            farm_address=farm_address,
            farm_abi_path=farm_abi_path
        )
        
        # Закрываем все позиции через multicall
        stats = await closer.close_all_positions_multicall()
        
        if stats["closed"] == stats["total"]:
            print("\n🎉 Все позиции успешно закрыты через multicall!")
        else:
            print(f"\n⚠️  Закрыто {stats['closed']} из {stats['total']} позиций")
        
        # Свапаем CAKE в USDT перед балансировкой
        router_address = os.getenv("PANCAKESWAP_ROUTER_ADDRESS")
        if router_address:
            cake_address = os.getenv("CAKE_ADDRESS")
            if cake_address:
                print("\n🍰 Свапаем CAKE в USDT...")
                cake_success = await closer.swap_cake_to_usdc(router_address, cake_address)
                if cake_success:
                    print("✅ CAKE успешно свапнут в USDT!")
                else:
                    print("⚠️  Свап CAKE завершился с ошибками")
            else:
                print("⚠️  CAKE_ADDRESS не задан, пропускаем свап CAKE")
        
        # Балансируем портфель 1:1 (выполняем всегда)
        if router_address:
            print("\n⚖️  Запускаем балансировку портфеля 1:1...")
            # Небольшая дополнительная пауза перед балансировкой
            await asyncio.sleep(5)
            balance_success = await closer.rebalance_portfolio_1_to_1(router_address)
            if balance_success:
                print("✅ Балансировка портфеля завершена успешно!")
            else:
                print("⚠️  Балансировка портфеля завершилась с ошибками")
        else:
            print("⚠️  PANCAKESWAP_ROUTER_ADDRESS не задан, пропускаем балансировку")
            
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main()) 