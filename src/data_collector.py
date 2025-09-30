import os
import json
from web3 import Web3
from dotenv import load_dotenv
import pandas as pd
from pathlib import Path
import time # Для возможной задержки между запросами к RPC

load_dotenv()

RPC_URL = os.getenv("RPC_URL")
POOL_ADDRESS = os.getenv("POOL_ADDRESS")
POOL_ABI_PATH = Path(__file__).parent / 'abi' / 'PancakeswapV3Pool.json'

# Имя файла для сохранения состояния
STATE_FILE = Path(__file__).parent / 'data_collector_state.json'
# Основные файлы с данными (куда будем дописывать)
CONSOLIDATED_DATA_DIR = Path('data') # Убедись, что эта папка создается, если ее нет
CONSOLIDATED_SWAPS_CSV = CONSOLIDATED_DATA_DIR / "consolidated_swap_events.csv"
CONSOLIDATED_MINTS_CSV = CONSOLIDATED_DATA_DIR / "consolidated_mint_events.csv"
# Добавь другие типы событий, если нужно

class DataCollector:
    def __init__(self):
        if not RPC_URL or not POOL_ADDRESS:
            raise ValueError("RPC_URL и POOL_ADDRESS должны быть установлены в .env")

        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        if not self.w3.is_connected():
            raise ConnectionError(f"Не удалось подключиться к RPC: {RPC_URL}")

        try:
            with open(POOL_ABI_PATH, 'r') as f:
                pool_abi = json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Файл ABI не найден: {POOL_ABI_PATH}.")
        except json.JSONDecodeError:
            raise ValueError(f"Ошибка декодирования ABI файла: {POOL_ABI_PATH}")

        self.pool_address = Web3.to_checksum_address(POOL_ADDRESS)
        self.pool_contract = self.w3.eth.contract(
            address=self.pool_address,
            abi=pool_abi
        )
        self._block_timestamp_cache = {} # Кэш для временных меток блоков
        CONSOLIDATED_DATA_DIR.mkdir(parents=True, exist_ok=True) # Создаем папку, если ее нет
        print(f"DataCollector инициализирован для пула: {self.pool_address}")

    def _load_last_processed_block(self) -> int:
        if STATE_FILE.exists():
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
                return state.get('last_processed_block', 0)
        return 0 # Если файла нет или нет ключа, начинаем с самого начала (или с блока создания пула)

    def _save_last_processed_block(self, block_number: int):
        with open(STATE_FILE, 'w') as f:
            json.dump({'last_processed_block': block_number}, f)
        print(f"Сохранен последний обработанный блок: {block_number}")

    def _get_block_timestamp(self, block_number: int) -> int | None:
        """
        Получает timestamp для блока, использует кэш.
        """
        if block_number not in self._block_timestamp_cache:
            try:
                block_info = self.w3.eth.get_block(block_number)
                self._block_timestamp_cache[block_number] = block_info.timestamp
            except Exception as e:
                print(f"Ошибка при получении timestamp для блока {block_number}: {e}")
                self._block_timestamp_cache[block_number] = None # или pd.NaT, но для int лучше None
        return self._block_timestamp_cache[block_number]

    async def get_events_in_range(self, event_name: str, start_block: int, end_block: int) -> list:
        """ Получает события и их timestamp'ы в заданном диапазоне. """
        if not hasattr(self.pool_contract.events, event_name):
            raise ValueError(f"Событие {event_name} не найдено в ABI контракта.")
        
        self._block_timestamp_cache = {}
        print(f"  Запрос событий '{event_name}' с блока {start_block} по {end_block}...")
        
        event_filter = self.pool_contract.events[event_name].create_filter(
            fromBlock=start_block,
            toBlock=end_block
        )
        
        try:
            logs = event_filter.get_all_entries()
        except Exception as e:
            print(f"  Ошибка при получении логов для '{event_name}': {e}")
            return []
        
        if not logs:
            return []
        
        unique_block_numbers = sorted(list(set(log.blockNumber for log in logs)))
        for bn in unique_block_numbers:
            self._get_block_timestamp(bn) # Заполняем кэш
            
        processed_logs = []
        for log in logs:
            ts = self._get_block_timestamp(log.blockNumber)
            entry = {
                'blockNumber': log.blockNumber,
                'timestamp_unix': ts if ts is not None else -1,
                'transactionHash': log.transactionHash.hex(),
                'logIndex': log.logIndex,
                'event': log.event
            }
            for key, value in log.args.items():
                entry[key] = value.hex() if isinstance(value, bytes) else value
            processed_logs.append(entry)
        
        print(f"  Найдено {len(processed_logs)} событий '{event_name}'.")
        return processed_logs

    def _append_df_to_csv(self, df_new_events: pd.DataFrame, target_csv_path: Path):
        """ Дописывает DataFrame в существующий CSV или создает новый. """
        if df_new_events.empty:
            return
        
        if target_csv_path.exists():
            # Дописываем без заголовка
            df_new_events.to_csv(target_csv_path, mode='a', header=False, index=False)
            print(f"  Дописано {len(df_new_events)} строк в {target_csv_path.name}")
        else:
            # Создаем новый файл с заголовком
            df_new_events.to_csv(target_csv_path, mode='w', header=True, index=False)
            print(f"  Создан новый файл {target_csv_path.name} с {len(df_new_events)} строками.")

    async def run_incremental_update(self, max_blocks_per_run: int = 10000):
        """
        Выполняет инкрементальное обновление данных: загружает новые события 
        и дописывает их в основные CSV файлы.
        """
        print("\n--- Запуск инкрементального обновления данных ---")
        last_processed_block = self._load_last_processed_block()
        current_block_on_chain = self.w3.eth.block_number
        
        start_block_for_update = last_processed_block + 1
        # Ограничиваем end_block, чтобы не запрашивать слишком много за раз
        end_block_for_update = min(current_block_on_chain, start_block_for_update + max_blocks_per_run - 1)

        if start_block_for_update > end_block_for_update:
            print(f"Нет новых блоков для обработки. Последний обработанный: {last_processed_block}, текущий на цепи: {current_block_on_chain}.")
            return

        print(f"Обновление данных с блока {start_block_for_update} по {end_block_for_update} (текущий на цепи: {current_block_on_chain})")

        event_types_to_consolidate = {
            "Swap": CONSOLIDATED_SWAPS_CSV,
            "Mint": CONSOLIDATED_MINTS_CSV,
            # "Burn": CONSOLIDATED_BURNS_CSV,
            # "Collect": CONSOLIDATED_COLLECTS_CSV,
        }
        
        any_new_data = False
        for event_name, target_csv in event_types_to_consolidate.items():
            new_event_logs = await self.get_events_in_range(event_name, start_block_for_update, end_block_for_update)
            if new_event_logs:
                df_new = pd.DataFrame(new_event_logs)
                self._append_df_to_csv(df_new, target_csv)
                any_new_data = True
        
        if any_new_data:
            self._save_last_processed_block(end_block_for_update)
        else:
            # Если данных не было, но мы проверяли диапазон, все равно обновим last_processed_block,
            # чтобы не проверять эти пустые блоки снова.
            if end_block_for_update >= start_block_for_update:
                self._save_last_processed_block(end_block_for_update)
            print("Новых данных для указанных событий не найдено в диапазоне.")
        
        print("--- Инкрементальное обновление данных завершено ---")

    # Оставляем исходный метод get_events для обратной совместимости
    async def get_events(self, event_name: str, start_block: int, end_block: int = 'latest'):
        """
        Получает события для указанного имени события из контракта пула,
        включая UNIX timestamp блока.
        """
        if not hasattr(self.pool_contract.events, event_name):
            raise ValueError(f"Событие {event_name} не найдено в ABI контракта.")

        # Сбрасываем кэш таймстемпов для каждого нового вызова get_events,
        # чтобы кэш не рос бесконечно при длительной работе бота.
        # Для разового скрипта можно и не сбрасывать.
        self._block_timestamp_cache = {}

        event_filter = self.pool_contract.events[event_name].create_filter(
            fromBlock=start_block,
            toBlock=end_block
        )
        
        try:
            logs = event_filter.get_all_entries()
        except Exception as e:
            print(f"Ошибка при получении логов для события {event_name} с блока {start_block} по {end_block}: {e}")
            return []

        processed_logs = []
        if not logs:
            print(f"Событий '{event_name}' не найдено с блока {start_block} по {end_block}.")
            return []

        # Оптимизация: сначала получим все уникальные номера блоков из логов
        unique_block_numbers_in_logs = sorted(list(set(log.blockNumber for log in logs)))
        print(f"Запрос timestamp для {len(unique_block_numbers_in_logs)} уникальных блоков в диапазоне событий '{event_name}'...")
        for i, block_num in enumerate(unique_block_numbers_in_logs):
            self._get_block_timestamp(block_num) # Заполняем кэш
            if (i + 1) % 100 == 0:
                print(f"Получен timestamp для {i+1}/{len(unique_block_numbers_in_logs)} уникальных блоков.")
                # time.sleep(0.1) # Небольшая задержка, если RPC чувствителен к частым запросам

        print("Обработка логов и добавление timestamp...")
        for log in logs:
            block_ts = self._get_block_timestamp(log.blockNumber)
            
            processed_log = {
                'blockNumber': log.blockNumber,
                'timestamp_unix': block_ts if block_ts is not None else -1, # -1 или None как маркер ошибки
                'transactionHash': log.transactionHash.hex(),
                'logIndex': log.logIndex,
                'event': log.event,
            }
            for key, value in log.args.items():
                if isinstance(value, bytes):
                    processed_log[key] = value.hex() 
                else:
                    processed_log[key] = value
            processed_logs.append(processed_log)
        
        print(f"Найдено и обработано {len(processed_logs)} событий '{event_name}' с блока {start_block} по {end_block}.")
        return processed_logs

    async def get_all_pool_events(self, start_block: int, end_block: int = 'latest', data_dir: str = 'data'):
        """
        Собирает все ключевые события пула (Swap, Mint, Burn, Collect)
        и сохраняет их в CSV файлы. Timestamp блока теперь включен.
        """
        event_names = ["Swap", "Mint", "Burn", "Collect"]
        all_events_data = {}
        Path(data_dir).mkdir(parents=True, exist_ok=True)

        for event_name in event_names:
            print(f"Сбор события: {event_name}...")
            # Кэш таймстемпов будет управляться внутри get_events для каждого типа событий
            events = await self.get_events(event_name, start_block, end_block)
            if events:
                df = pd.DataFrame(events)
                # Преобразуем Unix timestamp в datetime для удобства, если нужно, но для CSV Unix лучше
                # df['timestamp'] = pd.to_datetime(df['timestamp_unix'], unit='s', errors='coerce')
                file_path = Path(data_dir) / f"{event_name.lower()}_events_{start_block}_{end_block}.csv"
                df.to_csv(file_path, index=False)
                print(f"События '{event_name}' сохранены в {file_path}")
                all_events_data[event_name] = df
            # else: # Сообщение об отсутствии событий уже есть в get_events
            #     print(f"Событий '{event_name}' не найдено или произошла ошибка.")
        
        return all_events_data

