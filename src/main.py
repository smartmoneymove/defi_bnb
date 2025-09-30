import asyncio
import os
import signal
from dotenv import load_dotenv
from decimal import Decimal, getcontext
from web3 import Web3

# Устанавливаем точность для Decimal глобально
getcontext().prec = 36 

from liquidity_manager import LiquidityManager 
from schedule_manager import ScheduleManager, get_run_mode_choice
from google_sheets_logger import SheetLogger
from pathlib import Path

# Флаг для корректного завершения
shutdown_signal_received = False

# Глобальная переменная для Google Sheets Logger
sheet_logger = None
current_work_row = None

def signal_handler(signum, frame):
    global shutdown_signal_received
    print(f"\nПолучен сигнал {signal.Signals(signum).name}. Завершение работы...")
    shutdown_signal_received = True

def get_strategy_choice():
    """Запрашивает у пользователя выбор стратегии."""
    print("\n=== ВЫБОР СТРАТЕГИИ УПРАВЛЕНИЯ ЛИКВИДНОСТЬЮ ===")
    print("1. Стратегия 3 позиции (текущая)")
    print("   • 3 позиции по 4 тика каждая")
    print("   • Общая ширина: 0.12% (12 тиков)")
    print("   • Центральная + 2 боковые позиции")
    
    print("\n2. Стратегия 2 позиции (новая)")
    print("   • 2 позиции по 4 тика каждая")
    print("   • Общая ширина: 0.08% (8 тиков)")
    print("   • Позиция выше цены + позиция ниже цены")
    print("   • Асимметричная ребалансировка")
    
    while True:
        try:
            choice = input("\nВыберите стратегию (1 или 2): ").strip()
            if choice == "1":
                return 3
            elif choice == "2":
                return 2
            else:
                print("❌ Неверный выбор. Введите 1 или 2.")
        except KeyboardInterrupt:
            print("\nПрограмма прервана пользователем.")
            exit(0)


async def wait_for_next_work_period(schedule_manager):
    """Ожидает следующий рабочий период согласно расписанию"""
    global shutdown_signal_received
    
    next_start = schedule_manager.get_next_work_start()
    if not next_start:
        print("❌ Нет запланированных рабочих периодов в расписании.")
        shutdown_signal_received = True
        return
    
    print(f"⏰ Ожидание следующего рабочего периода: {next_start.strftime('%Y-%m-%d %H:%M UTC')}")
    
    while not shutdown_signal_received and not schedule_manager.is_work_time():
        now = schedule_manager._get_current_utc_time()
        time_left = schedule_manager.format_time_until(next_start)
        
        print(f"⏳ До начала работы: {time_left} (текущее время UTC: {now.strftime('%H:%M:%S')})")
        
        # Спим 60 секунд с проверкой сигнала каждые 5 секунд
        for i in range(12):  # 12 * 5 = 60 секунд
            if shutdown_signal_received:
                return
            await asyncio.sleep(5)
        
        # Обновляем время следующего старта на случай, если оно изменилось
        new_next_start = schedule_manager.get_next_work_start()
        if new_next_start and new_next_start != next_start:
            next_start = new_next_start
            print(f"⏰ Обновлено время следующего рабочего периода: {next_start.strftime('%Y-%m-%d %H:%M UTC')}")
    
    if not shutdown_signal_received and schedule_manager.is_work_time():
        print("🟢 Начинаем рабочий период!")
        schedule_manager.print_schedule_status()
        
        # Логируем начало рабочего периода в Google Sheets
        global sheet_logger, current_work_row
        if sheet_logger:
            try:
                current_work_row = sheet_logger.log_start()
                print(f"📊 Данные о начале периода записаны в Google Sheets (строка {current_work_row})")
            except Exception as e:
                print(f"❌ Ошибка логирования в Google Sheets: {e}")

