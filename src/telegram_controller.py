import asyncio
import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# Глобальный флаг управления
liquidity_manager_running = False
# Глобальная ссылка на LiquidityManager
liquidity_manager_instance = None

class TelegramController:
    def __init__(self):
        load_dotenv()
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = int(os.getenv("TELEGRAM_CHAT_ID"))
        self.application = None
        
        # Настройка логирования для telegram
        logging.getLogger("httpx").setLevel(logging.WARNING)
        
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start для запуска управления ликвидностью"""
        global liquidity_manager_running
        
        if update.effective_chat.id != self.chat_id:
            await update.message.reply_text("❌ У вас нет доступа к этому боту")
            return
            
        if liquidity_manager_running:
            await update.message.reply_text("✅ Управление ликвидностью уже запущено")
        else:
            liquidity_manager_running = True
            await update.message.reply_text("🚀 Управление ликвидностью ЗАПУЩЕНО")
            print("📱 Telegram: Управление ликвидностью запущено")
    
    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /stop для остановки управления ликвидности"""
        global liquidity_manager_running
        
        if update.effective_chat.id != self.chat_id:
            await update.message.reply_text("❌ У вас нет доступа к этому боту")
            return
            
        if not liquidity_manager_running:
            await update.message.reply_text("⏹️ Управление ликвидностью уже остановлено")
        else:
            liquidity_manager_running = False
            await update.message.reply_text("⏸️ Управление ликвидностью ОСТАНОВЛЕНО")
            print("📱 Telegram: Управление ликвидностью остановлено")
    
    async def rebalance_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /rebalance для принудительного полного ребаланса всех позиций"""
        global liquidity_manager_instance
        
        if update.effective_chat.id != self.chat_id:
            await update.message.reply_text("❌ У вас нет доступа к этому боту")
            return
        
        if not liquidity_manager_instance:
            await update.message.reply_text("❌ LiquidityManager не инициализирован")
            return
        
        await update.message.reply_text("🔄 Запускаю полный ребаланс всех позиций...")
        print("📱 Telegram: Запущен принудительный полный ребаланс")
        
        try:
            # Получаем текущую цену
            current_price, _, _ = await liquidity_manager_instance.get_current_pool_state()
            
            # Выполняем полный ребаланс
            await liquidity_manager_instance._perform_full_rebalance(current_price)
            
            await update.message.reply_text("✅ Полный ребаланс завершен успешно!")
            print("📱 Telegram: Полный ребаланс завершен успешно")
            
        except Exception as e:
            error_msg = f"❌ Ошибка при ребалансе: {str(e)}"
            await update.message.reply_text(error_msg)
            print(f"📱 Telegram: Ошибка ребаланса - {e}")
    
    async def reset_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /reset для полного сброса всех позиций и состояния"""
        global liquidity_manager_instance
        
        if update.effective_chat.id != self.chat_id:
            await update.message.reply_text("❌ У вас нет доступа к этому боту")
            return
        
        if not liquidity_manager_instance:
            await update.message.reply_text("❌ LiquidityManager не инициализирован")
            return
        
        await update.message.reply_text("🔥 Запускаю полный СБРОС всех позиций...\n⚠️ Все позиции будут закрыты и состояние очищено!")
        print("📱 Telegram: Запущен полный сброс позиций")
        
        try:
            # Закрываем все существующие позиции через multicall
            print("📱 Закрытие всех существующих позиций...")
            positions_to_close = []
            
            # Собираем позиции из managed_positions_slots
            for slot_idx, pos_data in enumerate(liquidity_manager_instance.managed_positions_slots):
                if pos_data and 'nft_id' in pos_data:
                    nft_id = pos_data['nft_id']
                    position_info = await liquidity_manager_instance.get_position_info(nft_id)
                    if position_info and 'error' not in position_info:
                        positions_to_close.append((slot_idx, nft_id, position_info))
                        print(f"  📱 Позиция для закрытия: слот {slot_idx}, NFT {nft_id}")
            
            # Ищем и добавляем осиротевшие позиции
            print("📱 Поиск осиротевших позиций...")
            orphaned_positions = await liquidity_manager_instance.find_orphaned_positions()
            for orphaned_pos in orphaned_positions:
                nft_id = orphaned_pos['nft_id']
                position_info = await liquidity_manager_instance.get_position_info(nft_id)
                if position_info and 'error' not in position_info:
                    positions_to_close.append((-1, nft_id, position_info))  # slot_id = -1 для орфанов
                    print(f"  📱 🚨 Осиротевшая позиция для закрытия: NFT {nft_id}")
                    
            if orphaned_positions:
                await update.message.reply_text(f"🚨 Найдено {len(orphaned_positions)} осиротевших позиций - они тоже будут закрыты!")
            
            if positions_to_close:
                success = await liquidity_manager_instance._execute_remove_liquidity_multicall(positions_to_close)
                if not success:
                    await update.message.reply_text("⚠️ Ошибка при закрытии через multicall, пробую обычный способ...")
                    print("📱 Fallback к обычному закрытию")
                    
                    # Fallback к обычному методу
                    for slot_idx, nft_id, pos_info in positions_to_close:
                        try:
                            await liquidity_manager_instance._execute_remove_liquidity_multicall([(slot_idx, nft_id, pos_info)])
                        except Exception as e:
                            print(f"📱 Ошибка закрытия позиции {nft_id}: {e}")
            
            # Полностью сбрасываем состояние
            liquidity_manager_instance.managed_positions_slots = [None] * len(liquidity_manager_instance.managed_positions_slots)
            
            # Сохраняем очищенное состояние
            liquidity_manager_instance._save_state_to_file()
            
            await update.message.reply_text("✅ Полный сброс завершен!\n\n🔄 Все позиции закрыты, состояние очищено. Цикл управления начнется заново при следующей итерации.")
            print("📱 Telegram: Полный сброс завершен успешно")
            
        except Exception as e:
            error_msg = f"❌ Ошибка при сбросе: {str(e)}"
            await update.message.reply_text(error_msg)
            print(f"📱 Telegram: Ошибка сброса - {e}")
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /status для проверки статуса"""
        if update.effective_chat.id != self.chat_id:
            await update.message.reply_text("❌ У вас нет доступа к этому боту")
            return
        
        status = "🟢 РАБОТАЕТ" if liquidity_manager_running else "🔴 ОСТАНОВЛЕН"
        lm_status = "✅ Инициализирован" if liquidity_manager_instance else "❌ Не инициализирован"
        
        status_text = f"""📊 Статус системы:

🤖 Управление ликвидностью: {status}
🔧 LiquidityManager: {lm_status}

💡 Используйте /help для списка команд"""
        
        await update.message.reply_text(status_text)
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /help со списком доступных команд"""
        if update.effective_chat.id != self.chat_id:
            await update.message.reply_text("❌ У вас нет доступа к этому боту")
            return
            
        help_text = """
🤖 Команды управления ликвидностью:

/start - Запустить управление ликвидностью
/stop - Остановить управление ликвидностью  
/rebalance - Принудительный полный ребаланс всех позиций
/reset - Полный сброс позиций и очистка состояния
/status - Проверить текущий статус
/help - Показать это сообщение

ℹ️ Управление:
• /start /stop - управляют основным циклом автоматических операций
• /rebalance - немедленно закрывает все позиции и создает новые с текущей ценой
• /reset - полностью закрывает позиции и сбрасывает состояние (начало с нуля)
• Скрипт продолжает работать всегда (мониторинг, логи)

⚠️ /reset полностью очищает состояние! Используйте осторожно.
        """
        await update.message.reply_text(help_text.strip())
    
    async def initialize(self):
        """Инициализация бота"""
        if not self.bot_token or not self.chat_id:
            print("❌ TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не настроены в .env")
            return False
            
        try:
            self.application = Application.builder().token(self.bot_token).build()
            
            # Добавляем команды
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("stop", self.stop_command))
            self.application.add_handler(CommandHandler("rebalance", self.rebalance_command))
            self.application.add_handler(CommandHandler("reset", self.reset_command))
            self.application.add_handler(CommandHandler("status", self.status_command))
            self.application.add_handler(CommandHandler("help", self.help_command))
            
            # Инициализируем приложение
            await self.application.initialize()
            await self.application.start()
            
            print(f"📱 Telegram бот инициализирован (Chat ID: {self.chat_id})")
            
            # Отправляем уведомление о запуске
            try:
                await self.application.bot.send_message(
                    chat_id=self.chat_id,
                    text="🤖 Telegram управление запущено!\n\nИспользуйте /help для списка команд"
                )
            except Exception as e:
                print(f"⚠️ Не удалось отправить стартовое сообщение: {e}")
            
            return True
            
        except Exception as e:
            print(f"❌ Ошибка инициализации Telegram бота: {e}")
            return False
    
    async def start_polling(self):
        """Запуск polling для бота"""
        if self.application:
            try:
                await self.application.updater.start_polling(
                    allowed_updates=["message"],
                    drop_pending_updates=True
                )
                print("📱 Telegram бот начал обработку сообщений")
            except Exception as e:
                print(f"❌ Ошибка запуска polling: {e}")
    
    async def stop_bot(self):
        """Остановка бота"""
        if self.application:
            try:
                await self.application.updater.stop()
                await self.application.stop()
                await self.application.shutdown()
                print("📱 Telegram бот остановлен")
            except Exception as e:
                print(f"⚠️ Ошибка при остановке бота: {e}")

# Функция для проверки статуса
def is_liquidity_manager_running():
    """Возвращает True если управление ликвидностью активно"""
    global liquidity_manager_running
    return liquidity_manager_running

# Функция для установки начального статуса
def set_liquidity_manager_status(status: bool):
    """Устанавливает статус управления ликвидностью"""
    global liquidity_manager_running
    liquidity_manager_running = status

# Функция для установки ссылки на LiquidityManager
def set_liquidity_manager_instance(lm_instance):
    """Устанавливает ссылку на экземпляр LiquidityManager"""
    global liquidity_manager_instance
    liquidity_manager_instance = lm_instance 