async def main():
    collector = DataCollector()
    # При первом запуске после пакетного сбора, начинаем с блока 30121321
    if collector._load_last_processed_block() == 0:
        collector._save_last_processed_block(30121321)
    
    print("\n=== Запуск постоянного сбора данных до текущего блока ===")
    print("Для остановки нажмите Ctrl+C")
    
    try:
        while True:
            # Проверяем, есть ли новые блоки для обработки
            last_processed = collector._load_last_processed_block()
            current_block = collector.w3.eth.block_number
            blocks_remaining = current_block - last_processed
            
            if blocks_remaining > 0:
                print(f"\nОбнаружено {blocks_remaining} новых блоков для обработки.")
                # Обрабатываем блоки порциями по 2000 за раз
                max_blocks_per_batch = 2000
                batches_needed = (blocks_remaining + max_blocks_per_batch - 1) // max_blocks_per_batch
                
                print(f"Будет выполнено {batches_needed} итераций сбора данных.")
                
                for i in range(batches_needed):
                    print(f"\nИтерация {i+1}/{batches_needed}:")
                    await collector.run_incremental_update(max_blocks_per_run=max_blocks_per_batch)
                    
                print("\nВсе новые блоки успешно обработаны.")
            else:
                print(f"\nНет новых блоков для обработки. Последний обработанный: {last_processed}, текущий: {current_block}.")
            
            # Пауза перед следующей проверкой новых блоков
            print("Пауза 60 секунд перед следующей проверкой новых блоков...")
            await asyncio.sleep(60)
            
    except KeyboardInterrupt:
        print("\n\n=== Сбор данных остановлен пользователем ===")
    except Exception as e:
        print(f"\nПроизошла ошибка при сборе данных: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    import asyncio
    # Меняем вызов с main_test_batch_collection на main
    asyncio.run(main())