async def main_loop():
    global shutdown_signal_received, sheet_logger, current_work_row
    load_dotenv()
    
    # Инициализация Google Sheets Logger
    try:
        # Получаем настройки из .env или используем значения по умолчанию
        spreadsheet_id = os.getenv("GOOGLE_SHEETS_ID", "15yp8anIMPNFFqtD5p-YGlg5u8ewUHeDjluKXrCoGrYo")
        wallet_address = os.getenv("WALLET_ADDRESS")
        service_account_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH", "service-account.json")
        
        if not wallet_address:
            print("⚠️ WALLET_ADDRESS не задан в .env, Google Sheets Logger отключен")
            sheet_logger = None
        else:
            sheet_logger = SheetLogger(
                spreadsheet_id=spreadsheet_id,
                wallet_address=wallet_address,
                service_account_path=service_account_path
            )
            print(f"✅ Google Sheets Logger инициализирован: {spreadsheet_id} -> {wallet_address}")
    except Exception as e:
        print(f"⚠️ Не удалось инициализировать Google Sheets Logger: {e}")
        print("📝 Логирование в Google Sheets отключено")
        sheet_logger = None

    # --- Выбор стратегии ---
    num_positions = get_strategy_choice()
    print(f"\n✅ Выбрана стратегия: {num_positions} позиции")

    # --- Выбор режима запуска ---
    run_mode = get_run_mode_choice()
    print(f"\n✅ Выбран режим: {'Немедленный запуск' if run_mode == 'immediate' else 'Работа по расписанию'}")

    # --- Инициализация менеджера расписания ---
    project_root = Path(__file__).resolve().parent.parent
    schedule_file = str(project_root / 'src' / 'schedule.json')
    schedule_manager = ScheduleManager(schedule_file)

    # --- Проверка расписания ---
    if run_mode == "scheduled":
        schedule_manager.print_schedule_status()
        if not schedule_manager.is_work_time():
            print("\n🔴 Сейчас не рабочее время по расписанию.")
            await wait_for_next_work_period(schedule_manager)
            if shutdown_signal_received:
                return
    else:
        # Для режима немедленного запуска логируем начало работы
        print("🟢 Начинаем рабочий период (немедленный режим)!")
        if sheet_logger:
            try:
                current_work_row = sheet_logger.log_start()
                print(f"📊 Данные о начале периода записаны в Google Sheets (строка {current_work_row})")
            except Exception as e:
                print(f"❌ Ошибка логирования в Google Sheets: {e}")

    # --- Загрузка Конфигурации ---
    rpc_url = os.getenv("RPC_URL")
    wallet_address = os.getenv("WALLET_ADDRESS")
    private_key = os.getenv("PRIVATE_KEY")
    pool_address = os.getenv("POOL_ADDRESS")
    
    pool_abi_filename = os.getenv("POOL_ABI_FILENAME", "PancakeswapV3Pool.json")
    pool_abi_path = str(project_root / 'src' / 'abi' / pool_abi_filename)

    # Токены для BNB Chain
    token0_address = os.getenv("TOKEN_1_ADDRESS")  # USDT на BNB Chain
    token1_address = os.getenv("TOKEN_2_ADDRESS")  # BTCB на BNB Chain
    token0_decimals = 18  # Все токены имеют 18 decimals согласно условию задачи
    token1_decimals = 18  # Все токены имеют 18 decimals согласно условию задачи
    token0_symbol = "USDT"
    token1_symbol = "BTCB"
    fee_tier = int(os.getenv("FEE_TIER", "500"))
    swap_pool_fee_tier = int(os.getenv("SWAP_POOL_FEE_TIER_FOR_REBALANCE", "100"))
    
    pancakeswap_router_address = os.getenv("PANCAKESWAP_ROUTER_ADDRESS")
    farm_address = os.getenv("FARM_ADDRESS")
    farm_abi_path = str(project_root / 'src' / 'abi' / 'CakeFarm.json')

    # Стратегия
    if num_positions == 3:
        strategy_params = {
            'num_positions': 3,
            'position_mode': '3_positions',
            'individual_position_width_pct': Decimal('0.0004'),  # 0.04%
            'total_range_width_pct': Decimal('0.0012'),          # 0.12%
            'overlap_pct': Decimal('0.0'),                       # 0% overlap
            'rebalance_threshold_pct': Decimal('0.001'),         # 0.1%
            'central_range_weight': Decimal('1.0'),              # Равные веса
            'side_range_weight': Decimal('1.0'),                 # Равные веса
        }
    else:  # 2 позиции
        strategy_params = {
            'num_positions': 2,
            'position_mode': '2_positions',
            'individual_position_width_pct': Decimal('0.0004'),  # 0.04% каждая позиция
            'total_range_width_pct': Decimal('0.0008'),          # 0.08% общая ширина
            'overlap_pct': Decimal('0.0'),                       # 0% overlap (позиции впритык)
            'rebalance_threshold_pct': Decimal('0.001'),         # 0.1%
            'position_weight': Decimal('1.0'),                   # Равные веса для обеих позиций
        }

    print(f"Инициализация Liquidity Manager...")

    lm = LiquidityManager(
        rpc_url=rpc_url,
        signer_address=wallet_address,
        private_key=private_key,
        pool_address=pool_address, 
        pool_abi_path=pool_abi_path,
        token0_address=token0_address, 
        token1_address=token1_address,
        token0_decimals=token0_decimals, 
        token1_decimals=token1_decimals,
        token0_symbol=token0_symbol,
        token1_symbol=token1_symbol,
        fee_tier=fee_tier,
        strategy_params=strategy_params,
        pancakeswap_router_address=pancakeswap_router_address,
        farm_address=farm_address,
        farm_abi_path=farm_abi_path,
        swap_pool_fee_tier=swap_pool_fee_tier
    )

    print("Liquidity Manager инициализирован.")
    
    # Основной цикл
    iteration = 0
    while not shutdown_signal_received:
        try:
            iteration += 1
            print(f"\n=== Итерация {iteration} ===")
            
            # Проверяем сигнал завершения перед каждой операцией
            if shutdown_signal_received:
                break
            
            # Проверяем расписание (если работаем по расписанию)
            if run_mode == "scheduled":
                if not schedule_manager.is_work_time():
                    print("🔴 Рабочее время закончилось по расписанию.")
                    
                    # Закрываем все позиции
                    await schedule_manager.close_all_positions()
                    
                    # Ждем 3 секунды после балансировки портфеля
                    print("⏳ Ожидание 3 секунды после закрытия позиций...")
                    await asyncio.sleep(3)
                    
                    # Логируем окончание рабочего периода в Google Sheets
                    if sheet_logger and current_work_row:
                        try:
                            sheet_logger.log_finish(current_work_row)
                            print(f"📊 Данные об окончании периода записаны в Google Sheets (строка {current_work_row})")
                            current_work_row = None
                        except Exception as e:
                            print(f"❌ Ошибка логирования окончания в Google Sheets: {e}")
                    
                    await wait_for_next_work_period(schedule_manager)
                    if shutdown_signal_received:
                        break
                    continue
            
            # Получаем текущее состояние пула
            current_price, current_tick, current_liquidity = await lm.get_current_pool_state()
            print(f"Текущее состояние пула: цена={current_price:.2f}, тик={current_tick}, ликвидность={current_liquidity}")
            
            if shutdown_signal_received:
                break
            
            # Управляем ликвидностью (без анализа данных)
            await lm.decide_and_manage_liquidity(None)
            
            if shutdown_signal_received:
                break
            
            # Проверяем статус позиций
            await lm._update_managed_positions_status()
            await lm._print_managed_positions_status()
            
            if shutdown_signal_received:
                break
            
            
            print(f"Итерация {iteration} завершена. Ожидание 60 секунд...")
            
            # Спим с проверкой сигнала каждые 5 секунд
            for i in range(12):  # 12 * 5 = 60 секунд
                if shutdown_signal_received:
                    break
                await asyncio.sleep(5)
            
        except KeyboardInterrupt:
            print("Получен KeyboardInterrupt в основном цикле")
            shutdown_signal_received = True
            break
        except Exception as e:
            print(f"Ошибка в основном цикле: {e}")
            import traceback
            traceback.print_exc()
            if shutdown_signal_received:
                break
            await asyncio.sleep(30)  # Короткая пауза при ошибке
            
    # Логируем окончание работы при завершении программы
    if sheet_logger and current_work_row:
        try:
            sheet_logger.log_finish(current_work_row)
            print(f"📊 Данные об окончании периода записаны в Google Sheets (строка {current_work_row})")
        except Exception as e:
            print(f"❌ Ошибка логирования окончания в Google Sheets: {e}")
    
    print("Главный цикл завершен.")

async def main():
    try:
        # Создаем задачи для обработки сигналов в asyncio
        loop = asyncio.get_running_loop()
        
        def signal_handler_async():
            global shutdown_signal_received
            print(f"\nПолучен сигнал завершения. Останавливаем программу...")
            shutdown_signal_received = True
        
        # Регистрируем обработчики сигналов для asyncio
        for sig in [signal.SIGTERM, signal.SIGINT]:
            loop.add_signal_handler(sig, signal_handler_async)
        
        await main_loop()
        
    except KeyboardInterrupt:
        print("Получен KeyboardInterrupt в main()")
    except Exception as e:
        print(f"Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("Программа завершена.")

if __name__ == "__main__":
    print("Запуск Liquidity Manager...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Программа прервана пользователем.")
    except Exception as e:
        print(f"Ошибка при запуске: {e}")
    finally:
        print("Выход из программы.")